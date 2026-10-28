from custom_tools.storybook import screenplay_shots_generator as shots_generator


def _context():
    phase = {
        "primary_subject": "neutral subject",
        "must_show": ["neutral subject"],
        "world_physics": {},
    }
    return {
        "shot_description": "neutral subject in the courtyard",
        "scene_action": "neutral action",
        "camera_plan": "Medium shot",
        "location_canon_name": "Workshop",
        "shot_frame_spec": {
            "primary_subject": "neutral subject",
            "must_show": ["neutral subject"],
            "start_state_spec": dict(phase),
            "end_state_spec": dict(phase),
        },
    }


def test_start_preserves_technical_location_when_artistic_result_omits_it(monkeypatch):
    courtyard_context = {
        "name": "Courtyard",
        "reference_image_path": "/references/locations/courtyard.png",
    }
    monkeypatch.setattr(
        shots_generator,
        "_analyze_shot_technical",
        lambda context: {
            "main_subject": "neutral subject",
            "camera_position": "side",
            "character_orientation": "profile",
            "spatial_composition": "center",
            "initial_state_summary": "standing",
            "location": "Courtyard",
            "location_context": courtyard_context,
        },
    )
    monkeypatch.setattr(
        shots_generator,
        "_generate_shot_artistic",
        lambda technical_params, context, language="en": {
            "english_prompt": "START WITHOUT LOCATION",
            "negative_prompt": "negative",
        },
    )

    result = shots_generator._generate_shot_prompt(_context(), "start", language="ru")

    assert result["location"] == "Courtyard"
    assert result["location_context"] == courtyard_context


def test_end_preserves_technical_location_when_artistic_result_omits_it(monkeypatch):
    courtyard_context = {
        "name": "Courtyard",
        "reference_image_path": "/references/locations/courtyard.png",
    }
    monkeypatch.setattr(
        shots_generator,
        "_analyze_end_shot_technical",
        lambda start_technical_params, video_prompt, extended_context: {
            "location": "Courtyard",
            "location_context": courtyard_context,
            "final_shot_size": "Medium shot",
            "framing_delta_percent": 0,
            "subject_scale_ratio": 1.0,
            "final_camera_position": "side",
            "final_character_orientation": "profile",
            "final_spatial_composition": "center",
            "final_lighting_style": "daylight",
            "final_color_palette": "neutral",
            "final_point_of_view": "objective",
        },
    )
    monkeypatch.setattr(
        shots_generator,
        "_generate_end_shot_artistic",
        lambda *args, **kwargs: {
            "english_prompt": "END WITHOUT LOCATION",
            "negative_prompt": "negative",
        },
    )

    result = shots_generator._generate_shot_prompt(
        _context(),
        "end",
        start_llm_result={
            "english_prompt": "START",
            "shot_size": "Medium shot",
            "camera_position": "side",
        },
        language="ru",
    )

    assert result["location"] == "Courtyard"
    assert result["location_context"] == courtyard_context
