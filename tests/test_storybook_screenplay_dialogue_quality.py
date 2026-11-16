import json

import pytest

from custom_tools.storybook import screenplay_generator as module


def _assert_dialogue_rules(prompt):
    for phrase in (
        'предыдущим действием или репликой', 'адресат', 'вопрос и ответ',
        'знания и цель', 'местоимений', 'понятны на слух и естественны', 'характеру и жанру', 'подтекст',
        'осмысленные обрывы', 'пересказом картинки', 'дословно',
        'line', 'direction', 'action/storyboard', 'voice ID',
    ):
        assert phrase in prompt


def test_generation_requests_dialogue_and_user_voice_rules(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    for name, data in {
        '00_brief.json': {},
        '20_story/story.json': {'pages': [{'body': 'История'}]},
        '20_bible/characters.json': [{'name': 'Герой', 'voice': 'user-id'}],
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(module, '_extract_story_anchors_llm', lambda **kwargs: {})
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError('captured')

    monkeypatch.setattr(module, '_llm_json_call', capture)
    with pytest.raises(RuntimeError, match='captured'):
        module.screenplay_generator_tool('s', 'p', 60, True)
    _assert_dialogue_rules(captured['system_prompt'])
    assert 'context_data.characters' in captured['system_prompt']
    assert 'иначе пустую строку' in captured['system_prompt']
    assert '"voice":' in captured['system_prompt']
    assert captured['payload']['context_data']['characters'][0]['voice'] == 'user-id'


def test_editor_requests_dialogue_rules_and_preserves_repaired_line_metadata(monkeypatch):
    dialogue = [
        {'id': 'a', 'order': 1, 'shot_number': 1, 'character': 'А', 'line': 'Подожди.', 'direction': 'тихо', 'pause_before': 0.2, 'pause_after': 0.3},
        {'id': 'b', 'order': 2, 'shot_number': 2, 'character': 'Б', 'line': 'От кого?', 'direction': 'ровно', 'pause_before': 0, 'pause_after': 0},
    ]
    scene = {'scene_number': 1, 'dialogue': dialogue, 'voiceover': 'Дословно.', 'sfx': [{'id': 'fx'}], 'storyboard': []}
    revised = {'scene_number': 1, 'dialogue': [{'id': 'b', 'line': 'Зачем ждать?'}]}
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return json.dumps({'screenplay_json': {'screenplay': [revised]}}, ensure_ascii=False)

    monkeypatch.setattr(module, 'call_openai_api', capture)
    result = module._validate_and_repair_screenplay_llm(story_anchors={}, screenplay_json={'screenplay': [scene]}, screenplay_time=60)
    _assert_dialogue_rules(captured['system_prompt'])
    for phrase in ('сцене целиком', 'id', 'order', 'shot_number', 'pause_before', 'pause_after'):
        assert phrase in captured['system_prompt']
    sent = json.loads(captured['prompt'])['screenplay_json']['screenplay'][0]
    assert sent['dialogue'] == dialogue
    assert sent['voiceover'] == scene['voiceover'] and sent['sfx'] == scene['sfx']
    merged = module._merge_checked_scenes([scene], result['screenplay_json']['screenplay'])[0]
    assert merged['dialogue'][0] == dialogue[0]
    assert merged['dialogue'][1] == {**dialogue[1], 'line': 'Зачем ждать?'}
    assert merged['voiceover'] == scene['voiceover'] and merged['sfx'] == scene['sfx']
