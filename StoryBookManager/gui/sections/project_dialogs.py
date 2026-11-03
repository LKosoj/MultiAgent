"""Окна экрана «Проекты»: создание из промпта и по форме, переименование, копия, бэкапы.

Окна только рисуют и собирают ввод; вызовы возможностей делает экран через переданные функции.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from StoryBookManager.gui.sections import projects_logic as L
from StoryBookManager.gui.shell.dialogs import _center_on
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, px


class _Modal(tk.Toplevel):
    """Модальное окно: Esc закрывает, пока оно не занято."""

    def __init__(self, parent, title: str, *, width_chars: int = 0):
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        self.resizable(False, False)
        self.closable = True
        self.box = ttk.Frame(self, padding=px(PAD_BLOCK))
        self.box.pack(fill="both", expand=True)
        self.box.columnconfigure(0, weight=1)
        self._error = tk.StringVar(value="")
        self.error_label = ttk.Label(self.box, textvariable=self._error, wraplength=px(440), justify="left",
                                     foreground=COLORS["error"])
        self._after_ids = set()
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def schedule(self, ms: int, fn: Callable) -> str:
        """after с учётом: при закрытии окна отложенные вызовы отменяются."""
        def run():
            self._after_ids.discard(after_id)
            fn()

        after_id = self.after(ms, run)
        self._after_ids.add(after_id)
        return after_id

    def cancel(self, after_id) -> None:
        self._after_ids.discard(after_id)
        try:
            self.after_cancel(after_id)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        for after_id in list(self._after_ids):
            self.cancel(after_id)
        super().destroy()

    def regrab(self) -> None:
        """После вложенного диалога подтверждения вернуть модальность."""
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def show_error(self, text: str) -> None:
        self._error.set(text)
        if text:
            self.error_label.grid()
        else:
            self.error_label.grid_remove()

    def finish_layout(self, focus=None) -> None:
        _center_on(self, self.master)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        if focus is not None:
            focus.focus_set()

    def close(self) -> None:
        if not self.closable:
            return
        try:
            self.destroy()
        except tk.TclError:
            pass


def _label(parent, text: str, row: int, *, caption: bool = False, pady=(0, 0)) -> ttk.Label:
    widget = ttk.Label(parent, text=text, style="Caption.TLabel" if caption else "TLabel",
                       wraplength=px(440), justify="left")
    widget.grid(row=row, column=0, sticky="w", pady=pady)
    return widget


def _text_area(parent, height: int, width: int = 56) -> tk.Text:
    return tk.Text(parent, height=height, width=width, wrap="word", font="SBM.Body", background=COLORS["card"],
                   foreground=COLORS["text_primary"], relief="solid", borderwidth=1,
                   highlightthickness=2, highlightcolor=COLORS["running"],
                   highlightbackground=COLORS["border_strong"], undo=True)


def _text_params(parent):
    """Язык и пределы страниц и слов; общий блок для двух окон создания."""
    frame = ttk.Frame(parent)
    language = tk.StringVar(value="Русский")
    numbers: Dict[str, tk.StringVar] = {}
    ttk.Label(frame, text=L.TXT_LANGUAGE).grid(row=0, column=0, sticky="w")
    box = ttk.Combobox(frame, textvariable=language, values=("Русский", "English"), state="readonly", width=12)
    box.grid(row=0, column=1, sticky="w", padx=px(PAD_INLINE), pady=px(2))
    for i, (key, label, default) in enumerate((
            ("pages_min", L.TXT_PAGES_MIN, "9"), ("pages_max", L.TXT_PAGES_MAX, "12"),
            ("words_per_page_min", L.TXT_WORDS_MIN, "400"), ("words_per_page_max", L.TXT_WORDS_MAX, "450")),
            start=1):
        var = tk.StringVar(value=default)
        numbers[key] = var
        ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w")
        ttk.Entry(frame, textvariable=var, width=8).grid(row=i, column=1, sticky="w", padx=px(PAD_INLINE),
                                                          pady=px(2))
    return frame, language, box, numbers


def _text_params_dict(language: tk.StringVar, numbers: Dict[str, tk.StringVar]) -> dict:
    data = {"language": "en" if language.get() == "English" else "ru"}
    for key, var in numbers.items():
        number = L.int_or_none(var.get())
        data[key] = number if number is not None else var.get()
    return data


def _buttons(parent, row: int, cancel_cmd, ok_text: str, ok_cmd):
    line = ttk.Frame(parent)
    line.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
    cancel = ttk.Button(line, text=L.TXT_CANCEL, command=cancel_cmd)
    cancel.pack(side="left")
    ok = ttk.Button(line, text=ok_text, style="Accent.TButton", command=ok_cmd)
    ok.pack(side="right")
    return cancel, ok


class CreateFromPromptDialog(_Modal):
    """Идея сказки, язык и параметры текста. Кнопка неактивна, пока промпт короче 10 символов."""

    def __init__(self, parent, on_submit: Callable[[dict], None]):
        super().__init__(parent, L.TXT_PROMPT_TITLE)
        self._on_submit = on_submit
        _label(self.box, L.TXT_PROMPT_FIELD, 0)
        self.prompt = _text_area(self.box, 7)
        self.prompt.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        self.counter = ttk.Label(self.box, text="", style="Caption.TLabel")
        self.counter.grid(row=2, column=0, sticky="e")
        _label(self.box, L.TXT_PROMPT_HINT, 3, caption=True)
        _label(self.box, L.TXT_PROMPT_LLM, 4, caption=True)
        self._params_open = False
        self.params_toggle = ttk.Button(self.box, text=f"{L.TXT_TEXT_PARAMS}…", command=self._toggle_params)
        self.params_toggle.grid(row=5, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.params = ttk.Frame(self.box)
        self.params.grid(row=6, column=0, sticky="ew", pady=(px(4), 0))
        self.params.grid_remove()
        params, self.language, self.language_box, self.numbers = _text_params(self.params)
        params.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Label(self.box, text="", foreground=COLORS["running"], wraplength=px(440), justify="left")
        self.progress.grid(row=7, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.error_label.grid(row=8, column=0, sticky="w")
        self.show_error("")
        self.cancel_btn, self.ok_btn = _buttons(self.box, 9, self.close, L.TXT_CREATE_AND_OPEN, self._submit)
        self.prompt.bind("<<Modified>>", self._on_modified)
        self.prompt.bind("<Control-Return>", lambda e: self._submit() or "break")
        self._update_state()
        self.finish_layout(self.prompt)

    def _toggle_params(self) -> None:
        self._params_open = not self._params_open
        if self._params_open:
            self.params.grid()
        else:
            self.params.grid_remove()

    def _on_modified(self, _event=None) -> None:
        if self.prompt.edit_modified():
            self.prompt.edit_modified(False)
            self._update_state()

    def text(self) -> str:
        return self.prompt.get("1.0", "end").strip()

    def _update_state(self) -> None:
        size = len(self.text())
        self.counter.configure(text=f"{size} / {L.PROMPT_MAX}")
        ok = L.validate_prompt(self.text()) and self.closable
        self.ok_btn.configure(state="normal" if ok else "disabled")

    def params_dict(self) -> dict:
        return {"prompt": self.text(), **_text_params_dict(self.language, self.numbers)}

    def _submit(self) -> None:
        if not (L.validate_prompt(self.text()) and self.closable):
            return
        self.show_error("")
        self._on_submit(self.params_dict())

    def set_busy(self, busy: bool, progress: str = "") -> None:
        """Пока идёт задание, окно закрыть нельзя, поля заблокированы."""
        self.closable = not busy
        self.prompt.configure(state="disabled" if busy else "normal")
        for widget in (self.cancel_btn, self.params_toggle, self.language_box):
            widget.configure(state="disabled" if busy else ("readonly" if widget is self.language_box else "normal"))
        self.progress.configure(text=progress if busy else "")
        self._update_state()


class CreateByFormDialog(_Modal):
    """Название, описание, жанр, возраст. Идентификатор подсказывается по названию с задержкой."""

    SUGGEST_DELAY_MS = 400

    def __init__(self, parent, on_submit: Callable[[dict], None],
                 on_suggest: Callable[[str, Callable[[Optional[str]], None]], None]):
        super().__init__(parent, L.TXT_FORM_TITLE)
        self._on_submit = on_submit
        self._on_suggest = on_suggest
        self._after = None
        self._seq = 0
        self.vars: Dict[str, tk.StringVar] = {k: tk.StringVar() for k in ("title", "genre", "age")}
        _label(self.box, L.TXT_FIELD_TITLE, 0)
        self.title_entry = ttk.Entry(self.box, textvariable=self.vars["title"], width=58)
        self.title_entry.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        self.id_var = tk.StringVar(value=f"{L.TXT_ID_LABEL}: —")
        ttk.Label(self.box, textvariable=self.id_var, style="Caption.TLabel", wraplength=px(440),
                  justify="left").grid(row=2, column=0, sticky="w")
        _label(self.box, L.TXT_ID_HINT, 3, caption=True)
        _label(self.box, L.TXT_FIELD_DESCRIPTION, 4, pady=(px(PAD_INLINE), 0))
        self.description = _text_area(self.box, 4)
        self.description.grid(row=5, column=0, sticky="ew", pady=(px(4), 0))
        row = ttk.Frame(self.box)
        row.grid(row=6, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=L.TXT_FIELD_GENRE).grid(row=0, column=0, sticky="w")
        ttk.Label(row, text=L.TXT_FIELD_AGE).grid(row=0, column=1, sticky="w", padx=(px(PAD_INLINE), 0))
        ttk.Entry(row, textvariable=self.vars["genre"]).grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        ttk.Entry(row, textvariable=self.vars["age"]).grid(row=1, column=1, sticky="ew", padx=(px(PAD_INLINE), 0),
                                                           pady=(px(4), 0))
        params, self.language, self.language_box, self.numbers = _text_params(self.box)
        params.grid(row=7, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.error_label.grid(row=8, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.show_error("")
        self.cancel_btn, self.ok_btn = _buttons(self.box, 9, self.close, L.TXT_CREATE, self._submit)
        for var in self.vars.values():
            var.trace_add("write", lambda *a: self._update_state())
        self.vars["title"].trace_add("write", lambda *a: self._schedule_suggest())
        self.description.bind("<<Modified>>", self._on_modified)
        self._update_state()
        self.finish_layout(self.title_entry)

    def _on_modified(self, _event=None) -> None:
        if self.description.edit_modified():
            self.description.edit_modified(False)
            self._update_state()

    def values(self) -> dict:
        return {"title": self.vars["title"].get().strip(),
                "description": self.description.get("1.0", "end").strip(),
                "genre": self.vars["genre"].get().strip(), "target_age": self.vars["age"].get().strip(),
                **_text_params_dict(self.language, self.numbers)}

    def _update_state(self) -> None:
        v = self.values()
        ready = L.form_ready(v["title"], v["description"], v["genre"], v["target_age"]) and self.closable
        self.ok_btn.configure(state="normal" if ready else "disabled")

    def _schedule_suggest(self) -> None:
        if self._after is not None:
            self.cancel(self._after)
            self._after = None
        self._seq += 1
        hint = self.vars["title"].get().strip()
        if not hint:
            self.id_var.set(f"{L.TXT_ID_LABEL}: —")
            return
        seq = self._seq
        self._after = self.schedule(self.SUGGEST_DELAY_MS, lambda: self._ask(hint, seq))

    def _ask(self, hint: str, seq: int) -> None:
        self._after = None

        def got(project_id: Optional[str]) -> None:
            if seq == self._seq and self.winfo_exists():
                self.id_var.set(f"{L.TXT_ID_LABEL}: {project_id or '—'}")

        self._on_suggest(hint, got)

    def _submit(self) -> None:
        if str(self.ok_btn.cget("state")) == "disabled":
            return
        self.show_error("")
        data = self.values()
        data["id_hint"] = data["title"]
        self._on_submit(data)

    def set_busy(self, busy: bool) -> None:
        self.closable = not busy
        self._update_state()


class RenameDialog(_Modal):
    def __init__(self, parent, current: str, on_submit: Callable[[str], None]):
        super().__init__(parent, L.TXT_RENAME_TITLE)
        self._on_submit = on_submit
        self.var = tk.StringVar(value=current)
        _label(self.box, L.TXT_FIELD_TITLE, 0)
        self.entry = ttk.Entry(self.box, textvariable=self.var, width=50)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        self.error_label.grid(row=2, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.show_error("")
        self.cancel_btn, self.ok_btn = _buttons(self.box, 3, self.close, L.TXT_SAVE, self._submit)
        self.var.trace_add("write", lambda *a: self._update_state())
        self.entry.bind("<Return>", lambda e: self._submit())
        self._update_state()
        self.finish_layout(self.entry)
        self.entry.selection_range(0, "end")

    def _update_state(self) -> None:
        self.ok_btn.configure(state="normal" if self.var.get().strip() else "disabled")

    def _submit(self) -> None:
        title = self.var.get().strip()
        if title:
            self.show_error("")
            self._on_submit(title)


class DuplicateDialog(_Modal):
    """Копия проекта: идентификатор необязателен, сервер предложит «{id}_copy»."""

    def __init__(self, parent, project_id: str, on_submit: Callable[[str], None]):
        super().__init__(parent, L.TXT_DUPLICATE_TITLE)
        self._on_submit = on_submit
        self.var = tk.StringVar()
        _label(self.box, L.TXT_DUPLICATE_ID, 0)
        self.entry = ttk.Entry(self.box, textvariable=self.var, width=50)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        _label(self.box, L.TXT_DUPLICATE_ID_HINT.format(id=project_id), 2, caption=True)
        self.error_label.grid(row=3, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.show_error("")
        self.cancel_btn, self.ok_btn = _buttons(self.box, 4, self.close, L.TXT_DUPLICATE, self._submit)
        self.entry.bind("<Return>", lambda e: self._submit())
        self.finish_layout(self.entry)

    def _submit(self) -> None:
        self.show_error("")
        self._on_submit(self.var.get().strip())


class BackupsDialog(_Modal):
    """Список бэкапов. Без выбранного проекта показаны бэкапы всех проектов."""

    def __init__(self, parent, selected_id: Optional[str], *,
                 load: Callable[[str, Callable[[Optional[List[dict]], str], None]], None],
                 create: Callable[[str, str, Callable[[bool, str, bool], None]], None],
                 restore: Callable[[dict, Callable[[bool, str], None]], None]):
        super().__init__(parent, L.TXT_BACKUPS_TITLE)
        self.selected_id = selected_id or ""
        self._load, self._create, self._restore = load, create, restore
        self._rows: List[dict] = []
        self._token = 0
        all_default, _ = L.default_backup_scope(selected_id)
        self.all_var = tk.BooleanVar(value=all_default)
        row = 0
        if self.selected_id:
            self.all_check = ttk.Checkbutton(self.box, text=L.TXT_BACKUPS_ALL, variable=self.all_var,
                                             command=self.reload)
            self.all_check.grid(row=row, column=0, sticky="w")
            row += 1
        self.tree = ttk.Treeview(self.box, columns=("date", "project", "size", "reason"), show="headings",
                                 height=9, selectmode="browse")
        for key, text, width in (("date", L.TXT_BACKUP_DATE, 130), ("project", L.TXT_BACKUP_PROJECT, 150),
                                 ("size", L.TXT_BACKUP_SIZE, 80), ("reason", L.TXT_BACKUP_REASON, 190)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=px(width), anchor="w", stretch=key == "reason")
        self.tree.grid(row=row, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_state())
        self.tree.bind("<Double-1>", lambda e: self._restore_selected())
        row += 1
        self.empty_var = tk.StringVar(value=L.TXT_BACKUPS_LOADING)
        ttk.Label(self.box, textvariable=self.empty_var, style="Caption.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        self.warn_var = tk.StringVar(value="")
        ttk.Label(self.box, textvariable=self.warn_var, foreground=COLORS["warning"], wraplength=px(560),
                  justify="left").grid(row=row, column=0, sticky="w")
        row += 1
        self.error_label.grid(row=row, column=0, sticky="w")
        self.show_error("")
        row += 1
        self.note_var = tk.StringVar()
        if self.selected_id:
            line = ttk.Frame(self.box)
            line.grid(row=row, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
            line.columnconfigure(0, weight=1)
            note = ttk.Entry(line, textvariable=self.note_var)
            note.grid(row=0, column=0, sticky="ew")
            ttk.Label(line, text=L.TXT_BACKUP_NOTE, style="Caption.TLabel").grid(row=1, column=0, sticky="w")
            self.create_btn = ttk.Button(line, text=L.TXT_BACKUP_CREATE, command=self._create_clicked)
            self.create_btn.grid(row=0, column=1, padx=(px(PAD_INLINE), 0))
            row += 1
        foot = ttk.Frame(self.box)
        foot.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        ttk.Button(foot, text=L.TXT_CLOSE, command=self.close).pack(side="left")
        self.restore_btn = ttk.Button(foot, text=L.TXT_BACKUP_RESTORE, style="Accent.TButton",
                                      command=self._restore_selected)
        self.restore_btn.pack(side="right")
        self._update_state()
        self.finish_layout(self.tree)
        self.reload()

    def _scope(self) -> str:
        return "" if (not self.selected_id or self.all_var.get()) else self.selected_id

    def reload(self) -> None:
        self._token += 1
        token = self._token
        self.tree.delete(*self.tree.get_children())
        self._rows = []
        self.empty_var.set(L.TXT_BACKUPS_LOADING)
        self.tree.configure(displaycolumns=("date", "project", "size", "reason") if not self._scope()
                            else ("date", "size", "reason"))

        def got(rows: Optional[List[dict]], error: str) -> None:
            if token != self._token or not self.winfo_exists():
                return
            if rows is None:
                self.show_error(error)
                rows = []
            self._rows = rows
            for i, b in enumerate(rows):
                reason = b.get("reason_title") or ""
                if b.get("note"):
                    reason += f" · {b['note']}"
                self.tree.insert("", "end", iid=str(i), values=(
                    L.format_datetime(b.get("created_at")), b.get("project_id", ""),
                    L.format_bytes(b.get("size_bytes")), reason))
            self.empty_var.set("" if rows else L.TXT_BACKUPS_EMPTY)
            self._update_state()

        self._load(self._scope(), got)

    def _selected(self) -> Optional[dict]:
        sel = self.tree.selection()
        return self._rows[int(sel[0])] if sel else None

    def _update_state(self) -> None:
        self.restore_btn.configure(state="normal" if self._selected() and self.closable else "disabled")

    def _create_clicked(self) -> None:
        self.show_error("")
        self.warn_var.set("")
        self.create_btn.configure(state="disabled")

        def done(ok: bool, error: str, consistent: bool) -> None:
            if not self.winfo_exists():
                return
            self.create_btn.configure(state="normal")
            if not ok:
                self.show_error(error)
                return
            if not consistent:
                self.warn_var.set(L.TXT_BACKUP_INCONSISTENT)
            self.note_var.set("")
            self.reload()

        self._create(self.selected_id, self.note_var.get().strip(), done)

    def _restore_selected(self) -> None:
        backup = self._selected()
        if backup is None or not self.closable:
            return
        self.show_error("")

        def done(ok: bool, error: str) -> None:
            if not self.winfo_exists():
                return
            if ok:
                self.close()
            else:
                self.show_error(error)

        self._restore(backup, done)
        if self.winfo_exists():
            self.regrab()
