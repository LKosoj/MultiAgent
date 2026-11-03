"""Прошлые прогоны: сворачиваемая таблица."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.theme_tokens import px


class HistoryView(ttk.Frame):
    def __init__(self, parent, on_open_log=None):
        super().__init__(parent, style="Panel.TFrame")
        self._on_open_log = on_open_log
        self._rows = []
        self.columnconfigure(0, weight=1)
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text=T.fmt("section_history"), variable=self.open_var, command=self._toggle,
                        style="Toolbutton").grid(row=0, column=0, sticky="w")
        self.box = ttk.Frame(self, style="Panel.TFrame")
        self.box.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.box, columns=("start", "duration", "mode", "where", "result"), show="headings",
                                 height=4, selectmode="browse")
        for col, key, width in (("start", "history_col_start", 130), ("duration", "history_col_duration", 90), ("mode", "history_col_mode", 140),
                                ("where", "history_col_where", 80), ("result", "history_col_result", 150)):
            self.tree.heading(col, text=T.fmt(key))
            self.tree.column(col, width=px(width), minwidth=px(50), stretch=col == "result")
        self.tree.grid(row=0, column=0, sticky="ew")
        self.log_btn = ttk.Button(self.box, text=T.fmt("history_open_log"), command=self._open_log)
        self.log_btn.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        self.empty = ttk.Label(self.box, text=T.fmt("history_empty"), style="Caption.TLabel")

    def _toggle(self) -> None:
        if self.open_var.get():
            self.box.grid(row=1, column=0, sticky="ew")
        else:
            self.box.grid_remove()

    def _open_log(self) -> None:
        sel = self.tree.selection()
        if sel and self._on_open_log:
            self._on_open_log(self._rows[int(sel[0])].get("run_id"))

    def show(self, rows) -> None:
        self._rows = list(rows)
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(rows):
            result = r["result"] + (f" ({r['note']})" if r["note"] else "")
            self.tree.insert("", "end", iid=str(i), values=(r["started"], r["duration"], r["mode"], r["where"], result))
        if rows:
            self.empty.grid_remove()
        else:
            self.empty.grid(row=2, column=0, sticky="w")
