from custom_tools.storybook.shot_speech_timing import apply_measured_shot_timing


def test_reapplying_measured_timing_keeps_original_storyboard_weights():
    items = [{'scene_number': 1, 'shot_number': n, 'shot_type': 'start', 'duration_s': duration}
             for n, duration in enumerate((2, 3, 4), 1)]
    speech = {'duration': 10, 'lines': [{'scene_number': 1, 'shot_number': 2, 'start': 3, 'end': 6}]}
    first = apply_measured_shot_timing(items, speech)
    second = apply_measured_shot_timing(first, speech)
    assert [(i['start_s'], i['end_s']) for i in second] == [(i['start_s'], i['end_s']) for i in first]


def test_legacy_aligned_shots_reuse_storyboard_timing_after_partial_regeneration():
    items = [{'scene_number': 1, 'shot_number': n, 'shot_type': 'start', 'timing': f'{duration}s', 'duration_s': duration}
             for n, duration in enumerate((2, 3, 4), 1)]
    speech = {'duration': 10, 'lines': [{'scene_number': 1, 'shot_number': 2, 'start': 3, 'end': 6}]}
    first = apply_measured_shot_timing(items, speech)
    for item in first:
        item.pop('storyboard_duration_s')
    regenerated = dict(items[1])
    mixed = [first[0], regenerated, first[2]]
    result = apply_measured_shot_timing(mixed, speech)
    assert [(i['start_s'], i['end_s']) for i in result] == [(i['start_s'], i['end_s']) for i in first]
