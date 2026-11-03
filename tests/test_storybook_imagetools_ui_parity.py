"""Паритет «зум, правка ИИ, иллюстрации» (R3): тексты, общие векторы и возможности в React и Tk.

Ядро и фикстуры проверяются всегда. Части про React и Tk пропускаются, пока их файлов нет.
"""

import importlib
import json
import re
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps, image_edit
from tests.helpers import imagetools_reference as ref

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/storybook_screens"
TS_LIB = ROOT / "frontend/client/src/app/lib/storybook"
TS_UI = ROOT / "frontend/client/src/app/components/sections/storybook"
TK_PKG = ROOT / "StoryBookManager/gui/imagetools"

IMAGE_CAPABILITIES = ("image_edit_preview", "image_edit_start", "media_inventory")
VECTOR_NAMES = (
    "zoom_step", "fit_scale", "zoom_percent", "pan_allowed", "edit_can_submit", "edit_button", "edit_error_text",
    "editable_extension", "gallery_groups", "gallery_filter", "file_info_lines",
)
TK_MODULES = ("zoom_model", "edit_model", "gallery_model")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


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


def _tk_function(name: str):
    if not (TK_PKG / "__init__.py").exists():
        pytest.skip("пакет StoryBookManager/gui/imagetools ещё не создан")
    for module_name in TK_MODULES:
        try:
            module = importlib.import_module(f"StoryBookManager.gui.imagetools.{module_name}")
        except ImportError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    pytest.skip(f"в StoryBookManager/gui/imagetools нет функции {name}")


# --- фикстуры и эталон (всегда) ---


def test_texts_golden_shape():
    texts = _golden("golden_imagetools_texts.json")
    assert texts and all(isinstance(k, str) and isinstance(v, str) and v for k, v in texts.items())
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
    assert not [k for k, v in texts.items() if emoji.search(v)]


def test_vectors_golden_shape():
    data = _golden("golden_imagetools_vectors.json")
    assert tuple(data["vectors"]) == VECTOR_NAMES
    for name, cases in data["vectors"].items():
        assert cases, name
        assert all(set(c) == {"in", "out"} and isinstance(c["in"], list) for c in cases), name


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_vectors_equal_reference(name):
    for case in _golden("golden_imagetools_vectors.json")["vectors"][name]:
        got = ref.FUNCS[name](*case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])


def test_edit_button_and_error_ids_exist_in_texts():
    texts = _golden("golden_imagetools_texts.json")
    data = _golden("golden_imagetools_vectors.json")["vectors"]
    for case in data["edit_button"]:
        assert case["out"]["note"] is None or case["out"]["note"] in texts
        template = texts["edit_go_paid"] if case["out"]["label"].startswith("Править за") else None
        if template:
            assert case["out"]["label"].startswith(template.split("{")[0])
    for case in data["edit_error_text"]:
        assert case["out"] in texts
    assert texts["edit_go_free"] == "Править" and texts["edit_go_unknown"] == "Править без оценки"


def test_edit_button_labels_use_nbsp():
    # деньги пишутся с неразрывным пробелом, как formatShotMoney и format_rub_amount
    assert ref.NBSP == "\u00a0"
    paid = [c["out"]["label"] for c in _golden("golden_imagetools_vectors.json")["vectors"]["edit_button"]
            if c["out"]["label"].startswith("Править за")]
    assert paid and all("\u00a0₽" in label and " ₽" not in label for label in paid)


def test_rub_boundaries_in_vectors():
    labels = {c["in"][0]: c["out"]["label"] for c in _golden("golden_imagetools_vectors.json")["vectors"]["edit_button"]
              if isinstance(c["in"][0], (int, float))}
    # «половина вверх», как Math.round
    assert labels[0.5] == "Править за 1\u00a0₽" and labels[2.5] == "Править за 3\u00a0₽"
    assert labels[0.004] == "Править за 0\u00a0₽" and labels[0.005] == "Править за 0,01\u00a0₽"


def test_tk_rub_rounding_is_half_up():
    texts = importlib.import_module("StoryBookManager.gui.shots.texts")
    nbsp = "\u00a0"
    assert texts.format_rub_int(0.5) == f"1{nbsp}₽" and texts.format_rub_int(2.5) == f"3{nbsp}₽"
    assert texts.format_rub_int(-2.5) == f"-2{nbsp}₽" and texts.format_rub_int(1.5) == f"2{nbsp}₽"
    assert texts.format_rub_exact(0.005) == f"0,01{nbsp}₽" and texts.format_rub_exact(0.004) == f"0{nbsp}₽"


def test_tk_money_matches_edit_button_vectors():
    texts = importlib.import_module("StoryBookManager.gui.shots.texts")
    for case in _golden("golden_imagetools_vectors.json")["vectors"]["edit_button"]:
        cost = case["in"][0]
        if isinstance(cost, (int, float)) and cost > 0:
            assert case["out"]["label"] == "Править за " + texts.format_rub_amount(cost), cost


def test_unknown_cost_confirmation_has_own_text():
    texts = _golden("golden_imagetools_texts.json")
    assert texts["edit_confirm_unknown"] == "Подтвердите правку без оценки цены."
    cases = {(c["in"][1], c["in"][2]): c["out"]
             for c in _golden("golden_imagetools_vectors.json")["vectors"]["edit_error_text"]}
    assert cases[("cost_confirmation_required", True)] == "edit_confirm_unknown"
    assert cases[("cost_confirmation_required", False)] == "edit_cost_changed"
    assert cases[("cost_changed", False)] == "edit_cost_changed"


def test_limits_match_server():
    assert ref.PROMPT_MIN == image_edit.PROMPT_MIN
    assert ref.REFERENCES_MAX == image_edit.REFERENCES_MAX
    assert tuple(ref.EDIT_EXTENSIONS) == tuple(image_edit.EXTENSIONS)
    texts = _golden("golden_imagetools_texts.json")
    assert str(ref.PROMPT_MIN) in texts["edit_prompt_hint"]
    assert str(ref.REFERENCES_MAX) in texts["edit_refs_label"] and str(ref.REFERENCES_MAX) in texts["edit_refs_count"]


def test_image_capabilities_registered():
    reg = caps.registry()
    for cap_id in IMAGE_CAPABILITIES:
        assert cap_id in reg


def test_removed_capabilities_stay_removed():
    reg = caps.registry()
    for gone in ("labels_get", "run_settings_validate", "cost_estimate", "reports_summary", "reports_findings"):
        assert gone not in reg


# --- React (пропуск, пока файлов нет) ---


def test_react_texts_match_golden():
    if not (TS_LIB / "imageToolsTexts.ts").exists():
        pytest.skip("React imageToolsTexts.ts ещё нет")
    assert _texts_from_ts("imageToolsTexts.ts", "IMAGE_TEXTS") == _golden("golden_imagetools_texts.json")


def test_react_parity_test_reads_golden_files():
    path = TS_LIB / "__tests__/imageToolsParity.test.ts"
    if not path.exists():
        pytest.skip("React imageToolsParity.test.ts ещё нет")
    source = path.read_text(encoding="utf-8")
    assert "golden_imagetools_texts.json" in source and "golden_imagetools_vectors.json" in source
    for name in VECTOR_NAMES:
        assert name in source, name


@pytest.mark.parametrize("cap_id", ("image_edit_preview", "image_edit_start"))
def test_react_calls_capability(cap_id):
    files = sorted(TS_LIB.glob("imageEdit*.ts")) + sorted((TS_UI / "imagetools").glob("*.tsx"))
    if not files:
        pytest.skip("React-код правки картинки ещё не написан")
    blob = "\n".join(p.read_text(encoding="utf-8") for p in files)
    cap = caps.registry()[cap_id]
    assert f'"{cap_id}"' in blob or re.search(rf"\b{cap.client_fn}\b", blob)


# --- Tk (пропуск, пока пакета нет) ---


def test_tk_texts_match_golden():
    if not (TK_PKG / "texts.py").exists():
        pytest.skip("Tk gui/imagetools/texts.py ещё нет")
    texts = importlib.import_module("StoryBookManager.gui.imagetools.texts")
    assert dict(texts.IMAGE_TEXTS) == _golden("golden_imagetools_texts.json")


@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_tk_matches_vectors(name):
    func = _tk_function(name)
    for case in _golden("golden_imagetools_vectors.json")["vectors"][name]:
        got = func(*case["in"])
        assert json.loads(json.dumps(got)) == case["out"], (name, case["in"])


def test_tk_calls_capabilities():
    gui = ROOT / "StoryBookManager/gui"
    blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in gui.rglob("*.py"))
    for cap_id in IMAGE_CAPABILITIES:
        cap = caps.registry()[cap_id]
        assert (f'"{cap_id}"' in blob or re.search(rf"\b{cap.tk_handler}\b", blob)
                or re.search(rf"\bCAP_{cap_id.upper()}\b", blob)), cap_id
