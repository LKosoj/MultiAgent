import pytest
from custom_tools.storybook.shot_speech_timing import apply_measured_shot_timing


def test_shots_use_measured_speech_and_keep_silent_gap():
    items = [dict(scene_number=1, shot_number=i, shot_type=phase, duration_s=2,
                  face_visibility_relative=[dict(speaker_id='a', start_fraction=0, end_fraction=1)])
             for i in (1, 2, 3) for phase in ('start', 'end')]
    speech = {'status': 'success', 'duration': 12, 'lines': [
        dict(scene_number=1, shot_number=1, start=0.5, end=3),
        dict(scene_number=1, shot_number=3, start=9, end=11)]}
    timed = apply_measured_shot_timing(items, speech)
    a, silent, b = timed[::2]
    assert a['start_s'] == 0 and b['end_s'] == 12
    assert a['end_s'] <= silent['end_s'] == b['start_s']
    assert silent['end_s'] > silent['start_s']
    assert a['end_s'] >= 3 and b['start_s'] <= 9
    assert a['face_visibility'][0]['end_s'] == a['end_s']


def test_missing_line_binding_fails():
    items = [dict(scene_number=1, shot_number=1, duration_s=2)]
    with pytest.raises(ValueError, match='shot_number'):
        apply_measured_shot_timing(items, {'status': 'success', 'duration': 2,
                                          'lines': [dict(scene_number=1, start=0, end=1)]})


def test_split_long_speech_at_word_gaps_without_losing_time():
    from custom_tools.storybook.shot_speech_timing import split_speech_interval
    words = [dict(start=0, end=2), dict(start=2.3, end=4),
             dict(start=4.8, end=7), dict(start=7.2, end=9.8)]
    parts = split_speech_interval(0, 10, words, 5)
    assert parts[0][0] == 0 and parts[-1][1] == 10
    assert all(end - start <= 5 for start, end in parts)
    assert all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1))
    assert all(not (word['start'] < end < word['end']) for _, end in parts[:-1] for word in words)


def test_visible_face_name_resolves_to_pinned_speaker_id():
    from custom_tools.storybook.video_plan import visible_speech_lines, _choose_with_speech
    item = dict(scene_number=1, shot_number=1, duration_s=2,
                face_visibility_relative=[dict(speaker_id='Петя', start_fraction=0, end_fraction=1)])
    speech = dict(status='success', duration=2, lines=[dict(id='l', scene_number=1, shot_number=1,
        speaker_id='hero-1', kind='on_screen', start=.2, end=1.5)])
    [shot] = apply_measured_shot_timing([item], speech, {'hero-1': 'Петя'})
    assert visible_speech_lines(shot, speech['lines']) == ['l']
    assert _choose_with_speech(shot, speech, {})['reason'] == 'visible_speech'


def test_ambiguous_visible_face_is_not_silently_ignored():
    item = dict(scene_number=1, shot_number=1, duration_s=2,
                face_visibility_relative=[dict(speaker_id='Петя', start_fraction=0, end_fraction=1)])
    with pytest.raises(ValueError, match='Петя'):
        apply_measured_shot_timing([item], {'duration': 2, 'lines': []}, {'a': 'Петя', 'b': 'Петя'})
