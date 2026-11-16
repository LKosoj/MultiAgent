"""English motion instructions preserve source-visible writing."""
import pytest

from custom_tools.storybook import shots_prompt_qa as qa


@pytest.mark.parametrize('visible,prompt,accepted', [
    (['табличка «ВЫХОД» над дверью'], 'Locked-off medium shot: the lamp illuminates the sign "ВЫХОД" completely by END.', True),
    ([], 'Locked-off medium shot: the lamp illuminates the sign "ВЫХОД" completely by END.', False),
    (['табличка «ВЫХОД» над дверью'], 'Камера неподвижна; лампа освещает табличку "ВЫХОД".', False),
])
def test_source_literal_does_not_allow_cyrillic_instruction_prose(monkeypatch, visible, prompt, accepted):
    monkeypatch.setattr(qa, '_llm_judge_video_prompt_lite', lambda **kwargs: {
        'contradiction_before': True, 'candidate_ok': True, 'rewritten_candidate': None,
    })
    item = {
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'video_prompt': 'The lamp is dark.',
        '_shot_frame_spec': {'visible_readable_texts': visible},
    }
    applied, rejected = qa._apply_repairs_to_items([item], [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'set': {'video_prompt': prompt},
    }], storyboard_lookup={(1, 1): {
        'camera_plan': 'MEDIUM SHOT', 'description': 'The lamp illuminates the sign completely.',
    }})
    assert applied == int(accepted)
    assert len(rejected) == int(not accepted)
    assert item['video_prompt'] == (prompt if accepted else 'The lamp is dark.')


def test_video_judge_requires_achieved_end_state_and_verbatim_visible_text(monkeypatch):
    qa._VIDEO_JUDGE_CACHE.clear()
    calls = []
    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"contradiction_before": false, "candidate_ok": true}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_call)
    qa._llm_judge_video_prompt_lite(
        before_video_prompt='A lamp begins illuminating a sign.', candidate_video_prompt=None,
        storyboard_description='By the end the sign «ВЫХОД» is completely illuminated.',
        storyboard_camera_plan='MEDIUM SHOT', model_override='test',
    )
    assert 'visible text verbatim' in calls[0]['system_prompt']
    assert 'achieved by END' in calls[0]['system_prompt']
    assert 'every source-authored event and reaction in source order' in calls[0]['system_prompt']
    assert 'Accept a compliant candidate unchanged' in calls[0]['system_prompt']
    assert 'minimal edits' in calls[0]['system_prompt']
    assert '3-6 MUST_INCLUDE_FACTS' not in calls[0]['system_prompt']


@pytest.mark.parametrize('candidate', [None, 'The lamp illuminates the sign.'])
def test_video_candidate_check_does_not_receive_rejected_before_text(monkeypatch, candidate):
    import json
    qa._VIDEO_JUDGE_CACHE.clear()
    calls = []
    def fake_call(**kwargs):
        calls.append(json.loads(kwargs['prompt'].split('INPUT:\n', 1)[1]))
        return '{"contradiction_before": true, "candidate_ok": true}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_call)
    qa._llm_judge_video_prompt_lite(
        before_video_prompt='Wrong old action.', candidate_video_prompt=candidate,
        storyboard_description='The lamp illuminates the sign.',
        storyboard_camera_plan='MEDIUM SHOT', model_override='test', duration_s=2.75,
    )
    assert calls[0]['duration_s'] == 2.75
    assert calls[0]['candidate_video_prompt'] == (candidate or '')
    if candidate is None:
        assert calls[0]['before_video_prompt'] == 'Wrong old action.'
    else:
        assert 'before_video_prompt' not in calls[0]


def test_formal_length_violation_allows_repair_without_story_contradiction(monkeypatch):
    monkeypatch.setattr(qa, '_llm_judge_video_prompt_lite', lambda **kwargs: {
        'contradiction_before': False, 'candidate_ok': True, 'rewritten_candidate': None,
    })
    item = {
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'video_prompt': ' '.join(['Steady'] * 151),
    }
    candidate = 'Camera locks off at eye level; the lamp glows steadily in a medium shot.'
    applied, rejected = qa._apply_repairs_to_items([item], [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'set': {'video_prompt': candidate},
    }], storyboard_lookup={(1, 1): {
        'camera_plan': 'MEDIUM SHOT', 'description': 'The lamp glows steadily.',
    }})
    assert applied == 1 and rejected == []
    assert item['video_prompt'] == candidate


def test_formal_timing_violation_allows_repair_despite_negative_semantic_judge(monkeypatch):
    monkeypatch.setattr(qa, '_llm_judge_video_prompt_lite', lambda **kwargs: {
        'contradiction_before': False, 'candidate_ok': True, 'rewritten_candidate': None,
    })
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
            'duration_s': 2.75, 'video_prompt': '0-8s: The lamp illuminates.'}
    candidate = 'Locked-off tripod shot, fixed frame. 0-2.75s: The lamp illuminates.'
    applied, rejected = qa._apply_repairs_to_items([item], [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'set': {'video_prompt': candidate},
    }], storyboard_lookup={(1, 1): {'camera_plan': 'MEDIUM SHOT', 'description': 'The lamp illuminates.'}})
    assert applied == 1 and rejected == []
    assert item['video_prompt'] == candidate


@pytest.mark.parametrize('stage', ['video', 'end_image', 'global_image'])
def test_intermediate_judges_use_shared_source_priority(monkeypatch, stage):
    qa._VIDEO_JUDGE_CACHE.clear()
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    qa._ENGLISH_SET_JUDGE_CACHE.clear()
    calls = []
    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true, "ok": true, "contradiction_before": false}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_call)
    common = {'storyboard_description': 'Hero lifts a parcel from the table.', 'storyboard_camera_plan': 'MEDIUM SHOT'}
    if stage == 'video':
        qa._llm_judge_video_prompt_lite(before_video_prompt='Hero stands.', candidate_video_prompt='Hero lifts the parcel.', **common)
    elif stage == 'end_image':
        qa._llm_judge_end_english_prompt_candidate(before_english_prompt='Parcel on table.', candidate_english_prompt='Hero holds parcel.', transition_video_prompt='Hero lifts parcel.', **common)
    else:
        qa._llm_judge_english_prompt_set_safe(before_english_prompt='Parcel on table.', after_english_prompt='Hero holds parcel.', shot_type='end', **common)
    assert calls
    assert 'SOURCE PRIORITY (highest to lowest)' in calls[0]['system_prompt']
    assert 'video_prompt spans the full START-to-END transition' in calls[0]['system_prompt']


def test_video_judge_rewrite_cannot_reintroduce_formal_length_violation(monkeypatch):
    calls = []
    def judge(**kwargs):
        calls.append(kwargs)
        return {'contradiction_before': True, 'candidate_ok': False,
                'rewritten_candidate': ' '.join(['Steady'] * 151)}
    monkeypatch.setattr(qa, '_llm_judge_video_prompt_lite', judge)
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start', 'video_prompt': 'Wrong action.'}
    applied, rejected = qa._apply_repairs_to_items([item], [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'set': {'video_prompt': 'Camera locks off; the lamp glows steadily.'},
    }], storyboard_lookup={(1, 1): {'camera_plan': 'MEDIUM SHOT', 'description': 'The lamp glows steadily.'}})
    assert applied == 0 and rejected
    assert item['video_prompt'] == 'Wrong action.'
