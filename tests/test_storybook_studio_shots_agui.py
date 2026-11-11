"""Ф6: шоты через диспетчер действий storybook.*."""

import json

import pytest

from tests.studio_run_fakes import make_series_project

from backend.fastapi_app.agui import _storybook_actions as actions
from backend.fastapi_app.agui.auth import Principal

USER = Principal(subject="u", roles=frozenset({"user"}))
ADMIN = Principal(subject="a", roles=frozenset({"admin", "user"}))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = make_series_project(tmp_path)
    (root / "97_shots").mkdir(parents=True)
    items = [{"scene_number": 1, "shot_number": 1, "shot_type": p, "english_prompt": f"p {p}", "negative_prompt": "",
              "video_prompt": "v" if p == "start" else "", "timing": "00:05"} for p in ("start", "end")]
    (root / "97_shots/shots.json").write_text(json.dumps({"items": items}))
    return root


@pytest.mark.parametrize("name", ["shots_list", "shots_keys", "shot_get", "shot_update", "shots_adopt_baseline"])
def test_admin_only(name):
    with pytest.raises(PermissionError):
        actions.require_storybook_role(f"storybook.{name}", USER)
    actions.require_storybook_role(f"storybook.{name}", ADMIN)
    assert actions.resolve(f"storybook.{name}").category == "shots"


def test_shape_conflict_and_ids_not_redacted(project):
    got = actions.dispatch("storybook.shot_get", {"project_id": "demo", "series_id": "main", "shot_key": "1-1"}, ADMIN)
    assert got["status"] == "success" and got["shot"]["shot_key"] == "1-1"
    digest = got["shot"]["digest"]
    ok = actions.dispatch("storybook.shot_update", {"project_id": "demo", "series_id": "main", "shot_key": "1-1",
                          "changes": {"start_prompt": "new"}, "base_digest": digest}, ADMIN)
    assert ok["status"] == "success" and ok["changed"] == ["start_prompt"]
    err = actions.dispatch("storybook.shot_update", {"project_id": "demo", "series_id": "main", "shot_key": "1-1",
                           "changes": {"start_prompt": "again"}, "base_digest": digest}, ADMIN)
    assert err["status"] == "error" and err["error"]["code"] == "conflict"
    assert err["error"]["details"]["current"]["start_prompt"] == "new"
    listing = actions.dispatch("storybook.shots_list", {"project_id": "demo", "series_id": "main"}, ADMIN)
    assert listing["items"][0]["shot_key"] == "1-1"
    bad = actions.dispatch("storybook.shots_list", {"project_id": "../x", "series_id": "main"}, ADMIN)
    assert bad["status"] == "error"
