from custom_tools.storybook.video_prompt_rules import (
    STATIC_CAMERA_SENTENCE,
    VIDEO_PROMPT_MAX_WORDS,
    VIDEO_PROMPT_RULES,
    finalize_video_prompt,
    shape_anchors,
    video_prompt_violations,
)


def _item(visible=True, shape='the boulder: a perfectly round, smooth grey granite sphere'):
    return {'_continuity': {'start': {
        'entities': [{'id': 'stone', 'name': 'Камень', 'kind': 'prop', 'shape': shape}],
        'states': {'stone': {'visible': visible}}}}}


def test_rules_fix_length_single_event_timeline_tempo_and_positive_camera():
    assert f'80-{VIDEO_PROMPT_MAX_WORDS} words' in VIDEO_PROMPT_RULES
    assert 'One clip shows one main event' in VIDEO_PROMPT_RULES
    assert '0-2s' in VIDEO_PROMPT_RULES
    assert 'real-time, natural speed' in VIDEO_PROMPT_RULES
    assert STATIC_CAMERA_SENTENCE in VIDEO_PROMPT_RULES


def test_violations_flag_long_prompt_and_negated_camera():
    assert video_prompt_violations('Slow dolly in on the hero. real-time, natural speed.') == []
    long_prompt = ' '.join(['word'] * (VIDEO_PROMPT_MAX_WORDS + 1))
    assert video_prompt_violations(long_prompt) == [
        f'video_prompt_too_long:{VIDEO_PROMPT_MAX_WORDS + 1}_words_max_{VIDEO_PROMPT_MAX_WORDS}']
    assert video_prompt_violations('The camera does not move; a leaf falls.') == ['camera_phrased_as_negation']
    assert video_prompt_violations('Wide shot, no camera movement.') == ['camera_phrased_as_negation']


def test_static_camera_sentence_goes_first_once():
    prompt = 'Static extreme long shot: sand drifts across the tomb.'
    result = finalize_video_prompt(prompt)
    assert result == f'{STATIC_CAMERA_SENTENCE} {prompt}'
    assert finalize_video_prompt(result) == result
    moving = 'Slow dolly in: the hero passes a static statue.'
    assert finalize_video_prompt(moving) == moving


def test_shape_lock_added_for_visible_objects_without_duplicates():
    result = finalize_video_prompt('Wide shot: the hero pushes the boulder uphill.', [_item(), _item()])
    assert result.endswith('Shape lock: the boulder: a perfectly round, smooth grey granite sphere.')
    assert finalize_video_prompt(result, [_item()]) == result
    assert shape_anchors([_item(visible=False)]) == []
    assert shape_anchors([_item(shape='')]) == []
