import json
from pathlib import Path

import pytest

from custom_tools.storybook.studio import speech
from custom_tools.storybook.studio.errors import StudioError


def test_music_controls_only_actual_audio_files(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, 'series_dir', lambda p, s: tmp_path)
    audio = tmp_path / '98_audio'; (audio / 'music').mkdir(parents=True)
    (audio / 'music/theme.wav').write_bytes(b'audio')
    outside = tmp_path / 'outside.wav'; outside.write_bytes(b'audio')
    manifest = audio / 'music_manifest.json'
    manifest.write_text(json.dumps({'language': 'ru', 'status': 'skipped', 'music_path': str(audio / 'music/theme.wav')}))
    assert speech.sound_events_get('p', 's')['events'] == []
    with pytest.raises(StudioError): speech.sound_event_update('p', 's', 'music:ru', 0, .5, True)
    manifest.write_text(json.dumps({'theme': 'music/theme.wav', 'missing': 'music/missing.mp3', 'outside': '../outside.wav'}))
    events = speech.sound_events_get('p', 's')['events']
    assert [item['id'] for item in events] == ['music:music/theme.wav']
    with pytest.raises(StudioError): speech.sound_event_update('p', 's', 'music:../outside.wav', 0, .5, True)
    speech.sound_event_update('p', 's', 'music:music/theme.wav', 0, .3, False)
    control = json.loads((audio / 'audio_manifest.json').read_text())['audio_tracks'][0]
    assert control['level'] == .3 and control['enabled'] is False
    manifest.write_text(json.dumps({'status': 'success', 'music_path': str(audio / 'music/theme.wav'), 'language': 'ru'}))
    assert [item['id'] for item in speech.sound_events_get('p', 's')['events']] == ['music:music/theme.wav']
    manifest.write_text(json.dumps({'theme': {'path': 'music/theme.wav'}}))
    assert [item['id'] for item in speech.sound_events_get('p', 's')['events']] == ['music:music/theme.wav']


def test_preview_does_not_clear_newer_sound_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, 'series_dir', lambda p, s: tmp_path)
    audio = tmp_path / '98_audio'; audio.mkdir()
    (audio / 'effect.wav').write_bytes(b'effect')
    (audio / 'speech.json').write_text(json.dumps({'status': 'silent', 'lines': []}))
    (audio / 'audio_manifest.json').write_text(json.dumps({'audio_tracks': [{'id': 'rain', 'role': 'sfx', 'path': 'effect.wav', 'offset_seconds': 3, 'duration_seconds': 1, 'level': .4, 'enabled': False}]}))
    series = tmp_path / 'series.json'; series.write_text(json.dumps({'sound_revision': 1, 'stale_results': ['98_audio/previews', '99_final']}))
    def render(command, **kwargs):
        data = json.loads(series.read_text()); data['sound_revision'] = 2
        series.write_text(json.dumps(data)); Path(command[-1]).write_bytes(b'preview')
        filters = command[command.index('-filter_complex') + 1]
        assert 'volume=0.4' in filters and 'adelay=2000:all=1' in filters
    monkeypatch.setattr(speech.subprocess, 'run', render)
    speech.sound_preview('p', 's', 'rain')
    assert json.loads(series.read_text())['stale_results'] == ['98_audio/previews', '99_final']
