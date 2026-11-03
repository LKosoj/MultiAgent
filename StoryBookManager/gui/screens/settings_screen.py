"""Экран «Настройки»: папки, бэкапы, журнал, параметры окна Tk."""

import tkinter as tk
from tkinter import filedialog, ttk

from custom_tools.storybook.studio import app_settings as studio_settings
from custom_tools.storybook.studio.errors import StudioError
from StoryBookManager.gui.screens import settings_model as sm
from StoryBookManager.gui.screens.calls import (
    CAP_APP_SETTINGS_CHECK_PATH, CAP_APP_SETTINGS_GET, CAP_APP_SETTINGS_RESET, CAP_APP_SETTINGS_UPDATE, StudioCalls,
)
from StoryBookManager.gui.screens.settings_texts import LOG_LEVELS, SETTINGS_TEXTS as T
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shell.widgets import PanelHeader
from StoryBookManager.gui.shots.model import result_error_text
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

CHECK_PAUSE_MS = 400
SCALES = ("100", "125", "150", "200")


class SettingsScreen(ttk.Frame, SectionAdapter):
    def __init__(self, parent, bridge, on_applied=None):
        ttk.Frame.__init__(self, parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.calls = StudioCalls(bridge)
        self.on_applied = on_applied
        self.ask_confirm = lambda spec: ConfirmDialog.ask(self.winfo_toplevel(), spec)
        self.pick_dir = lambda initial: filedialog.askdirectory(initialdir=initial or None)
        self.snapshot = None
        self.base = {}
        self.vars = {}
        self.entries = {}
        self.notes = {}
        self.check_labels = {}
        self._marks = {}
        self._check_jobs = {}
        self.digest = ""
        self.columnconfigure(0, weight=1)
        PanelHeader(self, T["title"]).grid(row=0, column=0, sticky="ew")
        self.banner_var = tk.StringVar()
        self.banner = ttk.Label(self, textvariable=self.banner_var, wraplength=px(680), justify="left", foreground=COLORS["error"])
        self.banner.grid(row=1, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.warn_var = tk.StringVar()
        ttk.Label(self, textvariable=self.warn_var, wraplength=px(680), justify="left", style="Caption.TLabel").grid(
            row=2, column=0, sticky="w")
        self.form = ttk.Frame(self, style="Panel.TFrame")
        self.form.grid(row=3, column=0, sticky="nw", pady=(px(PAD_BLOCK), 0))
        self._build_form()
        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.grid(row=4, column=0, sticky="w", pady=(px(PAD_BLOCK), 0))
        self.save_btn = ttk.Button(bar, text=T["save"], command=self.save)
        self.save_btn.pack(side="left")
        self.reset_btn = ttk.Button(bar, text=T["reset"], command=self.reset)
        self.reset_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
        self.retry_btn = ttk.Button(bar, text=T["retry"], command=self.load)
        self.refresh_btn = ttk.Button(bar, text=T["refresh"], command=self.load)
        self._loading = False  # идёт заполнение формы из ответа: проверку пути не планируем

    # --- форма -----------------------------------------------------------------
    def _build_form(self):
        rows = [("projects_directory", T["projects"], True), ("backup_directory", T["backups"], True),
                ("logs_directory", T["logs"], True), ("max_backup_files", T["max_backups"], False),
                ("log_level", T["log_level"], False), ("ui_scale", T["ui_scale"], False),
                ("auto_save_interval", T["autosave"], False)]
        self.form.columnconfigure(1, weight=1)
        r = 0
        for key, label, is_path in rows:
            if key == "ui_scale":
                ttk.Label(self.form, text=T["tk_group"], style="Subtitle.TLabel").grid(
                    row=r, column=0, columnspan=3, sticky="w", pady=(px(PAD_BLOCK), 0))
                r += 1
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Label(self.form, text=label).grid(row=r, column=0, sticky="w", padx=(0, px(PAD_PANEL)), pady=(px(PAD_INLINE), 0))
            if key == "log_level":
                w = ttk.Combobox(self.form, textvariable=var, values=[l for _, l in LOG_LEVELS], state="readonly", width=22)
            elif key == "ui_scale":
                w = ttk.Combobox(self.form, textvariable=var, values=SCALES, state="readonly", width=8)
            elif key in ("max_backup_files", "auto_save_interval"):
                w = ttk.Spinbox(self.form, textvariable=var, from_=0 if key == "auto_save_interval" else 1,
                                to=3600 if key == "auto_save_interval" else 100, width=8)
            else:
                w = ttk.Entry(self.form, textvariable=var, width=52)
            w.grid(row=r, column=1, sticky="w", pady=(px(PAD_INLINE), 0))
            self.entries[key] = w
            if is_path:
                ttk.Button(self.form, text=T["choose"], command=lambda k=key: self._choose(k)).grid(
                    row=r, column=2, padx=(px(PAD_INLINE), 0), pady=(px(PAD_INLINE), 0))
                kind = dict(sm.PATH_FIELDS)[key]
                var.trace_add("write", lambda *_a, k=key, kd=kind: self._schedule_check(k, kd))
                w.bind("<FocusOut>", lambda _e, k=key, kd=kind: self._check_now(k, kd), add="+")
                r += 1
                note = tk.StringVar()
                self.notes[key] = note
                ttk.Label(self.form, textvariable=note, style="Caption.TLabel", wraplength=px(560), justify="left").grid(
                    row=r, column=1, columnspan=2, sticky="w")
            r += 1

    def _choose(self, key):
        folder = self.pick_dir(self.vars[key].get())
        if folder:
            self.vars[key].set(folder)

    def _schedule_check(self, key, kind):
        if self.snapshot is None or self._loading:
            return
        job = self._check_jobs.pop(key, None)
        if job:
            self.after_cancel(job)
        self._check_jobs[key] = self.after(CHECK_PAUSE_MS, lambda: self._check_now(key, kind))

    def _check_now(self, key, kind):
        job = self._check_jobs.pop(key, None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        path = self.vars[key].get().strip()
        if not sm.should_check_path(path) or self.snapshot is None:
            return
        self.calls.call(CAP_APP_SETTINGS_CHECK_PATH, {"kind": kind, "path": path},
                        on_success=lambda r, k=key, p=path: self._on_check(k, p, r),
                        on_error=lambda r, k=key: self.notes[k].set(result_error_text(r)), key=f"check:{key}")

    def _on_check(self, key, path, result):
        if self.vars[key].get().strip() != path:
            return
        text = sm.check_text(result.get("state")) + (f" ({result['resolved']})" if result.get("resolved") else "")
        # пометки «задана переменной окружения» / «по умолчанию» остаются рядом с результатом проверки
        self.notes[key].set(" · ".join(x for x in (text, self._marks.get(key, "")) if x))

    # --- загрузка --------------------------------------------------------------
    def on_show(self):
        if self.snapshot is None:
            self.load()

    def on_refresh(self):
        self.load()

    def load(self):
        self.banner_var.set(T["loading"])
        self.retry_btn.pack_forget()
        self.calls.call(CAP_APP_SETTINGS_GET, {}, on_success=self._loaded,
                        on_error=lambda r: self._load_failed(result_error_text(r)), key="settings_get")

    def _load_failed(self, reason):
        self.banner_var.set(T["load_failed"].format(reason=reason.rstrip(".")))
        self.retry_btn.pack(side="left", padx=(px(PAD_INLINE), 0))

    def _loaded(self, snapshot):
        self.snapshot = snapshot
        self.digest = snapshot.get("digest", "")
        self.banner_var.set("")
        self.refresh_btn.pack_forget()
        form = sm.form_from_snapshot(snapshot)
        tk_values = studio_settings.tk_settings_get()
        form["ui_scale"] = str(tk_values["ui_scale"])
        form["auto_save_interval"] = str(tk_values["auto_save_interval"])
        self.base = dict(form)
        self._loading = True
        try:
            for key, value in form.items():
                self.vars[key].set(value)
        finally:
            self._loading = False
        for job in self._check_jobs.values():
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._check_jobs.clear()
        warnings = list(snapshot.get("warnings") or [])
        self.warn_var.set("\n".join(warnings))
        locked = snapshot.get("projects_directory_locked_reason")
        for key, _kind in sm.PATH_FIELDS:
            setting = snapshot["settings"][key]
            lock = sm.field_lock(snapshot, key)
            self.entries[key].configure(state="disabled" if lock["disabled"] else "normal")
            note = lock["note"] or sm.default_mark(setting) or ""
            self._marks[key] = note
            check = (snapshot.get("checks") or {}).get(key)
            self.notes[key].set(" · ".join(x for x in (sm.check_text(check["state"]) if check else "", note) if x))

    # --- сохранение ------------------------------------------------------------
    def current_form(self):
        return {key: var.get() for key, var in self.vars.items()}

    def has_unsaved_changes(self):
        return bool(self.snapshot) and bool(sm.changed_fields(self.current_form(), self.base))

    def can_leave(self):
        if not self.has_unsaved_changes():
            return True
        return bool(self.ask_confirm(ConfirmSpec(T["unsaved_title"], T["unsaved_body"], T["unsaved_leave"])))

    def on_save(self):
        self.save()

    def save(self):
        if self.snapshot is None:
            return
        form = self.current_form()
        # сервер принимает код уровня (DEBUG...), в форме стоит подпись
        codes = lambda f: {**f, "log_level": sm.LABEL_LEVELS.get(f["log_level"], f["log_level"])}
        built = sm.build_update(codes(form), codes(self.base), {**self.snapshot, "digest": self.digest})
        if not built["ok"]:
            self.banner_var.set(" ".join(built["errors"].values()))
            return
        payload = built["payload"] if len(built["payload"]) > 1 else {}
        try:
            tk_changes = sm.tk_changes(form, self.base)
        except ValueError as exc:
            self.banner_var.set(str(exc))
            return
        interval = tk_changes.get("auto_save_interval")
        if interval is not None and not 0 <= interval <= 3600:
            self.banner_var.set(T["autosave_range"])
            return
        if not payload and not tk_changes:
            notify.info(T["nothing"])
            return
        if not payload:
            self._write_tk(tk_changes, form)
            return
        self.calls.call(CAP_APP_SETTINGS_UPDATE, payload,
                        on_success=lambda snap: self._saved(snap, tk_changes, form), on_error=self._save_failed)

    def _write_tk(self, tk_changes, form):
        """Только значения окна: сервер не нужен, пишем сразу."""
        try:
            studio_settings.tk_settings_update(**tk_changes)
        except StudioError as exc:
            self.banner_var.set(str(exc))
            return
        if "ui_scale" in tk_changes:
            notify.info(T["scale_later"])
        self.base.update({k: form[k] for k in tk_changes})
        self.banner_var.set("")
        notify.success(T["saved"])
        self._applied()

    def _saved(self, snapshot, tk_changes=None, form=None):
        # сервер уже принял настройки: форму и отпечаток обновляем в любом случае, иначе будет ложный конфликт
        tk_error = None
        if tk_changes:
            try:
                studio_settings.tk_settings_update(**tk_changes)
                if "ui_scale" in tk_changes:
                    notify.info(T["scale_later"])
            except StudioError as exc:
                tk_error = str(exc)
        self._loaded(snapshot)
        if tk_error:
            self.banner_var.set(tk_error)
        notify.success(T["saved"])
        self._applied()

    def _save_failed(self, result):
        error = (result.get("error") or {})
        if error.get("code") == "conflict":
            self.banner_var.set(T["conflict"])
            self.refresh_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
            return
        reason = result_error_text(result).rstrip(".")
        field = sm.field_for_server_error(reason)
        if field in self.notes:
            self.notes[field].set(reason)
        self.banner_var.set(T["save_failed"].format(reason=reason))

    def _applied(self):
        if self.on_applied:
            self.on_applied()

    # --- сброс -----------------------------------------------------------------
    def destroy(self):
        for job in self._check_jobs.values():
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._check_jobs.clear()
        super().destroy()

    def reset(self):
        if not self.ask_confirm(ConfirmSpec(T["reset_title"], T["reset_body"], T["reset_confirm"])):
            return
        # размер окна и тему не трогаем: сбрасываются масштаб и автосохранение (после ответа сервера)
        defaults = {k: v for k, v in studio_settings.TK_DEFAULTS.items() if k in sm.TK_FIELDS}
        self.calls.call(CAP_APP_SETTINGS_RESET, {"confirm": True},
                        on_success=lambda snap: self._saved(snap, defaults), on_error=self._save_failed)
