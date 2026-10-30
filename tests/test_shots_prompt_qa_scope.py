"""M-27: per-scene coverage статуса валидации.

- Частичный скоуп не помечает shots.json как полностью провалидированный.
- Полный проход помечает prompts_validated=True и покрывает все сцены.
- Skip-гейт пропускает только если запрошенные сцены уже покрыты
  (prompts_validated_scenes), либо стоит легаси-флаг prompts_validated=True.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import custom_tools.storybook.shots_prompt_qa as sq
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


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
        "_shot_frame_spec_cache_key": shared_utils._build_shot_frame_spec_cache_key(
            scene_number=scene,
            shot_number=shot,
            shot_description="",
            camera_plan="MEDIUM SHOT",
            scene_characters=[],
            location_time="",
            location_canon_name="",
        ),
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
    data["prompts_validated_spec_version"] = shared_utils._SHOT_FRAME_SPEC_VERSION
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


def test_legacy_flag_without_version_is_revalidated(stub_llm):
    data = _two_scene_items()
    data["prompts_validated"] = True  # легаси-флаг без prompts_validated_scenes
    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj", shots_data=data, enable=True,
        force=False, model="hard", scene_numbers=[1], global_max_repairs=0, dry_run=True,
    )
    assert stub_llm["n"] >= 1
    assert "_qa_report" in result


def test_shot_scope_does_not_apply_repair_to_unselected_shot(stub_llm, monkeypatch):
    # The model may return a neighboring-shot repair; shot-level scope must discard it.
    project_root = Path(os.environ["STORYBOOK_PROJECTS_DIR"])
    _write_screenplay(project_root, "proj", [_scene(1, shots=(1, 2)), _scene(2)])
    before = _item(1, 1)
    selected = _item(1, 2)

    def fake_api(**kwargs):
        return json.dumps({
            "repairs": [
                {"scene_number": 1, "shot_number": 1, "shot_type": "start",
                 "reasons": ["neighbor"], "set": {"english_prompt": "wrong"}},
                {"scene_number": 1, "shot_number": 2, "shot_type": "start",
                 "reasons": ["selected"], "set": {"english_prompt": "fixed"}},
            ],
            "notes": "ok",
        })

    stub_llm["n"] = 0
    monkeypatch.setattr(sq, "call_openai_api", fake_api)
    result = sq.shots_prompt_qa_tool(
        session_id="s", project_id="proj",
        shots_data={"items": [before, selected], "consistency_rules": []},
        enable=True, force=True, model="hard", shot_keys=["1-2"],
        global_max_repairs=0, dry_run=True,
    )
    assert result["items"][0]["english_prompt"] == "x"
    assert result["items"][1]["english_prompt"] == "fixed"
    assert result["prompts_validated"] is False


def test_qa_reference_repair_accepts_only_existing_previous_end(tmp_path):
    previous = tmp_path / 'previous.png'
    previous.write_bytes(b'frame')
    old = tmp_path / 'older.png'
    old.write_bytes(b'frame')
    other = tmp_path / 'other.png'
    other.write_bytes(b'frame')
    missing = tmp_path / 'missing.png'
    items = [_item(1, 1, 'end'), _item(1, 3, 'end'), _item(1, 5, 'start'),
             _item(1, 5, 'end'), _item(2, 3, 'end'), _item(2, 5, 'start')]
    items[0]['output_path'], items[1]['output_path'], items[4]['output_path'] = str(old), str(previous), str(other)
    def apply(scene, shot, phase, path):
        sq._apply_repairs_to_items(items, [{'scene_number': scene, 'shot_number': shot, 'shot_type': phase,
                                          'set': {'reference_image_paths': [str(path)]}}])
    apply(1, 5, 'start', previous)
    assert items[2]['reference_image_paths'] == [str(previous)]
    items[2]['reference_image_paths'] = []
    for path in (old, other, missing):
        apply(1, 5, 'start', path)
        assert not items[2]['reference_image_paths']
    apply(1, 5, 'end', previous)
    assert not items[3]['reference_image_paths']


def test_previous_end_is_offered_and_shown_to_visual_audit(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    _write_screenplay(tmp_path, 'p', [_scene(1, (1, 3))])
    base = tmp_path / 'p'
    previous = base / 'previous.png'
    current = base / 'current.png'
    Image.new('RGB', (16, 16)).save(previous)
    Image.new('RGB', (16, 16)).save(current)
    items = [_item(1, 1, 'end'), _item(1, 3, 'start')]
    items[0]['output_path'], items[1]['output_path'] = str(previous), str(current)
    (base / '97_shots').mkdir()
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    rendered, calls = [], []
    monkeypatch.setattr(sq, '_scene_render_review_image', lambda frames: rendered.append(frames) or 'image')
    monkeypatch.setattr(sq, '_extract_shot_frame_spec_llm', lambda **kwargs: {})
    def model(**kwargs):
        calls.append(kwargs)
        return '{"violations": []}' if 'AUDIT_INPUT:' in kwargs['prompt'] else '{"repairs": []}'
    monkeypatch.setattr(sq, 'call_openai_api', model)
    sq.shots_prompt_qa_tool('s', 'p', shots_data={'items': [items[1]]}, shot_keys=['1-3'], force=True, dry_run=True)
    assert any(any(item.get('shot_type') == 'PREVIOUS_END_IDENTITY' and item['output_path'] == str(previous)
                   for item in group) for group in rendered)
    audit = next(call for call in calls if 'AUDIT_INPUT:' in call['prompt'])
    assert 'only for object identity' in audit['system_prompt']
    scene_call = next(call for call in calls if 'INPUT:\n' in call['prompt'] and 'AUDIT_INPUT:' not in call['prompt'])
    payload = json.loads(scene_call['prompt'].split('INPUT:\n', 1)[1])
    assert any(ref['reference_image_path'] == str(previous) for ref in payload['available_references'])
    assert items[1]['reference_image_paths'] == []
    previous.unlink()
    assert sq._previous_end_paths(items) == {}
