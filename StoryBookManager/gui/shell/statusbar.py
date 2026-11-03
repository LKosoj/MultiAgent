"""Нижняя строка: последнее сообщение, время, ссылка «Журнал», прогресс (если передан)."""

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from StoryBookManager.gui.theme_tokens import PAD_INLINE, px


class StatusBar(ttk.Frame):
    def __init__(self, parent, status_var: tk.StringVar, progress_var: tk.DoubleVar,
                 on_journal: Optional[Callable[[], None]] = None):
        super().__init__(parent, style="StatusBar.TFrame")
        pad = px(PAD_INLINE)
        self.journal_btn = ttk.Button(self, text="Журнал", command=on_journal or (lambda: None))
        self.journal_btn.pack(side="right", padx=(pad, pad))
        self.progress_bar = ttk.Progressbar(self, variable=progress_var, length=px(160), mode="determinate")
        self._progress_visible = False
        self.time_var = tk.StringVar(value="")
        self.time_label = ttk.Label(self, textvariable=self.time_var, style="StatusBar.TLabel")
        self.time_label.pack(side="right", padx=(pad, 0))
        self.message_label = ttk.Label(self, textvariable=status_var, style="StatusBar.TLabel", anchor="w")
        self.message_label.pack(side="left", fill="x", expand=True, padx=(pad, 0), pady=px(2))

    def stamp(self) -> None:
        self.time_var.set(time.strftime("%H:%M"))

    def show_progress(self, visible: bool) -> None:
        if visible and not self._progress_visible:
            self.progress_bar.pack(side="right", padx=(px(PAD_INLINE), 0))
            self._progress_visible = True
        elif not visible and self._progress_visible:
            self.progress_bar.pack_forget()
            self._progress_visible = False
