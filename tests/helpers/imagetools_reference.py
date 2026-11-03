"""Эталон общей логики зума, правки и галереи иллюстраций (R3).

React и Tk обязаны давать те же ответы, что эти функции: векторы в
tests/fixtures/storybook_screens/golden_imagetools_vectors.json получены отсюда, а тест
сверяет файл с эталоном. Деньги форматируются так же, как formatShotMoney / format_rub_amount.
"""

from __future__ import annotations

import math
import re

ZOOM_STEP = 1.25
ZOOM_MIN = 0.1
ZOOM_MAX = 8.0
FIT_MARGIN = 0.95
PROMPT_MIN = 5
REFERENCES_MAX = 3
EDIT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
NBSP = "\u00a0"


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(scale: float) -> float:
    return min(ZOOM_MAX, max(ZOOM_MIN, scale))


def round4(value: float) -> float:
    return math.floor(value * 10000 + 0.5) / 10000


def zoom_step(scale, direction: str) -> float:
    """Шаг 1,25 с пределами 0,1..8; не число или не больше нуля считается масштабом 1."""
    base = scale if _num(scale) and scale > 0 else 1.0
    return round4(_clamp(base * ZOOM_STEP if direction == "in" else base / ZOOM_STEP))


def fit_scale(view_w, view_h, img_w, img_h) -> float:
    """По размеру окна с запасом 0,95; нулевые или неизвестные размеры дают 1."""
    if not all(_num(v) and v > 0 for v in (view_w, view_h, img_w, img_h)):
        return 1.0
    return round4(_clamp(min(view_w / img_w, view_h / img_h) * FIT_MARGIN))


def zoom_percent(scale) -> str:
    value = scale if _num(scale) else 1.0
    return f"{math.floor(value * 100 + 0.5)}%"


def pan_allowed(scale, view, img) -> bool:
    """Двигать картинку можно, только когда она больше окна хотя бы по одной стороне."""
    if not (_num(scale) and scale > 0):
        return False
    (vw, vh), (iw, ih) = view, img
    if not all(_num(v) and v > 0 for v in (vw, vh, iw, ih)):
        return False
    return iw * scale > vw + 1e-9 or ih * scale > vh + 1e-9


def edit_can_submit(prompt, refs_n, estimate_ready) -> bool:
    text = prompt.strip() if isinstance(prompt, str) else ""
    return (len(text) >= PROMPT_MIN and isinstance(refs_n, int) and 0 <= refs_n <= REFERENCES_MAX
            and estimate_ready is True)


def _rub(amount: float) -> str:
    if 0 < amount < 0.5:  # как formatShotMoney: копейки, чтобы не писать «0 ₽»
        whole, rest = divmod(int(math.floor(amount * 100 + 0.5)), 100)
        tail = f",{rest:02d}" if rest else ""
        return f"{whole}{tail}{NBSP}₽"
    rounded = int(math.floor(abs(amount) + 0.5))
    digits = re.sub(r"\B(?=(\d{3})+(?!\d))", NBSP, str(rounded))
    return f"{digits}{NBSP}₽"


def edit_button(cost_rub, unknown_cost) -> dict:
    """Подпись кнопки запуска правки и id пояснения под ней."""
    if _num(cost_rub) and cost_rub > 0:
        return {"label": f"Править за {_rub(cost_rub)}", "note": None}
    if unknown_cost:
        return {"label": "Править без оценки", "note": "edit_cost_unknown"}
    return {"label": "Править", "note": None}


def edit_error_text(code, reason, unknown_cost=False) -> str:
    """id текста для ошибки image_edit_start (code, details.reason, details.unknown_cost)."""
    if reason == "plan_changed":
        return "edit_plan_changed"
    if reason == "cost_confirmation_required" and unknown_cost is True:
        return "edit_confirm_unknown"
    if reason in ("cost_changed", "cost_confirmation_required"):
        return "edit_cost_changed"
    if code == "locked" or reason in ("regeneration_pending", "edit_running"):
        return "edit_busy"
    return "edit_failed"


def editable_extension(name) -> bool:
    if not isinstance(name, str):
        return False
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in EDIT_EXTENSIONS


def _natural(name: str):
    return [(0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk) for chunk in re.split(r"(\d+)", name.lower())
            if chunk != ""]


def gallery_groups(items) -> list:
    """Группы по номеру страницы по возрастанию, без номера в конце; внутри естественный порядок имён."""
    pages: dict = {}
    for item in items:
        page = item.get("page")
        pages.setdefault(page if isinstance(page, int) and not isinstance(page, bool) else None, []).append(item["name"])
    order = sorted(p for p in pages if p is not None) + ([None] if None in pages else [])
    return [{"title": f"Страница {p}" if p is not None else "Без номера страницы", "page": p,
             "items": sorted(pages[p], key=_natural)} for p in order]


def gallery_filter(items, query) -> list:
    needle = query.strip().lower() if isinstance(query, str) else ""
    return [i["name"] for i in items if needle in i["name"].lower()]


def file_info_lines(name, size_text, modified_text, w, h) -> list:
    """Строки свойств; размер и дату клиент форматирует своими форматтерами. Размеры в пикселях - если известны."""
    lines = [f"Файл: {name}", f"Размер: {size_text}", f"Изменён: {modified_text}"]
    if _num(w) and _num(h) and w > 0 and h > 0:
        lines.append(f"Размеры: {int(w)} × {int(h)}")
    return lines


FUNCS = {f.__name__: f for f in (
    zoom_step, fit_scale, zoom_percent, pan_allowed, edit_can_submit, edit_button, edit_error_text,
    editable_extension, gallery_groups, gallery_filter, file_info_lines)}
