"""Левая колонка «Генерации»: режим запуска, бриф и настройки прогона."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.generation import form, model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.shell.widgets import Tooltip, WrapRow
from StoryBookManager.gui.theme_tokens import PAD_BLOCK, PAD_INLINE, px

DEFAULT_WORD = "по умолчанию"


class SettingsView(ttk.Frame):
    def __init__(self, parent, ctl):
        super().__init__(parent, style="Panel.TFrame")
        self.ctl = ctl
        self.columnconfigure(0, weight=1)
        self._busy = False                 # идёт программное заполнение: событий от полей не ждём
        self._provider_ids, self._style_values = [], []
        row = 0
        # --- режим
        ttk.Label(self, text=T.fmt("section_mode"), style="Subtitle.TLabel").grid(row=row, column=0, sticky="w")
        self.mode_var = tk.StringVar(value="full")
        self.mode_buttons = {}
        for mode in model.RUN_MODES:
            row += 1
            btn = ttk.Radiobutton(self, text=model.mode_label(mode), value=mode, variable=self.mode_var,
                                  command=lambda m=mode: self._pick_mode(m))
            btn.grid(row=row, column=0, sticky="w")
            self.mode_buttons[mode] = btn
            Tooltip(btn, lambda m=mode: self._hint(m))
        row += 1
        self.mode_note = ttk.Label(self, text="", style="Caption.TLabel", wraplength=px(380), justify="left")
        self.mode_note.grid(row=row, column=0, sticky="w")
        # шаг
        row += 1
        self.step_frame = ttk.Frame(self, style="Panel.TFrame")
        self.step_frame.grid(row=row, column=0, sticky="ew", pady=(px(4), 0))
        self.step_frame.columnconfigure(1, weight=1)
        ttk.Label(self.step_frame, text=T.fmt("mode_step")).grid(row=0, column=0, sticky="w", padx=(0, px(PAD_INLINE)))
        self.step_box = ttk.Combobox(self.step_frame, state="readonly", values=[])
        self.step_box.grid(row=0, column=1, sticky="ew")
        self.step_box.bind("<<ComboboxSelected>>", self._pick_step)
        self._step_ids = []
        # перегенерация
        row += 1
        self.regen_frame = ttk.Frame(self, style="Panel.TFrame")
        self.regen_frame.grid(row=row, column=0, sticky="ew", pady=(px(4), 0))
        ttk.Label(self.regen_frame, text=T.fmt("regen_what")).grid(row=0, column=0, columnspan=3, sticky="w")
        self.what_var = tk.StringVar(value="both")
        for i, (what, key) in enumerate((("images", "regen_images"), ("video", "regen_video"), ("both", "regen_both"),
                                         ("music", "regen_music"), ("film", "regen_film"))):
            ttk.Radiobutton(self.regen_frame, text=T.fmt(key), value=what, variable=self.what_var,
                            command=lambda w=what: self.ctl.set_regen_what(w)).grid(row=1 + i // 2, column=i % 2, sticky="w",
                                                                                     padx=(0, px(PAD_BLOCK)))
        # --- бриф
        row += 1
        self.brief_frame = ttk.Frame(self, style="Panel.TFrame")
        self.brief_frame.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        self.brief_frame.columnconfigure(1, weight=1)
        self._build_brief()
        # --- настройки
        row += 1
        ttk.Label(self, text=T.fmt("section_settings"), style="Subtitle.TLabel").grid(
            row=row, column=0, sticky="w", pady=(px(PAD_BLOCK), 0))
        row += 1
        self.form = ttk.Frame(self, style="Panel.TFrame")
        self.form.grid(row=row, column=0, sticky="ew")
        self.form.columnconfigure(1, weight=1)
        self._build_form()
        row += 1
        self.lock_note = ttk.Label(self, text=T.fmt("settings_locked"), style="Caption.TLabel", wraplength=px(380), justify="left")
        self.lock_note.grid(row=row, column=0, sticky="w")
        row += 1
        buttons = WrapRow(self)
        buttons.grid(row=row, column=0, sticky="ew", pady=(px(4), px(PAD_BLOCK)))
        self.save_btn = ttk.Button(buttons, text=T.fmt("save"), command=self.ctl.save_settings)
        self.reset_btn = ttk.Button(buttons, text=T.fmt("reset_defaults"), command=self.ctl.reset_defaults)
        buttons.set_items([self.save_btn, self.reset_btn])
        self.dirty_label = ttk.Label(self, text="", style="Caption.TLabel")
        self.dirty_label.grid(row=row + 1, column=0, sticky="w")

    # ------------------------------------------------------------------ сборка

    def _build_brief(self) -> None:
        f = self.brief_frame
        ttk.Label(f, text=T.fmt("brief_task")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.task_text = tk.Text(f, height=3, width=30, wrap="word", font="SBM.Body")
        self.task_text.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.task_text.bind("<KeyRelease>", lambda e: None if self._busy else self.ctl.set_task(self.task_text.get("1.0", "end").strip()))
        ttk.Label(f, text=T.fmt("brief_task_hint"), style="Caption.TLabel", wraplength=px(380), justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w")
        self.brief_note = ttk.Label(f, text=T.fmt("brief_exists"), style="Caption.TLabel", wraplength=px(380), justify="left")
        self.brief_note.grid(row=3, column=0, columnspan=2, sticky="w")
        self.brief_vars, self.brief_widgets = {}, {}
        for i, (key, label, kind) in enumerate(form.BRIEF_FIELDS):
            ttk.Label(f, text=T.fmt(label)).grid(row=4 + i, column=0, sticky="w", padx=(0, px(PAD_INLINE)))
            if kind == "bool":
                var = tk.BooleanVar()
                w = ttk.Checkbutton(f, variable=var, command=lambda k=key, v=var: self._brief_edit(k, v.get()))
            else:
                var = tk.StringVar()
                w = ttk.Entry(f, textvariable=var, width=10)
                w.bind("<KeyRelease>", lambda e, k=key, v=var: self._brief_edit(k, v.get()))
            w.grid(row=4 + i, column=1, sticky="w", pady=1)
            self.brief_vars[key], self.brief_widgets[key] = var, w
        self.blockout_label = ttk.Label(f, text="", style="Caption.TLabel")
        self.blockout_label.grid(row=4 + len(form.BRIEF_FIELDS), column=0, columnspan=2, sticky="w")

    def _build_form(self) -> None:
        f = self.form
        self.provider_box = self._combo(0, "video_provider", self._pick_provider)
        self.provider_hint = ttk.Label(f, text="", style="Caption.TLabel")
        self.provider_hint.grid(row=1, column=1, sticky="w")
        self.key_label = ttk.Label(f, text="", style="Caption.TLabel")
        self.key_label.grid(row=2, column=1, sticky="w")
        self.model_box = self._combo(3, "video_model", lambda e: self._pick_text("video", "model", self.model_box))
        self.fallback_box = self._combo(4, "video_fallback_model", lambda e: self._pick_text("video", "fallback_model", self.fallback_box))
        ttk.Label(f, text=T.fmt("video_retry_cycles")).grid(row=5, column=0, sticky="w", padx=(0, px(PAD_INLINE)))
        self.retry_var = tk.StringVar()
        self.retry_entry = ttk.Spinbox(f, from_=1, to=10, width=5, textvariable=self.retry_var, command=self._pick_retry)
        self.retry_entry.grid(row=5, column=1, sticky="w", pady=1)
        self.retry_entry.bind("<FocusOut>", lambda e: self._pick_retry())
        self.retry_entry.bind("<Return>", lambda e: self._pick_retry())
        self.res_box = self._combo(6, "video_resolution", lambda e: self._pick_text("video", "resolution", self.res_box))
        ttk.Label(f, text=T.fmt("video_durations")).grid(row=7, column=0, sticky="nw", padx=(0, px(PAD_INLINE)))
        self.dur_frame = WrapRow(f)
        self.dur_frame.grid(row=7, column=1, sticky="ew")
        self.dur_vars = {}
        self.style_box = self._combo(8, "style", self._pick_style)
        self.audio_vars = {}
        for i, (key, text) in enumerate((("music", "audio_music"), ("sfx", "audio_sfx"), ("subtitles", "audio_subtitles"))):
            var = tk.BooleanVar()
            ttk.Checkbutton(f, text=T.fmt(text), variable=var,
                            command=lambda k=key, v=var: self._pick_flag("audio", k, v)).grid(row=9 + i, column=0, columnspan=2, sticky="w")
            self.audio_vars[key] = var
        ttk.Label(f, text=T.fmt("budget")).grid(row=12, column=0, sticky="w", padx=(0, px(PAD_INLINE)))
        self.budget_var = tk.StringVar()
        self.budget_entry = ttk.Entry(f, textvariable=self.budget_var, width=12)
        self.budget_entry.grid(row=12, column=1, sticky="w", pady=1)
        self.budget_entry.bind("<KeyRelease>", lambda e: None if self._busy else self.ctl.edit_budget(self.budget_var.get()))
        self.budget_note = ttk.Label(f, text=T.fmt("budget_hint"), style="Caption.TLabel", wraplength=px(380), justify="left")
        self.budget_note.grid(row=13, column=0, columnspan=2, sticky="w")
        self.adv_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text=T.fmt("advanced"), variable=self.adv_open, style="Toolbutton",
                        command=self._toggle_adv).grid(row=14, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        self.adv_frame = ttk.Frame(f, style="Panel.TFrame")
        self.adv_vars = {}
        for i, (key, text) in enumerate((("force_update_prompts", "adv_force_prompts"), ("skip_prompt_enhancement", "adv_skip_enhance"),
                                         ("sample_before_batch", "adv_sample_first"), ("final_allow_missing_audio", "adv_final_no_audio"))):
            var = tk.BooleanVar()
            ttk.Checkbutton(self.adv_frame, text=T.fmt(text), variable=var,
                            command=lambda k=key, v=var: self._pick_flag("advanced", k, v)).grid(row=i, column=0, sticky="w")
            self.adv_vars[key] = var

    def _combo(self, row, text_id, handler):
        ttk.Label(self.form, text=T.fmt(text_id)).grid(row=row, column=0, sticky="w", padx=(0, px(PAD_INLINE)))
        box = ttk.Combobox(self.form, state="readonly", values=[], width=18)
        box.grid(row=row, column=1, sticky="ew", pady=1)
        box.bind("<<ComboboxSelected>>", handler)
        return box

    def _toggle_adv(self) -> None:
        if self.adv_open.get():
            self.adv_frame.grid(row=15, column=0, columnspan=2, sticky="w")
        else:
            self.adv_frame.grid_remove()

    # ------------------------------------------------------------------ события полей

    def _hint(self, mode):
        for o in model.mode_options(self.ctl.resumable, len(self.ctl.selection.shot_keys)):
            if o["mode"] == mode:
                return o["hint"] or ""
        return ""

    def _pick_mode(self, mode):
        if not self._busy:
            self.ctl.set_mode(mode)

    def _pick_step(self, _e=None):
        i = self.step_box.current()
        if not self._busy and 0 <= i < len(self._step_ids):
            self.ctl.set_step(self._step_ids[i])

    def _brief_edit(self, key, value):
        if not self._busy:
            self.ctl.set_brief(key, value)

    def _pick_provider(self, _e=None):
        i = self.provider_box.current()
        if not self._busy and 0 <= i < len(self._provider_ids):
            self.ctl.edit_provider(self._provider_ids[i])

    def _pick_text(self, section, key, box):
        if not self._busy:
            self.ctl.edit(section, key, box.get() or None)

    def _pick_retry(self):
        text = self.retry_var.get().strip()
        if not self._busy and text.isdigit() and int(text) >= 1:
            self.ctl.edit("video", "retry_cycles", int(text))

    def _pick_style(self, _e=None):
        i = self.style_box.current()
        if not self._busy and 0 <= i < len(self._style_values):
            self.ctl.edit_style(self._style_values[i])

    def _pick_flag(self, section, key, var):
        if not self._busy:
            self.ctl.edit(section, key, bool(var.get()))

    def _toggle_duration(self, value, var):
        if self._busy:
            return
        draft = self.ctl.draft or {}
        every = form.duration_choices(form.active_model(self.ctl.provider(), draft))
        current = (draft.get("video") or {}).get("allowed_durations")
        self.ctl.edit("video", "allowed_durations", form.toggle_duration(current, every, value))

    # ------------------------------------------------------------------ показ

    def show(self) -> None:
        ctl = self.ctl
        self._busy = True
        try:
            self._show_mode()
            self._show_form()
        finally:
            self._busy = False

    def _show_mode(self) -> None:
        ctl = self.ctl
        sel = ctl.selection
        self.mode_var.set(sel.mode)
        options = {o["mode"]: o for o in model.mode_options(ctl.resumable, len(sel.shot_keys))}
        for mode, btn in self.mode_buttons.items():
            btn.configure(state="normal" if options[mode]["enabled"] else "disabled")
        note = ""
        if sel.mode == "regenerate_shots":
            note = T.fmt("mode_shots_count", n=len(sel.shot_keys)) if sel.shot_keys else T.fmt("mode_shots_none")
        elif not options["resume"]["enabled"]:
            note = options["resume"]["hint"]
        self.mode_note.configure(text=note)
        stepped = sel.mode in ("from_step", "single_step")
        self._set_visible(self.step_frame, stepped)
        if stepped:
            self._step_ids = [s.get("id") for s in ctl.steps_catalog]
            self.step_box.configure(values=[f"{i + 1}. {ctl.titles(s.get('id'))}" for i, s in enumerate(ctl.steps_catalog)])
            if sel.step_id in self._step_ids:
                self.step_box.current(self._step_ids.index(sel.step_id))
            else:
                self.step_box.set("")
        regen = sel.mode == "regenerate_shots"
        self._set_visible(self.regen_frame, regen)
        self.what_var.set(ctl.regen_what)
        # бриф нужен только полному запуску
        full = sel.mode == "full"
        self._set_visible(self.brief_frame, full)
        if full:
            plan_inputs = (ctl.plan or {}).get("inputs")
            has_brief = not any(p.get("code") == "invalid_task" for p in (ctl.plan or {}).get("problems") or [])
            self._set_visible(self.brief_note, has_brief)
            if self.task_text.get("1.0", "end").strip() != ctl.task:
                self.task_text.delete("1.0", "end")
                self.task_text.insert("1.0", ctl.task)
            for key, label, kind in form.BRIEF_FIELDS:
                value = form.brief_value(ctl.brief, plan_inputs, key)
                var = self.brief_vars[key]
                if kind == "bool":
                    var.set(bool(value))
                elif var.get() != str(value):
                    var.set(str(value))
            self.blockout_label.configure(text=T.fmt("brief_blockout_on") if form.blockout_on(plan_inputs) else T.fmt("brief_blockout_off"))

    def _set_visible(self, widget, on: bool) -> None:
        if on:
            widget.grid()
        else:
            widget.grid_remove()

    def _show_form(self) -> None:
        ctl = self.ctl
        draft = ctl.draft
        ready = draft is not None
        locked = ctl.locked_settings()
        if not ready:
            return
        eff = ctl.effective
        video = draft.get("video") or {}
        opts = form.provider_options(ctl.providers)
        self._provider_ids = [o["value"] for o in opts]
        self.provider_box.configure(values=[o["label"] + ("" if not o["disabled"] else " (недоступен)") for o in opts])
        provider = ctl.provider()
        if provider and provider.get("id") in self._provider_ids:
            self.provider_box.current(self._provider_ids.index(provider["id"]))
        self.provider_hint.configure(text=form.source_hint(eff, "video.provider") or "")
        if provider:
            self.key_label.configure(text=T.fmt("key_found") if provider.get("credentials_found")
                                     else T.fmt("key_missing", name=provider.get("title")))
        else:
            self.key_label.configure(text="")
        models = form.model_ids(provider)
        active = form.active_model(provider, draft)
        self.model_box.configure(values=models)
        self.model_box.set(video.get("model") or (active or {}).get("id") or "")
        self.fallback_box.configure(values=[""] + models)
        self.fallback_box.set(video.get("fallback_model") or "")
        retry = video.get("retry_cycles")
        self.retry_var.set(str(retry if retry is not None else (eff.get("video.retry_cycles") or {}).get("value") or 1))
        resolutions = form.resolution_ids(active)
        self.res_box.configure(values=[""] + resolutions)
        self.res_box.set(video.get("resolution") or "")
        # сначала очищаем раскладку, потом уничтожаем старые флажки
        self.dur_frame.set_items([])
        for child in self.dur_frame.winfo_children():
            child.destroy()
        self.dur_vars = {}
        boxes = []
        for d in form.duration_choices(active):
            var = tk.BooleanVar(value=form.is_duration_on(video.get("allowed_durations"), d))
            box = ttk.Checkbutton(self.dur_frame, text=str(d), variable=var,
                                  command=lambda v=d, var=var: self._toggle_duration(v, var))
            if locked:
                box.configure(state="disabled")
            boxes.append(box)
            self.dur_vars[d] = var
        self.dur_frame.set_items(boxes)
        groups = form.style_groups(ctl.styles)
        flat = [(g["profile"], o) for g in groups for o in g["options"]]
        self._style_values = [o["value"] for _g, o in flat]
        self.style_box.configure(values=[f"{g} · {o['label']}" for g, o in flat])
        current = form.style_value(draft)
        if current in self._style_values:
            self.style_box.current(self._style_values.index(current))
        else:
            self.style_box.set(DEFAULT_WORD)
        for key, var in self.audio_vars.items():
            stored = (draft.get("audio") or {}).get(key)
            var.set(bool(stored if stored is not None else (eff.get(f"audio.{key}") or {}).get("value", True)))
        for key, var in self.adv_vars.items():
            stored = (draft.get("advanced") or {}).get(key)
            var.set(bool(stored if stored is not None else (eff.get(f"advanced.{key}") or {}).get("value", False)))
        if ctl.budget_text is not None and self.focus_get() is not self.budget_entry:
            self.budget_var.set(ctl.budget_text)
        self.budget_note.configure(text=ctl.budget_error or T.fmt("budget_hint"))
        # на время прогона можно менять только бюджет
        state = "disabled" if locked else "readonly"
        for box in (self.provider_box, self.model_box, self.fallback_box, self.res_box, self.style_box):
            box.configure(state=state)
        for w in (self.retry_entry,):
            w.configure(state="disabled" if locked else "normal")
        for child in self.form.winfo_children():
            if isinstance(child, ttk.Checkbutton) and child is not None and str(child.cget("text")) != T.fmt("advanced"):
                child.configure(state="disabled" if locked else "normal")
        for child in self.adv_frame.winfo_children():
            child.configure(state="disabled" if locked else "normal")
        self._set_visible(self.lock_note, locked)
        dirty = ctl.settings_dirty()
        self.save_btn.configure(state="normal" if (dirty and not ctl.saving and not ctl.budget_error) else "disabled")
        self.reset_btn.configure(state="disabled" if locked or ctl.defaults is None else "normal")
        self.dirty_label.configure(text=T.TK_TEXTS["tk_save_dirty"] if dirty else "")
