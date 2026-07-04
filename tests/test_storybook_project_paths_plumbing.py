"""M-23: project-пути через safe_storybook_project_dir.

- Небезопасный project_id (traversal / абсолютный / с разделителями)
  отвергается ValueError в обоих тулах потока.
- При заданном STORYBOOK_PROJECTS_DIR пути строятся под ним (пламбинг реально
  использует helper, а не относительный plots/storybooks/...).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import custom_tools.storybook.shots_prompt_qa as sq
import custom_tools.storybook.prompt_engineer as pe
from custom_tools.storybook.project_paths import safe_storybook_project_dir


@pytest.fixture
def stub_llm(monkeypatch):
    calls = {"n": 0}

    def fake_api(**kwargs):
        calls["n"] += 1
        return '{"repairs": [], "notes": "ok"}'

    monkeypatch.setattr(sq, "call_openai_api", fake_api)
    monkeypatch.setattr(sq, "_extract_shot_frame_spec_llm", lambda **k: {})
    return calls


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "..", "/abs/path"])
def test_prompt_engineer_rejects_unsafe_project_id(bad_id, tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        pe.prompt_engineer_tool(session_id="s", project_id=bad_id)


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "..", "/abs/path"])
def test_shots_prompt_qa_rejects_unsafe_project_id(bad_id, tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        sq.shots_prompt_qa_tool(
            session_id="s",
            project_id=bad_id,
            shots_data={"items": [], "consistency_rules": []},
            enable=True,
            force=True,
            model="hard",
            dry_run=True,
        )


def test_safe_dir_resolves_under_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    resolved = safe_storybook_project_dir("proj")
    assert resolved == (tmp_path / "proj").resolve()


def test_shots_prompt_qa_reads_screenplay_from_projects_dir(tmp_path, monkeypatch, stub_llm):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "proj" / "91_screenplay"
    base.mkdir(parents=True)
    scene = {
        "scene_number": 1,
        "action": "",
        "characters": [],
        "location_time": "",
        "storyboard": [
            {"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}
        ],
    }
    (base / "screenplay.json").write_text(
        json.dumps({"screenplay": [scene]}, ensure_ascii=False), encoding="utf-8"
    )
    item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "", "english_prompt": "x", "negative_prompt": "",
        "reference_image_paths": [], "_shot_frame_spec": {},
    }
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id="proj",
        shots_data={"items": [item], "consistency_rules": []},
        enable=True,
        force=True,
        model="hard",
        global_max_repairs=0,
        dry_run=True,
    )
    # Screenplay найден под STORYBOOK_PROJECTS_DIR -> прошла реальная обработка.
    assert "_qa_report" in result
