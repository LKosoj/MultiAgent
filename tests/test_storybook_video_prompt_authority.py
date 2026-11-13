import json

import utils
from custom_tools.storybook import video_generator_common as common


def test_english_video_prompt_is_aligned_to_bound_style_and_actual_duration(monkeypatch):
    requests = []
    def align(**kwargs):
        requests.append(kwargs)
        return 'Locked-off tripod shot. The figure moves within 1.8s in the supplied visual style.'
    monkeypatch.setattr(utils, 'call_openai_api', align)
    original = '0-8s: The figure moves. An obsolete visual treatment.'
    context = {'visual_style': 'A project-defined visual medium.', 'duration_s': 1.8}
    result = common._simple_translate_prompt(original, context)
    assert result and len(requests) == 1
    assert json.loads(requests[0]['prompt']) == {'video_prompt': original, 'context': context}
    assert 'replace conflicting legacy style wording' in requests[0]['system_prompt']
    assert 'retime old time marks' in requests[0]['system_prompt']
    assert 'Preserve action order, camera, staging' in requests[0]['system_prompt']


def test_english_translation_without_project_context_keeps_existing_fast_path(monkeypatch):
    monkeypatch.setattr(utils, 'call_openai_api', lambda **kwargs: (_ for _ in ()).throw(AssertionError('No alignment requested')))
    assert common._simple_translate_prompt(' A moving figure. ') == 'A moving figure.'
