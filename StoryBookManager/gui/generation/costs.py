"""Оценка стоимости для экрана «Генерация»: заголовок, разбивка, вердикт бюджета (без tkinter)."""

from typing import List, Optional

from StoryBookManager.gui.generation.texts import fmt
from StoryBookManager.gui.shots.texts import format_rub_amount

COMPONENT_ORDER = ("video", "images", "music", "sfx", "text_llm")
COMPONENT_WORDS = {"video": "видео", "images": "картинок", "music": "музыки", "sfx": "эффектов", "text_llm": "текстов"}
COMPONENT_ROW = {"video": "comp_video", "images": "comp_images", "music": "comp_music",
                 "sfx": "comp_sfx", "text_llm": "comp_text_llm"}


def is_cost_estimate(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("total_known_rub"), (int, float))
            and isinstance(value.get("components"), list))


def estimate_total(estimate: Optional[dict]) -> float:
    total = (estimate or {}).get("total_known_rub")
    return float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else 0.0


def estimate_headline(estimate: dict) -> str:
    """«Оценка: 1 180 ₽», «Оценка: не менее 200 ₽ (цена музыки, эффектов неизвестна)» или «цена неизвестна»."""
    total = estimate_total(estimate)
    if estimate.get("complete"):
        return fmt("estimate_label", cost=format_rub_amount(total))
    if total > 0:
        parts = ", ".join(COMPONENT_WORDS.get(c, c) for c in estimate.get("unknown_components") or [])
        return fmt("estimate_not_less", cost=format_rub_amount(total), parts=parts)
    return fmt("estimate_unknown")


def cost_rows(estimate: dict) -> List[dict]:
    """Строки разбивки по порядку; basis=rough помечается «ориентировочно» (решение В12)."""
    by_id = {c.get("id"): c for c in estimate.get("components") or [] if isinstance(c, dict)}
    rows = []
    for ident in COMPONENT_ORDER:
        comp = by_id.get(ident)
        if comp is None:
            continue
        amount = comp.get("amount_rub")
        known = comp.get("basis") != "unknown" and isinstance(amount, (int, float)) and not isinstance(amount, bool)
        rows.append({"id": ident, "title": fmt(COMPONENT_ROW[ident]),
                     "amount": format_rub_amount(amount) if known else "—",
                     "rough": comp.get("basis") == "rough"})
    return rows


def budget_line(estimate: dict) -> dict:
    """Строка вердикта бюджета; tone нужен для цвета, смысл несёт текст."""
    verdict = estimate.get("budget_verdict")
    if verdict == "enough":
        return {"text": fmt("budget_enough"), "tone": "ok"}
    if verdict == "short":
        return {"text": fmt("budget_short", sum=format_rub_amount(estimate.get("short_by_rub") or 0)), "tone": "warn"}
    if verdict == "cannot_check":
        return {"text": fmt("budget_cannot"), "tone": "muted"}
    return {"text": fmt("budget_none"), "tone": "muted"}
