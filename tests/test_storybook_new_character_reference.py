import json

from custom_tools.storybook import artist_batch_edit as artist


def test_new_character_is_not_created_by_editing_another_character(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p' / '20_bible'
    (base / 'references/characters').mkdir(parents=True)
    (base / 'references/characters/first.png').write_bytes(b'existing portrait')
    characters = [
        {'name': 'First', 'reference_image_path': '/references/characters/first.png'},
        {'name': 'Second', 'age': '20', 'reference_image_path': '/references/characters/second.png'},
    ]
    (base / 'characters.json').write_text(json.dumps(characters))
    calls = []
    monkeypatch.setattr(artist, '_smart_select_references_for_generation', lambda *a, **k: a[2])
    monkeypatch.setattr(artist, '_create_canon_reference', lambda **k: calls.append(('edit', k)) or True)
    monkeypatch.setattr(artist, '_generate_canon_reference_from_scratch', lambda **k: calls.append(('generate', k)) or True)
    target = str(base / 'references/characters/second.png')
    artist._ensure_references_exist('s', 'p', [{'reference_image_paths': [target]}], [], 'workflow')
    assert [kind for kind, _ in calls] == ['generate']
    assert calls[0][1]['entity_data']['age'] == '20'


def test_reuse_episode_image_requires_matching_existing_inputs(tmp_path, monkeypatch):
    from custom_tools.storybook import project_paths, continuity
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(project_paths, '_series_scope', project_paths.ContextVar('reuse_series', default=('p', 'episode')))
    relative = '97_shots/scene_01_shot_01/start.png'
    source = tmp_path / 'p' / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b'generated image')
    source.with_suffix('.continuity.json').write_text('{"input_hash":"same"}')
    target = tmp_path / 'p/series/episode' / relative
    item = {'project_id': 'p', 'output_path': str(target)}
    monkeypatch.setattr(continuity, 'image_continuity_hash', lambda _: 'changed')
    assert not artist._reuse_legacy_series_shot_image(item)
    assert not target.exists()
    monkeypatch.setattr(continuity, 'image_continuity_hash', lambda _: 'same')
    assert artist._reuse_legacy_series_shot_image(item)
    assert target.read_bytes() == b'generated image'
    assert continuity.current_continuity_image(item)
    target.write_bytes(b'newer episode image')
    assert not artist._reuse_legacy_series_shot_image(item)
    assert target.read_bytes() == b'newer episode image'


def test_artist_reuses_legacy_cached_step_output_in_selected_episode(tmp_path, monkeypatch):
    from custom_tools.storybook import project_paths, continuity
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path / 'plots/storybooks'))
    monkeypatch.setattr(project_paths, '_series_scope', project_paths.ContextVar('entry_series', default=('p', 'episode')))
    legacy = 'plots/storybooks/p/97_shots/scene_01_shot_01/start.png'
    from pathlib import Path
    source = Path(legacy)
    source.parent.mkdir(parents=True)
    source.write_bytes(b'accepted image')
    source.with_suffix('.continuity.json').write_text('{"input_hash":"same"}')
    monkeypatch.setattr(continuity, 'image_continuity_hash', lambda _: 'same')
    monkeypatch.setattr(artist, '_preprocess_canon_references', lambda **_: [])
    monkeypatch.setattr(artist, '_ensure_references_exist', lambda **_: None)
    calls = []
    def edit_missing(**kwargs):
        calls.append(kwargs)
        Path(kwargs['output_path']).write_bytes(b'new end image')
        return kwargs['output_path']
    monkeypatch.setattr(artist, 'edit_image_vse_tool', edit_missing)
    monkeypatch.setattr(artist, '_collect_visible_text_bindings', lambda *_: [])
    monkeypatch.setattr(artist, 'log_smolagents_panel', lambda **_: None)
    result = json.loads(artist.artist_agent_batch_edit_tool(
        session_id='resume', max_concurrency=1,
        items={'items': [{'project_id':'p', 'scene_number':1, 'shot_number':1,
                          'shot_type':'start', 'output_path':legacy, 'english_prompt':'A room',
                          'video_prompt':'Static camera'},
                         {'project_id':'p', 'scene_number':1, 'shot_number':1,
                          'shot_type':'end', 'output_path':legacy.replace('start.png','end.png'),
                          'english_prompt':'Edit image 1: A room', 'reference_image_paths':[legacy],
                          'video_prompt':'Static camera'}]}))
    target = tmp_path / 'plots/storybooks/p/series/episode/97_shots/scene_01_shot_01/start.png'
    assert target.is_file()
    assert target.read_bytes() == b'accepted image'
    assert result[0]['output_path'] == str(target)
    assert len(calls) == 1
    refs = calls[0]['image_paths']
    if isinstance(refs, str):
        refs = json.loads(refs)
    assert refs == [str(target)]
    assert Path(calls[0]['output_path']) == target.with_name('end.png')
    assert all(row['ok'] for row in result)
