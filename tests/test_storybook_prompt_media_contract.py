import json

import pytest

from custom_tools.storybook import audio_subtitle, video_generator_aitunnel_tool as aitunnel
from custom_tools.storybook import video_generator_common as common


_PREPARED_KEY = "_prepared_english_video_prompt"


def test_pp14_common_marks_only_cyrillic_prompt_after_current_translation(tmp_path, monkeypatch):
    shots_path = tmp_path / "shots.json"
    image_path = tmp_path / "start.png"
    image_path.write_bytes(b"image")
    items = [{
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "Камера движется", "output_path": str(image_path),
        "image_description": json.dumps({"content_analysis": "scene"}),
        "image_description_timestamp": 1,
    }]
    shots_path.write_text(json.dumps({"items": items}), encoding="utf-8")
    monkeypatch.setattr(common, "generate_image_description", lambda _path: None)
    monkeypatch.setattr(common, "_simple_translate_prompt", lambda _prompt: "camera moves")
    assert common.update_shots_with_descriptions(
        str(shots_path), items, force_update=True, skip_prompt_enhancement=True,
    ) == 1
    assert items[0][_PREPARED_KEY] == "camera moves"

    latin_items = [{**items[0], "video_prompt": "La camera avance", "original_video_prompt": "La camera avance"}]
    monkeypatch.setattr(common, "_simple_translate_prompt", lambda prompt: prompt)
    assert common.update_shots_with_descriptions(
        str(shots_path), latin_items, force_update=True, skip_prompt_enhancement=True,
    ) == 1
    assert _PREPARED_KEY not in latin_items[0]


def test_pw02_enhancement_request_contains_existing_item_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "utils.call_openai_api",
        lambda **kwargs: captured.update(kwargs) or "camera holds while the door opens",
    )
    result = common.enhance_video_prompt(
        "camera holds while the door opens",
        json.dumps({"content_analysis": "door"}),
        item_context={
            "camera_plan": "CAMERA_MARKER",
            "timing": "TIMING_MARKER",
            "scene_pacing": "PACING_MARKER",
            "spatial_changes_from_start": "SPACE_MARKER",
        },
    )
    assert result
    request = captured["prompt"] + "\n" + captured["system_prompt"]
    for marker in ("CAMERA_MARKER", "TIMING_MARKER", "PACING_MARKER", "SPACE_MARKER"):
        assert marker in request
    monkeypatch.setattr(
        "utils.call_openai_api",
        lambda **kwargs: captured.update(kwargs) or "camera holds while the door opens",
    )
    common.enhance_video_prompt(
        "camera holds while the door opens",
        json.dumps({"content_analysis": "door"}),
        item_context=None,
    )
    assert "ITEM CONTEXT" not in captured["prompt"]


def test_pw03_video_prompt_is_not_a_subtitle_fallback():
    assert audio_subtitle._resolve_cue_text(
        {"scene_number": 1, "shot_number": 1, "video_prompt": "camera pans right"},
        {"shots": {}, "scenes": {}},
    ) == ("", "")


def test_pw03_existing_viewer_text_keeps_priority_and_content():
    item = {
        "scene_number": 1, "shot_number": 1, "subtitle": "ru subtitle",
        "voiceover": "en voiceover", "video_prompt": "camera pans right",
    }
    assert audio_subtitle._resolve_cue_text(item, {"shots": {}, "scenes": {}}) == ("ru subtitle", "shots.item")


@pytest.mark.parametrize("initial_payload", [
    {"items": []},
    {"items": [{"scene_number": 2, "shot_number": 1, "shot_type": "start", "video_prompt": "other"}]},
    [],
])
def test_pp14_transient_marker_never_reaches_any_shots_json_write_path(tmp_path, monkeypatch, initial_payload):
    shots_path = tmp_path / "shots.json"
    image_path = tmp_path / "start.png"
    image_path.write_bytes(b"image")
    items = [{
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "Камера движется", "output_path": str(image_path),
        "image_description": json.dumps({"content_analysis": "scene"}),
        "image_description_timestamp": 1,
    }]
    shots_path.write_text(json.dumps(initial_payload), encoding="utf-8")
    monkeypatch.setattr(common, "generate_image_description", lambda _path: None)
    monkeypatch.setattr(common, "_simple_translate_prompt", lambda _prompt: "camera moves")
    assert common.update_shots_with_descriptions(
        str(shots_path), items, force_update=True, skip_prompt_enhancement=True,
    ) == 1
    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    assert all(_PREPARED_KEY not in item for item in saved["items"])


def test_pp14_prepared_marker_requires_exact_current_prompt(monkeypatch):
    prepared = {_PREPARED_KEY: "translated English prompt", "video_prompt": "translated English prompt"}
    calls = []
    monkeypatch.setattr(
        aitunnel, "translate_prompts_in_items",
        lambda item, _language: calls.append(item["video_prompt"]) or {**item, "video_prompt": "translated again"},
    )
    assert aitunnel._resolve_video_prompt(prepared, "ru") == "translated English prompt"
    prepared["video_prompt"] = "manual replacement"
    assert aitunnel._resolve_video_prompt(prepared, "ru") == "translated again"
    assert calls == ["manual replacement"]


def test_pp14_unprepared_latin_non_english_project_prompt_still_translates(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aitunnel, "translate_prompts_in_items",
        lambda item, _language: calls.append(item["video_prompt"]) or {**item, "video_prompt": "English translation"},
    )
    assert aitunnel._resolve_video_prompt({"video_prompt": "La camera avance"}, "fr") == "English translation"
    assert calls == ["La camera avance"]


@pytest.mark.parametrize("language,original_prompt,provider_translations", [
    ("ru", "Камера движется", []),
    ("fr", "La camera avance", ["La camera avance"]),
])
def test_pp14_real_preparation_reload_and_resolution_translate_once(
    tmp_path, monkeypatch, language, original_prompt, provider_translations,
):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("AITUNNEL_API_KEY", "test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "model")
    shots_path = tmp_path / "project" / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True)
    start_image = tmp_path / "project" / "97_shots" / "start.png"
    start_image.write_bytes(b"image")
    original = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": original_prompt, "video_path": str(tmp_path / "video.mp4"),
        "start_image": str(start_image), "output_path": str(start_image),
        "image_description": json.dumps({"content_analysis": "scene"}),
        "image_description_timestamp": 1,
    }
    shots_path.write_text(json.dumps({"items": [original]}), encoding="utf-8")
    resolved = []
    preparation_calls = []
    translations = []

    def prepare(prompt):
        preparation_calls.append(prompt)
        return "camera moves" if language == "ru" else prompt

    monkeypatch.setattr(common, "generate_image_description", lambda _path: None)
    monkeypatch.setattr(common, "_simple_translate_prompt", prepare)
    monkeypatch.setattr("utils.call_openai_api", lambda **_kwargs: pytest.fail("unexpected LLM call"))
    monkeypatch.setattr(aitunnel, "_get_aitunnel_video_models", lambda: {"model": {}})
    monkeypatch.setattr(
        aitunnel, "translate_prompts_in_items",
        lambda item, _language: translations.append(item["video_prompt"]) or {**item, "video_prompt": "camera moves"},
    )
    monkeypatch.setattr(
        aitunnel,
        "_generate_single_video_aitunnel",
        lambda item, *args, **kwargs: resolved.append(aitunnel._resolve_video_prompt(item, language)) or {"success": True},
    )
    aitunnel.video_generator_aitunnel_tool(
        "s", project_id="project", enable=True, language=language, skip_prompt_enhancement=True,
    )
    assert preparation_calls == [original_prompt]
    assert translations == provider_translations
    assert resolved == ["camera moves"]
    saved = json.loads(shots_path.read_text(encoding="utf-8"))
    assert all(_PREPARED_KEY not in item for item in saved["items"])
