"""ProjectManager (Tk) делегирует логику в project_store, не трогая tkinter."""

import json
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project


from custom_tools.storybook.studio import project_store
from StoryBookManager.core.project_manager import Project, ProjectManager


@pytest.fixture
def pm(tmp_path):
    manager = ProjectManager()
    manager.projects_dir = tmp_path / "projects"
    manager.backup_dir = tmp_path / "backups"
    manager.projects_dir.mkdir()
    manager.backup_dir.mkdir()
    return manager


def make(pm, name="alpha"):
    base = pm.projects_dir / name
    make_series_project(pm.projects_dir, name)
    (base / "data.txt").write_text("x")
    return base


def test_structure_shared_and_generate_id(pm):
    assert ProjectManager.PROJECT_STRUCTURE is project_store.PROJECT_STRUCTURE
    assert pm.generate_project_id("Снег") == "sneg"
    assert not pm.generate_project_id("_x").startswith("_")


def test_backup_uses_instance_dirs_and_name_format(pm):
    make(pm)
    path = pm.backup_project("alpha")
    assert Path(path).parent == pm.backup_dir and "alpha_backup_" in Path(path).name
    assert (Path(path) / "data.txt").is_file()
    assert pm.backup_project("missing") is None
    assert pm.backup_project("../x") is None


def test_delete_false_on_backup_failure_and_busy(pm, monkeypatch):
    base = make(pm)
    monkeypatch.setattr(project_store, "create_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert pm.delete_project("alpha") is False and base.is_dir()
    monkeypatch.undo()
    handle = project_store.try_lock(base)
    try:
        assert pm.delete_project("alpha") is False and base.is_dir()
    finally:
        project_store.unlock(handle)
    assert pm.delete_project("alpha") is True and not base.exists()
    assert pm.delete_project("alpha") is False


def test_export_false_for_empty_and_true_with_progress(pm, tmp_path):
    empty = pm.projects_dir / "empty"
    empty.mkdir()
    assert pm.export_project("empty", str(tmp_path / "e.zip")) is False
    make(pm)
    calls = []
    assert pm.export_project("alpha", str(tmp_path / "a.zip"), lambda c, t: calls.append((c, t))) is True
    assert calls and calls[-1][0] == calls[-1][1]


def test_list_projects_and_preview_cover(pm):
    base = make(pm)
    (pm.projects_dir / "_shared_internal").mkdir()
    (base / "50_images/page_2").mkdir(parents=True)
    (base / "50_images/page_10").mkdir(parents=True)
    (base / "50_images/page_2/img_final.png").write_bytes(b"x")
    (base / "50_images/page_10/img_final.png").write_bytes(b"x")
    projects = pm.list_projects()
    assert [p.project_id for p in projects] == ["alpha"] and isinstance(projects[0], Project)
    assert projects[0].get_preview_info()["thumbnail"] == str(base / "50_images/page_2/img_final.png")
