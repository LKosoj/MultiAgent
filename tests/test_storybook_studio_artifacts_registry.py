"""Ф10: реестр артефактов и artifacts_list."""

import json
import re
from pathlib import Path

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook import video_contract
from custom_tools.storybook.storybook_surface import JSON_ARTIFACTS
from custom_tools.storybook.studio import artifacts as art
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import labels, schemas

FIXTURES = Path(__file__).parent / "fixtures" / "storybook_artifacts"
READ_ONLY = {"chains", "provider_jobs", "final_manifest", "asset_manifest", "edit_decisions",
             "render_report", "final_review", "video_plan"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    series = make_series_project(tmp_path)
    with series_scope("demo", "main"):
        yield series


def _list(project_id="demo"):
    result = caps.invoke("artifacts_list", {"project_id": project_id, "series_id": "main"})
    assert result["status"] == "success", result
    return {item["artifact_key"]: item for item in result["items"]}, result


def test_registry_matches_surface_paths_and_fixtures():
    assert list(art.ARTIFACTS) == list(JSON_ARTIFACTS)
    assert len(art.ARTIFACTS) == 23
    for key, spec in art.ARTIFACTS.items():
        assert spec.title and spec.path == JSON_ARTIFACTS[key]
        fixture = json.loads((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))
        assert isinstance(fixture, list if spec.root == "array" else dict), key
        assert schemas.load_schema(key)["title"] == spec.title, key
        assert schemas.load_schema(key)["type"] == spec.root, key


def test_writers_are_real_pipeline_steps():
    pipeline = Path(__file__).parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
    text = pipeline.read_text(encoding="utf-8")
    for spec in art.ARTIFACTS.values():
        for step in spec.writers:
            assert step in labels.STEP_LABELS, (spec.key, step)
            assert re.search(rf"^- id: {step}$", text, re.M), step


def test_read_only_set_and_reasons():
    assert {k for k, s in art.ARTIFACTS.items() if not s.editable} == READ_ONLY
    for key in READ_ONLY:
        assert art.ARTIFACTS[key].read_only_reason
    assert [k for k, s in art.ARTIFACTS.items() if s.merge_safe] == ["shots"]


def test_empty_project_all_missing(project):
    items, result = _list()
    assert len(items) == 23
    assert result["run"] == {"active": False, "current_steps": None}
    for item in items.values():
        assert item["status"] == "missing" and not item["exists"] and item["history_count"] == 0
        assert item["produced_by"].startswith("Создаёт") or item["produced_by"].startswith("Создают")
    assert "«Бриф из промпта»" in items["brief"]["produced_by"]
    assert items["brief"]["run_state"] == "free"


def test_present_invalid_and_too_big(project, monkeypatch):
    (project / "00_brief.json").write_text('{"title": "x"}', encoding="utf-8")
    (project / "10_synopsis").mkdir()
    (project / "10_synopsis" / "synopsis.json").write_text('{\n  "a": 1,\n  oops\n}', encoding="utf-8")
    items, _ = _list()
    assert items["brief"]["status"] == "present" and items["brief"]["size"] > 0 and items["brief"]["modified"]
    bad = items["synopsis"]
    assert bad["status"] == "invalid" and bad["error"]["line"] == 3 and bad["error"]["column"]
    monkeypatch.setattr(art, "MAX_BYTES", 5)
    items, _ = _list()
    assert items["brief"]["status"] == "invalid" and "большой" in items["brief"]["error"]["message"]


def test_no_absolute_paths_in_result(project):
    (project / "00_brief.json").write_text("{}", encoding="utf-8")
    _, result = _list()
    blob = json.dumps(result, ensure_ascii=False)
    assert str(project.parent) not in blob and '"/' not in blob


def test_history_count_grows_after_saves(project):
    brief = json.loads((FIXTURES / "brief.json").read_text(encoding="utf-8"))
    digest = None
    for idx in range(3):
        brief["pages_max"] = 3 + idx
        saved = caps.invoke("artifact_save", {"project_id": "demo", "series_id": "main", "key": "brief", "base_digest": digest,
                                              "content": dict(brief)})
        assert saved["status"] == "success", saved
        digest = saved["digest"]
    items, _ = _list()
    assert items["brief"]["history_count"] == 2  # первое сохранение - создание, снимка нет


def test_run_state_uses_pipeline_lock(project):
    from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock

    handle = acquire_storybook_project_lock("demo")
    try:
        items, result = _list()
    finally:
        release_storybook_project_lock(handle)
    assert result["run"]["active"] is True
    assert items["brief"]["run_state"] == "busy" and "Идёт прогон" in items["brief"]["run_state_message"]
    assert items["shots"]["run_state"] == "warn"


def test_supersedes_point_to_real_legacy_ids():
    legacy = {a["id"] for a in video_contract._LEGACY_WORKFLOW_ACTIONS}
    for cap in caps.registry().values():
        if cap.module == "artifacts":
            assert set(cap.supersedes) <= legacy


def test_unknown_project(project):
    assert caps.invoke("artifacts_list", {"project_id": "ghost", "series_id": "main"})["error"]["code"] == "not_found"
