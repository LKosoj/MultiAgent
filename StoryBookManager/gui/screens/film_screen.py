"""Экран «Фильм»: плеер без звука, таймлайн из пяти дорожек, субтитры, музыка, файл, версии."""

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from StoryBookManager.gui.screens import film_model as fm
from StoryBookManager.gui.screens.calls import (
    CAP_MEDIA_SUBTITLES, CAP_MEDIA_TIMELINE, CAP_REGENERATE_PREVIEW, CAP_REGENERATE_START, CAP_VERSIONS_LIST, StudioCalls,
)
from StoryBookManager.gui.screens.common_model import format_bytes, format_created_at, format_timecode
from StoryBookManager.gui.screens.film_player import FilmPlayer
from StoryBookManager.gui.screens.film_texts import FILM_TEXTS as T
from StoryBookManager.gui.screens.film_timeline import FilmTimeline
from StoryBookManager.gui.screens.keys import KeyBindings, key_is_for_screen
from StoryBookManager.gui.shell import external, notify
from StoryBookManager.gui.shell.adapters import SectionAdapter
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec
from StoryBookManager.gui.shell.dialogs import ConfirmDialog
from StoryBookManager.gui.shell.widgets import PanelHeader, Tooltip, WrapRow
from StoryBookManager.gui.shots.dialogs import ChoiceDialog
from StoryBookManager.gui.shots.model import error_parts, is_run_busy, result_error_text, start_payload_from_preview
from StoryBookManager.gui.shots.texts import format_rub_amount
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

LISTEN_TEXT = "Слушать"  # есть только в Tk (в эталоне React аудио играет прямо в таблице)
PROBE_LIMIT = 20
MAX_REPEATS = 10
REPEAT_PAUSE_MS = 1500


def fmt(text_id, **vars_):
    out = T[text_id]
    for k, v in vars_.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class FilmScreen(ttk.Frame, SectionAdapter):
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
        self.ask_confirm = lambda spec: ConfirmDialog.ask(self.winfo_toplevel(), spec)
        self.ask_choice = lambda title, body, buttons, default="": ChoiceDialog.ask(self, title, body, buttons, default)
        self.timeline = None
        self.cues = []
        self.error = None
        self.layout_data = {"blocks": [], "duration_s": 0, "out_of_cut": 0}
        self.source = "film"
        self._prev_run = None
        self._repeat = 0
        self._after = None
        self._loaded_path = None
        self._loaded_key = None
        self._resolution = None
        self._subs_truncated = False
        self._gen = 0
        self._versions_gen = 0
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        PanelHeader(self, T["title"]).grid(row=0, column=0, sticky="ew")
        top = ttk.Frame(self, style="Panel.TFrame")
        top.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.banner_var = tk.StringVar(value=T["loading"])
        self.banner = ttk.Label(top, textvariable=self.banner_var, wraplength=px(760), justify="left", foreground=COLORS["warning"])
        self.banner.pack(anchor="w")
        self.retry_btn = ttk.Button(top, text=T["retry"], command=self.refresh)
        self.goto_btn = ttk.Button(top, text=T["empty_goto"], command=lambda: self.on_goto_generation and self.on_goto_generation())
        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=3, uniform="film")
        self.body.columnconfigure(1, weight=2, uniform="film")
        self.body.rowconfigure(0, weight=1)
        left = ttk.Frame(self.body, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(PAD_PANEL)))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        bar = WrapRow(left, pad_x=px(PAD_INLINE))
        bar.grid(row=0, column=0, sticky="ew")
        self.source_var = tk.StringVar(value=T["source_film"])
        self.source_box = ttk.Combobox(bar, textvariable=self.source_var, state="readonly", width=14,
                                       values=[T["source_film"], T["source_photo"]])
        self.source_box.bind("<<ComboboxSelected>>", self._on_source)
        self.rebuild_btn = ttk.Button(bar, text=T["rebuild"], command=self.rebuild_montage)
        self.zoom_in_btn = ttk.Button(bar, text=T["zoom_in"], command=lambda: self.timeline_view.zoom(True))
        self.zoom_out_btn = ttk.Button(bar, text=T["zoom_out"], command=lambda: self.timeline_view.zoom(False))
        self.zoom_fit_btn = ttk.Button(bar, text=T["zoom_fit"], command=lambda: self.timeline_view.zoom_fit())
        self.reveal_btn = ttk.Button(bar, text="Показать в папке", command=self.reveal)
        bar.set_items([self.source_box, self.rebuild_btn, self.zoom_in_btn, self.zoom_out_btn, self.zoom_fit_btn, self.reveal_btn])
        self.player = FilmPlayer(left, on_time=self._on_player_time, on_external=self.play_external)
        self.player.grid(row=1, column=0, sticky="nsew", pady=(px(PAD_INLINE), 0))
        self.cue_var = tk.StringVar()

        ttk.Label(left, textvariable=self.cue_var, wraplength=px(560), justify="left").grid(row=2, column=0, sticky="w")
        self.timeline_view = FilmTimeline(left, on_seek=self.player.seek, on_open_shot=self._open_shot)
        self.timeline_view.grid(row=3, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.out_var = tk.StringVar()
        ttk.Label(left, textvariable=self.out_var, style="Caption.TLabel").grid(row=4, column=0, sticky="w")
        self.tabs = ttk.Notebook(self.body)
        self.tabs.grid(row=0, column=1, sticky="nsew")
        self.subs_list = self._tab_tree(T["tab_subtitles"], (("time", T["col_time"], 90), ("text", T["col_text"], 160)))
        self.truncated_var = tk.StringVar()
        ttk.Label(self.subs_list.master, textvariable=self.truncated_var, style="Caption.TLabel").pack(anchor="w")
        self.music_list = self._tab_tree(T["tab_music"], (("scenes", T["col_scenes"], 80), ("source", T["col_source"], 100)), name_col=T["col_name"])
        self.file_tab = ttk.Frame(self.tabs, style="Panel.TFrame")
        self.file_text = tk.Text(self.file_tab, height=8, wrap="word", background=COLORS["card"], state="disabled")
        self.file_text.pack(fill="both", expand=True)
        self.tabs.add(self.file_tab, text=T["tab_file"])
        self.versions_text = tk.Text(self.tabs, height=8, wrap="word", background=COLORS["card"], state="disabled")
        self.tabs.add(self.versions_text, text=T["tab_versions"])
        self.music_btn = ttk.Button(self.music_list.master, text=T["regen_music"], command=self.regen_music)
        self.music_btn.pack(side="bottom", anchor="w", pady=(px(PAD_INLINE), 0), before=self.music_list)
        self.listen_btn = ttk.Button(self.music_list.master, text=LISTEN_TEXT, command=self.listen_music, state="disabled")
        self.listen_btn.pack(side="bottom", anchor="w", pady=(px(PAD_INLINE), 0), before=self.music_list)
        self.music_list.bind("<<TreeviewSelect>>", lambda e: self._update_listen())
        self.report_btn = ttk.Button(self.file_tab, text=T["report_link"], command=self.open_report)
        self._rebuild_hint = self._music_hint = ""
        Tooltip(self.rebuild_btn, lambda: self._rebuild_hint)
        Tooltip(self.music_btn, lambda: self._music_hint)
        self.body.grid_remove()
        self.keys = KeyBindings(self)
        for seq in ("<space>", "<Left>", "<Right>", "<Shift-Left>", "<Shift-Right>", "<plus>", "<minus>", "<equal>"):
            self.keys.bind(seq, lambda e, sq=seq: self._key(e, sq))
        # ушли из раздела: останавливаем воспроизведение, чтобы цикл кадров не крутился скрытым
        self.bind("<Unmap>", self._on_unmap, add="+")

    def _tab_tree(self, title, columns, name_col=None):
        frame = ttk.Frame(self.tabs, style="Panel.TFrame")
        cols = [c[0] for c in columns]
        tree = ttk.Treeview(frame, columns=cols, show="tree headings" if name_col else "headings", height=8)
        if name_col:
            tree.heading("#0", text=name_col)
            tree.column("#0", width=px(120))
        for cid, label, width in columns:
            tree.heading(cid, text=label)
            tree.column(cid, width=px(width))
        tree.pack(fill="both", expand=True)
        self.tabs.add(frame, text=title)
        return tree

    # --- клавиши -------------------------------------------------------------------
    def _key(self, event, seq):
        if not key_is_for_screen(self):
            return None
        names = {"space": " ", "<Left>": "ArrowLeft", "<Right>": "ArrowRight", "<Shift-Left>": "ArrowLeft",
                 "<Shift-Right>": "ArrowRight", "<plus>": "+", "<equal>": "=", "<minus>": "-"}
        action = fm.key_action(names.get(seq, event.keysym), "Shift" in seq, False)
        if not action:
            return None
        if action["type"] == "toggle":
            self.player.toggle()
        elif action["type"] == "seek_by":
            self.player.seek_by(action["delta"])
        elif action["type"] == "zoom":
            self.timeline_view.zoom(action["direction"] == "in")
        return "break"

    def _on_unmap(self, event):
        if event.widget is self:
            self.player.pause()

    # --- загрузка ------------------------------------------------------------------
    def load_project(self, _project=None):
        self.timeline = None
        self.cues = []
        self.error = None
        self._repeat = 0
        self.player.close()
        self._loaded_path = None
        self._loaded_key = None
        self._gen += 1  # ответы прежнего проекта больше не принимаем
        self.refresh()

    def on_project_changed(self, project):
        self.load_project(project)

    def on_show(self):
        if self.timeline is None:
            self.refresh()

    def on_refresh(self):
        self.refresh()

    def update_view(self, brief):
        """Сводка проекта: когда прогон закончился, экран перечитывается; тост только после успешного конца."""
        rv = self.get_run_view()
        state = getattr(rv, "state", None)
        out = fm.run_transition(self._prev_run, state)
        self._prev_run = state
        if out["reload"]:
            if out["toast"]:
                notify.success(T["toast_done"])
            self.refresh()
        elif self.timeline is not None:
            self._render_banners()
        self._update_run_buttons()

    def on_status(self, brief):
        self.update_view(brief)

    def refresh(self):
        pid = self.get_project_id()
        self._gen += 1  # растёт и при пустом проекте: поздний ответ прежнего проекта отбрасывается
        if not pid:
            return
        gen = self._gen
        self.calls.call(CAP_MEDIA_TIMELINE, {"project_id": pid, "probe_limit": PROBE_LIMIT},
                        on_success=lambda r: self._timeline_loaded(gen, r), on_error=lambda r: self._failed(gen, r), key="film_timeline")

    def _failed(self, gen, result):
        if gen != self._gen:
            return
        if self.timeline is not None:
            notify.warning(T["refresh_failed"])
            return
        self.error = result_error_text(result)
        self._render()

    def _timeline_loaded(self, gen, timeline):
        if gen != self._gen:
            return
        self.timeline = timeline
        self.error = None
        self._update_run_buttons()
        pid = self.get_project_id()
        self.calls.call(CAP_MEDIA_SUBTITLES, {"project_id": pid, "source": "auto"},
                        on_success=lambda r: self._subs_loaded(gen, r), on_error=lambda r: self._subs_loaded(gen, {"cues": []}), key="film_subs")
        # студия досчитывает длительности: перечитываем, пока что-то осталось (до 10 раз)
        if timeline.get("durations_pending", 0) > 0 and self._repeat < MAX_REPEATS:
            self._repeat += 1
            if self._after:
                self.after_cancel(self._after)
            self._after = self.after(REPEAT_PAUSE_MS, self.refresh)
        else:
            self._repeat = 0

    def _subs_loaded(self, gen, subs):
        if gen != self._gen:
            return
        self.cues = subs.get("cues", [])
        self._subs_truncated = bool(subs.get("truncated"))
        self._render()

    # --- показ ---------------------------------------------------------------------
    def _screen(self):
        return fm.resolve_state(self.timeline, bool(self.error))

    def _render_banners(self):
        rv = self.get_run_view()
        building = fm.is_montage_running(getattr(rv, "state", None), [getattr(rv, "step_id", "")])
        final = (self.timeline or {}).get("final") or {}
        banners = fm.banners(bool(final.get("stale_hint")), bool((final.get("video") or {}).get("older_than_shots")),
                             (self.timeline or {}).get("source"), building)
        self.banner_var.set("\n".join(b["text"] for b in banners))
        self._update_run_buttons()

    def _run_busy(self):
        return is_run_busy(getattr(self.get_run_view(), "state", None))

    def _update_run_buttons(self):
        """Пока идёт прогон, «Пересобрать» и «Перегенерировать музыку» отключены (подсказка объясняет почему)."""
        busy = self._run_busy()
        for btn in (self.rebuild_btn, self.music_btn):
            btn.state(["disabled"] if busy else ["!disabled"])
        self._rebuild_hint = T["rebuild_blocked"] if busy else ""
        self._music_hint = T["music_blocked"] if busy else ""

    def _render(self):
        screen = self._screen()
        self.retry_btn.pack_forget()
        self.goto_btn.pack_forget()
        if screen != "ready":
            # экран ушёл из «готово»: плеер закрываем, ключ файла сбрасываем (новый файл откроется заново)
            self.player.close()
            self._loaded_path = None
            self._loaded_key = None
        if screen == "loading":
            self.banner_var.set(T["loading"])
            self.body.grid_remove()
            return
        if screen == "error":
            self.banner_var.set(fmt("load_error", reason=(self.error or "").rstrip(".")))
            self.retry_btn.pack(anchor="w")
            self.body.grid_remove()
            return
        if screen == "empty":
            self.banner_var.set(T["empty"])
            self.goto_btn.pack(anchor="w")
            self.body.grid_remove()
            return
        self.body.grid()
        self._render_banners()
        self.layout_data = fm.build_layout(self.timeline, self.cues)
        self.timeline_view.set_layout(self.layout_data)
        self.out_var.set(fmt("out_of_cut", n=self.layout_data["out_of_cut"]) if self.layout_data["out_of_cut"] else "")
        self._fill_tabs()
        self._load_video()

    def _exists(self):
        final = self.timeline["final"]
        return {"video": bool((final.get("video") or {}).get("exists")), "photo": bool((final.get("photo_video") or {}).get("exists"))}

    def _load_video(self):
        if self._screen() != "ready":
            self.player.close()
            self._loaded_path = None
            self._loaded_key = None
            return
        if self._loaded_path is None:
            self.source = fm.default_source(self._exists())
            self.source_var.set(T["source_film"] if self.source == "film" else T["source_photo"])
        video = fm.source_video(self.timeline, self.source)
        directory = self.get_project_dir()
        if not video or not directory:
            return
        path = str(Path(directory) / video["path"])
        # после пересборки путь тот же, но файл другой: ключ включает размер и время изменения
        key = (path, video.get("size"), video.get("modified"))
        if key != self._loaded_key:
            self._loaded_path, self._loaded_key = path, key
            self._resolution = None
            self.player.load(path, video.get("duration_s"), on_loaded=self._on_video_opened)
            self._fill_file()

    def _on_video_opened(self, size):
        self._resolution = size
        self._fill_file()

    def _on_source(self, _e):
        self.source = "film" if self.source_var.get() == T["source_film"] else "photo"
        self._loaded_path = None
        self._loaded_key = None
        self._load_video()

    def _on_player_time(self, t):
        self.timeline_view.set_cursor(t)
        cue = fm.cue_at(self.cues, t)
        self.cue_var.set(f"{T['subs_now']}: {cue['text']}" if cue else "")

    def _fill_tabs(self):
        self.subs_list.delete(*self.subs_list.get_children())
        for c in self.cues:
            self.subs_list.insert("", "end", values=(format_timecode(c["start_s"]), c["text"]))
        if not self.cues:
            self.subs_list.insert("", "end", values=("", T["subs_none"]))
        self.truncated_var.set(T["subs_truncated"] if self._subs_truncated else "")
        self.music_list.delete(*self.music_list.get_children())
        rows = fm.music_rows(self.timeline["tracks"]["music"]["clips"])
        for r in rows:
            self.music_list.insert("", "end", iid=r["id"], text=r["name"] if r["exists"] else f"{r['name']} ({T['no_file']})",
                                  values=(r["scenes"], r["source"]))
        self._update_listen()
        self._fill_file()
        self.load_versions()

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _fill_file(self):
        if not self.timeline:
            return
        video = fm.source_video(self.timeline, self.source)
        res = getattr(self, "_resolution", None)
        rows = fm.file_rows(video, {"width": res[0], "height": res[1]} if res else None)
        self._set_text(self.file_text, "\n".join(f"{r['label']}: {r['value']}" for r in rows))
        if (self.timeline["final"] or {}).get("render_report_path"):
            self.report_btn.pack(anchor="w", pady=(px(PAD_INLINE), 0))
        else:
            self.report_btn.pack_forget()

    def load_versions(self):
        pid = self.get_project_id()
        lines = []
        self._versions_gen += 1
        vgen = self._versions_gen

        def got(kind, result):
            if vgen != self._versions_gen or pid != self.get_project_id():
                return  # ответ прежнего запроса или другого проекта
            versions = sorted(result.get("versions") or [], key=lambda v: -v.get("number", 0))
            lines.append(T["versions_music" if kind == "music" else "versions_film"])
            for v in versions:
                lines.append(f"  №{v['number']} · {format_created_at(v.get('created_at'))} · {v.get('reason_title') or ''}")
            if not versions:
                lines.append("  " + T["versions_none"])
            if len(lines) and getattr(self, "_versions_done", 0) + 1 >= 2:
                self._set_text(self.versions_text, "\n".join(lines))
            self._versions_done = getattr(self, "_versions_done", 0) + 1

        self._versions_done = 0
        for kind in ("music", "film"):
            self.calls.call(CAP_VERSIONS_LIST, {"project_id": pid, "kind": kind}, on_success=lambda r, k=kind: got(k, r),
                            on_error=lambda r, k=kind: got(k, {"versions": []}))

    # --- действия --------------------------------------------------------------------
    def _open_shot(self, key):
        if self.on_open_shot:
            self.on_open_shot(key)

    def play_external(self):
        video = fm.source_video(self.timeline, self.source) if self.timeline else None
        directory = self.get_project_dir()
        if video and directory and not external.play_video(Path(directory) / video["path"]):
            notify.error(T["video_failed"])

    def open_report(self):
        rel = ((self.timeline or {}).get("final") or {}).get("render_report_path")
        directory = self.get_project_dir()
        if rel and directory and not external.open_external(Path(directory) / rel):
            notify.error(T["video_failed"])

    def _selected_music(self):
        sel = self.music_list.selection()
        if not sel or not self.timeline:
            return None
        rows = fm.music_rows(self.timeline["tracks"]["music"]["clips"])
        return next((r for r in rows if r["id"] == sel[0]), None)

    def _update_listen(self):
        row = self._selected_music()
        self.listen_btn.state(["!disabled"] if row and row["exists"] else ["disabled"])

    def listen_music(self):
        """Звук воспроизводит внешний плеер: во встроенном плеере звука нет."""
        row = self._selected_music()
        directory = self.get_project_dir()
        if row and row["exists"] and directory and not external.open_external(Path(directory) / row["path"]):
            notify.error(T["video_failed"])

    def reveal(self):
        video = fm.source_video(self.timeline, self.source) if self.timeline else None
        directory = self.get_project_dir()
        if video and directory:
            external.reveal(Path(directory) / video["path"])

    def _regen_failed(self, result):
        notify.error(result_error_text(result))

    def rebuild_montage(self):
        pid = self.get_project_id()
        if is_run_busy(getattr(self.get_run_view(), "state", None)):
            notify.warning(T["rebuild_blocked"])
            return

        def previewed(preview):
            if not preview.get("can_start"):
                notify.warning("; ".join(p["message"] for p in preview.get("problems", [])) or T["rebuild_blocked"])
            elif (preview.get("confirm_required") or {}).get("cost_rub") or (preview.get("confirm_required") or {}).get("unknown_cost"):
                notify.warning(T["rebuild_confirm_needed"])
            else:
                payload = start_payload_from_preview({"project_id": pid, "what": "film", "launcher": "local"}, preview, False)
                self.calls.call(CAP_REGENERATE_START, payload, on_success=lambda r: self._regen_started("toast_rebuilding"),
                                on_error=self._regen_failed)

        self.calls.call(CAP_REGENERATE_PREVIEW, {"project_id": pid, "what": "film"}, on_success=previewed, on_error=self._regen_failed)

    def regen_music(self):
        pid = self.get_project_id()
        if is_run_busy(getattr(self.get_run_view(), "state", None)):
            notify.warning(T["music_blocked"])
            return

        def previewed(preview):
            confirm = preview.get("confirm_required") or {}
            cost, unknown = confirm.get("cost_rub") or 0, bool(confirm.get("unknown_cost"))
            money = format_rub_amount(cost)
            body = fmt("music_body_partial" if unknown else "music_body", cost=money) if cost > 0 else T["music_body_unknown" if unknown else "music_body_free"]
            go = fmt("music_go_paid", cost=money) if cost > 0 else T["music_go_unknown" if unknown else "music_go_free"]
            if not preview.get("can_start"):
                notify.warning("; ".join(p["message"] for p in preview.get("problems", [])) or T["music_blocked"])
                return
            if self.ask_confirm(ConfirmSpec(T["music_title"], body, go, T["music_cancel"])):
                self.calls.call(CAP_REGENERATE_PREVIEW, {"project_id": pid, "what": "music"}, on_success=lambda fresh: self._music_start(pid, preview, fresh),
                                on_error=self._regen_failed)

        self.calls.call(CAP_REGENERATE_PREVIEW, {"project_id": pid, "what": "music"}, on_success=previewed, on_error=self._regen_failed)

    def _music_start(self, pid, shown, fresh):
        if not fresh.get("can_start"):
            notify.warning(T["music_blocked"])
        elif fresh.get("plan_digest") != shown.get("plan_digest"):
            notify.warning(T["plan_changed"])
        elif (fresh.get("confirm_required") or {}).get("cost_rub") != (shown.get("confirm_required") or {}).get("cost_rub"):
            notify.warning(T["cost_changed"])
        else:
            payload = start_payload_from_preview({"project_id": pid, "what": "music", "launcher": "local"}, fresh, True)
            self.calls.call(CAP_REGENERATE_START, payload, on_success=lambda r: self._regen_started("toast_music"), on_error=self._regen_failed)

    def _regen_started(self, toast_id):
        notify.success(T[toast_id])
        if self.on_run_started:
            self.on_run_started()

    def destroy(self):
        self.keys.clear()
        if self._after:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        self.player.close()
        super().destroy()
