"""Опрос статуса через мост: один запрос в полёте, интервалы по состоянию прогона.

Режим brief - project_status_brief; режим full (раздел «Обзор») - project_status,
краткий вид считается из полного ответа, второго таймера нет.
"""

import logging
from typing import Callable, Optional

from StoryBookManager.gui.shell.run_status import poll_interval_ms
from StoryBookManager.gui.shell.status_model import brief_from_status, run_state

logger = logging.getLogger(__name__)

ERRORS_BEFORE_UNAVAILABLE = 3
RETRY_MS = 10000
HIDDEN_CHECK_MS = 1000


class StatusPoller:
    def __init__(self, bridge, widget, *, on_update: Callable[[dict], None],
                 on_unavailable: Callable[[], None],
                 on_full: Optional[Callable[[dict], None]] = None,
                 on_error: Optional[Callable[[dict], None]] = None):
        self._bridge = bridge
        self._widget = widget
        self._on_update = on_update
        self._on_unavailable = on_unavailable
        self._on_full = on_full
        self._on_error = on_error
        self._mode = "brief"
        self._force = False
        self._project_id: Optional[str] = None
        self._generation = 0
        self._in_flight = False
        self._again = False
        self._errors = 0
        self._state: Optional[str] = None
        self._after_id = None

    @property
    def project_id(self) -> Optional[str]:
        return self._project_id

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def errors(self) -> int:
        return self._errors

    def start(self, project_id: str) -> None:
        self.stop()
        self._project_id = project_id
        self._state = None
        self._errors = 0
        self._request()

    def stop(self) -> None:
        self._generation += 1
        self._project_id = None
        self._in_flight = False
        self._again = False
        self._cancel_timer()

    def set_mode(self, mode: str) -> None:
        """brief | full. Смена режима сбрасывает запрос в полёте и спрашивает сразу."""
        if mode not in ("brief", "full") or mode == self._mode:
            return
        self._mode = mode
        if self._project_id is None:
            return
        self._generation += 1  # ответ прошлого режима другой формы - отбрасываем
        self._in_flight = False
        self._again = False
        self._cancel_timer()
        self._request()

    def request_refresh(self, force: bool = False) -> None:
        """Внеочередной запрос (после «Пауза», по F5, после завершения генерации).

        force=True - ручное «Обновить»: в режиме full сервер перечитывает кэш.
        """
        if self._project_id is None:
            return
        if force:
            self._force = True
        if self._in_flight:
            self._again = True
            return
        self._cancel_timer()
        self._request()

    # --- Внутреннее --------------------------------------------------------
    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self, delay_ms: int) -> None:
        self._cancel_timer()
        if self._project_id is None:
            return
        try:
            self._after_id = self._widget.after(delay_ms, self._tick)
        except Exception:
            self._after_id = None

    def _visible(self) -> bool:
        try:
            return bool(self._widget.winfo_viewable())
        except AttributeError:
            return True
        except Exception:
            return False

    def _tick(self) -> None:
        self._after_id = None
        if self._project_id is None:
            return
        if not self._visible():
            # окно свёрнуто: базу не читаем, ждём, пока развернут
            self._schedule(HIDDEN_CHECK_MS)
            return
        self._request()

    def _request(self) -> None:
        if self._project_id is None or self._in_flight:
            return
        self._in_flight = True
        generation = self._generation
        if self._mode == "full":
            capability = "project_status"
            payload = {"project_id": self._project_id, "surface": "tk"}
            if self._force:
                payload["refresh"] = True
        else:
            capability = "project_status_brief"
            payload = {"project_id": self._project_id, "surface": "tk"}
        self._force = False
        try:
            self._bridge.call(
                capability, payload,
                latest_only_key="topbar_status",
                on_success=lambda r: self._done(generation, r),
                on_error=lambda r: self._failed(generation, r),
            )
        except Exception:
            logger.exception("Не удалось запросить состояние проекта")
            self._in_flight = False
            self._schedule(RETRY_MS)

    def _finish(self) -> bool:
        """Общая часть ответа; True - нужно ещё раз сразу (был внеочередной запрос)."""
        self._in_flight = False
        again, self._again = self._again, False
        return again

    def _done(self, generation: int, result: dict) -> None:
        if generation != self._generation:
            return
        again = self._finish()
        self._errors = 0
        self._state = run_state(result)
        brief = result
        if self._mode == "full":
            brief = brief_from_status(result)
            if self._on_full is not None:
                try:
                    self._on_full(result)
                except Exception:
                    logger.exception("Ошибка обновления «Обзора»")
        try:
            self._on_update(brief)
        except Exception:
            logger.exception("Ошибка обновления верхней строки")
        self._next(again)

    def _failed(self, generation: int, result: dict) -> None:
        if generation != self._generation:
            return
        again = self._finish()
        self._errors += 1
        if self._on_error is not None:
            try:
                self._on_error(result)
            except Exception:
                logger.exception("Ошибка показа сбоя статуса")
        if self._errors >= ERRORS_BEFORE_UNAVAILABLE:
            try:
                self._on_unavailable()
            except Exception:
                logger.exception("Ошибка обновления верхней строки")
        self._next(again)

    def _next(self, again: bool) -> None:
        if again:
            self._request()
            return
        delay = poll_interval_ms(self._state)
        self._schedule(delay)
