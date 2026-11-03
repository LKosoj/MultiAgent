"""Раздел «Кадры» для оболочки: пять действий SectionAdapter плюс переходы из других разделов."""

import logging
import tkinter as tk
from typing import Optional

from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shots.panel import ShotsPanel

logger = logging.getLogger(__name__)

LEAVE_TIMEOUT_MS = 15000


class ShotsAdapter(ShotsPanel, SectionAdapter):
    def on_show(self) -> None:
        self.ctl.on_show()

    def on_refresh(self) -> None:
        self.ctl.on_refresh()

    def on_save(self) -> None:
        self.ctl.save()

    def on_project_changed(self, project) -> None:
        self.ctl.set_project(getattr(project, "project_id", None) if project is not None else None)

    def on_status(self, brief: Optional[dict]) -> None:
        """Сводка проекта из верхней панели: состояние прогона блокирует перегенерацию."""
        self.ctl.on_status(brief)

    def apply_intent(self, filter: Optional[str] = None, shot_key: Optional[str] = None) -> None:
        self.ctl.apply_intent(filter, shot_key)

    def can_leave(self) -> bool:
        """Несохранённая правка: спросить; при «Сохранить» дождаться ответа сервера (не дольше 15 с)."""
        if not self.ctl.has_unsaved():
            return True
        choice = self.ask_unsaved(self.ctl.unsaved_what())
        if choice == "discard":
            return True
        if choice != "save":
            return False
        outcome = {}
        done = tk.StringVar()

        def finish(ok: bool):
            if "ok" not in outcome:
                outcome["ok"] = ok
                done.set("1")

        self.ctl.save(then=lambda: finish(True), on_fail=lambda: finish(False))
        if "ok" not in outcome:
            timer = self.after(LEAVE_TIMEOUT_MS, lambda: finish(False))
            self.wait_variable(done)
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass
        return bool(outcome.get("ok"))
