"""Модель экрана «Файлы» (без дисплея): поля формы, применение ввода, разбор JSON, группы."""

import pytest

from StoryBookManager.gui.screens import files_model as fm

BRIEF = {"title": "Лиса", "pages": 12, "ready": False, "note": None, "tags": ["a", "b"]}


def _field(pointer):
    return next(f for f in fm.build_fields("brief", BRIEF) if f["pointer"] == pointer)


def test_build_fields_kinds():
    assert _field("/title")["kind"] in ("string", "text")
    assert _field("/pages")["kind"] == "number"
    assert _field("/ready")["kind"] == "bool"
    assert _field("/note")["kind"] == "null"


def test_apply_input_string_changes_only_that_field():
    out = fm.apply_input("brief", BRIEF, "/title", "Волк")
    assert out["error"] is None
    assert out["content"]["title"] == "Волк"
    assert out["content"]["pages"] == 12
    assert BRIEF["title"] == "Лиса"  # исходный документ не меняется


def test_apply_input_number_accepts_comma_and_rejects_text():
    assert fm.apply_input("brief", BRIEF, "/pages", "7,5")["content"]["pages"] == 7.5
    bad = fm.apply_input("brief", BRIEF, "/pages", "много")
    assert bad["error"]
    assert bad["content"] == BRIEF


def test_apply_input_bool_needs_real_bool():
    assert fm.apply_input("brief", BRIEF, "/ready", True)["content"]["ready"] is True
    assert fm.apply_input("brief", BRIEF, "/ready", "yes")["content"] == BRIEF


def test_apply_input_null_field_empty_stays_null():
    assert fm.apply_input("brief", BRIEF, "/note", "")["content"]["note"] is None
    assert fm.apply_input("brief", BRIEF, "/note", "текст")["content"]["note"] == "текст"


def test_parse_json_text_round_trip_and_error_with_line():
    text = fm.content_to_text(BRIEF)
    assert fm.parse_json_text(text) == BRIEF
    with pytest.raises(ValueError) as exc:
        fm.parse_json_text('{"a": }')
    assert "строка 1" in str(exc.value)


def test_group_of_is_stable_for_top_level_field():
    assert fm.group_of(_field("/title")["path"]) == fm.group_of(_field("/pages")["path"])


def test_unknown_pointer_changes_nothing():
    out = fm.apply_input("brief", BRIEF, "/нет", "x")
    assert out == {"content": BRIEF, "error": None}
