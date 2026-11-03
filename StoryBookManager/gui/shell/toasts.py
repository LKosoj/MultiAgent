"""Плашки-тосты поверх окна: правый нижний угол, до 4 штук, ошибки не исчезают сами."""

import logging
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from StoryBookManager.gui.shell import dialogs
from StoryBookManager.gui.shell.toast_model import KIND_TONES, ToastModel
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px

logger = logging.getLogger(__name__)


class ToastManager:
    def __init__(self, root, now: Callable[[], float] = time.monotonic):
        self.root = root
        self.model = ToastModel(now=now)
        self._widgets = {}
        self._after = None

    # --- API ---------------------------------------------------------------
    def show(self, kind: str, text: str, details: Optional[str] = None) -> None:
        self.model.add(kind, text, details)
        self._render()

    def dismiss_top(self) -> bool:
        if self.model.dismiss_top() is None:
            return False
        self._render()
        return True

    def close(self) -> None:
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        self._destroy_widgets()

    # --- Отрисовка ---------------------------------------------------------
    def _destroy_widgets(self) -> None:
        for frame in self._widgets.values():
            try:
                frame.destroy()
            except tk.TclError:
                pass
        self._widgets = {}

    def _render(self) -> None:
        try:
            self._destroy_widgets()
            acc = 0
            for toast in self.model.items:
                frame = self._build(toast)
                frame.update_idletasks()
                height = frame.winfo_reqheight()
                frame.place(relx=1.0, rely=1.0, anchor="se", x=-px(16), y=-(px(40) + acc),
                            width=px(360), height=height)
                frame.lift()
                acc += height + px(PAD_INLINE)
                self._widgets[toast.id] = frame
            self._schedule()
        except tk.TclError:
            logger.debug("Тост не показан: окно уже закрыто")

    def _build(self, toast) -> tk.Frame:
        tone = COLORS[KIND_TONES[toast.kind]]
        frame = tk.Frame(self.root, background=COLORS["card"], highlightthickness=1,
                         highlightbackground=COLORS["border_strong"], cursor="hand2")
        strip = tk.Frame(frame, width=px(6), background=tone)
        strip.pack(side="left", fill="y")
        body = tk.Frame(frame, background=COLORS["card"])
        body.pack(side="left", fill="both", expand=True, padx=px(PAD_INLINE), pady=px(6))
        head = tk.Label(body, text=toast.label, foreground=tone, background=COLORS["card"],
                        font="SBM.Caption", anchor="w")
        head.pack(fill="x")
        text = tk.Label(body, text=toast.display_text, foreground=COLORS["text_primary"],
                        background=COLORS["card"], font="SBM.Body", wraplength=px(300),
                        justify="left", anchor="w")
        text.pack(fill="x")
        close_widgets = [frame, strip, body, head, text]
        if toast.kind == "error" and toast.details:
            more = ttk.Button(body, text="Подробнее",
                              command=lambda d=toast.details: dialogs.show_details(self.root, d))
            more.pack(anchor="w", pady=(px(4), 0))
        for widget in close_widgets:
            widget.bind("<Button-1>", lambda e, tid=toast.id: self._close_one(tid), add="+")
        return frame

    def _close_one(self, toast_id: int) -> None:
        if self.model.dismiss(toast_id):
            self._render()

    def _schedule(self) -> None:
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        delay = self.model.next_expiry_ms()
        if delay is not None:
            self._after = self.root.after(delay, self._tick)

    def _tick(self) -> None:
        self._after = None
        if self.model.expire():
            self._render()
        else:
            self._schedule()
