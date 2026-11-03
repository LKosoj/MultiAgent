"""Мелкие общие виджеты оболочки: подсказка, ряд с переносом, точка статуса, шапка панели."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Optional, Tuple, Union

from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px


def ellipsize(text: str, font, max_px: int) -> str:
    """Обрезать текст до ширины в пикселях, добавив «…»."""
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid].rstrip() + "…") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else "…"


class Tooltip:
    """Всплывающая подсказка; text может быть строкой или функцией без аргументов."""

    DELAY_MS = 500

    def __init__(self, widget, text: Union[str, Callable[[], str]]):
        self.widget = widget
        self.text = text
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def _current_text(self) -> str:
        try:
            return self.text() if callable(self.text) else self.text
        except Exception:
            return ""

    def _schedule(self, _event=None):
        self._hide()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _show(self):
        self._after = None
        text = self._current_text()
        if not text or self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + px(8)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(4)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=text, background=COLORS["card"], foreground=COLORS["text_primary"],
                     relief="solid", borderwidth=1, padx=px(6), pady=px(3), wraplength=px(360),
                     justify="left", font="SBM.Caption").pack()
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _event=None):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class WrapRow(ttk.Frame):
    """Ряд виджетов: не помещающиеся переносятся на следующую строку, а не обрезаются.

    Детей добавляют через add(); сам ряд растягивают по ширине (sticky="ew").
    """

    def __init__(self, parent, pad_x: int = 2, pad_y: int = 2, **kwargs):
        super().__init__(parent, **kwargs)
        self._items = []
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._height = 0
        self._layout_sig = None
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.configure(width=1, height=1)
        self.bind("<Configure>", lambda e: self.relayout(), add="+")

    def add(self, widget, **_ignored):
        self._items.append(widget)
        self.after_idle(self.relayout)
        return widget

    def set_items(self, widgets):
        """Заменить набор виджетов ряда; лишние скрываются."""
        keep = {id(w) for w in widgets}
        for w in self._items:
            if id(w) not in keep:
                w.place_forget()
        self._items = list(widgets)
        self._layout_sig = None
        self.relayout()

    def relayout(self):
        try:
            avail = self.winfo_width()
        except tk.TclError:
            return
        if avail <= 1 or not self._items:
            return
        px_x, px_y = px(self._pad_x), px(self._pad_y)
        x = y = row_h = 0
        placements = []
        for w in self._items:
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x > 0 and x + ww > avail:
                x, y, row_h = 0, y + row_h + px_y, 0
            placements.append((w, x, y, ww))
            x += ww + px_x
            row_h = max(row_h, wh)
        total = y + row_h
        sig = (avail, tuple((id(w), px_, py_, ww) for w, px_, py_, ww in placements), total)
        if sig == self._layout_sig:
            return
        self._layout_sig = sig
        for w, wx, wy, ww in placements:
            w.place(in_=self, x=wx, y=wy)
        if total != self._height:
            self._height = total
            self.configure(height=total)


class StatusDot(tk.Canvas):
    """Цветная точка 10 px: рядом со словом, цвет никогда не единственный признак."""

    def __init__(self, parent, tone: str = "text_muted", bg: Optional[str] = None, **kwargs):
        size = px(10)
        super().__init__(parent, width=size + 2, height=size + 2, highlightthickness=0,
                         background=bg or COLORS["surface"], **kwargs)
        self._size = size
        self._oval = self.create_oval(1, 1, size + 1, size + 1, outline="", fill=COLORS[tone])

    def set_tone(self, tone: str) -> None:
        self.itemconfigure(self._oval, fill=COLORS.get(tone, COLORS["text_muted"]))


class PanelHeader(ttk.Frame):
    """Единая шапка экрана: заголовок, необязательное пояснение, кнопки справа."""

    def __init__(self, parent, title: str, subtitle: Optional[str] = None,
                 actions: Iterable[Tuple[str, Callable[[], None]]] = ()):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=title, style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        if subtitle:
            self.subtitle_label = ttk.Label(self, text=subtitle, style="Caption.TLabel", wraplength=px(640),
                                            justify="left")
            self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        actions = list(actions)
        if actions:
            row = ttk.Frame(self, style="Panel.TFrame")
            row.grid(row=0, column=1, rowspan=2, sticky="e")
            for i, (text, command) in enumerate(actions):
                ttk.Button(row, text=text, command=command).pack(side="left", padx=(0 if i == 0 else px(PAD_INLINE), 0))
