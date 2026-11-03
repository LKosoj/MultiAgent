"""Прокручиваемая область: Canvas + полоса прокрутки + колесо мыши (когда указатель над областью)."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.theme_tokens import COLORS


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=COLORS["window_bg"])
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw", tags="inner")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure("inner", width=e.width))
        # колесо ловим на окне (привязка снимается в destroy), реагируем только когда указатель над этой областью
        self._top = None
        self._ids = []
        self.bind("<Map>", self._attach, add="+")

    def _attach(self, _e):
        if self._top is not None:
            return
        self._top = self.winfo_toplevel()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._ids.append((seq, self._top.bind(seq, self._wheel, add="+")))

    def destroy(self):
        if self._top is not None:
            for seq, func_id in self._ids:
                try:
                    self._top.unbind(seq, func_id)
                except tk.TclError:
                    pass
            self._ids = []
        super().destroy()

    def _wheel(self, event):
        under = self.winfo_containing(event.x_root, event.y_root)
        if under is None or not str(under).startswith(str(self)):
            return None
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return None
        step = -1 if (getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(step * 3, "units")
        return "break"

    def scroll_to_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def visible(self, widget):
        """Виден ли виджет целиком в видимой части области (после прокрутки)."""
        self.canvas.update_idletasks()
        top = self.canvas.winfo_rooty()
        bottom = top + self.canvas.winfo_height()
        y, h = widget.winfo_rooty(), widget.winfo_height()
        return widget.winfo_viewable() and y >= top - 1 and y + h <= bottom + 1
