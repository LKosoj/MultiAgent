"""Паритет экрана «Генерация»: тексты, общие векторы и возможности в React и Tk."""

import importlib
import json
import re
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import labels

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
TS_LIB = ROOT / "frontend/client/src/app/lib/storybook"
TS_GEN_UI = ROOT / "frontend/client/src/app/components/sections/storybook/generation"
TK_GEN = ROOT / "StoryBookManager/gui/generation"

GENERATION_CAPABILITIES = (
    "run_plan",
    "run_start",
    "run_status",
    "run_set_pause",
    "run_cancel",
    "run_mark_interrupted",
    "run_log_tail",
    "run_history",
    "run_steps_get",
    "run_settings_get",
    "run_settings_save",
    "provider_catalog",
    "style_catalog",
    "run_preflight_summary",
    "regenerate_preview",
    "regenerate_start",
)
VECTOR_NAMES = ("start_button", "rerun_view", "estimate_headline", "parse_log_line", "banner_for")


def _load():
    return json.loads((FIXTURES / "storybook_generation_parity.json").read_text(encoding="utf-8"))


def _react_blob() -> str:
    """Код экрана: хуки и поток запуска в lib/storybook, компоненты в generation/*.tsx."""
    names = ("useRunStatus.ts", "useRunSettings.ts", "useGenerationFlow.ts", "generationFlow.ts", "runs.ts")
    parts = [TS_LIB / n for n in names] + sorted(TS_GEN_UI.glob("*.tsx"))
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)


def _tk_blob() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TK_GEN.rglob("*.py")))


def _needs_tk_package() -> None:
    if not (TK_GEN / "__init__.py").exists():
        pytest.skip("пакет StoryBookManager/gui/generation ещё не создан")


def _has_capability(blob: str, cap) -> bool:
    return (
        f'"{cap.id}"' in blob
        or f"'{cap.id}'" in blob
        or re.search(rf"\b{cap.client_fn}\b", blob) is not None
        or re.search(rf"\b{cap.tk_handler}\b", blob) is not None
    )


def _gen_texts_from_ts() -> dict:
    source = (TS_LIB / "generationTexts.ts").read_text(encoding="utf-8")
    body = source.split("export const GEN_TEXTS = {", 1)[1].split("} as const;", 1)[0]
    out = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'  (\w+): "(.*)",', line)
        assert match, f"строка текста должна быть однострочным литералом: {line!r}"
        out[match.group(1)] = json.loads(f'"{match.group(2)}"')
    return out


def test_gen_texts_match_fixture():
    assert _gen_texts_from_ts() == _load()["texts"]


def test_step_status_words_match_server_labels():
    # слова статусов шагов совпадают с серверными: React и Tk не расходятся с журналом и «Обзором»
    assert _load()["step_status_labels"] == labels.STATUS_LABELS["step"]
    source = (TS_LIB / "generationTexts.ts").read_text(encoding="utf-8")
    for value in labels.STATUS_LABELS["step"].values():
        assert f'"{value}"' in source


def test_vectors_have_all_functions():
    data = _load()
    assert tuple(data["vectors"]) == VECTOR_NAMES
    assert set(data["start_button_base"]) >= {"loading", "isAdmin", "selection", "plan", "estimate"}
    for name, cases in data["vectors"].items():
        assert cases, name
        for case in cases:
            assert set(case) == {"in", "out"}, name


def test_ts_parity_test_reads_the_same_file():
    source = (TS_LIB / "__tests__/generationParity.test.ts").read_text(encoding="utf-8")
    assert "storybook_generation_parity.json" in source


def test_react_does_not_reuse_shots_texts_fixture():
    # тексты кнопки «Кадров» живут здесь, а не в запрещённой к правке фикстуре «Кадров»
    shots = json.loads((FIXTURES / "storybook_shots_texts.json").read_text(encoding="utf-8"))
    assert "open_in_generation" not in shots
    assert "open_in_generation" in _load()["texts"]


@pytest.mark.parametrize("cap_id", GENERATION_CAPABILITIES)
def test_capability_used_by_react_screen(cap_id):
    assert cap_id in caps.registry()
    assert _has_capability(_react_blob(), caps.registry()[cap_id]), cap_id


@pytest.mark.parametrize("cap_id", GENERATION_CAPABILITIES)
def test_capability_used_by_tk_screen(cap_id):
    _needs_tk_package()
    assert _has_capability(_tk_blob(), caps.registry()[cap_id]), cap_id


def _tk_call(name: str, args: list):
    """Вызов Tk-функции по имени вектора; нет модуля или функции - пропуск."""
    _needs_tk_package()
    try:
        module = importlib.import_module("StoryBookManager.gui.generation.model")
    except ImportError as exc:
        pytest.skip(f"модуль gui.generation.model не импортируется: {exc}")
    func = getattr(module, name, None)
    if func is None:
        pytest.skip(f"gui.generation.model.{name} ещё нет")
    return func(*args)


def _norm(value):
    if isinstance(value, tuple):
        return [_norm(v) for v in value]
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _pick(value, keys):
    if value is None:
        return None
    if not isinstance(value, dict):
        value = {k: getattr(value, k) for k in keys}
    return {k: value[k] for k in keys}


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_tk_matches_shared_vectors(name):
    data = _load()
    for case in data["vectors"][name]:
        args = case["in"]
        keys = None
        if name == "start_button":
            args = [{**data["start_button_base"], **case["in"][0]}, data["titles"]]
            keys = ("label", "enabled", "reason")
        elif name == "banner_for":
            args = [case["in"][0], data["titles"]]
            keys = ("kind", "text")
        elif name == "parse_log_line":
            keys = ("time", "level", "word", "text")
        got = _tk_call(name, args)
        got = _pick(got, keys) if keys else got
        assert _norm(got) == case["out"], (name, case["in"])
