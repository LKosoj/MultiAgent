import json
from pathlib import Path

import yaml

from custom_tools.storybook.studio import errors, labels
from workflow.models import StepStatus, WorkflowStatus

ROOT = Path(__file__).resolve().parents[1]


def test_result_shapes():
    assert errors.success(a=1) == {"status": "success", "a": 1}
    err = errors.error_result("not_found", "нет", id=3)
    assert err == {"status": "error", "error": {"code": "not_found", "message": "нет", "details": {"id": 3}}}
    assert "details" not in errors.error_result("conflict", "x")["error"]
    assert errors.is_error(err) and not errors.is_error({"status": "success"})
    assert not errors.is_error("x")


def test_from_exception_maps_codes():
    assert errors.from_exception(errors.StudioError("locked", "занят"))["error"]["code"] == "locked"
    assert errors.from_exception(ValueError("bad"))["error"] == {"code": "invalid_argument", "message": "bad"}
    internal = errors.from_exception(RuntimeError("секрет"))
    assert internal["error"]["code"] == "internal"
    assert "секрет" not in json.dumps(internal, ensure_ascii=False)


def test_step_labels_match_pipeline_yaml():
    data = yaml.safe_load((ROOT / "workflow_pipelines" / "storybook_pipeline.yaml").read_text(encoding="utf-8"))
    assert list(labels.STEP_LABELS) == [step["id"] for step in data["steps"]]
    assert len(labels.STEP_LABELS) == len(data["steps"])


def test_statuses_cover_workflow_models():
    assert set(labels.STATUS_LABELS["step"]) == {s.value for s in StepStatus}
    assert set(labels.STATUS_LABELS["run"]) == {s.value for s in WorkflowStatus}


def test_unknown_key_returns_itself():
    assert labels.step_title("nope") == "nope"
    assert labels.step_description("nope") == ""
    assert labels.status_title("step", "zzz") == "zzz"
    assert labels.status_title("zzz", "a") == "a"
    assert labels.media_type_title("zzz") == "zzz"
    assert labels.step_title("video_generator") == "Генерация видео"
    assert labels.media_type_title("shot") == "Кадр шота"


