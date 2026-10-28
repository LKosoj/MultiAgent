"""Проверка phase-local readable text источников без сетевых вызовов."""
from __future__ import annotations

import json
import pytest

import utils
from custom_tools.storybook import artist_batch_edit as artist


def _item(shot_type: str):
    return {
        "shot_type": shot_type,
        "characters": [],
        "locations": [{"name": "Blank paper", "description": "blank paper"}],
        "_shot_frame_spec": {
            "visible_readable_texts": ["Dynamic"],
            "start_state_spec": {"visible_readable_texts": []},
            "end_state_spec": {"visible_readable_texts": ["Dynamic"]},
        },
    }


def test_end_collects_dynamic_phase_readable_text(monkeypatch):
    captured = {}

    def fake_call_openai_api(**kwargs):
        payload = json.loads(kwargs["prompt"].split("Source data:\n", 1)[1])
        captured.update(payload)
        assert "Dynamic" in payload["candidate_texts"]
        assert payload["frame_spec"] == {"visible_readable_texts": ["Dynamic"]}
        return json.dumps({
            "bindings": [{
                "text": "Dynamic",
                "carrier": "paper sign",
                "visibility": "keep the exact lettering readable",
            }],
        })

    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(artist, "_normalize_visible_text_bindings_language", lambda bindings: bindings)

    result = artist._collect_visible_text_bindings(_item("end"), "A blank sheet", "en")

    assert result == [{
        "text": "Dynamic",
        "carrier": "paper sign",
        "visibility": "keep the exact lettering readable",
    }]
    assert captured["frame_spec"]["visible_readable_texts"] == ["Dynamic"]


def test_start_does_not_inherit_end_or_global_dynamic_text(monkeypatch):
    captured = {}

    def fake_call_openai_api(**kwargs):
        payload = json.loads(kwargs["prompt"].split("Source data:\n", 1)[1])
        captured.update(payload)
        assert "Dynamic" not in payload["candidate_texts"]
        assert payload["frame_spec"] == {"visible_readable_texts": []}
        return json.dumps({"bindings": []})

    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)
    monkeypatch.setattr(artist, "_normalize_visible_text_bindings_language", lambda bindings: bindings)

    assert artist._collect_visible_text_bindings(_item("start"), "A blank sheet", "en") == []
    assert captured["candidate_texts"] == []
    assert captured["frame_spec"] == {"visible_readable_texts": []}


@pytest.mark.parametrize("entry", ['полная надпись «ВХОД» на двери', 'надпись "ВХОД" на двери'])
def test_phase_text_descriptor_binds_quoted_literal(monkeypatch, entry):
    item = _item("end")
    item["_shot_frame_spec"]["end_state_spec"]["visible_readable_texts"] = [entry]
    captured = {}

    def fake_call(**kwargs):
        captured.update(json.loads(kwargs["prompt"].split("Source data:\n", 1)[1]))
        return json.dumps({"bindings": [{
            "text": "ВХОД", "carrier": "door surface", "visibility": "fully readable",
        }]})

    monkeypatch.setattr(utils, "call_openai_api", fake_call)
    monkeypatch.setattr(artist, "_normalize_visible_text_bindings_language", lambda bindings: bindings)
    result = artist._collect_visible_text_bindings(item, "Closed door", "ru")
    assert captured["candidate_texts"] == ["ВХОД"]
    assert result[0]["text"] == "ВХОД"
