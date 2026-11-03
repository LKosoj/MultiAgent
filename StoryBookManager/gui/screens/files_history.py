"""Окно «История» файла: версии, просмотр, сравнение, откат."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.screens.calls import CAP_ARTIFACT_DIFF, CAP_ARTIFACT_HISTORY, CAP_ARTIFACT_READ, CAP_ARTIFACT_RESTORE
from StoryBookManager.gui.screens.common_model import format_bytes, format_created_at
from StoryBookManager.gui.screens.files_texts import FILES_TEXTS as T
from StoryBookManager.gui.screens.files_model import content_to_text as to_json_text
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shots.model import error_parts, result_error_text
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, px


class HistoryWindow(tk.Toplevel):
    def __init__(self, owner, calls, project_id, state, on_restored):
        super().__init__(owner)
        self.calls, self.project_id, self.state, self.on_restored, self.owner = calls, project_id, state, on_restored, owner
        self.title(T["history_title"].format(name=state.title))
        self.transient(owner.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        self.ask_confirm = lambda spec: ConfirmDialog.ask(self, spec)
        self.versions = []
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(box, columns=("size", "reason"), show="tree headings", height=8, selectmode="browse")
        self.table.heading("#0", text=T["col_date"])
        self.table.heading("size", text=T["col_size"])
        self.table.heading("reason", text=T["col_reason"])
        self.table.column("#0", width=px(190))
        self.table.column("size", width=px(90))
        self.table.column("reason", width=px(140))
        self.table.grid(row=0, column=0, sticky="nsew")
        row = ttk.Frame(box)
        row.grid(row=1, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        ttk.Button(row, text=T["view"], command=self.view).pack(side="left")
        ttk.Button(row, text=T["diff"], command=self.diff).pack(side="left", padx=px(PAD_INLINE))
        ttk.Button(row, text=T["restore"], command=self.restore).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(box, textvariable=self.status, style="Caption.TLabel").grid(row=2, column=0, sticky="w")
        self.out = tk.Text(box, height=12, width=80, wrap="word", background=COLORS["card"], state="disabled")
        self.out.grid(row=3, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        calls.call(CAP_ARTIFACT_HISTORY, {"project_id": project_id, "key": state.key}, on_success=self._loaded,
                   on_error=lambda r: self.status.set(result_error_text(r)))

    def _loaded(self, result):
        self.versions = result.get("items", [])
        for v in self.versions:
            self.table.insert("", "end", iid=v["version_id"], text=format_created_at(v["created_at"]),
                              values=(format_bytes(v["size"]), v.get("reason_title", "")))
        if not self.versions:
            self.status.set(T["history_empty"])

    def _selected(self):
        sel = self.table.selection()
        return next((v for v in self.versions if sel and v["version_id"] == sel[0]), None)

    def _write(self, text):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.configure(state="disabled")

    def view(self):
        v = self._selected()
        if not v:
            return
        self.calls.call(CAP_ARTIFACT_READ, {"project_id": self.project_id, "key": self.state.key, "version_id": v["version_id"]},
                        on_success=lambda r: (self.status.set(T["version_view"].format(date=format_created_at(v["created_at"]))),
                                              self._write(r.get("text") or to_json_text(r.get("content")))),
                        on_error=lambda r: self.status.set(result_error_text(r)))

    def diff(self):
        v = self._selected()
        if not v:
            return
        self.calls.call(CAP_ARTIFACT_DIFF, {"project_id": self.project_id, "key": self.state.key, "version_id": v["version_id"]},
                        on_success=self._diff_done, on_error=lambda r: self.status.set(result_error_text(r)))

    def _diff_done(self, r):
        if r.get("identical"):
            self._write(T["diff_same"])
            return
        lines = [f"{c.get('label') or c.get('pointer')} · {c.get('before')!r} → {c.get('after')!r}" for c in r.get("changes", [])]
        if r.get("truncated"):
            lines.append(T["diff_truncated"])
        self._write("\n".join(lines))

    def restore(self, allow_invalid=False):
        v = self._selected()
        if not v:
            return
        if not allow_invalid:
            date = format_created_at(v["created_at"])
            if not self.ask_confirm(ConfirmSpec(T["restore_title"].format(date=date), T["restore_body"], T["restore_go"])):
                return
        payload = {"project_id": self.project_id, "key": self.state.key, "version_id": v["version_id"],
                   "base_digest": self.state.digest}
        if allow_invalid:
            payload["allow_invalid"] = True
        self.calls.call(CAP_ARTIFACT_RESTORE, payload, on_success=lambda r: self._restored(r),
                        on_error=lambda r: self._restore_failed(r))

    def _restored(self, _r):
        notify.success(T["restore_done"])
        self.on_restored()
        self.destroy()

    def _restore_failed(self, result):
        code, _reason, _msg, details = error_parts(result)
        if code == "invalid_argument":
            if self.ask_confirm(ConfirmSpec(T["restore_invalid_title"], T["restore_invalid_body"], T["restore_go"])):
                self.restore(allow_invalid=True)
            return
        if code == "conflict":
            self.on_restored()
        self.status.set(result_error_text(result))
