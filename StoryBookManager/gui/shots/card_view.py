"""Карточка шота: плитки частей, поля правки, ревью, отчёты, версии."""

import os
import tkinter as tk
import tkinter.font
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from StoryBookManager.gui.imagetools.edit_model import editable_extension
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS
from StoryBookManager.gui.shell.shortcuts import TEXT_INPUT_CLASSES
from StoryBookManager.gui.shell.widgets import Tooltip, WrapRow
from StoryBookManager.gui.shots import model as M
from StoryBookManager.gui.shots import texts as T
from StoryBookManager.gui.shots.thumb_queue import ThumbRequest
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, PAD_PANEL, px

TILE_FIT = (190, 130)
PARTS = ("start", "end", "video")


class CardView(ttk.Frame):
    """Правая часть экрана. Сама ничего не решает: действия уходят в callbacks."""

    def __init__(self, parent, *, loader_factory: Callable, resolve: Callable[[str], Optional[str]],
                 actions: Dict[str, Callable]):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.act = actions
        self.resolve = resolve
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
        self._bind_wheel(self.canvas)
        self.loader = loader_factory(self.canvas, self._on_thumb)
        self._wrap: List[ttk.Label] = []
        self._texts: Dict[str, tk.Text] = {}
        self._duration = tk.StringVar()
        self._note: Optional[tk.Text] = None
        self._tiles: Dict[str, dict] = {}
        self._card: Optional[dict] = None
        self._entry: Optional[dict] = None
        self._ctx: dict = {}
        self.show_message(T.TK_ONLY["pick_shot"])

    # --- служебное -----------------------------------------------------------------------
    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        width = max(event.width - px(PAD_PANEL) * 2 - px(24), px(200))
        for label in self._wrap:
            try:
                label.configure(wraplength=width)
            except tk.TclError:
                pass

    def _bind_wheel(self, widget) -> None:
        """Колесо привязываем к своим виджетам (текстовые поля и таблицы крутят себя сами)."""
        if isinstance(widget, (tk.Text, ttk.Treeview)):
            return
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event) -> None:
        if self.content.winfo_reqheight() <= self.canvas.winfo_height():
            return
        delta = -1 if (getattr(event, "num", 0) == 4 or event.delta > 0) else 1
        self.canvas.yview_scroll(delta * 3, "units")

    @property
    def current_key(self) -> Optional[str]:
        return self._card.get("shot_key") if self._card else None

    def reset(self) -> None:
        """Забыть показанную карточку (следующий показ не берёт правки из полей)."""
        self._card = None

    def _clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        self._wrap, self._texts, self._tiles, self._note = [], {}, {}, None

    def _label(self, parent, text, style="TLabel", tone="", wrap=True) -> ttk.Label:
        label = ttk.Label(parent, text=text, style=style, justify="left")
        if tone:
            label.configure(foreground=COLORS[tone])
        if wrap:
            self._wrap.append(label)
        return label

    # --- сообщения ------------------------------------------------------------------------
    def show_message(self, text: str, retry: Optional[Callable] = None) -> None:
        self._clear()
        self._card = None
        self._label(self.content, text, style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        if retry:
            ttk.Button(self.content, text=T.SHOT_TEXTS["retry"], command=retry).grid(row=1, column=0, sticky="w",
                                                                                      pady=(px(PAD_INLINE), 0))
        self._bind_wheel(self.content)

    # --- ошибка формы и чтение формы ---------------------------------------------------------------------
    def form_error(self) -> str:
        """Текст ошибки, если длительность не число; поле подсвечивается."""
        entry = getattr(self, "_duration_entry", None)
        raw = self._duration.get().strip().replace(",", ".")
        bad = False
        if raw and self._card is not None:
            try:
                float(raw)
            except ValueError:
                bad = True
        if entry is not None:
            try:
                entry.configure(foreground=COLORS["error"] if bad else COLORS["text_primary"])
            except (tk.TclError, KeyError):
                pass
        return T.TK_ONLY["duration_invalid"] if bad else ""

    def read_form(self) -> Tuple[dict, str]:
        if self._card is None:
            return {}, ""
        form = {}
        for name, widget in self._texts.items():
            form[name] = widget.get("1.0", "end-1c")
        raw = self._duration.get().strip().replace(",", ".")
        try:
            form["duration_s"] = float(raw) if raw else None
            if form["duration_s"] is not None and float(form["duration_s"]).is_integer():
                form["duration_s"] = int(form["duration_s"])
        except ValueError:
            form["duration_s"] = None
        note = self._note.get("1.0", "end-1c") if self._note is not None else ""
        return form, note

    # --- построение карточки -----------------------------------------------------------------
    def show_card(self, card: dict, entry: dict, ctx: dict, draft: Optional[dict] = None,
                  draft_note: Optional[str] = None) -> None:
        """ctx: busy_reason, run_busy, rows (строка списка), project_dir-функция уже в resolve."""
        self._clear()
        self._card, self._entry, self._ctx = card, entry or {}, ctx
        S = T.SHOT_TEXTS
        row = 0
        self._label(self.content, T.card_title(card["shot_key"], card.get("scene"), card.get("duration_s")),
                    style="Subtitle.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        state_line = card.get("state_title") or ""
        if card.get("review"):
            state_line += f"  ·  {card['review']['status_title']}"
        self._label(self.content, state_line, style="Caption.TLabel").grid(row=row, column=0, sticky="w")
        row += 1
        lines = M.stale_lines(card)
        if lines:
            box = ttk.LabelFrame(self.content, text=T.SHOT_TEXTS["stale_title"], padding=px(PAD_INLINE))
            box.grid(row=row, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
            for line in lines:
                self._label(box, line, tone="warning").pack(anchor="w")
            row += 1
        # ревью
        review = WrapRow(self.content)
        review.grid(row=row, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self.review_buttons = {}
        current = (card.get("review") or {}).get("status")
        for letter, key, status in (("a", "approve", "approved"), ("r", "redo", "redo"), ("x", "reject", "rejected")):
            btn = ttk.Button(review, text=f"{S[key]} ({letter.upper()})", command=lambda l=letter: self.act["letter"](l),
                             style="Accent.TButton" if status == current else "TButton")
            review.add(btn)
            self.review_buttons[letter] = btn
        row += 1
        # плитки
        tiles = ttk.Frame(self.content, style="Panel.TFrame")
        tiles.grid(row=row, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        self._build_tiles(tiles, card)
        row += 1
        # поля
        row = self._build_fields(row, card, draft)
        row = self._build_note(row, draft_note)
        row = self._build_actions(row, card)
        self._extra_row = row
        self.set_edit_block(ctx.get("edit_block", ""))
        self.content.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)
        self._bind_wheel(self.content)

    def _build_tiles(self, tiles: WrapRow, card: dict) -> None:
        S = T.SHOT_TEXTS
        scene, shot = card.get("scene"), card.get("shot")
        parts = card.get("parts") or {}
        specs = [(p, (parts.get(p) or {}).get("path"), bool((parts.get(p) or {}).get("exists"))) for p in PARTS]
        blockout = card.get("blockout") or {}
        for p in PARTS:
            if blockout.get(p) and scene is not None:
                specs.append((f"blockout_{p}", M.blockout_rel(scene, shot, p), True))
        for name, rel, exists in specs:
            frame = ttk.Frame(tiles, relief="solid", borderwidth=1, padding=px(4))
            title = (T.part_title(name) if name in PARTS
                     else f"{S['part_blockout']}: {T.part_title(name.split('_', 1)[1])}")
            ttk.Label(frame, text=title, style="Caption.TLabel").pack(anchor="w")
            missing = S["part_missing"] if (parts.get(name) or {}).get("state") == "missing" else S["no_image"]
            image = ttk.Label(frame, text=missing, width=20, anchor="center", cursor="hand2" if exists else "")
            image.pack()
            frame.grid(row=len(self._tiles) // 2, column=len(self._tiles) % 2, sticky="nw", padx=(0, px(8)), pady=(0, px(8)))
            self._tiles[name] = {"rel": rel if exists else None, "label": image, "frame": frame}
            if exists and rel:
                image.bind("<Button-1>", lambda e, n=name: self._open_tile(n))
                req = ThumbRequest(card["shot_key"], rel, 512, self._file_rev(rel), 0, TILE_FIT)
                photo = self.loader.cached(req)
                if photo:
                    image.configure(image=photo, text="", width=0)
                    image.image = photo
                else:
                    self.loader.request([req])
            elif name in PARTS and self._ctx.get("generating"):
                image.configure(text=S["generating"])
            if name in ("start", "end") and exists and rel and "edit" in self.act and editable_extension(rel):
                edit = ttk.Button(frame, text=IMAGE_TEXTS["edit_open"], command=lambda n=name: self.act["edit"](n))
                edit.pack(anchor="w", pady=(px(4), 0))
                Tooltip(edit, lambda: self._ctx.get("edit_block", ""))
                self._tiles[name]["edit"] = edit
            if name == "video" and exists and rel:
                row = ttk.Frame(frame)
                row.pack(anchor="w")
                ttk.Button(row, text=S["watch_with_sound"], command=lambda r=rel: self.act["watch"](r)).pack(side="left")
                shared = M.shared_video_keys(card)
                if shared:
                    ttk.Label(frame, text=T.fmt("video_shared", keys=", ".join(shared)), style="Caption.TLabel",
                              wraplength=px(240)).pack(anchor="w")

    def _file_rev(self, rel: str) -> str:
        """Токен версии файла: после перегенерации или возврата версии меняется mtime/размер."""
        path = self.resolve(rel)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        return f"{st.st_mtime_ns}-{st.st_size}" if st else ""

    def _on_thumb(self, request: ThumbRequest, photo) -> None:
        for name, tile in self._tiles.items():
            if tile["rel"] == request.rel and photo:
                try:
                    tile["label"].configure(image=photo, text="", width=0)
                    tile["label"].image = photo
                except tk.TclError:
                    pass

    def _open_tile(self, name: str) -> None:
        rel = self._tiles[name]["rel"]
        if name == "video" or name.endswith("_video"):
            self.act["watch"](rel)
            return
        images = [(T.part_title(n) if n in PARTS else n, t["rel"]) for n, t in self._tiles.items()
                  if t["rel"] and not n.endswith("video") and n != "video"]
        index = next((i for i, (_, r) in enumerate(images) if r == rel), 0)
        self.act["view"](images, index)

    def _text_field(self, row: int, name: str, label: str, value: str, editable: bool, height: int = 4,
                    parent=None) -> int:
        parent = parent or self.content
        self._label(parent, label, style="Caption.TLabel").grid(row=row, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        widget = tk.Text(parent, height=height, wrap="word", background=COLORS["card"],
                         foreground=COLORS["text_primary"], font="SBM.Body", highlightthickness=1,
                         highlightbackground=COLORS["border_strong"], highlightcolor=COLORS["running"], undo=True)
        widget.insert("1.0", value)
        if not editable:
            widget.configure(state="disabled", background=COLORS["surface"])
        widget.grid(row=row + 1, column=0, sticky="ew")
        self._texts[name] = widget
        return row + 2

    def _build_fields(self, row: int, card: dict, draft: Optional[dict]) -> int:
        S = T.SHOT_TEXTS
        editable = M.can_edit(card)
        allowed = set((card.get("editable") or {}).get("fields") or [])
        form = {**M.form_from_card(card), **(draft or {})}
        if not editable:
            reason = (card.get("editable") or {}).get("locked_reason") or (card.get("editable") or {}).get("hint") or ""
            if reason:
                self._label(self.content, reason, style="Caption.TLabel", tone="warning").grid(row=row, column=0, sticky="w")
                row += 1
        for name, label, height in (("start_prompt", S["prompt_start"], 4), ("end_prompt", S["prompt_end"], 4),
                                    ("video_prompt", S["prompt_video"], 4)):
            if name == "end_prompt" and card.get("end") is None:
                continue
            row = self._text_field(row, name, label, form[name], editable and name in allowed, height)
        self._label(self.content, S["duration_label"], style="Caption.TLabel").grid(row=row, column=0, sticky="w",
                                                                                     pady=(px(PAD_INLINE), 0))
        dur = form.get("duration_s")
        self._duration.set("" if dur is None else str(dur).replace(".", ","))
        entry = ttk.Entry(self.content, textvariable=self._duration, width=10,
                          state="normal" if editable and "duration_s" in allowed else "disabled")
        entry.grid(row=row + 1, column=0, sticky="w")
        self._duration_entry = entry
        row += 2
        # отрицательные промпты под раскрывающимся «Дополнительно»
        toggle = ttk.Button(self.content, text=T.SHOT_TEXTS["advanced"] + " ▸")
        toggle.grid(row=row, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        extra = ttk.Frame(self.content, style="Panel.TFrame")
        extra.columnconfigure(0, weight=1)
        extra_rows = [("start_negative_prompt", S["prompt_neg_start"])]
        if card.get("end") is not None:
            extra_rows.append(("end_negative_prompt", S["prompt_neg_end"]))
        inner = 0
        for name, label in extra_rows:
            inner = self._text_field(inner, name, label, form[name], editable and name in allowed, 3, parent=extra)

        def flip():
            if extra.winfo_ismapped():
                extra.grid_remove()
                toggle.configure(text=T.SHOT_TEXTS["advanced"] + " ▸")
            else:
                extra.grid()
                toggle.configure(text=T.SHOT_TEXTS["advanced"] + " ▾")
                self.canvas.after_idle(lambda: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        toggle.configure(command=flip)
        extra.grid(row=row + 1, column=0, sticky="ew")
        extra.grid_remove()
        return row + 2

    def _build_note(self, row: int, draft_note: Optional[str]) -> int:
        self._label(self.content, T.SHOT_TEXTS["note_label"], style="Caption.TLabel").grid(
            row=row, column=0, sticky="w", pady=(px(PAD_INLINE), 0))
        note = tk.Text(self.content, height=3, wrap="word", background=COLORS["card"], foreground=COLORS["text_primary"],
                       font="SBM.Body", highlightthickness=1, highlightbackground=COLORS["border_strong"],
                       highlightcolor=COLORS["running"], undo=True)
        note.insert("1.0", draft_note if draft_note is not None else (self._entry.get("note") or ""))
        note.grid(row=row + 1, column=0, sticky="ew")
        self._note = note
        return row + 2

    def _build_actions(self, row: int, card: dict) -> int:
        S = T.SHOT_TEXTS
        bar = WrapRow(self.content)
        bar.grid(row=row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        self.save_btn = ttk.Button(bar, text=S["save"], style="Accent.TButton", command=self.act["save"])
        self.discard_btn = ttk.Button(bar, text=S["discard_edit"], command=self.act["discard"])
        self.regen_btn = ttk.Button(bar, text=S["regen_button"], command=self.act["regen"])
        for w in (self.save_btn, self.discard_btn, self.regen_btn):
            bar.add(w)
        self.regen_tip = Tooltip(self.regen_btn, lambda: self._ctx.get("busy_reason", ""))
        self.set_busy(self._ctx.get("busy_reason", ""))
        return row + 1

    def set_edit_block(self, reason: str) -> None:
        """«Править ИИ…» на плитках «Начало»/«Конец»: выключена, пока идёт прогон, перегенерация или другая правка."""
        self._ctx["edit_block"] = reason
        for tile in self._tiles.values():
            button = tile.get("edit")
            if button is not None:
                try:
                    button.configure(state="disabled" if reason else "normal")
                except tk.TclError:
                    pass

    def set_busy(self, reason: str) -> None:
        self._ctx["busy_reason"] = reason
        btn = getattr(self, "regen_btn", None)
        if btn is not None and self._card is not None:
            try:
                btn.configure(state="disabled" if reason else "normal")
            except tk.TclError:
                pass
        self._sync_restore()

    def _sync_restore(self) -> None:
        """«Вернуть эту версию» доступна при выбранной версии и без блокировки; «Сравнить» не блокируется."""
        btn = getattr(self, "_restore_btn", None)
        if btn is None:
            return
        ok = getattr(self, "_restore_sel", False) and not self._ctx.get("busy_reason")
        try:
            btn.configure(state="normal" if ok else "disabled")
        except tk.TclError:
            pass

    # --- отчёты и версии ----------------------------------------------------------------------
    def show_secondary(self, reports: Optional[List[dict]], versions: Optional[dict]) -> None:
        if self._card is None:
            return
        self._restore_btn = None
        box = getattr(self, "_secondary", None)
        if box is not None:
            try:
                box.destroy()
            except tk.TclError:
                pass
        S = T.SHOT_TEXTS
        box = ttk.Frame(self.content, style="Panel.TFrame")
        box.grid(row=self._extra_row, column=0, sticky="ew", pady=(px(PAD_BLOCK), 0))
        box.columnconfigure(0, weight=1)
        self._secondary = box
        findings = [f for g in reports or [] for f in g.get("findings") or []]
        frame = ttk.LabelFrame(box, text=T.fmt("reports_title", n=len(findings)), padding=px(PAD_INLINE))
        frame.grid(row=0, column=0, sticky="ew")
        if not findings:
            ttk.Label(frame, text=S["reports_none"], style="Caption.TLabel").pack(anchor="w")
        else:
            tree = ttk.Treeview(frame, columns=("level", "source", "text"), show="headings", height=min(len(findings), 5))
            for col, key, w in (("level", "reports_level", 80), ("source", "reports_source", 140), ("text", "reports_text", 360)):
                tree.heading(col, text=S[key])
                tree.column(col, width=px(w), stretch=col == "text")
            for g in reports or []:
                for f in g.get("findings") or []:
                    tree.insert("", "end", values=(f.get("level", ""), g.get("title", ""), f.get("message") or f.get("text") or ""))
            tree.pack(fill="x")
        items = (versions or {}).get("versions") or []
        vframe = ttk.LabelFrame(box, text=T.fmt("versions_title", n=len(items)), padding=px(PAD_INLINE))
        vframe.grid(row=1, column=0, sticky="ew", pady=(px(PAD_INLINE), 0))
        if not items:
            ttk.Label(vframe, text=S["versions_none"], style="Caption.TLabel").pack(anchor="w")
            return
        tree = ttk.Treeview(vframe, columns=("v",), show="", height=min(len(items), 6), selectmode="extended")
        tree.column("v", width=px(400), stretch=True)
        by_iid = {}
        for v in items:
            iid = tree.insert("", "end", values=(M.version_label(v),))
            by_iid[iid] = v
        tree.pack(fill="x")
        row = WrapRow(vframe)
        row.pack(fill="x", pady=(px(4), 0))
        compare = ttk.Button(row, text=S["compare"], state="disabled")
        restore = ttk.Button(row, text=S["restore"], state="disabled")
        row.add(compare)
        row.add(restore)
        self._restore_btn, self._restore_sel = restore, False
        self._restore_tip = Tooltip(restore, lambda: self._ctx.get("busy_reason", ""))

        def selection():
            return [by_iid[i] for i in tree.selection()]

        def changed(_e=None):
            sel = selection()
            compare.configure(state="normal" if len(sel) in (1, 2) else "disabled")
            self._restore_sel = len(sel) == 1
            self._sync_restore()

        def do_compare():
            sel = selection()
            left = sel[0]["id"]
            right = sel[1]["id"] if len(sel) == 2 else "current"
            self.act["compare"](left, right)

        tree.bind("<<TreeviewSelect>>", changed)
        compare.configure(command=do_compare)
        restore.configure(command=lambda: (lambda s: self.act["restore"](s[0]["id"], s[0].get("number")))(selection()))
        self._bind_wheel(box)
