import json

import pytest

from custom_tools.storybook.items_builder import items_for_artist_tool


@pytest.mark.parametrize(('technical', 'expected'), [
    ({'size': '1280x720'}, (1280, 720)),
    ({'size': '1920x1088'}, (1920, 1088)),
    ({'size': '1280x720', 'width': 1536, 'height': 864}, (1536, 864)),
])
def test_items_use_declared_prompt_size(tmp_path, monkeypatch, technical, expected):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    prompt = tmp_path / 'p/40_prompts/page_01_prompt.json'
    prompt.parent.mkdir(parents=True)
    prompt.write_text(json.dumps({'technical': technical, 'references': {}}))
    beats = tmp_path / 'p/10_synopsis/beats.json'
    beats.parent.mkdir(parents=True)
    beats.write_text('[{}]')
    item = json.loads(items_for_artist_tool('s', 'p', 'ru'))['items'][0]
    assert (item['width'], item['height']) == expected


def test_series_frame_resolution_overrides_model_technical_size(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    (base / '40_prompts').mkdir(parents=True)
    (base / '10_synopsis').mkdir()
    (base / '00_brief.json').write_text(json.dumps({'frame_resolution': '1080x1920'}))
    (base / '40_prompts/page_01_prompt.json').write_text(json.dumps({
        'technical': {'size': '1920x1088', 'width': 1920, 'height': 1088}, 'references': {},
    }))
    (base / '10_synopsis/beats.json').write_text('[{}]')
    item = json.loads(items_for_artist_tool('s', 'p', 'ru'))['items'][0]
    assert (item['width'], item['height']) == (1080, 1920)
