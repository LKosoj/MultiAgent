"""H-5: пустая генерация кадров -> RuntimeError (а не молчаливый «успех»).

Раньше shots_prompt_qa_tool при пустых items или пустом скоупе молча
возвращал shots_data, из-за чего пустая генерация помечалась как пройденная.
Теперь это явный отказ. Позитивный случай (есть шоты в скоупе) не падает и
считает items_total/missing_shots.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import custom_tools.storybook.shots_prompt_qa as sq


def _write_screenplay(root: Path, pid: str, scenes):
    base = root / pid / "91_screenplay"
    base.mkdir(parents=True, exist_ok=True)
    (base / "screenplay.json").write_text(
        json.dumps({"screenplay": scenes}, ensure_ascii=False), encoding="utf-8"
    )


def _scene(n, shots=(1,)):
    return {
        "scene_number": n,
        "action": "",
        "characters": [],
        "location_time": "",
        "storyboard": [
            {"shot_number": s, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}
            for s in shots
        ],
    }


def _item(scene, shot, shot_type="start"):
    return {
        "scene_number": scene,
        "shot_number": shot,
        "shot_type": shot_type,
        "video_prompt": "",
        "english_prompt": "x",
        "negative_prompt": "",
        "reference_image_paths": [],
        "_shot_frame_spec": {},
    }


@pytest.fixture
def stub_llm(monkeypatch):
    calls = {"n": 0}

    def fake_api(**kwargs):
        calls["n"] += 1
        return '{"repairs": [], "notes": "ok"}'

    monkeypatch.setattr(sq, "call_openai_api", fake_api)
    monkeypatch.setattr(sq, "_extract_shot_frame_spec_llm", lambda **k: {})
    return calls


@pytest.fixture(autouse=True)
def _projects_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    return tmp_path


def test_empty_items_raises(_projects_dir):
    _write_screenplay(_projects_dir, "proj", [_scene(1)])
    with pytest.raises(RuntimeError):
        sq.shots_prompt_qa_tool(
            session_id="s",
            project_id="proj",
            shots_data={"items": [], "consistency_rules": []},
            enable=True,
            force=True,
            model="hard",
            global_max_repairs=0,
            dry_run=True,
        )


def test_scope_with_no_shots_raises(_projects_dir):
    _write_screenplay(_projects_dir, "proj", [_scene(1)])
    with pytest.raises(RuntimeError):
        sq.shots_prompt_qa_tool(
            session_id="s",
            project_id="proj",
            shots_data={"items": [_item(1, 1)], "consistency_rules": []},
            enable=True,
            force=True,
            model="hard",
            scene_numbers=[999],  # нет такой сцены -> пустой скоуп
            global_max_repairs=0,
            dry_run=True,
        )


def test_positive_non_empty_scope_does_not_raise(_projects_dir, stub_llm):
    _write_screenplay(_projects_dir, "proj", [_scene(1, shots=(1, 2))])
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id="proj",
        shots_data={"items": [_item(1, 1), _item(1, 2)], "consistency_rules": []},
        enable=True,
        force=True,
        model="hard",
        scene_numbers=[1],
        global_max_repairs=0,
        dry_run=True,
    )
    report = result["_qa_report"]
    assert report["items_total"] == 2
    assert report["missing_shots"] == []
    assert stub_llm["n"] >= 1
