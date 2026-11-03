"""Мост Tk -> studio: вызывает те же функции, что и бэкенд, в фоновых потоках.

Результат всегда отдаётся колбэку в главном потоке Tk: рабочие потоки Tk не трогают,
главный поток сам опрашивает очередь через widget.after.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from custom_tools.storybook.studio.capabilities import invoke, registry
from custom_tools.storybook.studio.errors import error_result

logger = logging.getLogger(__name__)

Callback = Callable[[dict], None]


class StudioBridge:
    def __init__(self, widget, *, max_workers: int = 4, poll_ms: int = 50):
        self._widget = widget
        self._poll_ms = poll_ms
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="studio")
        self._results: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._counter = 0
        self._latest: dict[str, int] = {}
        self._inflight = 0
        self._polling = False
        self._closed = False

    def call(self, capability_id: str, payload: dict | None = None, *,
             on_success: Callback | None = None, on_error: Callback | None = None,
             latest_only_key: str | None = None) -> int:
        """Запустить возможность в фоне; вернуть номер задания."""
        with self._lock:
            self._counter += 1
            job = self._counter
            if latest_only_key is not None:
                self._latest[latest_only_key] = job
            self._inflight += 1
            start_polling = not self._polling
            self._polling = True
        self._executor.submit(self._run, job, capability_id, dict(payload or {}),
                              on_success, on_error, latest_only_key)
        if start_polling:
            self._schedule()
        return job

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job, capability_id, payload, on_success, on_error, key) -> None:
        try:
            result = invoke(capability_id, payload)
        except Exception:  # PermissionError и прочее: мост не должен падать
            logger.exception("studio bridge: %s failed", capability_id)
            result = error_result("internal", "Внутренняя ошибка")
        self._results.put((job, result, on_success, on_error, key))

    def _schedule(self) -> None:
        try:
            self._widget.after(self._poll_ms, self._drain)
        except Exception:
            self._stop()

    def _stop(self) -> None:
        with self._lock:
            self._polling = False
        self._closed = True

    def _alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self._widget.winfo_exists())
        except Exception:
            return False

    def _drain(self) -> None:
        if not self._alive():
            self._stop()
            return
        while True:
            try:
                job, result, on_success, on_error, key = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._inflight -= 1
                stale = key is not None and self._latest.get(key) != job
            if stale:
                continue
            callback = on_success if result.get("status") == "success" else on_error
            if callback is None:
                if result.get("status") != "success":
                    logger.warning("studio bridge: ошибка без обработчика: %s", result.get("error"))
                continue
            try:
                callback(result)
            except Exception:
                logger.exception("studio bridge: ошибка в колбэке")
        with self._lock:
            if self._inflight <= 0:
                self._polling = False
                return
        self._schedule()


class TkHandler:
    """Вызываемый обработчик одной возможности для Tk-панелей."""

    def __init__(self, capability_id: str):
        self.capability_id = capability_id

    def __call__(self, bridge: StudioBridge, payload: dict | None = None, **callbacks) -> int:
        return bridge.call(self.capability_id, payload, **callbacks)


def tk_handlers() -> dict[str, TkHandler]:
    """Обработчики всех возможностей реестра по ключу tk_handler."""
    return {cap.tk_handler: TkHandler(cap.id) for cap in registry().values()}
