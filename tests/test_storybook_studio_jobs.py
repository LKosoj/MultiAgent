"""Фоновые задания studio: состояние в файле, опрос, зависшие задания."""

import json
import threading
import time

import pytest

from custom_tools.storybook.studio import jobs
from custom_tools.storybook.studio.errors import StudioError


def wait_state(root, job_id, states=("done", "error", "interrupted"), timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = jobs.read_job(root, job_id)
        if record["state"] in states:
            return record
        time.sleep(0.02)
    raise AssertionError("задание не завершилось")


def test_running_to_done_with_result_and_progress(tmp_path):
    gate = threading.Event()

    def work(progress):
        progress(1, 3, "шаг")
        gate.wait(5)
        return {"value": 7}

    job_id = jobs.start_job(tmp_path, "demo", "p1", work)
    assert len(job_id) == 32
    first = jobs.read_job(tmp_path, job_id)
    assert first["state"] == "running" and first["project_id"] == "p1" and first["kind"] == "demo"
    gate.set()
    done = wait_state(tmp_path, job_id)
    assert done["state"] == "done" and done["result"] == {"value": 7}


def test_studio_error_and_generic_exception(tmp_path):
    def fail(progress):
        raise StudioError("locked", "Занято")

    def crash(progress):
        raise RuntimeError("секретная трассировка")

    err = wait_state(tmp_path, jobs.start_job(tmp_path, "d", None, fail))
    assert err["state"] == "error" and err["error"] == {"code": "locked", "message": "Занято"}
    internal = wait_state(tmp_path, jobs.start_job(tmp_path, "d", None, crash))
    assert internal["error"]["code"] == "internal" and "секретная" not in json.dumps(internal, ensure_ascii=False)


def test_interrupted_dead_pid_and_stale(tmp_path, monkeypatch):
    job_id = "b" * 32
    folder = tmp_path / ".studio_jobs"
    folder.mkdir()
    record = {"job_id": job_id, "kind": "k", "project_id": None, "state": "running",
              "progress": {"current": 0, "total": 0, "message": ""}, "pid": 2 ** 22 + 12345,
              "created_at": time.time(), "updated_at": time.time()}
    (folder / f"{job_id}.json").write_text(json.dumps(record))
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    assert jobs.read_job(tmp_path, job_id)["state"] == "interrupted"
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    assert jobs.read_job(tmp_path, job_id)["state"] == "running"
    monkeypatch.setattr(jobs, "_now", lambda: time.time() + jobs.STALE_AFTER_S + 5)
    assert jobs.read_job(tmp_path, job_id)["state"] == "interrupted"


def test_bad_job_id_and_missing(tmp_path):
    for bad in ("../x", "abc", "G" * 32, 5):
        with pytest.raises(StudioError) as err:
            jobs.read_job(tmp_path, bad)
        assert err.value.code == "invalid_argument"
    with pytest.raises(StudioError) as err:
        jobs.read_job(tmp_path, "c" * 32)
    assert err.value.code == "not_found"


def test_reap_old_jobs(tmp_path):
    job_id = jobs.start_job(tmp_path, "k", None, lambda progress: {})
    wait_state(tmp_path, job_id)
    jobs.reap_old_jobs(tmp_path, max_age_s=3600)
    assert jobs.read_job(tmp_path, job_id)["state"] == "done"
    jobs.reap_old_jobs(tmp_path, max_age_s=-1)
    with pytest.raises(StudioError):
        jobs.read_job(tmp_path, job_id)


def test_at_most_two_run_in_parallel(tmp_path):
    lock, current, peak = threading.Lock(), [0], [0]
    gate = threading.Event()

    def work(progress):
        with lock:
            current[0] += 1
            peak[0] = max(peak[0], current[0])
        gate.wait(5)
        with lock:
            current[0] -= 1
        return {}

    ids = [jobs.start_job(tmp_path, "k", None, work) for _ in range(4)]
    deadline = time.time() + 5
    while current[0] < 2 and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)  # третий и четвёртый успели бы стартовать, если бы лимита не было
    assert peak[0] == 2
    gate.set()
    for job_id in ids:
        assert wait_state(tmp_path, job_id)["state"] == "done"
    assert peak[0] == 2


def test_active_project_ids(tmp_path):
    gate = threading.Event()
    job_id = jobs.start_job(tmp_path, "create_from_prompt", "p9", lambda progress: gate.wait(5) and {})
    assert jobs.active_project_ids(tmp_path, "create_from_prompt") == {"p9"}
    assert jobs.active_project_ids(tmp_path, "other") == set()
    gate.set()
    wait_state(tmp_path, job_id)
    assert jobs.active_project_ids(tmp_path, "create_from_prompt") == set()


def test_start_job_reaps_old_finished_jobs(tmp_path):
    old = jobs.start_job(tmp_path, "demo", "p1", lambda progress: {})
    wait_state(tmp_path, old)
    path = tmp_path / ".studio_jobs" / f"{old}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = time.time() - 2 * 86400
    path.write_text(json.dumps(record), encoding="utf-8")
    fresh = jobs.start_job(tmp_path, "demo", "p1", lambda progress: {})
    wait_state(tmp_path, fresh)
    assert not path.exists() and (tmp_path / ".studio_jobs" / f"{fresh}.json").exists()


def test_read_job_survives_finish_between_read_and_check(tmp_path, monkeypatch):
    """Поток успел записать «done» и выйти из _active между чтением файла и проверкой: это не «interrupted»."""
    import json
    from custom_tools.storybook.studio import jobs

    job_id = "a" * 32
    record = {"job_id": job_id, "kind": "x", "project_id": "p", "state": "running", "pid": __import__("os").getpid(),
              "progress": {}, "created_at": 1.0, "updated_at": 1.0}
    jobs._write(tmp_path, record)
    real = jobs._effective

    def finish_first(rec):
        jobs._write(tmp_path, {**record, "state": "done", "result": {"ok": 1}, "updated_at": 2.0})
        return real(rec)

    monkeypatch.setattr(jobs, "_effective", finish_first)
    got = jobs.read_job(tmp_path, job_id)
    assert got["state"] == "done" and got["result"] == {"ok": 1}



def test_job_is_active_before_running_file_is_written(tmp_path, monkeypatch):
    """Между записью файла «running» и появлением в _active read_job не должен видеть «interrupted»."""
    from custom_tools.storybook.studio import jobs

    seen = {}
    real_write = jobs._write

    def spy(root, record):
        if record["state"] == "running" and "first" not in seen:
            seen["first"] = True
            seen["active"] = record["job_id"] in jobs._active
            seen["state"] = jobs.read_job(root, record["job_id"])["state"] if (
                jobs._job_path(root, record["job_id"]).exists()) else None
        real_write(root, record)

    monkeypatch.setattr(jobs, "_write", spy)
    job_id = jobs.start_job(tmp_path, "x", "p", lambda progress: {"ok": 1})
    assert seen["active"] is True
    deadline = __import__("time").time() + 5
    while jobs.read_job(tmp_path, job_id)["state"] == "running" and __import__("time").time() < deadline:
        pass
    assert jobs.read_job(tmp_path, job_id)["state"] == "done"


def test_start_job_write_failure_leaves_no_active_garbage(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import jobs

    before = set(jobs._active)

    def boom(root, record):
        raise OSError("disk")

    monkeypatch.setattr(jobs, "_write", boom)
    with pytest.raises(OSError):
        jobs.start_job(tmp_path, "x", "p", lambda progress: {})
    assert set(jobs._active) == before
