"""Денежный охранник: траты, резерв, остановка и гонка потоков."""

import json
import threading

import pytest

from custom_tools.storybook import video_budget as vb
from custom_tools.storybook import video_run_config as cfg


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("ROUTERAI_VIDEO_COST_RUB", "STORYBOOK_VIDEO_COST_RUB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "no_pricing.yaml"))
    cfg.bind_run_project(None)
    cfg.set_run_budget_fallback("demo", 0)
    base = tmp_path / "demo"
    (base / "97_shots").mkdir(parents=True)
    yield base
    cfg.set_run_budget_fallback("demo", 0)


def set_budget(base, value):
    (base / ".studio").mkdir(exist_ok=True)
    (base / ".studio" / "run_settings.json").write_text(json.dumps({"budget_rub": value}))


def write_jobs(base, jobs):
    (base / "97_shots" / "provider_jobs.json").write_text(json.dumps({"jobs": jobs}))


def reserve(base, key="1-1"):
    return vb.reserve_budget(base, "routerai", "m", 5, "720p", key)


def test_zero_budget_does_nothing_and_reads_nothing(project):
    write_jobs(project, [])
    (project / "97_shots" / "provider_jobs.json").write_text("не json")
    with reserve(project) as reservation:
        assert reservation == {}
    assert vb.read_report(project) is None


def test_enough_budget_returns_reservation(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    set_budget(project, 25)
    with reserve(project) as reservation:
        assert reservation == {"reserved_cost_rub": 10.0, "cost_price_source": "env"}


def test_exceeded_writes_report(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    set_budget(project, 15)
    write_jobs(project, [{"shot_key": "1-1", "input_hash": "a", "status": "submitting", "reserved_cost_rub": 10.0}])
    with pytest.raises(vb.BudgetExceededError) as exc:
        with reserve(project, "1-2"):
            pass
    assert exc.value.code == "budget_exceeded"
    report = vb.read_report(project)
    assert report["code"] == "budget_exceeded"
    assert report["budget_rub"] == 15 and report["spent_rub"] == 10 and report["next_price_rub"] == 10
    assert report["remaining_shot_keys"] == ["1-2"]
    assert report["provider"] == "routerai"


def test_unknown_price_with_budget_is_refused(project):
    set_budget(project, 100)
    with pytest.raises(vb.BudgetExceededError) as exc:
        with reserve(project):
            pass
    assert exc.value.code == "budget_price_unknown"
    assert vb.read_report(project)["next_price_rub"] is None


def test_actual_rub_cost_beats_reserve_and_other_currency_uses_reserve():
    jobs = [
        {"task_id": "t1", "status": "downloaded", "cost": 7.0, "currency": "RUB", "reserved_cost_rub": 20.0},
        {"task_id": "t2", "status": "downloaded", "cost": 3.0, "currency": "USD", "reserved_cost_rub": 12.0},
    ]
    spent = vb.spent_rub(jobs)
    assert spent == {"total": 19.0, "actual": 7.0, "reserved": 12.0}


def test_submitting_without_task_id_counts_but_prepared_and_unsent_failure_do_not():
    jobs = [
        {"shot_key": "1", "input_hash": "a", "status": "submitting", "reserved_cost_rub": 5.0},
        {"shot_key": "2", "input_hash": "b", "status": "prepared", "reserved_cost_rub": 5.0},
        {"shot_key": "3", "input_hash": "c", "status": "failed", "reserved_cost_rub": 5.0},
        {"shot_key": "4", "input_hash": "d", "status": "failed", "reserved_cost_rub": 5.0,
         "timestamps": {"submitting_at": "x"}},
    ]
    assert vb.spent_rub(jobs)["total"] == 10.0


def test_repeat_of_same_unsent_request_counts_once():
    jobs = [{"shot_key": "1", "input_hash": "a", "status": "submitting", "reserved_cost_rub": 5.0}] * 3
    assert vb.spent_rub(jobs)["total"] == 5.0


def test_budget_is_reread_each_time(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    set_budget(project, 5)
    with pytest.raises(vb.BudgetExceededError):
        with reserve(project):
            pass
    set_budget(project, 50)
    with reserve(project) as reservation:
        assert reservation["reserved_cost_rub"] == 10.0
    # после успешной отправки отчёт об остановке переносится в resolved
    assert vb.read_report(project) is None
    assert (project / "96_video_contract" / "budget_report.resolved.json").is_file()


def test_fallback_budget_without_file(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    cfg.set_run_budget_fallback("demo", 5)
    with pytest.raises(vb.BudgetExceededError):
        with reserve(project):
            pass


def test_race_four_threads_budget_for_two(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    set_budget(project, 25)
    jobs_path = project / "97_shots" / "provider_jobs.json"
    jobs_path.write_text(json.dumps({"jobs": []}))
    barrier = threading.Barrier(4)
    outcomes = []

    def worker(n):
        barrier.wait(timeout=5)
        try:
            with reserve(project, f"1-{n}") as reservation:
                data = json.loads(jobs_path.read_text())
                data["jobs"].append({"shot_key": f"1-{n}", "input_hash": str(n), "status": "submitting", **reservation})
                jobs_path.write_text(json.dumps(data))
            outcomes.append("ok")
        except vb.BudgetExceededError:
            outcomes.append("stopped")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["ok", "ok", "stopped", "stopped"]
