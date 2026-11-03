"""Общие чистые помощники экранов: время, размер, дата (без tkinter)."""

import math
import re

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")

_SP = " "


def format_timecode(seconds) -> str:
    """m:ss или h:mm:ss, как formatTimecode в React."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    total = max(0, int(math.floor(value if math.isfinite(value) else 0)))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def format_bytes(size) -> str:
    """Размер как в React: «N Б», КБ/МБ/ГБ с одной цифрой и запятой (пробел перед единицей - обычный/неразрывный как в TS)."""
    n = abs(float(size or 0))
    if n < 1024:
        return f"{int(n) if n == int(n) else n}{_SP}Б"
    for power, unit in ((1, "КБ"), (2, "МБ"), (3, "ГБ")):
        value = n / 1024 ** power
        if value < 1024 or unit == "ГБ":
            return f"{value:.1f}".replace(".", ",") + f"{_SP}{unit}"
    return ""


def format_created_at(iso) -> str:
    """«3 ноября 2026, 10:15» из строки ISO без пересчёта часового пояса; пустое значение даёт «—»."""
    if not iso:
        return "—"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", str(iso))
    if not m:
        return str(iso)
    idx = int(m.group(2)) - 1
    if not 0 <= idx < 12:
        return str(iso)
    return f"{int(m.group(3))} {_MONTHS[idx]} {m.group(1)}, {m.group(4)}:{m.group(5)}"
