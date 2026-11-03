"""Холст с картинкой: зум колесом и клавишами, перетаскивание мышью, «по размеру окна»."""

import logging
import tkinter as tk
from typing import Callable, Optional

from StoryBookManager.gui.imagetools.zoom_model import ZoomState
from StoryBookManager.gui.theme_tokens import COLORS

logger = logging.getLogger(__name__)

RESIZE_DELAY_MS = 60
ZOOM_KEYS = {
    "in": ("<plus>", "<equal>", "<KP_Add>"),
    "out": ("<minus>", "<KP_Subtract>"),
    "fit": ("<Key-0>", "<KP_0>"),
    "actual": ("<Key-1>", "<KP_1>"),
}


def open_image(path: str):
    """Картинка с диска; с прозрачностью - на белом фоне, как в старой панели. None, если не открылась."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                base = Image.new("RGB", rgba.size, (255, 255, 255))
                base.paste(rgba, mask=rgba.split()[-1])
                return base
            return image.convert("RGB")
    except Exception:
        logger.debug("Картинка %s не открылась", path, exc_info=True)
        return None


class ZoomCanvas(tk.Canvas):
    """Рисует только видимую часть картинки, поэтому масштаб 800 % не раздувает память."""

    def __init__(self, parent, on_change: Optional[Callable[[str], None]] = None, empty_text: str = ""):
        super().__init__(parent, highlightthickness=0, background=COLORS["surface"], takefocus=1)
        self.state = ZoomState()
        self._pil = None
        self._photo = None
        self._on_change = on_change
        self._empty_text = empty_text
        self._resize_job = None
        self._drag = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self.zoom("in"))
        self.bind("<Button-5>", lambda e: self.zoom("out"))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.bind("<Destroy>", self._on_destroy, add="+")

    # --- картинка ----------------------------------------------------------------------
    @property
    def has_image(self) -> bool:
        return self._pil is not None

    @property
    def image_size(self):
        return self._pil.size if self._pil is not None else (0, 0)

    def load(self, path: Optional[str], empty_text: Optional[str] = None) -> bool:
        """Показать файл «по размеру окна». False и подпись вместо картинки, если файла нет или он не открылся."""
        pil = open_image(path) if path else None
        self._pil = pil
        if empty_text is not None:
            self._empty_text = empty_text
        if pil is not None:
            self.state.set_image(*pil.size)
        else:
            self.state.set_image(0, 0)
        self.render()
        return pil is not None

    # --- управление ---------------------------------------------------------------------
    def zoom(self, direction: str) -> None:
        if self._pil is not None:
            self.state.step(direction)
            self.render()

    def fit(self) -> None:
        if self._pil is not None:
            self.state.set_view(max(self.winfo_width(), 1), max(self.winfo_height(), 1))
            self.state.fit()
            self.render()

    def actual(self) -> None:
        if self._pil is not None:
            self.state.actual()
            self.render()

    def bind_keys(self, widget) -> None:
        """Клавиши + = - 0 1 на выбранном виджете (окно просмотра или сам холст)."""
        actions = {"in": lambda e: self.zoom("in"), "out": lambda e: self.zoom("out"),
                   "fit": lambda e: self.fit(), "actual": lambda e: self.actual()}
        for name, sequences in ZOOM_KEYS.items():
            for seq in sequences:
                widget.bind(seq, lambda e, a=actions[name]: (a(e), "break")[1], add="+")

    # --- события --------------------------------------------------------------------------
    def _on_wheel(self, event) -> None:
        self.zoom("in" if event.delta > 0 else "out")

    def _on_press(self, event) -> None:
        self.focus_set()
        self._drag = (event.x, event.y)
        self.configure(cursor="fleur" if self.state.can_pan() else "")

    def _on_drag(self, event) -> None:
        if self._drag is None or self._pil is None:
            return
        self.state.pan(event.x - self._drag[0], event.y - self._drag[1])
        self._drag = (event.x, event.y)
        self.render()

    def _on_configure(self, _event) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(RESIZE_DELAY_MS, self.render)

    def _on_destroy(self, event) -> None:
        if event.widget is self and self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass

    # --- рисование --------------------------------------------------------------------------
    def render(self) -> None:
        self._resize_job = None
        try:
            self._draw()
        except tk.TclError:
            return
        if self._on_change is not None:
            self._on_change(self.state.percent)

    def _draw(self) -> None:
        self.delete("all")
        vw, vh = max(self.winfo_width(), 1), max(self.winfo_height(), 1)
        self._photo = None
        if self._pil is None:
            if self._empty_text:
                self.create_text(vw // 2, vh // 2, text=self._empty_text, fill=COLORS["text_muted"])
            return
        from PIL import Image, ImageTk

        st = self.state
        st.set_view(vw, vh)
        iw, ih = self._pil.size
        scale = st.scale
        left = (vw - iw * scale) / 2 + st.dx     # левый верхний угол картинки на холсте
        top = (vh - ih * scale) / 2 + st.dy
        x1, y1 = max(0.0, left), max(0.0, top)
        x2, y2 = min(float(vw), left + iw * scale), min(float(vh), top + ih * scale)
        if x2 - x1 < 1 or y2 - y1 < 1:
            return
        box = (max(0.0, (x1 - left) / scale), max(0.0, (y1 - top) / scale),
               min(float(iw), (x2 - left) / scale), min(float(ih), (y2 - top) / scale))
        size = (max(1, round(x2 - x1)), max(1, round(y2 - y1)))
        resample = Image.NEAREST if scale >= 2 else Image.LANCZOS
        shown = self._pil.resize(size, resample, box=box)
        self._photo = ImageTk.PhotoImage(shown)
        self.create_image(round(x1), round(y1), anchor="nw", image=self._photo)
