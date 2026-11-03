"""Режим «Иллюстрации» экрана «Файлы»: список по страницам, картинка с зумом, свойства, правка ИИ."""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Optional

from StoryBookManager.gui.imagetools import edit_model as EM
from StoryBookManager.gui.imagetools import gallery_model as GM
from StoryBookManager.gui.imagetools.edit_controller import EditController
from StoryBookManager.gui.imagetools.edit_dialog import ImageEditDialog
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT
from StoryBookManager.gui.imagetools.texts import TK_ONLY
from StoryBookManager.gui.imagetools.zoom_canvas import ZoomCanvas
from StoryBookManager.gui.screens.calls import CAP_MEDIA_INVENTORY, StudioCalls
from StoryBookManager.gui.screens.common_model import format_bytes, format_created_at
from StoryBookManager.gui.sections.job_runner import JobRunner
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.widgets import WrapRow
from StoryBookManager.gui.shots.model import is_run_busy, result_error_text
from StoryBookManager.gui.theme_tokens import PAD_INLINE, px

logger = logging.getLogger(__name__)

INVENTORY_LIMIT = 2000
LIST_WIDTH = 280


def resolve_abs(project_id: Optional[str], rel: str) -> Optional[str]:
    """Полный путь файла проекта; None, если путь не из проекта."""
    if not project_id or not rel:
        return None
    try:
        from custom_tools.storybook.studio.paths import resolve_in_project

        return str(resolve_in_project(project_id, rel))
    except Exception:
        return None


class GalleryView(ttk.Frame):
    def __init__(self, parent, bridge, get_project_id, get_run_state=None):
        super().__init__(parent, style="Panel.TFrame")
        self.get_run_state = get_run_state or (lambda: None)
        self.calls = StudioCalls(bridge)
        self.get_project_id = get_project_id
        self.bridge = bridge
        self._new_edit()
        self.items: List[dict] = []
        self.by_path: Dict[str, dict] = {}
        self.current: Optional[str] = None
        self.state = "idle"     # idle, loading, ready, error
        self._loaded_for: Optional[str] = None
        self._gen = 0
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build()

    def _new_edit(self) -> None:
        self.runner = JobRunner(self.bridge, self)
        self.edit_ctl = EditController(self.bridge, self.runner, notify, self.get_project_id)

    # --- построение ---------------------------------------------------------------------
    def _build(self) -> None:
        left = ttk.Frame(self, style="Panel.TFrame", width=px(LIST_WIDTH))
        left.grid(row=0, column=0, sticky="ns", padx=(0, px(PAD_INLINE)))
        left.grid_propagate(False)
        left.rowconfigure(3, weight=1)
        left.columnconfigure(0, weight=1)
        self.count = tk.StringVar()
        ttk.Label(left, textvariable=self.count, style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left, text=IT["gallery_search"], style="Caption.TLabel").grid(row=1, column=0, sticky="w")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._fill_list())
        self.search = ttk.Entry(left, textvariable=self.query)
        self.search.grid(row=2, column=0, sticky="ew", pady=(0, px(PAD_INLINE)))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.message = tk.StringVar()
        ttk.Label(left, textvariable=self.message, style="Caption.TLabel", wraplength=px(LIST_WIDTH - 20),
                  justify="left").grid(row=4, column=0, sticky="w")
        self.retry_btn = ttk.Button(left, text=TK_ONLY["gallery_retry"], command=self.reload)

        right = ttk.Frame(self, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        bar = WrapRow(right)
        bar.grid(row=0, column=0, sticky="ew")
        self.canvas = ZoomCanvas(right, on_change=self._on_zoom, empty_text=TK_ONLY["gallery_pick"])
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=px(PAD_INLINE))
        self.zoom_buttons = {}
        for name, text_id, command in (("in", "zoom_in", lambda: self.canvas.zoom("in")),
                                       ("out", "zoom_out", lambda: self.canvas.zoom("out")),
                                       ("fit", "zoom_fit", self.canvas.fit),
                                       ("actual", "zoom_actual", self.canvas.actual)):
            self.zoom_buttons[name] = bar.add(ttk.Button(bar, text=IT[text_id], command=command))
        self.level = tk.StringVar()
        bar.add(ttk.Label(bar, textvariable=self.level, width=6))
        self.edit_btn = bar.add(ttk.Button(bar, text=IT["edit_open"], command=self.open_edit, state="disabled"))
        self.canvas.bind_keys(self.canvas)
        self.info = tk.StringVar()
        ttk.Label(right, textvariable=self.info, justify="left", wraplength=px(560)).grid(row=2, column=0, sticky="w")
        self.status = tk.StringVar()
        ttk.Label(right, textvariable=self.status, style="Caption.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Label(right, text=IT["zoom_hint"], style="Caption.TLabel", wraplength=px(560),
                  justify="left").grid(row=4, column=0, sticky="w")

    def _on_zoom(self, percent: str) -> None:
        self.level.set(IT["zoom_level"].replace("{pct}", percent.rstrip("%")))

    # --- загрузка списка ---------------------------------------------------------------------
    def set_project(self) -> None:
        """Проект сменился: забыть список; перечитается при показе режима."""
        if self.edit_ctl.closed:
            self._new_edit()   # после shutdown() прежний опрос закрыт: новому проекту нужен свежий
        self.items, self.by_path, self.current = [], {}, None
        self.state, self._loaded_for = "idle", None
        self._gen += 1
        self.canvas.load(None)
        self._fill_list()
        self._show_info()

    def ensure_loaded(self) -> None:
        if self.get_project_id() and (self.state == "idle" or self._loaded_for != self.get_project_id()):
            self.reload()

    def reload(self) -> None:
        project = self.get_project_id()
        if not project:
            return
        self._gen += 1
        gen = self._gen
        self.state = "loading"
        self.message.set(TK_ONLY["gallery_loading"])
        self.retry_btn.grid_remove()

        def ok(result):
            if gen == self._gen:
                self._loaded(project, result.get("items") or [])

        def fail(result):
            if gen == self._gen:
                self.state = "error"
                self.message.set(f"{IT['gallery_load_error']}: {result_error_text(result).rstrip('.')}.")
                self.retry_btn.grid(row=5, column=0, sticky="w")

        self.calls.call(CAP_MEDIA_INVENTORY, {"project_id": project, "types": ["illustration"],
                                              "limit": INVENTORY_LIMIT, "probe_limit": 0},
                        on_success=ok, on_error=fail, key="gallery_inventory")

    def _loaded(self, project: str, items: List[dict]) -> None:
        self.state, self._loaded_for = "ready", project
        self.items = items
        self.by_path = {i["path"]: i for i in items}
        self.message.set("" if items else IT["gallery_empty"])
        self.retry_btn.grid_remove()
        self._fill_list()
        if self.current not in self.by_path:
            self.current = None
        if self.current:
            self.tree.selection_set(self.current)
        self._show_current()

    # --- список ------------------------------------------------------------------------------
    def _fill_list(self) -> None:
        self.count.set(IT["gallery_count"].replace("{n}", str(len(self.items))) if self.items else "")
        self.tree.delete(*self.tree.get_children())
        for group in GM.filtered_groups(self.items, self.query.get()):
            node = self.tree.insert("", "end", iid=f"group:{group['page']}", text=group["title"], open=True)
            for item in group["items"]:
                self.tree.insert(node, "end", iid=item["path"], text=item["name"])
        if self.current and self.tree.exists(self.current):
            self.tree.selection_set(self.current)

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection and selection[0] in self.by_path and selection[0] != self.current:
            self.current = selection[0]
            self._show_current()

    # --- картинка и свойства -------------------------------------------------------------------------
    def _show_current(self) -> None:
        item = self.by_path.get(self.current or "")
        if item:
            self.canvas.load(resolve_abs(self.get_project_id(), item["path"]), TK_ONLY["image_load_failed"])
        else:
            self.canvas.load(None, TK_ONLY["gallery_pick"])
        self._show_info()
        self._sync_edit()

    def _show_info(self) -> None:
        item = self.by_path.get(self.current or "")
        if not item:
            self.info.set("")
            return
        w, h = self.canvas.image_size
        self.info.set("\n".join(GM.file_info_lines(item["name"], format_bytes(item.get("size")),
                                                     format_created_at(item.get("modified")), w, h)))

    def edit_blocked(self) -> bool:
        """Правка выключена, пока идёт другая правка или прогон проекта (как runBusy в React)."""
        return self.edit_ctl.is_running() or is_run_busy(self.get_run_state())

    def on_run_state(self) -> None:
        """Состояние прогона изменилось: кнопка правки пересчитывается."""
        try:
            self._sync_edit()
        except tk.TclError:
            pass

    def _sync_edit(self) -> None:
        item = self.by_path.get(self.current or "")
        ok = bool(item) and EM.editable_extension(item["name"]) and not self.edit_blocked()
        self.edit_btn.configure(state="normal" if ok else "disabled")

    # --- правка ИИ --------------------------------------------------------------------------------------
    def open_edit(self) -> None:
        item = self.by_path.get(self.current or "")
        if not item or self.edit_blocked():
            return
        if not EM.editable_extension(item["name"]):
            notify.warning(IT["edit_not_supported"])
            return
        target = {"path": item["path"], "name": item["name"], "shot_key": "", "part": ""}
        ImageEditDialog(self, self.edit_ctl, target, resolve_abs(self.get_project_id(), item["path"]),
                        on_done=self._edit_done, on_status=self._edit_status)
        self._sync_edit()

    def shutdown(self) -> None:
        """Закрытие экрана или смена проекта: опрос задания правки и ответы контроллера останавливаются."""
        self.edit_ctl.close()
        self.runner.close()

    def _edit_status(self, text: str) -> None:
        try:
            self.status.set(text)
            self._sync_edit()
        except tk.TclError:
            pass

    def _edit_done(self, _result: dict) -> None:
        try:
            self.reload()
        except tk.TclError:
            pass
