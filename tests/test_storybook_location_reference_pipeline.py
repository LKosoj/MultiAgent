import json
import os
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import artist_batch_edit as artist


def test_artist_uses_location_reference_instead_of_old_background_binding(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    base.mkdir()
    location = base / 'location.png'
    location.write_bytes(b'canonical four-view location')
    background = base / 'old_background_sheet.png'
    background.write_bytes(b'old per-frame sheet')
    monkeypatch.setattr(artist, '_preprocess_canon_references', lambda **_: [])
    monkeypatch.setattr(artist, '_ensure_references_exist', lambda **_: None)
    monkeypatch.setattr(artist, '_collect_visible_text_bindings', lambda *_: [])
    monkeypatch.setattr(artist, 'log_smolagents_panel', lambda **_: None)
    monkeypatch.setattr('utils.needs_translation_to_english', lambda _: False)
    calls = []

    def edit(**kwargs):
        calls.append(kwargs)
        Path(kwargs['output_path']).write_bytes(b'scene image')
        return kwargs['output_path']

    monkeypatch.setattr(artist, 'edit_image_vse_tool', edit)
    result = json.loads(artist.artist_agent_batch_edit_tool(
        session_id='location-reference', max_concurrency=1,
        items={'items': [{'project_id': 'p', 'page_number': 1,
                          'english_prompt': 'A room with the screen showing the current scene state.',
                          'output_path': str(base / 'scene.png'),
                          'reference_image_paths': [str(location)],
                          'locations': [{'name': 'Room', 'reference_image_path': str(location)}],
                          'background_reference': {'sheet_path': str(background), 'card_number': 2}}]}))

    assert result[0]['ok']
    refs = calls[0]['image_paths']
    if isinstance(refs, str):
        refs = json.loads(refs)
    assert refs == [str(location)]


@pytest.mark.parametrize(('reference_folder', 'bad_attempts', 'attempts', 'ok'), [
    ('references', 1, 2, True),
    ('references', 3, 3, False),
    ('97_shots', 1, 1, True),
])
@pytest.mark.parametrize('local_edit', [False, True])
def test_artist_does_not_accept_returned_canonical_reference(
        tmp_path, monkeypatch, reference_folder, bad_attempts, attempts, ok, local_edit):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    reference = base / reference_folder / 'input.png'
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b'input reference image')
    output = base / 'output.png'
    original = b'current scene before edit'
    if local_edit:
        output.write_bytes(original)
    monkeypatch.setattr(artist, '_preprocess_canon_references', lambda **_: [])
    monkeypatch.setattr(artist, '_ensure_references_exist', lambda **_: None)
    monkeypatch.setattr(artist, '_collect_visible_text_bindings', lambda *_: [])
    monkeypatch.setattr(artist, 'log_smolagents_panel', lambda **_: None)
    monkeypatch.setattr('utils.needs_translation_to_english', lambda _: False)
    calls = []

    def edit(**kwargs):
        if local_edit:
            assert output.read_bytes() == original
        before_mtime = output.stat().st_mtime_ns if output.exists() else 0
        calls.append(kwargs)
        output.write_bytes(reference.read_bytes() if len(calls) <= bad_attempts else b'new scene image')
        os.utime(output, ns=(before_mtime + 1, before_mtime + 1))
        return str(output)

    monkeypatch.setattr(artist, 'edit_image_vse_tool', edit)
    result = json.loads(artist.artist_agent_batch_edit_tool(
        session_id='returned-reference', max_concurrency=1,
        items={'items': [{'project_id': 'p', 'page_number': 1,
                          'english_prompt': 'One cinematic frame of the room.',
                          **({'_continuity_edit_findings': [{'required': 'Correct one detail.'}]} if local_edit else {}),
                          'output_path': str(output), 'reference_image_paths': [str(reference)]}]}))

    assert len(calls) == attempts
    assert result[0]['ok'] is ok
    if reference_folder == 'references':
        if ok:
            assert output.read_bytes() == b'new scene image'
        elif local_edit:
            assert output.read_bytes() == original
        else:
            assert not output.exists()


@pytest.mark.parametrize('local_edit', [False, True])
def test_artist_accepts_gateway_checked_rounding_without_paid_retry(tmp_path, monkeypatch, local_edit):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'p'
    base.mkdir()
    reference = base / 'reference.png'
    Image.new('RGB', (32, 32), 'white').save(reference)
    output = base / 'output.png'
    if local_edit:
        Image.new('RGB', (1920, 1088), 'white').save(output)
    original = output.read_bytes() if local_edit else None
    monkeypatch.setattr(artist, '_preprocess_canon_references', lambda **_: [])
    monkeypatch.setattr(artist, '_ensure_references_exist', lambda **_: None)
    monkeypatch.setattr(artist, '_collect_visible_text_bindings', lambda *_: [])
    monkeypatch.setattr(artist, 'log_smolagents_panel', lambda **_: None)
    monkeypatch.setattr('utils.needs_translation_to_english', lambda _: False)
    calls = []

    def edit(**kwargs):
        if local_edit:
            assert output.read_bytes() == original
        before_mtime = output.stat().st_mtime_ns if output.exists() else 0
        calls.append(kwargs)
        Image.new('RGB', (1920, 1089), 'blue').save(output)
        os.utime(output, ns=(before_mtime + 1, before_mtime + 1))
        return str(output)

    monkeypatch.setattr(artist, 'edit_image_vse_tool', edit)
    result = json.loads(artist.artist_agent_batch_edit_tool(
        session_id='explicit-dimensions', max_concurrency=1,
        items={'items': [{'project_id': 'p', 'page_number': 1, 'width': 1920, 'height': 1088,
                          'english_prompt': 'One cinematic frame.', 'output_path': str(output),
                          **({'_continuity_edit_findings': [{'required': 'Correct one detail.'}]} if local_edit else {}),
                          'reference_image_paths': [str(reference)]}]}))
    assert len(calls) == 1
    assert result[0]['ok']
    assert (calls[0]['width'], calls[0]['height']) == (1920, 1088)
    with Image.open(output) as image:
        assert image.size == (1920, 1089)
