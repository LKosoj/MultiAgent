import json

from custom_tools.storybook import items_builder, prompt_engineer, style_keeper


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_prompt_engineer_uses_cached_catalog_bindings_and_appends_after_validator(
    tmp_path, monkeypatch
):
    project_id = "book_style_bindings"
    base = tmp_path / project_id
    _write_json(base / "10_synopsis" / "beats.json", [{"page_number": 1}, {"page_number": 2}])
    _write_json(base / "20_bible" / "characters.json", [])
    _write_json(base / "20_bible" / "locations.json", [])
    _write_json(base / "20_story" / "story.json", {"title": "Neutral book"})
    _write_json(base / "30_style" / "style_images.json", {"art_style": "legacy source"})
    (base / "30_style" / "negative_prompt_list.txt").write_text(
        "GLOBAL_NEGATIVE_MARKER", encoding="utf-8"
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    catalog_calls = []
    catalog = {
        "default_style_id": "main",
        "styles": {
            "main": {"prompt": "MAIN BOOK STYLE"},
            "other": {"prompt": "OTHER BOOK STYLE"},
        },
        "page_bindings": {"1": "other"},
    }

    def fake_ensure(base_arg, style_images):
        catalog_calls.append((base_arg, style_images))
        return catalog

    monkeypatch.setattr(style_keeper, "ensure_image_style_catalog", fake_ensure)
    monkeypatch.setattr(
        prompt_engineer,
        "_build_scene_packets",
        lambda *args, **kwargs: [
            {"page_number": 1, "page_body": "first"},
            {"page_number": 2, "page_body": "second"},
        ],
    )
    monkeypatch.setattr(prompt_engineer, "_annotate_scene_packets", lambda packets, *a, **k: packets)
    monkeypatch.setattr(
        prompt_engineer,
        "_select_frame_specs",
        lambda **kwargs: [
            {"selected_moment": "first", "primary_subject": "subject"},
            {"selected_moment": "second", "primary_subject": "subject"},
        ],
    )
    monkeypatch.setattr(prompt_engineer, "_prompt_cache_is_current", lambda *a, **k: False)
    validated = []
    monkeypatch.setattr(
        prompt_engineer,
        "_finalize_prompt_from_frame_spec",
        lambda prompt, *args, **kwargs: validated.append(dict(prompt)),
    )
    monkeypatch.setattr(prompt_engineer, "_validate_prompts", lambda *args, **kwargs: None)

    model_calls = []

    def fake_call_openai_api(**kwargs):
        model_calls.append(kwargs)
        payload = json.loads(kwargs["prompt"])
        assert "style_images" not in payload
        assert "negative_list" not in payload
        return json.dumps(
            {
                "prompts": [
                    {"english_prompt": "PAGE ONE", "negative_prompt": "page negative", "references": {}},
                    {"english_prompt": "PAGE TWO", "negative_prompt": "page negative", "references": {}},
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(prompt_engineer, "call_openai_api", fake_call_openai_api)
    output_dir = prompt_engineer.prompt_engineer_tool("session", project_id, language="ru")

    assert output_dir.endswith("/40_prompts")
    assert len(catalog_calls) == 1
    assert len(model_calls) == 1
    assert len(validated) == 2
    assert "Не генерируй и не выбирай художественный стиль" in model_calls[0]["system_prompt"]

    page_one = json.loads((base / "40_prompts" / "page_01_prompt.json").read_text(encoding="utf-8"))
    page_two = json.loads((base / "40_prompts" / "page_02_prompt.json").read_text(encoding="utf-8"))
    assert page_one["style_id"] == "other"
    assert page_two["style_id"] == "main"
    assert "OTHER BOOK STYLE" in page_one["english_prompt"]
    assert "MAIN BOOK STYLE" in page_two["english_prompt"]
    assert "OTHER BOOK STYLE" not in validated[0]["english_prompt"]
    assert "MAIN BOOK STYLE" not in validated[1]["english_prompt"]


def test_items_builder_forwards_page_style_id(tmp_path, monkeypatch):
    project_id = "book_style_items"
    base = tmp_path / project_id
    _write_json(base / "10_synopsis" / "beats.json", [{"page_number": 1}])
    _write_json(base / "20_bible" / "characters.json", [])
    _write_json(base / "20_bible" / "locations.json", [])
    _write_json(
        base / "40_prompts" / "page_01_prompt.json",
        {
            "style_id": "other",
            "english_prompt": "PAGE ONE",
            "negative_prompt": "negative",
            "technical": {"size": "1920x1088"},
            "references": {},
        },
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    result = json.loads(items_builder.items_for_artist_tool("session", project_id, "ru"))
    assert result["items"][0]["style_id"] == "other"
