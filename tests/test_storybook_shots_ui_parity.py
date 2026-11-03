"""Паритет экрана «Кадры»: тексты, общие векторы, возможности и пути болванки в React и Tk."""

import importlib
import json
import re
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_preview
from custom_tools.storybook.studio import capabilities as caps

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
TS_LIB = ROOT / "frontend/client/src/app/lib/storybook"
TS_SHOTS_UI = ROOT / "frontend/client/src/app/components/sections/storybook/shots"
TK_SHOTS = ROOT / "StoryBookManager/gui/shots"

SHOT_CAPABILITIES = (
    "shots_list",
    "shots_keys",
    "shot_get",
    "shot_update",
    "shots_adopt_baseline",
    "review_get",
    "review_set_status",
    "review_set_note",
    "versions_list",
    "versions_compare",
    "versions_restore",
    "regenerate_preview",
    "regenerate_start",
    "reports_for_shot",
)
VECTOR_NAMES = (
    "filter_from_intent",
    "next_status_on_key",
    "plural_shots",
    "default_what",
    "regen_button",
    "format_rub_exact",
    "neighbor_key",
    "bulk_target_keys",
    "error_text",
    "dirty_changes",
    "layout_rows",
    "visible_range",
    "left_column_width",
    "columns_for_width",
)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _react_blob() -> str:
    """Код экрана: хуки useShot*/useVersions/useRegeneration и компоненты shots/*.tsx."""
    names = ("useShot*.ts", "useVersions.ts", "useRegeneration.ts")
    parts = [p for pattern in names for p in sorted(TS_LIB.glob(pattern))]
    parts += sorted(TS_SHOTS_UI.glob("*.tsx"))
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)


def _tk_blob() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TK_SHOTS.rglob("*.py")))


def _needs_tk_package() -> None:
    if not (TK_SHOTS / "__init__.py").exists():
        pytest.skip("пакет StoryBookManager/gui/shots ещё не создан")


def _needs_tk_screen() -> None:
    # возможности вызывает адаптер экрана; пока его нет, сверять нечего
    _needs_tk_package()
    if not (TK_SHOTS / "adapter.py").exists():
        pytest.skip("адаптер экрана gui/shots/adapter.py ещё не создан")


def _has_capability(blob: str, cap) -> bool:
    return (
        f'"{cap.id}"' in blob
        or f"'{cap.id}'" in blob
        or re.search(rf"\b{cap.client_fn}\b", blob) is not None
        or re.search(rf"\b{cap.tk_handler}\b", blob) is not None
    )


def _shot_texts_from_ts() -> dict:
    source = (TS_LIB / "shotsTexts.ts").read_text(encoding="utf-8")
    body = source.split("export const SHOT_TEXTS = {", 1)[1].split("} as const;", 1)[0]
    out = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'  (\w+): "(.*)",', line)
        assert match, f"строка текста должна быть однострочным литералом: {line!r}"
        out[match.group(1)] = json.loads(f'"{match.group(2)}"')
    return out


def test_shot_texts_match_fixture():
    assert _shot_texts_from_ts() == _load("storybook_shots_texts.json")


def test_vectors_file_has_all_functions():
    vectors = _load("storybook_shots_vectors.json")
    assert tuple(vectors) == VECTOR_NAMES
    for name, cases in vectors.items():
        assert cases, name
        for case in cases:
            assert set(case) == {"in", "out"}, name


def test_ts_vectors_test_reads_the_same_file():
    source = (TS_LIB / "__tests__/shotsVectors.test.ts").read_text(encoding="utf-8")
    assert "storybook_shots_vectors.json" in source


@pytest.mark.parametrize("cap_id", SHOT_CAPABILITIES)
def test_capability_used_by_react_screen(cap_id):
    assert cap_id in caps.registry()
    assert _has_capability(_react_blob(), caps.registry()[cap_id]), cap_id


def test_media_token_is_react_only():
    cap = caps.registry()["media_token"]
    # токен медиа нужен только браузеру: Tk читает файлы с диска напрямую
    assert f'"{cap.id}"' in (TS_LIB / "client.ts").read_text(encoding="utf-8")
    assert re.search(r"ensureMediaToken", (TS_LIB / "useShotMedia.ts").read_text(encoding="utf-8"))
    if TK_SHOTS.exists():
        assert not _has_capability(_tk_blob(), cap)


@pytest.mark.parametrize("cap_id", SHOT_CAPABILITIES)
def test_capability_used_by_tk_screen(cap_id):
    _needs_tk_screen()
    assert _has_capability(_tk_blob(), caps.registry()[cap_id]), cap_id


def _tk_call(name: str, args: list):
    """Вызов Tk-функции по имени вектора; нет модуля или функции - пропуск."""
    _needs_tk_package()
    modules = {
        "plural_shots": ("texts", "plural_shots"),
        "format_rub_exact": ("texts", "format_rub_exact"),
        "error_text": ("texts", "error_text"),
        "layout_rows": ("layout", "layout_rows"),
        "visible_range": ("layout", "visible_range"),
        "left_column_width": ("layout", "left_column_width"),
        "columns_for_width": ("layout", "columns_for_width"),
    }
    module_name, func_name = modules.get(name, ("model", name))
    try:
        module = importlib.import_module(f"StoryBookManager.gui.shots.{module_name}")
    except ImportError as exc:
        pytest.skip(f"модуль gui.shots.{module_name} не импортируется: {exc}")
    func = getattr(module, func_name, None)
    if func is None:
        pytest.skip(f"gui.shots.{module_name}.{func_name} ещё нет")
    return func(*args)


def _norm(value):
    if isinstance(value, tuple):
        return [_norm(v) for v in value]
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_tk_matches_shared_vectors(name):
    for case in _load("storybook_shots_vectors.json")[name]:
        assert _norm(_tk_call(name, case["in"])) == case["out"], (name, case["in"])


def test_blockout_path_convention():
    # React собирает путь по соглашению об именах: он должен совпадать с тем, что пишет болванка
    ts = (TS_LIB / "shotsView.ts").read_text(encoding="utf-8")
    assert "93_blockout/scene_${pad(scene)}_shot_${pad(shot)}/" in ts
    assert blockout_preview.shot_dir_name(2, 3) == "scene_02_shot_03"
    writer = Path(blockout_preview.__file__).read_text(encoding="utf-8")
    for part, filename in (("start", "ref_start.png"), ("end", "ref_end.png"), ("video", "blockout_ref.mp4")):
        assert f'"{filename}"' in ts, part
        if part != "end":
            assert filename in writer, part


def test_tk_blockout_path_matches_react():
    _needs_tk_package()
    model = importlib.import_module("StoryBookManager.gui.shots.model")
    shot_dir = blockout_preview.shot_dir_name(2, 3)
    for part, filename in (("start", "ref_start.png"), ("end", "ref_end.png"), ("video", "blockout_ref.mp4")):
        assert model.blockout_rel(2, 3, part) == f"93_blockout/{shot_dir}/{filename}"
