"""Паритет: каждая возможность реестра есть в бэкенде, React-клиенте и Tk-мосте."""

import inspect
import os
import re
from pathlib import Path

import pytest

from backend.fastapi_app.agui import _storybook_actions as actions
from backend.fastapi_app.agui.auth import Principal
from custom_tools.storybook import video_contract
from custom_tools.storybook.studio import capabilities as caps
from StoryBookManager.core import studio_bridge

ROOT = Path(__file__).resolve().parents[1]
TS_DIR = ROOT / "frontend/client/src/app/lib/storybook"
USER = Principal(subject="u", roles=frozenset({"user"}))
ADMIN = Principal(subject="a", roles=frozenset({"admin", "user"}))


def _ts_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TS_DIR.glob("*.ts"))
        if path.name != "client.ts"
    )


def test_registry_is_consistent():
    reg = caps.registry()
    assert reg
    for key in ("client_fn", "tk_handler"):
        values = [getattr(cap, key) for cap in reg.values()]
        assert len(values) == len(set(values)), key
    legacy_ids = {action["id"] for action in video_contract._LEGACY_WORKFLOW_ACTIONS}
    for cap in reg.values():
        assert cap.access in ("read", "write", "run")
        assert set(cap.supersedes) <= legacy_ids


@pytest.mark.parametrize("cap_id", sorted(caps.registry()))
def test_capability_present_on_every_surface(cap_id):
    cap = caps.registry()[cap_id]
    # 1. функция
    assert callable(cap.func)
    params = inspect.signature(cap.func).parameters.values()
    assert not any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)
    assert inspect.signature(cap.func).return_annotation in (dict, "dict")
    # 2. бэкенд
    assert actions.resolve(cap.action) is cap
    with pytest.raises(PermissionError):
        actions.require_storybook_role(cap.action, USER)
    actions.require_storybook_role(cap.action, ADMIN)
    entries = {a["id"]: a for a in video_contract.storybook_workflow_actions()["actions"]}
    assert entries[cap.id]["service_action"] == cap.action
    # 3. React
    ts = _ts_sources()
    assert re.search(rf"export\s+(?:const|function)\s+{cap.client_fn}\b", ts), cap.client_fn
    assert re.search(rf"\.call(?:<[^>]*>)?\(\s*\"{cap.id}\"", ts), cap.id
    # 4. Tk
    handler = studio_bridge.tk_handlers().get(cap.tk_handler)
    assert callable(handler)


def test_no_orphan_ts_calls():
    ids = set(caps.registry())
    called = set(re.findall(r"\.call(?:<[^>]*>)?\(\s*\"([a-z0-9_]+)\"", _ts_sources()))
    assert called <= ids, called - ids


def test_workflow_actions_have_no_streamlit_surface():
    for action in video_contract.STORYBOOK_WORKFLOW_ACTIONS:
        assert "streamlit" not in action["surfaces"], action["id"]


@pytest.mark.skipif(os.getenv("STUDIO_PARITY_UI") != "1", reason="включается после треков У: STUDIO_PARITY_UI=1")
def test_screens_call_every_capability():
    texts = [
        p.read_text(encoding="utf-8", errors="ignore")
        for base, pattern in (
            (ROOT / "StoryBookManager/gui", "*.py"),
            (ROOT / "frontend/client/src/app/components", "*.ts*"),
            (ROOT / "frontend/client/src/app/lib/storybook", "use*.ts"),
        )
        for p in base.rglob(pattern)
    ]
    blob = "\n".join(texts)
    missing = [
        cap.id for cap in caps.registry().values()
        if f'"{cap.id}"' not in blob and f"'{cap.id}'" not in blob
        and not re.search(rf"\b{cap.client_fn}\b", blob)
    ]
    assert not missing, missing
