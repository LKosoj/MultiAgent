"""Tk ProjectManager: файлы в 97_shots/_versions не попадают в счётчики (запускать отдельным процессом)."""

import json

from StoryBookManager.core.project_manager import Project


def test_structure_and_has_video_skip_versions(tmp_path):
    root = tmp_path / "p"
    (root / "97_shots/_versions/1-1/v1").mkdir(parents=True)
    (root / "97_shots/_versions/1-1/v1/old.mp4").write_bytes(b"v")
    (root / "97_shots/_versions/1-1/v1/old.png").write_bytes(b"p")
    (root / "97_shots/scene_01_shot_01").mkdir()
    (root / "97_shots/scene_01_shot_01/new.png").write_bytes(b"p")
    (root / "00_brief.json").write_text(json.dumps({"title": "T"}), encoding="utf-8")
    project = Project("p", root)
    assert project.get_preview_info()["has_video"] is False
    struct = project.get_files_structure()["media_dirs"]["shots"]
    assert struct["files_count"] == 1 and struct["files"] == ["scene_01_shot_01/new.png"]
