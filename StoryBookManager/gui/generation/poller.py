"""Опрос состояния прогона для экрана «Генерация» (без tkinter).

Интервалы: 2 с - идёт, пауза запрошена или остановка; 5 с - пауза; 15 с - всё остальное.
Одновременно уходит один запрос; ответы прошлого проекта и прошлого запуска опроса отбрасываются.
Три сбоя подряд - «состояние недоступно».
"""

from typing import Callable, Optional

FAILS_TO_UNAVAILABLE = 3


def interval_ms(state: Optional[str]) -> int:
    if state in ("running", "pause_requested", "stopping"):
        return 2000
    if state == "paused":
        return 5000
    return 15000


class RunPoller:
    def __init__(self, bridge, schedule: Callable, cancel: Callable, on_status: Callable[[dict], None],
                 on_unavailable: Callable[[], None]):
        self.bridge = bridge
        self.schedule = schedule
        self.cancel = cancel
        self.on_status = on_status
        self.on_unavailable = on_unavailable
        self.project_id: Optional[str] = None
        self.state: Optional[str] = None
        self.errors = 0
        self._gen = 0
        self._in_flight = False
        self._again = False
        self._timer = None

    def start(self, project_id: Optional[str]) -> None:
        """Новый проект или первый запуск: старые ответы больше не принимаются."""
        self._gen += 1
        self._in_flight = False
        self._again = False
        self._drop_timer()
        self.project_id = project_id
        self.state = None
        self.errors = 0
        if project_id:
            self.refresh()

    def stop(self) -> None:
        self._gen += 1
        self._in_flight = False
        self._drop_timer()
        self.project_id = None

    def refresh(self) -> None:
        """Спросить сейчас; если запрос уже идёт - повторить сразу после него."""
        if not self.project_id:
            return
        if self._in_flight:
            self._again = True
            return
        self._drop_timer()
        self._in_flight = True
        gen, project = self._gen, self.project_id

        def ok(result):
            if gen != self._gen:
                return
            self._in_flight = False
            self.errors = 0
            self.state = result.get("state")
            self.on_status(result)
            self._next()

        def failed(_result):
            if gen != self._gen:
                return
            self._in_flight = False
            self.errors += 1
            if self.errors == FAILS_TO_UNAVAILABLE:
                self.on_unavailable()
            self._next()

        self.bridge.call("run_status", {"project_id": project}, on_success=ok, on_error=failed,
                         latest_only_key="generation.run_status")

    def _next(self) -> None:
        if self._again:
            self._again = False
            self.refresh()
            return
        gen = self._gen
        self._timer = self.schedule(interval_ms(self.state), lambda: self._tick(gen))

    def _tick(self, gen: int) -> None:
        self._timer = None
        if gen == self._gen:
            self.refresh()

    def _drop_timer(self) -> None:
        if self._timer is not None:
            try:
                self.cancel(self._timer)
            except Exception:
                pass
            self._timer = None
