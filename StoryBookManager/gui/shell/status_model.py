"""Общий разбор ответа статуса: краткий вид из полного (без tkinter).

Верхняя строка, нижняя строка и «Обзор» получают один и тот же brief,
поэтому в режиме full второй запрос на сервер не нужен.
"""

from typing import Optional


def is_full(result: Optional[dict]) -> bool:
    """Полный ответ project_status отличается от brief наличием active_run."""
    return isinstance(result, dict) and isinstance(result.get("active_run"), dict)


def run_state(result: Optional[dict]) -> Optional[str]:
    """Состояние прогона из ответа любого вида."""
    if not isinstance(result, dict):
        return None
    run = result.get("active_run") if is_full(result) else result.get("run")
    return run.get("state") if isinstance(run, dict) else None


def brief_from_status(status: dict) -> dict:
    """Краткий статус из полного (то же правило, что briefFromStatus в React)."""
    run = status.get("active_run") or {}
    steps = status.get("steps") or []
    project = status.get("project") or {}
    stage = status.get("stage") or {}
    spend = status.get("spend") or {}
    current = run.get("current_step")
    found = next((s for s in steps if s.get("id") == current), None) if current else None
    failed = next((s for s in reversed(steps) if s.get("state") == "failed"), None)
    return {
        "generated_at": status.get("generated_at"),
        "project": {"id": project.get("id"), "title": project.get("title")},
        "stage": {"id": stage.get("current"), "title": stage.get("title")},
        "run": {
            "state": run.get("state"),
            "text": run.get("text"),
            "step": ({"id": current, "title": (found or {}).get("title"),
                      "index": run.get("step_index"), "total": run.get("step_total")} if current else None),
            "percent": run.get("percent"),
            "pause_requested": run.get("pause_requested"),
            "cancel_requested": run.get("cancel_requested"),
            "surface": run.get("surface"),
        },
        "spend": {"total": spend.get("total"), "currency": spend.get("currency"),
                  "budget_rub": spend.get("budget_rub")},
        "failed_step": {"id": failed.get("id"), "title": failed.get("title")} if failed else None,
        "final_video_exists": bool((status.get("final_video") or {}).get("exists")),
    }
