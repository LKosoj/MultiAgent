"""Таймлайн «Фильма» на Canvas: линейка, пять дорожек, курсор."""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from StoryBookManager.gui.screens import film_model as fm
from StoryBookManager.gui.theme_tokens import COLORS, px

TRACK_H = 26
RULER_H = 22
LABEL_W = 150
TONES = {"shot": "accent_soft", "unit": "accent_soft", "covered": "surface", "clip": "card", "missing": "surface", "cue": "card"}


class FilmTimeline(ttk.Frame):
    def __init__(self, parent, on_seek, on_open_shot=None):
        super().__init__(parent, style="Panel.TFrame")
        self.on_seek = on_seek
        self.on_open_shot = on_open_shot
        self.layout = {"blocks": [], "duration_s": 0, "out_of_cut": 0}
        self.scale = 1.0
        self.fit = 1.0
        self.cursor_t = 0.0
        height = px(RULER_H + TRACK_H * len(fm.TRACK_ORDER) + 6)
        self.columnconfigure(1, weight=1)
        # подписи дорожек стоят на своём холсте: при прокрутке вправо они не уезжают
        self.labels = tk.Canvas(self, background=COLORS["window_bg"], highlightthickness=0, width=px(LABEL_W), height=height)
        self.labels.grid(row=0, column=0, sticky="ns")
        self.canvas = tk.Canvas(self, background=COLORS["window_bg"], highlightthickness=0, height=height)
        self.canvas.grid(row=0, column=1, sticky="ew")
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.hbar.grid(row=1, column=1, sticky="ew")
        self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self._on_yscroll)
        for w in (self.canvas, self.labels):
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", self._wheel)
            w.bind("<Button-5>", self._wheel)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", self._double)
        self.canvas.bind("<Configure>", lambda e: self._refit())
        self.canvas.bind("<Control-MouseWheel>", lambda e: self.zoom(e.delta > 0))
        self.canvas.bind("<Control-Button-4>", lambda e: self.zoom(True))
        self.canvas.bind("<Control-Button-5>", lambda e: self.zoom(False))
        self.tip = None

    def _yview(self, *args):
        self.canvas.yview(*args)
        self.labels.yview(*args)

    def _on_yscroll(self, first, last):
        """Полоса вертикальной прокрутки появляется, только если дорожки не помещаются."""
        self.vbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.vbar.grid_remove()
        else:
            self.vbar.grid(row=0, column=2, sticky="ns")

    def _wheel(self, event):
        if event.state & 0x4:  # Ctrl - масштаб, он на своём обработчике
            return None
        up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
        self._yview("scroll", -1 if up else 1, "units")
        return "break"

    def set_layout(self, layout):
        self.layout = layout
        self._refit(reset=True)

    @property
    def duration(self):
        return self.layout["duration_s"]

    def _view_width(self):
        return max(self.canvas.winfo_width(), 1)

    def _refit(self, reset=False):
        self.fit = fm.fit_scale(self._view_width(), self.duration)
        self.scale = self.fit if reset else fm.clamp_scale(self.scale, self.fit, self.duration)
        self.redraw()

    def zoom(self, closer):
        self.scale = (fm.zoom_in if closer else fm.zoom_out)(self.scale, self.fit, self.duration)
        self.redraw()
        return "break"

    def zoom_fit(self):
        self.scale = self.fit
        self.redraw()

    def redraw(self):
        c, lab = self.canvas, self.labels
        c.delete("all")
        lab.delete("all")
        total = max(self.duration * self.scale, 1)
        height = px(RULER_H + TRACK_H * len(fm.TRACK_ORDER) + 6)
        c.configure(scrollregion=(0, 0, total, height))
        lab.configure(scrollregion=(0, 0, px(LABEL_W), height))
        for tick in fm.ruler_ticks(self.duration, self.scale):
            x = tick["t"] * self.scale
            c.create_line(x, px(RULER_H) - 6, x, px(RULER_H), fill=COLORS["border_strong"])
            c.create_text(x + 3, px(RULER_H) // 2, text=tick["label"], anchor="w", fill=COLORS["text_muted"])
        for i, track in enumerate(fm.TRACK_ORDER):
            y = px(RULER_H + TRACK_H * i)
            lab.create_text(4, y + px(TRACK_H) // 2, text=fm.TRACK_TITLES[track], anchor="w", fill=COLORS["text_primary"])
            lab.create_line(0, y + px(TRACK_H), px(LABEL_W), y + px(TRACK_H), fill=COLORS["border"])
            c.create_line(0, y + px(TRACK_H), total, y + px(TRACK_H), fill=COLORS["border"])
            blocks = [b for b in self.layout["blocks"] if b["track"] == track]
            for b in fm.merge_narrow(blocks, self.scale):
                x1, x2 = b["start_s"] * self.scale, b["end_s"] * self.scale
                tone = COLORS[TONES.get(b["tone"], "card")]
                outline = COLORS["warning"] if b.get("warning") else COLORS["border_strong"]
                dash = (3, 2) if b.get("approximate") else None
                c.create_rectangle(x1, y + 2, max(x2, x1 + 1), y + px(TRACK_H) - 2, fill=tone, outline=outline, dash=dash,
                                   tags=("block",))
                if b["label"] and (x2 - x1) >= fm.LABEL_MIN_PX:
                    c.create_text(x1 + 3, y + px(TRACK_H) // 2, text=self._fit_text(b["label"], x2 - x1 - 6), anchor="w", fill=COLORS["text_primary"])
        self._draw_cursor()

    def _fit_text(self, text, width):
        """Подпись в одну строку: не помещающийся хвост заменяется «…»."""
        font = tkfont.nametofont("TkDefaultFont")
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def _draw_cursor(self):
        self.canvas.delete("cursor")
        x = self.cursor_t * self.scale
        self.canvas.create_line(x, 0, x, px(RULER_H + TRACK_H * len(fm.TRACK_ORDER)), fill=COLORS["running"], width=2, tags="cursor")

    def set_cursor(self, t):
        self.cursor_t = t
        self._draw_cursor()
        left, right = self.canvas.xview()
        total = max(self.duration * self.scale, 1)
        x = t * self.scale
        if x < left * total or x > right * total:
            self.canvas.xview_moveto(max(0.0, (x - self._view_width() * 0.1) / total))

    def _time_at(self, event):
        left = self.canvas.canvasx(0)
        return fm.time_from_x(event.x, left, self.scale, self.duration)

    def _click(self, event):
        t = self._time_at(event)
        self.set_cursor(t)
        self.on_seek(t)

    def _double(self, event):
        t = self._time_at(event)
        row = int((self.canvas.canvasy(event.y) - px(RULER_H)) // px(TRACK_H))
        if 0 <= row < len(fm.TRACK_ORDER) and fm.TRACK_ORDER[row] == "shots" and self.on_open_shot:
            block = fm.block_at(self.layout["blocks"], "shots", t)
            if block and block.get("key"):
                self.on_open_shot(block["key"])
