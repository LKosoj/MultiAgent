"""Левая навигация из восьми разделов. Пункты - кнопки, чтобы работали Tab и пробел."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict

from StoryBookManager.gui.shell.sections import SECTIONS
from StoryBookManager.gui.shell.shortcuts import accelerator_for
from StoryBookManager.gui.shell.widgets import Tooltip
from StoryBookManager.gui.theme_tokens import COLORS, px

WIDTH_NORMAL = 200
WIDTH_COMPACT = 160


class NavigationPanel(tk.Frame):
    def __init__(self, parent, on_activate: Callable[[str], None]):
        super().__init__(parent, background=COLORS["nav_bg"])
        self._on_activate = on_activate
        self._items: Dict[str, dict] = {}
        self._selected = None
        self._project_available = False
        self._compact = False
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.configure(width=px(WIDTH_NORMAL))
        for section in SECTIONS:
            row = tk.Frame(self, background=COLORS["nav_bg"])
            row.pack(fill="x")
            row.columnconfigure(0, weight=1)
            button = ttk.Button(row, text=section.title, style="Nav.TButton",
                                command=lambda sid=section.id: self._on_activate(sid))
            button.grid(row=0, column=0, sticky="ew")
            hint = tk.Label(row, text=accelerator_for(f"section:{section.number}"),
                            background=COLORS["nav_bg"], foreground=COLORS["nav_muted"], font="SBM.Caption")
            hint.grid(row=0, column=1, sticky="e", padx=(0, px(8)))
            hint.bind("<Button-1>", lambda e, sid=section.id: self._on_activate(sid))
            Tooltip(button, lambda s=section: s.hint if self._is_blocked(s) else "")
            self._items[section.id] = {"section": section, "row": row, "button": button, "hint": hint}
        self._refresh()

    # --- Состояние ---------------------------------------------------------
    def _is_blocked(self, section) -> bool:
        return section.needs_project and not self._project_available

    @property
    def selected(self):
        return self._selected

    def button(self, section_id: str):
        return self._items[section_id]["button"]

    def is_disabled(self, section_id: str) -> bool:
        return self._is_blocked(self._items[section_id]["section"])

    def select(self, section_id: str) -> None:
        self._selected = section_id
        self._refresh()

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        self._refresh()

    def set_compact(self, compact: bool) -> None:
        """Узкое окно: колонка 160 px, подписи остаются полными."""
        self._compact = bool(compact)
        self.configure(width=px(WIDTH_COMPACT if compact else WIDTH_NORMAL))
        # В узкой колонке подсказки сочетаний прячем, чтобы подписи влезли целиком
        for item in self._items.values():
            if compact:
                item["hint"].grid_remove()
            else:
                item["hint"].grid()

    @property
    def compact(self) -> bool:
        return self._compact

    def _refresh(self) -> None:
        for sid, item in self._items.items():
            if sid == self._selected:
                style, bg = "NavSelected.TButton", COLORS["nav_selected_bg"]
            elif self._is_blocked(item["section"]):
                style, bg = "NavDisabled.TButton", COLORS["nav_bg"]
            else:
                style, bg = "Nav.TButton", COLORS["nav_bg"]
            item["button"].configure(style=style)
            item["row"].configure(background=bg)
            item["hint"].configure(background=bg)
