"""Раскладка виртуального списка шотов (без tkinter).

Алгоритм тот же, что в lib/storybook/shotsWindow.ts; обе версии сверяются общими
векторами (tests/fixtures/storybook_shots_vectors.json).
"""

from typing import Dict, Iterable, List, Sequence, Tuple, Union

HEADER_H = 32
LIST_ROW_H = 64
GRID_ROW_H = 120
GRID_CELL_W = 132
GRID_PAD_W = 16  # полоса прокрутки и отступы

Item = Union[str, dict]


def scene_of(item: Item) -> int:
    """Номер сцены: из поля scene либо из ключа «S-N»."""
    if isinstance(item, dict):
        if isinstance(item.get("scene"), int):
            return item["scene"]
        key = str(item.get("shot_key") or "")
    else:
        key = str(item)
    head = key.split("-", 1)[0]
    return int(head) if head.isdigit() else 0


def key_of(item: Item) -> str:
    return str(item.get("shot_key")) if isinstance(item, dict) else str(item)


def layout_rows(items: Iterable[Item], mode: str, columns: int) -> Dict[str, object]:
    """Плоский список строк: заголовок сцены, затем строки списка или сетки.

    Возвращает {"rows": [...], "total": высота}. Строка - словарь:
    header (scene), row или grid (keys), у всех top и height.
    """
    cols = max(1, int(columns)) if mode == "grid" else 1
    row_h = GRID_ROW_H if mode == "grid" else LIST_ROW_H
    kind = "grid" if mode == "grid" else "row"
    rows: List[dict] = []
    top = 0
    scene = None
    pending: List[str] = []

    def flush():
        nonlocal top, pending
        if pending:
            rows.append({"kind": kind, "keys": pending, "top": top, "height": row_h})
            top += row_h
            pending = []

    for item in items:
        item_scene = scene_of(item)
        if item_scene != scene:
            flush()
            scene = item_scene
            rows.append({"kind": "header", "scene": item_scene, "top": top, "height": HEADER_H})
            top += HEADER_H
        pending.append(key_of(item))
        if len(pending) == cols:
            flush()
    flush()
    return {"rows": rows, "total": top}


def visible_range(rows: Sequence[dict], scroll_top: int, viewport_h: int, overscan: int = 0) -> Tuple[int, int]:
    """Индексы первой и последней строки в окне прокрутки с запасом overscan (px).

    Пусто: (0, -1). Прокрутка за конец даёт последнюю строку.
    """
    n = len(rows)
    if n == 0:
        return (0, -1)
    low = max(0, scroll_top - overscan)
    high = scroll_top + viewport_h + overscan
    first = 0
    while first < n and rows[first]["top"] + rows[first]["height"] <= low:
        first += 1
    if first >= n:
        return (n - 1, n - 1)
    last = first
    while last + 1 < n and rows[last + 1]["top"] < high:
        last += 1
    return (first, last)


def row_index_of_key(rows: Sequence[dict], key: str) -> int:
    for index, row in enumerate(rows):
        if row["kind"] != "header" and key in row["keys"]:
            return index
    return -1


def columns_for_width(width: int) -> int:
    """Сколько ячеек сетки помещается в колонку данной ширины (не меньше одной)."""
    return max(1, (int(width) - GRID_PAD_W) // GRID_CELL_W)


def left_column_width(window_w: int) -> int:
    """Ширина левой колонки по ширине окна (пороги одинаковы с React)."""
    if window_w < 1100:
        return 280
    return 360 if window_w < 1500 else 440
