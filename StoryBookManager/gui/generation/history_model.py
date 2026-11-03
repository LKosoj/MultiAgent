"""Таблица прошлых прогонов (без tkinter)."""

from typing import List

from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.generation.model import RUN_MODES, format_duration, mode_label, seconds_between


def format_started(value) -> str:
    """«2026-11-03 12:00» из ISO-времени; пусто - тире."""
    text = str(value or "")
    return text[:16].replace("T", " ") if text else "—"


def _duration(r) -> str:
    secs = seconds_between(r.get("started_at"), r.get("finished_at"))
    return format_duration(secs) if secs is not None else "—"


def history_rows(rows) -> List[dict]:
    out = []
    for r in rows or []:
        mode = r.get("mode")
        surface = r.get("surface")
        outcome = r.get("outcome")
        out.append({
            "run_id": r.get("run_id"),
            "started": format_started(r.get("started_at")),
            "duration": _duration(r),
            "mode": mode_label(mode) if mode in RUN_MODES else "—",
            "where": T.GEN_TEXTS["history_where_tk"] if surface == "tk"
            else T.GEN_TEXTS["history_where_react"] if surface == "react" else "—",
            "result": T.RUN_RESULT_LABELS.get(outcome, outcome) if outcome
            else (T.GEN_TEXTS["history_unfinished"] if r.get("unfinished") else "—"),
            "note": T.GEN_TEXTS["history_resumable"] if r.get("resumable") else None,
        })
    return out
