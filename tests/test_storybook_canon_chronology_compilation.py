import json
from pathlib import Path

import utils

from custom_tools.storybook import artist_batch_edit as artist
from custom_tools.storybook import style_keeper


def _prepare_project(tmp_path):
    project_dir = tmp_path / "plots" / "storybooks" / "chronology_project"
    style_dir = project_dir / "30_style"
    style_dir.mkdir(parents=True)
    (style_dir / "style_images.json").write_text(
        json.dumps(
            {
                "art_style": "GLOBAL_FILM_STYLE",
                "project_note": "GLOBAL_FUTURE_STYLE",
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
    return project_dir, source, output


def _patch_common(monkeypatch, source, output, captured):
    monkeypatch.setattr(
        artist,
        "_smart_select_references_for_generation",
        lambda *args, **kwargs: [str(source)],
    )
    monkeypatch.setattr(artist, "build_canon_image_prompt", lambda *args: "CANON_TEMPLATE")
    monkeypatch.setattr(
        utils,
        "translate_prompts_in_items",
        lambda item, language: dict(item),
    )
    monkeypatch.setattr(
        style_keeper,
        "ensure_image_style_catalog",
        lambda *args, **kwargs: {
            "default_style_id": "main",
            "styles": {"main": {"prompt": "READY_STYLE"}},
            "frame_bindings": {},
        },
    )

    def fake_edit(**kwargs):
        captured.update(kwargs)
        Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kwargs["output_path"]).write_bytes(b"fake output")
        return "ok"

    monkeypatch.setattr(artist, "edit_image_vse_tool", fake_edit)


def test_location_canon_prompt_compiles_chronology_before_style(monkeypatch, tmp_path):
    _project_dir, source, output = _prepare_project(tmp_path)
    captured = {}
    calls = []
    _patch_common(monkeypatch, source, output, captured)

    entity = {
        "name": "Courtyard",
        "description": "A narrow courtyard beside a workshop",
        "historical_period": "late nineteenth century",
        "environment_context": "brick paving, hand tools, no modern infrastructure",
        "reference_image_path": str(source),
        "_passport_version": 1,
    }

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        payload_text = str(kwargs.get("prompt") or "")
        assert "late nineteenth century" in payload_text
        assert "brick paving, hand tools" in payload_text
        assert "CANON_TEMPLATE" in payload_text
        assert "_passport_version" not in payload_text
        assert "READY_STYLE" not in payload_text
        assert "GLOBAL_FUTURE_STYLE" not in payload_text
        return json.dumps(
            {
                "english_prompt": "PERIOD_SPECIFIC_ENTITY_PROMPT, empty workshop without fuel",
                "negative_prompt": "UNRELATED_NEGATIVE_DUMP",
            }
        )

    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)

    assert artist._create_canon_reference(
        "session",
        "location",
        entity,
        [str(source)],
        str(output),
        [],
    ) is True

    assert len(calls) == 1
    assert captured["prompt"].count("PERIOD_SPECIFIC_ENTITY_PROMPT") == 1
    assert "empty workshop without fuel" in captured["prompt"]
    assert captured["prompt"].count("READY_STYLE") == 1
    assert "_passport_version" not in captured["prompt"]
    assert "UNRELATED_NEGATIVE_DUMP" not in captured["negative_prompt"]


def test_location_canon_prompt_empty_compilation_skips_image_api(monkeypatch, tmp_path):
    _project_dir, source, output = _prepare_project(tmp_path)
    captured = {}
    calls = []
    _patch_common(monkeypatch, source, output, captured)

    monkeypatch.setattr(
        utils,
        "call_openai_api",
        lambda **kwargs: calls.append(kwargs)
        or json.dumps({"english_prompt": "", "negative_prompt": "negative"}),
        raising=False,
    )

    assert artist._create_canon_reference(
        "session",
        "location",
        {
            "name": "Courtyard",
            "description": "A narrow courtyard",
            "historical_period": "late nineteenth century",
            "environment_context": "brick paving",
            "reference_image_path": str(source),
        },
        [str(source)],
        str(output),
        [],
    ) is False
    assert len(calls) == 1
    assert "prompt" not in captured
    assert not output.exists()
