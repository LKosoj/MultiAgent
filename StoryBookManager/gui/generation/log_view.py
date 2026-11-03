"""Журнал прогона: цветные уровни со словом, копирование, сохранение, «Следить за концом»."""

import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable, Optional

from StoryBookManager.gui.generation import log_model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.shell.widgets import WrapRow
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px

LEVEL_TAGS = {"ERROR": "error", "CRITICAL": "error", "WARNING": "warning"}


class LogView(ttk.Frame):
    def __init__(self, parent, *, on_clear: Callable[[], None], on_copy: Callable[[str], None],
                 on_open_full: Callable[[], None], on_save_error: Callable[[str], None],
                 on_back: Callable[[], None] = lambda: None):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._on_copy, self._on_save_error = on_copy, on_save_error
        head = ttk.Frame(self, style="Panel.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(head, text=T.fmt("section_log"), style="Subtitle.TLabel").pack(side="left", padx=(0, px(PAD_INLINE)))
        self.back_btn = ttk.Button(head, text=T.fmt("log_back"), command=on_back)
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text=T.fmt("log_follow"), variable=self.follow_var).pack(side="right")
        bar = WrapRow(self)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(2), px(PAD_INLINE // 2)))
        for text, cmd in ((T.fmt("log_clear"), on_clear), (T.fmt("log_copy"), self._copy),
                          (T.fmt("log_save"), self._save)):
            bar.add(ttk.Button(bar, text=text, command=cmd))
        # «Открыть полный журнал» в строке заголовка: экономим высоту
        ttk.Button(head, text=T.TK_TEXTS["tk_open_full_log"], command=on_open_full).pack(side="right", padx=(0, px(8)))
        self.text = tk.Text(self, height=8, wrap="word", state="disabled", font="SBM.Mono", background=COLORS["card"],
                            foreground=COLORS["text_primary"], relief="flat", borderwidth=1)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=2, column=0, sticky="nsew")
        scroll.grid(row=2, column=1, sticky="ns")
        self.text.tag_configure("error", foreground=COLORS["error"])
        self.text.tag_configure("warning", foreground=COLORS["warning"])
        self.text.tag_configure("muted", foreground=COLORS["text_muted"])
        self.note = ttk.Label(self, text="", style="Caption.TLabel")
        self.note.grid(row=3, column=0, sticky="w")
        self.lines = ()

    def set_back_visible(self, on: bool) -> None:
        if on:
            self.back_btn.pack(side="right", padx=(0, px(8)))
        else:
            self.back_btn.pack_forget()

    def show(self, state: log_model.LogState) -> None:
        self.lines = state.lines
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for raw in state.lines:
            entry = log_model.parse_line(raw)
            if entry["level"] is None:
                self.text.insert("end", entry["text"] + "\n")
                continue
            tag = LEVEL_TAGS.get(entry["level"], "")
            self.text.insert("end", f"{entry['time']} ", "muted")
            self.text.insert("end", f"{entry['word']} ", tag or "muted")
            self.text.insert("end", entry["text"] + "\n", tag)
        if not state.lines:
            self.text.insert("end", T.fmt("log_empty"), "muted")
        self.text.configure(state="disabled")
        if self.follow_var.get():
            self.text.see("end")
        note = state.error or (T.fmt("log_truncated", n=len(state.lines)) if state.truncated else "")
        self.note.configure(text=T.fmt("log_error", reason=note) if state.error and state.error != T.fmt("log_deleted") else note)

    def _copy(self) -> None:
        self._on_copy(log_model.log_text(self.lines))

    def _save(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".log", initialfile="run.log")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(log_model.log_text(self.lines))
        except OSError as exc:
            self._on_save_error(str(exc))
