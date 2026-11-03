"""Раздел «Обзор проекта»: прогон, следующее действие, этапы, счётчики, фильм, шаги, последний прогон."""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from StoryBookManager.gui.sections import overview_logic as O
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.external import open_external, play_video
from StoryBookManager.gui.shell.widgets import WrapRow
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

logger = logging.getLogger(__name__)

SEVERITY_TONES = {"error": "error", "warning": "warning", "success": "success"}


class OverviewScreen(ttk.Frame, SectionAdapter):
    def __init__(self, parent, *, goto: Callable[[str], None], get_project_dir: Callable[[], Optional[str]],
                 refresh: Callable[[], None], retry: Optional[Callable[[], None]] = None,
                 goto_filter: Optional[Callable[[str, str], None]] = None):
        super().__init__(parent, style="Panel.TFrame")
        self._goto = goto
        self._goto_filter = goto_filter   # (раздел, фильтр): счётчик открывает «Кадры» уже с фильтром
        self._get_dir = get_project_dir
        self._refresh = refresh
        self._retry = retry or refresh
        self._view: Optional[O.OverviewView] = None
        self._wrap: List[ttk.Label] = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        # прокручиваемое поле: на 1024x680 содержимое не обрезается
        self.canvas = tk.Canvas(self, highlightthickness=0, background=COLORS["window_bg"])
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.content.columnconfigure(0, weight=1)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(sequence, self._on_wheel, add="+")
        self.update_view(O.no_project_view())

    # --- Протокол раздела ---------------------------------------------------------------
    def on_refresh(self) -> None:
        self._refresh()

    def on_project_changed(self, project) -> None:
        self.update_view(O.loading_view() if project is not None else O.no_project_view())

    def update_view(self, view: O.OverviewView) -> None:
        if view == self._view:
            return
        keep_scroll = self._view is not None and self._view.mode == view.mode == "ready"
        top = self.canvas.yview()[0] if keep_scroll else 0.0
        self._view = view
        for child in self.content.winfo_children():
            child.destroy()
        self._wrap = []
        {"no_project": self._build_no_project, "loading": self._build_loading,
         "error": self._build_error}.get(view.mode, self._build_ready)(view)
        self.after_idle(self._fit_wrap)
        if keep_scroll:
            self.after_idle(lambda: self.canvas.yview_moveto(top))

    # --- Прокрутка и перенос строк ----------------------------------------------------------------
    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._fit_wrap()

    def _fit_wrap(self) -> None:
        width = max(self.canvas.winfo_width() - px(PAD_PANEL) * 2 - px(24), px(200))
        for label in self._wrap:
            try:
                label.configure(wraplength=width)
            except tk.TclError:
                pass

    def _on_wheel(self, event) -> None:
        try:
            if not self.winfo_viewable() or not str(event.widget).startswith(str(self)):
                return
        except tk.TclError:
            return
        if self.content.winfo_reqheight() <= self.canvas.winfo_height():
            return
        delta = -1 if (getattr(event, "num", 0) == 4 or event.delta > 0) else 1
        self.canvas.yview_scroll(delta * 3, "units")

    # --- Помощники построения ----------------------------------------------------------------------
    def _label(self, parent, text: str, *, style: str = "TLabel", tone: str = "", wrap: bool = True) -> ttk.Label:
        label = ttk.Label(parent, text=text, style=style, justify="left")
        if tone:
            label.configure(foreground=COLORS.get(tone, COLORS["text_primary"]))
        if wrap:
            self._wrap.append(label)
        return label

    def _section(self, row: int, title: str) -> ttk.Frame:
        box = ttk.LabelFrame(self.content, text=title, padding=px(PAD_BLOCK))
        box.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        box.columnconfigure(0, weight=1)
        return box

    def _header(self, title: str) -> None:
        head = ttk.Frame(self.content, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=O.TXT_TITLE, style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        if title:
            ttk.Label(head, text=title, style="Subtitle.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Button(head, text=O.TXT_REFRESH, command=self._refresh).grid(row=0, column=1, rowspan=2, sticky="e")

    def _centered(self, text: str, button: Optional[tuple] = None) -> None:
        ttk.Label(self.content, text=O.TXT_TITLE, style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self._label(self.content, text, style="Caption.TLabel").grid(row=1, column=0, sticky="w",
                                                                    pady=(px(PAD_BLOCK), 0))
        if button:
            ttk.Button(self.content, text=button[0], command=button[1]).grid(
                row=2, column=0, sticky="w", pady=(px(PAD_BLOCK), 0))

    # --- Состояния ------------------------------------------------------------------------------------
    def _build_no_project(self, view) -> None:
        self._centered(O.TXT_NO_PROJECT, (O.TXT_TO_PROJECTS, lambda: self._goto("projects")))

    def _build_loading(self, view) -> None:
        self._centered(O.TXT_LOADING)

    def _build_error(self, view) -> None:
        self._header("")
        self._banner(view, 1)
        if view.gone:
            ttk.Button(self.content, text=O.TXT_TO_PROJECTS, command=lambda: self._goto("projects")).grid(
                row=2, column=0, sticky="w", pady=(px(PAD_BLOCK), 0))

    def _banner(self, view, row: int) -> None:
        box = ttk.Frame(self.content, style="Panel.TFrame")
        box.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        box.columnconfigure(0, weight=1)
        self._label(box, view.error, tone="error").grid(row=0, column=0, sticky="w")
        line = ttk.Frame(box, style="Panel.TFrame")
        line.grid(row=1, column=0, sticky="w")
        if not view.gone:
            ttk.Button(line, text=O.TXT_RETRY, command=self._retry).pack(side="left")
        if view.stale:
            ttk.Label(line, text=O.TXT_STALE, style="Caption.TLabel").pack(side="left", padx=(px(PAD_BLOCK), 0))

    # --- Готовый обзор ------------------------------------------------------------------------------------
    def _build_ready(self, view) -> None:
        self._header(view.title)
        row = 1
        if view.error:
            self._banner(view, row)
            row += 1
        if view.run_text:
            self._label(self.content, view.run_text, tone=view.run_tone).grid(
                row=row, column=0, sticky="w", pady=(px(PAD_BLOCK), 0))
            row += 1
        if view.next_action:
            self._next_action(view, row)
            row += 1
        if view.empty:
            self._empty(row)
            row += 1
        self._stages(view, row)
        self._counters(view, row + 1)
        self._final(view, row + 2)
        self._steps(view, row + 3)
        self._last_run(view, row + 4)

    def _next_action(self, view, row: int) -> None:
        action = view.next_action
        box = self._section(row, O.TXT_NEXT)
        tone = SEVERITY_TONES.get(action.severity, "text_primary")
        self._label(box, action.title, style="Subtitle.TLabel", tone=tone).grid(row=0, column=0, sticky="w")
        if action.detail:
            self._label(box, action.detail, style="Caption.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Button(box, text=action.button, style="Accent.TButton",
                   command=lambda: self._goto(action.screen)).grid(row=2, column=0, sticky="w",
                                                                  pady=(px(PAD_INLINE), 0))

    def _empty(self, row: int) -> None:
        box = self._section(row, "")
        self._label(box, O.TXT_EMPTY).grid(row=0, column=0, sticky="w")
        ttk.Button(box, text=O.TXT_GO_GENERATION, style="Accent.TButton",
                   command=lambda: self._goto("generation")).grid(row=1, column=0, sticky="w",
                                                                 pady=(px(PAD_INLINE), 0))

    def _stages(self, view, row: int) -> None:
        box = self._section(row, O.TXT_STAGES)
        strip = WrapRow(box, pad_x=px(PAD_INLINE), pad_y=px(PAD_INLINE))
        strip.grid(row=0, column=0, sticky="ew")
        for cell in view.stages:
            frame = ttk.Frame(strip, relief="solid", borderwidth=1, padding=px(6))
            text = cell.title + (" (сейчас)" if cell.current else "")
            ttk.Label(frame, text=text, font="SBM.Body").pack(anchor="w")
            ttk.Label(frame, text=cell.state_title, foreground=COLORS.get(cell.tone, COLORS["text_primary"])
                      ).pack(anchor="w")
            if cell.progress:
                ttk.Label(frame, text=cell.progress, style="Caption.TLabel").pack(anchor="w")
            strip.add(frame)

    def _counters(self, view, row: int) -> None:
        if not view.counters:
            return
        strip = WrapRow(self.content, pad_x=px(PAD_INLINE), pad_y=px(PAD_INLINE))
        strip.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        for counter in view.counters:
            frame = ttk.Frame(strip, relief="solid", borderwidth=1, padding=px(6))
            ttk.Label(frame, text=counter.label, style="Caption.TLabel").pack(anchor="w")
            ttk.Label(frame, text=counter.value, style="Subtitle.TLabel").pack(anchor="w")
            if counter.sub:
                ttk.Label(frame, text=counter.sub, style="Caption.TLabel").pack(anchor="w")
            if counter.note:
                ttk.Label(frame, text=counter.note, style="Caption.TLabel").pack(anchor="w")
            if counter.target:
                ttk.Button(frame, text=O.TXT_GO, command=lambda c=counter: self._open_counter(c)).pack(
                    anchor="w", pady=(px(2), 0))
            strip.add(frame)

    def _open_counter(self, counter) -> None:
        if self._goto_filter is not None and counter.target == "shots":
            self._goto_filter(counter.target, counter.key if counter.key != "total" else "all")
        else:
            self._goto(counter.target)

    def _final(self, view, row: int) -> None:
        box = self._section(row, O.TXT_FINAL)
        final = view.final
        if not final.exists:
            self._label(box, O.TXT_NO_FINAL, style="Caption.TLabel").grid(row=0, column=0, sticky="w")
            return
        if final.meta:
            self._label(box, final.meta).grid(row=0, column=0, sticky="w")
        if final.warning:
            self._label(box, final.warning, tone="warning").grid(row=1, column=0, sticky="w")
        path = O.final_video_path(self._get_dir(), final)
        buttons = WrapRow(box)
        buttons.grid(row=2, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        buttons.add(ttk.Button(buttons, text=O.TXT_WATCH, style="Accent.TButton",
                               command=lambda: self._watch(path)))
        buttons.add(ttk.Button(buttons, text=O.TXT_SHOW_FOLDER, command=lambda: self._folder(path)))

    def _watch(self, path) -> None:
        if path is None or not play_video(path):
            notify.error("Не удалось открыть видео. Проверьте, что файл на месте.")

    def _folder(self, path) -> None:
        if path is not None and not open_external(str(path.parent)):
            notify.error("Не удалось открыть папку.")

    def _steps(self, view, row: int) -> None:
        box = self._section(row, view.steps_title)
        if not view.steps:
            self._label(box, O.DASH, style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        for i, step in enumerate(view.steps):
            line = " · ".join(p for p in (step.title, step.state_title, step.duration, step.note) if p)
            self._label(box, line, tone=step.tone if step.tone in COLORS else "").grid(row=i, column=0, sticky="w")
        if view.warnings_text:
            self._label(box, view.warnings_text, style="Caption.TLabel").grid(
                row=len(view.steps) + 1, column=0, sticky="w", pady=(px(PAD_INLINE), 0))

    def _last_run(self, view, row: int) -> None:
        box = self._section(row, O.TXT_LAST_RUN)
        if not view.last_run:
            self._label(box, O.TXT_NO_RUNS, style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        for i, (name, value) in enumerate(view.last_run):
            self._label(box, f"{name}: {value}").grid(row=i, column=0, sticky="w")
        extra = len(view.last_run)
        for text, tone in ((view.last_run_error, "error"), (view.last_run_note, "")):
            if text:
                self._label(box, text, tone=tone).grid(row=extra, column=0, sticky="w")
                extra += 1
