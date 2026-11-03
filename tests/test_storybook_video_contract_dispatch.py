"""video_contract с реальным YAML: шаг указывает на диспетчер, имя провайдера берётся из настроек."""

import json

import pytest

from custom_tools.storybook import video_contract as vc
from custom_tools.storybook import video_run_config as cfg


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "ROUTERAI_API_KEY", "ROUTERAI_VIDEO_MODEL", "AITUNNEL_API_KEY",
                 "AITUNNEL_VIDEO_MODEL", "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    cfg.bind_run_project(None)
    base = tmp_path / "demo"
    base.mkdir()
    yield base
    cfg.bind_run_project(None)


def write_settings(project, data):
    (project / ".studio").mkdir(exist_ok=True)
    (project / ".studio" / "run_settings.json").write_text(json.dumps(data))


def test_yaml_step_points_to_dispatcher():
    assert vc._video_step_tool_name() == cfg.DISPATCH_TOOL_NAME


def test_active_tool_name_is_never_the_dispatcher(project):
    assert vc._active_video_tool_name() == "video_generator_routerai_tool"
    write_settings(project, {"video": {"provider": "veo"}})
    with vc._scoped_provider("demo"):
        assert vc._active_video_tool_name() == "video_generator_veo_tool"
    assert vc._active_video_tool_name() != cfg.DISPATCH_TOOL_NAME


def test_active_provider_reads_project_file_and_explicit_input(project):
    assert vc._active_video_provider("demo") == "routerai"
    write_settings(project, {"video": {"provider": "aitunnel"}})
    assert vc._active_video_provider("demo") == "aitunnel"
    assert vc._active_video_provider("demo", "veo") == "veo"


def test_preflight_provider_default_and_explicit(project):
    default = vc.storybook_video_preflight_tool(session_id="s", project_id="demo")
    assert default["provider"] == "routerai"
    explicit = vc.storybook_video_preflight_tool(session_id="s", project_id="demo", video_provider="aitunnel")
    assert explicit["provider"] == "aitunnel"
    promise = vc.storybook_video_delivery_promise_tool(session_id="s", project_id="demo", video_provider="aitunnel")
    assert promise["provider"] == "aitunnel"


def test_video_capability_follows_selected_provider_credentials(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m")
    assert vc.storybook_video_preflight_tool(session_id="s", project_id="demo")["capabilities"]["video"] is True
    # выбран провайдер, для которого ключа нет
    out = vc.storybook_video_preflight_tool(session_id="s", project_id="demo", video_provider="aitunnel")
    assert out["capabilities"]["video"] is False


def test_old_tests_style_lambda_patch_still_works(project, monkeypatch):
    monkeypatch.setattr(vc, "_active_video_tool_name", lambda: "video_generator_aitunnel_tool")
    assert vc._active_video_provider("demo") == "aitunnel"


def test_readiness_uses_project_settings(project):
    write_settings(project, {"video": {"provider": "aitunnel"}})
    assert vc.storybook_video_music_readiness("demo")["video"]["provider"] == "aitunnel"
