"""Окно просмотра картинок с зумом: кнопки, колесо, клавиши, перетаскивание, листание стрелками."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT
from StoryBookManager.gui.imagetools.texts import fmt
from StoryBookManager.gui.imagetools.zoom_canvas import ZoomCanvas
from StoryBookManager.gui.shell.widgets import Tooltip, WrapRow
from StoryBookManager.gui.shots import texts as T
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px


class ImageZoomViewer(tk.Toplevel):
    """Стрелки листают картинки шота (масштаб сбрасывается на «по окну»), Esc закрывает.

    on_edit(part, viewer) вызывается по «Править ИИ…» для частей из edit_parts ("start"/"end");
    is_blocked() возвращает причину, по которой править нельзя (пустая строка - можно).
    """

    def __init__(self, parent, title: str, items: Sequence[Tuple[str, Optional[str]]], index: int,
                 on_edit: Optional[Callable[[str, "ImageZoomViewer"], None]] = None,
                 edit_parts: Optional[Sequence[Optional[str]]] = None,
                 is_blocked: Optional[Callable[[], str]] = None):
        super().__init__(parent)
        self.items: List[Tuple[str, Optional[str]]] = list(items)
        self.index = index
        self._on_edit = on_edit
        self._edit_parts = list(edit_parts or [None] * len(self.items))
        self._is_blocked = is_blocked or (lambda: "")
        self.title(T.fmt("viewer_title", title=title))
        self.transient(parent.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        self.geometry(f"{px(900)}x{px(640)}")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        bar = WrapRow(self)
        bar.grid(row=0, column=0, sticky="ew", padx=px(PAD_INLINE), pady=(px(PAD_INLINE), 0))
        self.canvas = ZoomCanvas(self, on_change=self._on_zoom, empty_text=T.SHOT_TEXTS["no_image"])
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=px(PAD_INLINE), pady=px(PAD_INLINE))
        self.buttons = {}
        for name, text_id, command in (("in", "zoom_in", lambda: self.canvas.zoom("in")),
                                       ("out", "zoom_out", lambda: self.canvas.zoom("out")),
                                       ("fit", "zoom_fit", self.canvas.fit),
                                       ("actual", "zoom_actual", self.canvas.actual)):
            self.buttons[name] = bar.add(ttk.Button(bar, text=IT[text_id], command=command))
        self.level = tk.StringVar()
        bar.add(ttk.Label(bar, textvariable=self.level, width=6))
        self.edit_btn = None
        if on_edit is not None:
            self.edit_btn = bar.add(ttk.Button(bar, text=IT["edit_open"], command=self._edit))
            self._edit_tip = Tooltip(self.edit_btn, self._is_blocked)
        hint = ttk.Label(self, text=f"{T.SHOT_TEXTS['viewer_hint']}. {IT['zoom_hint']}", style="Caption.TLabel",
                         wraplength=px(860), justify="left")
        hint.grid(row=2, column=0, sticky="w", padx=px(PAD_INLINE), pady=(0, px(4)))
        self.bind("<Left>", lambda e: self._step(-1))
        self.bind("<Right>", lambda e: self._step(1))
        self.bind("<Escape>", lambda e: self.destroy())
        self.canvas.bind_keys(self)
        self._show_after = self.after(50, self._first_show)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.focus_set()

    def _first_show(self) -> None:
        self._show_after = None
        self._show()

    def _on_destroy(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        after_id, self._show_after = self._show_after, None
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass

    def _on_zoom(self, percent: str) -> None:
        self.level.set(fmt("zoom_level", pct=percent.rstrip("%")))

    def _step(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.items)
        self._show()

    def _show(self) -> None:
        """Показать выбранную картинку «по размеру окна»; вызывается и после правки, чтобы перечитать файл."""
        title, path = self.items[self.index]
        self.title(T.fmt("viewer_title", title=title))
        self.canvas.load(path)
        self._sync_edit()

    refresh_image = _show

    def current_part(self) -> Optional[str]:
        return self._edit_parts[self.index] if self.index < len(self._edit_parts) else None

    def _sync_edit(self) -> None:
        if self.edit_btn is None:
            return
        path = self.items[self.index][1]
        ok = self.current_part() in ("start", "end") and bool(path) and not self._is_blocked()
        self.edit_btn.configure(state="normal" if ok else "disabled")

    def _edit(self) -> None:
        part = self.current_part()
        if part in ("start", "end") and self._on_edit is not None and not self._is_blocked():
            self._on_edit(part, self)
