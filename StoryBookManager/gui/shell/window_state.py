"""Размер и положение окна: проверка сохранённой геометрии (без tkinter)."""

import re

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]-?\d+)([+-]-?\d+))?$")
MIN_VISIBLE = 100  # сколько пикселей окна должно остаться на экране


def default_geometry(screen_w: int, screen_h: int) -> str:
    """1280x800, а на маленьком экране - сколько помещается."""
    return f"{min(1280, max(1, screen_w - 40))}x{min(800, max(1, screen_h - 80))}"


def _num(token: str) -> int:
    return int(token[1:]) if token[0] == "+" else -int(token[1:])


def clamp_geometry(saved, screen_w: int, screen_h: int, min_w: int, min_h: int) -> str:
    """Привести сохранённую строку к тому, что помещается на экране.

    Размер ограничивается экраном и минимумом. Позиция, ушедшая за экран
    (отключили монитор), отбрасывается, окно центрируется.
    """
    match = _GEOMETRY_RE.match(str(saved or "").strip())
    if not match:
        saved = default_geometry(screen_w, screen_h)
        match = _GEOMETRY_RE.match(saved)
    width, height = int(match.group(1)), int(match.group(2))
    width = max(min_w, min(width, screen_w))
    height = max(min_h, min(height, screen_h))
    if match.group(3) is None:
        return f"{width}x{height}"
    x, y = _num(match.group(3)), _num(match.group(4))
    visible = (x + width > MIN_VISIBLE and x < screen_w - MIN_VISIBLE
               and y >= 0 and y < screen_h - MIN_VISIBLE)
    if not visible:
        x, y = max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
    return f"{width}x{height}+{x}+{y}"
