"""Экран «Генерация»: настройки, сводка, ход прогона и журнал; запуск и управление прогоном."""

import logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from StoryBookManager.gui.generation import model, steps_model
from StoryBookManager.gui.generation import texts as T
from StoryBookManager.gui.generation.history_view import HistoryView
from StoryBookManager.gui.generation.log_view import LogView
from StoryBookManager.gui.generation.run_controller import GenerationController
from StoryBookManager.gui.generation.run_view import RunView
from StoryBookManager.gui.generation.settings_view import SettingsView
from StoryBookManager.gui.generation.steps_view import StepsView
from StoryBookManager.gui.generation.summary_view import SummaryView
from StoryBookManager.gui.shell import notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shell.external import open_external
from StoryBookManager.gui.shell.shortcuts import expand_sequences
from StoryBookManager.gui.shell.widgets import PanelHeader
from StoryBookManager.gui.shots.dialogs import ChoiceDialog
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

logger = logging.getLogger(__name__)

NARROW_WIDTH = 1100
WIDE_WIDTH = 1600
LEFT_WIDTH = 440
TABS = (("settings", "tab_settings"), ("summary", "tab_summary"), ("progress", "tab_progress"), ("log", "tab_log"))
BANNER_COLORS = {"info": "info", "warn": "warning", "error": "error", "ok": "success"}


class GenerationScreen(ttk.Frame):
    def __init__(self, parent, *, bridge, get_project_id: Callable[[], Optional[str]],
                 get_project_dir: Optional[Callable[[], Optional[str]]] = None,
                 on_run_changed: Optional[Callable[[], None]] = None):
        super().__init__(parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        self._get_project_dir = get_project_dir or (lambda: None)
        self.ctl = GenerationController(bridge, self, notify, lambda ms, fn: self.after(ms, fn), self.after_cancel,
                                        get_project_id, on_run_changed=on_run_changed)
        self._mode = "narrow"
        self._tab = "settings"
        PanelHeader(self, T.fmt("title"), T.TK_TEXTS["tk_subtitle"]).grid(row=0, column=0, sticky="ew")
        # баннер состояния прогона
        self.banner = ttk.Frame(self, style="Panel.TFrame")
        self.banner.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.banner_label = ttk.Label(self.banner, text="", wraplength=px(700), justify="left")
        self.banner_label.pack(side="left")
        self.banner_btn = ttk.Button(self.banner, text="", command=self.ctl.banner_action)
        self.banner_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
        self.banner.grid_remove()
        # баннер «платные задания ещё идут» (перегенерация)
        self.pending = ttk.Frame(self, style="Panel.TFrame")
        self.pending.grid(row=2, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.pending_label = ttk.Label(self.pending, text="", wraplength=px(700), justify="left", foreground=COLORS["warning"])
        self.pending_label.pack(side="left")
        ttk.Button(self.pending, text=T.fmt("inflight_wait"), command=self.ctl.wait_for_pending).pack(side="left", padx=(px(PAD_INLINE), 0))
        ttk.Button(self.pending, text=T.fmt("inflight_abandon"), command=self.ctl.abandon_and_regenerate).pack(
            side="left", padx=(px(PAD_INLINE), 0))
        self.pending.grid_remove()
        # загрузка и ошибка
        self.state = ttk.Frame(self, style="Panel.TFrame")
        self.state.grid(row=3, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.state_label = ttk.Label(self.state, text="", wraplength=px(700), justify="left")
        self.state_label.pack(side="left")
        self.retry_btn = ttk.Button(self.state, text=T.fmt("retry"), command=self.ctl.on_refresh)
        self.retry_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
        self.state.grid_remove()
        # переключатель частей на узком окне
        self.tabbar = ttk.Frame(self, style="Panel.TFrame")
        self.tabbar.grid(row=4, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.tab_var = tk.StringVar(value="settings")
        for name, text_id in TABS:
            ttk.Radiobutton(self.tabbar, text=T.fmt(text_id), value=name, variable=self.tab_var, style="Toolbutton",
                            command=lambda n=name: self.show_tab(n)).pack(side="left", padx=(0, px(4)))
        self.tabbar.grid_remove()
        # тело: слева настройки и сводка (прокрутка), справа прогон, шаги и журнал
        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=5, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.body.rowconfigure(0, weight=1)
        self.left = ttk.Frame(self.body, style="Panel.TFrame")
        self.left.rowconfigure(0, weight=1)
        self.left.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.left, highlightthickness=0, background=COLORS["window_bg"])
        scroll = ttk.Scrollbar(self.left, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.left_inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self.left_inner.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window(0, 0, window=self.left_inner, anchor="nw")
        self.left_inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.settings = SettingsView(self.left_inner, self.ctl)
        self.summary = SummaryView(self.left_inner, self.ctl)
        self.right = ttk.Frame(self.body, style="Panel.TFrame")
        self.run_view = RunView(self.right, on_pause=self.ctl.toggle_pause, on_stop=self.ctl.stop)
        self.steps_box = ttk.Frame(self.right, style="Panel.TFrame")
        self.steps_box.columnconfigure(0, weight=1)
        self.steps_box.rowconfigure(0, weight=1)
        self.steps_view = StepsView(self.steps_box, on_pick=self.ctl.pick_step)
        self.steps_view.grid(row=0, column=0, sticky="nsew")
        self.history_view = HistoryView(self.steps_box, on_open_log=self.ctl.open_run_log)
        self.history_view.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.log_view = LogView(self.right, on_clear=self.ctl.clear_log, on_copy=self._copy, on_open_full=self.open_full_log,
                                on_save_error=lambda text: notify.error(text),
                                on_back=self.ctl.back_to_current_log)
        self._bind_wheel(self.left_inner)
        self._bind_wheel(self.canvas)
        self._apply_layout()
        # нижняя полоса запуска
        self.strip = ttk.Frame(self, style="Panel.TFrame")
        self.strip.grid(row=6, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.strip.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(self.strip, text=T.fmt("go"), style="Accent.TButton", command=self.request_run)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.reason = ttk.Label(self.strip, text="", style="Caption.TLabel", wraplength=px(420), justify="left")
        self.reason.grid(row=0, column=1, sticky="w", padx=(px(PAD_INLINE), 0))
        self.warn = ttk.Label(self.strip, text="", foreground=COLORS["warning"], wraplength=px(900), justify="left")
        self.warn.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(self.strip, text=T.TK_TEXTS["tk_shortcuts"], style="Caption.TLabel").grid(row=0, column=2, sticky="e")
        self.bind("<Configure>", self._on_resize)
        self._bind_keys()
        self.render("all")

    # ------------------------------------------------------------------ интерфейс для контроллера

    def render(self, *what) -> None:
        parts = set(what or ("all",))
        every = "all" in parts
        ctl = self.ctl
        if every or parts & {"all", "banner"}:
            self._show_banners()
        if every or parts & {"mode", "settings"}:
            self.settings.show()
        if every or parts & {"summary", "settings", "mode"}:
            self.summary.show()
        if every or "run" in parts:
            self.run_view.show(ctl.status, ctl.titles, ctl.unavailable, ctl.pause_busy, ctl.stop_busy)
        if every or "steps" in parts:
            rows = (steps_model.step_rows((ctl.status or {}).get("steps"), ctl.titles)
                    if (ctl.status or {}).get("steps") else steps_model.catalog_rows(ctl.steps_catalog))
            self.steps_view.show(rows)
            self.steps_view.set_enabled(not model.is_busy_state(ctl.run_state))
        if every or "log" in parts:
            self.log_view.show(ctl.log)
            self.log_view.set_back_visible(bool(ctl.viewing_run))
        if every or "history" in parts:
            self.history_view.show(ctl.history)
        if every or parts & {"button", "mode", "summary"}:
            self._show_button()

    def _show_banners(self) -> None:
        ctl = self.ctl
        if ctl.load_state == "error":
            self.state_label.configure(text=T.fmt("load_error", reason=ctl.load_error), foreground=COLORS["error"])
            self.retry_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
            self.state.grid()
        elif ctl.load_state == "loading":
            self.state_label.configure(text=T.fmt("loading"), foreground=COLORS["text_muted"])
            self.retry_btn.pack_forget()
            self.state.grid()
        else:
            self.state.grid_remove()
        banner = ctl.banner()
        if banner:
            color = COLORS[BANNER_COLORS.get(banner["tone"], "info")]
            self.banner_label.configure(text=banner["text"], foreground=color)
            labels = {"resume": T.fmt("resume"), "restart": T.fmt("confirm_restart_ok"),
                      "mark_interrupted": T.fmt("mark_interrupted"), "refresh": T.TK_TEXTS["tk_retry_plan"]}
            if banner.get("action"):
                self.banner_btn.configure(text=labels.get(banner["action"], ""))
                self.banner_btn.pack(side="left", padx=(px(PAD_INLINE), 0))
            else:
                self.banner_btn.pack_forget()
            self.banner.grid()
        else:
            self.banner.grid_remove()
        if ctl.paid_pending:
            self.pending_label.configure(text=T.fmt("paid_pending", keys=", ".join(ctl.paid_pending)))
            self.pending.grid()
        else:
            self.pending.grid_remove()

    def _show_button(self) -> None:
        ctl = self.ctl
        button = ctl.start_button()
        self.start_btn.configure(text=button.label, state="normal" if button.enabled else "disabled")
        self.reason.configure(text=button.reason or "")
        warning = model.inflight_warning(ctl.plan) or ""
        self.warn.configure(text=warning)
        if warning:
            self.warn.grid()
        else:
            self.warn.grid_remove()

    def ask_confirm(self, spec) -> bool:
        return bool(ConfirmDialog.ask(self, spec))

    def ask_choice(self, title, body, buttons, default="") -> Optional[str]:
        return ChoiceDialog.ask(self, title, body, buttons, default)

    def show_tab(self, name: str) -> None:
        """На узком окне показывает одну часть; на широком все части видны и вкладка не нужна."""
        self._tab = name
        self.tab_var.set(name)
        self._apply_layout()
        if name == "log" and self.log_wanted():
            self.ctl.poll_log()

    def focus_budget(self) -> None:
        self.settings.budget_entry.focus_set()

    def log_wanted(self) -> bool:
        return self.log_view.winfo_manager() != ""

    # ------------------------------------------------------------------ раскладка

    def _on_resize(self, event) -> None:
        if event.widget is not self:
            return
        width = self.winfo_toplevel().winfo_width()
        mode = "narrow" if width < NARROW_WIDTH else ("two" if width < WIDE_WIDTH else "wide")
        if mode != self._mode:
            self._apply_layout(mode)

    def _apply_layout(self, mode: Optional[str] = None) -> None:
        """narrow - вкладки; two - слева настройки и сводка, справа прогон, шаги, журнал друг под другом;
        wide - то же, но шаги и журнал в две колонки."""
        mode = mode or self._mode
        self._mode = mode
        narrow = mode == "narrow"
        if narrow:
            self.tabbar.grid()
        else:
            self.tabbar.grid_remove()
        for w in (self.left, self.right, self.settings, self.summary, self.run_view, self.steps_box, self.log_view):
            w.grid_forget()
        for c in range(3):
            self.body.columnconfigure(c, weight=0, minsize=0)
            self.right.columnconfigure(c, weight=0)
        for r in range(4):
            self.right.rowconfigure(r, weight=0)
        tab = self._tab
        if narrow:
            self.body.columnconfigure(0, weight=1)
            show_left = tab in ("settings", "summary")
            (self.left if show_left else self.right).grid(row=0, column=0, sticky="nsew")
            if show_left:
                (self.settings if tab == "settings" else self.summary).grid(row=0, column=0, sticky="ew")
            elif tab == "progress":
                self._grid_right(wide=False, parts=("run", "steps"))
            else:
                self._grid_right(wide=False, parts=("log",))
        else:
            self.body.columnconfigure(0, minsize=px(LEFT_WIDTH))
            self.body.columnconfigure(1, weight=1)
            self.left.grid(row=0, column=0, sticky="nsew", padx=(0, px(PAD_INLINE)))
            self.right.grid(row=0, column=1, sticky="nsew")
            self.settings.grid(row=0, column=0, sticky="ew")
            self.summary.grid(row=1, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
            self._grid_right(wide=mode == "wide", parts=("run", "steps", "log"))
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _grid_right(self, wide: bool, parts) -> None:
        r = self.right
        r.columnconfigure(0, weight=1)
        if "run" in parts:
            self.run_view.grid(row=0, column=0, columnspan=2, sticky="ew")
        if wide:
            r.columnconfigure(1, weight=1, uniform="half")
            r.columnconfigure(0, uniform="half")
            r.rowconfigure(1, weight=1)
            self.steps_box.grid(row=1, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0), padx=(0, px(PAD_INLINE)))
            self.log_view.grid(row=1, column=1, sticky="nsew", pady=(px(PAD_INLINE), 0))
            return
        row = 1
        if "steps" in parts:
            r.rowconfigure(row, weight=1)
            self.steps_box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(px(PAD_INLINE), 0))
            row += 1
        if "log" in parts:
            r.rowconfigure(row, weight=1)
            self.log_view.grid(row=row if "run" in parts else 0, column=0, columnspan=2, sticky="nsew",
                               pady=(px(PAD_INLINE), 0) if "run" in parts else 0)

    def _bind_wheel(self, widget) -> None:
        """Колесо привязываем к своим виджетам (текстовые поля и таблицы крутят себя сами)."""
        if isinstance(widget, (tk.Text, ttk.Treeview)):
            return
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event) -> None:
        if self.left_inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        delta = -1 if (getattr(event, "num", 0) == 4 or event.delta > 0) else 1
        self.canvas.yview_scroll(delta * 3, "units")

    # ------------------------------------------------------------------ действия

    def request_run(self, mode: Optional[str] = None) -> None:
        self.ctl.request_run(mode)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        notify.info(T.fmt("log_copied"))

    def open_full_log(self) -> None:
        """Полный журнал прогона открывается файлом; нет файла или программы - окно с хвостом."""
        run_id = self.ctl.viewing_run or self.ctl.run_id
        root = self._get_project_dir()
        if run_id and root:
            path = Path(root) / ".studio" / "logs" / f"{run_id}.log"
            if not path.is_file():
                notify.warning(T.TK_TEXTS["tk_log_missing"])
            elif open_external(path):
                return
            else:
                notify.warning(T.TK_TEXTS["tk_open_log_failed"])
        self._tail_window()

    def _tail_window(self) -> None:
        win = tk.Toplevel(self)
        win.title(T.fmt("section_log"))
        win.geometry("900x520")
        text = tk.Text(win, wrap="word", font="SBM.Mono", background=COLORS["card"], foreground=COLORS["text_primary"])
        scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", "\n".join(self.ctl.log.lines) or T.fmt("log_empty"))
        text.configure(state="disabled")
        text.bind("<Escape>", lambda e: win.destroy())

    def _bind_keys(self) -> None:
        def guard(action, running_only=False):
            def run(_e=None):
                if self.winfo_ismapped() and (not running_only or model.is_busy_state(self.ctl.run_state)):
                    action()
                    return "break"
            return run
        root = self.winfo_toplevel()
        for key, action, ctrl, only_running in (("Return", self.request_run, True, False), ("period", self.ctl.stop, True, True),
                                  ("p", self.ctl.toggle_pause, True, True),
                                  ("l", lambda: self.show_tab("log"), True, False)):
            for seq in expand_sequences(key, ctrl=ctrl):
                try:
                    root.bind(seq, guard(action, only_running), add="+")
                except tk.TclError:
                    pass

    def shutdown(self) -> None:
        self.ctl.shutdown()


class GenerationAdapter(GenerationScreen, SectionAdapter):
    def on_show(self) -> None:
        self.ctl.on_show()
        if self._mode == "narrow" and model.is_busy_state(self.ctl.run_state):
            self.show_tab("progress")  # идёт прогон: сразу показываем «Ход»

    def on_refresh(self) -> None:
        self.ctl.on_refresh()

    def on_save(self) -> None:
        self.ctl.save_settings()

    def on_project_changed(self, project) -> None:
        self.ctl.set_project(getattr(project, "project_id", None) if project is not None else None)

    def apply_intent(self, mode=None, step_id=None, shot_keys=None) -> None:
        self.ctl.apply_intent(mode, step_id, shot_keys)

    def can_leave(self) -> bool:
        if not self.ctl.settings_dirty():
            return True
        choice = ChoiceDialog.ask(self, T.TK_TEXTS["tk_leave_title"], T.TK_TEXTS["tk_leave_body"],
                                  [("save", T.fmt("save")), ("discard", T.TK_TEXTS["tk_leave_discard"]),
                                   ("stay", T.TK_TEXTS["tk_cancel"])], "stay")
        if choice == "discard":
            return True
        if choice == "save":
            self.ctl.save_settings()
        return False
