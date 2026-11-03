import json
from pathlib import Path

import pytest

from backend.fastapi_app.agui import _storybook_actions as actions
from backend.fastapi_app.agui.auth import Principal
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.media_tokens import verify_media_token

USER = Principal(subject="u", roles=frozenset({"user"}))
ADMIN = Principal(subject="a", roles=frozenset({"admin", "user"}))


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "disabled")
    from backend.fastapi_app.agui import service as module

    return module


@pytest.fixture
def temp_caps(monkeypatch):
    monkeypatch.setattr(caps, "_REGISTRY", dict(caps.registry()))

    @caps.capability("tmp_read", title="x", access="read", project_scoped=False)
    def tmp_read() -> dict:
        return {"path": Path("/tmp/x")}

    @caps.capability("tmp_write", title="x", access="write", project_scoped=False)
    def tmp_write() -> dict:
        return {}

    @caps.capability("tmp_run", title="x", access="run", project_scoped=False)
    def tmp_run() -> dict:
        return {}


def test_unknown_action_not_classified(service):
    with pytest.raises(PermissionError, match="not classified"):
        service._require_service_action_role("storybook.nope", ADMIN)


@pytest.mark.parametrize("name", ["tmp_read", "tmp_write", "tmp_run"])
def test_every_access_level_is_admin_only(service, temp_caps, name):
    action = f"storybook.{name}"
    with pytest.raises(PermissionError, match="requires role 'admin'"):
        service._require_service_action_role(action, USER)
    service._require_service_action_role(action, ADMIN)


def test_read_capability_via_service(service, temp_caps):
    result = service.handle_service_action("storybook.tmp_read", {"__request_id": "r"}, principal=ADMIN)
    assert result["status"] == "success"
    json.dumps(result)
    with pytest.raises(PermissionError):
        service.handle_service_action("storybook.tmp_read", {}, principal=USER)


def test_errors_are_returned_not_raised(service, temp_caps):
    extra = service.handle_service_action("storybook.tmp_read", {"zzz": 1}, principal=ADMIN)
    assert extra["status"] == "error" and extra["error"]["code"] == "invalid_argument"
    bad = service.handle_service_action("storybook.media_token", {"project_id": "../x"}, principal=ADMIN)
    assert bad["error"]["code"] == "invalid_argument"


def test_result_is_serialized(service, temp_caps):
    result = service.handle_service_action("storybook.tmp_read", {}, principal=ADMIN)
    assert result["path"] == "/tmp/x"


def test_media_token_survives_redaction(service, tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k")
    (tmp_path / "demo").mkdir()
    result = service.handle_service_action("storybook.media_token", {"project_id": "demo"}, principal=ADMIN)
    assert result["status"] == "success" and verify_media_token("demo", result["t"])
