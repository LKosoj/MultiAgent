"""Контракт подтверждённого визуального источника для style_keeper."""

from __future__ import annotations

import json

import pytest


SOURCE_QUOTE = "Визуальный стиль: акварель, графика, ксероксный коллаж и перекладка."


@pytest.fixture
def _storybook_project(tmp_path, monkeypatch):
    project_id = "source_style_test"
    base = tmp_path / project_id
    (base / "10_synopsis").mkdir(parents=True)
    (base / "20_bible").mkdir(parents=True)
    (base / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
    (base / "10_synopsis" / "beats.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "characters.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "locations.json").write_text("[]", encoding="utf-8")
    (base / "00_brief.json").write_text(
        json.dumps({"storybook_prompt": SOURCE_QUOTE}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "llmgateway/gpt-image-2")
    return project_id, base


def _llm_reply(*, source_quote=SOURCE_QUOTE):
    payload = {
        "style_preset_id": "retro_print",
        "style_overrides": {
            "art_style": "2D watercolour and cutout collage",
            "texture": "watercolour paper and photocopy cutouts",
            "material_signature": "ink bleed, paper edges and layered cutouts",
            "model": "flux-1.1-pro",
        },
        "style_text": {},
        "negative_list": "modern cars",
    }
    if source_quote is not None:
        payload["source_style_quote"] = source_quote
    return json.dumps(payload, ensure_ascii=False)


def test_style_keeper_applies_confirmed_source_style(_storybook_project, monkeypatch):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper
    from custom_tools.storybook import style_library_config as sl

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply(),
    )

    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    style_images = json.loads(
        (base / "30_style" / "style_images.json").read_text(encoding="utf-8")
    )
    assert style_images["model"] == "llmgateway/gpt-image-2"
    assert style_images["art_style"] == "2D watercolour and cutout collage"
    assert style_images["texture"] == "watercolour paper and photocopy cutouts"
    assert style_images["material_signature"] == (
        "ink bleed, paper edges and layered cutouts"
    )
    assert style_images["source_style_quote"] == SOURCE_QUOTE
    assert style_images["camera_defaults"] == {}
    assert style_images["style_pitfalls"] == sl.get_active_style_profile().universal_pitfalls

    negative = (base / "30_style" / "negative_prompt_list.txt").read_text(
        encoding="utf-8"
    )
    assert "modern cars" in negative
    assert "watermark" in negative
    assert "full cmyk photographic colour" not in negative


def test_style_keeper_matches_source_quote_ignoring_markdown_spacing_and_case(
    _storybook_project, monkeypatch
):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper

    (base / "00_brief.json").write_text(
        json.dumps(
            {
                "storybook_prompt": (
                    "Визуальный стиль: **АКВАРЕЛЬ, графика,  ксероксный\n"
                    "коллаж и перекладка.**"
                )
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply(
            source_quote="ВИЗУАЛЬНЫЙ СТИЛЬ: АКВАРЕЛЬ, ГРАФИКА, КСЕРОКСНЫЙ КОЛЛАЖ И ПЕРЕКЛАДКА."
        ),
    )

    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    style_images = json.loads(
        (base / "30_style" / "style_images.json").read_text(encoding="utf-8")
    )
    assert style_images["style_preset_id"] == "retro_print"
    assert style_images["model"] == "llmgateway/gpt-image-2"
    assert style_images["art_style"] == "2D watercolour and cutout collage"
    assert style_images["texture"] == "watercolour paper and photocopy cutouts"
    assert style_images["material_signature"] == (
        "ink bleed, paper edges and layered cutouts"
    )


def test_style_keeper_rejects_unconfirmed_source_quote(_storybook_project, monkeypatch):
    project_id, _ = _storybook_project
    from custom_tools.storybook import style_keeper

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply(
            source_quote="неподтверждённая цитата"
        ),
    )

    with pytest.raises(ValueError):
        style_keeper.style_keeper_tool(session_id="s", project_id=project_id)


def test_style_keeper_ignores_source_overrides_without_confirmed_quote(
    _storybook_project, monkeypatch
):
    project_id, base = _storybook_project
    from custom_tools.storybook import style_keeper
    from custom_tools.storybook import style_library_config as sl

    monkeypatch.setattr(
        style_keeper,
        "call_openai_api",
        lambda prompt, system_prompt, **kwargs: _llm_reply(
            source_quote=None
        ),
    )

    style_keeper.style_keeper_tool(session_id="s", project_id=project_id)

    style_images = json.loads(
        (base / "30_style" / "style_images.json").read_text(encoding="utf-8")
    )
    preset = sl.resolve_preset("retro_print")
    assert style_images["style_preset_id"] == "retro_print"
    assert style_images["art_style"] == preset.art_style
    assert style_images["texture"] == preset.texture
    assert style_images["material_signature"] == preset.material_signature
    assert "source_style_quote" not in style_images
