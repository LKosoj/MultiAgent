"""Панель фильтров списка шотов: сцена, статус ревью, состояние, флажки, поиск, режим."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Optional

from StoryBookManager.gui.shell.widgets import WrapRow
from StoryBookManager.gui.shots import model as M
from StoryBookManager.gui.shots import texts as T

QUERY_DELAY_MS = 300
REVIEW_CHOICES = ("approved", "redo", "rejected", "pending")
STATE_CHOICES = M.STATE_ORDER  # шесть состояний сервера


class FilterBar(ttk.Frame):
    def __init__(self, parent, *, on_change: Callable[[M.ListFilters], None], on_mode: Callable[[str], None],
                 on_reset: Callable[[], None]):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self._on_change = on_change
        self._on_mode = on_mode
        self._busy = False
        self._query_after = None
        self.filters = M.ListFilters()
        self._scene_ids: Dict[str, Optional[int]] = {}
        self.row = WrapRow(self)
        self.row.grid(row=0, column=0, sticky="ew")
        S = T.SHOT_TEXTS
        self.scene_var = tk.StringVar(value=S["filter_scene_all"])
        self.review_var = tk.StringVar(value=S["filter_all"])
        self.state_var = tk.StringVar(value=S["filter_all"])
        self.stale_var = tk.BooleanVar()
        self.findings_var = tk.BooleanVar()
        self.query_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="list")
        self.scene_box = self._combo(self.scene_var, [S["filter_scene_all"]], 14, S["filter_scene"])
        self.review_box = self._combo(self.review_var, [S["filter_all"]], 14, S["filter_review"])
        self.state_box = self._combo(self.state_var, [S["filter_all"]], 14, S["filter_state"])
        self.stale_check = ttk.Checkbutton(self.row, text=S["filter_stale_only"], variable=self.stale_var, command=self._changed)
        self.findings_check = ttk.Checkbutton(self.row, text=S["filter_with_findings"], variable=self.findings_var,
                                              command=self._changed)
        self.query_frame = ttk.Frame(self.row, style="Panel.TFrame")
        ttk.Label(self.query_frame, text=S["filter_search"], style="Caption.TLabel").pack(side="left", padx=(0, 4))
        self.query_entry = ttk.Entry(self.query_frame, textvariable=self.query_var, width=18)
        self.query_entry.pack(side="left")
        self.query_var.trace_add("write", lambda *a: self._query_typed())
        self.list_radio = ttk.Radiobutton(self.row, text=S["view_list"], value="list", variable=self.mode_var,
                                          command=lambda: self._on_mode("list"))
        self.grid_radio = ttk.Radiobutton(self.row, text=S["view_grid"], value="grid", variable=self.mode_var,
                                          command=lambda: self._on_mode("grid"))
        self.reset_btn = ttk.Button(self.row, text=S["reset_filters"], command=on_reset)
        for w in (self.stale_check, self.findings_check, self.query_frame, self.list_radio, self.grid_radio, self.reset_btn):
            self.row.add(w)
        self.found_var = tk.StringVar()
        ttk.Label(self, textvariable=self.found_var, style="Caption.TLabel").grid(row=1, column=0, sticky="w")
        self._fill_static()

    def _combo(self, var, values, width, label):
        frame = ttk.Frame(self.row, style="Panel.TFrame")
        ttk.Label(frame, text=label, style="Caption.TLabel").pack(side="left", padx=(0, 4))
        box = ttk.Combobox(frame, textvariable=var, values=values, width=width, state="readonly")
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda e: self._changed())
        self.row.add(frame)
        box.frame = frame
        return box

    def _fill_static(self) -> None:
        S = T.SHOT_TEXTS
        self._review_ids = {S["filter_all"]: ""}
        self._state_ids = {S["filter_all"]: ""}
        self.review_box.configure(values=list(self._review_ids))
        self.state_box.configure(values=list(self._state_ids))

    def update_facets(self, facets: Optional[dict], review_titles: dict, state_titles: dict) -> None:
        """Наполнить списки значениями из ответа (названия приходят с сервера)."""
        S = T.SHOT_TEXTS
        scenes = [S["filter_scene_all"]]
        self._scene_ids = {S["filter_scene_all"]: None}
        for entry in (facets or {}).get("scenes") or []:
            name = T.fmt("filter_scene_n", n=entry["scene"])
            scenes.append(name)
            self._scene_ids[name] = entry["scene"]
        self.scene_box.configure(values=scenes)
        self._review_ids = {S["filter_all"]: ""}
        for code in REVIEW_CHOICES:
            self._review_ids[review_titles.get(code, code)] = code
        self._state_ids = {S["filter_all"]: ""}
        for code in STATE_CHOICES:
            self._state_ids[state_titles.get(code, code)] = code
        self.review_box.configure(values=list(self._review_ids))
        self.state_box.configure(values=list(self._state_ids))

    def show(self, filters: M.ListFilters, found: int, review_titles: dict, state_titles: dict) -> None:
        """Показать текущие фильтры контроллера, не вызывая on_change."""
        self._busy = True
        try:
            self.filters = filters
            S = T.SHOT_TEXTS
            scene_name = next((n for n, v in self._scene_ids.items() if v == filters.scene), S["filter_scene_all"])
            self.scene_var.set(scene_name)
            self.review_var.set(next((n for n, v in self._review_ids.items() if v == filters.review), S["filter_all"]))
            self.state_var.set(next((n for n, v in self._state_ids.items() if v == filters.state), S["filter_all"]))
            self.stale_var.set(filters.stale_only)
            self.findings_var.set(filters.with_findings)
            if self.query_var.get().strip() != filters.query.strip():
                self.query_var.set(filters.query)
            self.found_var.set(T.fmt("found", n=found))
        finally:
            self._busy = False

    def _collect(self) -> M.ListFilters:
        return self.filters.with_(
            scene=self._scene_ids.get(self.scene_var.get()), review=self._review_ids.get(self.review_var.get(), ""),
            state=self._state_ids.get(self.state_var.get(), ""), stale_only=bool(self.stale_var.get()),
            with_findings=bool(self.findings_var.get()), query=self.query_var.get())

    def _changed(self) -> None:
        if not self._busy:
            self._on_change(self._collect())

    def _query_typed(self) -> None:
        if self._busy:
            return
        if self._query_after is not None:
            self.after_cancel(self._query_after)
        self._query_after = self.after(QUERY_DELAY_MS, self._changed)
