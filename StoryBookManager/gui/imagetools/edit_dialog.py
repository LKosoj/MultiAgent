"""Окно «Правка картинки»: что изменить, образцы, цена, запуск.

Кнопка запуска включается, когда есть промпт от 5 символов, не больше 3 образцов и готов предпросмотр цены.
Цены в конфиге может не быть: тогда кнопка «Править без оценки», и деньги всё равно спишутся.
"""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from StoryBookManager.gui.imagetools import edit_model as EM
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT
from StoryBookManager.gui.imagetools.texts import fmt
from StoryBookManager.gui.imagetools.zoom_canvas import open_image
from StoryBookManager.gui.shell.dialogs import _center_on
from StoryBookManager.gui.shots.texts import format_rub_amount
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, px

logger = logging.getLogger(__name__)

THUMB = (240, 150)


class ImageEditDialog(tk.Toplevel):
    """target: {path, name, shot_key, part} (shot_key пустой у иллюстрации); abs_path - файл для миниатюры."""

    def __init__(self, parent, controller, target: dict, abs_path: Optional[str],
                 on_done: Callable[[dict], None], on_status: Callable[[str], None]):
        super().__init__(parent)
        self.ctl, self.target = controller, target
        self._done_cb, self._status_cb = on_done, on_status
        self._notice = ""
        self.preview: Optional[dict] = None
        self.refs_choices: List[dict] = []
        self._selected: List[int] = []
        self.closed = False
        self._starting = False   # запрос запуска ушёл, ответа ещё нет: закрыть окно нельзя
        # проект запоминаем при открытии: пока диалог открыт, пользователь мог переключить проект
        self.project_id = getattr(controller, "get_project_id", lambda: None)()
        self.title(fmt("edit_title", name=target["name"]))
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.configure(background=COLORS["window_bg"])
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        wrap = px(460)
        ttk.Label(box, text=self.title(), style="Subtitle.TLabel", wraplength=wrap).pack(anchor="w")
        self._photo = None
        if abs_path:
            self._build_thumb(box, abs_path)
        ttk.Label(box, text=IT["edit_backup_note"], style="Caption.TLabel", wraplength=wrap).pack(anchor="w")
        ttk.Label(box, text=IT["edit_prompt_label"]).pack(anchor="w", pady=(px(PAD_INLINE), 0))
        self.prompt = tk.Text(box, height=4, width=56, wrap="word", background=COLORS["card"],
                              foreground=COLORS["text_primary"], font="SBM.Body", highlightthickness=1,
                              highlightbackground=COLORS["border_strong"], highlightcolor=COLORS["running"], undo=True)
        self.prompt.pack(fill="x")
        self.prompt.bind("<<Modified>>", self._on_prompt)
        ttk.Label(box, text=IT["edit_prompt_hint"], style="Caption.TLabel").pack(anchor="w")
        ttk.Label(box, text=IT["edit_refs_label"]).pack(anchor="w", pady=(px(PAD_INLINE), 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.refs = tk.Listbox(row, height=5, selectmode="multiple", exportselection=False, activestyle="none")
        self.refs.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(row, command=self.refs.yview)
        scroll.pack(side="left", fill="y")
        self.refs.configure(yscrollcommand=scroll.set)
        self.refs.bind("<<ListboxSelect>>", self._on_refs)
        self.refs_info = ttk.Label(box, text=IT["edit_loading"], style="Caption.TLabel", wraplength=wrap)
        self.refs_info.pack(anchor="w")
        self.cost = ttk.Label(box, text=IT["edit_loading"], wraplength=wrap, justify="left")
        self.cost.pack(anchor="w", pady=(px(PAD_INLINE), 0))
        self.problems = ttk.Label(box, text="", wraplength=wrap, justify="left", foreground=COLORS["error"])
        self.problems.pack(anchor="w")
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(px(PAD_BLOCK), 0))
        self.cancel_btn = ttk.Button(buttons, text=IT["edit_cancel"], command=self.close)
        self.cancel_btn.pack(side="left")
        self.go_btn = ttk.Button(buttons, text=IT["edit_go_free"], style="Accent.TButton", command=self._go,
                                 state="disabled")
        self.go_btn.pack(side="right")
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        _center_on(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.cancel_btn.focus_set()   # Enter по умолчанию отменяет, деньги подтверждает только явный клик
        self.ctl.load_references(target["path"], self._on_refs_loaded, self._on_refs_failed,
                                 project_id=self.project_id)
        self._load_preview()

    # --- построение -------------------------------------------------------------------
    def _build_thumb(self, box, abs_path: str) -> None:
        image = open_image(abs_path)
        if image is None:
            return
        try:
            from PIL import ImageTk

            image.thumbnail((px(THUMB[0]), px(THUMB[1])))
            self._photo = ImageTk.PhotoImage(image)
            ttk.Label(box, image=self._photo).pack(anchor="w", pady=(px(PAD_INLINE), 0))
        except Exception:
            logger.debug("Миниатюра правки не построилась", exc_info=True)

    def close(self) -> None:
        if self._starting:
            return   # пока идёт запуск, «Отмена», Esc и крестик не работают: закрытие только по ответу запуска
        self._close_now()

    def _close_now(self) -> None:
        self.closed = True
        self.destroy()

    # --- образцы ------------------------------------------------------------------------
    def _on_refs_loaded(self, choices: List[dict]) -> None:
        if self.closed:
            return
        self.refs_choices = choices
        for choice in choices:
            self.refs.insert("end", choice["label"])
        self._sync_refs_info()

    def _on_refs_failed(self, text: str) -> None:
        if not self.closed:
            self.refs_info.configure(text=text)

    def selected_paths(self) -> List[str]:
        return [self.refs_choices[i]["path"] for i in self._selected if i < len(self.refs_choices)]

    def _on_refs(self, _event=None) -> None:
        if self._starting:
            return
        now = list(self.refs.curselection())
        if len(now) > EM.REFERENCES_MAX:   # четвёртый образец не принимаем: возвращаем прежний выбор
            self.refs.selection_clear(0, "end")
            for i in self._selected:
                self.refs.selection_set(i)
            return
        if now == self._selected:
            return
        self._selected = now
        self._sync_refs_info()
        self._load_preview()

    def _sync_refs_info(self) -> None:
        if not self.refs_choices:
            self.refs_info.configure(text=IT["edit_refs_none"])
        else:
            self.refs_info.configure(text=fmt("edit_refs_count", n=len(self._selected)))

    # --- предпросмотр ----------------------------------------------------------------------
    def _load_preview(self) -> None:
        self.preview = None
        self.cost.configure(text=IT["edit_loading"])
        self.problems.configure(text=self._notice)
        self._sync()
        self.ctl.preview(self.target, self.selected_paths(), self._on_preview, self._on_preview_failed,
                         project_id=self.project_id)

    def _on_preview_failed(self, text: str) -> None:
        if not self.closed:
            self.cost.configure(text="")
            self.problems.configure(text=text)

    def _on_preview(self, preview: dict) -> None:
        if self.closed:
            return
        self.preview = preview
        estimate = preview.get("estimate") or {}
        button = EM.edit_button(estimate.get("cost_rub"), estimate.get("unknown_cost"))
        lines = []
        if estimate.get("unknown_cost"):
            lines.append(IT[button["note"]] if button["note"] else IT["edit_cost_unknown"])
        else:
            lines.append(fmt("edit_cost", cost=format_rub_amount(estimate.get("cost_rub"))))
        self.cost.configure(text="\n".join(lines))
        messages = [self._notice] if self._notice else []
        messages += [str(p.get("message") or "") for p in preview.get("problems") or []]
        self.problems.configure(text="\n".join(messages))
        self._sync()

    # --- запуск ---------------------------------------------------------------------------------
    def _on_prompt(self, _event=None) -> None:
        if self.prompt.edit_modified():
            self.prompt.edit_modified(False)
            self._sync()

    def can_submit(self) -> bool:
        if self._starting:
            return False
        ready = self.preview is not None and bool(self.preview.get("can_start"))
        return EM.edit_can_submit(self.prompt.get("1.0", "end-1c"), len(self._selected), ready)

    def _sync(self) -> None:
        estimate = (self.preview or {}).get("estimate") or {}
        label = EM.edit_button(estimate.get("cost_rub"), estimate.get("unknown_cost"))["label"]
        self.go_btn.configure(text=label, state="normal" if self.can_submit() else "disabled")

    def _go(self) -> None:
        if not self.can_submit():
            return
        self._starting = True
        self.go_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self._set_inputs(False)
        self.ctl.start(self.target, self.prompt.get("1.0", "end-1c"), self.selected_paths(), self.preview,
                       on_started=self._close_now, on_fail=self._on_start_failed, on_done=self._done_cb,
                       on_status=self._status_cb, project_id=self.project_id)

    def _set_inputs(self, enabled: bool) -> None:
        """На время запуска промпт и список образцов заблокированы."""
        self.prompt.configure(state="normal" if enabled else "disabled")
        self.refs.configure(state="normal" if enabled else "disabled")

    def _on_start_failed(self, info: dict) -> None:
        self._starting = False
        if self.closed:
            return
        self.cancel_btn.configure(state="normal")
        self._set_inputs(True)
        self._notice = info["text"]
        if info["kind"] == "refresh":
            self._load_preview()   # цена или картинка изменились: берём свежий план
        else:
            self.problems.configure(text=self._notice)
            self._sync()
