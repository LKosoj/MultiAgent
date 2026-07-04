"""M-27: per-scene coverage статуса валидации.

- Частичный скоуп не помечает shots.json как полностью провалидированный.
- Полный проход помечает prompts_validated=True и покрывает все сцены.
- Skip-гейт пропускает только если запрошенные сцены уже покрыты
  (prompts_validated_scenes), либо стоит легаси-флаг prompts_validated=True.
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
    _write_screenplay(tmp_path, "proj", [_scene(1), _scene(2)])
    return tmp_path


def _two_scene_items():
    return {"items": [_item(1, 1), _item(2, 1)], "consistency_rules": []}


def test_partial_scope_not_marked_fully_validated(stub_llm):
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id="proj",
        shots_data=_two_scene_items(),
        enable=True,
        force=True,
        model="hard",
        scene_numbers=[1],
        global_max_repairs=0,
        dry_run=True,
    )
    assert result["prompts_validated_scenes"] == [1]
    assert result["prompts_validated"] is False


def test_full_scope_marks_validated(stub_llm):
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id="proj",
        shots_data=_two_scene_items(),
        enable=True,
        force=True,
        model="hard",
        scene_numbers=None,  # все сцены
        global_max_repairs=0,
        dry_run=True,
    )
    assert result["prompts_validated_scenes"] == [1, 2]
    assert result["prompts_validated"] is True


def test_two_partial_runs_union_covers_all(stub_llm):
    data = _two_scene_items()
    r1 = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=data, enable=True,
        force=True, model="hard", scene_numbers=[1], global_max_repairs=0, dry_run=True,
    )
    assert r1["prompts_validated"] is False
    # Второй проход по сцене 2 объединяется с уже покрытой сценой 1.
    r2 = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=r1, enable=True,
        force=True, model="hard", scene_numbers=[2], global_max_repairs=0, dry_run=True,
    )
    assert r2["prompts_validated_scenes"] == [1, 2]
    assert r2["prompts_validated"] is True


def test_skip_gate_skips_when_requested_scene_covered(stub_llm):
    data = _two_scene_items()
    data["prompts_validated_scenes"] = [1, 2]
    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=data, enable=True,
        force=False, model="hard", scene_numbers=[1], global_max_repairs=0, dry_run=True,
    )
    # Пропуск: LLM не вызывался.
    assert stub_llm["n"] == 0
    assert "_qa_report" not in result


def test_skip_gate_runs_when_requested_scene_not_covered(stub_llm):
    data = _two_scene_items()
    data["prompts_validated_scenes"] = [1]
    sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=data, enable=True,
        force=False, model="hard", scene_numbers=[2], global_max_repairs=0, dry_run=True,
    )
    # Сцена 2 не покрыта -> валидация действительно запускается.
    assert stub_llm["n"] >= 1


def test_legacy_flag_skips_all(stub_llm):
    data = _two_scene_items()
    data["prompts_validated"] = True  # легаси-флаг без prompts_validated_scenes
    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=data, enable=True,
        force=False, model="hard", scene_numbers=[1], global_max_repairs=0, dry_run=True,
    )
    assert stub_llm["n"] == 0
    assert "_qa_report" not in result
