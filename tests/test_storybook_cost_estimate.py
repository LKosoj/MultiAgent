"""Оценка стоимости прогона: неизвестная цена никогда не равна нулю."""

import json

import pytest

from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.studio import cost_estimate as ce
from custom_tools.storybook.studio import run_settings as rs


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "ROUTERAI_VIDEO_COST_RUB", "ROUTERAI_VIDEO_MODEL", "AITUNNEL_VIDEO_COST_RUB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m")
    cfg.bind_run_project(None)
    base = tmp_path / "demo"
    (base / "97_shots").mkdir(parents=True)
    return base


def shots(project, n, timing="5s"):
    items = [{"scene_number": 1, "shot_number": i + 1, "shot_type": "start", "timing": timing} for i in range(n)]
    (project / "97_shots" / "shots.json").write_text(json.dumps(items))


def estimate(budget=0, **kw):
    values = rs.effective_values(dict(rs.default_settings(), budget_rub=budget), "demo")
    return ce.build_estimate("demo", values, **kw)


def comp(result, cid):
    return next(c for c in result["components"] if c["id"] == cid)


def test_unknown_price_is_none_not_zero(project):
    shots(project, 3)
    result = estimate()
    video = comp(result, "video")
    assert video["amount_rub"] is None and video["unit_price_rub"] is None
    assert video["units"] == 3
    assert "video" in result["unknown_components"] and result["complete"] is False
    assert result["total_known_rub"] == 0.0
    assert any(w["code"] == "price_unknown" for w in result["warnings"])


def test_env_price_per_clip(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 3)
    video = comp(estimate(), "video")
    assert video["amount_rub"] == 30.0 and video["price_source"] == "env" and video["price_unit"] == "per_clip"


def test_text_llm_always_unknown(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 1)
    result = estimate()
    assert comp(result, "text_llm")["amount_rub"] is None
    assert result["complete"] is False  # текстовые модели не оцениваются


def test_verdicts(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 3)
    assert estimate(0)["budget_verdict"] == "no_budget"
    short = estimate(20)
    assert short["budget_verdict"] == "short" and short["short_by_rub"] == 10.0
    assert estimate(1000)["budget_verdict"] == "cannot_check"  # часть компонентов неизвестна
    monkeypatch.delenv("ROUTERAI_VIDEO_COST_RUB")
    assert estimate(1000)["budget_verdict"] == "cannot_check"


def test_no_shots_gives_rough_estimate_with_warning(project):
    result = estimate()
    video = comp(result, "video")
    assert video["basis"] == "rough" and video["units"] >= 1
    assert any(w["code"] == "shots_not_built" for w in result["warnings"])


def test_shot_keys_limit_units(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 4)
    video = comp(estimate(shot_keys=["1-2", "1-3"]), "video")
    assert video["units"] == 2 and video["amount_rub"] == 20.0


def test_steps_filter_and_toggles(project):
    shots(project, 2)
    ids = {c["id"] for c in estimate(step_ids=["music_generator"])["components"]}
    assert "video" not in ids and "text_llm" in ids
    values = rs.effective_values(dict(rs.default_settings()), "demo")
    values["music"] = values["sfx"] = False
    ids = {c["id"] for c in ce.build_estimate("demo", values)["components"]}
    assert "music" not in ids and "sfx" not in ids


def test_cost_estimate_get_returns_estimate(project):
    shots(project, 2)
    result = rs.cost_estimate_get("demo")
    assert result["status"] == "success", result
    assert result["currency"] == "RUB" or result.get("estimate", {}).get("currency") == "RUB"


def test_verdict_counts_already_spent(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 2)
    (project / "97_shots" / "provider_jobs.json").write_text(json.dumps({"jobs": [
        {"shot_key": "9-9", "status": "downloaded", "task_id": "x", "cost": 25.0, "currency": "RUB",
         "input_hash": "h", "model": "other", "output_path": "none"}]}))
    result = estimate(40)  # 25 потрачено + 20 осталось > 40
    assert result["spent_rub"] == 25.0
    assert result["budget_verdict"] == "short" and result["short_by_rub"] == 5.0


def test_env_price_is_marked_as_assumption(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 2)
    video = comp(estimate(), "video")
    assert video["basis"] == "rough" and "допущение: рублей за ролик" in video["note"]


def test_unknown_duration_shot_is_excluded_and_marks_incomplete(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 2)
    data = json.loads((project / "97_shots" / "shots.json").read_text())
    data[1]["timing"] = "???"
    (project / "97_shots" / "shots.json").write_text(json.dumps(data))
    monkeypatch.setattr(ce, "_seconds", lambda item: None if item["shot_number"] == 2 else 5.0)
    result = estimate()
    video = comp(result, "video")
    assert video["incomplete"] is True and video["unknown_duration_shots"] == 1
    assert video["seconds"] == 5.0 and video["amount_rub"] == 10.0
    assert "video" in result["unknown_components"] and result["complete"] is False


def test_zero_units_cost_zero_even_with_unknown_price(project):
    shots(project, 2)
    video = comp(estimate(shot_keys=["7-7"]), "video")
    assert video["units"] == 0 and video["amount_rub"] == 0.0


def test_enough_ignores_text_llm(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    shots(project, 2)
    result = estimate(100, step_ids=["video_generator"])
    assert result["budget_verdict"] == "enough"
    assert result["unknown_components"] == ["text_llm"] and result["complete"] is False
    assert comp(result, "text_llm")["amount_rub"] is None
