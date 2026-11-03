import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.media_tokens import verify_media_token


def test_core_registers_media_token():
    reg = caps.registry()
    assert reg["media_token"].access == "read" and reg["media_token"].project_scoped


def test_media_token(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("STORYBOOK_MEDIA_SIGNING_KEY", "k")
    (tmp_path / "demo").mkdir()
    result = caps.invoke("media_token", {"project_id": "demo"})
    assert result["status"] == "success" and verify_media_token("demo", result["t"])
    assert caps.invoke("media_token", {"project_id": "ghost"})["error"]["code"] == "not_found"
    assert caps.invoke("media_token", {"project_id": ".."})["error"]["code"] == "invalid_argument"
