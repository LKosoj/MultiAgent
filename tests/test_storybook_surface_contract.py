import json

import pytest

from custom_tools.storybook import project_paths, storybook_surface


def test_storybook_projects_root_prefers_env_over_cwd(tmp_path, monkeypatch):
    env_root = tmp_path / "env_storybooks"
    cwd_root = tmp_path / "plots" / "storybooks"
    cwd_root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(env_root))

    assert project_paths.storybook_projects_root() == env_root.resolve()


def test_storybook_projects_root_ignores_app_settings_and_uses_cwd(tmp_path, monkeypatch):
    from StoryBookManager.config.settings import app_settings

    settings_root = tmp_path / "settings_storybooks"
    cwd_root = tmp_path / "plots" / "storybooks"
    settings_root.mkdir(parents=True)
    cwd_root.mkdir(parents=True)
    original_projects_dir = app_settings.get("projects_directory")
    app_settings.set("projects_directory", str(settings_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STORYBOOK_PROJECTS_DIR", raising=False)
    try:
        assert project_paths.storybook_projects_root() == cwd_root.resolve()
    finally:
        app_settings.set("projects_directory", original_projects_dir)


@pytest.mark.parametrize(
    "project_id",
    [".", "./", "..", "../outside", "project/..", "a/../b", "a/b", r"a\b"],
)
def test_safe_storybook_project_dir_rejects_unsafe_project_id(tmp_path, monkeypatch, project_id):
    projects_dir = tmp_path / "storybooks"
    projects_dir.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects_dir))

    with pytest.raises(ValueError, match="project_id must be a safe path segment"):
        project_paths.safe_storybook_project_dir(project_id)


def test_storybook_project_inventory_lists_projects_artifacts_and_media(tmp_path, monkeypatch):
    projects_dir = tmp_path / "storybooks"
    project_dir = projects_dir / "demo"
    (project_dir / "20_story").mkdir(parents=True)
    (project_dir / "50_images" / "page_1").mkdir(parents=True)
    (project_dir / "97_shots" / "scene_1").mkdir(parents=True)
    (project_dir / "98_audio").mkdir(parents=True)
    (project_dir / "99_final").mkdir(parents=True)
    (project_dir / "00_brief.json").write_text(
        json.dumps(
            {
                "title": "Demo tale",
                "description": "Short description",
                "language": "ru",
                "main_characters": ["Alice"],
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "20_story" / "story.json").write_text(
        json.dumps({"pages": [{"page": 1}, {"page": 2}]}),
        encoding="utf-8",
    )
    (project_dir / "50_images" / "page_1" / "image.png").write_bytes(b"png")
    (project_dir / "97_shots" / "scene_1" / "shot.mp4").write_bytes(b"mp4")
    (project_dir / "98_audio" / "music.mp3").write_bytes(b"mp3")
    (project_dir / "99_final" / "final_review.json").write_text("{", encoding="utf-8")

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects_dir))

    result = storybook_surface.storybook_project_inventory("demo")

    assert result["projects_count"] == 1
    assert result["projects"][0]["project_id"] == "demo"
    selected = result["selected_project"]
    assert selected["preview"]["name"] == "Demo tale"
    assert selected["preview"]["pages_count"] == 2
    assert selected["artifacts"]["brief"]["status"] == "present"
    assert selected["artifacts"]["final_review"]["status"] == "invalid"
    assert selected["media"]["counts"] == {"image": 1, "video": 1, "audio": 1, "other": 0}
    assert {item["path"] for item in selected["media"]["items"]} == {
        "50_images/page_1/image.png",
        "97_shots/scene_1/shot.mp4",
        "98_audio/music.mp3",
    }


def test_storybook_project_inventory_rejects_path_escape(tmp_path, monkeypatch):
    projects_dir = tmp_path / "storybooks"
    projects_dir.mkdir()
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(projects_dir))

    with pytest.raises(ValueError, match="project_id must be a safe path segment"):
        storybook_surface.storybook_project_inventory("../outside")
