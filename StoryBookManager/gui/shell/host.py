"""Контейнер разделов: показывает один, остальные скрыты (grid_remove, не tkraise - иначе фокус уходит в скрытое)."""

from tkinter import ttk
from typing import Dict, Optional


class SectionHost(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._widgets: Dict[str, object] = {}
        self.current: Optional[str] = None

    def add(self, section_id: str, widget) -> None:
        self._widgets[section_id] = widget
        widget.grid(row=0, column=0, sticky="nsew")
        widget.grid_remove()

    def show(self, section_id: str) -> None:
        if section_id not in self._widgets:
            raise KeyError(section_id)
        if self.current and self.current != section_id:
            self._widgets[self.current].grid_remove()
        self._widgets[section_id].grid()
        self.current = section_id
