import json

import pytest

from custom_tools.storybook import shots_prompt_qa as qa


def test_video_judge_receives_geometry_and_invalidates_cached_verdict(monkeypatch):
    requests = []

    def call(**kwargs):
        requests.append(json.loads(kwargs["prompt"].split("INPUT:\n", 1)[1]))
        return '{"contradiction_before": true, "candidate_ok": true}'

    monkeypatch.setattr(qa, "call_openai_api", call)
    monkeypatch.setattr(qa, "_VIDEO_JUDGE_CACHE", {})
    params = dict(
        before_video_prompt="A seated observer looks out.",
        candidate_video_prompt="The seated observer breathes quietly.",
        storyboard_description="An observer wearing boots watches quietly.",
        storyboard_camera_plan="MEDIUM SHOT",
    )
    first = {"end_state_spec": {"world_physics": {"occlusion_constraints": ["table hides feet"]}}}
    second = {"end_state_spec": {"world_physics": {"occlusion_constraints": ["desk hides lower body"]}}}
    qa._llm_judge_video_prompt_lite(**params, frame_spec=first)
    qa._llm_judge_video_prompt_lite(**params, frame_spec=second)

    assert [request["shot_frame_spec"] for request in requests] == [first, second]


@pytest.mark.parametrize("spec_phase", ["start", "end"])
def test_repair_validation_preserves_current_phase_geometry(monkeypatch, spec_phase):
    specs = []

    def judge(**kwargs):
        specs.append(kwargs.get("frame_spec"))
        return {"contradiction_before": True, "candidate_ok": True}

    monkeypatch.setattr(qa, "_llm_judge_video_prompt_lite", judge)
    monkeypatch.setattr(qa, "_normalize_shot_frame_spec", lambda value, characters: value)
    spec = {"start_state_spec": {"visible_characters": ["Observer"]},
            "end_state_spec": {"world_physics": {"occlusion_constraints": ["table hides feet"]}}}
    item = {"scene_number": 1, "shot_number": 1, "shot_type": "start",
            "video_prompt": "Boots become visible.", "_shot_frame_spec": {"must_show": ["feet"]}}
    repair = {"scene_number": 1, "shot_number": 1, "shot_type": "start",
              "set": {"video_prompt": "The seated observer breathes quietly."}}
    items, repairs = [item], [repair]
    if spec_phase == "start":
        repair["set"]["_shot_frame_spec"] = spec
    else:
        items.append(dict(item, shot_type="end"))
        repairs.append({"scene_number": 1, "shot_number": 1, "shot_type": "end",
                        "set": {"_shot_frame_spec": spec}})
    applied, rejected = qa._apply_repairs_to_items(
        items, repairs, storyboard_lookup={(1, 1): {
            "description": "An observer wearing boots watches quietly.", "camera_plan": "MEDIUM SHOT"}},
    )

    assert applied == len(repairs)
    assert not rejected
    assert specs == [spec, spec]
