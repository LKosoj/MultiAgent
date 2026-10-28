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
