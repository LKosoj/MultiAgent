import json

from custom_tools.storybook.audio_subtitle import storybook_audio_subtitle_tool
from custom_tools.storybook.studio import series


def _setup(tmp_path, monkeypatch, speech):
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    root = tmp_path / '98_audio'
    root.mkdir(exist_ok=True)
    (root / 'speech.json').write_text(json.dumps(speech, ensure_ascii=False))
    return root


def test_measured_subtitles_and_speech_track(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, {'status': 'success', 'duration': 4, 'audio_path': 'speech/a.wav', 'lines': [
        {'id': 'a', 'scene_number': 1, 'shot_number': 2, 'text': 'Привет!', 'start': 1.25, 'end': 2.5}]})
    (root / 'audio_manifest.json').write_text(json.dumps({'audio_tracks': [{'role':'music','path':'music.mp3'}]}))
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e')
    assert result['status'] == 'success'
    assert '00:00:01,250 --> 00:00:02,500\nПривет!' in (root / 'subtitles.srt').read_text()
    cues = json.loads((root / 'cue_sheet.json').read_text())
    assert cues['timeline'] == 'measured' and cues['cues'][0]['shot_number'] == 2
    tracks = json.loads((root / 'audio_manifest.json').read_text())['audio_tracks']
    assert tracks[0]['path'] == 'speech/a.wav' and tracks[1]['role'] == 'music'


def test_silent_series_is_valid_without_calls(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, {'status':'silent', 'duration':0, 'audio_path':None, 'lines':[]})
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e')
    assert result['status'] == 'success' and result['cue_count'] == 0
    assert json.loads((root / 'audio_manifest.json').read_text())['audio_tracks'] == []


def test_subtitles_can_be_disabled_without_removing_audio(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, {'status':'success', 'duration':2, 'audio_path':'a.mp3', 'lines':[]})
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e', generate_subtitles=False)
    assert result['status'] == 'success' and not (root / 'subtitles.srt').exists()
    assert json.loads((root / 'audio_manifest.json').read_text())['audio_tracks'][0]['path'] == 'a.mp3'


def test_missing_measured_speech_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e')
    assert result['status'] == 'error' and result['audio_manifest_path']


def test_invalid_series_cannot_escape_scope(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    result = storybook_audio_subtitle_tool('s', '../outside', series_id='e')
    assert result['status'] == 'error'


def test_disabled_step_does_not_require_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e', enable=False)
    assert result['status'] == 'skipped' and not (tmp_path / '98_audio').exists()


def test_stale_speech_error_includes_all_required_output_paths(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch, {'status': 'stale'})
    result = storybook_audio_subtitle_tool('s', 'p', series_id='e')
    assert result['status'] == 'error'
    assert result['subtitles_path'] == str(root / 'subtitles.srt')
    assert result['audio_manifest_path'] == str(root / 'audio_manifest.json')
    assert result['cue_sheet_path'] == str(root / 'cue_sheet.json')
