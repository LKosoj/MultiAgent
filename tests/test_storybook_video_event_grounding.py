import json
from custom_tools.storybook import screenplay_shots_generator as generator


def test_transition_request_keeps_each_phase_cast_and_physics(monkeypatch):
    captured={}
    monkeypatch.setattr(generator,'_classify_shot_type',lambda *a:{'edge_case_rules':[]})
    monkeypatch.setattr(generator,'_validate_video_prompt_structure',lambda text:None)
    def call(**kwargs):
        captured.update(json.loads(kwargs['prompt'].split('INPUT:\n',1)[1]))
        return '{"video_prompt":"Camera locks off at eye level; the first subject leaves naturally; snow drifts; steadily"}'
    monkeypatch.setattr(generator,'call_openai_api',call)
    spec={'visible_characters':['First','Second'],
          'start_state_spec':{'visible_characters':['First'],'world_physics':{'body_relation':'outside left'}},
          'end_state_spec':{'visible_characters':['Second'],'world_physics':{'body_relation':'outside right'}}}
    generator._generate_transition_video_prompt({'english_prompt':'First'},{'english_prompt':'Second'},
                                                {'shot_frame_spec':spec,'shot_description':'First leaves; Second is revealed.'})
    assert captured['start_visible_characters']==['First']
    assert captured['end_visible_characters']==['Second']
    assert captured['start_world_physics']['body_relation']=='outside left'
    assert captured['end_world_physics']['body_relation']=='outside right'
