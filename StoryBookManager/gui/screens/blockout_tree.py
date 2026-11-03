"""Дерево «сцена -> цепочка -> шот» на ttk.Treeview; состояние шота - словом и цветным тегом."""

from tkinter import ttk

from StoryBookManager.gui.screens import blockout_model as bm
from StoryBookManager.gui.theme_tokens import COLORS, px

STATE_TAGS = {"rendered": "success", "stale": "warning", "missing": "text_muted", "junction_error": "error"}


class BlockoutTree(ttk.Frame):
    def __init__(self, parent, on_select, on_enter=None):
        super().__init__(parent, style="Panel.TFrame")
        self.on_select = on_select
        self.on_enter = on_enter
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, columns=("state",), show="tree headings", selectmode="extended", height=14)
        self.tree.heading("#0", text="Шот")
        self.tree.heading("state", text="Состояние")
        self.tree.column("#0", width=px(190))
        self.tree.column("state", width=px(150), stretch=False)
        for state, tone in STATE_TAGS.items():
            self.tree.tag_configure(state, foreground=COLORS[tone])
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.selection = dict(bm.EMPTY_SELECTION)
        self.ordered = []
        self._busy = False
        self.tree.bind("<<TreeviewSelect>>", self._changed)
        self.tree.bind("<Return>", lambda e: self.on_enter and self.on_enter())

    def fill(self, state):
        self._busy = True
        self.tree.delete(*self.tree.get_children())
        self.ordered = bm.ordered_keys(state)
        parents = {0: ""}
        for row in bm.tree_rows(state):
            parent = parents.get(row["depth"] - 1, "") if row["depth"] else ""
            if row["kind"] == "shot":
                text = row["label"]
                state_text = row["stateTitle"] + (" · " + ", ".join(row["marks"]) if row["marks"] else "")
                self.tree.insert(parent, "end", iid=row["id"], text=text, values=(state_text,), tags=(row["state"],))
            else:
                suffix = "" if row["kind"] != "chain" or row["blendExists"] else ""
                self.tree.insert(parent, "end", iid=row["id"], text=row["label"] + suffix, open=True, values=("",))
                parents[row["depth"]] = row["id"]
        keep = [f"shot:{k}" for k in self.selection["keys"] if k in self.ordered]
        self.selection = bm.prune_selection(self.selection, self.ordered)
        if keep:
            self.tree.selection_set(keep)
        self._busy = False

    def _changed(self, _e):
        if self._busy:
            return
        keys = [i[len("shot:"):] for i in self.tree.selection() if i.startswith("shot:")]
        ordered_keys = [k for k in self.ordered if k in keys]
        focus = self.tree.focus()
        focus_key = focus[len("shot:"):] if focus.startswith("shot:") else (ordered_keys[-1] if ordered_keys else None)
        self.selection = {"keys": ordered_keys, "anchor": focus_key, "focus": focus_key if focus_key in ordered_keys else (ordered_keys[-1] if ordered_keys else None)}
        self.on_select(self.selection)

    def select_keys(self, keys):
        self.tree.selection_set([f"shot:{k}" for k in keys if k in self.ordered])
