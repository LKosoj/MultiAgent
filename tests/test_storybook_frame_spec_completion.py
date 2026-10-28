"""Completion patches must fill missing spec data without rewriting source results."""
from __future__ import annotations

import copy
import json

from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


def test_deep_merge_fills_empty_values_but_preserves_existing_lists_and_strings():
    base = {
        "must_show": ["sealed hatch"],
        "must_not_show": ["open hatch"],
        "start_state_spec": {
            "must_show": ["device housing"],
            "pose_signature": "hands on hatch",
            "gaze_direction": "",
        },
        "transition_spec": {"environment_delta": []},
        "camera_anchor": "fixed side view",
    }
    patch = {
        "must_show": ["incoming signal"],
        "must_not_show": ["open device"],
        "start_state_spec": {
            "must_show": ["replacement housing"],
            "pose_signature": "different pose",
            "gaze_direction": "toward hatch",
            "camera_anchor": "near hatch",
        },
        "transition_spec": {"environment_delta": ["hatch remains sealed"]},
        "camera_anchor": "overhead view",
    }
    original_patch = copy.deepcopy(patch)

    shared_utils._deep_merge_shot_frame_spec_patch(base, patch)

    assert base["must_show"] == ["sealed hatch"]
    assert base["must_not_show"] == ["open hatch"]
    assert base["start_state_spec"]["must_show"] == ["device housing"]
    assert base["start_state_spec"]["pose_signature"] == "hands on hatch"
    assert base["start_state_spec"]["gaze_direction"] == "toward hatch"
    assert base["start_state_spec"]["camera_anchor"] == "near hatch"
    assert base["transition_spec"]["environment_delta"] == ["hatch remains sealed"]
    assert base["camera_anchor"] == "fixed side view"
    assert patch == original_patch


def test_completion_preserves_end_results_and_fills_empty_transition(monkeypatch):
    spec = {
        "scene_mode": "object_focus",
        "end_state_spec": {
            "must_show": ["fully closed hatch"],
            "must_not_show": ["incoming particles"],
        },
        "transition_spec": {"environment_delta": []},
    }

    def fake_call(**kwargs):
        return json.dumps({
            "end_state_spec": {
                "must_show": ["partially open hatch"],
                "must_not_show": ["clear air"],
            },
            "transition_spec": {
                "environment_delta": ["device emits a brief signal"],
            },
        })

    monkeypatch.setattr(shared_utils, "call_openai_api", fake_call)
    result = shared_utils._complete_shot_frame_spec_fields_via_llm(spec)

    assert result["end_state_spec"]["must_show"] == ["fully closed hatch"]
    assert result["end_state_spec"]["must_not_show"] == ["incoming particles"]
    assert result["transition_spec"]["environment_delta"] == ["device emits a brief signal"]


def test_explicit_empty_phase_cast_does_not_inherit_other_actors(monkeypatch):
    monkeypatch.setattr(shared_utils, 'call_openai_api', lambda **kwargs: '{}')
    monkeypatch.setattr(shared_utils, '_complete_shot_frame_spec_fields_via_llm', lambda spec:spec)
    raw={'scene_mode':'object_focus','primary_subject':'fragment','camera_anchor':'fixed',
         'visible_characters':['Hero'],'must_show':['fragment'],
         'start_state_spec':{'primary_subject':'fragment','camera_anchor':'fixed',
                             'must_show':['fragment'],'visible_characters':[]},
         'end_state_spec':{'primary_subject':'fragment','camera_anchor':'fixed',
                           'must_show':['fragment'],'visible_characters':['Hero']}}
    result=shared_utils._normalize_shot_frame_spec(raw,['Hero'])
    assert result['start_state_spec']['visible_characters']==[]
    assert result['end_state_spec']['visible_characters']==['Hero']


def test_completion_does_not_fill_an_explicitly_empty_phase_cast():
    base={'start_state_spec':{'visible_characters':[]}}
    shared_utils._deep_merge_shot_frame_spec_patch(base, {'start_state_spec':{'visible_characters':['Hero']}})
    assert base['start_state_spec']['visible_characters']==[]


def test_phase_profiles_include_only_that_phase_cast():
    profiles=[{'name':'First'},{'name':'Second'}]
    spec={'visible_characters':['First','Second'],'start_state_spec':{'visible_characters':['First']},
          'end_state_spec':{'visible_characters':[]}}
    start=shared_utils._build_phase_shot_character_visual_profiles(profiles,spec,state_key='start_state_spec')
    end=shared_utils._build_phase_shot_character_visual_profiles(profiles,spec,state_key='end_state_spec')
    assert [p['name'] for p in start]==['First']
    assert end==[]
