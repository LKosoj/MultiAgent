"""Верхняя строка: проект, этап, состояние прогона, траты, «Пауза» и «Стоп»."""

import dataclasses
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Optional

from StoryBookManager.gui.shell.run_status import RunView, UNAVAILABLE_TEXT
from StoryBookManager.gui.shell.widgets import StatusDot, Tooltip, ellipsize
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, PAD_PANEL, px

NO_PROJECT_TITLE = "Проект не выбран"
STAGE_MAX_PX = 190


class TopBar(ttk.Frame):
    def __init__(self, parent, *, on_projects: Callable[[], None], on_generation: Callable[[], None],
                 on_pause: Callable[[], None], on_stop: Callable[[], None],
                 on_rerun: Optional[Callable[[], None]] = None):
        super().__init__(parent, style="TopBar.TFrame")
        self._on_rerun = on_rerun or (lambda: None)
        self._on_pause = on_pause
        self._on_stop = on_stop
        self._title_full = NO_PROJECT_TITLE
        self._project_id = ""
        self._stage_full = ""
        self._run_full = ""
        self._spend = ""
        self._step_id = ""
        self._view = RunView()
        self._body = tkfont.nametofont("SBM.Body")
        self._subtitle = tkfont.nametofont("SBM.Subtitle")
        pad = px(PAD_PANEL)
        inner = px(PAD_INLINE)
        self.configure(height=px(48))
        self.pack_propagate(False)

        # справа налево: кнопки, траты
        self.stop_btn = ttk.Button(self, text="Стоп", command=self._stop_clicked, state="disabled")
        self.stop_btn.pack(side="right", padx=(inner, pad))
        self.pause_btn = ttk.Button(self, text="Пауза", command=self._pause_clicked, state="disabled")
        self.pause_btn.pack(side="right")
        self.rerun_btn = ttk.Button(self, text="Запустить заново", command=self._rerun_clicked, state="disabled")
        self.rerun_btn.pack(side="right", padx=(inner, inner))
        self.spend_label = ttk.Label(self, text="", style="TopBar.TLabel")
        self.spend_label.pack(side="right", padx=(inner, pad))

        # слева направо: проект, этап, индикатор прогона (занимает остаток)
        self.title_label = ttk.Label(self, text=NO_PROJECT_TITLE, style="TopBarTitle.TLabel", cursor="hand2",
                                     takefocus=True)
        self.title_label.pack(side="left", padx=(pad, inner))
        self.stage_label = ttk.Label(self, text="", style="TopBarMuted.TLabel")
        self.stage_label.pack(side="left", padx=(inner, inner))
        run_box = ttk.Frame(self, style="TopBar.TFrame", takefocus=True)
        run_box.pack(side="left", fill="x", expand=True, padx=(inner, 0))
        self.run_box = run_box
        self.dot = StatusDot(run_box)
        self.dot.pack(side="left", padx=(0, inner))
        self.run_label = ttk.Label(run_box, text="", style="TopBar.TLabel", cursor="hand2")
        self.run_label.pack(side="left")

        for widget, command in ((self.title_label, on_projects), (self.run_label, on_generation),
                                (self.dot, on_generation)):
            widget.bind("<Button-1>", lambda e, c=command: c(), add="+")
        self.title_label.bind("<Return>", lambda e: on_projects())
        self.title_label.bind("<space>", lambda e: on_projects())
        run_box.bind("<Return>", lambda e: on_generation())
        run_box.bind("<space>", lambda e: on_generation())

        Tooltip(self.title_label, lambda: self._project_id)
        Tooltip(self.run_label, lambda: self._step_id)
        Tooltip(self.pause_btn, lambda: self._view.pause_hint if not self._view.pause_enabled else "")
        Tooltip(self.rerun_btn, lambda: self._view.rerun_hint)
        Tooltip(self.stop_btn, lambda: self._view.stop_hint if not self._view.stop_enabled else "")
        self.bind("<Configure>", lambda e: self._fit(), add="+")
        self._apply_view(RunView())

    # --- Публичное ---------------------------------------------------------
    def set_project(self, title: str, project_id: str = "") -> None:
        self._title_full = title or NO_PROJECT_TITLE
        self._project_id = project_id
        self._fit()

    def set_stage(self, stage_title: str) -> None:
        self._stage_full = f"Этап: {stage_title}" if stage_title else ""
        self._fit()

    def clear(self) -> None:
        self._title_full, self._project_id, self._stage_full, self._spend = NO_PROJECT_TITLE, "", "", ""
        self._apply_view(RunView())

    def update_view(self, view: RunView, spend: str = "") -> None:
        self._spend = spend
        self._apply_view(view)

    def show_unavailable(self) -> None:
        """Опрос не отвечает: старые данные не выдаём за свежие."""
        self._spend = ""
        self._apply_view(dataclasses.replace(
            RunView(), text=UNAVAILABLE_TEXT, pause_hint=UNAVAILABLE_TEXT, stop_hint=UNAVAILABLE_TEXT))

    # --- Внутреннее --------------------------------------------------------
    def _apply_view(self, view: RunView) -> None:
        self._view = view
        self._run_full = view.text
        self._step_id = view.step_id
        self.dot.set_tone(view.tone)
        self.run_label.configure(foreground=COLORS[view.tone])
        self.pause_btn.configure(text=view.pause_label, state="normal" if view.pause_enabled else "disabled")
        self.stop_btn.configure(state="normal" if view.stop_enabled else "disabled")
        self.rerun_btn.configure(state="normal" if view.rerun_enabled else "disabled")
        self._fit()

    def _pause_clicked(self) -> None:
        self._lock_buttons()
        self._on_pause()

    def _stop_clicked(self) -> None:
        self._lock_buttons()
        self._on_stop()

    def _rerun_clicked(self) -> None:
        self._on_rerun()

    def _lock_buttons(self) -> None:
        """Защита от двойного клика: кнопки оживают после ответа или следующего опроса."""
        self.pause_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")

    def unlock(self) -> None:
        """Вернуть кнопки по текущему состоянию (диалог отменили)."""
        self.pause_btn.configure(state="normal" if self._view.pause_enabled else "disabled")
        self.stop_btn.configure(state="normal" if self._view.stop_enabled else "disabled")

    def _fit(self) -> None:
        """Уложить тексты в одну строку: траты и кнопки остаются, названия обрезаются с «…»."""
        self.spend_label.configure(text=self._spend)
        width = self.winfo_width()
        if width <= 1:
            self.title_label.configure(text=self._title_full)
            self.stage_label.configure(text=self._stage_full)
            self.run_label.configure(text=self._run_full)
            return
        self.update_idletasks()
        inner, pad = px(PAD_INLINE), px(PAD_PANEL)
        fixed = (self.pause_btn.winfo_reqwidth() + self.stop_btn.winfo_reqwidth() + self.rerun_btn.winfo_reqwidth()
                 + self.spend_label.winfo_reqwidth() + self.dot.winfo_reqwidth() + inner * 14 + pad * 2)
        stage = ellipsize(self._stage_full, self._body, px(STAGE_MAX_PX)) if self._stage_full else ""
        avail = width - fixed - self._body.measure(stage)
        title_w, run_w = self._subtitle.measure(self._title_full), self._body.measure(self._run_full)
        if title_w + run_w > avail:
            run_cap = max(int(avail * 0.55), avail - title_w)
            run_w = min(run_w, max(run_cap, px(120)))
            title_w = max(avail - run_w, px(80))
        self.title_label.configure(text=ellipsize(self._title_full, self._subtitle, title_w))
        self.stage_label.configure(text=stage)
        self.run_label.configure(text=ellipsize(self._run_full, self._body, run_w))
