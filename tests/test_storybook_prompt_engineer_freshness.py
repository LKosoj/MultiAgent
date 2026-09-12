import json

from custom_tools.storybook import items_builder, prompt_engineer as pe


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_inputs(root):
    _write(root / "10_synopsis" / "beats.json", [{"page_number": 1}, {"page_number": 2}])
    _write(root / "20_story" / "story.json", {"title": "T", "pages": []})
    _write(root / "20_bible" / "characters.json", [])
    _write(root / "20_bible" / "locations.json", [])
    _write(root / "30_style" / "style_images.json", {})
    path = root / "30_style" / "negative_prompt_list.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("none", encoding="utf-8")


def test_prompt_cache_requires_expected_pages_and_actual_input_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / "p"
    _prepare_inputs(root)
    prompts = root / "40_prompts"
    prompts.mkdir()
    _write(prompts / "page_02_prompt.json", {"old": True})
    _write(prompts / "page_99_prompt.json", {"user_file": True})
    calls = []

    monkeypatch.setattr(pe, "_build_story_page_lookup", lambda _story: {})
    monkeypatch.setattr(pe, "_build_scene_packets", lambda beats, *_args: list(beats))
    monkeypatch.setattr(pe, "_annotate_scene_packets", lambda packets, *_args: packets)
    monkeypatch.setattr(pe, "_select_frame_specs", lambda **_kwargs: [{}, {}])
    monkeypatch.setattr(pe, "_validate_prompts", lambda *_args: None)
    monkeypatch.setattr(pe, "_sync_prompt_references_with_frame_spec", lambda *_args: None)
    monkeypatch.setattr(pe, "_finalize_prompt_from_frame_spec", lambda *_args: None)
    monkeypatch.setattr(pe, "_extract_authoritative_readable_texts", lambda *_args: [])

    def fake_api(**_kwargs):
        calls.append(1)
        return json.dumps({"prompts": [
            {"english_prompt": "one", "negative_prompt": "n", "references": {}},
            {"english_prompt": "two", "negative_prompt": "n", "references": {}},
        ]})

    monkeypatch.setattr(pe, "call_openai_api", fake_api)
    pe.prompt_engineer_tool("s", "p", language="en")
    assert len(calls) == 1
    assert (prompts / "page_01_prompt.json").exists()
    assert json.loads((prompts / "page_99_prompt.json").read_text(encoding="utf-8")) == {"user_file": True}

    pe.prompt_engineer_tool("s", "p", language="en")
    assert len(calls) == 1

    _write(root / "20_story" / "story.json", {"title": "Changed", "pages": []})
    pe.prompt_engineer_tool("s", "p", language="en")
    assert len(calls) == 2


def test_items_builder_ignores_retained_prompt_after_page_count_shrinks(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / "p"
    _write(root / "10_synopsis" / "beats.json", [{"page_number": 1}, {"page_number": 2}])
    for page in (1, 2, 3):
        _write(root / "40_prompts" / f"page_{page:02d}_prompt.json", {
            "technical": {}, "references": {}, "english_prompt": str(page), "negative_prompt": "n",
        })

    items = json.loads(items_builder.items_for_artist_tool("s", "p", "en"))

    assert [item["page_number"] for item in items["items"]] == [1, 2]
    assert (root / "40_prompts" / "page_03_prompt.json").exists()
