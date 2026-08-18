"""Дедуп RU/EN синонимов в negative_prompt (technical._dedup_negative_prompt)."""

from __future__ import annotations

from custom_tools.storybook.screenplay_shots_generator_utils.technical import (
    _dedup_negative_prompt,
)


def test_mixed_ru_en_split_screen_collapses_to_one():
    src = "split screen, split-screen, splitscreen, разделённый экран, blur"
    result = _dedup_negative_prompt(src)
    parts = [p.strip() for p in result.split(",")]
    assert parts == ["split screen", "blur"]


def test_ru_synonyms_keep_first_occurrence():
    src = "разделённый экран, сплит-скрин, split screen"
    result = _dedup_negative_prompt(src)
    parts = [p.strip() for p in result.split(",")]
    assert parts == ["разделённый экран"]


def test_hyphen_and_underscore_are_ignored():
    src = "split-screen, split_screen, split screen"
    result = _dedup_negative_prompt(src)
    parts = [p.strip() for p in result.split(",")]
    assert parts == ["split-screen"]


def test_unknown_unique_token_passes_through():
    src = "very_specific_forbidden_token, split screen, split-screen"
    result = _dedup_negative_prompt(src)
    parts = [p.strip() for p in result.split(",")]
    assert parts == ["very_specific_forbidden_token", "split screen"]


def test_empty_and_none_input_return_empty():
    assert _dedup_negative_prompt("") == ""
    assert _dedup_negative_prompt(None) == ""  # type: ignore[arg-type]
