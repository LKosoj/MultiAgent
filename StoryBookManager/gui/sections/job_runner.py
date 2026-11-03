"""Запуск долгих возможностей studio и опрос projects_job_status (без tkinter).

Возможность возвращает job_id; состояние задания читается раз в секунду
до done, error или interrupted. На один job_id идёт один опрос,
на один проект - не больше одного задания.
"""

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

POLL_MS = 1000
POLL_ERRORS_LIMIT = 3
INTERRUPTED_TEXT = "Операция прервана"


class JobRunner:
    def __init__(self, bridge, widget, *, poll_ms: int = POLL_MS):
        self._bridge = bridge
        self._widget = widget
        self._poll_ms = poll_ms
        self._active: Dict[str, dict] = {}   # job_id -> запись
        self._projects: Dict[str, str] = {}  # project_id -> job_id
        self._closed = False

    # --- Состояние ------------------------------------------------------------
    def is_busy(self, project_id: Optional[str]) -> bool:
        return bool(project_id) and project_id in self._projects

    def active_count(self) -> int:
        return len(self._active)

    def close(self) -> None:
        self._closed = True
        for entry in self._active.values():
            after_id = entry.get("after")
            if after_id is not None:
                try:
                    self._widget.after_cancel(after_id)
                except Exception:
                    pass
        self._active.clear()
        self._projects.clear()

    # --- Запуск ------------------------------------------------------------------
    def start(self, capability: str, payload: dict, *, project_id: Optional[str] = None,
              on_started: Optional[Callable[[dict], None]] = None,
              on_progress: Optional[Callable[[dict], None]] = None,
              on_done: Optional[Callable[[dict], None]] = None,
              on_error: Optional[Callable[[dict], None]] = None) -> bool:
        """Запустить возможность; False, если для проекта уже идёт задание."""
        if project_id and project_id in self._projects:
            return False
        key = project_id or ""
        if key:
            self._projects[key] = ""   # место занято до ответа сервера
        fail = on_error or (lambda error: None)

        def started(result: dict) -> None:
            job_id = result.get("job_id")
            if self._closed:
                return
            if not isinstance(job_id, str) or not job_id:
                self._release(key, "")
                fail({"code": "internal", "message": "Сервер не вернул номер задания"})
                return
            if key:
                self._projects[key] = job_id
            self._active[job_id] = {"project_id": key, "errors": 0, "after": None,
                                    "progress": on_progress, "done": on_done, "error": fail}
            if on_started is not None:
                try:
                    on_started(result)
                except Exception:
                    logger.exception("on_started упал")
            self._poll(job_id)

        def failed(result: dict) -> None:
            self._release(key, "")
            if not self._closed:
                fail(result.get("error") or {"code": "internal", "message": "Внутренняя ошибка"})

        self._bridge.call(capability, payload, on_success=started, on_error=failed)
        return True

    # --- Опрос ------------------------------------------------------------------------
    def _release(self, project_key: str, job_id: str) -> None:
        if project_key and self._projects.get(project_key, "") == job_id:
            self._projects.pop(project_key, None)

    def _poll(self, job_id: str) -> None:
        entry = self._active.get(job_id)
        if entry is None or self._closed:
            return
        entry["after"] = None
        self._bridge.call("projects_job_status", {"job_id": job_id},
                          on_success=lambda r: self._status(job_id, r),
                          on_error=lambda r: self._poll_failed(job_id, r))

    def _again(self, job_id: str) -> None:
        entry = self._active.get(job_id)
        if entry is None or self._closed:
            return
        try:
            entry["after"] = self._widget.after(self._poll_ms, lambda: self._poll(job_id))
        except Exception:
            self._finish(job_id, "error", {"code": "internal", "message": "Опрос задания остановлен"})

    def _status(self, job_id: str, result: dict) -> None:
        entry = self._active.get(job_id)
        if entry is None:
            return
        entry["errors"] = 0
        state = result.get("state")
        if state == "done":
            self._finish(job_id, "done", result.get("result") or {})
        elif state == "error":
            self._finish(job_id, "error", result.get("error") or {"code": "internal", "message": "Внутренняя ошибка"})
        elif state == "interrupted":
            self._finish(job_id, "error", {"code": "interrupted", "message": INTERRUPTED_TEXT})
        else:
            callback = entry.get("progress")
            if callback is not None:
                try:
                    callback(result.get("progress") or {})
                except Exception:
                    logger.exception("on_progress упал")
            self._again(job_id)

    def _poll_failed(self, job_id: str, result: dict) -> None:
        entry = self._active.get(job_id)
        if entry is None:
            return
        entry["errors"] += 1
        if entry["errors"] >= POLL_ERRORS_LIMIT:
            self._finish(job_id, "error", result.get("error") or {"code": "internal", "message": "Нет связи с заданием"})
        else:
            self._again(job_id)

    def _finish(self, job_id: str, outcome: str, payload: dict) -> None:
        entry = self._active.pop(job_id, None)
        if entry is None:
            return
        self._release(entry["project_id"], job_id)
        callback = entry["done"] if outcome == "done" else entry["error"]
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            logger.exception("Обработчик задания упал")
