"""Список/сетка шотов на Canvas с виртуализацией: рисуются только видимые строки."""

import tkinter as tk
import tkinter.font
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from StoryBookManager.gui.shell.widgets import ellipsize
from StoryBookManager.gui.shots import layout as L
from StoryBookManager.gui.shots import texts as T
from StoryBookManager.gui.shots.thumb_queue import ThumbRequest, priority_for
from StoryBookManager.gui.theme_tokens import COLORS, px

OVERSCAN = 0
STATE_TONES = {"ready": "success", "stale": "warning", "missing": "text_muted", "generating": "running",
               "error": "error", "partial": "warning"}
REVIEW_MARK = {"approved": "OK", "redo": "↻", "rejected": "✕"}


class ShotsListView(ttk.Frame):
    """Левая колонка: заголовки сцен, строки списка или ячейки сетки."""

    def __init__(self, parent, *, loader_factory: Callable, on_select: Callable[[str], None],
                 on_toggle_mark: Callable[[str], None], on_near_end: Callable[[int], None]):
        super().__init__(parent, style="Panel.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._on_select = on_select
        self._on_toggle = on_toggle_mark
        self._on_near_end = on_near_end
        self.canvas = tk.Canvas(self, highlightthickness=0, background=COLORS["card"], takefocus=1)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.bind("<Configure>", lambda e: self._relayout())
        self.canvas.bind("<Button-1>", self._click)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        self.font = tk.font.nametofont("SBM.Body") if hasattr(tk, "font") else None
        self.items: List[dict] = []
        self.by_key: Dict[str, dict] = {}
        self.marked: set = set()
        self.selected: Optional[str] = None
        self.mode = "list"
        self.layout = {"rows": [], "total": 0}
        self._top = 0
        self._photos: Dict[str, object] = {}   # ссылки на PhotoImage, иначе Tk их удалит
        self._hits: List[tuple] = []           # (x0, y0, x1, y1, key, kind)
        self.loader = loader_factory(self.canvas, self._on_thumb)

    # --- данные -------------------------------------------------------------------------
    def set_data(self, items: List[dict], marked: set, selected: Optional[str], mode: str) -> None:
        self.items = items
        self.by_key = {r["shot_key"]: r for r in items}
        self.marked, self.selected, self.mode = set(marked), selected, mode
        self._relayout()

    def set_marks(self, marked: set, selected: Optional[str]) -> None:
        self.marked, self.selected = set(marked), selected
        self.redraw()

    def columns(self) -> int:
        return L.columns_for_width(self.canvas.winfo_width()) if self.mode == "grid" else 1

    def _relayout(self) -> None:
        self.layout = L.layout_rows(self.items, self.mode, self.columns())
        self._top = min(self._top, max(0, self.layout["total"] - self.canvas.winfo_height()))
        self.redraw()

    def scroll_to_key(self, key: str) -> None:
        index = L.row_index_of_key(self.layout["rows"], key)
        if index < 0:
            return
        row = self.layout["rows"][index]
        height = self.canvas.winfo_height()
        top = self._top
        if row["top"] < top:
            top = row["top"] - L.HEADER_H
        elif row["top"] + row["height"] > top + height:
            top = row["top"] + row["height"] - height
        self._set_top(max(0, top))

    # --- прокрутка ---------------------------------------------------------------------------
    def _set_top(self, top: int) -> None:
        total = max(self.layout["total"], 1)
        self._top = max(0, min(top, max(0, total - self.canvas.winfo_height())))
        self.redraw()

    def _yview(self, *args) -> None:
        total = max(self.layout["total"], 1)
        height = self.canvas.winfo_height()
        if args[0] == "moveto":
            self._set_top(int(float(args[1]) * total))
        elif args[0] == "scroll":
            step = int(args[1]) * (px(L.LIST_ROW_H) if args[2] == "units" else height)
            self._set_top(self._top + step)

    def _on_yscroll(self, first, last) -> None:
        self.scroll.set(first, last)

    def _wheel(self, event) -> str:
        up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
        self._set_top(self._top + (-1 if up else 1) * 3 * px(24))
        return "break"

    # --- рисование ---------------------------------------------------------------------------
    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._hits = []
        self._photos = {}   # на канвасе остались только рисуемые сейчас картинки
        total = max(self.layout["total"], 1)
        self.scroll.set(self._top / total, min(1.0, (self._top + c.winfo_height()) / total))
        rows = self.layout["rows"]
        height = max(c.winfo_height(), 1)
        width = max(c.winfo_width(), 1)
        if not rows:
            return
        first, last = L.visible_range(rows, self._top, height, OVERSCAN)
        wanted: List[ThumbRequest] = []
        for index in range(first, last + 1):
            row = rows[index]
            y = row["top"] - self._top
            if row["kind"] == "header":
                self._draw_header(row, y, width)
            elif row["kind"] == "row":
                self._draw_list_row(row["keys"][0], y, width, index, first, last, wanted)
            else:
                for col, key in enumerate(row["keys"]):
                    self._draw_cell(key, col * px(L.GRID_CELL_W), y, index, first, last, wanted)
        self.loader.request(wanted)
        self._notify_near_end(last)

    def _notify_near_end(self, last_row: int) -> None:
        rows = self.layout["rows"]
        seen = sum(len(r.get("keys", [])) for r in rows[:last_row + 1])
        self._on_near_end(seen)

    def _draw_header(self, row: dict, y: int, width: int) -> None:
        self.canvas.create_rectangle(0, y, width, y + px(L.HEADER_H), fill=COLORS["surface"], outline="")
        self.canvas.create_text(px(8), y + px(L.HEADER_H) // 2, anchor="w", text=T.fmt("scene_header", n=row["scene"]),
                                fill=COLORS["text_primary"], font="SBM.Subtitle")

    def _thumb_request(self, item: dict, size: int, priority: int) -> Optional[ThumbRequest]:
        rel = item.get("thumb_path")
        fit = (px(L.LIST_ROW_H) - px(8),) * 2 if self.mode == "list" else (px(L.GRID_CELL_W) - px(16), px(64))
        return ThumbRequest(item["shot_key"], rel, size, item.get("thumb_rev"), priority, fit) if rel else None

    def _cached_photo(self, item: dict, size: int):
        req = self._thumb_request(item, size, 0)
        return self.loader.cached(req) if req else False

    def _draw_list_row(self, key: str, y: int, width: int, index: int, first: int, last: int, wanted: list) -> None:
        item = self.by_key[key]
        h = px(L.LIST_ROW_H)
        selected = key == self.selected
        c = self.canvas
        c.create_rectangle(0, y, width, y + h, fill=COLORS["accent_soft"] if selected else COLORS["card"],
                           outline=COLORS["running"] if selected else COLORS["border"], width=2 if selected else 1)
        mark = "[x]" if key in self.marked else "[ ]"
        c.create_text(px(8), y + h // 2, anchor="w", text=mark, fill=COLORS["text_primary"], font="SBM.Mono")
        self._hits.append((0, y, px(40), y + h, key, "mark"))
        thumb_x, thumb = px(44), h - px(8)
        req = self._thumb_request(item, 128, priority_for(index, (first + last) // 2, selected))
        photo = self.loader.cached(req) if req else False
        if photo:
            self._photos[key] = photo
            c.create_image(thumb_x + thumb // 2, y + h // 2, image=photo)
        elif req is not None:
            wanted.append(req)
        else:
            c.create_text(thumb_x + thumb // 2, y + h // 2, text="—", fill=COLORS["text_muted"], font="SBM.Body")
        text_x = thumb_x + thumb + px(8)
        room = max(width - text_x - px(8), px(40))
        title = f"{key}  ·  {item.get('state_title') or ''}"
        review = (item.get("review") or {}).get("status_title")
        if review:
            title += f"  ·  {review}"
        c.create_text(text_x, y + px(18), anchor="w", text=self._fit(title, "SBM.Body", room),
                      fill=COLORS[STATE_TONES.get(item.get("state"), "text_primary")] if item.get("state") == "error"
                      else COLORS["text_primary"], font="SBM.Body")
        c.create_text(text_x, y + px(42), anchor="w", text=self._fit(item.get("prompt_preview") or "", "SBM.Caption", room),
                      fill=COLORS["text_muted"], font="SBM.Caption")
        self._hits.append((px(40), y, width, y + h, key, "select"))

    def _draw_cell(self, key: str, x: int, y: int, index: int, first: int, last: int, wanted: list) -> None:
        item = self.by_key[key]
        w, h = px(L.GRID_CELL_W), px(L.GRID_ROW_H)
        selected = key == self.selected
        c = self.canvas
        c.create_rectangle(x + 2, y + 2, x + w - 2, y + h - 2, fill=COLORS["accent_soft"] if selected else COLORS["card"],
                           outline=COLORS["running"] if selected else COLORS["border"], width=2 if selected else 1)
        req = self._thumb_request(item, 128, priority_for(index, (first + last) // 2, selected))
        photo = self.loader.cached(req) if req else False
        if photo:
            self._photos[key] = photo
            c.create_image(x + w // 2, y + px(44), image=photo)
        elif req is not None:
            wanted.append(req)
        else:
            c.create_text(x + w // 2, y + px(44), text=T.SHOT_TEXTS["no_image"], fill=COLORS["text_muted"], font="SBM.Caption")
        mark = "[x]" if key in self.marked else "[ ]"
        c.create_text(x + px(8), y + h - px(14), anchor="w", text=f"{mark} {key}", fill=COLORS["text_primary"], font="SBM.Mono")
        status = (item.get("review") or {}).get("status")
        if status in REVIEW_MARK:
            c.create_text(x + w - px(8), y + h - px(14), anchor="e", text=REVIEW_MARK[status], fill=COLORS["text_primary"],
                          font="SBM.Caption")
        self._hits.append((x, y, x + px(30), y + h, key, "mark"))
        self._hits.append((x, y, x + w, y + h, key, "select"))

    def _fit(self, text: str, font_name: str, room: int) -> str:
        try:
            return ellipsize(text, tk.font.nametofont(font_name), room)
        except Exception:
            return text[:max(room // 7, 4)]

    # --- события -------------------------------------------------------------------------------
    def _click(self, event) -> None:
        self.canvas.focus_set()
        for x0, y0, x1, y1, key, kind in self._hits:
            if x0 <= event.x < x1 and y0 <= event.y < y1:
                (self._on_toggle if kind == "mark" else self._on_select)(key)
                return

    def _on_thumb(self, request: ThumbRequest, photo) -> None:
        if request.key in self.by_key:
            self.redraw()
