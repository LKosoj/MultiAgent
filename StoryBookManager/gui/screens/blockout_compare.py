"""Сравнение картинок и видео выбранного шота: четыре картинки и два постера видео."""

import tkinter as tk
from tkinter import ttk

from StoryBookManager.gui.screens.blockout_texts import BLOCKOUT_TEXTS as T
from StoryBookManager.gui.shell.widgets import WrapRow
from StoryBookManager.gui.shots.thumb_queue import ThumbRequest
from StoryBookManager.gui.theme_tokens import COLORS, PAD_INLINE, px

IMAGES = (("blockout_start", "img_blockout_start"), ("image_start", "img_image_start"),
          ("image_end", "img_image_end"), ("blockout_end", "img_blockout_end"))
VIDEOS = (("blockout_ref", "video_ref"), ("final", "video_final"))
TILE = (180, 110)


class ComparePanel(ttk.Frame):
    def __init__(self, parent, loader_factory, on_open_video, on_zoom=None):
        super().__init__(parent, style="Panel.TFrame")
        self.on_open_video = on_open_video
        self.on_zoom = on_zoom
        self.loader = loader_factory(self._ready)
        self.tiles = {}
        self._wanted = {}
        self._detail = None
        self.title = ttk.Label(self, text=T["compare_title"], style="Subtitle.TLabel")
        self.title.pack(anchor="w")
        self.hint = ttk.Label(self, text=T["compare_hint"], style="Caption.TLabel")
        self.hint.pack(anchor="w")
        self.row = WrapRow(self, pad_x=px(PAD_INLINE), pad_y=px(PAD_INLINE))
        self.row.pack(fill="x", pady=(px(PAD_INLINE), 0))
        items = []
        for name, text_id in IMAGES + VIDEOS:
            tile = ttk.Frame(self.row, style="Panel.TFrame")
            ttk.Label(tile, text=T[text_id], style="Caption.TLabel").pack(anchor="w")
            lab = tk.Label(tile, text=T["img_none"], width=px(TILE[0]) // 7, height=px(TILE[1]) // 16,
                           background=COLORS["card"], foreground=COLORS["text_muted"], relief="solid", borderwidth=1)
            lab.pack()
            self.tiles[name] = lab
            if name in dict(VIDEOS):
                ttk.Button(tile, text="Открыть во внешнем плеере", command=lambda n=name: self._open_video(n)).pack(anchor="w", pady=(2, 0))
            else:
                lab.bind("<Button-1>", lambda e, n=name: self.on_zoom and self.on_zoom(self._ref(n)))
            items.append(tile)
        self.row.set_items(items)

    def _ref(self, name):
        d = self._detail or {}
        return (d.get("images") or {}).get(name) or (d.get("videos") or {}).get(name)

    def show(self, detail):
        """detail - ответ blockout_detail по шоту (или None: показать пустые плитки)."""
        self._detail = detail
        self._wanted = {}
        reqs = []
        for name, _t in IMAGES + VIDEOS:
            ref = self._ref(name) if detail else None
            lab = self.tiles[name]
            lab.configure(image="", text=T["img_none"])
            lab.image = None
            if ref and ref.get("servable"):
                req = ThumbRequest(detail["key"], ref["path"], 256, str(ref.get("mtime")), 0, TILE)
                self._wanted[ref["path"]] = name
                photo = self.loader.cached(req)
                if photo:
                    lab.configure(image=photo, text="", width=0, height=0)
                    lab.image = photo
                elif photo is None:
                    reqs.append(req)
        self.loader.request(reqs)

    def _ready(self, request, photo):
        name = self._wanted.get(request.rel)
        if name and photo:
            lab = self.tiles[name]
            lab.configure(image=photo, text="", width=0, height=0)
            lab.image = photo

    def _open_video(self, name):
        ref = self._ref(name)
        if ref and self.on_open_video:
            self.on_open_video(ref["path"])

    def destroy(self):
        # останавливаем фоновую загрузку миниатюр
        close = getattr(self.loader, "close", None)
        if close:
            close()
        super().destroy()
