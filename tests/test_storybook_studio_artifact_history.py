"""Ф10: artifact_history, artifact_diff, artifact_restore."""

import copy
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock
from custom_tools.storybook.studio import artifacts as art
from custom_tools.storybook.studio import capabilities as caps

FIXTURES = Path(__file__).parent / "fixtures" / "storybook_artifacts"


def _fixture(key):
    return json.loads((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    art._SNAPSHOTS.clear()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    series = make_series_project(tmp_path)
    with series_scope("demo", "main"):
        yield series


def _call(cap, **payload):
    return caps.invoke(cap, {"project_id": "demo", "series_id": "main", **payload})


def _path(project, key):
    return project / art.ARTIFACTS[key].path


def _save_brief(digest, **changes):
    data = _fixture("brief")
    data.update(changes)
    result = _call("artifact_save", key="brief", base_digest=digest, content=data)
    assert result["status"] == "success", result
    return result


def _make_history(n):
    digest = None
    for idx in range(n + 1):
        digest = _save_brief(digest, pages_max=3 + idx)["digest"]
    return digest


def test_history_order_and_fields(project):
    digest = _make_history(2)
    items = _call("artifact_history", key="brief")["items"]
    assert len(items) == 2 and items[0]["version_id"] > items[1]["version_id"]
    first = items[0]
    assert first["reason"] == "save" and first["reason_title"] == "Сохранение" and first["size"] > 0
    assert len(first["digest8"]) == 8 and first["created_at"].endswith("+00:00")
    assert _call("artifact_history", key="brief")["artifact_key"] == "brief"
    assert digest


def test_same_microsecond_does_not_overwrite(project, monkeypatch):
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(art, "_now", lambda: frozen)
    _make_history(3)
    assert len(_call("artifact_history", key="brief")["items"]) == 3


def test_identical_bytes_do_not_multiply(project):
    path = _path(project, "brief")
    digest = _save_brief(None)["digest"]
    raw = path.read_bytes()
    first = art._snapshot("demo", "brief", raw, "save")
    second = art._snapshot("demo", "brief", raw, "save")
    assert first == second and len(_call("artifact_history", key="brief")["items"]) == 1
    assert digest


def test_retention_keeps_newest(project, monkeypatch):
    monkeypatch.setattr(art, "HISTORY_KEEP", 3)
    _make_history(6)
    items = _call("artifact_history", key="brief")["items"]
    assert len(items) == 3
    newest = _call("artifact_read", key="brief", version_id=items[0]["version_id"])
    assert newest["content"]["pages_max"] == 3 + 5  # снимок хранит содержимое перед последним сохранением


def test_foreign_names_are_ignored(project):
    _make_history(1)
    directory = project / ".studio" / "artifact_history" / "brief"
    (directory / "notes.txt").write_text("x")
    (directory / "bad_name.json").write_text("{}")
    (directory / "20260101T000000000000_hack_00000000.json").write_text("{}")
    os.symlink("/etc/hostname", directory / "20260101T000000000001_save_00000000.json")
    items = _call("artifact_history", key="brief")["items"]
    assert len(items) == 1


def test_version_id_validation_and_symlink(project):
    _make_history(1)
    directory = project / ".studio" / "artifact_history" / "brief"
    link = directory / "20260101T000000000001_save_00000000.json"
    os.symlink("/etc/hostname", link)
    for cap in ("artifact_read", "artifact_diff"):
        for bad in ("../../x", "a/b", "/etc/passwd", "x" * 500, "", None):
            assert _call(cap, key="brief", version_id=bad)["status"] == "error" if cap == "artifact_diff" or bad is not None else True
    assert _call("artifact_diff", key="brief", version_id="../../x")["error"]["code"] == "invalid_argument"
    assert _call("artifact_restore", key="brief", version_id="a/b", base_digest=None)["error"]["code"] == "invalid_argument"
    assert _call("artifact_read", key="brief", version_id=link.stem)["error"]["code"] == "forbidden_path"


def test_diff_changes(project):
    first = _save_brief(None)
    second = _save_brief(first["digest"], title="Новое название", pages_max=7)
    data = _fixture("brief")
    data["title"] = "Новое название"
    data["extra"] = "x" * 500
    del data["moral"]
    _path(project, "brief").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    result = _call("artifact_diff", key="brief", version_id=version)
    ops = {c["pointer"]: c for c in result["changes"]}
    assert result["identical"] is False and result["truncated"] is False
    assert ops["/title"]["op"] == "changed" and ops["/title"]["before"] == "Лисёнок и луна" and ops["/title"]["label"] == "Бриф · Название"
    assert ops["/extra"]["op"] == "added" and len(ops["/extra"]["after"]) == art.CLIP_LEN + 1
    assert ops["/moral"]["op"] == "removed"
    assert second


def test_diff_identical(project):
    _save_brief(None)
    d = _save_brief(art.obj_digest(_fixture("brief")), pages_max=9)["digest"]
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    _call("artifact_restore", key="brief", version_id=version, base_digest=d)
    newest = _call("artifact_history", key="brief")["items"][0]["version_id"]
    assert _call("artifact_diff", key="brief", version_id=version)["identical"] is True
    assert newest


def test_diff_shots_insert_is_not_a_cascade(project):
    base = _fixture("shots")
    for scene in range(1, 21):
        for kind in ("start", "end"):
            base["items"].append({"scene_number": scene + 10, "shot_number": 1, "shot_type": kind, "seed": scene})
    first = _call("artifact_save", key="shots", base_digest=None, content=base, allow_invalid=True)
    assert first["status"] == "success"
    changed = copy.deepcopy(base)
    changed["items"].insert(0, {"scene_number": 0, "shot_number": 1, "shot_type": "start"})
    second = _call("artifact_save", key="shots", base_digest=first["digest"], content=changed, allow_invalid=True)
    assert second["status"] == "success"
    version = _call("artifact_history", key="shots")["items"][0]["version_id"]
    changes = _call("artifact_diff", key="shots", version_id=version)["changes"]
    assert [c["op"] for c in changes] == ["added"]


def test_diff_limit_and_non_json(project):
    first = _save_brief(None)
    second = _save_brief(first["digest"], title="Другое")
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    huge = _fixture("brief")
    for idx in range(400):
        huge[f"f{idx}"] = idx
    _path(project, "brief").write_text(json.dumps(huge), encoding="utf-8")
    result = _call("artifact_diff", key="brief", version_id=version)
    assert result["truncated"] is True and len(result["changes"]) == art.MAX_DIFF_CHANGES
    _path(project, "brief").write_text("{oops", encoding="utf-8")
    assert _call("artifact_diff", key="brief", version_id=version)["error"]["code"] == "invalid_argument"
    assert first and second


def test_restore_roundtrip_is_byte_exact_and_reversible(project):
    d1 = _save_brief(None)["digest"]
    original = _path(project, "brief").read_bytes()
    d2 = _save_brief(d1, title="Второе")["digest"]
    changed = _path(project, "brief").read_bytes()
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    restored = _call("artifact_restore", key="brief", version_id=version, base_digest=d2)
    assert restored["status"] == "success" and restored["restored_from"] == version
    assert _path(project, "brief").read_bytes() == original and restored["digest"] == d1
    history = _call("artifact_history", key="brief")["items"]
    assert history[0]["reason"] == "restore"
    again = _call("artifact_restore", key="brief", version_id=history[0]["version_id"], base_digest=d1)
    assert again["status"] == "success" and _path(project, "brief").read_bytes() == changed


def test_restore_errors(project):
    d1 = _save_brief(None)["digest"]
    d2 = _save_brief(d1, title="Второе")["digest"]
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    stale = _call("artifact_restore", key="brief", version_id=version, base_digest=d1)
    assert stale["error"]["code"] == "conflict" and stale["error"]["details"]["current_digest"] == d2
    assert _call("artifact_restore", key="brief", version_id="20200101T000000000000_save_00000000",
                 base_digest=d2)["error"]["code"] == "not_found"
    handle = acquire_storybook_project_lock("demo")
    try:
        assert _call("artifact_restore", key="brief", version_id=version, base_digest=d2)["error"]["code"] == "locked"
    finally:
        release_storybook_project_lock(handle)
    assert _call("artifact_restore", key="provider_jobs", version_id=version, base_digest=None)["error"]["code"] == "unsupported"


def test_restore_invalid_version_needs_allow_invalid(project):
    d1 = _save_brief(None)["digest"]
    bad = _fixture("brief")
    del bad["title"]
    forced = _call("artifact_save", key="brief", base_digest=d1, content=bad, allow_invalid=True)
    d3 = _save_brief(forced["digest"], title="Третье")["digest"]
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]  # версия без title
    before = _path(project, "brief").read_bytes()
    refused = _call("artifact_restore", key="brief", version_id=version, base_digest=d3)
    assert refused["error"]["code"] == "invalid_argument" and refused["error"]["details"]["errors"]
    assert _path(project, "brief").read_bytes() == before
    result = _call("artifact_restore", key="brief", version_id=version, base_digest=d3, allow_invalid=True)
    assert result["status"] == "success" and result["validation"]["counts"]["errors"] == 1
    assert "title" not in json.loads(_path(project, "brief").read_text(encoding="utf-8"))


def test_restore_shots_refused_during_run(project, monkeypatch):
    first = _call("artifact_save", key="shots", base_digest=None, content=_fixture("shots"))
    edited = _fixture("shots")
    edited["items"] = edited["items"][:1]
    second = _call("artifact_save", key="shots", base_digest=first["digest"], content=edited)
    version = _call("artifact_history", key="shots")["items"][0]["version_id"]
    before = _path(project, "shots").read_bytes()
    monkeypatch.setattr(art, "_run_info", lambda pid: {"active": True, "current_steps": None})
    result = _call("artifact_restore", key="shots", version_id=version, base_digest=second["digest"])
    assert result["error"]["code"] == "locked" and _path(project, "shots").read_bytes() == before


def test_surrogate_in_content_is_invalid_argument(project):
    result = _call("artifact_save", key="brief", base_digest=None, content={"title": "\ud800"}, allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument"


def test_commit_keeps_file_mode(project):
    _save_brief(None)
    path = _path(project, "brief")
    os.chmod(path, 0o640)
    d = art.file_digest(path.read_bytes())
    _save_brief(d, title="Права")
    assert (path.stat().st_mode & 0o777) == 0o640


def test_restore_shots_is_full_replace_under_sidecar_lock(project):
    from StoryBookManager.core.file_manager import merge_write_shots_json

    first = _call("artifact_save", key="shots", base_digest=None, content=_fixture("shots"))
    original = _path(project, "shots").read_bytes()
    edited = _fixture("shots")
    edited["items"] = edited["items"][:1]
    second = _call("artifact_save", key="shots", base_digest=first["digest"], content=edited)
    version = _call("artifact_history", key="shots")["items"][0]["version_id"]
    stop = threading.Event()

    def pipeline():  # параллельная запись по протоколу не ломает файл
        while not stop.is_set():
            doc = json.loads(_path(project, "shots").read_text(encoding="utf-8"))
            merge_write_shots_json(_path(project, "shots"), doc["items"], doc["items"])

    worker = threading.Thread(target=pipeline)
    worker.start()
    try:
        current = art.file_digest(_path(project, "shots").read_bytes())
        result = _call("artifact_restore", key="shots", version_id=version, base_digest=current)
    finally:
        stop.set()
        worker.join()
    assert result["status"] == "success" and _path(project, "shots").read_bytes() == original
    assert second


def test_tk_backups_are_untouched(project, tmp_path, monkeypatch):
    from StoryBookManager.config.settings import app_settings

    backups = tmp_path / "tk_backups"
    (backups / "files").mkdir(parents=True)
    old = backups / "files" / "demo_00_brief_20260101_000000.json"
    old.write_text("{}")
    monkeypatch.setattr(app_settings, "get_backup_directory", lambda: backups)
    _make_history(1)
    assert [p.name for p in (backups / "files").iterdir()] == [old.name]
    assert len(_call("artifact_history", key="brief")["items"]) == 1


def test_restore_wrong_root_refused_even_with_allow_invalid(project):
    path = _path(project, "brief")
    path.write_text("[1, 2]", encoding="utf-8")  # версия с неверным корнем попадёт в историю
    saved = _save_brief(art.file_digest(path.read_bytes()))
    version = _call("artifact_history", key="brief")["items"][0]["version_id"]
    before = path.read_bytes()
    result = _call("artifact_restore", key="brief", version_id=version, base_digest=saved["digest"],
                   allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["errors"][0]["code"] == "root"
    assert path.read_bytes() == before


def test_surrogate_in_shots_is_invalid_argument(project):
    shots = _fixture("shots")
    shots["items"][0]["prompt"] = "\ud800"
    result = _call("artifact_save", key="shots", base_digest=None, content=shots, allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument"
    assert "суррогат" in result["error"]["message"]
    assert not _path(project, "shots").exists()


def test_surrogate_on_disk_shots_is_invalid_argument(project):
    shots = _fixture("shots")
    first = _call("artifact_save", key="shots", base_digest=None, content=shots, allow_invalid=True)
    path = _path(project, "shots")
    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["extra_note"] = "\ud800"  # внешний писатель добавил поле с экранированным суррогатом
    path.write_text(json.dumps(disk, ensure_ascii=True), encoding="utf-8")
    before = path.read_bytes()
    shots["items"][0]["prompt"] = "новый промпт"
    result = _call("artifact_save", key="shots", base_digest=first["digest"], content=shots,
                   allow_invalid=True)
    assert result["error"]["code"] == "invalid_argument", result
    assert path.read_bytes() == before
