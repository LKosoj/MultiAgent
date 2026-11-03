"""Чистая логика галереи иллюстраций «50_images» (без tkinter)."""

import re
from typing import List, Optional

from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS, fmt


def _natural(name: str) -> list:
    return [(0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk) for chunk in re.split(r"(\d+)", name.lower())
            if chunk != ""]


def _page(item: dict) -> Optional[int]:
    page = item.get("page")
    return page if isinstance(page, int) and not isinstance(page, bool) else None


def gallery_groups(items: List[dict]) -> List[dict]:
    """Группы по номеру страницы по возрастанию, без номера в конце; внутри естественный порядок имён."""
    pages: dict = {}
    for item in items:
        pages.setdefault(_page(item), []).append(item["name"])
    order = sorted(p for p in pages if p is not None) + ([None] if None in pages else [])
    return [{"title": fmt("gallery_page", n=p) if p is not None else IMAGE_TEXTS["gallery_page_other"],
             "page": p, "items": sorted(pages[p], key=_natural)} for p in order]


def gallery_filter(items: List[dict], query) -> List[str]:
    needle = query.strip().lower() if isinstance(query, str) else ""
    return [i["name"] for i in items if needle in i["name"].lower()]


def file_info_lines(name, size_text, modified_text, w, h) -> List[str]:
    """Строки свойств; размер и дату вызывающий форматирует сам. Размеры в пикселях - если известны."""
    lines = [fmt("info_name", v=name), fmt("info_size", v=size_text), fmt("info_modified", v=modified_text)]
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (w, h)):
        lines.append(fmt("info_dimensions", w=int(w), h=int(h)))
    return lines


def filtered_groups(items: List[dict], query) -> List[dict]:
    """Группы после поиска: в каждой остаются только подходящие файлы; пустые группы убираются.

    Строка списка однозначно задаётся путём файла: одинаковые имена на разных страницах возможны.
    """
    needle = query.strip().lower() if isinstance(query, str) else ""
    pages: dict = {}
    for item in items:
        if needle in item["name"].lower():
            pages.setdefault(_page(item), []).append(item)
    order = sorted(p for p in pages if p is not None) + ([None] if None in pages else [])
    return [{"title": fmt("gallery_page", n=p) if p is not None else IMAGE_TEXTS["gallery_page_other"],
             "page": p, "items": sorted(pages[p], key=lambda i: _natural(i["name"]))} for p in order]
