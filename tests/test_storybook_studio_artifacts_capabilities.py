"""Ф10: регистрация возможностей модуля artifacts."""

import inspect

from custom_tools.storybook.studio import capabilities as caps

EXPECTED = {
    "artifacts_list": "read",
    "artifact_read": "read",
    "artifact_validate": "read",
    "artifact_save": "write",
    "artifact_history": "read",
    "artifact_diff": "read",
    "artifact_restore": "write",
}


def test_seven_capabilities_with_access():
    reg = {cid: cap for cid, cap in caps.registry().items() if cap.module == "artifacts"}
    assert {cid: cap.access for cid, cap in reg.items()} == EXPECTED
    assert reg["artifacts_list"].supersedes == ("artifact_inventory",)
    assert reg["artifact_save"].supersedes == ("artifact_edit",)
    for cid, cap in reg.items():
        assert cap.client_fn == "".join(p if i == 0 else p.capitalize() for i, p in enumerate(cid.split("_")))
        assert cap.tk_handler == cid and cap.project_scoped
        params = inspect.signature(cap.func).parameters.values()
        assert not any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)


def test_invoke_rejects_extra_and_missing_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "demo").mkdir()
    extra = caps.invoke("artifact_read", {"project_id": "demo", "key": "brief", "force": True})
    assert extra["error"]["code"] == "invalid_argument" and "force" in extra["error"]["message"]
    # base_digest обязателен: null надо передавать явно
    missing = caps.invoke("artifact_save", {"project_id": "demo", "key": "brief", "content": {}})
    assert missing["error"]["code"] == "invalid_argument" and "base_digest" in missing["error"]["message"]
