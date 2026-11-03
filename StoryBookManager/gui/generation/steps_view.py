"""Список шагов прогона: Treeview со статусом словом и временем."""

from tkinter import ttk

from StoryBookManager.gui.generation import steps_model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.theme_tokens import px


class StepsView(ttk.Frame):
    def __init__(self, parent, on_pick=None):
        super().__init__(parent, style="Panel.TFrame")
        self._on_pick = on_pick
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text=T.fmt("section_steps"), style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        self.tree = ttk.Treeview(self, columns=("step", "status", "time"), show="headings", height=6, selectmode="browse")
        for col, text, width, stretch in (("step", T.fmt("steps_col_step"), 200, True),
                                          ("status", T.fmt("steps_col_status"), 110, False),
                                          ("time", T.fmt("steps_col_time"), 80, False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=px(width), minwidth=px(60), stretch=stretch)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.empty = ttk.Label(self, text="", style="Caption.TLabel")
        self.empty.grid(row=2, column=0, sticky="w")
        actions = ttk.Frame(self, style="Panel.TFrame")
        actions.grid(row=0, column=0, columnspan=2, sticky="e")  # в строке заголовка: экономим высоту
        self.from_btn = ttk.Button(actions, text=T.fmt("step_from_here"), command=lambda: self._pick("from_step"))
        self.from_btn.pack(side="left")
        self.only_btn = ttk.Button(actions, text=T.fmt("step_only_this"), command=lambda: self._pick("single_step"))
        self.only_btn.pack(side="left", padx=(px(8), 0))
        self.follow = {"follow": True, "last_current": None}
        self.rows = []
        # пользователь сам крутит список: колесо или полоса прокрутки
        self._user_scrolled = False
        for w in (self.tree, scroll):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                w.bind(ev, self._mark_scrolled, add="+")
        scroll.bind("<ButtonPress-1>", self._mark_scrolled, add="+")

    def _mark_scrolled(self, _event=None) -> None:
        self._user_scrolled = True

    def _pick(self, mode: str) -> None:
        sel = self.tree.selection()
        if sel and self._on_pick:
            row = next((r for r in self.rows if str(r["index"]) == sel[0]), None)
            if row:
                self._on_pick(mode, row["id"])

    def set_enabled(self, enabled: bool) -> None:
        for btn in (self.from_btn, self.only_btn):
            btn.configure(state="normal" if enabled else "disabled")

    def show(self, rows) -> None:
        """rows - результат steps_model.step_rows; текущий шаг отмечается стрелкой и виден в списке."""
        self.rows = rows
        current = None
        existing = list(self.tree.get_children())
        wanted = [str(r["index"]) for r in rows]
        if existing != wanted:
            self.tree.delete(*existing)
            existing = []
        for row in rows:
            title = ("▶ " if row["current"] else "") + f"{row['index']}. {row['title']}"
            if row["error"]:
                title += f" — {row['error'].splitlines()[0]}"
            values = (title, row["word"], row["duration"] or "—")
            iid = str(row["index"])
            if iid in existing:
                self.tree.item(iid, values=values)  # обновляем на месте: выделение и прокрутка остаются
            else:
                self.tree.insert("", "end", iid=iid, values=values)
            if row["current"]:
                current = iid
        self.empty.configure(text="" if rows else T.TK_TEXTS["tk_steps_empty"])
        if rows:
            self.empty.grid_remove()
        else:
            self.empty.grid()
        self.follow = steps_model.next_follow(self.follow, current, self._user_scrolled)
        self._user_scrolled = False
        if current and self.follow["follow"]:
            self.tree.see(current)
