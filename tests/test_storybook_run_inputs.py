"""settings_to_inputs: входы пайплайна из настроек прогона (их вызывает Ф4)."""

import json

import pytest

from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.studio import run_settings as rs
from custom_tools.storybook.studio.errors import StudioError


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "STORYBOOK_STYLE_PROFILE", "SUNO_COOKIE", "ROUTERAI_VIDEO_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m")
    monkeypatch.setenv("SUNO_COOKIE", "c")
    monkeypatch.setenv("AITUNNEL_API_KEY", "k")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "m")
    base = tmp_path / "demo"
    base.mkdir()
    cfg.bind_run_project(None)
    return base


def save(project, data):
    (project / ".studio").mkdir(exist_ok=True)
    (project / ".studio" / "run_settings.json").write_text(json.dumps(data))


def test_defaults_contain_only_primitives_and_no_optional_keys(project):
    inputs = rs.settings_to_inputs("demo")
    assert inputs == {"video_provider": "routerai", "generate_music": True, "generate_sfx": True,
                      "generate_subtitles": True, "budget_rub": 0, "narration_path": "",
                      "style_profile": "", "style_preset": ""}
    assert all(isinstance(v, (str, bool, int, float)) for v in inputs.values())


def test_optional_keys_only_when_set(project):
    save(project, {"video": {"provider": "aitunnel", "allowed_durations": [5, 10]},
                   "audio": {"sfx": False, "subtitles": False},
                   "advanced": {"skip_prompt_enhancement": False, "sample_shot_key": "1-2"}})
    inputs = rs.settings_to_inputs("demo")
    assert inputs["video_provider"] == "aitunnel"
    assert inputs["blockout_allowed_durations"] == [5, 10]
    assert inputs["generate_sfx"] is False and inputs["generate_subtitles"] is False
    assert inputs["skip_prompt_enhancement"] is False and inputs["sample_shot_key"] == "1-2"
    assert "force_update_prompts" not in inputs and "final_allow_missing_audio" not in inputs


def test_provider_matches_video_run_config(project, monkeypatch):
    monkeypatch.setenv("STORYBOOK_VIDEO_PROVIDER", "aitunnel")
    assert rs.settings_to_inputs("demo")["video_provider"] == cfg.effective_provider("demo")
    save(project, {"video": {"provider": "routerai"}})
    assert rs.settings_to_inputs("demo")["video_provider"] == cfg.effective_provider("demo") == "routerai"


def test_error_issues_raise(project):
    save(project, {"video": {"retry_cycles": 99}})
    with pytest.raises(StudioError) as exc:
        rs.settings_to_inputs("demo")
    assert exc.value.code == "invalid_argument"
