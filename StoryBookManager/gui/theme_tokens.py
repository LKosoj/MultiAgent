"""
Токены оформления StoryBookManager (без tkinter)
================================================

Палитра (раздел 1.2 UX-спеки), подбор шрифтов по платформе (1.3), масштаб
HiDPI и проверка контраста. Модуль не импортирует tkinter, поэтому его можно
проверять без дисплея.
"""

import sys
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Палитра
# ---------------------------------------------------------------------------

COLORS: dict = {
    "window_bg":       "#F5F0EB",  # фон окна
    "surface":         "#EDE8E2",  # панели, кнопки
    "card":            "#FBF8F4",  # карточки, поля ввода, журнал
    "text_primary":    "#2C2018",  # основной текст
    "text_muted":      "#5E5042",  # вторичный текст, подписи
    "text_disabled":   "#6B5E50",  # отключённые элементы
    "accent":          "#8A4524",  # акцент, выбранный пункт
    "accent_hover":    "#6F3519",  # кнопка при наведении
    "accent_text":     "#FFFFFF",  # текст на акценте
    "accent_soft":     "#EBD9CC",  # фон выбранной строки, подсветка
    "success":         "#2F6B42",  # успех
    "error":           "#9B2335",  # ошибка
    "warning":         "#7A5A00",  # предупреждение
    "info":            "#2B5797",  # информация
    "running":         "#1A5276",  # прогон идёт, рамка фокуса
    "border":          "#C8BFB4",  # декоративные разделители
    "border_strong":   "#8F8274",  # границы полей и кнопок
    "nav_bg":          "#2C2018",  # левая навигация
    "nav_text":        "#F5F0EB",  # пункты навигации
    "nav_muted":       "#C9BDB0",  # подписи в навигации
    "nav_selected_bg": "#8A4524",  # выбранный раздел
}

# Единственное место, где перечислены темы и масштабы интерфейса
UI_SCALES = (100, 125, 150, 200)

# Отступы (сетка 4 px, раздел 1.4); значения в px на 96 dpi, наружу - через px()
PAD_PANEL = 12
PAD_BLOCK = 16
PAD_INLINE = 8

# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------

FONT_CANDIDATES: dict = {
    "win32": {"sans": ("Segoe UI",), "mono": ("Consolas",)},
    "darwin": {"sans": ("Helvetica Neue",), "mono": ("Menlo",)},
    "linux": {"sans": ("Noto Sans", "DejaVu Sans"), "mono": ("DejaVu Sans Mono",)},
}

# роль -> (семейство, размер pt, полужирный); размеры не меньше 9
FONT_ROLES: dict = {
    "title":    ("sans", 14, True),
    "subtitle": ("sans", 11, True),
    "body":     ("sans", 10, False),
    "caption":  ("sans", 9, False),
    "mono":     ("mono", 10, False),
    "mono_sm":  ("mono", 9, False),
}


def _platform_key(platform: Optional[str]) -> str:
    platform = platform or sys.platform
    if platform.startswith("win"):
        return "win32"
    if platform == "darwin":
        return "darwin"
    return "linux"


def pick_font(kind: str, available: Iterable[str], platform: Optional[str] = None) -> Optional[str]:
    """Первый установленный шрифт из списка платформы; None - брать системный."""
    have = {name.lower() for name in available}
    for name in FONT_CANDIDATES[_platform_key(platform)].get(kind, ()):
        if name.lower() in have:
            return name
    return None


# ---------------------------------------------------------------------------
# Контраст (WCAG 2.1)
# ---------------------------------------------------------------------------

def _channel(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    color = color.lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Масштаб
# ---------------------------------------------------------------------------

_factor = 1.0


def normalize_ui_scale(value) -> int:
    """Значение вне допустимого набора означает 100."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 100
    return value if value in UI_SCALES else 100


def scale_factor(tk_scaling: float, ui_scale: int = 100) -> float:
    """Во сколько раз пиксели больше, чем на экране 96 dpi (tk scaling = 96/72)."""
    return (tk_scaling * ui_scale / 100.0) / (96.0 / 72.0)


def set_scale(factor: float) -> None:
    global _factor
    _factor = max(0.5, float(factor))


def px(n: float) -> int:
    """Пиксели с учётом масштаба интерфейса."""
    return int(round(n * _factor))


def enable_dpi_awareness() -> bool:
    """Windows: сообщает системе, что приложение само масштабируется (иначе окно размыто)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return True
    except Exception:
        return False
