"""Настройки приложения studio: чтение, запись, проверки, Tk-ключи."""

import json
import logging
import os
import threading

import pytest

from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock
from custom_tools.storybook.studio import app_settings as mod
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.capabilities import invoke
from StoryBookManager.config.settings import app_settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    projects, backups, logs = tmp_path / "projects", tmp_path / "backups", tmp_path / "logs"
    for folder in (projects, backups, logs):
        folder.mkdir()
    file = tmp_path / "settings.json"
    disk = {
        "projects_directory": str(projects), "backup_directory": str(backups), "logs_directory": str(logs),
        "max_backup_files": 7, "log_level": "INFO", "theme": "dark", "json_editor_mode": "raw",
        "media_cache_size": 55,
    }
    file.write_text(json.dumps(disk), encoding="utf-8")
    monkeypatch.setattr(app_settings, "config_file", file)
    monkeypatch.setattr(app_settings, "settings", dict(disk))
    monkeypatch.setattr(app_settings, "_raw_replaced", {})
    monkeypatch.setattr(app_settings, "defaults", {**app_settings.defaults,
                        "projects_directory": str(tmp_path / "default_projects"),
                        "backup_directory": str(tmp_path / "default_backups"),
                        "logs_directory": str(tmp_path / "default_logs")})
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(mod, "_env_set_by_us", None)
    return tmp_path, file


def ok(result):
    assert result["status"] == "success", result
    return result


def err(result, code):
    assert result["status"] == "error" and result["error"]["code"] == code, result
    return result["error"]


def on_disk(file):
    return json.loads(file.read_text(encoding="utf-8"))


def test_registry_has_four_and_tk_keys_are_not_capabilities():
    reg = caps.registry()
    assert {k: reg[k].access for k in reg if k.startswith("app_settings_")} == {
        "app_settings_get": "read", "app_settings_update": "write",
        "app_settings_reset": "write", "app_settings_check_path": "write"}
    assert not [k for k in reg if k.startswith("tk_settings")]


def test_get_sources_and_digest(cfg, monkeypatch):
    tmp, file = cfg
    data = ok(invoke("app_settings_get", {}))
    values = data["settings"]
    assert values["max_backup_files"] == {"value": 7, "default": 10, "source": "file"}
    assert values["projects_directory"]["source"] == "file"
    assert data["checks"]["backup_directory"] == {"state": "ok", "message": "Папка доступна"}
    assert data["projects_directory_locked_reason"] is None and data["warnings"] == []
    assert ok(invoke("app_settings_get", {}))["digest"] == data["digest"]
    disk = on_disk(file)
    del disk["log_level"]
    file.write_text(json.dumps(disk), encoding="utf-8")
    assert ok(invoke("app_settings_get", {}))["settings"]["log_level"]["source"] == "default"
    other = tmp / "other"
    other.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(other))
    after = ok(invoke("app_settings_get", {}))
    assert after["settings"]["projects_directory"] == {
        "value": str(other.resolve()), "default": app_settings.defaults["projects_directory"], "source": "env"}
    assert after["projects_directory_locked_reason"] == mod.MSG_LOCKED_ENV


def test_get_warns_about_replaced_path(cfg):
    tmp, _ = cfg
    default = app_settings.defaults["projects_directory"]
    app_settings._raw_replaced["projects_directory"] = ("/Users/kosoj/x", default)
    app_settings.settings["projects_directory"] = default
    data = ok(invoke("app_settings_get", {}))
    assert data["warnings"] == ["Путь /Users/kosoj/x не найден, использована папка по умолчанию"]
    assert data["settings"]["projects_directory"]["source"] == "default"


def test_path_states(cfg):
    tmp, _ = cfg
    (tmp / "afile").write_text("x")
    assert ok(invoke("app_settings_check_path", {"kind": "logs", "path": str(tmp)}))["state"] == "ok"
    assert ok(invoke("app_settings_check_path", {"kind": "logs", "path": str(tmp / "nope")}))["state"] == "missing"
    assert ok(invoke("app_settings_check_path", {"kind": "logs", "path": str(tmp / "afile")}))["state"] == "not_dir"
    err(invoke("app_settings_check_path", {"kind": "tmp", "path": "/x"}), "invalid_argument")
    err(invoke("app_settings_check_path", {"kind": "logs", "path": ""}), "invalid_argument")


@pytest.mark.skipif(os.geteuid() == 0, reason="под root chmod не действует")
def test_path_not_writable(cfg):
    tmp, _ = cfg
    locked = tmp / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert ok(invoke("app_settings_check_path", {"kind": "backups", "path": str(locked)}))["state"] == "not_writable"
    finally:
        locked.chmod(0o700)


def test_update_success_keeps_dead_and_tk_keys(cfg):
    tmp, file = cfg
    new_backups = tmp / "new_backups"
    data = ok(invoke("app_settings_update", {
        "backup_directory": str(new_backups), "max_backup_files": 20, "log_level": "debug"}))
    assert data["changed"] == ["backup_directory", "log_level", "max_backup_files"]
    assert any("Прежние бэкапы остались" in w for w in data["warnings"])
    disk = on_disk(file)
    assert disk["backup_directory"] == str(new_backups) and disk["max_backup_files"] == 20
    assert disk["log_level"] == "DEBUG"
    assert (disk["theme"], disk["json_editor_mode"], disk["media_cache_size"]) == ("dark", "raw", 55)
    assert new_backups.is_dir() and app_settings.get("max_backup_files") == 20
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger().setLevel(logging.WARNING)
    assert ok(invoke("app_settings_update", {}))["changed"] == []


def test_update_projects_directory_rules(cfg):
    tmp, file = cfg
    before = file.read_text(encoding="utf-8")
    err(invoke("app_settings_update", {"projects_directory": str(tmp / "missing")}), "invalid_argument")
    err(invoke("app_settings_update", {"projects_directory": ""}), "invalid_argument")
    err(invoke("app_settings_update", {"projects_directory": "a\0b"}), "invalid_argument")
    assert file.read_text(encoding="utf-8") == before
    target = tmp / "newroot"
    target.mkdir()
    handle = None
    (tmp / "projects" / "alpha").mkdir()
    handle = acquire_storybook_project_lock("alpha")
    try:
        err(invoke("app_settings_update", {"projects_directory": str(target)}), "locked")
        assert ok(invoke("app_settings_get", {}))["projects_directory_locked_reason"] == mod.MSG_LOCKED_RUN
    finally:
        release_storybook_project_lock(handle)
    ok(invoke("app_settings_update", {"projects_directory": str(target)}))
    assert on_disk(file)["projects_directory"] == str(target.resolve())
    assert os.environ["STORYBOOK_PROJECTS_DIR"] == str(target.resolve())


def test_update_env_source_unsupported(cfg, monkeypatch):
    tmp, file = cfg
    other = tmp / "envroot"
    other.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(other))
    target = tmp / "x"
    target.mkdir()
    err(invoke("app_settings_update", {"projects_directory": str(target)}), "unsupported")
    ok(invoke("app_settings_update", {"max_backup_files": 3}))


def test_update_overlap_and_values(cfg):
    tmp, file = cfg
    projects = tmp / "projects"
    for payload in (
        {"backup_directory": str(projects / "inside")},
        {"logs_directory": str(tmp / "backups")},
        {"backup_directory": str(tmp)},
        {"log_level": "LOUD"}, {"max_backup_files": 0}, {"max_backup_files": 101}, {"max_backup_files": "x"},
        {"max_backup_files": True}, {"backup_directory": str(tmp / "no" / "parent")},
    ):
        before = file.read_text(encoding="utf-8")
        err(invoke("app_settings_update", payload), "invalid_argument")
        assert file.read_text(encoding="utf-8") == before, payload


def test_update_digest_conflict_and_none(cfg):
    tmp, file = cfg
    digest = ok(invoke("app_settings_get", {}))["digest"]
    ok(invoke("app_settings_update", {"max_backup_files": 11, "expected_digest": digest}))
    err(invoke("app_settings_update", {"max_backup_files": 12, "expected_digest": digest}), "conflict")
    assert on_disk(file)["max_backup_files"] == 11
    ok(invoke("app_settings_update", {"max_backup_files": None, "log_level": None}))
    assert on_disk(file)["max_backup_files"] == 11


def test_update_is_atomic_on_write_error(cfg, monkeypatch):
    tmp, file = cfg
    before = file.read_text(encoding="utf-8")
    monkeypatch.setattr(mod.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    err(invoke("app_settings_update", {"max_backup_files": 50}), "internal")
    monkeypatch.undo()
    assert file.read_text(encoding="utf-8") == before
    assert not [p for p in tmp.iterdir() if p.name.endswith(".tmp")]


def test_parallel_updates_do_not_lose_keys(cfg):
    tmp, file = cfg
    errors = []

    def one(key, value):
        try:
            mod._write_settings({key: value})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(f"k{i}", i)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    disk = on_disk(file)
    assert not errors and all(disk[f"k{i}"] == i for i in range(12)) and disk["theme"] == "dark"


def test_reset(cfg):
    tmp, file = cfg
    err(invoke("app_settings_reset", {}), "invalid_argument")
    ok(invoke("app_settings_update", {"max_backup_files": 5}))
    mod.tk_settings_update(theme="light")
    (tmp / "default_projects").mkdir()
    data = ok(invoke("app_settings_reset", {"confirm": True}))
    disk = on_disk(file)
    assert disk["max_backup_files"] == 10 and disk["projects_directory"] == str((tmp / "default_projects").resolve())
    assert disk["theme"] == "light" and disk["json_editor_mode"] == "raw"
    assert "max_backup_files" in data["changed"]


def test_tk_settings(cfg):
    tmp, file = cfg
    assert mod.tk_settings_get()["theme"] == "dark"
    result = mod.tk_settings_update(window_geometry="900x700+10+-5", ui_scale=125, auto_save_interval=0)
    assert result["ui_scale"] == 125 and on_disk(file)["window_geometry"] == "900x700+10+-5"
    for bad in ({"window_geometry": "big"}, {"ui_scale": 110}, {"auto_save_interval": 4000},
                {"theme": ""}, {"theme": "x" * 33}, {"nope": 1}):
        with pytest.raises(mod.StudioError):
            mod.tk_settings_update(**bad)
    assert mod.tk_settings_reset()["ui_scale"] == 100


def test_apply_runtime_environment_only_if_unset(cfg, monkeypatch):
    tmp, _ = cfg
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR")
    mod.apply_runtime_environment()
    assert os.environ["STORYBOOK_PROJECTS_DIR"] == str(tmp / "projects")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", "/keep/me")
    mod.apply_runtime_environment()
    assert os.environ["STORYBOOK_PROJECTS_DIR"] == "/keep/me"


def test_env_set_by_us_is_not_server_lock(cfg, monkeypatch):
    """Подставленный (Ф0) путь, выставленный нами в env, не блокирует смену папки из React."""
    tmp, file = cfg
    default = app_settings.defaults["projects_directory"]
    monkeypatch.setattr(app_settings, "_raw_replaced", {"projects_directory": ("/Users/kosoj/x", default)})
    monkeypatch.setattr(app_settings, "settings", {**app_settings.settings, "projects_directory": default})
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR")
    mod.apply_runtime_environment()
    assert os.environ["STORYBOOK_PROJECTS_DIR"] == default
    assert mod._projects_from_env() is False
    assert ok(invoke("app_settings_get", {}))["projects_directory_locked_reason"] is None
    target = tmp / "other"
    target.mkdir()
    ok(invoke("app_settings_update", {"projects_directory": str(target)}))
    assert os.environ["STORYBOOK_PROJECTS_DIR"] == str(target.resolve())
    assert mod._projects_from_env() is False
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp / "server"))
    assert mod._projects_from_env() is True


def test_tk_save_keeps_studio_changes_and_writes_only_own(cfg, monkeypatch):
    """Tk-сохранение не затирает ключи, изменённые студией на диске после его загрузки."""
    tmp, file = cfg
    monkeypatch.setattr(app_settings, "_baseline", dict(app_settings.settings))
    disk = on_disk(file)
    disk["max_backup_files"] = 99  # поменяла студия в другом процессе
    file.write_text(json.dumps(disk), encoding="utf-8")
    app_settings.set("theme", "light")
    assert app_settings.save_settings() is True
    after = on_disk(file)
    assert after["theme"] == "light" and after["max_backup_files"] == 99
    # студия в этом процессе: база обновляется, и Tk не пишет её ключи как свои
    ok(invoke("app_settings_update", {"max_backup_files": 5}))
    disk = on_disk(file)
    disk["log_level"] = "DEBUG"
    file.write_text(json.dumps(disk), encoding="utf-8")
    app_settings.set("theme", "dark")
    assert app_settings.save_settings() is True
    final = on_disk(file)
    assert final["theme"] == "dark" and final["max_backup_files"] == 5 and final["log_level"] == "DEBUG"


def test_baseline_is_deep_copy_nested_changes_detected(cfg, monkeypatch):
    tmp, file = cfg
    disk = on_disk(file)
    disk.update({"recent": ["a"], "nested": {"x": 1}})
    file.write_text(json.dumps(disk), encoding="utf-8")
    loaded = app_settings.load_settings()
    monkeypatch.setattr(app_settings, "settings", loaded)
    monkeypatch.setattr(app_settings, "_baseline", app_settings._baseline)
    loaded["recent"].append("b")
    loaded["nested"]["x"] = 2
    assert app_settings.save_settings() is True
    after = on_disk(file)
    assert after["recent"] == ["a", "b"] and after["nested"] == {"x": 2}
