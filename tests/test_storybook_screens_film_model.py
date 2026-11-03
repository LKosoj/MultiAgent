"""Модель «Фильма» (без tkinter): все золотые векторы и раскладка дорожек."""

import json
from pathlib import Path

import pytest

from StoryBookManager.gui.screens import film_model as fm
from StoryBookManager.gui.screens.film_texts import FILM_TEXTS

GOLDEN = Path(__file__).parent / "fixtures/storybook_screens"


def _load(name):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


VEC = _load("golden_film_vectors.json")
LAYOUTS = _load("golden_film_layout.json")["cases"]


def _norm(x):
    return json.loads(json.dumps(x))


def test_texts_equal_golden():
    assert FILM_TEXTS == _load("golden_film_texts.json")


@pytest.mark.parametrize("name", ("tick_step", "fit_scale", "clamp_scale", "zoom_in", "zoom_out", "format_position",
                                   "clamp_time", "time_from_x", "run_transition", "resolve_state", "music_rows",
                                   "file_rows", "default_source", "merge_narrow_plain"))
def test_simple_vectors(name):
    real = name.replace("_plain", "")
    for case in VEC["vectors"].get(real, []):
        args = case["in"]
        if real == "merge_narrow":
            continue
        assert _norm(getattr(fm, real)(*args)) == case["out"], (real, args)


def test_ruler_ticks():
    for case in VEC["vectors"]["ruler_ticks"]:
        got = [[t["t"], t["label"]] for t in fm.ruler_ticks(*case["in"])]
        assert got == case["out"]


def test_cue_at():
    for case in VEC["vectors"]["cue_at"]:
        cue = fm.cue_at(VEC["cues"], case["in"][0])
        assert (cue["index"] if cue else None) == case["out"]


def test_banners():
    for case in VEC["vectors"]["banners"]:
        assert fm.banner_ids(*case["in"]) == case["out"]


def test_key_action():
    for case in VEC["vectors"]["key_action"]:
        key, shift, in_field = case["in"]
        assert fm.key_action(key, shift, in_field) == case["out"]


def test_merge_narrow():
    for case in VEC["vectors"]["merge_narrow"]:
        blocks = [{"track": "shots", "start_s": b["s"], "end_s": b["e"], "label": "t", "key": "k", "tooltip": "", "nested": False}
                  for b in case["in"][0]]
        got = [{"start_s": b["start_s"], "end_s": b["end_s"], "label": b["label"]} for b in fm.merge_narrow(blocks, case["in"][1])]
        assert got == case["out"]


@pytest.mark.parametrize("case", LAYOUTS, ids=lambda c: c["name"])
def test_layout(case):
    assert _norm(fm.build_layout(case["timeline"], case["cues"])) == case["layout"]


def test_block_at():
    layout = fm.build_layout(LAYOUTS[0]["timeline"], LAYOUTS[0]["cues"])
    for case in VEC["vectors"]["block_at"]:
        block = fm.block_at(layout["blocks"], *case["in"])
        assert (block["label"].replace("Шот ", "") if block else None) == case["out"]
