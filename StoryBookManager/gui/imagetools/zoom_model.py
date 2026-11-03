"""Чистая логика зума и панорамы картинки (без tkinter).

Числа и правила общие с React (golden_imagetools_vectors.json): шаг 1,25, пределы 0,1..8,
«по размеру окна» с запасом 0,95, двигать можно только картинку больше окна.
"""

import math
from typing import Tuple

ZOOM_STEP = 1.25
ZOOM_MIN = 0.1
ZOOM_MAX = 8.0
FIT_MARGIN = 0.95


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(scale: float) -> float:
    return min(ZOOM_MAX, max(ZOOM_MIN, scale))


def _round4(value: float) -> float:
    return math.floor(value * 10000 + 0.5) / 10000


def zoom_step(scale, direction: str) -> float:
    """Шаг 1,25 с пределами 0,1..8; не число или не больше нуля считается масштабом 1."""
    base = scale if _num(scale) and scale > 0 else 1.0
    return _round4(_clamp(base * ZOOM_STEP if direction == "in" else base / ZOOM_STEP))


def fit_scale(view_w, view_h, img_w, img_h) -> float:
    """По размеру окна с запасом 0,95; нулевые или неизвестные размеры дают 1."""
    if not all(_num(v) and v > 0 for v in (view_w, view_h, img_w, img_h)):
        return 1.0
    return _round4(_clamp(min(view_w / img_w, view_h / img_h) * FIT_MARGIN))


def zoom_percent(scale) -> str:
    value = scale if _num(scale) else 1.0
    return f"{math.floor(value * 100 + 0.5)}%"


def pan_allowed(scale, view, img) -> bool:
    """Двигать картинку можно, только когда она больше окна хотя бы по одной стороне."""
    if not (_num(scale) and scale > 0):
        return False
    (vw, vh), (iw, ih) = view, img
    if not all(_num(v) and v > 0 for v in (vw, vh, iw, ih)):
        return False
    return iw * scale > vw + 1e-9 or ih * scale > vh + 1e-9


class ZoomState:
    """Масштаб и сдвиг от центра. Режим «по окну» пересчитывается при смене размера окна, ручной - нет."""

    def __init__(self) -> None:
        self.scale = 1.0
        self.fit_mode = True
        self.dx = 0.0
        self.dy = 0.0
        self.view: Tuple[int, int] = (0, 0)
        self.image: Tuple[int, int] = (0, 0)

    def set_image(self, width: int, height: int) -> None:
        """Новая картинка (в том числе при листании): снова «по окну»."""
        self.image = (int(width), int(height))
        self.fit()

    def set_view(self, width: int, height: int) -> None:
        self.view = (int(width), int(height))
        if self.fit_mode:
            self.scale = fit_scale(*self.view, *self.image)
        self._limit()

    def fit(self) -> None:
        self.fit_mode = True
        self.dx = self.dy = 0.0
        self.scale = fit_scale(*self.view, *self.image)

    def actual(self) -> None:
        self.fit_mode = False
        self.dx = self.dy = 0.0
        self.scale = 1.0

    def step(self, direction: str) -> None:
        self.fit_mode = False
        self.scale = zoom_step(self.scale, direction)
        self._limit()

    @property
    def percent(self) -> str:
        return zoom_percent(self.scale)

    def can_pan(self) -> bool:
        return pan_allowed(self.scale, self.view, self.image)

    def pan(self, ddx: float, ddy: float) -> None:
        if self.can_pan():
            self.dx += ddx
            self.dy += ddy
            self._limit()

    def _limit(self) -> None:
        """Картинка не уезжает за край: по каждой оси сдвиг не больше излишка размера над окном."""
        for axis in (0, 1):
            extra = (self.image[axis] * self.scale - self.view[axis]) / 2
            value = self.dx if axis == 0 else self.dy
            value = 0.0 if extra <= 0 else max(-extra, min(extra, value))
            if axis == 0:
                self.dx = value
            else:
                self.dy = value
