"""Ф0 п.1-2: подмена несуществующих путей и единый синглтон настроек."""
import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from StoryBookManager.config.settings import AppSettings  # noqa: E402


@contextlib.contextmanager
def _tk_stubbed(stubs):
    """На время блока подменяет tkinter заглушками и даёт свежий StoryBookManager.*.

    На выходе возвращает только эти модули как были: заглушки и
    StoryBookManager.*, импортированные под ними, не протекают в другие тесты
    (сторонние библиотеки, импортированные внутри, не трогаем).
    """
    def _own(name):
        return name in stubs or name == "StoryBookManager" or name.startswith("StoryBookManager.")

    saved = {n: m for n, m in sys.modules.items() if _own(n)}
    for name in saved:
        del sys.modules[name]
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _own(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


def _load(tmp_path, data):
    import json
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    s = AppSettings()
    s.config_file = cfg
    return s, cfg, s.load_settings()


def test_missing_absolute_paths_replaced_by_defaults(tmp_path):
    data = {
        "projects_directory": "/Users/nobody/x/projects",
        "backup_directory": "/Users/nobody/x/backups",
        "logs_directory": "/Users/nobody/x/logs",
    }
    s, cfg, before = None, None, None
    import json
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    raw = cfg.read_bytes()
    s = AppSettings()
    s.config_file = cfg
    loaded = s.load_settings()
    for key in data:
        assert loaded[key] == s.defaults[key]
    assert cfg.read_bytes() == raw


def test_existing_projects_dir_kept(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _, _, loaded = _load(tmp_path, {"projects_directory": str(proj)})
    assert loaded["projects_directory"] == str(proj)


def test_backup_dir_with_existing_parent_kept(tmp_path):
    target = tmp_path / "new_backups"
    _, _, loaded = _load(tmp_path, {"backup_directory": str(target)})
    assert loaded["backup_directory"] == str(target)


def test_relative_projects_dir_resolved_from_repo_root(tmp_path):
    s, _, loaded = _load(tmp_path, {"projects_directory": "StoryBookManager"})
    assert Path(loaded["projects_directory"]) == s.project_root / "StoryBookManager"


def test_relative_projects_dir_saved_as_written(tmp_path):
    import json
    s, cfg, loaded = _load(tmp_path, {"projects_directory": "StoryBookManager"})
    s.settings = loaded
    assert s.save_settings()
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["projects_directory"] == "StoryBookManager"


def test_nothing_created_for_replaced_paths(tmp_path):
    _load(tmp_path, {"backup_directory": "/Users/nobody_f0/b", "logs_directory": "/Users/nobody_f0/l"})
    assert not Path("/Users/nobody_f0").exists()


def test_single_settings_singleton():
    stubs = {
        "tkinter": MagicMock(),
        "tkinter.ttk": MagicMock(),
        "tkinter.filedialog": MagicMock(),
        "tkinter.messagebox": MagicMock(),
    }
    sys.path.insert(0, str(project_root / "StoryBookManager"))
    # на выходе tkinter и StoryBookManager.* возвращаются как были: заглушки не
    # протекают в другие тесты
    with _tk_stubbed(stubs):
        import StoryBookManager.config.settings as canon
        from StoryBookManager.core import project_manager, file_manager
        from StoryBookManager.utils import logging_config
        for mod in (project_manager, file_manager, logging_config):
            assert mod.app_settings is canon.app_settings, mod.__name__


def test_unwritable_backup_dir_replaced(tmp_path, monkeypatch):
    import os
    target = tmp_path / "ro"
    target.mkdir()
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, m: False if str(p) == str(target) else real_access(p, m))
    s, _, loaded = _load(tmp_path, {"logs_directory": str(target)})
    assert loaded["logs_directory"] == s.defaults["logs_directory"]


_BAD_PATHS = {
    "projects_directory": "/Users/nobody/x/projects",
    "backup_directory": "/Users/nobody/x/backups",
    "logs_directory": "/Users/nobody/x/logs",
}


def test_save_without_changes_keeps_original_paths_on_disk(tmp_path):
    import json
    s, cfg, loaded = _load(tmp_path, dict(_BAD_PATHS))
    s.settings = loaded
    assert loaded["projects_directory"] == s.defaults["projects_directory"]
    assert s.save_settings()
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    for key, value in _BAD_PATHS.items():
        assert on_disk[key] == value
    # прочие настройки сохраняются как обычно
    s.set("window_geometry", "800x600")
    assert s.save_settings()
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["window_geometry"] == "800x600"
    assert on_disk["projects_directory"] == _BAD_PATHS["projects_directory"]


def test_explicit_set_of_replaced_path_is_saved(tmp_path):
    import json
    s, cfg, loaded = _load(tmp_path, dict(_BAD_PATHS))
    s.settings = loaded
    new_dir = tmp_path / "new_projects"
    new_dir.mkdir()
    s.set("projects_directory", str(new_dir))
    assert s.save_settings()
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["projects_directory"] == str(new_dir)
    assert on_disk["backup_directory"] == _BAD_PATHS["backup_directory"]
