"""Модель «Болванки» (без tkinter): золотые векторы."""

import json
from pathlib import Path

from StoryBookManager.gui.screens import blockout_model as bm
from StoryBookManager.gui.screens.blockout_texts import BLOCKOUT_TEXTS

GOLDEN = Path(__file__).parent / "fixtures/storybook_screens"
DATA = json.loads((GOLDEN / "golden_blockout_vectors.json").read_text(encoding="utf-8"))
V, STATES = DATA["vectors"], DATA["states"]
BASE = STATES["base"]


def _n(x):
    return json.loads(json.dumps(x))


def test_texts_equal_golden():
    assert BLOCKOUT_TEXTS == json.loads((GOLDEN / "golden_blockout_texts.json").read_text(encoding="utf-8"))


def test_tree_and_order():
    for c in V["tree_rows"]:
        assert _n(bm.tree_rows(c["in"][0])) == c["out"]
    for c in V["ordered_keys"]:
        assert bm.ordered_keys(c["in"][0]) == c["out"]


def test_select_and_prune():
    ordered = bm.ordered_keys(BASE)
    for c in V["select"]:
        sel, key, mods = c["in"]
        assert bm.select(sel, ordered, key, mods) == c["out"]
    for c in V["prune_selection"]:
        sel, keys = c["in"]
        assert bm.prune_selection(sel, keys) == c["out"]


def test_lines():
    for c in V["summary_line"]:
        assert bm.summary_line(c["in"][0]) == c["out"]
    for c in V["tools_line"]:
        assert bm.tools_line(c["in"][0]) == c["out"]


def test_screen_state():
    for c in V["screen_state"]:
        state, error, reason = c["in"]
        assert bm.screen_state(state, error, reason) == c["out"]


def test_action_buttons():
    for c in V["action_buttons"]:
        name, keys, ctx = c["in"]
        got = [{k: b[k] for k in ("action", "enabled", "hint")} for b in bm.action_buttons(STATES[name], keys, ctx)]
        assert got == c["out"]


def test_plan_target_and_dialog():
    for c in V["plan_target"]:
        assert bm.plan_target(c["in"][0], BASE, c["in"][1]) == c["out"]
    for c in V["plan_dialog"]:
        assert _n(bm.plan_dialog(*c["in"])) == c["out"]


def test_duration_view():
    for c in V["duration_view"]:
        assert _n(bm.duration_view(c["in"][0], c["in"][1])) == c["out"]
