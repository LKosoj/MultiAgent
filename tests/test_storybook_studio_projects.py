"""Возможности studio для проектов: вызовы через invoke."""

import hashlib
import json
import sys
import threading
import time
import types

import pytest

from tests.studio_run_fakes import make_series_project

from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import jobs
from custom_tools.storybook.studio.capabilities import invoke
from StoryBookManager.config.settings import app_settings

EXPECTED = {
    "projects_list": ("read", False), "projects_get": ("read", True), "projects_size": ("read", True),
    "projects_suggest_id": ("read", False), "projects_create": ("write", False),
    "projects_create_from_prompt": ("write", False), "projects_duplicate": ("write", True),
    "projects_rename": ("write", True), "projects_delete": ("write", True),
    "projects_backups_list": ("read", False), "projects_backup_create": ("write", True),
    "projects_backup_restore": ("write", False), "projects_export": ("write", True),
    "projects_job_status": ("read", False),
}
FORM = dict(title="Лисёнок", description="Добрая история про лисёнка", genre="сказка", target_age="3-5")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, backups = tmp_path / "projects", tmp_path / "backups"
    root.mkdir()
    backups.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    monkeypatch.setattr(app_settings, "get_backup_directory", lambda: backups)
    return root, backups


def ok(result):
    assert result["status"] == "success", result
    return result


def err(result, code):
    assert result["status"] == "error" and result["error"]["code"] == code, result
    return result["error"]


def run_job(started):
    ok(started)
    deadline = time.time() + 10
    while time.time() < deadline:
        record = ok(invoke("projects_job_status", {"job_id": started["job_id"]}))
        if record["state"] != "running":
            return record
        time.sleep(0.02)
    raise AssertionError("задание не завершилось")


def hashes(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def make(root, name="alpha"):
    series = make_series_project(root, name)
    base = root / name
    project = {"format": "storybook-series-v1", "project_id": name, "title": "Альфа",
               "description": "описание", "language": "ru", "resources": {}}
    (base / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    (series / "20_story").mkdir()
    (series / "20_story/story.json").write_text(json.dumps({"pages": [1, 2, 3]}))
    (base / "data.txt").write_text("hello")
    return base


def test_registry_matches_table():
    reg = caps.registry()
    for cap_id, (access, scoped) in EXPECTED.items():
        assert (reg[cap_id].access, reg[cap_id].project_scoped) == (access, scoped), cap_id
    assert reg["projects_create"].supersedes == ("project_create_backup_export_delete",)
    assert len({c.client_fn for c in reg.values()}) == len(reg)


def test_list_card_search_sort_busy(env):
    root, _ = env
    make(root)
    beta = make(root, "beta")
    data = json.loads((beta / "project.json").read_text(encoding="utf-8"))
    data["title"] = "Бета"
    (beta / "project.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (root / "_shared_internal").mkdir()
    (root / "bare").mkdir()
    data = ok(invoke("projects_list", {}))
    assert data["projects_dir"] == str(root) and data["total"] == 2 == data["count"]
    card = next(p for p in data["projects"] if p["project_id"] == "alpha")
    assert card["title"] == "Альфа" and card["series_count"] == 1
    assert card["state"] == "ready" and card["stage"]["id"] == "project" and card["busy"] is False
    assert ok(invoke("projects_list", {"search": "АЛЬФ"}))["count"] == 1
    assert [p["project_id"] for p in ok(invoke("projects_list", {"sort": "name"}))["projects"]] == ["alpha", "beta"]
    assert {p["project_id"] for p in ok(invoke("projects_list", {"sort": "stage"}))["projects"]} == {"alpha", "beta"}
    err(invoke("projects_list", {"sort": "size"}), "invalid_argument")
    handle = acquire_storybook_project_lock("alpha")
    try:
        assert next(p for p in ok(invoke("projects_list", {}))["projects"] if p["project_id"] == "alpha")["busy"]
        assert [p["project_id"] for p in ok(invoke("projects_list", {"include_busy": False}))["projects"]] == ["beta"]
    finally:
        release_storybook_project_lock(handle)
    assert not (root / "bare" / ".pipeline.lock").exists()


def test_list_missing_root(env):
    root, _ = env
    root.rmdir()
    error = err(invoke("projects_list", {}), "not_found")
    assert error["details"]["path"] == str(root)


def test_get_size_suggest(env):
    root, _ = env
    make(root)
    data = ok(invoke("projects_get", {"project_id": "alpha"}))
    assert data["details"]["format"] == "storybook-series-v1"
    assert data["details"]["title"] == "Альфа"
    err(invoke("projects_get", {"project_id": "nope"}), "not_found")
    size = ok(invoke("projects_size", {"project_id": "alpha"}))
    assert size["size_bytes"] == sum(p.stat().st_size for p in (root / "alpha").rglob("*") if p.is_file())
    assert ok(invoke("projects_size", {"project_id": "alpha"}))["cached"] is True
    suggested = ok(invoke("projects_suggest_id", {"id_hint": "Мой проект"}))
    assert suggested["project_id"] == "moy_proekt" and suggested["changed"] is True
    err(invoke("projects_suggest_id", {"id_hint": "  "}), "invalid_argument")


def test_create_with_hint_trap_and_validation(env):
    root, _ = env
    created = ok(invoke("projects_create", {**FORM, "id_hint": "Мой проект"}))
    assert created["project_id"] == "moy_proekt" and created["project"]["state"] == "ready"
    brief = json.loads((root / "moy_proekt/project.json").read_text(encoding="utf-8"))
    assert brief["title"] == "Лисёнок" and brief["defaults"]["pages_min"] == 9 and brief["defaults"]["words_per_page_max"] == 450
    assert "Страниц" in err(invoke("projects_create", {**FORM, "pages_min": 0}), "invalid_argument")["message"]
    err(invoke("projects_create", {**FORM, "title": "x"}), "invalid_argument")
    assert ok(invoke("projects_create", FORM))["project_id"] == "lisenok"


def test_create_from_prompt_unsupported_and_create_world_with_series(env):
    root, _ = env
    err(invoke("projects_create_from_prompt", {"prompt": "Сказка про храброго ёжика"}), "unsupported")
    assert not list(root.iterdir())
    project_id = ok(invoke("projects_create", FORM))["project_id"]
    created = ok(invoke("series_create", {"project_id": project_id, "title": "Первая серия",
                                          "source_material": "Сказка про храброго ёжика",
                                          "participant_ids": [], "settings": {}}))
    series_id = created["series_id"]
    assert ok(invoke("series_get", {"project_id": project_id, "series_id": series_id}))["series"]["source_material"] == "Сказка про храброго ёжика"
    assert (root / project_id / "series" / series_id / "series.json").is_file()


def test_duplicate_rename(env):
    root, _ = env
    make(root)
    started = ok(invoke("projects_duplicate", {"project_id": "alpha"}))
    assert started["new_project_id"] == "alpha_copy"
    record = run_job(started)
    assert record["result"]["new_project_id"] == "alpha_copy"
    copy = ok(invoke("projects_get", {"project_id": "alpha_copy"}))["project"]
    assert copy["title"] == "Альфа (копия)"
    renamed = ok(invoke("projects_rename", {"project_id": "alpha", "title": "Бета"}))
    assert renamed["previous_title"] == "Альфа" and renamed["project"]["title"] == "Бета"
    err(invoke("projects_rename", {"project_id": "alpha", "title": "x"}), "invalid_argument")
    err(invoke("projects_rename", {"project_id": "bare", "title": "Имя"}), "not_found")
    (root / "bare").mkdir()
    err(invoke("projects_rename", {"project_id": "bare", "title": "Имя"}), "not_found")
    handle = acquire_storybook_project_lock("alpha")
    try:
        err(invoke("projects_rename", {"project_id": "alpha", "title": "Гамма"}), "locked")
    finally:
        release_storybook_project_lock(handle)


def test_confirm_required_and_nothing_changed(env):
    root, backups = env
    base = make(root)
    err(invoke("projects_delete", {"project_id": "alpha"}), "invalid_argument")
    err(invoke("projects_backup_restore", {"project_id": "alpha", "backup_id": "alpha_backup_20260101_000000"}),
        "invalid_argument")
    assert base.is_dir() and not list(backups.iterdir())


def test_locked_operations(env):
    root, backups = env
    make(root)
    saved = run_job(ok(invoke("projects_backup_create", {"project_id": "alpha"})))["result"]
    handle = acquire_storybook_project_lock("alpha")
    try:
        err(invoke("projects_delete", {"project_id": "alpha", "confirm": True}), "locked")
        err(invoke("projects_backup_restore", {"project_id": "alpha", "backup_id": saved["backup_id"], "confirm": True}),
            "locked")
        started = ok(invoke("projects_backup_create", {"project_id": "alpha"}))
        assert started["consistent"] is False
        assert run_job(started)["result"]["consistent"] is False
    finally:
        release_storybook_project_lock(handle)
    assert (root / "alpha" / "data.txt").read_text() == "hello"


def test_backup_list_export_job_status(env):
    root, backups = env
    base = make(root)
    started = ok(invoke("projects_backup_create", {"project_id": "alpha", "note": "до правок"}))
    created = run_job(started)["result"]
    old = backups / "oldproj_backup_20240101_000000"
    old.mkdir()
    (old / "f").write_text("1")
    rows = ok(invoke("projects_backups_list", {}))["backups"]
    by_id = {r["backup_id"]: r for r in rows}
    assert by_id[created["backup_id"]]["reason"] == "manual" and by_id[created["backup_id"]]["note"] == "до правок"
    assert by_id[old.name]["reason"] == "unknown" and by_id[old.name]["project_exists"] is False
    assert [r["backup_id"] for r in ok(invoke("projects_backups_list", {"for_project_id": "alpha"}))["backups"]] == [created["backup_id"]]
    exported = run_job(ok(invoke("projects_export", {"project_id": "alpha"})))["result"]
    assert exported["path"].startswith(".studio/exports/") and (base / exported["path"]).is_file()
    err(invoke("projects_job_status", {"job_id": "../x"}), "invalid_argument")
    err(invoke("projects_job_status", {"job_id": "d" * 32}), "not_found")
    err(invoke("projects_backup_restore", {"project_id": "alpha", "backup_id": "../x", "confirm": True}), "invalid_argument")


def test_delete_find_restore_roundtrip(env):
    root, _ = env
    base = make(root)
    before = hashes(base)
    deleted = run_job(ok(invoke("projects_delete", {"project_id": "alpha", "confirm": True})))
    assert deleted["state"] == "done" and not base.exists()
    assert "alpha" not in [p["project_id"] for p in ok(invoke("projects_list", {}))["projects"]]
    rows = ok(invoke("projects_backups_list", {"for_project_id": "alpha"}))["backups"]
    assert rows[0]["reason"] == "before_delete" and rows[0]["project_exists"] is False
    restored = run_job(ok(invoke("projects_backup_restore", {
        "project_id": "alpha", "backup_id": rows[0]["backup_id"], "confirm": True})))
    assert restored["state"] == "done" and restored["result"]["project"]["project_id"] == "alpha"
    assert hashes(root / "alpha") == before


def test_legacy_inventory_hides_underscore_dirs(env):
    from custom_tools.storybook.storybook_surface import storybook_project_inventory

    root, _ = env
    make(root)
    (root / "_shared_internal").mkdir()
    ids = [p["project_id"] for p in storybook_project_inventory()["projects"]]
    assert ids == ["alpha"]


def test_backup_consistent_recomputed_in_job(env, monkeypatch):
    """Прогон начался уже во время копии: задание свободно в начале, но результат не consistent."""
    from custom_tools.storybook.studio import project_store
    make(root := env[0])
    real = project_store.create_backup
    holder = []

    def copy_then_run_starts(*args, **kwargs):
        result = real(*args, **kwargs)
        holder.append(acquire_storybook_project_lock("alpha"))
        return result

    monkeypatch.setattr(project_store, "create_backup", copy_then_run_starts)
    started = ok(invoke("projects_backup_create", {"project_id": "alpha"}))
    assert started["consistent"] is True
    try:
        assert run_job(started)["result"]["consistent"] is False
    finally:
        for handle in holder:
            release_storybook_project_lock(handle)
