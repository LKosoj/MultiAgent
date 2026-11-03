"""Экран «Кадры»: собирает фильтры, список, карточку и связывает их с контроллером."""

import logging
import os
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Tuple

from StoryBookManager.gui.imagetools.edit_controller import EditController
from StoryBookManager.gui.imagetools.edit_dialog import ImageEditDialog
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS
from StoryBookManager.gui.sections.job_runner import JobRunner
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.external import play_video
from StoryBookManager.gui.shell.widgets import PanelHeader, Tooltip, WrapRow
from StoryBookManager.gui.shots import keys as K
from StoryBookManager.gui.shots import layout as L
from StoryBookManager.gui.shots import model as M
from StoryBookManager.gui.shots import texts as T
from StoryBookManager.gui.shots.card_view import CardView
from StoryBookManager.gui.shots.controller import ShotsController
from StoryBookManager.gui.shots.dialogs import ChoiceDialog, CompareDialog, ImageViewer, RegenerateDialog
from StoryBookManager.gui.shots.filter_bar import FilterBar
from StoryBookManager.gui.shots.list_view import ShotsListView
from StoryBookManager.gui.shots.thumb_loader import ThumbLoader
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, PAD_PANEL, px

logger = logging.getLogger(__name__)


def _load_photo(path: Optional[str], box: Tuple[int, int]):
    """Картинка с диска, вписанная в рамку (главный поток, вызывается по одному окну)."""
    if not path:
        return None
    try:
        from PIL import Image, ImageTk

        with Image.open(path) as image:
            image.load()
            pil = image.copy()
        pil.thumbnail(box)
        return ImageTk.PhotoImage(pil)
    except Exception:
        logger.debug("Картинка %s не открылась", path, exc_info=True)
        return None


class ShotsPanel(ttk.Frame):
    def __init__(self, parent, *, bridge, get_project_id: Callable[[], Optional[str]],
                 on_goto_generation: Callable[[], None], on_run_started: Optional[Callable[[], None]] = None,
                 on_open_generation: Optional[Callable[[list], None]] = None):
        super().__init__(parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._get_project_id = get_project_id
        self._goto_generation = on_goto_generation
        self._open_generation = on_open_generation
        self.ctl = ShotsController(bridge, self, notify, lambda ms, fn: self.after(ms, fn), get_project_id,
                                   on_started=on_run_started)
        self.edit_runner = JobRunner(bridge, self)
        self.edit_ctl = EditController(bridge, self.edit_runner, notify, get_project_id)
        S = T.SHOT_TEXTS
        PanelHeader(self, S["title"]).grid(row=0, column=0, sticky="ew")
        self.filter_bar = FilterBar(self, on_change=self.ctl.set_filters, on_mode=self.ctl.set_mode,
                                    on_reset=self.ctl.reset_filters)
        self.filter_bar.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.banner = ttk.Frame(self, style="Panel.TFrame")
        self.banner_label = ttk.Label(self.banner, text="", style="Caption.TLabel")
        self.banner_label.pack(side="left")
        ttk.Button(self.banner, text=S["baseline_button"], command=self.ctl.adopt_baseline).pack(side="left", padx=px(8))
        self.banner.grid(row=2, column=0, sticky="ew")
        self.banner.grid_remove()
        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=3, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(1, weight=1)
        self.left = ttk.Frame(self.body, style="Panel.TFrame")
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, px(PAD_INLINE)))
        self.left.rowconfigure(0, weight=1)
        self.left.columnconfigure(0, weight=1)
        self.list_view = ShotsListView(self.left, loader_factory=self._loader_factory, on_select=self._on_pick,
                                       on_toggle_mark=self.ctl.toggle_mark, on_near_end=self.ctl.maybe_load_more)
        self.list_view.grid(row=0, column=0, sticky="nsew")
        self.message = ttk.Frame(self.left, style="Panel.TFrame")
        self.message_label = ttk.Label(self.message, text="", wraplength=px(240), justify="left")
        self.message_label.pack(anchor="w")
        self.message_button = ttk.Button(self.message)
        self.message.grid(row=0, column=0, sticky="nw")
        self.message.grid_remove()
        self._build_bulk()
        self.card_view = CardView(self.body, loader_factory=self._loader_factory, resolve=self._resolve,
                                  actions={"letter": self.ctl.set_review_letter, "save": self.ctl.save,
                                           "discard": self.ctl.discard_edit, "regen": self.ctl.open_regen,
                                           "watch": self._watch, "view": self._view_images,
                                           "compare": self._compare, "restore": self._restore,
                                           "edit": self._edit_part})
        self.card_view.grid(row=0, column=1, sticky="nsew")
        self.edit_status = tk.StringVar()
        self.edit_status_label = ttk.Label(self, textvariable=self.edit_status, style="Caption.TLabel")
        self.edit_status_label.grid(row=4, column=0, sticky="w")
        self.edit_status_label.grid_remove()
        self.bind("<Configure>", self._on_resize)
        self._bind_keys()
        self._regen_dialog = None

    # --- сборка -----------------------------------------------------------------------------
    def _loader_factory(self, widget, on_ready):
        return ThumbLoader(widget, self._get_project_id, on_ready)

    def _build_bulk(self) -> None:
        S = T.SHOT_TEXTS
        self.bulk = ttk.Frame(self.left, style="Panel.TFrame")
        self.bulk_label = ttk.Label(self.bulk, text="", style="Caption.TLabel")
        self.bulk_label.pack(anchor="w")
        row = WrapRow(self.bulk)
        row.pack(fill="x")
        self.bulk_regen_btn = None
        for text, command in ((S["bulk_approve"], lambda: self.ctl.set_status_for_marked("approved")),
                              (S["bulk_redo"], lambda: self.ctl.set_status_for_marked("redo")),
                              (S["bulk_regen"], self.ctl.open_regen),
                              (S["close"], self.ctl.clear_marks)):
            btn = ttk.Button(row, text=text, command=command)
            row.add(btn)
            if text == S["bulk_regen"]:
                self.bulk_regen_btn = btn
        if self._open_generation is not None:
            from StoryBookManager.gui.generation.texts import fmt as gen_fmt
            row.add(ttk.Button(row, text=gen_fmt("open_in_generation"), command=self._open_marked_in_generation))
        self.bulk_tip = Tooltip(self.bulk_regen_btn, lambda: self.ctl.regen_block())
        self.bulk.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        self.bulk.grid_remove()

    def _open_marked_in_generation(self) -> None:
        """Отмеченные шоты уходят в «Генерацию» для режима «Перегенерировать выбранные шоты»."""
        keys = M.bulk_target_keys(self.ctl.order, self.ctl.marked, self.ctl.selected)
        if keys:
            self._open_generation(list(keys))

    def _bind_keys(self) -> None:
        top = self.winfo_toplevel()
        self._top = top
        for action, sequences in K.all_sequences().items():
            for seq in sequences:
                top.bind(seq, lambda e, a=action: self._on_key(e, a), add="+")

    def _on_key(self, event, action: str):
        widget = event.widget
        try:
            cls = widget.winfo_class()
            mapped = self.winfo_ismapped()
            toplevel = widget.winfo_toplevel()
        except tk.TclError:
            return None
        modal = toplevel is not self._top
        if not K.should_handle(cls, action, modal, mapped):
            return None
        if action == "prev":
            self.ctl.move(-1)
        elif action == "next":
            self.ctl.move(1)
        elif action in K.STATUS_ACTIONS:
            self.ctl.set_review_letter(K.STATUS_ACTIONS[action])
        elif action == "mark":
            self.ctl.toggle_mark()
        elif action == "mark_all":
            self.ctl.mark_all()
        elif action == "open":
            self._open_selected()
        return "break"

    def _on_resize(self, event) -> None:
        if event.widget is self:
            self.body.columnconfigure(0, minsize=px(L.left_column_width(event.width)))
            self.left.configure(width=px(L.left_column_width(event.width)))

    # --- протокол вида для контроллера -------------------------------------------------------
    def is_visible(self) -> bool:
        try:
            return bool(self.winfo_ismapped())
        except tk.TclError:
            return False

    def read_form(self):
        return self.card_view.read_form()

    def ask_unsaved(self, what: str) -> str:
        S = T.SHOT_TEXTS
        result = ChoiceDialog.ask(self, S["unsaved_title"], T.fmt("unsaved_body", what=what),
                                  [("save", S["unsaved_save"]), ("discard", S["unsaved_discard"]),
                                   ("stay", S["unsaved_stay"])], default="save")
        return result or "stay"

    def ask_conflict(self, body: str) -> str:
        S = T.SHOT_TEXTS
        result = ChoiceDialog.ask(self, S["conflict_title"], body,
                                  [("refresh", S["conflict_refresh"]), ("keep", S["conflict_keep"])], default="refresh")
        return result or "refresh"

    def confirm_restore(self, number) -> bool:
        S = T.SHOT_TEXTS
        return ChoiceDialog.ask(self, T.fmt("restore_title", n=number), S["restore_body"],
                                [("no", S["regen_cancel"]), ("yes", S["restore_confirm"])], default="no") == "yes"

    def open_regen(self, keys: List[str]) -> None:
        self._regen_dialog = RegenerateDialog(self, self.ctl, keys)

    def scroll_to_key(self, key: str) -> None:
        self.list_view.scroll_to_key(key)

    def refresh(self, what: str) -> None:
        try:
            handler = getattr(self, "_refresh_" + what, None)
            if handler is not None:
                handler()
        except tk.TclError:
            logger.debug("Обновление части %s не удалось", what, exc_info=True)

    # --- обновление частей ---------------------------------------------------------------------
    def _refresh_filters(self) -> None:
        c = self.ctl
        self.filter_bar.update_facets(c.facets, c.review_titles, c.state_titles)
        self.filter_bar.show(c.filters, c.found_count, c.review_titles, c.state_titles)

    def _refresh_list(self) -> None:
        c = self.ctl
        S = T.SHOT_TEXTS
        self.filter_bar.show(c.filters, c.found_count, c.review_titles, c.state_titles)
        text, button = None, None
        if c.load_state == "loading":
            text = S["loading"]
        elif c.load_state == "error":
            text, button = T.fmt("load_error", reason=c.load_error), (S["retry"], c.reload)
        elif c.load_state == "empty":
            text, button = S["empty"], (S["empty_goto"], self._goto_generation)
        elif c.load_state == "empty_filter":
            text, button = S["empty_filter"], (S["reset_filters"], c.reset_filters)
        if text is not None:
            self.list_view.grid_remove()
            self.message_label.configure(text=text)
            self.message_button.pack_forget()
            if button:
                self.message_button.configure(text=button[0], command=button[1])
                self.message_button.pack(anchor="w", pady=(px(8), 0))
            self.message.grid()
            return
        self.message.grid_remove()
        self.list_view.grid()
        self.list_view.set_data(c.visible_items, c.marked, c.selected, c.mode)

    def _refresh_selection(self) -> None:
        self.list_view.set_marks(self.ctl.marked, self.ctl.selected)

    def _refresh_bulk(self) -> None:
        self.list_view.set_marks(self.ctl.marked, self.ctl.selected)
        n = len(self.ctl.marked)
        if n:
            self.bulk_label.configure(text=T.fmt("bulk_selected", n=n))
            self._sync_bulk_regen()
            self.bulk.grid()
        else:
            self.bulk.grid_remove()

    def _refresh_banner(self) -> None:
        missing = (self.ctl.baseline or {}).get("missing", 0)
        if missing:
            self.banner_label.configure(text=T.fmt("baseline_banner", n=missing))
            self.banner.grid()
        else:
            self.banner.grid_remove()

    def _refresh_busy(self) -> None:
        reason = self.ctl.regen_block()
        self.card_view.set_busy(reason)
        self.card_view.set_edit_block(self._edit_block())
        self._sync_bulk_regen()

    def _sync_bulk_regen(self) -> None:
        self.bulk_regen_btn.configure(state="disabled" if self.ctl.regen_block() else "normal")

    def _refresh_secondary(self) -> None:
        self.card_view.show_secondary(self.ctl.reports, self.ctl.versions)

    def _refresh_card_reset(self) -> None:
        self.card_view.reset()
        self._refresh_card()

    def _refresh_card(self) -> None:
        c = self.ctl
        if c.card_state == "none":
            self.card_view.show_message(T.TK_ONLY["pick_shot"])
        elif c.card_state == "loading":
            self.card_view.show_message(T.SHOT_TEXTS["loading"])
        elif c.card_state == "error":
            self.card_view.show_message(T.fmt("card_error", reason=c.card_error),
                                        retry=lambda: c.select(c.selected, immediate=True, force=True))
        else:
            draft, note = c.draft, c.draft_note
            if draft is None and self.card_view.current_key is not None and \
                    self.card_view.current_key == (c.card or {}).get("shot_key"):
                draft, note = self.card_view.read_form()   # правки в полях переживают смену ревью
            row = c.row_of(c.selected) or {}
            self.card_view.show_card(c.card, c.entry or {}, {"busy_reason": c.regen_block(),
                                                             "edit_block": self._edit_block(),
                                                             "generating": M.is_generating(row)}, draft, note)
            if c.reports is not None or c.versions is not None:
                self.card_view.show_secondary(c.reports, c.versions)

    # --- действия пользователя ----------------------------------------------------------------
    def _on_pick(self, key: str) -> None:
        self.ctl.select(key)

    def _open_selected(self) -> None:
        card = self.ctl.card
        rel = ((card or {}).get("parts") or {}).get("start", {}).get("path")
        if rel and (card.get("parts") or {}).get("start", {}).get("exists"):
            self._view_images([(T.part_title("start"), rel)], 0)

    def _resolve(self, rel: str) -> Optional[str]:
        project = self._get_project_id()
        if not project or not rel:
            return None
        try:
            from custom_tools.storybook.studio.paths import resolve_in_project

            return str(resolve_in_project(project, rel))
        except Exception:
            return None

    def _watch(self, rel: str) -> None:
        path = self._resolve(rel) or (rel if rel and rel.startswith("/") else None)
        if not path or not play_video(path):
            notify.error(T.TK_ONLY["open_failed"])

    def _view_images(self, items: List[Tuple[str, str]], index: int) -> None:
        paths = [(title, self._resolve(rel)) for title, rel in items]
        parts = ((self.ctl.card or {}).get("parts") or {})
        edit_parts = [next((p for p in ("start", "end") if (parts.get(p) or {}).get("path") == rel), None)
                      for _, rel in items]
        ImageViewer(self, (self.ctl.card or {}).get("shot_key", ""), paths, index, on_edit=self._edit_part,
                    edit_parts=edit_parts, is_blocked=self._edit_block)

    # --- правка картинки ИИ -------------------------------------------------------------------
    def _edit_block(self) -> str:
        """Почему «Править ИИ…» выключена: идёт прогон или перегенерация шота, либо уже идёт другая правка."""
        return self.ctl.regen_block() or (IMAGE_TEXTS["edit_busy"] if self.edit_ctl.is_running() else "")

    def _edit_part(self, part: str, viewer=None) -> None:
        card = self.ctl.card or {}
        rel = ((card.get("parts") or {}).get(part) or {}).get("path")
        if not rel or self._edit_block():
            return
        target = {"path": rel, "name": os.path.basename(rel), "shot_key": card.get("shot_key", ""), "part": part}
        ImageEditDialog(viewer or self, self.edit_ctl, target, self._resolve(rel),
                        on_done=lambda result: self._edit_done(viewer), on_status=self._edit_status)
        self.card_view.set_edit_block(self._edit_block())

    def _edit_status(self, text: str) -> None:
        try:
            self.edit_status.set(text)
            if text:
                self.edit_status_label.grid()
            else:
                self.edit_status_label.grid_remove()
            self.card_view.set_edit_block(self._edit_block())
        except tk.TclError:
            logger.debug("Строка правки не обновилась", exc_info=True)

    def _edit_done(self, viewer) -> None:
        """Картинка заменена: карточка и список перечитываются, открытый просмотр показывает новый файл."""
        try:
            self.ctl.reload_card()
            self.ctl.reload(quiet=True)
            if viewer is not None and viewer.winfo_exists():
                viewer.refresh_image()
        except tk.TclError:
            logger.debug("Обновление после правки не удалось", exc_info=True)

    def _compare(self, left: str, right: str) -> None:
        def ok(data):
            CompareDialog(self, data, _load_photo, self._resolve, lambda p: play_video(p) or notify.error(T.TK_ONLY["open_failed"]),
                          lambda vid, number: self.ctl.restore(vid, number))

        self.ctl.compare(left, right, ok, lambda text: notify.error(text))

    def _restore(self, version_id: str, number) -> None:
        self.ctl.restore(version_id, number)

    def shutdown(self) -> None:
        self.edit_ctl.close()
        self.edit_runner.close()
        self.ctl.shutdown()
        self.list_view.loader.close()
        self.card_view.loader.close()
