"""Ф10: artifact_save (обычный путь, shots-слияние, прогон, гонки, права)."""

import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from backend.fastapi_app.agui import _storybook_actions as actions
from backend.fastapi_app.agui.auth import Principal
from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock
from custom_tools.storybook.studio import artifacts as art
from custom_tools.storybook.studio import capabilities as caps

FIXTURES = Path(__file__).parent / "fixtures" / "storybook_artifacts"


def _fixture(key):
    return json.loads((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    art._SNAPSHOTS.clear()  # кэш снимков shots живёт в процессе
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    series = make_series_project(tmp_path)
    with series_scope("demo", "main"):
        yield series


def _call(cap, **payload):
    return caps.invoke(cap, {"project_id": "demo", "series_id": "main", **payload})


def _path(project, key):
    return project / art.ARTIFACTS[key].path


def _seed(project, key, data=None):
    path = _path(project, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data if data is not None else _fixture(key), ensure_ascii=False, indent=2), encoding="utf-8")
    return art.file_digest(path.read_bytes())


def test_create_new_file_and_parent_dir(project):
    result = _call("artifact_save", key="synopsis", base_digest=None, content=_fixture("synopsis"))
    assert result["status"] == "success" and result["saved"] is True and result["version_id"] is None
    assert _path(project, "synopsis").is_file() and result["digest"] == art.obj_digest(_fixture("synopsis"))
    again = _call("artifact_save", key="synopsis", base_digest=None, content=_fixture("synopsis"))
    assert again["error"]["code"] == "conflict" and again["error"]["details"]["current_digest"] == result["digest"]


def test_regular_save_snapshots_previous_and_leaves_no_tmp(project):
    old_digest = _seed(project, "brief")
    old_bytes = _path(project, "brief").read_bytes()
    data = _fixture("brief")
    data["title"] = "Кириллица читаема"
    result = _call("artifact_save", key="brief", base_digest=old_digest, content=data)
    assert result["status"] == "success" and result["previous_digest"] == old_digest and result["digest"] != old_digest
    snap = project / ".studio" / "artifact_history" / "brief" / f"{result['version_id']}.json"
    assert snap.read_bytes() == old_bytes
    assert not list(project.rglob("*.tmp"))
    assert result["validation"]["counts"] == {"errors": 0, "warnings": 0}


def test_conflict_when_changed_outside(project):
    digest = _seed(project, "brief")
    outside = _fixture("brief")
    outside["title"] = "Чужая правка"
    _path(project, "brief").write_text(json.dumps(outside), encoding="utf-8")
    on_disk = _path(project, "brief").read_bytes()
    result = _call("artifact_save", key="brief", base_digest=digest, content=_fixture("brief"))
    err = result["error"]
    assert err["code"] == "conflict" and err["details"]["current_digest"] == art.file_digest(on_disk)
    assert "Обновите" in err["message"] and _path(project, "brief").read_bytes() == on_disk


def test_formatting_indent_unicode_and_key_order(project):
    data = {"title": "Я", "language": "ru", "description": "д", "pages_min": 1, "pages_max": 2,
            "main_characters": [], "main_locations": [], "zeta": 1, "alpha": 2}
    _call("artifact_save", key="brief", base_digest=None, content=data)
    raw = _path(project, "brief").read_text(encoding="utf-8")
    assert raw == json.dumps(data, indent=2, ensure_ascii=False)
    assert list(json.loads(raw)) == list(data)


def test_text_and_exactly_one_input(project):
    ok = _call("artifact_save", key="synopsis", base_digest=None, text=json.dumps(_fixture("synopsis"), ensure_ascii=False))
    assert ok["status"] == "success"
    broken = _call("artifact_save", key="synopsis", base_digest=ok["digest"], text='{"a": ')
    assert broken["error"]["code"] == "invalid_argument" and broken["error"]["details"]["errors"][0]["code"] == "syntax"
    both = _call("artifact_save", key="synopsis", base_digest=ok["digest"], text="{}", content={})
    none = _call("artifact_save", key="synopsis", base_digest=ok["digest"])
    assert both["error"]["code"] == "invalid_argument" and none["error"]["code"] == "invalid_argument"


def test_schema_errors_block_and_allow_invalid_bypasses(project):
    bad = _fixture("brief")
    del bad["title"]
    blocked = _call("artifact_save", key="brief", base_digest=None, content=bad)
    assert blocked["error"]["code"] == "invalid_argument"
    assert blocked["error"]["details"]["errors"][0]["pointer"] == "/title" and not _path(project, "brief").exists()
    forced = _call("artifact_save", key="brief", base_digest=None, content=bad, allow_invalid=True)
    assert forced["status"] == "success" and forced["validation"]["counts"]["errors"] == 1
    assert "title" not in json.loads(_path(project, "brief").read_text(encoding="utf-8"))


def test_root_type_cannot_be_bypassed(project):
    result = _call("artifact_save", key="brief", base_digest=None, content=[], allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument" and result["error"]["details"]["errors"][0]["code"] == "root"
    assert not _path(project, "brief").exists()


def test_read_only_artifact_is_unsupported(project):
    digest = _seed(project, "provider_jobs")
    before = _path(project, "provider_jobs").read_bytes()
    result = _call("artifact_save", key="provider_jobs", base_digest=digest, content=_fixture("provider_jobs"))
    assert result["error"]["code"] == "unsupported" and result["error"]["details"]["reason"]
    assert _path(project, "provider_jobs").read_bytes() == before


def test_running_pipeline_locks_files_with_writers_but_not_shots(project, monkeypatch):
    brief_digest = _seed(project, "brief")
    shots_digest = _seed(project, "shots")
    handle = acquire_storybook_project_lock("demo")
    try:
        locked = _call("artifact_save", key="brief", base_digest=brief_digest, content=_fixture("brief"))
        assert locked["error"]["code"] == "locked" and "Идёт прогон" in locked["error"]["message"]
        shots = _fixture("shots")
        shots["items"][0]["seed"] = 99
        ok = _call("artifact_save", key="shots", base_digest=shots_digest, content=shots)
        assert ok["status"] == "success" and ok["warnings"][0]["code"] == "run_active"
        # шаг известен: занят только файл, который пишет текущий шаг
        monkeypatch.setattr(art, "_run_info", lambda pid: {"active": True, "current_steps": ["story_planner"]})
        assert _call("artifact_save", key="synopsis", base_digest=None, content=_fixture("synopsis"))["error"]["code"] == "locked"
        free = _call("artifact_save", key="brief", base_digest=brief_digest, content=_fixture("brief"))
        assert free["status"] == "success" and free["warnings"][0]["code"] == "run_active"
    finally:
        release_storybook_project_lock(handle)


def test_busy_is_rechecked_under_lock(project, monkeypatch):
    digest = _seed(project, "brief")
    answers = iter([{"active": False, "current_steps": None}, {"active": True, "current_steps": ["brief_from_prompt"]}])
    monkeypatch.setattr(art, "_run_info", lambda pid: next(answers))
    before = _path(project, "brief").read_bytes()
    result = _call("artifact_save", key="brief", base_digest=digest, content=_fixture("brief"))
    assert result["error"]["code"] == "locked" and _path(project, "brief").read_bytes() == before


def test_two_threads_same_digest_one_wins(project):
    digest = _seed(project, "brief")
    results = []
    barrier = threading.Barrier(2)

    def worker(title):
        data = _fixture("brief")
        data["title"] = title
        barrier.wait()
        results.append(_call("artifact_save", key="brief", base_digest=digest, content=data))

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("А", "Б")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["status"] for r in results) == ["error", "success"]
    assert [r for r in results if r["status"] == "error"][0]["error"]["code"] == "conflict"
    json.loads(_path(project, "brief").read_text(encoding="utf-8"))


def _proc_worker(root, digest, title, queue):
    os.environ["STORYBOOK_PROJECTS_DIR"] = root
    from custom_tools.storybook.studio import capabilities as c

    data = json.loads((FIXTURES / "brief.json").read_text(encoding="utf-8"))
    data["title"] = title
    queue.put(c.invoke("artifact_save", {"project_id": "demo", "series_id": "main", "key": "brief", "base_digest": digest, "content": data})["status"])


def test_two_processes_same_digest_one_wins(project):
    digest = _seed(project, "brief")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_proc_worker, args=(str(project.parents[2]), digest, t, queue)) for t in ("А", "Б")]
    [p.start() for p in procs]
    [p.join(30) for p in procs]
    assert sorted([queue.get(timeout=5), queue.get(timeout=5)]) == ["error", "success"]
    json.loads(_path(project, "brief").read_text(encoding="utf-8"))


def test_write_failure_keeps_original_and_cleans_tmp(project, monkeypatch):
    digest = _seed(project, "brief")
    before = _path(project, "brief").read_bytes()

    def boom(src, dst):
        raise OSError("disk full /secret/path")

    monkeypatch.setattr(os, "replace", boom)
    data = _fixture("brief")
    data["title"] = "Не запишется"
    result = _call("artifact_save", key="brief", base_digest=digest, content=data)
    assert result["error"]["code"] == "internal" and "secret" not in json.dumps(result)
    assert _path(project, "brief").read_bytes() == before and not list(project.rglob("*.tmp"))


# --- shots -----------------------------------------------------------------------------


def _read_shots(project):
    return json.loads(_path(project, "shots").read_text(encoding="utf-8"))


def test_shots_merge_keeps_pipeline_changes(project):
    from StoryBookManager.core.file_manager import merge_write_shots_json

    _seed(project, "shots")
    opened = _call("artifact_read", key="shots")
    mine = opened["content"]
    mine["items"][0]["seed"] = 111  # правка пользователя в одном шоте
    theirs = _read_shots(project)  # «пайплайн» меняет другой шот и корневое поле
    theirs["items"][1]["video_path"] = "97_shots/video.mp4"
    theirs["generation_completed"] = True
    merge_write_shots_json(_path(project, "shots"), theirs["items"], _read_shots(project)["items"],
                           fresh_root=theirs, baseline_root=_read_shots(project))
    result = _call("artifact_save", key="shots", base_digest=opened["digest"], content=mine)
    assert result["status"] == "success" and result["external_changes"] is True and result["merged"] is True
    final = _read_shots(project)
    assert final["items"][0]["seed"] == 111 and final["items"][1]["video_path"] == "97_shots/video.mp4"
    assert final["generation_completed"] is True
    assert result["digest"] == art.obj_digest(final)
    history = _call("artifact_history", key="shots")["items"]
    assert history and history[0]["reason"] == "external"


def test_shots_conflict_without_cached_snapshot(project):
    digest = _seed(project, "shots")
    changed = _read_shots(project)
    changed["items"][0]["seed"] = 5
    _path(project, "shots").write_text(json.dumps(changed), encoding="utf-8")
    result = _call("artifact_save", key="shots", base_digest=digest, content=_fixture("shots"))
    assert result["error"]["code"] == "conflict"


def test_shots_deleted_and_added_items(project):
    _seed(project, "shots")
    opened = _call("artifact_read", key="shots")
    mine = opened["content"]
    removed = mine["items"].pop(1)
    # пайплайн добавил чужой шот
    theirs = _read_shots(project)
    theirs["items"].append({"scene_number": 9, "shot_number": 1, "shot_type": "start"})
    _path(project, "shots").write_text(json.dumps(theirs), encoding="utf-8")
    result = _call("artifact_save", key="shots", base_digest=opened["digest"], content=mine)
    assert result["status"] == "success"
    keys = {(i["scene_number"], i["shot_number"], i["shot_type"]) for i in _read_shots(project)["items"]}
    assert (removed["scene_number"], removed["shot_number"], removed["shot_type"]) not in keys and (9, 1, "start") in keys


def test_shots_repeated_save_does_not_drop_external_changes(project):
    _seed(project, "shots")
    opened = _call("artifact_read", key="shots")
    mine = opened["content"]
    theirs = _read_shots(project)
    theirs["items"][1]["output_path"] = "x.png"
    _path(project, "shots").write_text(json.dumps(theirs), encoding="utf-8")
    first = _call("artifact_save", key="shots", base_digest=opened["digest"], content=mine)
    mine["items"][0]["seed"] = 77
    second = _call("artifact_save", key="shots", base_digest=first["digest"], content=mine)
    assert second["status"] == "success"
    assert _read_shots(project)["items"][1]["output_path"] == "x.png" and _read_shots(project)["items"][0]["seed"] == 77


def test_shots_requires_items(project):
    result = _call("artifact_save", key="shots", base_digest=None, content={"seed": 1}, allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument"


# --- права -----------------------------------------------------------------------------


def test_permissions_through_dispatch(project):
    user = Principal(subject="u", roles=frozenset({"user"}))
    admin = Principal(subject="a", roles=frozenset({"admin", "user"}))
    for name in ("artifact_save", "artifact_read", "artifact_restore"):
        with pytest.raises(PermissionError):
            actions.require_storybook_role(f"storybook.{name}", user)
        actions.require_storybook_role(f"storybook.{name}", admin)
    assert caps.registry()["artifact_save"].access == "write" and caps.registry()["artifact_read"].access == "read"
    payload = {"project_id": "demo", "series_id": "main", "key": "synopsis", "base_digest": None, "content": _fixture("synopsis")}
    assert actions.dispatch("storybook.artifact_save", payload, admin)["status"] == "success"
    listed = actions.dispatch("storybook.artifacts_list", {"project_id": "demo", "series_id": "main"}, admin)
    assert [i["artifact_key"] for i in listed["items"]][:2] == ["brief", "synopsis"]  # не закрашено как секрет
