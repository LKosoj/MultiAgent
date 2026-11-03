"""Экран «Файлы»: список JSON-файлов проекта, форма и JSON, проверка, история, откат."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.screens import files_model as fmod
from StoryBookManager.gui.screens.calls import (
    CAP_ARTIFACT_DIFF, CAP_ARTIFACT_HISTORY, CAP_ARTIFACT_READ, CAP_ARTIFACT_RESTORE, CAP_ARTIFACT_SAVE,
    CAP_ARTIFACT_VALIDATE, CAP_ARTIFACTS_LIST, StudioCalls,
)
from StoryBookManager.gui.screens.common_model import format_bytes, format_created_at
from StoryBookManager.gui.screens.files_texts import FILES_TEXTS as T
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IMG_T
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shell.widgets import PanelHeader, Tooltip, WrapRow
from StoryBookManager.gui.shots.dialogs import ChoiceDialog
from StoryBookManager.gui.shots.model import error_parts, result_error_text
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

NARROW_WIDTH = 1100


class FilesScreen(ttk.Frame, SectionAdapter):
    """Интерфейс экрана для окна: load_project, has_unsaved_changes, save_*, validate_current_project, undo, redo."""

    def __init__(self, parent, bridge, get_project_id=None, sync_invoke=None):
        ttk.Frame.__init__(self, parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.calls = StudioCalls(bridge)
        self.bridge = bridge
        self.view_mode = "docs"   # docs - файлы проекта, images - иллюстрации 50_images
        self.gallery = None
        self.run_state = None   # состояние прогона из сводки оболочки (running, paused, ...)
        self.get_project_id = get_project_id or (lambda: self.project_id)
        self.project = None
        self.project_id = None
        self.items = []
        self.rows = []
        self.states = {}      # key -> EditorState (открытые и изменённые)
        self.current = None   # key открытого файла
        self.mode = "form"
        self.findings = []
        self.forbidden = False
        self.sync_invoke = sync_invoke
        self.ask_confirm = lambda spec: ConfirmDialog.ask(self.winfo_toplevel(), spec)
        self.ask_choice = lambda title, body, buttons, default="": ChoiceDialog.ask(self, title, body, buttons, default)
        self._narrow = None
        self._field_vars = {}
        self._field_errors = {}
        self._group = ""
        self._appliers = {}      # виджет поля -> функция «применить введённое»
        self._invalid_warned = set()  # файлы, про которые автосохранение уже предупредило
        self._build()
        self.bind("<Configure>", self._on_resize, add="+")

    # --- построение -------------------------------------------------------------
    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        header = PanelHeader(self, T["title"])
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.view_var = tk.StringVar(value="docs")
        modes = ttk.Frame(header, style="Panel.TFrame")
        modes.grid(row=0, column=1, sticky="e")
        for value, text_id in (("docs", "mode_docs"), ("images", "mode_images")):
            ttk.Radiobutton(modes, text=IMG_T[text_id], value=value, variable=self.view_var,
                            command=self._switch_view).pack(side="left", padx=(0, px(PAD_INLINE)))
        self.left = ttk.Frame(self, style="Panel.TFrame", width=px(280))
        self.left.grid(row=1, column=0, sticky="ns", padx=(0, px(PAD_PANEL)), pady=(px(PAD_INLINE), 0))
        self.left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.left, columns=("status",), show="tree headings", selectmode="browse", height=18)
        self.tree.heading("#0", text="Файл")
        self.tree.heading("status", text="Статус")
        self.tree.column("#0", width=px(190), stretch=True)
        self.tree.column("status", width=px(80), stretch=False)
        self.tree.grid(row=0, column=0, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.combo_var = tk.StringVar()
        self.combo = ttk.Combobox(self, textvariable=self.combo_var, state="readonly")
        self.combo.bind("<<ComboboxSelected>>", self._on_combo_select)
        self.list_msg = tk.StringVar(value=T["no_project"])
        ttk.Label(self.left, textvariable=self.list_msg, style="Caption.TLabel", wraplength=px(260)).grid(row=1, column=0, sticky="w")

        self.right = ttk.Frame(self, style="Panel.TFrame")
        self.right.grid(row=1, column=1, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.right.columnconfigure(0, weight=1)
        self.right.rowconfigure(3, weight=1)
        self.title_var = tk.StringVar(value=T["pick_file"])
        ttk.Label(self.right, textvariable=self.title_var, style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        bar = WrapRow(self.right)
        bar.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.mode_var = tk.StringVar(value="form")
        self.form_rb = ttk.Radiobutton(bar, text=T["mode_form"], value="form", variable=self.mode_var, command=self._switch_mode)
        self.json_rb = ttk.Radiobutton(bar, text=T["mode_json"], value="json", variable=self.mode_var, command=self._switch_mode)
        self.save_btn = ttk.Button(bar, text=T["save"], command=self.save_current_file)
        self.anyway_btn = ttk.Button(bar, text=T["save_anyway"], command=self._save_anyway)
        self.validate_btn = ttk.Button(bar, text=T["validate"], command=self.validate_file)
        self.reload_btn = ttk.Button(bar, text=T["reload"], command=self.reload_file)
        self.history_btn = ttk.Button(bar, text=T["history"], command=self.open_history)
        self.edits_btn = ttk.Button(bar, text=T["my_edits"], command=self.show_my_edits)
        bar.set_items([self.form_rb, self.json_rb, self.save_btn, self.validate_btn, self.reload_btn, self.history_btn])
        self.bar = bar
        self.banner_var = tk.StringVar()
        self.banner = ttk.Label(self.right, textvariable=self.banner_var, wraplength=px(640), justify="left", foreground=COLORS["warning"])
        self.banner.grid(row=2, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.editor = ttk.Frame(self.right, style="Panel.TFrame")
        self.editor.grid(row=3, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.editor.columnconfigure(1, weight=1)
        self.editor.rowconfigure(0, weight=1)
        # форма: список групп слева, поля справа
        self.form_frame = ttk.Frame(self.editor, style="Panel.TFrame")
        self.form_frame.columnconfigure(1, weight=1)
        self.form_frame.rowconfigure(0, weight=1)
        self.groups = tk.Listbox(self.form_frame, width=22, exportselection=False, activestyle="none")
        self.groups.grid(row=0, column=0, sticky="ns")
        self.groups.bind("<<ListboxSelect>>", self._on_group)
        self.canvas = tk.Canvas(self.form_frame, highlightthickness=0, background=COLORS["window_bg"])
        self.canvas.grid(row=0, column=1, sticky="nsew")
        scroll = ttk.Scrollbar(self.form_frame, command=self.canvas.yview)
        scroll.grid(row=0, column=2, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.fields_box = ttk.Frame(self.canvas, style="Panel.TFrame")
        self.canvas.create_window((0, 0), window=self.fields_box, anchor="nw", tags="box")
        self.fields_box.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure("box", width=e.width))
        # JSON
        self.json_frame = ttk.Frame(self.editor, style="Panel.TFrame")
        self.json_frame.columnconfigure(0, weight=1)
        self.json_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(self.json_frame, undo=True, wrap="none", font="TkFixedFont", background=COLORS["card"], height=12)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(self.json_frame, command=self.text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=yscroll.set)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.tag_configure("err", background="#F3D5D9")
        self.findings_var = tk.StringVar()
        ttk.Label(self.right, textvariable=self.findings_var, style="Caption.TLabel").grid(row=4, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.findings_list = tk.Listbox(self.right, height=4, activestyle="none")
        self.findings_list.grid(row=5, column=0, sticky="ew")
        self.findings_list.bind("<Double-Button-1>", self._on_finding)
        self.findings_list.bind("<Return>", self._on_finding)
        self._show_mode_widgets()

    # --- раскладка списка --------------------------------------------------------
    def _on_resize(self, event):
        if event.widget is not self or self.view_mode == "images":
            return
        self._relayout()

    def _relayout(self):
        narrow = self.winfo_toplevel().winfo_width() < NARROW_WIDTH
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            self.left.grid_remove()
            self.combo.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(px(PAD_INLINE), 0))
            self.right.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(px(PAD_INLINE), 0))
            self.rowconfigure(1, weight=0)
            self.rowconfigure(2, weight=1)
        else:
            self.combo.grid_remove()
            self.left.grid()
            self.right.grid(row=1, column=1, columnspan=1, sticky="nsew", pady=(px(PAD_INLINE), 0))
            self.rowconfigure(2, weight=0)
            self.rowconfigure(1, weight=1)

    def _switch_view(self):
        """Переключатель «Документы | Иллюстрации»: галерея занимает место списка и редактора."""
        mode = self.view_var.get()
        if mode == self.view_mode:
            return
        self.view_mode = mode
        if mode == "images":
            if self.gallery is None:
                from StoryBookManager.gui.imagetools.gallery_view import GalleryView
                self.gallery = GalleryView(self, self.bridge, lambda: self.project_id,
                                           get_run_state=lambda: self.run_state)
            for widget in (self.left, self.combo, self.right):
                widget.grid_remove()
            for row, weight in ((1, 0), (2, 0), (3, 1)):
                self.rowconfigure(row, weight=weight)
            self.gallery.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(px(PAD_INLINE), 0))
            self.gallery.ensure_loaded()
        else:
            self.gallery.grid_remove()
            self.rowconfigure(3, weight=0)
            self.right.grid()
            self._narrow = None
            self._relayout()

    def _show_mode_widgets(self):
        self.form_frame.grid_remove()
        self.json_frame.grid_remove()
        (self.form_frame if self.mode == "form" else self.json_frame).grid(row=0, column=0, columnspan=2, sticky="nsew")

    # --- проект и список ----------------------------------------------------------
    def load_project(self, project):
        self.project = project
        self.project_id = getattr(project, "project_id", None) or getattr(project, "name", None)
        self.states.clear()
        self._invalid_warned.clear()
        self.current = None
        self.findings = []
        self._render_findings()
        self._clear_editor(T["pick_file"])
        self.load_list()
        if self.gallery is not None:
            self.gallery.shutdown()
            self.gallery.set_project()
            if self.view_mode == "images":
                self.gallery.ensure_loaded()

    def on_project_changed(self, project):
        pass

    def on_status(self, brief):
        """Сводка проекта из верхней панели: идущий прогон блокирует правку картинки ИИ."""
        from StoryBookManager.gui.shell.status_model import run_state

        state = run_state(brief)
        if state == self.run_state:
            return
        self.run_state = state
        if self.gallery is not None:
            self.gallery.on_run_state()

    def destroy(self):
        if self.gallery is not None:
            self.gallery.shutdown()
        super().destroy()

    def on_show(self):
        if self.project_id and not self.items:
            self.load_list()

    def on_refresh(self):
        if not self.project_id:
            return
        if self.view_mode == "images":
            self.gallery.reload()
            return
        if self.has_unsaved_changes():
            notify.warning("Есть несохранённые правки. Сохраните их и повторите обновление.")
            return
        self.load_list()
        if self.current:
            self.reload_file()

    def load_list(self):
        if not self.project_id:
            return
        self.list_msg.set(T["loading_list"])
        self.calls.call(CAP_ARTIFACTS_LIST, {"project_id": self.project_id}, on_success=self._list_loaded,
                        on_error=lambda r: self.list_msg.set(T["list_failed"].format(reason=result_error_text(r).rstrip("."))),
                        key="artifacts_list")

    def _list_loaded(self, result):
        self.items = result.get("items", [])
        self.rows = fmod.list_rows(self.items)
        self.list_msg.set("")
        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            self.tree.insert("", "end", iid=row["key"], text=fmod.row_text(row), values=(row["status"],))
        self.combo["values"] = [f"{fmod.row_text(r)} — {r['status']}" for r in self.rows]
        self._tooltip_paths()
        if self.current and self.current in self.tree.get_children():
            self.tree.selection_set(self.current)
        self._sync_combo()

    def _sync_combo(self):
        """В узком режиме список заменён выпадающим: показываем в нём открытый файл."""
        idx = next((i for i, r in enumerate(self.rows) if r["key"] == self.current), None)
        if idx is None:
            self.combo.set("")
        else:
            self.combo.current(idx)

    def _tooltip_paths(self):
        paths = {r["key"]: r["path"] for r in self.rows}
        if not hasattr(self, "_tip"):
            self._tip = Tooltip(self.tree, lambda: paths.get(self.tree.identify_row(self.tree.winfo_pointery() - self.tree.winfo_rooty()), ""))

    def _on_tree_select(self, _e):
        sel = self.tree.selection()
        if sel and sel[0] != self.current:
            self.open_file(sel[0])

    def _on_combo_select(self, _e):
        idx = self.combo.current()
        if 0 <= idx < len(self.rows) and self.rows[idx]["key"] != self.current:
            self.open_file(self.rows[idx]["key"])

    # --- открыть файл ------------------------------------------------------------
    def open_file(self, key):
        if not self._leave_current():
            self._restore_selection()
            return
        self.current = key
        self._sync_combo()
        saved = self.states.get(key)
        if saved is not None and saved.dirty:
            self._show_state(saved)
            return
        self.title_var.set(T["loading_file"])
        self.calls.call(CAP_ARTIFACT_READ, {"project_id": self.project_id, "key": key},
                        on_success=lambda r, k=key: self._read_done(k, r),
                        on_error=lambda r: self._clear_editor(T["read_failed"].format(reason=result_error_text(r).rstrip("."))),
                        key="artifact_read")

    def _restore_selection(self):
        if self.current and self.current in self.tree.get_children():
            self.tree.selection_set(self.current)
        self._sync_combo()

    def _read_done(self, key, result):
        if key != self.current:
            return
        state = fmod.EditorState(key, result)
        old = self.states.get(key)
        state.my_edits = old.my_edits if old else None
        self.states[key] = state
        self.findings = []
        self._render_findings()
        self._show_state(state)

    def reload_file(self):
        if not self.current:
            return
        state = self.states.get(self.current)
        if state and state.dirty:
            state.my_edits = self._edited_text(state)
            state.dirty = False
        key = self.current
        self.calls.call(CAP_ARTIFACT_READ, {"project_id": self.project_id, "key": key},
                        on_success=lambda r, k=key: self._read_done(k, r),
                        on_error=lambda r: self.banner_var.set(T["read_failed"].format(reason=result_error_text(r).rstrip("."))))

    def _clear_editor(self, message):
        self.current = None if message == T["pick_file"] else self.current
        self.title_var.set(message)
        self.banner_var.set("")
        for w in self.fields_box.winfo_children():
            w.destroy()
        self.groups.delete(0, "end")
        self.text.delete("1.0", "end")

    def _show_state(self, state):
        self.title_var.set(f"{state.title} ({state.path})")
        notes = []
        if not state.exists:
            step = state_step(self.items, state.key)
            notes.append(T["not_created"].format(step=step))
        elif state.parse_error:
            notes.append(fmod.broken_text(state))
        note = fmod.read_only_note(state, self.forbidden)
        if note:
            notes.append(note)
        if state.run_state == "warn" and state.run_state_message:
            notes.append(state.run_state_message)
        self.banner_var.set("\n".join(notes))
        self.mode = "json" if state.parse_error or not isinstance(state.content, (dict, list)) else self.mode
        self.mode_var.set(self.mode)
        self._show_mode_widgets()
        self._fill_editor(state)
        self._update_buttons()

    def _fill_editor(self, state):
        editable = state.can_edit and not self.forbidden
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if state.text is not None and (state.parse_error or state.content is None):
            body = state.text
        elif state.content is not None:
            body = fmod.content_to_text(state.content)
        else:
            body = state.text or ""
        self.text.insert("1.0", body)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.text.configure(state="normal" if editable else "disabled")
        self._build_groups(state)

    def _build_groups(self, state):
        self.groups.delete(0, "end")
        read_only = not (state.can_edit and not self.forbidden)
        fields = fmod.build_fields(state.key, state.content) or []
        self._field_list = []
        for f in fields:
            if f["kind"] in ("group", "list", "item"):
                continue  # заголовки вложенности: форма показывает листья, группы слева
            f["readOnly"] = f["readOnly"] or read_only
            f["group"] = fmod.group_of(f["path"])
            self._field_list.append(f)
        seen = []
        for f in self._field_list:
            if f["group"] not in seen:
                seen.append(f["group"])
        self._group_ids = seen
        for g in seen:
            self.groups.insert("end", fmod.group_title(state.key, g))
        if seen:
            self._group = self._group if self._group in seen else seen[0]
            self.groups.selection_set(seen.index(self._group))
            self._render_group()
        else:
            self._render_group()

    def _on_group(self, _e):
        sel = self.groups.curselection()
        if sel:
            self._group = self._group_ids[sel[0]]
            self._render_group()

    def _render_group(self):
        for w in self.fields_box.winfo_children():
            w.destroy()
        self._field_vars = {}
        self._appliers = {}
        self.fields_box.columnconfigure(1, weight=1)
        by_pointer = fmod.findings_by_pointer(self.findings)
        row = 0
        for f in getattr(self, "_field_list", []):
            if f["group"] != self._group:
                continue
            ttk.Label(self.fields_box, text=f["label"]).grid(row=row, column=0, sticky="nw", padx=(0, px(PAD_PANEL)), pady=(px(PAD_INLINE) // 2, 0))
            Tooltip(self.fields_box.winfo_children()[-1], f["pointer"] or "/")
            disabled = "disabled" if f["readOnly"] else "normal"
            if f["kind"] == "bool":
                var = tk.BooleanVar(value=f["value"])
                w = ttk.Checkbutton(self.fields_box, variable=var, state=disabled, command=lambda ff=f, v=var: self._apply_field(ff, v.get()))
            elif f["kind"] in ("text", "lines", "numlines"):
                w = tk.Text(self.fields_box, width=20, height=4, wrap="word", background=COLORS["card"])
                w.insert("1.0", f["value"] if f["kind"] == "text" else "\n".join(str(x) for x in f["value"]))
                w.configure(state=disabled)
                w.bind("<FocusOut>", lambda e, ff=f, ww=w: self._apply_field(ff, ww.get("1.0", "end-1c")))
                self._appliers[w] = lambda ff=f, ww=w: self._apply_field(ff, ww.get("1.0", "end-1c"))
                var = None
            else:
                var = tk.StringVar(value="" if f["value"] is None else str(f["value"]))
                w = ttk.Entry(self.fields_box, textvariable=var, width=20, state=disabled)
                w.bind("<FocusOut>", lambda e, ff=f, v=var: self._apply_field(ff, v.get()))
                w.bind("<Return>", lambda e, ff=f, v=var: self._apply_field(ff, v.get()))
                self._appliers[w] = lambda ff=f, v=var: self._apply_field(ff, v.get())
            w.grid(row=row, column=1, sticky="ew", pady=(px(PAD_INLINE) // 2, 0))
            self._field_vars[f["pointer"]] = w
            row += 1
            notes = []
            if f["kind"] == "null":
                notes.append(T["not_set"])
            notes += [x["message"] for x in by_pointer.get(f["pointer"], [])]
            if f["pointer"] in self._field_errors:
                notes.append(self._field_errors[f["pointer"]])
            if notes:
                ttk.Label(self.fields_box, text=" · ".join(notes), foreground=COLORS["error"], wraplength=px(520)).grid(row=row, column=1, sticky="w")
                row += 1

    def _commit_focused(self):
        """Применяет поле, в котором сейчас печатают: сохранение не должно ждать ухода курсора."""
        try:
            widget = self.focus_get()
        except (tk.TclError, KeyError):
            return
        apply = self._appliers.get(widget)
        if apply:
            apply()

    def _apply_field(self, field, raw):
        state = self.states.get(self.current)
        if state is None or not state.can_edit or self.forbidden:
            return
        pointer = field["pointer"]
        result = fmod.apply_input(state.key, state.content, pointer, raw)
        if result["error"]:
            self._field_errors[pointer] = result["error"]
            self._render_group()
            return
        self._field_errors.pop(pointer, None)
        if result["content"] == state.content:
            return
        state.content = result["content"]
        state.dirty = True
        self._sync_text(state)
        self._update_buttons()

    def _sync_text(self, state):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", fmod.content_to_text(state.content))
        self.text.edit_modified(False)

    def _on_text_modified(self, _e):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        state = self.states.get(self.current)
        if state is None or not state.can_edit or self.forbidden:
            return
        if self.text.cget("state") == "normal" and self.mode == "json":
            state.dirty = True
            self._update_buttons()

    # --- режимы -------------------------------------------------------------------
    def _switch_mode(self):
        target = self.mode_var.get()
        state = self.states.get(self.current)
        if state is None or target == self.mode:
            return
        if target == "form":
            if state.can_edit and (state.dirty or self.text.edit_modified() or self._text_differs(state)):
                try:
                    state.content = fmod.parse_json_text(self.text.get("1.0", "end-1c"))
                except ValueError as exc:
                    self.mode_var.set("json")
                    self.banner_var.set(str(exc))
                    return
            elif state.parse_error or not isinstance(state.content, (dict, list)):
                self.mode_var.set("json")
                self.banner_var.set(T["form_unavailable"])
                return
            self._build_groups(state)
        self.mode = target
        self._show_mode_widgets()

    def _text_differs(self, state):
        """Текст в окне JSON отличается от содержимого файла (правка могла прийти и без отметки)."""
        if state.parse_error or not isinstance(state.content, (dict, list)):
            return False
        return self.text.get("1.0", "end-1c").strip() != fmod.content_to_text(state.content).strip()

    def _edited_text(self, state):
        return self.text.get("1.0", "end-1c") if self.mode == "json" else fmod.content_to_text(state.content)

    def _payload_content(self, state):
        """Что отправить на сервер: (поле, значение). В режиме JSON уходит текст, в форме - структура."""
        if self.mode == "json":
            return {"text": self.text.get("1.0", "end-1c")}
        return {"content": state.content}

    # --- проверка / сохранение ------------------------------------------------------
    def validate_file(self):
        self._commit_focused()
        state = self.states.get(self.current)
        if not state:
            return
        pid, key = self.project_id, state.key
        payload = {"project_id": pid, "key": key, **(self._payload_content(state) if state.dirty or self.mode == "json" else {})}
        self.calls.call(CAP_ARTIFACT_VALIDATE, payload, on_success=lambda r: self._validated(r, pid, key),
                        on_error=lambda r: self._set_error_findings(r) if (pid, key) == (self.project_id, self.current) else None)

    def _validated(self, result, pid=None, key=None):
        if pid is not None and (pid, key) != (self.project_id, self.current):
            return  # ответ про другой проект или файл
        self.findings = result.get("findings", [])
        self._render_findings(result.get("counts"))
        if result.get("valid"):
            notify.success(T["valid_ok"])
        if self.mode == "form":
            self._render_group()

    def _set_error_findings(self, result):
        _code, _reason, message, details = error_parts(result)
        self.findings = details.get("errors") or []
        self._render_findings()
        if not self.findings:
            self.banner_var.set(result_error_text(result))

    def _render_findings(self, counts=None):
        self.findings_list.delete(0, "end")
        if counts is None:
            counts = {"errors": sum(1 for f in self.findings if f.get("severity") == "error"),
                      "warnings": sum(1 for f in self.findings if f.get("severity") != "error")}
        self.findings_var.set(fmod.counts_text(counts) if self.findings else "")
        for f in self.findings:
            self.findings_list.insert("end", fmod.finding_text(f))

    def _on_finding(self, _e):
        sel = self.findings_list.curselection()
        if not sel or sel[0] >= len(self.findings):
            return
        f = self.findings[sel[0]]
        if self.mode == "json" and f.get("line"):
            self.text.tag_remove("err", "1.0", "end")
            self.text.tag_add("err", f"{f['line']}.0", f"{f['line']}.end")
            self.text.see(f"{f['line']}.0")
        elif self.mode == "form":
            for field in getattr(self, "_field_list", []):
                if field["pointer"] == f.get("pointer") and field["group"] in self._group_ids:
                    self._group = field["group"]
                    self.groups.selection_clear(0, "end")
                    self.groups.selection_set(self._group_ids.index(self._group))
                    self._render_group()
                    break

    def save_current_file(self, allow_invalid=False):
        self._commit_focused()
        state = self.states.get(self.current)
        if state is None or not state.can_edit or self.forbidden:
            return
        pid = self.project_id
        content = self._payload_content(state)
        payload = {"project_id": pid, "key": state.key, "base_digest": state.digest, **content}
        if allow_invalid:
            payload["allow_invalid"] = True
        sent = content.get("text") if "text" in content else None
        self.calls.call(CAP_ARTIFACT_SAVE, payload, on_success=lambda r, k=state.key: self._saved(k, r, pid, sent, content),
                        on_error=lambda r, k=state.key: self._save_failed(k, r))

    def _save_anyway(self):
        if self.ask_confirm(ConfirmSpec(T["save_anyway_title"], T["save_anyway_body"], T["save_anyway_go"])):
            self.save_current_file(allow_invalid=True)

    def _saved(self, key, result, pid=None, sent=None, payload=None):
        """sent - текст, ушедший из режима JSON; payload - то, что отправили (текст или структура)."""
        state = self.states.get(key)
        if state is None or (pid is not None and pid != self.project_id):
            return
        state.digest = result.get("digest")
        if sent is not None:
            # сохранён текст из окна JSON: файл теперь такой, форма должна показывать его же
            try:
                state.content = fmod.parse_json_text(sent)
            except ValueError:
                pass
            changed_after = key == self.current and self.text.get("1.0", "end-1c") != sent
            state.dirty = changed_after
            if key == self.current and not changed_after:
                self._build_groups(state)
        else:
            state.dirty = state.content != (payload or {}).get("content", state.content)
        if not state.dirty:
            state.my_edits = None
        self.findings = (result.get("validation") or {}).get("findings", [])
        self._render_findings((result.get("validation") or {}).get("counts"))
        self._update_buttons()
        notify.success(T["saved"])
        self.load_list()
        if result.get("merged") or result.get("external_changes"):
            self.reload_file()

    def _save_failed(self, key, result):
        code, _reason, _message, details = error_parts(result)
        if code == "conflict":
            self._conflict()
            return
        if code == "invalid_argument" and details.get("errors"):
            self.findings = details["errors"]
            self._render_findings()
            self._update_buttons()
            if self.mode == "form":
                self._render_group()
            return
        self.banner_var.set(T["save_failed"].format(reason=result_error_text(result).rstrip(".")))

    def _conflict(self):
        self.ask_choice(T["conflict_title"], T["conflict_body"], [("reload", T["conflict_go"])], "reload")
        self.reload_file()

    def _update_buttons(self):
        state = self.states.get(self.current)
        can = bool(state and state.can_edit and not self.forbidden)
        self.save_btn.state(["!disabled"] if can and state.dirty else ["disabled"])
        self.reload_btn.state(["!disabled"] if state else ["disabled"])
        self.validate_btn.state(["!disabled"] if state and state.exists else ["disabled"])
        self.history_btn.state(["!disabled"] if state and state.exists else ["disabled"])
        has_errors = any(f.get("severity") == "error" for f in self.findings)
        items = [self.form_rb, self.json_rb, self.save_btn]
        if can and state.dirty and has_errors:
            items.append(self.anyway_btn)
        items += [self.validate_btn, self.reload_btn, self.history_btn]
        if state and state.my_edits:
            items.append(self.edits_btn)
        self.bar.set_items(items)
        parts = [self.title_var.get().rstrip(" *")]
        if state and state.dirty and not self.title_var.get().endswith("*"):
            self.title_var.set(parts[0] + " *")
        elif state and not state.dirty:
            self.title_var.set(parts[0])

    def show_my_edits(self):
        """Буфер «Мои правки»: текст, который был в редакторе до обновления из файла (можно скопировать)."""
        state = self.states.get(self.current)
        if not state or not state.my_edits:
            return
        win = tk.Toplevel(self)
        win.title(T["my_edits"])
        txt = tk.Text(win, width=80, height=24, background=COLORS["card"])
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", state.my_edits)

    # --- интерфейс экрана для окна ---------------------------------------------------------
    def has_unsaved_changes(self):
        self._commit_focused()
        return any(s.dirty for s in self.states.values())

    def _invoke(self, cap, payload):
        if self.sync_invoke:
            return self.sync_invoke(cap, payload)
        from custom_tools.storybook.studio.capabilities import invoke
        return invoke(cap, payload)

    def save_all_files(self):
        """Сохраняет все изменённые файлы (синхронно: нужно при выходе и по таймеру). Возвращает ключи, которые не сохранились."""
        self._commit_focused()
        failed, skipped = [], []
        for key, state in self.states.items():
            if not state.dirty or not state.can_edit:
                continue
            content = {"text": self.text.get("1.0", "end-1c")} if key == self.current and self.mode == "json" else {"content": state.content}
            if "text" in content:
                try:
                    fmod.parse_json_text(content["text"])
                    self._invalid_warned.discard(key)
                except ValueError:
                    # битый JSON не шлём; предупреждаем один раз, пока текст не исправят
                    if key not in self._invalid_warned:
                        self._invalid_warned.add(key)
                        notify.warning(T["other_save_failed"].format(names=state.title))
                    failed.append(state.title)
                    skipped.append(state.title)
                    continue
            result = self._invoke(CAP_ARTIFACT_SAVE, {"project_id": self.project_id, "key": key, "base_digest": state.digest, **content})
            if result.get("status") == "success" or result.get("saved"):
                state.digest = result.get("digest", state.digest)
                state.dirty = False
            else:
                failed.append(state.title)
        real = [n for n in failed if n not in skipped]
        if real:
            notify.warning(T["other_save_failed"].format(names=", ".join(real)))
        elif self.states and not failed:
            notify.success(T["all_saved"])
        self._update_buttons()
        return failed

    def validate_current_project(self):
        names = []
        pending = [r for r in self.rows if r["item"].get("exists")]
        if not pending:
            notify.info(T["pick_file"])
            return
        bad = []

        def done(row, result):
            if not result.get("valid", True):
                bad.append(row["title"])
            names.append(row["key"])
            if len(names) == len(pending):
                notify.success(T["valid_all_ok"]) if not bad else notify.warning(T["valid_all_bad"].format(names=", ".join(bad)))

        for row in pending:
            self.calls.call(CAP_ARTIFACT_VALIDATE, {"project_id": self.project_id, "key": row["key"]},
                            on_success=lambda r, rw=row: done(rw, r),
                            on_error=lambda r, rw=row: done(rw, {"valid": False}))

    def undo(self):
        self._edit_event("<<Undo>>")

    def redo(self):
        self._edit_event("<<Redo>>")

    def _edit_event(self, name):
        widget = self.focus_get()
        if isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            widget.event_generate(name)

    def can_leave(self):
        self._commit_focused()
        return self._leave_current()

    def on_save(self):
        self.save_current_file()

    def _leave_current(self):
        """Спрашивает про несохранённые правки открытого файла; False - остаться."""
        state = self.states.get(self.current)
        if state is None or not state.dirty:
            return True
        choice = self.ask_choice(T["unsaved_title"], T["unsaved_body"].format(what=state.title),
                                 [("save", T["unsaved_save"]), ("discard", T["unsaved_discard"]), ("stay", T["unsaved_stay"])], "stay")
        if choice == "discard":
            state.dirty = False
            self.states.pop(state.key, None)
            return True
        if choice == "save":
            return not self.save_all_files()
        return False

    # --- история ------------------------------------------------------------------------
    def open_history(self):
        state = self.states.get(self.current)
        if not state:
            return
        from StoryBookManager.gui.screens.files_history import HistoryWindow

        HistoryWindow(self, self.calls, self.project_id, state, on_restored=self._after_restore)

    def _after_restore(self):
        self.load_list()
        self.reload_file()


def state_step(items, key):
    for it in items:
        if it["artifact_key"] == key:
            return it.get("produced_by") or ""
    return ""
