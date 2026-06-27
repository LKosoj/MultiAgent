import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def workflows_page(monkeypatch):
    if "streamlit" not in sys.modules:
        monkeypatch.setitem(sys.modules, "streamlit", types.SimpleNamespace())

    module_name = "workflows_page_storybook_readiness_test"
    module_path = (
        Path(__file__).resolve().parents[1]
        / "streamlit_app"
        / "pages"
        / "02_Workflows.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_is_storybook_workflow_accepts_name_dict_and_object(workflows_page):
    class WorkflowInfo:
        name = "storybook_pipeline"

    assert workflows_page._is_storybook_workflow("storybook_pipeline")
    assert workflows_page._is_storybook_workflow({"workflow_name": "storybook_pipeline"})
    assert workflows_page._is_storybook_workflow(WorkflowInfo())
    assert not workflows_page._is_storybook_workflow("text_to_sql_pipeline")


def test_storybook_readiness_payload_uses_defaults_without_task(workflows_page):
    payload = workflows_page._storybook_readiness_payload_from_params(
        {"project_id": " custom-project ", "generate_screenplay": "false"},
        {
            "project_id": "default-project",
            "language": "en",
            "generate_screenplay": True,
            "generate_music": "yes",
            "task": "",
        },
    )

    assert payload == {
        "project_id": "custom-project",
        "session_id": "streamlit-readiness",
        "language": "en",
        "enable": False,
        "generate_music": True,
    }
    assert "task" not in payload


def test_load_storybook_readiness_delegates_to_video_contract(workflows_page, monkeypatch):
    from custom_tools.storybook import video_contract

    calls = []

    def fake_storybook_video_music_readiness(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "project_id": kwargs["project_id"]}

    monkeypatch.setattr(
        video_contract,
        "storybook_video_music_readiness",
        fake_storybook_video_music_readiness,
    )

    readiness = workflows_page._load_storybook_readiness(
        {"project_id": "project-1", "language": "ru", "generate_music": False},
        {"generate_screenplay": True},
    )

    assert readiness == {"status": "success", "project_id": "project-1"}
    assert calls == [
        {
            "project_id": "project-1",
            "session_id": "streamlit-readiness",
            "language": "ru",
            "enable": True,
            "generate_music": False,
        }
    ]


def test_load_storybook_project_inventory_delegates_to_surface_contract(workflows_page, monkeypatch):
    from custom_tools.storybook import storybook_surface

    calls = []

    def fake_inventory(project_id):
        calls.append(project_id)
        return {"status": "success", "project_id": project_id}

    monkeypatch.setattr(storybook_surface, "storybook_project_inventory", fake_inventory)

    inventory = workflows_page._load_storybook_project_inventory(
        {"project_id": "project-1"},
        {"project_id": "default-project"},
    )

    assert inventory == {"status": "success", "project_id": "project-1"}
    assert calls == ["project-1"]


def test_load_storybook_validation_uses_pipeline_runner_without_init(workflows_page, monkeypatch):
    runner_module = types.ModuleType("StoryBookManager.core.pipeline_runner")
    calls = []

    class FakePipelineRunner:
        def validate_project_for_pipeline(self, project_id, start_step=None):
            calls.append({"project_id": project_id, "start_step": start_step})
            return {"valid": True, "message": "ok", "errors": [], "warnings": []}

    runner_module.PipelineRunner = FakePipelineRunner
    monkeypatch.setitem(sys.modules, "StoryBookManager.core.pipeline_runner", runner_module)

    validation = workflows_page._load_storybook_validation(
        {"project_id": "project-1", "start_step": "story_writer"},
        {},
    )

    assert validation == {"valid": True, "message": "ok", "errors": [], "warnings": []}
    assert calls == [{"project_id": "project-1", "start_step": "story_writer"}]


def test_load_storybook_validation_rejects_project_id_path_traversal(workflows_page):
    validation = workflows_page._load_storybook_validation({"project_id": "../outside"}, {})

    assert validation["valid"] is False
    assert "project_id must be a safe path segment" in validation["message"]


def test_storybook_music_status_label_prioritizes_failed_state(workflows_page):
    assert (
        workflows_page._storybook_music_status_label(
            {"enabled": True, "configured": True, "music_exists": True, "status": "failed"},
            [],
        )
        == "failed"
    )
    assert (
        workflows_page._storybook_music_status_label(
            {"enabled": True, "configured": True, "music_exists": True, "status": "success"},
            ["music_generation_failed"],
        )
        == "failed"
    )
    assert workflows_page._storybook_music_status_label({"enabled": False}, []) == "disabled"


def test_render_storybook_readiness_summary_includes_errors_and_final_artifacts(workflows_page, monkeypatch):
    calls = []

    class Column:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_st = types.SimpleNamespace(
        markdown=lambda text: calls.append(("markdown", text)),
        warning=lambda text: calls.append(("warning", text)),
        error=lambda text: calls.append(("error", text)),
        success=lambda text: calls.append(("success", text)),
        json=lambda value: calls.append(("json", value)),
        metric=lambda label, value: calls.append(("metric", label, value)),
        columns=lambda count: [Column() for _ in range(count)],
    )
    monkeypatch.setattr(workflows_page, "st", fake_st)

    workflows_page._render_storybook_readiness_summary(
        {
            "capabilities": {"video": True, "music": True, "render": True},
            "video": {"provider": "aitunnel", "expected_clip_count": 1},
            "music": {"enabled": True, "configured": True, "music_exists": True},
            "render": {"configured": True},
            "blocking_reasons": [],
            "warnings": [],
            "errors": ["final_review_failed"],
            "artifacts": {
                "asset_manifest": {"status": "present"},
                "edit_decisions": {"status": "present"},
                "render_report": {"status": "present"},
                "final_review": {"status": "failed"},
            },
        }
    )

    rendered = "\n".join(str(call) for call in calls)
    assert "final_review_failed" in rendered
    assert "`asset_manifest`=present" in rendered
    assert "`edit_decisions`=present" in rendered
    assert "`render_report`=present" in rendered


def test_render_storybook_project_inventory_reports_invalid_and_missing_artifacts(workflows_page, monkeypatch):
    calls = []

    class Column:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_st = types.SimpleNamespace(
        markdown=lambda text: calls.append(("markdown", text)),
        warning=lambda text: calls.append(("warning", text)),
        error=lambda text: calls.append(("error", text)),
        json=lambda value: calls.append(("json", value)),
        metric=lambda label, value: calls.append(("metric", label, value)),
        columns=lambda count: [Column() for _ in range(count)],
    )
    monkeypatch.setattr(workflows_page, "st", fake_st)

    workflows_page._render_storybook_project_inventory(
        {
            "projects_count": 1,
            "selected_project": {
                "preview": {"pages_count": 2},
                "artifacts": {
                    "brief": {"status": "present"},
                    "shots": {"status": "missing"},
                    "final_review": {"status": "invalid"},
                },
                "media": {"counts": {"image": 3, "video": 1}},
            },
        }
    )

    rendered = "\n".join(str(call) for call in calls)
    assert "Invalid JSON artifacts: final_review" in rendered
    assert "Missing JSON artifacts: shots" in rendered
    assert "('metric', 'Images', 3)" in rendered
