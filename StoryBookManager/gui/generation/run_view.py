"""Блок «Прогон»: шаг, полоса прогресса, видео, траты, оставшееся время, «Пауза» и «Стоп»."""

from tkinter import ttk
from typing import Callable

from StoryBookManager.gui.generation import model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.shell.run_status import format_money
from StoryBookManager.gui.theme_tokens import PAD_INLINE, px


class RunView(ttk.Frame):
    def __init__(self, parent, *, on_pause: Callable[[], None], on_stop: Callable[[], None]):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=T.fmt("section_run"), style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        self.step_label = ttk.Label(self, text=T.fmt("progress_idle"))
        self.step_label.grid(row=1, column=0, sticky="w")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=px(4))
        self.info = ttk.Label(self, text="", style="Caption.TLabel", justify="left", wraplength=px(560))
        self.info.grid(row=3, column=0, columnspan=2, sticky="w")
        # перенос строки сводки следует за шириной блока
        self.bind("<Configure>", lambda e: self.info.configure(wraplength=max(120, e.width - 8)))
        # кнопки справа от названия шага: экономим высоту
        row = ttk.Frame(self, style="Panel.TFrame")
        row.grid(row=0, column=1, rowspan=2, sticky="e")
        self.pause_btn = ttk.Button(row, text=T.fmt("pause"), command=on_pause, state="disabled")
        self.pause_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text=T.fmt("stop"), command=on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(px(PAD_INLINE), 0))

    def show(self, status, titles, unavailable: bool = False, pause_busy: bool = False,
             stop_busy: bool = False) -> None:
        if unavailable:
            self.step_label.configure(text=T.TK_TEXTS["tk_unavailable"])
            return
        status = status or {}
        state = status.get("state") or "idle"
        busy = model.is_busy_state(state)
        self.step_label.configure(text=model.step_line(status, titles) if busy else T.fmt("progress_idle"))
        percent = (status.get("progress") or {}).get("percent")
        self.bar.configure(value=percent if busy and isinstance(percent, (int, float)) else 0)
        parts = []
        video = status.get("video") or {}
        if busy and video.get("total"):
            line = T.fmt("video_progress", done=video.get("done", 0), total=video["total"])
            if video.get("failed"):
                line += f" ({T.fmt('video_failed', n=video['failed'])})"
            parts.append(line)
        cost = status.get("cost") or {}
        if busy and isinstance(cost.get("spent_rub"), (int, float)):
            if cost.get("budget_rub"):
                parts.append(T.fmt("spent_of", sum=format_money(cost["spent_rub"]), budget=format_money(cost["budget_rub"])))
            else:
                parts.append(T.fmt("spent", sum=format_money(cost["spent_rub"])))
        eta = status.get("eta")
        if busy and eta:
            parts.append(T.fmt("eta", time=model.format_duration(eta.get("seconds"))) if eta.get("seconds") is not None
                         and eta.get("basis") == "history" else T.fmt("eta_none"))
        self.info.configure(text="  ·  ".join(parts))
        paused = state in ("paused", "pause_requested")
        self.pause_btn.configure(text=T.fmt("resume") if paused else T.fmt("pause"),
                                 state="normal" if state in ("running", "paused", "pause_requested") and not pause_busy
                                 else "disabled")
        self.stop_btn.configure(state="normal" if state in ("running", "paused", "pause_requested") and not stop_busy
                                else "disabled")
