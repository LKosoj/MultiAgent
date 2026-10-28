import json
from pathlib import Path

import utils

from custom_tools.storybook import artist_batch_edit as artist


def test_canon_reference_uses_cached_catalog_style_only(tmp_path, monkeypatch):
    project_dir = tmp_path / "plots" / "storybooks" / "neutral_project"
    style_dir = project_dir / "30_style"
    style_dir.mkdir(parents=True)
    (style_dir / "style_images.json").write_text(
        json.dumps(
            {
                "color_palette": "GLOBAL_OTHER_PALETTES",
                "model": "GLOBAL_MODEL_ID",
                "project_note": "GLOBAL_PROJECT_NOTE",
                "do_not_include": ["GLOBAL_DO_NOT_INCLUDE"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (style_dir / "image_styles.json").write_text(
        json.dumps(
            {
                "default_style_id": "main",
                "styles": {"main": {"prompt": "READY_STYLE"}},
                "frame_bindings": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    output = project_dir / "20_bible" / "references" / "locations" / "courtyard.png"
    captured = {}
    build_calls = []

    monkeypatch.setattr(
        artist,
        "_smart_select_references_for_generation",
        lambda *args, **kwargs: [str(source)],
    )
    def fake_build_canon_image_prompt(*args):
        build_calls.append(args)
        return "ENTITY_PROMPT"

    monkeypatch.setattr(artist, "build_canon_image_prompt", fake_build_canon_image_prompt)
    monkeypatch.setattr(
        utils,
        "call_openai_api",
        lambda **kwargs: json.dumps({"english_prompt": "ENTITY_PROMPT", "negative_prompt": ""}),
    )
    monkeypatch.setattr(
        utils,
        "translate_prompts_in_items",
        lambda item, language: dict(item),
    )

    def fake_edit(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kwargs["output_path"]).write_bytes(b"fake output")
        return "ok"

    monkeypatch.setattr(artist, "edit_image_vse_tool", fake_edit)

    assert artist._create_canon_reference(
        "session",
        "location",
        {"name": "Courtyard", "reference_image_path": str(source)},
        [str(source)],
        str(output),
        [{"rule": "GLOBAL_FUTURE_RULE"}],
    ) is True

    prompt = captured["prompt"]
    assert "ENTITY_PROMPT" in prompt
    assert "READY_STYLE" in prompt
    assert "GLOBAL_OTHER_PALETTES" not in prompt
    assert "GLOBAL_MODEL_ID" not in prompt
    assert "GLOBAL_PROJECT_NOTE" not in prompt
    assert "GLOBAL_DO_NOT_INCLUDE" not in prompt
    assert build_calls[0][2] == []
