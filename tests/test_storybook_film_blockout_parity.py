"""Паритет экранов «Фильм» и «Болванка»: тексты, золотые файлы и возможности в React и Tk."""

import importlib
import json
import re
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/storybook_screens"
TS_LIB = ROOT / "frontend/client/src/app/lib/storybook"
TS_UI = ROOT / "frontend/client/src/app/components/sections/storybook"
TK_SCREENS = ROOT / "StoryBookManager/gui/screens"

FILM_CAPABILITIES = ("media_timeline", "media_subtitles", "regenerate_preview", "regenerate_start", "versions_list")
BLOCKOUT_CAPABILITIES = (
    "blockout_state", "blockout_detail", "blockout_plan", "blockout_run", "blockout_set_duration", "blockout_leave_as_is",
)
FILM_VECTORS = (
    "tick_step", "fit_scale", "clamp_scale", "zoom_in", "zoom_out", "ruler_ticks", "cue_at", "block_at", "merge_narrow",
    "resolve_state", "run_transition", "banners", "music_rows", "file_rows", "default_source", "format_position",
    "clamp_time", "time_from_x", "key_action",
)
BLOCKOUT_VECTORS = (
    "tree_rows", "ordered_keys", "select", "prune_selection", "summary_line", "tools_line", "screen_state",
    "action_buttons", "plan_target", "plan_dialog", "duration_view",
)


def _golden(name: str):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def _texts_from_ts(file: str, const: str) -> dict:
    source = (TS_LIB / file).read_text(encoding="utf-8")
    body = source.split(f"export const {const} = {{", 1)[1].split("} as const;", 1)[0]
    out = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'  (\w+): "(.*)",', line)
        assert match, f"строка текста должна быть однострочным литералом: {line!r}"
        out[match.group(1)] = json.loads(f'"{match.group(2)}"')
    return out


def _react_blob(subdir: str, lib_files: tuple) -> str:
    parts = [TS_LIB / n for n in lib_files] + sorted((TS_UI / subdir).glob("*.tsx"))
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)


def _has_capability(blob: str, cap) -> bool:
    return (
        f'"{cap.id}"' in blob
        or re.search(rf"\b{cap.client_fn}\b", blob) is not None
        or re.search(rf"\b{cap.tk_handler}\b", blob) is not None
    )


def test_film_texts_match_golden():
    assert _texts_from_ts("filmTexts.ts", "FILM_TEXTS") == _golden("golden_film_texts.json")


def test_blockout_texts_match_golden():
    assert _texts_from_ts("blockoutTexts.ts", "BLOCKOUT_TEXTS") == _golden("golden_blockout_texts.json")


def test_no_emoji_in_texts():
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
    for name in ("golden_film_texts.json", "golden_blockout_texts.json"):
        for key, value in _golden(name).items():
            assert not emoji.search(value), (name, key)


def test_film_golden_shape():
    data = _golden("golden_film_vectors.json")
    assert tuple(data["vectors"]) == FILM_VECTORS
    for name, cases in data["vectors"].items():
        assert cases, name
        assert all(set(c) == {"in", "out"} for c in cases), name
    layout = _golden("golden_film_layout.json")
    assert len(layout["cases"]) >= 2
    for case in layout["cases"]:
        assert set(case) == {"name", "timeline", "cues", "layout"}
        assert set(case["layout"]) == {"blocks", "duration_s", "out_of_cut"}


def test_blockout_golden_shape():
    data = _golden("golden_blockout_vectors.json")
    assert tuple(data["vectors"]) == BLOCKOUT_VECTORS
    assert {"base", "no_blender", "blocked", "busy_state"} <= set(data["states"])
    for name, cases in data["vectors"].items():
        assert cases, name
        assert all(set(c) == {"in", "out"} for c in cases), name


def test_select_vectors_carry_focus():
    for case in _golden("golden_blockout_vectors.json")["vectors"]["select"]:
        assert set(case["in"][0]) == {"keys", "anchor", "focus"}
        assert set(case["out"]) == {"keys", "anchor", "focus"}
        assert case["out"]["focus"] in (None, *case["out"]["keys"])


def test_golden_inputs_use_real_server_codes():
    # входы golden берём из настоящих ответов сервера: выдуманные коды закрепили бы ошибку
    server = (ROOT / "custom_tools/storybook/studio/blockout.py").read_text(encoding="utf-8")
    timeline_src = (ROOT / "custom_tools/storybook/studio/media_timeline.py").read_text(encoding="utf-8")
    for case in _golden("golden_blockout_vectors.json")["vectors"]["duration_view"]:
        code = case["in"][0].get("reason")
        if code and code != "future_code":
            assert f'"{code}"' in server, code
    for case in _golden("golden_film_layout.json")["cases"]:
        for shot in case["timeline"]["shots"]:
            for code in shot["warnings"]:
                if code != "future_code":
                    assert f'"{code}"' in timeline_src, code


def test_ts_parity_test_reads_golden_files():
    source = (TS_LIB / "__tests__/filmBlockoutParity.test.ts").read_text(encoding="utf-8")
    for name in ("golden_film_texts.json", "golden_film_layout.json", "golden_film_vectors.json",
                 "golden_blockout_texts.json", "golden_blockout_vectors.json"):
        assert name in source


@pytest.mark.parametrize("cap_id", FILM_CAPABILITIES)
def test_capability_used_by_react_film(cap_id):
    blob = _react_blob("film", ("media.ts", "filmLoader.ts", "filmRegen.ts", "useFilm.ts"))
    assert _has_capability(blob, caps.registry()[cap_id]), cap_id


@pytest.mark.parametrize("cap_id", BLOCKOUT_CAPABILITIES)
def test_capability_used_by_react_blockout(cap_id):
    blob = _react_blob("blockout", ("blockout.ts", "blockoutFlow.ts", "useBlockout.ts"))
    assert _has_capability(blob, caps.registry()[cap_id]), cap_id


def _tk_call(module_name: str, name: str, args: list):
    if not (TK_SCREENS / "__init__.py").exists():
        pytest.skip("пакет StoryBookManager/gui/screens ещё не создан")
    try:
        module = importlib.import_module(f"StoryBookManager.gui.screens.{module_name}")
    except ImportError as exc:
        pytest.skip(f"модуль screens.{module_name} не импортируется: {exc}")
    func = getattr(module, name, None)
    if func is None:
        pytest.skip(f"screens.{module_name}.{name} ещё нет")
    return func(*args)


@pytest.mark.parametrize("name", ("tick_step", "fit_scale", "clamp_scale", "zoom_in", "zoom_out", "format_position",
                                   "clamp_time", "time_from_x", "run_transition"))
def test_tk_film_matches_vectors(name):
    for case in _golden("golden_film_vectors.json")["vectors"][name]:
        got = _tk_call("film_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])


@pytest.mark.parametrize("name", ("summary_line", "tools_line"))
def test_tk_blockout_matches_vectors(name):
    for case in _golden("golden_blockout_vectors.json")["vectors"][name]:
        got = _tk_call("blockout_model", name, case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])
