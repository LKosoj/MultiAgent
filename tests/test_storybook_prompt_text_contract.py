import json

from custom_tools.storybook import music_planner, story_editor, story_writer


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _story_project(tmp_path):
    base = tmp_path / "project"
    _write(base / "00_brief.json", {
        "language": "fr", "genre": "mystery", "target_age": "10-12",
        "tone": "calm", "words_per_page_min": 10, "words_per_page_max": 20,
    })
    _write(base / "10_synopsis" / "synopsis.json", {"title": "T"})
    _write(base / "10_synopsis" / "beats.json", [{"page_number": 1}])
    _write(base / "20_story" / "story.json", {
        "title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}],
    })
    _write(base / "30_style" / "style_text.json", {
        "narrative_voice": "VOICE_MARKER",
        "sentence_length": "LENGTH_MARKER",
        "vocabulary_bounds": "VOCAB_MARKER",
    })
    return base


def test_pp12_writer_and_both_editor_paths_send_canonical_style_with_explicit_requirements(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _story_project(tmp_path)
    writer_call, batch_call, chapter_call = {}, {}, {}

    def writer_api(**kwargs):
        writer_call.update(kwargs)
        return json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}]})

    def editor_api(**kwargs):
        payload = json.loads(kwargs["prompt"])
        target = batch_call if "story" in payload else chapter_call
        target.update(kwargs)
        if "story" in payload:
            return json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}]})
        return json.dumps({"page": 1, "title": "P", "body": "B"})

    monkeypatch.setattr(story_writer, "call_openai_api", writer_api)
    monkeypatch.setattr(story_editor, "call_openai_api", editor_api)
    (tmp_path / "project" / "20_story" / "story.json").unlink()
    story_writer.story_writer_tool("s", "project")
    story_editor.story_editor_tool("s", "project", force_edit=True, edit_all_chapters=True)
    story_editor.story_editor_tool("s", "project", force_edit=True, edit_all_chapters=False)

    for captured in (writer_call, batch_call, chapter_call):
        text = captured["prompt"] + "\n" + captured["system_prompt"]
        assert "VOICE_MARKER" in text
        assert "LENGTH_MARKER" in text
        assert "VOCAB_MARKER" in text
        assert "fr" in text
        assert "10-12" in text
        assert "mystery" in text
        assert "calm" in text
        assert "10-20" in text


def test_pw01_music_prompt_allows_its_required_no_vocals_suffix():
    prompt = music_planner._system_prompt(2, "ru")
    assert "instrumental only, no vocals" in prompt
    assert "must never mention singing/vocals/lyrics/voice" not in prompt


def test_pp12_editor_formats_one_sided_word_limit_without_none(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = _story_project(tmp_path)
    _write(base / "00_brief.json", {"language": "en", "target_age": "all", "words_per_page_max": 100})
    captured = {}
    monkeypatch.setattr(
        story_editor,
        "call_openai_api",
        lambda **kwargs: captured.update(kwargs) or json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}]}),
    )
    story_editor.story_editor_tool("s", "project", force_edit=True, edit_all_chapters=True)
    assert "не более 100 слов" in captured["system_prompt"]
    assert "None" not in captured["system_prompt"]
