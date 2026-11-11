"""Раздел «Проекты»: список, поиск, подробности справа, действия над проектом."""

import io
import logging
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path
from typing import Callable, Dict, Optional

from StoryBookManager.gui.sections import project_dialogs as D
from StoryBookManager.gui.sections import projects_logic as L
from StoryBookManager.gui.sections.job_runner import JobRunner
from StoryBookManager.gui.sections.projects_controller import ProjectsController
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialogs import ConfirmDialog, show_details
from StoryBookManager.gui.shell.external import open_external
from StoryBookManager.gui.shell.widgets import Tooltip, WrapRow
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

logger = logging.getLogger(__name__)

THUMB_PX = 40
QUERY_DELAY_MS = 280
WIDE_MIN = 1000   # уже этого - подробности уходят под список


class ProjectsScreen(ttk.Frame, SectionAdapter):
    def __init__(self, parent, project_manager, on_project_selected: Callable, on_projects_changed: Callable,
                 bridge, *, on_open_settings: Optional[Callable] = None, on_open_overview: Optional[Callable] = None):
        super().__init__(parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.project_manager = project_manager
        self._on_selected = on_project_selected
        self._on_changed = on_projects_changed
        self._open_settings = on_open_settings
        self._open_overview = on_open_overview
        self.ctl = ProjectsController(bridge, JobRunner(bridge, self), self,
                                      run_bg=self._run_bg_thread)
        self.selected_id: Optional[str] = None
        self._thumbs: Dict[str, object] = {}
        self._thumb_queue: queue.Queue = queue.Queue()
        self._thumb_wanted: set = set()
        self._bg_results: queue.Queue = queue.Queue()
        self._details_token = 0
        self._details_for: Optional[str] = None
        self._query_after = None
        self._build()
        self.bind("<Configure>", self._on_configure, add="+")
        self.after(100, self._drain)

    # --- Построение ---------------------------------------------------------------
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        head = ttk.Frame(self, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=L.TXT_TITLE, style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        bar = WrapRow(head)
        bar.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        for button in (ttk.Button(bar, text=L.TXT_CREATE_FORM, style="Accent.TButton", command=self.create_by_form),
                       ttk.Button(bar, text=L.TXT_REFRESH, command=self.refresh_projects),
                       ttk.Button(bar, text=L.TXT_ALL_BACKUPS, command=lambda: self.open_backups(None))):
            bar.add(button)

        find = ttk.Frame(self, style="Panel.TFrame")
        find.grid(row=1, column=0, sticky="ew", pady=(px(PAD_BLOCK), px(PAD_INLINE)))
        find.columnconfigure(1, weight=1)
        ttk.Label(find, text=L.TXT_SEARCH).grid(row=0, column=0, padx=(0, px(PAD_INLINE)))
        self.query_var = tk.StringVar()
        self.search = ttk.Entry(find, textvariable=self.query_var)
        self.search.grid(row=0, column=1, sticky="ew")
        self.query_var.trace_add("write", lambda *a: self._query_changed())
        Tooltip(self.search, L.TXT_SEARCH_HINT)
        ttk.Label(find, text=L.TXT_SORT).grid(row=0, column=2, padx=(px(PAD_BLOCK), px(PAD_INLINE)))
        self.sort_var = tk.StringVar(value=L.SORT_LABELS[L.DEFAULT_SORT])
        self.sort_box = ttk.Combobox(find, textvariable=self.sort_var, state="readonly", width=12,
                                     values=[label for _, label in L.SORTS])
        self.sort_box.grid(row=0, column=3)
        self.sort_box.bind("<<ComboboxSelected>>", lambda e: self._sort_changed())

        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self._build_list(self.body)
        self._build_details(self.body)
        self._wide = None
        self._layout_body(True)

    def _build_list(self, parent) -> None:
        self.list_box = ttk.Frame(parent, style="Panel.TFrame")
        self.list_box.columnconfigure(0, weight=1)
        self.list_box.rowconfigure(0, weight=1)
        style = ttk.Style()
        style.configure("Projects.Treeview", rowheight=px(THUMB_PX + 4))
        self.tree = ttk.Treeview(self.list_box, style="Projects.Treeview", columns=("series", "modified", "size"),
                                 selectmode="browse", height=6)
        self.tree.heading("#0", text="Название")
        self.tree.heading("series", text="Серий")
        self.tree.heading("modified", text="Изменён")
        self.tree.heading("size", text="Размер")
        self.tree.column("#0", width=px(260), stretch=True)
        self.tree.column("series", width=px(150), stretch=False)
        self.tree.column("modified", width=px(100), stretch=False)
        self.tree.column("size", width=px(80), anchor="e", stretch=False)
        self.tree.tag_configure("muted", foreground=COLORS["text_muted"])
        self.tree.tag_configure("busy", foreground=COLORS["warning"])
        scroll = ttk.Scrollbar(self.list_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        # сообщение о состоянии списка поверх таблицы
        self.state_box = ttk.Frame(self.list_box, style="Panel.TFrame", padding=px(PAD_BLOCK))
        self.state_label = ttk.Label(self.state_box, text="", wraplength=px(520), justify="left")
        self.state_label.pack(anchor="w")
        self.state_actions = WrapRow(self.state_box)
        self.state_actions.pack(fill="x", pady=(px(PAD_INLINE), 0))
        self._state_buttons = {
            "form": ttk.Button(self.state_actions, text=L.TXT_CREATE_FORM, style="Accent.TButton", command=self.create_by_form),
            "reset": ttk.Button(self.state_actions, text=L.TXT_RESET_SEARCH, command=self._reset_search),
            "retry": ttk.Button(self.state_actions, text=L.TXT_RETRY, command=self.refresh_projects),
            "details": ttk.Button(self.state_actions, text=L.TXT_DETAILS, command=self._show_error_details),
            "settings": ttk.Button(self.state_actions, text=L.TXT_OPEN_SETTINGS, command=self._settings),
        }
        for button in self._state_buttons.values():
            self.state_actions.add(button)
            button.place_forget()

    def _build_details(self, parent) -> None:
        self.details = ttk.LabelFrame(parent, text=L.TXT_SELECTED, padding=px(PAD_BLOCK))
        self.details.columnconfigure(0, weight=1)
        self.d_title = tk.StringVar(value=L.TXT_SELECT_NONE)
        self.d_info = tk.StringVar(value="")
        self.d_spend = tk.StringVar(value="")
        self.d_hint = tk.StringVar(value="")
        ttk.Label(self.details, textvariable=self.d_title, style="Subtitle.TLabel", wraplength=px(280),
                  justify="left").grid(row=0, column=0, sticky="w")
        for row, (var, style) in enumerate(((self.d_info, "TLabel"), (self.d_spend, "Caption.TLabel"),
                                            (self.d_hint, "Caption.TLabel")), start=1):
            label = ttk.Label(self.details, textvariable=var, style=style, wraplength=px(280), justify="left")
            label.grid(row=row, column=0, sticky="w", pady=(px(PAD_INLINE) if row == 1 else 0, 0))
            var.trace_add("write", lambda *a, l=label, v=var: l.grid() if v.get() else l.grid_remove())
            label.grid_remove()
        actions = WrapRow(self.details)
        actions.grid(row=4, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        self.actions: Dict[str, ttk.Button] = {}
        for key, text, command in (
                ("open", L.TXT_OPEN, self.open_selected), ("duplicate", L.TXT_DUPLICATE, self.duplicate_selected),
                ("rename", L.TXT_RENAME, self.rename_selected), ("backups", L.TXT_BACKUPS, self.backups_selected),
                ("export", L.TXT_EXPORT, self.export_selected_project),
                ("folder", L.TXT_SHOW_FOLDER, self.show_in_folder), ("remove", L.TXT_REMOVE, self.delete_selected)):
            self.actions[key] = ttk.Button(actions, text=text, command=command)
            actions.add(self.actions[key])
        self.actions["open"].configure(style="Accent.TButton")
        self.link = ttk.Button(self.details, text=L.TXT_OPEN_OVERVIEW, command=lambda: self._open_overview and self._open_overview())
        self.link.grid(row=5, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self._update_details()

    def _layout_body(self, wide: bool) -> None:
        if wide == self._wide:
            return
        self._wide = wide
        for widget in (self.list_box, self.details):
            widget.grid_forget()
        for col in (0, 1):
            self.body.columnconfigure(col, weight=0)
        for row in (0, 1):
            self.body.rowconfigure(row, weight=0)
        if wide:
            self.body.columnconfigure(0, weight=1)
            self.body.rowconfigure(0, weight=1)
            self.list_box.grid(row=0, column=0, sticky="nsew")
            self.details.grid(row=0, column=1, sticky="ns", padx=(px(PAD_BLOCK), 0))
        else:
            self.body.columnconfigure(0, weight=1)
            self.body.rowconfigure(0, weight=1)
            self.list_box.grid(row=0, column=0, sticky="nsew")
            self.details.grid(row=1, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))

    def _on_configure(self, event) -> None:
        if event.widget is self:
            self._layout_body(event.width >= px(WIDE_MIN))

    # --- Фасад для оболочки -----------------------------------------------------------------
    def on_show(self) -> None:
        if not self.ctl.loaded and not self.ctl.loading:
            self.refresh_projects()

    def on_refresh(self) -> None:
        self.refresh_projects()

    def focus_search(self) -> None:
        self.search.focus_set()
        self.search.selection_range(0, "end")

    def refresh_projects(self) -> None:
        self._details_for = None
        self.ctl.refresh()

    def create_new_project(self) -> None:
        self.create_by_form()

    def _created(self, result: dict) -> None:
        notify.success(L.TOAST_CREATED)
        self.ctl.refresh()
        project_id = result.get("project_id")
        if project_id:
            self._open(project_id)

    def create_by_form(self) -> None:
        ref = []

        def submit(values: dict) -> None:
            dlg = ref[0]
            dlg.set_busy(True)

            def done(ok: bool, error: str, result: dict) -> None:
                if not self._alive(dlg):
                    return
                dlg.set_busy(False)
                if ok:
                    dlg.close()
                    self._created(result)
                else:
                    dlg.show_error(error)

            self.ctl.create_by_form(values, done)

        ref.append(D.CreateByFormDialog(self, submit, self.ctl.suggest_id))

    def export_selected_project(self) -> None:
        if self._need_selection():
            self.ctl.export(self.selected_id)

    # --- Действия над выбранным ------------------------------------------------------------------
    def _need_selection(self) -> bool:
        if self.selected_id:
            return True
        notify.info(L.TXT_NO_PROJECT_SELECTED)
        return False

    def open_selected(self) -> None:
        card = self.ctl.card(self.selected_id)
        if card is None or not L.gate_open(card).allowed:
            return
        self._open(card["project_id"])

    def _open(self, project_id: str) -> None:
        project = self.project_manager.load_project(project_id)
        if project is None:
            notify.error("Не удалось открыть проект.")
            return
        self._on_selected(project)

    def rename_selected(self) -> None:
        card = self.ctl.card(self.selected_id)
        if card is None or not L.gate_change(card).allowed:
            return
        ref = []

        def submit(title: str) -> None:
            def done(ok: bool, error: str, result: dict) -> None:
                if ok:
                    ref[0].close()
                    self._on_changed()
                else:
                    ref[0].show_error(error)

            self.ctl.rename(card["project_id"], title, done)

        ref.append(D.RenameDialog(self, card.get("title") or card["project_id"], submit))

    def duplicate_selected(self) -> None:
        card = self.ctl.card(self.selected_id)
        if card is None or not L.gate_copy(card, self.ctl.jobs.is_busy(card["project_id"])).allowed:
            return
        ref = []

        def submit(id_hint: str) -> None:
            def done(ok: bool, error: str, result: dict) -> None:
                if ok:
                    ref[0].close()
                else:
                    ref[0].show_error(error)

            self.ctl.duplicate(card["project_id"], id_hint, done)

        ref.append(D.DuplicateDialog(self, card["project_id"], submit))

    def delete_selected(self) -> None:
        if self._need_selection():
            self.ctl.delete(self.selected_id)

    def backups_selected(self) -> None:
        self.open_backups(self.selected_id)

    def open_backups(self, project_id: Optional[str]) -> None:
        D.BackupsDialog(self, project_id, load=self.ctl.backups_load, create=self.ctl.backup_create,
                        restore=self.ctl.backup_restore)

    def show_in_folder(self) -> None:
        if self._need_selection():
            open_external(str(self._project_dir(self.selected_id)))

    def _project_dir(self, project_id: str) -> str:
        return str(Path(self.ctl.projects_dir) / project_id)

    # --- Интерфейс для контроллера ---------------------------------------------------------------
    def list_updated(self) -> None:
        state, rows = self.ctl.view_rows()
        for item in self.tree.get_children():
            self.tree.delete(item)
        if state.kind != "rows":
            self.state_label.configure(text=state.text)
            self.state_actions.set_items([b for k, b in self._state_buttons.items() if k in state.actions])
            self.state_box.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.state_box.lift()
        else:
            self.state_box.place_forget()
        for row in rows if state.kind == "rows" else ():
            self.tree.insert("", "end", iid=row.project_id, text=row.title, tags=row.tags,
                             values=(row.series, row.modified, row.size))
            self._load_thumb(row.project_id)
        if self.selected_id and self.tree.exists(self.selected_id):
            self.tree.selection_set(self.selected_id)
        elif self.selected_id and self.ctl.loaded:
            self.selected_id = None
        self._update_details()

    def row_updated(self, project_id: str) -> None:
        if not self.tree.exists(project_id):
            return
        card = self.ctl.card(project_id)
        if card is None:
            return
        row = L.row_for(card, self.ctl.sizes.sizes.get(project_id), self.ctl.job_text.get(project_id, ""))
        self.tree.item(project_id, tags=row.tags, values=(row.series, row.modified, row.size))
        if project_id == self.selected_id:
            self._update_details()

    def select_project(self, project_id: str) -> None:
        self.selected_id = project_id
        if self.tree.exists(project_id):
            self.tree.selection_set(project_id)
            self.tree.see(project_id)
        self._update_details()

    def open_project(self, project_id: str) -> None:
        self._open(project_id)

    def confirm(self, spec) -> bool:
        return ConfirmDialog.ask(self, spec)

    def ask_save_path(self, initial: str) -> str:
        return filedialog.asksaveasfilename(parent=self, initialfile=initial, defaultextension=".zip",
                                            filetypes=[("ZIP", "*.zip")]) or ""

    def toast_success(self, text: str) -> None:
        notify.success(text)

    def toast_info(self, text: str) -> None:
        notify.info(text)

    def toast_error(self, text: str, details: str = "") -> None:
        notify.error(text, details or None)

    def show_blocking(self, text: str) -> None:
        show_details(self, text, "Операция недоступна")

    def project_removed(self, project_id: str) -> None:
        if self.selected_id == project_id:
            self.selected_id = None
        self._on_changed()

    def schedule(self, ms: int, fn: Callable) -> None:
        self.after(ms, fn)

    def is_visible(self) -> bool:
        try:
            return bool(self.winfo_viewable()) and self.winfo_toplevel().state() != "iconic"
        except tk.TclError:
            return False

    # --- Поиск, сортировка, выбор ---------------------------------------------------------------------
    def _query_changed(self) -> None:
        """Фильтр применяется после паузы в наборе, а не на каждую букву."""
        if self._query_after is not None:
            self.after_cancel(self._query_after)
        self._query_after = self.after(QUERY_DELAY_MS, self._apply_query)

    def _apply_query(self) -> None:
        self._query_after = None
        self.ctl.query = self.query_var.get()
        self.list_updated()

    def _reset_search(self) -> None:
        self.query_var.set("")
        self.search.focus_set()

    def _sort_changed(self) -> None:
        label = self.sort_var.get()
        self.ctl.sort = next((v for v, text in L.SORTS if text == label), L.DEFAULT_SORT)
        self.list_updated()

    def _on_tree_select(self) -> None:
        selection = self.tree.selection()
        self.selected_id = selection[0] if selection else None
        self._update_details()

    def _show_error_details(self) -> None:
        error = self.ctl.error or {}
        show_details(self, f"{error.get('code', '')}\n{error.get('message', '')}\n{error.get('details') or ''}")

    def _settings(self) -> None:
        if self._open_settings:
            self._open_settings()

    # --- Подробности -----------------------------------------------------------------------------------------
    def _update_details(self) -> None:
        card = self.ctl.card(self.selected_id)
        if card is None:
            self.d_title.set(L.TXT_SELECT_NONE)
            self.d_info.set("")
            self.d_spend.set("")
            self.d_hint.set("")
            self._details_for = None
            self._details_token += 1
            for button in self.actions.values():
                button.state(["disabled"])
            self.link.state(["disabled"])
            return
        project_id = card["project_id"]
        self.d_title.set(card.get("title") or project_id)
        size = self.ctl.sizes.sizes.get(project_id)
        size_text = L.format_bytes(size) if size is not None else (
            L.TXT_COUNTING_SIZES if self.ctl.sizes.remaining else "—")
        lines = [f"Серий: {card.get('series_count', 0)}", f"Язык: {card.get('language') or '—'}",
                 f"Изменён: {L.format_date(card.get('modified'))}", f"{L.TXT_SIZE}: {size_text}"]
        if card.get("description"):
            lines.append(card["description"])
        self.d_info.set("\n".join(lines))
        job = self.ctl.jobs.is_busy(project_id)
        gates = {"open": L.gate_open(card), "rename": L.gate_change(card), "remove": L.gate_change(card),
                 "duplicate": L.gate_copy(card, job), "export": L.gate_copy(card, job)}
        for key, button in self.actions.items():
            gate = gates.get(key, L.Gate(True))
            button.state(["!disabled"] if gate.allowed else ["disabled"])
        self.link.state(["!disabled"])
        self.d_hint.set(next((g.reason for g in gates.values() if not g.allowed), ""))
        if project_id == self._details_for:
            return   # выбранный проект тот же: траты не перезапрашиваем
        self._details_for = project_id
        self._details_token += 1
        token = self._details_token
        self.d_spend.set("")

        def got(details, brief) -> None:
            if token == self._details_token and details is not None:
                resources = details.get("resources", {})
                counts = (("Герои", "characters"), ("Места", "locations"), ("Предметы", "items"),
                          ("Стили", "style"), ("Рассказчики", "narrators"))
                self.d_spend.set(" · ".join(f"{label}: {len(resources.get(key, {}))}" for label, key in counts))

        self.ctl.load_details(project_id, got)

    # --- Миниатюры и фон --------------------------------------------------------------------------------------
    def _load_thumb(self, project_id: str) -> None:
        card = self.ctl.card(project_id)
        cover = (card or {}).get("cover")
        if project_id in self._thumbs:
            self.tree.item(project_id, image=self._thumbs[project_id])
            return
        if not cover or project_id in self._thumb_wanted:
            return
        self._thumb_wanted.add(project_id)

        def work():
            try:
                from custom_tools.storybook.studio.thumbs import make_thumb
                from PIL import Image
                data, _ = make_thumb(project_id, cover, 128)
                raw = data if isinstance(data, bytes) else data.read_bytes()
                image = Image.open(io.BytesIO(raw)).convert("RGB").resize((px(THUMB_PX), px(THUMB_PX)))
                self._thumb_queue.put((project_id, image))
            except Exception:
                logger.debug("Нет миниатюры для %s", project_id, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def _run_bg_thread(self, func: Callable, done: Callable) -> None:
        def work():
            self._bg_results.put((done, func()))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        try:
            self._drain_thumbs()
            self._drain_background()
        finally:
            try:
                self.after(100, self._drain)
            except tk.TclError:
                pass

    def _drain_thumbs(self) -> None:
        while True:
            try:
                project_id, image = self._thumb_queue.get_nowait()
            except queue.Empty:
                return
            try:
                from PIL import ImageTk
                photo = ImageTk.PhotoImage(image, master=self)
                self._thumbs[project_id] = photo
                if self.tree.exists(project_id):
                    self.tree.item(project_id, image=photo)
            except Exception:
                logger.exception("Не удалось показать миниатюру %s", project_id)

    def _drain_background(self) -> None:
        while True:
            try:
                done, outcome = self._bg_results.get_nowait()
            except queue.Empty:
                return
            try:
                done(outcome)
            except Exception:
                logger.exception("Ошибка обработки фоновой работы")

    @staticmethod
    def _alive(dialog) -> bool:
        try:
            return bool(dialog.winfo_exists())
        except tk.TclError:
            return False

    def destroy(self) -> None:
        if self._query_after is not None:
            self.after_cancel(self._query_after)
            self._query_after = None
        self.ctl.close()
        super().destroy()
