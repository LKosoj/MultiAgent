from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook.artist_batch_edit import _background_reference_binding


def _png(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "gray").save(path)
    return str(path)


def test_background_sheet_is_pinned_and_location_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")
    sheet = _png(tmp_path / "route_sheet.png")
    continuity = _png(tmp_path / "97_shots/scene_01_shot_01/img_final_end.png")
    boy = _png(tmp_path / "characters/boy.png")
    platonov = _png(tmp_path / "characters/platonov.png")
    location = _png(tmp_path / "locations/moskva_dvor.png")
    paths, note = _background_reference_binding(
        {"background_reference": {"sheet_path": sheet, "card_number": 7}},
        [continuity, boy, platonov, location],
        "ru",
    )
    assert paths == [continuity, boy, platonov, sheet]
    assert "image 4" in note and "карточку 7" in note
    assert "сетку" in note


def test_background_sheet_survives_four_reference_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")
    sheet = _png(tmp_path / "sheet.png")
    refs = [_png(tmp_path / f"ref{i}.png") for i in range(7)]
    paths, _ = _background_reference_binding(
        {"background_reference": {"sheet_path": sheet, "card_number": 2}},
        refs,
        "en",
    )
    assert paths[-1] == sheet
    assert len(paths) == 4


def test_required_character_is_not_silently_dropped_for_background(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")
    sheet = _png(tmp_path / "sheet.png")
    refs = [_png(tmp_path / f"characters/ref{i}.png") for i in range(3)]
    continuity = _png(tmp_path / "97_shots/previous.png")
    location = _png(tmp_path / "locations/location.png")
    with pytest.raises(ValueError, match="лимит"):
        _background_reference_binding(
            {"background_reference": {"sheet_path": sheet, "card_number": 2}},
            [continuity, *refs, location], "en", {location},
        )


def test_items_without_binding_are_unchanged(tmp_path):
    refs = [_png(tmp_path / "location.png"), _png(tmp_path / "boy.png")]
    paths, note = _background_reference_binding({}, refs, "ru")
    assert paths == refs
    assert note == ""


def test_build_edit_instruction_uses_bound_sheet_in_wire_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")
    monkeypatch.setattr("utils.needs_translation_to_english", lambda _: False)
    continuity = _png(tmp_path / "97_shots/scene_01_shot_01/img_final_end.png")
    boy = _png(tmp_path / "characters/boy.png")
    platonov = _png(tmp_path / "characters/platonov.png")
    location = _png(tmp_path / "locations/moskva_dvor.png")
    sheet = _png(tmp_path / "background_routes/chase_sheet_01.png")
    from custom_tools.storybook.artist_batch_edit import _build_edit_instruction

    instruction, paths, _, prompt = _build_edit_instruction(
        "session",
        {
            "project_id": "",
            "scene_number": 2,
            "shot_number": 3,
            "shot_type": "end",
            "english_prompt": "Continue the exterior chase.",
            "negative_prompt": "text",
            "_visible_text_bindings": [],
            "reference_image_paths": [continuity, boy, platonov, location],
            "locations": [{"name": "yard", "reference_image_path": location}],
            "characters": [{"name": "Boy"}, {"name": "Platonov"}],
            "background_reference": {"sequence_id": "chase", "sheet_path": sheet, "card_number": 3},
            "output_path": str(tmp_path / "out.png"),
        },
        language="en",
    )
    assert paths == [continuity, boy, platonov, sheet]
    assert "image 4, card 3" in prompt
    assert "image_paths=[" in instruction
    assert sheet in instruction and location not in instruction


def test_wire_rebuilds_roles_after_cap_without_losing_text(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")
    monkeypatch.setattr("utils.needs_translation_to_english", lambda _: False)
    continuity = _png(tmp_path / "97_shots/previous.png")
    boy = _png(tmp_path / "characters/boy.png")
    platonov = _png(tmp_path / "characters/platonov.png")
    extra = _png(tmp_path / "characters/extra.png")
    location = _png(tmp_path / "locations/location.png")
    sheet = _png(tmp_path / "97_shots/background_references/chase/sheet.png")
    from custom_tools.storybook.artist_batch_edit import _build_edit_instruction

    item = {
        "project_id": "", "scene_number": 2, "shot_number": 3, "shot_type": "end",
        "english_prompt": 'Кадр. Опорные образы: image 1 as old; image 2 as Boy; image 3 as Platonov; image 4 as extra; image 5 as location.\n\n[in-image text: "СНЕГ"] Keep exposure 6500 K.',
        "negative_prompt": "text", "reference_image_paths": [continuity, boy, platonov, extra, location],
        "locations": [{"name": "yard", "reference_image_path": location}],
        "characters": [
            {"name": "Boy", "reference_image_path": "/references/characters/boy.png", "immutable_attributes": {"face_shape": "angular"}, "appearance_state": "свитер, без пальто"},
            {"name": "Platonov", "reference_image_path": platonov, "immutable_attributes": {"face_shape": "long"}},
        ],
        "background_reference": {"sequence_id": "chase", "sheet_path": sheet, "card_number": 3},
        "_visible_text_bindings": [{"text": "СНЕГ", "carrier": "reads SNOW", "visibility": "no extra text"}],
        "output_path": str(tmp_path / "out.png"),
    }
    instruction, paths, _, prompt = _build_edit_instruction("session", item, language="ru")
    assert paths == [continuity, boy, platonov, sheet]
    assert "background-card sheet" in prompt
    assert "old" not in prompt and "image 5" not in prompt
    assert "СНЕГ" in prompt
    assert "Keep exposure 6500 K" in prompt
    assert "Boy" in prompt and "Platonov" in prompt
    assert "angular" in prompt and "свитер, без пальто" in prompt
    assert sheet in instruction and location not in instruction
