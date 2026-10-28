import json
import pytest
from custom_tools.storybook import story_writer, bible_builder, style_keeper

@pytest.mark.parametrize('module,tool', [(story_writer, 'story_writer_tool'), (bible_builder, 'bible_builder_tool'), (style_keeper, 'style_keeper_tool')])
def test_generation_receives_original_source_brief(tmp_path, monkeypatch, module, tool):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    brief = {'storybook_prompt': 'Exact supplied poem and director screenplay', 'target_age': '18+'}
    for name, data in {'00_brief.json': brief, '10_synopsis/synopsis.json': {}, '10_synopsis/beats.json': [], '20_bible/characters.json': [], '20_bible/locations.json': []}.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    captured = {}
    def capture(**kwargs):
        captured.update(json.loads(kwargs['prompt']))
        raise RuntimeError('captured model input')
    monkeypatch.setattr(module, 'call_openai_api', capture)
    with pytest.raises((RuntimeError, ValueError)):
        getattr(module, tool)('s', 'p')
    assert captured.get('brief', {}).get('storybook_prompt') == brief['storybook_prompt']

def test_screenplay_receives_original_source_brief(tmp_path, monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    brief = {'storybook_prompt': 'Exact supplied director screenplay'}
    for name, data in {'00_brief.json': brief, '20_story/story.json': {'pages': [{'body': 'Poem'}]}}.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    monkeypatch.setattr(module, '_extract_story_anchors_llm', lambda **kwargs: {})
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs['payload']['context_data'])
        captured['max_tokens'] = kwargs['max_tokens']
        raise RuntimeError('captured model input')
    monkeypatch.setattr(module, '_llm_json_call', capture)
    with pytest.raises(RuntimeError, match='captured model input'):
        module.screenplay_generator_tool('s', 'p', 178, True)
    assert captured['brief'] == brief
    assert captured['max_tokens'] <= 65536

def test_bible_retries_mixed_script_before_saving(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    for name in ['synopsis', 'beats']:
        path = base / '10_synopsis' / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}' if name == 'synopsis' else '[]')
    replies = iter([{'characters': [{'name': 'Мальчик', 'description': 'рот 始终保持 закрыт'}]}, {'characters': [{'name': 'Мальчик', 'description': 'рот закрыт'}]}])
    monkeypatch.setattr(bible_builder, 'call_openai_api', lambda **kwargs: json.dumps(next(replies), ensure_ascii=False))
    bible_builder.bible_builder_tool('s', 'p', language='ru')
    assert '始终保持' not in (base / '20_bible/characters.json').read_text()
    assert 'рот закрыт' in (base / '20_bible/characters.json').read_text()

def test_screenplay_anchors_receive_supplied_director_source(tmp_path, monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    captured = {}
    def capture(**kwargs):
        captured.update(json.loads(kwargs['prompt']))
        return '{}'
    monkeypatch.setattr(module, 'call_openai_api', capture)
    brief = {'storybook_prompt': 'Director ending beyond the voiceover poem'}
    module._extract_story_anchors_llm(story_text='Original poem', title='T', target_age='16+', genre='satire', moral='', screenplay_time=178, source_brief=brief)
    assert captured['source_brief'] == brief

def test_screenplay_compaction_preserves_sound_and_voiceover():
    from custom_tools.storybook import screenplay_generator as module
    sfx = {'prompt': 'Wind, then glass breaking', 'duration': 18}
    data = {'screenplay': [{'scene_number': 1, 'voiceover': 'Original verse', 'sfx': sfx, 'storyboard': [{'shot_number': 1, 'voiceover': 'Original verse', 'description': 'Visible action'}]}]}
    scene = module._compact_screenplay_for_alignment(data)['screenplay'][0]
    assert scene['sfx'] == sfx
    assert scene['voiceover'] == 'Original verse'
    assert scene['storyboard'][0]['voiceover'] == 'Original verse'


def test_storyboard_requests_voiceover_separately_from_visible_action(monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return {'storyboard': []}
    monkeypatch.setattr(module, '_llm_json_call', capture)
    module._generate_storyboard_for_scene_llm(scene_number=1, screenplay_core={'screenplay': [{'scene_number': 1, 'voiceover': 'Original verse'}]}, story_anchors={}, bible_location_names=[])
    assert '"voiceover"' in captured['system_prompt']


def test_screenplay_repairs_only_mixed_script_fields(monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    data = {'screenplay': [{'voiceover': 'Ноябрь. Москва.'}], 'director_notes': {'technical_aspects': 'Короткая 三维-вставка'}}
    def fake_call(**kwargs):
        assert kwargs['payload']['texts'] == ['Короткая 三维-вставка']
        return {'texts': ['Короткая трёхмерная вставка']}
    monkeypatch.setattr(module, '_llm_json_call', fake_call)
    module._repair_mixed_script_fields(data)
    assert data['director_notes']['technical_aspects'] == 'Короткая трёхмерная вставка'
    assert data['screenplay'][0]['voiceover'] == 'Ноябрь. Москва.'


def test_screenplay_clean_language_needs_no_repair_call(monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    def unexpected(**kwargs):
        raise AssertionError('Clean fields must not be regenerated')
    monkeypatch.setattr(module, '_llm_json_call', unexpected)
    module._repair_mixed_script_fields({'notes': 'Акварель, графика, cut-out, 50mm'})


def test_screenplay_alignment_receives_original_director_source(monkeypatch):
    from custom_tools.storybook import screenplay_generator as module
    captured = {}
    def capture(**kwargs):
        captured.update(json.loads(kwargs['prompt']))
        return '{}'
    monkeypatch.setattr(module, 'call_openai_api', capture)
    brief = {'storybook_prompt': 'Maurois is visible only from scene7; two people sit opposite in scene8'}
    module._validate_and_repair_screenplay_llm(story_anchors={}, screenplay_json={'screenplay': []}, screenplay_time=178, source_brief=brief)
    assert captured['source_brief'] == brief


def test_bible_requests_era_and_environment_for_each_reference(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    for name, value in {'10_synopsis/synopsis.json': {}, '10_synopsis/beats.json': [], '20_bible/characters.json': [{'name': 'Мальчик'}]}.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return '{"characters": [], "locations": [], "consistency_rules": []}'
    monkeypatch.setattr(bible_builder, 'call_openai_api', capture)
    bible_builder.bible_builder_tool('s', 'p', 'ru')
    assert 'historical_period' in captured['system_prompt']
    assert 'environment_context' in captured['system_prompt']
    assert 'Не допускай анахронизмов' in captured['system_prompt']
    assert 'транспорт' not in captured['system_prompt']
    assert json.loads(captured['prompt'])['existing_canon']['characters'] == [{'name': 'Мальчик'}]


def test_visual_canon_analysis_excludes_nonvisual_narration(monkeypatch):
    from custom_tools.storybook.entity_generator_utils import analyzers
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return '{"new_characters": []}'
    monkeypatch.setattr(analyzers, 'call_openai_api', capture)
    result = analyzers.analyze_missing_characters({'screenplay': [{'voiceover': 'Закадровый текст'}]}, [])
    assert result == []
    assert 'ВИЗУАЛЬНЫХ' in captured['system_prompt']
    assert 'Закадровый рассказчик без визуального тела' in captured['system_prompt']
