"""Шаги прогона: названия из каталога, статусы из run_status (без tkinter)."""

from datetime import datetime
from typing import List, Optional

from StoryBookManager.gui.generation.model import format_duration, title_of
from StoryBookManager.gui.generation.texts import step_status_word

ERROR_LIMIT = 300


def _elapsed(row: dict, now: datetime) -> Optional[float]:
    if isinstance(row.get("duration_s"), (int, float)):
        return row["duration_s"]
    if row.get("status") != "running" or not row.get("started_at"):
        return None
    try:
        started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    delta = (now - started).total_seconds()
    return round(delta) if delta >= 0 else None


def step_rows(steps, titles, now: Optional[datetime] = None) -> List[dict]:
    """Строки в порядке плана; текущий - идущий, иначе упавший."""
    now = now or datetime.now()
    steps = list(steps or [])
    current = next((i for i, s in enumerate(steps) if s.get("status") == "running"), -1)
    if current < 0:
        current = next((i for i, s in enumerate(steps) if s.get("status") == "failed"), -1)
    out = []
    for i, s in enumerate(steps):
        secs = _elapsed(s, now)
        out.append({"id": s.get("id"), "index": i + 1, "title": title_of(titles, s.get("id")),
                    "status": s.get("status"), "word": step_status_word(s.get("status")),
                    "duration": None if secs is None else format_duration(secs),
                    "error": (s.get("error") or "")[:ERROR_LIMIT] or None, "current": i == current})
    return out


def catalog_rows(catalog) -> List[dict]:
    """Каталог шагов до первого прогона: все «Ожидает»."""
    return step_rows([{"id": s.get("id"), "status": "pending"} for s in catalog or []],
                     {s.get("id"): s.get("title") for s in catalog or []})


def next_follow(state: dict, current_id: Optional[str], user_scrolled: bool) -> dict:
    """Автопрокрутка к текущему шагу выключается, когда пользователь прокрутил список сам."""
    if current_id != state.get("last_current"):
        return {"follow": True, "last_current": current_id}
    return {"follow": False, "last_current": current_id} if user_scrolled else state
