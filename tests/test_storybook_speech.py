import json

import pytest

from custom_tools.storybook.speech_alignment import measure_line_timing, merge_chunks, chunk_ranges
from custom_tools.storybook import speech


def test_alignment_numbers_abbreviations_and_repeated_words():
    lines = [{'id': 'a', 'text': 'AI 2024'}, {'id': 'b', 'text': 'да'}, {'id': 'c', 'text': 'да'}]
    texts = ['эй', 'ай', 'две', 'тысячи', 'двадцать', 'четыре', 'да', 'да']
    words = [{'text': text, 'start': float(i), 'end': i + .5} for i, text in enumerate(texts)]
    timing = measure_line_timing(lines, words, 8, 'ru')
    assert [(x['word_start'], x['word_end']) for x in timing] == [(0, 6), (6, 7), (7, 8)]


def test_missing_line_reports_expected_and_heard():
    with pytest.raises(ValueError, match='Реплика 2.*ожидалось.*услышано'):
        measure_line_timing([{'text': 'привет'}, {'text': 'совершенно другое'}],
                            [{'text': 'привет', 'start': 0., 'end': .5}, {'text': 'привет', 'start': 1., 'end': 1.5}], 2., 'ru')


def test_laughter_and_sneeze_variants_keep_the_whole_utterance():
    lines = [{'id': 'a', 'text': 'Ты просто не шаришь.'}, {'id': 'b', 'text': 'Хы-хы... хы... апчхи!'}]
    texts = ['Ты', 'просто', 'не', 'шаришь', 'Хе', '-хе', '-хе', '-хе', '-а', '-а', '-а', '-чхи']
    words = [{'text': text, 'start': i * .5, 'end': (i + 1) * .5} for i, text in enumerate(texts)]
    result = measure_line_timing(lines, words, 6, 'ru')
    assert [(line['word_start'], line['word_end']) for line in result] == [(0, 4), (4, 12)]
    assert result[-1]['end'] == 6


def test_chunk_seam_and_repeated_words():
    ranges = chunk_ranges(350)
    assert all(end - start <= 180 for start, end in ranges)
    assert all(a[1] - b[0] == 5 for a, b in zip(ranges, ranges[1:]))
    words = merge_chunks([(0, 180, [{'text': 'да', 'start': 177, 'end': 177.3}, {'text': 'да', 'start': 179, 'end': 179.3}]),
                          (175, 350, [{'text': 'да', 'start': 177.05, 'end': 177.35}, {'text': 'да', 'start': 179.05, 'end': 179.35}])])
    assert len(words) == 2


def test_retry_keeps_paid_speech(tmp_path, monkeypatch):
    lines = [{'id': 'a', 'scene_number': 1, 'speaker_id': 'a', 'kind': 'on_screen', 'text': 'Привет', 'voice': 'v1'}]
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append(lines) or b'audio')
    monkeypatch.setattr(speech, '_duration', lambda path: 1.0)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: (_ for _ in ()).throw(ValueError('ASR failed')))
    with pytest.raises(ValueError, match='ASR failed'):
        speech.prepare_speech(tmp_path, 'p', 's', lines, 'ru')
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text': 'Привет', 'start': .1, 'end': .7}])
    result = speech.prepare_speech(tmp_path, 'p', 's', lines, 'ru')
    assert len(calls) == 1
    assert result['lines'][0]['start'] == .1
    assert json.loads((tmp_path / '98_audio/speech.json').read_text())['series_id'] == 's'


def test_snapshot_voices_and_narrator():
    snapshot = {'characters': {'hero': {'version_id': 'v1', 'data': {'name': 'Лиса', 'voice': {'voice_id': 'fox'}}}},
                'narrators': {'reader': {'version_id': 'v2', 'data': {'voice': 'reader-voice'}}}}
    script = {'screenplay': [{'scene_number': 1, 'voiceover': 'Утро.', 'voiceover_order': 1, 'dialogue': [{'character': 'Лиса', 'order': 2, 'line': 'Привет!', 'direction': 'тихо', 'shot_number': 2}]}]}
    lines = speech.screenplay_lines(script, snapshot)
    assert [(line['speaker_id'], line['voice'], line['voice_version']) for line in lines] == [('reader', 'reader-voice', 'v2'), ('hero', 'fox', 'v1')]
    assert lines[1]['shot_number'] == 2


def test_subtitles_use_measured_speech(tmp_path, monkeypatch):
    from custom_tools.storybook import audio_subtitle
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda project_id, series_id: tmp_path)
    directory = tmp_path / '98_audio'
    directory.mkdir()
    (directory / 'speech.json').write_text(json.dumps({'status': 'success', 'audio_path': 'speech/a.mp3', 'duration': 3, 'lines': [{'id': 'x', 'scene_number': 1, 'shot_number': 1, 'text': 'Привет', 'start': 1.2, 'end': 2.3}]}))
    result = audio_subtitle.storybook_audio_subtitle_tool('s', 'p', series_id='e')
    assert result['status'] == 'success'
    assert '00:00:01,200 --> 00:00:02,300' in (directory / 'subtitles.srt').read_text()


def test_pause_is_inserted_into_source_audio(tmp_path):
    import subprocess
    original = tmp_path / 'original.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', str(original)], check=True)
    words = [{'text': 'слово', 'start': .2, 'end': .6}]
    lines = [{'id': 'x', 'text': 'слово', 'start': .2, 'end': .6, 'word_start': 0, 'word_end': 1, 'pause_before': .5}]
    output, duration = speech._insert_pauses(original, tmp_path / 'out.wav', 1., lines, words)
    assert abs(speech._duration(output) - 1.5) < .05
    assert duration == 1.5 and lines[0]['start'] == .7


def test_sfx_event_time_and_level_reuse_generated_file(tmp_path, monkeypatch):
    from custom_tools.storybook import sfx_generator as sfx
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda project_id, series_id: tmp_path)
    monkeypatch.setattr(sfx, '_load_env_file', lambda: None)
    calls = []
    def generate(payload, path):
        calls.append(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
    monkeypatch.setattr(sfx, '_generate_mp3', generate)
    script = tmp_path / '91_screenplay/screenplay.json'
    script.parent.mkdir()
    (tmp_path / '98_audio').mkdir()
    (tmp_path / '98_audio/speech.json').write_text(json.dumps({'status':'silent', 'lines':[], 'words':[]}))
    for start, level in [(2, .3), (3, .5)]:
        script.write_text(json.dumps({'screenplay': [{'scene_number': 1, 'sfx': [{'id': 'door', 'prompt': 'door', 'start': start, 'duration': 1, 'level': level}]}]}))
        result = sfx.storybook_sfx_generator_tool('s', 'p', series_id='e')
        assert result['status'] == 'success'
    tracks = json.loads((tmp_path / '98_audio/audio_manifest.json').read_text())['audio_tracks']
    assert len(calls) == 1 and tracks[0]['offset_seconds'] == 3 and tracks[0]['level'] == .5


def test_sound_edit_survives_regeneration(tmp_path, monkeypatch):
    from custom_tools.storybook import sfx_generator as sfx
    from custom_tools.storybook.studio import series, speech as studio_speech
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(studio_speech, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(sfx, '_load_env_file', lambda: None)
    def generate(payload, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
    monkeypatch.setattr(sfx, '_generate_mp3', generate)
    path = tmp_path / '91_screenplay/screenplay.json'
    path.parent.mkdir()
    (tmp_path / '98_audio').mkdir()
    (tmp_path / '98_audio/speech.json').write_text(json.dumps({'status':'silent', 'lines':[], 'words':[]}))
    path.write_text(json.dumps({'screenplay': [{'scene_number': 1, 'sfx': [{'id': 'x', 'prompt': 'wind', 'start': 1, 'duration': 2}]}]}))
    sfx.storybook_sfx_generator_tool('s', 'p', series_id='e')
    studio_speech.sound_event_update('p', 'e', 'x', 4., .3, True)
    sfx.storybook_sfx_generator_tool('s', 'p', series_id='e')
    track = json.loads((tmp_path / '98_audio/audio_manifest.json').read_text())['audio_tracks'][0]
    assert track['offset_seconds'] == 4 and track['level'] == .3


def test_script_uses_explicit_order_ids_and_pauses():
    snapshot = {'characters': {'fox': {'version_id': 'v1', 'data': {'name': 'Лиса', 'voice': 'v'}}},
                'narrators': {'reader': {'version_id': 'v2', 'data': {'voice': 'n'}}}}
    data = {'screenplay': [{'scene_number': 1, 'storyboard': [{'shot_number': 1, 'voiceover': 'Утро.', 'voiceover_id': 'n1', 'voiceover_order': 2, 'pause_before': 1}],
                            'dialogue': [{'id': 'd1', 'order': 1, 'character': 'Лиса', 'line': 'Да!', 'shot_number': 1, 'pause_after': 2}]}]}
    lines = speech.screenplay_lines(data, snapshot)
    assert [line['id'] for line in lines] == ['d1', 'n1']
    assert lines[0]['pause_after'] == 2 and lines[1]['pause_before'] == 1
    data['screenplay'][0]['dialogue'][0]['line'] = 'Нет!'
    assert [line['id'] for line in speech.screenplay_lines(data, snapshot)] == ['d1', 'n1']


def test_mixed_speech_without_order_is_rejected():
    data = {'screenplay': [{'scene_number': 1, 'voiceover': 'Утро', 'dialogue': [{'character': 'Лиса', 'line': 'Да'}]}]}
    with pytest.raises(ValueError, match='порядок'):
        speech.screenplay_lines(data, {})


def test_duplicate_observation_at_overlap_seam_but_not_repeated_word():
    chunks = [(0, 180, [{'text':'да', 'start':177.2, 'end':177.7}]),
              (175, 355, [{'text':'да', 'start':177.4, 'end':177.9}, {'text':'да', 'start':178.2, 'end':178.4}])]
    assert len(merge_chunks(chunks)) == 2


def test_silent_scenes_become_explicit_pauses():
    snapshot = {'characters': {'x': {'version_id': 'v', 'data': {'name': 'Лиса', 'voice': 'v'}}}}
    data = {'screenplay': [{'scene_number': 1, 'silence_duration': 3},
                           {'scene_number': 2, 'dialogue': [{'id': 'x', 'character': 'Лиса', 'line': 'Да', 'pause_before': 1}]},
                           {'scene_number': 3, 'silence_duration': 2}]}
    lines = speech.screenplay_lines(data, snapshot)
    assert lines[0]['pause_before'] == 4 and lines[0]['pause_after'] == 2


def test_silent_scene_pauses_use_final_storyboard_duration():
    snapshot = {'characters': {'x': {'version_id': 'v', 'data': {'name': 'Лиса', 'voice': 'v'}}}}
    data = {'screenplay': [
        {'scene_number': 1, 'silence_duration': 2, 'storyboard': [
            {'shot_number': 1, 'timing': '00:03'}, {'shot_number': 2, 'timing': '00:07'}]},
        {'scene_number': 2, 'dialogue': [{'id': 'a', 'character': 'Лиса', 'line': 'Привет'}]},
        {'scene_number': 3, 'silence_duration': 1, 'storyboard': [{'shot_number': 1, 'timing': '00:05'}]},
    ]}
    lines = speech.screenplay_lines(data, snapshot)
    assert lines[0]['pause_before'] == 10
    assert lines[0]['pause_after'] == 5


def test_studio_edit_line_preserves_other_lines(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import speech as studio_speech
    monkeypatch.setattr(studio_speech, 'series_dir', lambda p, s: tmp_path)
    path = tmp_path / '91_screenplay/screenplay.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'screenplay': [{'scene_number': 1, 'dialogue': [
        {'id': 'a', 'character': 'Лиса', 'line': 'Да', 'direction': ''},
        {'id': 'b', 'character': 'Волк', 'line': 'Нет', 'direction': ''}]}]}))
    studio_speech.speech_line_update('p', 's', 'a', 'Конечно', 'тихо', .5, 1.)
    items = json.loads(path.read_text())['screenplay'][0]['dialogue']
    assert items[0]['line'] == 'Конечно' and items[0]['direction'] == 'тихо'
    assert items[0]['pause_before'] == .5 and items[1]['line'] == 'Нет'


def test_preview_mixes_existing_speech_and_effect(tmp_path, monkeypatch):
    import subprocess
    from custom_tools.storybook.studio import speech as studio_speech
    monkeypatch.setattr(studio_speech, 'series_dir', lambda p, s: tmp_path)
    directory = tmp_path / '98_audio'
    directory.mkdir()
    for filename, frequency in [('speech.wav', 440), ('effect.wav', 880)]:
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration=2', str(directory / filename)], check=True)
    (directory / 'speech.json').write_text(json.dumps({'status': 'success', 'audio_path':'speech.wav', 'duration':2, 'lines':[], 'words':[]}))
    (directory / 'audio_manifest.json').write_text(json.dumps({'audio_tracks':[{'role':'sfx', 'id':'x', 'path':'effect.wav', 'offset_seconds':.5, 'duration_seconds':1, 'level':.3, 'enabled':True}]}))
    result = studio_speech.sound_preview('p', 's', 'x')
    assert (directory / result['preview_path']).is_file()


def test_script_compaction_preserves_speech_and_shots():
    from custom_tools.storybook.screenplay_generator import _compact_screenplay_for_alignment
    text = 'a' * 500
    scene = {'scene_number': 1, 'voiceover_id': 'n', 'dialogue': [{'id':'a', 'order': 20, 'line':text, 'character':'x'}],
             'storyboard': [{'shot_number': i, 'voiceover_id':f'n{i}', 'face_visibility_relative': []} for i in range(1, 11)]}
    result = _compact_screenplay_for_alignment({'screenplay': [scene]})['screenplay'][0]
    assert result['dialogue'][0]['line'] == text and result['dialogue'][0]['id'] == 'a'
    assert len(result['storyboard']) == 10 and result['storyboard'][0]['face_visibility_relative'] == []


def test_self_check_keeps_omitted_speech_metadata_and_shots():
    from custom_tools.storybook.screenplay_generator import _merge_checked_scenes
    source = [{'scene_number':1, 'dialogue':[{'id':'x', 'line':'старый', 'order':10, 'pause_after':2}],
               'storyboard':[{'shot_number':1, 'face_visibility_relative':[]}, {'shot_number':2, 'voiceover_id':'n'}]}]
    checked = [{'scene_number':1, 'dialogue':[{'line':'исправлен'}], 'storyboard':[{'shot_number':1, 'description':'исправлен'}]}]
    result = _merge_checked_scenes(source, checked)[0]
    assert result['dialogue'][0] == {'id':'x', 'line':'исправлен', 'order':10, 'pause_after':2}
    assert result['storyboard'][0]['face_visibility_relative'] == [] and len(result['storyboard']) == 2


def test_assigned_speech_ids_survive_insertion():
    from custom_tools.storybook.screenplay_generator import _ensure_speech_ids
    script = {'screenplay':[{'scene_number':1, 'dialogue':[{'line':'да'}]}]}
    _ensure_speech_ids(script)
    old = script['screenplay'][0]['dialogue'][0]['id']
    script['screenplay'][0]['dialogue'].insert(0, {'line':'нет'})
    _ensure_speech_ids(script)
    assert script['screenplay'][0]['dialogue'][1]['id'] == old


def test_sfx_event_anchor_uses_measured_words(tmp_path, monkeypatch):
    from custom_tools.storybook import sfx_generator as sfx
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(sfx, '_load_env_file', lambda: None)
    def generate(payload, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'audio')
    monkeypatch.setattr(sfx, '_generate_mp3', generate)
    script = tmp_path / '91_screenplay/screenplay.json'
    script.parent.mkdir()
    script.write_text(json.dumps({'screenplay':[{'scene_number':1, 'sfx':[{'id':'hit', 'prompt':'удар', 'line_id':'l', 'word_index':1, 'duration':.5}]}]}))
    audio = tmp_path / '98_audio'
    audio.mkdir()
    (audio / 'speech.json').write_text(json.dumps({'status':'success', 'duration':4, 'lines':[{'id':'l', 'scene_number':1,'start':1,'end':3, 'word_start':0,'word_end':2}], 'words':[{'start':1,'end':1.2}, {'start':2,'end':2.3}]}))
    result = sfx.storybook_sfx_generator_tool('s', 'p', series_id='e')
    assert result['status'] == 'success'
    track = json.loads((audio / 'audio_manifest.json').read_text())['audio_tracks'][0]
    assert track['offset_seconds'] == 2 and track['line_id'] == 'l' and track['word_index'] == 1


def test_alignment_failure_retranscribes_without_synthesizing(tmp_path, monkeypatch):
    lines = [{'id':'x','text':'привет','speaker_id':'a','kind':'on_screen','voice':'v'}]
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: calls.append('tts') or b'audio')
    monkeypatch.setattr(speech, '_duration', lambda _: 2.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text':'другое','start':0.,'end':1.}])
    with pytest.raises(ValueError):
        speech.prepare_speech(tmp_path, 'p','s', lines, 'ru')
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text':'привет','start':0.,'end':1.}])
    assert speech.prepare_speech(tmp_path, 'p','s', lines, 'ru')['status'] == 'success'
    assert calls == ['tts']


def test_synthesis_sends_unchanged_text_with_voice_and_delivery(monkeypatch):
    from types import SimpleNamespace
    sent = []
    monkeypatch.setattr(speech, '_post', lambda endpoint, **kwargs: sent.append(kwargs['json']) or SimpleNamespace(content=b'voice', headers={'Content-Type':'audio/mpeg'}))
    speech._synthesize([{'text':'Привет, мир!', 'voice':'v1', 'delivery':'весело'}, {'text':'Да.', 'voice':'v2', 'delivery':''}])
    assert sent[0]['input'] == [{'text':'[весело] Привет, мир!', 'voice':'v1'}, {'text':'Да.', 'voice':'v2'}]


def test_silent_shots_produce_real_silence_around_speech():
    snapshot = {'characters': {'x': {'version_id': 'v', 'data': {'name': 'Лиса', 'voice': 'v'}}}}
    script = {'screenplay':[{'scene_number':1, 'storyboard':[{'shot_number':1,'timing':'2s'}, {'shot_number':2,'timing':'3s', 'dialogue_ids':['l']}, {'shot_number':3,'timing':'4s'}],
                            'dialogue':[{'id':'l','character':'Лиса','line':'Да'}]}]}
    line = speech.screenplay_lines(script, snapshot)[0]
    assert line['shot_number'] == 2 and line['pause_before'] == 2 and line['pause_after'] == 4


def test_late_speech_result_does_not_overwrite_changed_script(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(series, 'resolve_snapshot', lambda p, s: {'characters':{'fox':{'version_id':'v', 'data':{'name':'Лиса','voice':'v'}}}})
    path = tmp_path / '91_screenplay/screenplay.json'
    path.parent.mkdir()
    def write_line(value):
        path.write_text(json.dumps({'screenplay':[{'scene_number':1, 'dialogue':[{'id':'l','character':'Лиса','line':value}]}]}))
    write_line('Привет')
    monkeypatch.setattr(speech, '_synthesize', lambda lines: b'paid audio')
    monkeypatch.setattr(speech, '_duration', lambda path: 2.)
    def recognize(*args):
        write_line('Изменено')
        return [{'text':'Привет','start':.1,'end':.5}]
    monkeypatch.setattr(speech, '_transcribe', recognize)
    result = speech.storybook_speech_tool('run', 'p', 's')
    assert result['status'] == 'error'
    assert not (tmp_path / '98_audio/speech.json').is_file()
    assert list((tmp_path / '98_audio/speech').glob('*.mp3'))


def test_changed_input_marks_old_speech_stale_before_failed_tts(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(series, 'resolve_snapshot', lambda p, s: {'characters':{'fox':{'version_id':'v', 'data':{'name':'Лиса','voice':'v'}}}})
    directory = tmp_path / '98_audio'
    directory.mkdir()
    (directory / 'speech.json').write_text(json.dumps({'status':'success','input_hash':'old','audio_path':'speech/old.mp3'}))
    script = tmp_path / '91_screenplay/screenplay.json'
    script.parent.mkdir()
    script.write_text(json.dumps({'screenplay':[{'scene_number':1, 'dialogue':[{'id':'l', 'character':'Лиса','line':'Новый текст'}]}]}))
    monkeypatch.setattr(speech, '_synthesize', lambda _: (_ for _ in ()).throw(ValueError('ошибка')))
    assert speech.storybook_speech_tool('s','p','e')['status'] == 'error'
    assert json.loads((directory / 'speech.json').read_text())['status'] == 'stale'


def test_long_audio_requests_overlapping_chunks_with_language(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    source = tmp_path / 'long.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '185', str(source)], check=True)
    calls = []
    def post(endpoint, **kwargs):
        calls.append(kwargs['data'])
        words = ([{'word':'да', 'start':177.2, 'end':177.7}] if len(calls) == 1 else
                 [{'word':'да', 'start':2.4, 'end':2.9}, {'word':'да', 'start':3.2, 'end':3.4}])
        return SimpleNamespace(json=lambda: {'words':words})
    monkeypatch.setattr(speech, '_post', post)
    words = speech._transcribe(source, 185, 'ru')
    assert len(calls) == 2 and all(call['language'] == 'ru' for call in calls)
    assert len(words) == 2 and words[0]['start'] >= 177 and words[1]['start'] >= 178


def test_full_measured_audio_and_subtitle_with_silent_shots(tmp_path, monkeypatch):
    import subprocess
    from custom_tools.storybook import audio_subtitle
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    monkeypatch.setattr(series, 'resolve_snapshot', lambda p, s: {'characters':{'fox':{'version_id':'v', 'data':{'name':'Лиса','voice':'v'}}}})
    source = tmp_path / 'tone.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', str(source)], check=True)
    monkeypatch.setattr(speech, '_synthesize', lambda lines: source.read_bytes())
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text':'Да', 'start':.2, 'end':.6}])
    script = tmp_path / '91_screenplay/screenplay.json'
    script.parent.mkdir()
    script.write_text(json.dumps({'screenplay':[{'scene_number':1, 'storyboard':[{'shot_number':1,'timing':'2s'}, {'shot_number':2,'timing':'3s','dialogue_ids':['l']}, {'shot_number':3,'timing':'4s'}], 'dialogue':[{'id':'l','character':'Лиса','line':'Да'}]}]}))
    assert speech.storybook_speech_tool('r','p','s')['status'] == 'success'
    result = json.loads((tmp_path / '98_audio/speech.json').read_text())
    audio_path = tmp_path / '98_audio' / result['audio_path']
    assert abs(speech._duration(audio_path) - result['duration']) < .05
    assert abs(result['lines'][0]['start'] - 2.2) < .01 and result['duration'] > 6.9
    assert audio_subtitle.storybook_audio_subtitle_tool('r','p',series_id='s')['status'] == 'success'
    assert '00:00:02,200 --> 00:00:02,600' in (tmp_path / '98_audio/subtitles.srt').read_text()


def test_music_level_control_uses_existing_audio_without_generation(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import speech as studio_speech
    monkeypatch.setattr(studio_speech, 'series_dir', lambda p, s: tmp_path)
    directory = tmp_path / '98_audio'
    directory.mkdir()
    (directory / 'music').mkdir()
    (directory / 'music/theme.mp3').write_bytes(b'audio')
    (directory / 'music_manifest.json').write_text(json.dumps({'theme':'music/theme.mp3'}))
    events = studio_speech.sound_events_get('p','s')['events']
    assert events[0]['id'] == 'music:music/theme.mp3' and events[0]['editable_start'] is False
    studio_speech.sound_event_update('p','s','music:music/theme.mp3',0,.2,False)
    track = json.loads((directory / 'audio_manifest.json').read_text())['audio_tracks'][0]
    assert track['role'] == 'music' and track['path'] == 'music/theme.mp3' and track['level'] == .2 and track['enabled'] is False
    with pytest.raises(Exception, match='сцен'):
        studio_speech.sound_event_update('p','s','music:music/theme.mp3',1,.2,False)


def test_music_level_survives_manifest_refresh(tmp_path):
    from custom_tools.storybook.music_generator import _merge_music_status
    path = tmp_path / 'audio_manifest.json'
    path.write_text(json.dumps({'audio_tracks':[{'role':'music','provider':'suno','path':'music/theme.mp3','level':.2,'enabled':False}]}))
    _merge_music_status(path, {'provider':'suno','status':'success'}, {'role':'music','provider':'suno','path':'music/theme.mp3'})
    track = json.loads(path.read_text())['audio_tracks'][0]
    assert track['level'] == .2 and track['enabled'] is False


def test_visual_resource_revision_reuses_voice_audio_and_updates_metadata(tmp_path, monkeypatch):
    line = {'id': 'l', 'text': 'привет', 'speaker_id': 'fox', 'kind': 'on_screen', 'voice': 'same', 'voice_version': 'image-v1'}
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append(lines) or b'paid-audio')
    monkeypatch.setattr(speech, '_duration', lambda path: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text': 'привет', 'start': .1, 'end': .7}])
    first = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    second = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'voice_version': 'image-v2'}], 'ru')
    assert len(calls) == 1
    assert first['input_hash'] != second['input_hash']
    assert second['lines'][0]['voice_version'] == 'image-v2'


def test_ambiguous_boundary_follows_longer_pause():
    lines = [{'text': 'one two'}, {'text': 'three four'}]
    cases = [
        ([(0, .2), (.3, .5), (.6, .8), (2, 2.2), (2.3, 2.5)], [(0, 3), (3, 5)]),
        ([(0, .2), (.3, .5), (2, 2.2), (2.3, 2.5), (2.6, 2.8)], [(0, 2), (2, 5)]),
    ]
    for times, expected in cases:
        words = [{'text': text, 'start': start, 'end': end}
                 for text, (start, end) in zip(['one', 'two', 'hmm', 'three', 'four'], times)]
        measured = measure_line_timing(lines, words, 3., 'en')
        assert [(line['word_start'], line['word_end']) for line in measured] == expected


def test_existing_audio_cache_is_reused_after_voice_cache_key_change(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'привет', 'speaker_id':'fox', 'voice':'v', 'voice_version':'old'}
    key = speech._fingerprint([line], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{key}.mp3').write_bytes(b'paid')
    (directory / f'{key}.words.json').write_text(json.dumps([{'text':'привет', 'start':.1, 'end':.7}]))
    monkeypatch.setattr(speech, '_synthesize', lambda *_: pytest.fail('paid audio already exists'))
    monkeypatch.setattr(speech, '_transcribe', lambda *_: pytest.fail('words already exist'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    assert speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')['status'] == 'success'
    changed = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'voice_version':'new'}], 'ru')
    assert changed['lines'][0]['voice_version'] == 'new'


def test_resource_revision_during_synthesis_is_not_published(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import series
    monkeypatch.setattr(series, 'series_dir', lambda p, s: tmp_path)
    version = ['v1']
    monkeypatch.setattr(series, 'resolve_snapshot', lambda p, s: {'characters':{'fox':{'version_id':version[0], 'data':{'name':'Лиса','voice':'v'}}}})
    path = tmp_path / '91_screenplay/screenplay.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'screenplay':[{'scene_number':1, 'dialogue':[{'id':'l','character':'Лиса','line':'Привет'}]}]}))
    monkeypatch.setattr(speech, '_synthesize', lambda _: b'paid')
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    def transcribe(*args):
        version[0] = 'v2'
        return [{'text':'Привет','start':.1,'end':.7}]
    monkeypatch.setattr(speech, '_transcribe', transcribe)
    assert speech.storybook_speech_tool('r','p','s')['status'] == 'error'
    assert not (tmp_path / '98_audio/speech.json').exists()
    assert speech.storybook_speech_tool('r','p','s')['status'] == 'success'
    assert json.loads((tmp_path / '98_audio/speech.json').read_text())['lines'][0]['voice_version'] == 'v2'


def test_existing_paid_audio_reused_after_resource_changed_before_first_retry(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'привет', 'speaker_id':'fox', 'voice':'v', 'voice_version':'old'}
    old_key = speech._fingerprint([line], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.mp3').write_bytes(b'paid')
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'привет', 'start':.1, 'end':.7}]))
    (directory.parent / 'speech.json').write_text(json.dumps({'status':'stale', 'lines':[{'id':'l', 'voice_version':'old'}]}))
    monkeypatch.setattr(speech, '_synthesize', lambda *_: pytest.fail('paid audio already exists'))
    monkeypatch.setattr(speech, '_transcribe', lambda *_: pytest.fail('words already exist'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    result = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'voice_version':'new'}], 'ru')
    assert result['lines'][0]['voice_version'] == 'new'


def test_voice_change_still_synthesizes_again(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'привет', 'speaker_id':'fox', 'voice':'v1', 'voice_version':'old'}
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append(lines[0]['voice']) or b'paid')
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *_: [{'text':'привет','start':.1,'end':.7}])
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'voice':'v2', 'voice_version':'new'}], 'ru')
    assert calls == ['v1', 'v2']


def test_pause_edit_reuses_paid_audio_and_changes_real_wav(tmp_path, monkeypatch):
    import subprocess
    tone = tmp_path / 'source.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', str(tone)], check=True)
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append('tts') or tone.read_bytes())
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'привет', 'start':.1, 'end':.7}])
    line = {'id':'l', 'text':'привет', 'voice':'v', 'voice_version':'one', 'pause_before':.5}
    first = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    second = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'pause_before':2}], 'ru')
    assert calls == ['tts', 'asr']
    assert second['input_hash'] != first['input_hash']
    assert second['lines'][0]['start'] == pytest.approx(first['lines'][0]['start'] + 1.5)
    root = tmp_path / '98_audio'
    assert speech._duration(root / second['audio_path']) == pytest.approx(speech._duration(root / first['audio_path']) + 1.5, abs=.03)


def test_speech_metadata_edits_reuse_paid_audio_and_current_metadata(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append('tts') or b'audio')
    monkeypatch.setattr(speech, '_duration', lambda path: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: [{'text':'привет', 'start':.1, 'end':.7}])
    line = {'id':'old', 'text':'привет', 'voice':'v', 'voice_version':'one', 'scene_number':1, 'shot_number':1, 'speaker_id':'a'}
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    second = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'id':'new', 'scene_number':2, 'shot_number':2, 'speaker_id':'b'}], 'ru')
    assert calls == ['tts']
    assert second['lines'][0]['id'] == 'new' and second['words'][0]['line_id'] == 'new'


@pytest.mark.parametrize('field,value', [('text','пока'), ('voice','v2'), ('delivery','громко')])
def test_actual_speech_request_change_synthesizes_again(tmp_path, monkeypatch, field, value):
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append('tts') or json.dumps(speech._synthesis_inputs(lines), ensure_ascii=False).encode())
    monkeypatch.setattr(speech, '_duration', lambda path: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda path, duration, language: [{'text': current_text[0], 'start':.1, 'end':.7}])
    current_text = ['привет']
    line = {'id':'l', 'text':'привет', 'voice':'v', 'delivery':'тихо'}
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    if field == 'text':
        current_text[0] = value
    speech.prepare_speech(tmp_path, 'p', 's', [{**line, field:value}], 'ru')
    assert calls == ['tts', 'tts']


def test_language_change_reuses_tts_but_retranscribes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda lines: calls.append('tts') or b'audio')
    monkeypatch.setattr(speech, '_duration', lambda path: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda path, duration, language: calls.append(language) or [{'text':'hello','start':.1,'end':.7}])
    line = {'id':'l', 'text':'hello', 'voice':'v'}
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'en')
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['tts', 'en', 'ru']


@pytest.mark.parametrize('old_key_kind', ['full', 'without_version'])
def test_old_paid_cache_reused_after_pause_and_metadata_change(tmp_path, monkeypatch, old_key_kind):
    line = {'id':'old', 'text':'привет', 'voice':'v', 'delivery':'тихо', 'voice_version':'v1',
            'pause_before':1, 'scene_number':1, 'shot_number':1, 'speaker_id':'a'}
    previous_line = {key:value for key,value in line.items() if key != 'voice'}
    previous_line.update(start=1.1, end=1.7, word_start=0, word_end=1)
    key_lines = [line] if old_key_kind == 'full' else [{k:v for k,v in line.items() if k != 'voice_version'}]
    old_key = speech._fingerprint(key_lines, 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.mp3').write_bytes(b'paid')
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'привет', 'start':.1, 'end':.7}]))
    (directory.parent / 'speech.json').write_text(json.dumps({'status':'stale', 'language':'ru', 'lines':[previous_line]}))
    monkeypatch.setattr(speech, '_synthesize', lambda *_: pytest.fail('existing paid audio'))
    monkeypatch.setattr(speech, '_transcribe', lambda *_: pytest.fail('existing words'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_insert_pauses', lambda source, target, duration, lines, words: (source, duration))
    result = speech.prepare_speech(tmp_path, 'p', 's', [{**line, 'id':'new', 'pause_before':2, 'scene_number':2}], 'ru')
    assert result['lines'][0]['id'] == 'new'


def test_old_words_not_reused_for_new_language(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'hello', 'voice':'v'}
    old_key = speech._fingerprint([line], 'en')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.mp3').write_bytes(b'paid')
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'hello','start':.1,'end':.7}]))
    (directory.parent / 'speech.json').write_text(json.dumps({'status':'stale', 'language':'en', 'lines':[{'id':'l','text':'hello','start':.1,'end':.7,'word_start':0,'word_end':1}]}))
    monkeypatch.setattr(speech, '_synthesize', lambda *_: pytest.fail('existing paid audio'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    calls = []
    monkeypatch.setattr(speech, '_transcribe', lambda path, duration, lang: calls.append(lang) or [{'text':'hello','start':.1,'end':.7}])
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['ru']


@pytest.mark.parametrize('current_audio_exists', [False, True])
def test_orphan_legacy_words_never_used_for_different_audio(tmp_path, monkeypatch, current_audio_exists):
    line = {'id':'l', 'text':'привет', 'voice':'v', 'voice_version':'v1'}
    old_key = speech._fingerprint([line], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'привет','start':.1,'end':.2}]))
    if current_audio_exists:
        audio_key = speech._fingerprint(speech._synthesis_inputs([line]), '')
        (directory / f'{audio_key}.mp3').write_bytes(b'current-audio')
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: calls.append('tts') or b'new-audio')
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'привет','start':.7,'end':.9}])
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == (['asr'] if current_audio_exists else ['tts', 'asr'])
    assert result['lines'][0]['start'] == .7 and result['lines'][0]['end'] == .9


def test_new_tts_does_not_reuse_orphan_current_words(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'привет', 'voice':'v'}
    audio_key = speech._fingerprint(speech._synthesis_inputs([line]), '')
    words_key = speech._fingerprint([{'audio':audio_key}], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{words_key}.words.json').write_text(json.dumps([{'text':'привет','start':.1,'end':.2}]))
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: calls.append('tts') or b'new-audio')
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'привет','start':.7,'end':.9}])
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['tts', 'asr'] and result['lines'][0]['start'] == .7


@pytest.mark.parametrize('same_bytes', [False, True])
def test_legacy_words_follow_audio_bytes_when_current_audio_exists(tmp_path, monkeypatch, same_bytes):
    line = {'id':'l', 'text':'привет', 'voice':'v', 'voice_version':'v1'}
    old_key = speech._fingerprint([line], 'ru')
    audio_key = speech._fingerprint(speech._synthesis_inputs([line]), '')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.mp3').write_bytes(b'old-audio')
    (directory / f'{audio_key}.mp3').write_bytes(b'old-audio' if same_bytes else b'other-audio')
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'привет','start':.1,'end':.2}]))
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: pytest.fail('current audio exists'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'привет','start':.7,'end':.9}])
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ([] if same_bytes else ['asr'])
    assert result['lines'][0]['start'] == (.1 if same_bytes else .7)


def test_recreated_audio_never_uses_other_language_query_key_words(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'hello', 'voice':'v'}
    audio_key = speech._fingerprint(speech._synthesis_inputs([line]), '')
    old_words_key = speech._fingerprint([{'audio':audio_key}], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_words_key}.words.json').write_text(json.dumps([{'text':'hello','start':.1,'end':.2}]))
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: calls.append('tts') or b'new-audio')
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda path, duration, language: calls.append(language) or [{'text':'hello','start':.7,'end':.9}])
    speech.prepare_speech(tmp_path, 'p', 's', [line], 'en')
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['tts', 'en', 'ru']
    assert result['lines'][0]['start'] == .7


def test_existing_audio_ignores_old_query_key_words(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'hello', 'voice':'v'}
    audio_key = speech._fingerprint(speech._synthesis_inputs([line]), '')
    old_words_key = speech._fingerprint([{'audio':audio_key}], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{audio_key}.mp3').write_bytes(b'current')
    (directory / f'{old_words_key}.words.json').write_text(json.dumps([{'text':'hello','start':.1,'end':.2}]))
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: pytest.fail('audio exists'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'hello','start':.7,'end':.9}])
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['asr'] and result['lines'][0]['start'] == .7


def test_bad_legacy_alignment_retries_asr_without_resynthesizing(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'привет', 'voice':'v'}
    old_key = speech._fingerprint([line], 'ru')
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    (directory / f'{old_key}.mp3').write_bytes(b'paid')
    (directory / f'{old_key}.words.json').write_text(json.dumps([{'text':'совсем другое','start':.1,'end':.2}]))
    calls = []
    monkeypatch.setattr(speech, '_synthesize', lambda _: pytest.fail('paid audio exists'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    monkeypatch.setattr(speech, '_transcribe', lambda *args: calls.append('asr') or [{'text':'привет','start':.7,'end':.9}])
    with pytest.raises(ValueError):
        speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert (directory / f'{old_key}.mp3').read_bytes() == b'paid'
    result = speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert calls == ['asr'] and result['lines'][0]['start'] == .7


def test_alignment_error_keeps_other_language_and_other_audio_words(tmp_path, monkeypatch):
    line = {'id':'l', 'text':'hello', 'voice':'v'}
    directory = tmp_path / '98_audio' / 'speech'
    directory.mkdir(parents=True)
    same_key = speech._fingerprint([line], 'ru')
    other_language_key = speech._fingerprint([line], 'en')
    different = {**line, 'id':'different'}
    other_audio_key = speech._fingerprint([different], 'ru')
    for key, audio in [(same_key, b'paid'), (other_language_key, b'paid'), (other_audio_key, b'other')]:
        (directory / f'{key}.mp3').write_bytes(audio)
        (directory / f'{key}.words.json').write_text(json.dumps([{'text':'unrelated','start':.1,'end':.2}]))
    monkeypatch.setattr(speech, '_synthesize', lambda _: pytest.fail('paid audio exists'))
    monkeypatch.setattr(speech, '_duration', lambda _: 1.)
    with pytest.raises(ValueError):
        speech.prepare_speech(tmp_path, 'p', 's', [line], 'ru')
    assert not (directory / f'{same_key}.words.json').exists()
    assert (directory / f'{same_key}.mp3').exists()
    assert (directory / f'{other_language_key}.words.json').exists()
    assert (directory / f'{other_audio_key}.words.json').exists()
