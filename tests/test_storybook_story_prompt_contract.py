import json

import pytest

from custom_tools.storybook import story_planner, story_writer


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_planner_explicitly_passes_requested_english_language(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    _write(tmp_path / "p" / "00_brief.json", {
        "language": "en", "genre": "horror", "target_age": "18+",
        "pages_min": 1, "pages_max": 1,
    })
    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return json.dumps({"synopsis": {}, "beats": []})

    monkeypatch.setattr(story_planner, "call_openai_api", fake_api)
    story_planner.story_planner_tool("s", "p")

    assert "Пиши по-русски" not in captured["system_prompt"]
    assert "en" in captured["system_prompt"]
    payload = json.loads(captured["prompt"])
    assert payload["language"] == "en"
    assert payload["genre"] == "horror"
    assert payload["target_age"] == "18+"


def test_writer_keeps_adult_brief_explicit_without_child_only_ban(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "p"
    _write(base / "00_brief.json", {
        "language": "en", "genre": "horror", "target_age": "18+",
        "words_per_page_min": 1, "words_per_page_max": 5,
    })
    _write(base / "10_synopsis" / "synopsis.json", {})
    _write(base / "10_synopsis" / "beats.json", [{"page_number": 1}])
    _write(base / "20_bible" / "characters.json", [])
    _write(base / "20_bible" / "locations.json", [])
    _write(base / "20_bible" / "consistency_rules.json", [])
    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}]})

    monkeypatch.setattr(story_writer, "call_openai_api", fake_api)
    result = story_writer.story_writer_tool("s", "p")

    assert result["regenerated"] is True
    assert "en" in captured["system_prompt"]
    assert "Жанр: horror" in captured["system_prompt"]
    assert "Аудитория: 18+" in captured["system_prompt"]
    assert "Без насилия и мрачных деталей" not in captured["system_prompt"]
    payload = json.loads(captured["prompt"])
    assert payload["language"] == "en"
    assert payload["genre"] == "horror"
    assert payload["target_age"] == "18+"


@pytest.mark.parametrize(
    ("target_age", "allows_adult_content"),
    [("взрослые", True), ("для взрослых", True), ("6-8", False)],
)
def test_writer_applies_child_ban_only_to_non_adult_russian_audiences(
    tmp_path, monkeypatch, target_age, allows_adult_content,
):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "p"
    _write(base / "00_brief.json", {"language": "ru", "target_age": target_age})
    _write(base / "10_synopsis" / "synopsis.json", {})
    _write(base / "10_synopsis" / "beats.json", [{"page_number": 1}])
    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return json.dumps({"title": "T", "pages": [{"page": 1, "title": "P", "body": "B"}]})

    monkeypatch.setattr(story_writer, "call_openai_api", fake_api)
    story_writer.story_writer_tool("s", "p")

    child_ban = "Без насилия и мрачных деталей"
    assert (child_ban not in captured["system_prompt"]) is allows_adult_content
