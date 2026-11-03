"""Экран «Болванка»: дерево шотов, сравнение картинок, действия, длительность, превью."""

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from StoryBookManager.gui.screens import blockout_local
from StoryBookManager.gui.screens import blockout_model as bm
from StoryBookManager.gui.screens.blockout_compare import ComparePanel
from StoryBookManager.gui.screens.blockout_flow import BlockoutFlow
from StoryBookManager.gui.screens.blockout_texts import BLOCKOUT_TEXTS as T
from StoryBookManager.gui.screens.blockout_tree import BlockoutTree
from StoryBookManager.gui.screens.keys import KeyBindings, key_is_for_screen
from StoryBookManager.gui.screens.scroll_frame import ScrollFrame
from StoryBookManager.gui.screens.calls import (
    CAP_BLOCKOUT_DETAIL, CAP_BLOCKOUT_LEAVE_AS_IS, CAP_BLOCKOUT_SET_DURATION, CAP_BLOCKOUT_STATE, StudioCalls,
)
from StoryBookManager.gui.shell import external, notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shell.widgets import PanelHeader, Tooltip, WrapRow
from StoryBookManager.gui.shots import texts as shot_texts
from StoryBookManager.gui.shots.dialogs import ChoiceDialog
from StoryBookManager.gui.shots.model import error_parts, is_run_busy, result_error_text
from StoryBookManager.gui.shots.thumb_loader import ThumbLoader
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px


class BlockoutScreen(ttk.Frame, SectionAdapter):
    def __init__(self, parent, bridge, get_project_id, get_project_dir=None, get_run_view=None,
                 on_goto_generation=None, on_open_shot=None, on_run_started=None):
        ttk.Frame.__init__(self, parent, style="Panel.TFrame", padding=px(PAD_PANEL))
        self.calls = StudioCalls(bridge)
        self.get_project_id = get_project_id
        self.get_project_dir = get_project_dir or (lambda: None)
        self.get_run_view = get_run_view or (lambda: None)
        self.on_goto_generation = on_goto_generation
        self.on_open_shot = on_open_shot
        self.on_run_started = on_run_started
        self.flow = BlockoutFlow(self.calls, get_project_id, launcher="local")
        self.ask_confirm = lambda spec: ConfirmDialog.ask(self.winfo_toplevel(), spec)
        self.ask_choice = lambda title, body, buttons, default="": ChoiceDialog.ask(self, title, body, buttons, default)
        self.state = None
        self.error = None
        self.detail = None
        self.forbidden = False
        self.busy = False
        self._duration_chosen = None
        self._hints = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        PanelHeader(self, T["title"]).grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar(value=T["loading"])
        self.status_label = ttk.Label(self, textvariable=self.status_var, wraplength=px(760), justify="left")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        self.goto_btn = ttk.Button(self, text=T["goto_generation"], command=lambda: self.on_goto_generation and self.on_goto_generation())
        self.retry_btn = ttk.Button(self, text=T["retry"], command=self.refresh)
        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=2, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.body.columnconfigure(1, weight=1)
        self.body.rowconfigure(0, weight=1)
        left = ttk.Frame(self.body, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="ns", padx=(0, px(PAD_PANEL)))
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text=T["tree_title"], style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")
        self.tree = BlockoutTree(left, self._on_select, on_enter=self.open_in_shots)
        self.tree.grid(row=1, column=0, sticky="ns")
        self.summary_var = tk.StringVar()
        ttk.Label(left, textvariable=self.summary_var, style="Caption.TLabel", wraplength=px(320), justify="left").grid(row=2, column=0, sticky="w")
        self.tools_var = tk.StringVar()
        ttk.Label(left, textvariable=self.tools_var, style="Caption.TLabel", wraplength=px(320), justify="left").grid(row=3, column=0, sticky="w")
        # правая колонка прокручивается: на малых окнах нижние плитки и замечания достижимы колесом мыши
        self.right_scroll = ScrollFrame(self.body)
        self.right_scroll.grid(row=0, column=1, sticky="nsew")
        right = self.right_scroll.inner
        right.columnconfigure(0, weight=1)
        self.compare = ComparePanel(right, lambda cb: ThumbLoader(self, self.get_project_id, cb), self._open_video)
        self.compare.grid(row=2, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.actions = WrapRow(right, pad_x=px(PAD_INLINE), pad_y=px(PAD_INLINE))
        self.actions.grid(row=0, column=0, sticky="ew")
        self.buttons = {}
        for action in bm.ACTION_ORDER:
            btn = ttk.Button(self.actions, text=bm.action_label(action), command=lambda a=action: self.run_action(a))
            self.buttons[action] = btn
            Tooltip(btn, lambda a=action: self._hints.get(a) or "")
        self.shots_btn = ttk.Button(self.actions, text=T["open_in_shots"], command=self.open_in_shots)
        self.blend_btn = ttk.Button(self.actions, text="Открыть .blend", command=self.open_blend)
        self.preview_btn = ttk.Button(self.actions, text="Открыть превью", command=self.open_preview)
        self.actions.set_items([*self.buttons.values(), self.shots_btn, self.blend_btn, self.preview_btn])
        # длительность
        dur = ttk.Frame(right, style="Panel.TFrame")
        dur.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        ttk.Label(dur, text=T["duration_label"]).pack(side="left")
        self.dur_var = tk.StringVar()
        self.dur_combo = ttk.Combobox(dur, textvariable=self.dur_var, state="readonly", width=6)
        self.dur_combo.pack(side="left", padx=px(PAD_INLINE))
        self.dur_combo.bind("<<ComboboxSelected>>", self._on_duration_pick)
        self.dur_btn = ttk.Button(dur, text=T["duration_apply"], command=self.apply_duration)
        self.dur_btn.pack(side="left")
        self.dur_note = tk.StringVar()
        ttk.Label(dur, textvariable=self.dur_note, style="Caption.TLabel").pack(side="left", padx=px(PAD_INLINE))
        # замечания и предупреждения
        self.notes = tk.Text(right, height=6, wrap="word", background=COLORS["card"], state="disabled")
        self.notes.grid(row=3, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.preview_var = tk.StringVar()
        ttk.Label(right, textvariable=self.preview_var, style="Caption.TLabel").grid(row=4, column=0, sticky="w")
        self.body.grid_remove()
        self.keys = KeyBindings(self)
        # Ctrl+R и в русской раскладке (там та же клавиша даёт букву «к»)
        self.keys.bind("<Control-Key>", self._on_ctrl_key)

    # --- загрузка ----------------------------------------------------------------
    def load_project(self, _project=None):
        self.state = None
        self.error = None
        self.detail = None
        self.refresh()

    def on_project_changed(self, project):
        pass  # проект загружает главное окно через load_project

    def on_show(self):
        if self.state is None:
            self.refresh()

    def on_refresh(self):
        self.refresh()

    def refresh(self):
        pid = self.get_project_id()
        if not pid:
            return
        self.error = None
        self.status_var.set(T["loading"] if self.state is None else self.status_var.get())
        self.calls.call(CAP_BLOCKOUT_STATE, {"project_id": pid}, on_success=self._loaded, on_error=self._load_failed, key="blockout_state")

    def on_status(self, _brief):
        """Сводка проекта обновилась: состояние прогона влияет на кнопки."""
        if self.state is not None:
            self._render_actions()

    def _load_failed(self, result):
        self.error = result_error_text(result)
        self.state = None
        self._render()

    def _loaded(self, state):
        self.state = state
        self.error = None
        self._render()

    def _screen(self):
        return bm.screen_state(self.state, bool(self.error), (self.state or {}).get("empty_reason"))

    def _render(self):
        screen = self._screen()
        self.goto_btn.grid_forget()
        self.retry_btn.grid_forget()
        self.status_label.grid()
        if screen == "loading":
            self.status_var.set(T["loading"])
            self.body.grid_remove()
        elif screen == "error":
            self.status_var.set(T["load_error"].format(reason=(self.error or "").rstrip(".")))
            self.retry_btn.grid(row=3, column=0, sticky="w")
            self.body.grid_remove()
        elif screen == "not_built":
            self.status_var.set(T["not_built"])
            self.goto_btn.grid(row=3, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
            self.body.grid_remove()
        elif screen == "no_shots":
            self.status_var.set(T["no_shots"])
            self.body.grid_remove()
        else:
            self.body.grid()
            warn = T["no_blender"] if not self.state["tools"]["blender"]["available"] else ""
            self.status_var.set(warn)
            if warn:
                self.status_label.grid()
            else:
                self.status_label.grid_remove()  # пустая строка не занимает место над деревом
            self.summary_var.set(bm.summary_line(self.state["summary"]))
            self.tools_var.set(bm.tools_line(self.state["tools"]))
            self.tree.fill(self.state)
            self._render_actions()
            self._render_preview()
            if self.tree.selection["focus"]:
                self._load_detail(self.tree.selection["focus"])
            else:
                self._show_detail(None)

    def _render_preview(self):
        pv = self.state.get("preview") or {}
        if pv.get("path"):
            kind = T["preview_burnin"] if pv.get("kind") == "burnin" else T["preview_plain"]
            self.preview_var.set(f"{T['preview_title']}: {pv['path']} ({kind})")
        else:
            self.preview_var.set(f"{T['preview_title']}: {T['preview_none']}")
        warnings = self.state.get("warnings") or []
        if warnings:
            self.preview_var.set(self.preview_var.get() + f" · {T['warnings_title']}: " + "; ".join(w["message"] for w in warnings))

    # --- выбор и детали -----------------------------------------------------------
    def _on_select(self, selection):
        self._render_actions()
        focus = selection.get("focus")
        if focus:
            self._load_detail(focus)
        else:
            self._show_detail(None)

    def _load_detail(self, key):
        self.calls.call(CAP_BLOCKOUT_DETAIL, {"project_id": self.get_project_id(), "shot_key": key},
                        on_success=lambda d, k=key: self.tree.selection["focus"] == k and self._show_detail(d),
                        on_error=lambda r: self._show_detail(None), key="blockout_detail")

    def _show_detail(self, detail):
        self.detail = detail
        self.compare.show(detail)
        self._render_notes()
        self._render_duration()

    def _render_notes(self):
        notes = (self.detail or {}).get("notes") or []
        lines = [f"{n.get('level') or ''} {n.get('code') or ''} {n.get('message') or ''}".strip() for n in notes] or [T["notes_none"]]
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", T["notes_title"] + "\n" + "\n".join(lines))
        self.notes.configure(state="disabled")

    def _render_duration(self):
        d = (self.detail or {}).get("duration")
        if not d:
            self.dur_combo.configure(values=[], state="disabled")
            self.dur_var.set("")
            self.dur_btn.state(["disabled"])
            self.dur_note.set("")
            return
        result = bm.duration_view(d, None)
        view = result["view"]
        self.dur_combo.configure(values=[str(x) for x in view["allowed"]], state="readonly" if view["editable"] else "disabled")
        self.dur_var.set("" if view["current"] is None else str(view["current"]).removesuffix(".0"))
        self._duration_chosen = view["current"]
        self.dur_note.set(view["reason"] or "")
        self._update_dur_button()

    def _on_duration_pick(self, _e):
        try:
            self._duration_chosen = float(self.dur_var.get())
            if self._duration_chosen == int(self._duration_chosen):
                self._duration_chosen = int(self._duration_chosen)
        except ValueError:
            self._duration_chosen = None
        self._update_dur_button()

    def _update_dur_button(self):
        d = (self.detail or {}).get("duration")
        ok = bool(d) and bm.duration_view(d, self._duration_chosen)["can_apply"] and not self._busy_now()
        self.dur_btn.state(["!disabled"] if ok else ["disabled"])

    def apply_duration(self):
        if not self.detail or self._duration_chosen is None:
            return
        self.calls.call(CAP_BLOCKOUT_SET_DURATION, {"project_id": self.get_project_id(), "shot_key": self.detail["key"],
                                                   "seconds": self._duration_chosen, "base_digest": self.detail.get("digest") or ""},
                        on_success=lambda r: (notify.success(T["duration_changed"] + (". " + T["duration_rebuild"] if r.get("rebuild_needed") else "")),
                                              self.refresh()),
                        on_error=self._duration_failed)

    def _duration_failed(self, result):
        code = error_parts(result)[0]
        if code == "conflict":
            notify.warning(T["duration_stale"])
            self.refresh()
        else:
            notify.error(result_error_text(result))

    # --- действия -------------------------------------------------------------------
    def _busy_now(self):
        rv = self.get_run_view()
        return is_run_busy(getattr(rv, "state", None)) or bool(self.state and (self.state.get("run") or {}).get("active"))

    def _selected(self):
        return list(self.tree.selection["keys"])

    def _render_actions(self):
        if not self.state:
            return
        ctx = {"runBusy": is_run_busy(getattr(self.get_run_view(), "state", None)), "forbidden": self.forbidden}
        for b in bm.action_buttons(self.state, self._selected(), ctx):
            self.buttons[b["action"]].state(["!disabled"] if b["enabled"] else ["disabled"])
            self._hints[b["action"]] = b["hint"]
        focus = self.tree.selection["focus"]
        row = next((s for s in self.state["shots"] if s["key"] == focus), None)
        self.shots_btn.state(["!disabled"] if row else ["disabled"])
        chain = next((c for sc in self.state["scenes"] for c in sc["chains"] if row and c["chain_id"] == row.get("chain_id")), None)
        can_blend = bool(chain and chain.get("blend_exists") and blockout_local.blender_binary_path() and not self._busy_now())
        self.blend_btn.state(["!disabled"] if can_blend else ["disabled"])
        self._chain = chain
        self.preview_btn.state(["!disabled"] if (self.state.get("preview") or {}).get("path") else ["disabled"])
        self._update_dur_button()

    def run_action(self, action):
        if not self.state:
            return
        keys = self._selected()
        if action == "leave_as_is":
            self.calls.call(CAP_BLOCKOUT_LEAVE_AS_IS, {"project_id": self.get_project_id(), "shot_keys": keys},
                            on_success=lambda r: (notify.success(bm.leave_as_is_text(r)), self.refresh()), on_error=self._action_failed)
            return
        target = bm.plan_target(action, self.state, keys)
        self.flow.start(action, target, lambda step, a=action, t=target: self._on_step(a, t, step), self._action_failed)

    def _on_step(self, action, target, step):
        kind = step["kind"]
        if kind == "started":
            self._started(action)
            return
        view = bm.plan_dialog(action, step["plan"], len(target.get("shot_keys") or []))
        if kind == "blocked" or view["kind"] == "problems":
            self.ask_choice(view["title"], "\n".join(f"{p['label']}: {p['message']}" for p in view["problems"]), [("close", T["dialog_close"])], "close")
            return
        body = "\n".join([view["text"], *[f"{p['label']}: {p['message']}" for p in view["problems"]], *view["notes"]]).strip()
        if self.ask_confirm(ConfirmSpec(view["title"], body, view["go"], view["cancel"])):
            self.flow.confirm(action, target, step["plan"], lambda out, a=action, t=target: self._on_outcome(a, t, out), self._action_failed)

    def _on_outcome(self, action, target, outcome):
        if outcome["kind"] == "started":
            self._started(action)
        elif outcome["kind"] == "blocked":
            self.ask_choice(T["dialog_problems_title"], outcome["text"], [("close", T["dialog_close"])], "close")
        else:
            notify.warning(outcome["text"])
            self._on_step(action, target, outcome["step"])

    def _started(self, action):
        notify.success(bm.success_toast(action))
        if self.on_run_started:
            self.on_run_started()
        self.refresh()

    def _action_failed(self, result):
        code, reason, message, _d = error_parts(result)
        notify.error(shot_texts.error_text(code, reason, message))
        if code in ("conflict", "locked"):
            self.refresh()

    # --- только Tk ----------------------------------------------------------------------
    def open_in_shots(self):
        key = self.tree.selection["focus"]
        if key and self.on_open_shot:
            self.on_open_shot(key)

    def open_blend(self):
        chain = getattr(self, "_chain", None)
        directory = self.get_project_dir()
        if chain and directory and not blockout_local.open_blend(directory, chain["blend_path"]):
            notify.error("Не удалось открыть файл цепочки в Blender")

    def open_preview(self):
        path = (self.state.get("preview") or {}).get("path") if self.state else None
        self._open_video(path)

    def _open_video(self, rel):
        directory = self.get_project_dir()
        if rel and directory and not external.play_video(Path(directory) / rel):
            notify.error("Не удалось открыть видео во внешнем плеере")

    def _on_ctrl_key(self, e):
        if e.keysym.lower() not in ("r", "cyrillic_ka") or not key_is_for_screen(self):
            return None
        if self.buttons["redraw_shot"].instate(["!disabled"]):
            self.run_action("redraw_shot")
            return "break"
        return None

    def destroy(self):
        self.keys.clear()
        try:
            self.compare.destroy()
        except tk.TclError:
            pass
        super().destroy()
