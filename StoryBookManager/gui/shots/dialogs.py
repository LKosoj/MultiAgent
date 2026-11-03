"""Окна экрана «Кадры»: выбор из трёх вариантов, перегенерация, просмотр картинки, сравнение версий."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from StoryBookManager.gui.imagetools.viewer import ImageZoomViewer
from StoryBookManager.gui.shell.dialogs import _center_on
from StoryBookManager.gui.shots import model as M
from StoryBookManager.gui.shots import texts as T
from StoryBookManager.gui.theme_tokens import COLORS, PAD_BLOCK, PAD_INLINE, px


class ChoiceDialog(tk.Toplevel):
    """Заголовок, текст и несколько кнопок; result - id нажатой кнопки или None (Esc, закрытие)."""

    def __init__(self, parent, title: str, body: str, buttons: Sequence[Tuple[str, str]], default: str = ""):
        super().__init__(parent)
        self.result: Optional[str] = None
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.configure(background=COLORS["window_bg"])
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=title, style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(box, text=body, wraplength=px(440), justify="left").pack(anchor="w", pady=(px(PAD_INLINE), px(PAD_BLOCK)))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.buttons = {}
        for ident, text in buttons:
            btn = ttk.Button(row, text=text, command=lambda i=ident: self._pick(i))
            btn.pack(side="left", padx=(0, px(PAD_INLINE)))
            self.buttons[ident] = btn
        self.bind("<Escape>", lambda e: self._pick(None))
        self.bind("<Return>", self._press_focused)
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))
        _center_on(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        (self.buttons.get(default) or next(iter(self.buttons.values()))).focus_set()

    def _press_focused(self, _event):
        widget = self.focus_get()
        if isinstance(widget, ttk.Button):
            widget.invoke()
        return "break"

    def _pick(self, ident):
        self.result = ident
        self.destroy()

    @classmethod
    def ask(cls, parent, title, body, buttons, default="") -> Optional[str]:
        dialog = cls(parent, title, body, buttons, default)
        parent.wait_window(dialog)
        return dialog.result


# Окно просмотра картинок шота строится на холсте с зумом (imagetools); имя сохранено для экрана «Кадры»
ImageViewer = ImageZoomViewer


class RegenerateDialog(tk.Toplevel):
    """«Перегенерировать»: что пересоздать, план, цена, связанные шоты, запуск с подтверждением цены."""

    def __init__(self, parent, controller, keys: List[str]):
        super().__init__(parent)
        self.ctl, self.keys = controller, list(keys)
        self.preview: Optional[dict] = None
        self.view: Optional[dict] = None
        self.closed = False
        S = T.SHOT_TEXTS
        self.title(S["regen_title"] if len(keys) == 1 else T.fmt("regen_title_many", k=len(keys)))
        self.transient(parent.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=self.title(), style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(box, text=S["regen_what"], style="Caption.TLabel").pack(anchor="w", pady=(px(PAD_INLINE), 0))
        self.what = tk.StringVar(value=controller.regen_default_what(keys))
        for value, key in (("images", "regen_images"), ("video", "regen_video"), ("both", "regen_both")):
            ttk.Radiobutton(box, text=S[key], value=value, variable=self.what, command=self._load).pack(anchor="w")
        self.info = ttk.Label(box, text=T.SHOT_TEXTS["regen_loading"], wraplength=px(460), justify="left")
        self.info.pack(anchor="w", pady=(px(PAD_INLINE), 0))
        self.problems = ttk.Label(box, text="", wraplength=px(460), justify="left", foreground=COLORS["error"])
        self.problems.pack(anchor="w")
        self.abandon_var = tk.BooleanVar()
        self.abandon = ttk.Checkbutton(box, text=S["regen_abandon"], variable=self.abandon_var, command=self._sync)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(px(PAD_BLOCK), 0))
        self.cancel_btn = ttk.Button(row, text=S["regen_cancel"], command=self.close)
        self.cancel_btn.pack(side="left")
        self.go_btn = ttk.Button(row, text=S["regen_go_free"], style="Accent.TButton", command=self._go, state="disabled")
        self.go_btn.pack(side="right")
        self._abandon_parent = box
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        _center_on(self, parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.cancel_btn.focus_set()
        self._load()

    def close(self) -> None:
        self.closed = True
        self.destroy()

    def _load(self) -> None:
        self.preview = self.view = None
        self.go_btn.configure(state="disabled")
        self.info.configure(text=T.SHOT_TEXTS["regen_loading"])
        self.ctl.regen_preview(self.keys, self.what.get(), self._on_preview, self._on_preview_fail)

    def _on_preview_fail(self, text: str) -> None:
        if not self.closed:
            self.info.configure(text=text)

    def _on_preview(self, preview: dict) -> None:
        if self.closed:
            return
        self.preview = preview
        self.view = M.regen_view(preview)
        v, S = self.view, T.SHOT_TEXTS
        lines = [T.fmt("regen_cost", cost=T.format_rub_amount(v["cost_rub"]), k=v["shot_count"])]
        note = v["button"]["note"]
        if note:
            lines.append(S[note])
        if v["linked_keys"]:
            lines.append(T.fmt("regen_linked", keys=", ".join(v["linked_keys"])))
        if v["disk_needed"]:
            lines.append(T.fmt("regen_disk", size=f"{v['disk_needed'] / 1048576:.0f} МБ"))
        lines.append(S["regen_versions_kept"])
        self.info.configure(text="\n".join(lines))
        self.problems.configure(text="\n".join(f"{p['message']}" for p in v["problems"]))
        if v["needs_abandon"]:
            self.problems.configure(text=T.fmt("regen_pending_keys", keys=", ".join(v["pending_keys"])))
            self.abandon.pack(anchor="w", before=self.cancel_btn.master)
        else:
            self.abandon.pack_forget()
        self._sync()

    def _sync(self) -> None:
        if not self.view:
            return
        ok = self.view["can_start"] and (not self.view["needs_abandon"] or self.abandon_var.get())
        self.go_btn.configure(text=self.view["button"]["label"], state="normal" if ok else "disabled")

    def _go(self) -> None:
        if not self.preview or not self.view:
            return
        self.go_btn.configure(state="disabled")
        self.ctl.regen_start(self.keys, self.what.get(), self.preview, self.abandon_var.get(), self.close, self._on_start_fail)

    def _on_start_fail(self, info: dict) -> None:
        if self.closed:
            return
        kind = info["kind"]
        text = info.get("text") or ""
        if kind == "need_abandon":
            self.problems.configure(text=T.fmt("regen_pending_keys", keys=", ".join(info.get("keys") or [])))
            self.abandon.pack(anchor="w", before=self.cancel_btn.master)
            self.abandon_var.set(False)
            self.go_btn.configure(state="disabled")
            return
        if kind == "blocked":
            text = "\n".join(p["message"] or "" for p in info.get("problems") or []) or text
        self.problems.configure(text=text)
        if kind == "refresh":
            self._load()
        elif kind != "locked":
            self.go_btn.configure(state="normal")


class CompareDialog(tk.Toplevel):
    """Две версии рядом: картинки или видео, промпты, действия «Использовать левую/правую»."""

    def __init__(self, parent, data: dict, load_photo: Callable[[Optional[str], Tuple[int, int]], object],
                 resolve: Callable[[str], Optional[str]], watch: Callable[[str], None],
                 restore: Callable[[str, object], None]):
        super().__init__(parent)
        S = T.SHOT_TEXTS
        self.title(T.SHOT_TEXTS["compare_title"])
        self.transient(parent.winfo_toplevel())
        self.configure(background=COLORS["window_bg"])
        self._photos = []
        box = ttk.Frame(self, padding=px(PAD_BLOCK))
        box.pack(fill="both", expand=True)
        for column, side_name in enumerate(("left", "right")):
            side = data[side_name]
            box.columnconfigure(column, weight=1)
            frame = ttk.LabelFrame(box, text=self._caption(side), padding=px(PAD_INLINE))
            frame.grid(row=0, column=column, sticky="nsew", padx=px(4))
            for part in ("start", "end", "video"):
                info = (side.get("parts") or {}).get(part)
                line = ttk.Frame(frame)
                line.pack(fill="x", pady=2)
                ttk.Label(line, text=T.part_title(part), width=9).pack(side="left")
                if not info or not info.get("exists"):
                    ttk.Label(line, text=T.SHOT_TEXTS["compare_none"], style="Caption.TLabel").pack(side="left")
                    continue
                path = resolve(info["path"])
                if part == "video":
                    ttk.Button(line, text=T.TK_ONLY["compare_watch_" + side_name],
                               command=lambda p=path: p and watch(p)).pack(side="left")
                else:
                    photo = load_photo(path, (px(260), px(200)))
                    if photo:
                        self._photos.append(photo)
                        ttk.Label(line, image=photo).pack(side="left")
            changed = any((data.get("diff") or {}).get("prompt_changed", {}).values())
            ttk.Label(frame, text=T.SHOT_TEXTS["compare_prompt_changed" if changed else "compare_prompt_same"],
                      style="Caption.TLabel").pack(anchor="w")
            if side.get("id") != "current":
                label = S["use_left"] if side_name == "left" else S["use_right"]
                ttk.Button(frame, text=label, command=lambda s=side: (self.destroy(), restore(s["id"], s.get("number")))
                           ).pack(anchor="w", pady=(px(PAD_INLINE), 0))
        ttk.Button(box, text=S["close"], command=self.destroy).grid(row=1, column=0, columnspan=2, sticky="e",
                                                                   pady=(px(PAD_INLINE), 0))
        self.bind("<Escape>", lambda e: self.destroy())
        _center_on(self, parent)

    @staticmethod
    def _caption(side: dict) -> str:
        if side.get("id") == "current":
            return T.SHOT_TEXTS["compare_current"]
        return f"№{side.get('number')}  {side.get('created_at') or ''}"
