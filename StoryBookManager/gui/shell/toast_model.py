"""Очередь тостов без tkinter: лимит, сроки, слияние повторов. Время подаётся снаружи."""

import itertools
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

MAX_TOASTS = 4
MERGE_WINDOW_S = 1.0
# None - ошибка не исчезает сама (спека 5.2)
LIFETIME_S = {"success": 5.0, "info": 5.0, "warning": 8.0, "error": None}
KIND_LABELS = {"success": "Готово", "info": "Сообщение", "warning": "Внимание", "error": "Ошибка"}
KIND_TONES = {"success": "success", "info": "info", "warning": "warning", "error": "error"}


@dataclass
class Toast:
    id: int
    kind: str
    text: str
    details: Optional[str]
    created: float
    last_seen: float
    expires: Optional[float]
    count: int = 1

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def display_text(self) -> str:
        return f"{self.text} (x{self.count})" if self.count > 1 else self.text


class ToastModel:
    def __init__(self, now: Callable[[], float] = time.monotonic, limit: int = MAX_TOASTS):
        self._now = now
        self._limit = limit
        self._ids = itertools.count(1)
        self._items: List[Toast] = []

    @property
    def items(self) -> List[Toast]:
        """Порядок показа: старые первыми."""
        return list(self._items)

    def add(self, kind: str, text: str, details: Optional[str] = None) -> Toast:
        if kind not in LIFETIME_S:
            kind = "info"
        now = self._now()
        for item in self._items:
            if item.kind == kind and item.text == text and now - item.last_seen <= MERGE_WINDOW_S:
                item.count += 1
                item.last_seen = now
                lifetime = LIFETIME_S[kind]
                item.expires = None if lifetime is None else now + lifetime
                return item
        lifetime = LIFETIME_S[kind]
        toast = Toast(next(self._ids), kind, text, details, now, now,
                      None if lifetime is None else now + lifetime)
        self._items.append(toast)
        while len(self._items) > self._limit:
            self._evict()
        return toast

    def _evict(self) -> None:
        """Вытесняется самый старый не ошибочный тост; если все ошибки - самый старый."""
        for item in self._items:
            if item.kind != "error":
                self._items.remove(item)
                return
        self._items.pop(0)

    def dismiss(self, toast_id: int) -> bool:
        for item in self._items:
            if item.id == toast_id:
                self._items.remove(item)
                return True
        return False

    def dismiss_top(self) -> Optional[Toast]:
        """Закрыть самый новый тост (Esc)."""
        return self._items.pop() if self._items else None

    def expire(self) -> List[Toast]:
        """Убрать тосты с истёкшим сроком; вернуть убранные."""
        now = self._now()
        gone = [t for t in self._items if t.expires is not None and t.expires <= now]
        for toast in gone:
            self._items.remove(toast)
        return gone

    def next_expiry_ms(self) -> Optional[int]:
        """Через сколько мс проверять сроки; None - ждать нечего."""
        times = [t.expires for t in self._items if t.expires is not None]
        if not times:
            return None
        return max(50, int((min(times) - self._now()) * 1000) + 20)
