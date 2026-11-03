"""Чистая логика правки картинки ИИ (без tkinter): кнопка, ошибки, состав запроса."""

import math
import re
from typing import List

from StoryBookManager.gui.shots.texts import format_rub_amount

PROMPT_MIN = 5
REFERENCES_MAX = 3
EDIT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def edit_can_submit(prompt, refs_n, estimate_ready) -> bool:
    text = prompt.strip() if isinstance(prompt, str) else ""
    return (len(text) >= PROMPT_MIN and isinstance(refs_n, int) and 0 <= refs_n <= REFERENCES_MAX
            and estimate_ready is True)


def edit_button(cost_rub, unknown_cost) -> dict:
    """Подпись кнопки запуска правки и id пояснения под ней."""
    known = isinstance(cost_rub, (int, float)) and not isinstance(cost_rub, bool) and math.isfinite(cost_rub)
    if known and cost_rub > 0:
        return {"label": f"Править за {format_rub_amount(cost_rub)}", "note": None}
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


def _natural(text: str) -> list:
    return [(0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk) for chunk in re.split(r"(\d+)", text.lower())
            if chunk != ""]


def reference_choices(items: List[dict], exclude_path: str) -> List[dict]:
    """Образцы из media_inventory как в React (referenceCandidates): картинки, которые можно показать
    (servable), без правимой, подпись «папка/имя», порядок по подписи с числами по величине."""
    out = []
    for item in items:
        path = item.get("path")
        if not path or path == exclude_path or not item.get("servable") or not editable_extension(item.get("name")):
            continue
        parts = str(path).replace("\\", "/").split("/")
        out.append({"path": path, "label": "/".join(parts[-2:])})
    return sorted(out, key=lambda c: _natural(c["label"]))


def target_payload(target: dict) -> dict:
    """Цель правки в полях запроса: шот+часть или путь иллюстрации."""
    if target.get("shot_key"):
        return {"shot_key": target["shot_key"], "part": target["part"]}
    return {"path": target["path"]}


def start_payload(target: dict, prompt: str, refs: List[str], preview: dict, surface: str = "tk") -> dict:
    """Запрос на запуск: сумма подтверждается ровно той, что показана в предпросмотре."""
    estimate = preview.get("estimate") or {}
    payload = {**target_payload(target), "prompt": prompt.strip(), "expected_plan_digest": preview.get("plan_digest", ""),
               "surface": surface}
    if refs:
        payload["reference_paths"] = list(refs)
    if estimate.get("unknown_cost"):
        payload["confirm_unknown_cost"] = True
    elif (estimate.get("cost_rub") or 0) > 0:
        payload["confirm_cost_rub"] = float(estimate["cost_rub"])
    return payload
