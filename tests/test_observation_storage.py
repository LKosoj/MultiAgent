from memory.observation_storage import (
    compact_observation_for_context,
    parse_observation_entries,
)


SAMPLE_SEARCH = """## Search Results

|AI News June 2026: In-Depth and Concise](https://theaitrack.com/ai-news-june-2026-in-depth-and-concise/)
Each month, we compile significant news, trends, and happenings in AI, providing detailed summaries with
key points in bullet form for concise yet complete understanding.

|LinkedIn - Wikipedia](https://en.wikipedia.org/wiki/LinkedIn)
LinkedIn is an American business and employment-oriented social networking service used globally.
"""


def test_parse_observation_entries_extracts_titles_and_urls():
    entries = parse_observation_entries(SAMPLE_SEARCH)
    assert len(entries) == 2
    assert entries[0].url.startswith("https://theaitrack.com")
    assert "AI News June 2026" in entries[0].title
    assert "compile significant news" in entries[0].description


def test_compact_keeps_urls_and_truncates_descriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "memory.observation_storage._PLOTS_ROOT",
        str(tmp_path),
    )
    long_desc = "x" * 500
    text = f"## Search Results\n\n[Article A](https://example.com/a)\n{long_desc}\n\n[Article B](https://example.com/b)\nshort"
    compact = compact_observation_for_context(
        text,
        session_id="sess1",
        agent_name="researcher",
        step_number=1,
        externalize_threshold=50_000,
        max_description_chars=80,
    )

    assert "https://example.com/a" in compact
    assert "https://example.com/b" in compact
    assert "Article A" in compact
    assert "Article B" in compact
    assert "x" * 500 not in compact
    assert "short" in compact
    assert not list(tmp_path.rglob("step_1.md"))


def test_compact_externalizes_large_observation_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "memory.observation_storage._PLOTS_ROOT",
        str(tmp_path),
    )
    big_text = SAMPLE_SEARCH + ("\n|Extra](https://example.com/extra)\n" + ("detail " * 3000))

    compact = compact_observation_for_context(
        big_text,
        session_id="sess2",
        agent_name="researcher",
        step_number=3,
        run_id="run-abc",
        externalize_threshold=1000,
        max_description_chars=120,
    )

    saved = tmp_path / "observations" / "sess2" / "researcher" / "step_3.md"
    assert saved.exists()
    saved_content = saved.read_text(encoding="utf-8")
    assert "detail detail" in saved_content
    assert "run-abc" in saved_content

    assert "plots/observations/sess2/researcher/step_3.md" in compact
    assert "file_read" in compact
    assert "https://theaitrack.com" in compact
    assert len(compact) < len(big_text) // 4

    index = tmp_path / "observations" / "sess2" / "index.md"
    assert index.exists()
    assert "step_3.md" in index.read_text(encoding="utf-8")
