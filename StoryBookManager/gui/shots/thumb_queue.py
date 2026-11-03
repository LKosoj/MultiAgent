"""Очередь миниатюр и кэш (без tkinter): дедупликация, отмена по поколению, порядок запросов."""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Hashable, Iterable, List, Optional, Set, Tuple


class LruCache:
    """Кэш с вытеснением давно не использованных записей."""

    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self._items: "OrderedDict[Hashable, object]" = OrderedDict()

    def get(self, key):
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key, value) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def discard(self, key) -> None:
        self._items.pop(key, None)

    def discard_where(self, predicate) -> int:
        doomed = [k for k in self._items if predicate(k)]
        for key in doomed:
            del self._items[key]
        return len(doomed)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, key) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def keys(self) -> List[Hashable]:
        return list(self._items)


@dataclass(frozen=True)
class ThumbRequest:
    key: str                              # ключ шота, которому нужна картинка
    rel: str                              # путь внутри проекта
    size: int                             # 128, 256 или 512 (размеры studio.thumbs)
    rev: Optional[str] = None             # thumb_rev: при смене запись заменяется
    priority: int = 100                   # меньше - раньше
    fit: Optional[Tuple[int, int]] = None  # уменьшить до рамки (ширина, высота) в воркере

    @property
    def cache_key(self) -> Tuple:
        return (self.rel, self.rev, self.size, self.fit)


def priority_for(index: int, center: int, selected: bool) -> int:
    """Выбранный шот первым, затем ближайшие к центру экрана."""
    return -1 if selected else abs(index - center)


class ThumbScheduler:
    """Что ещё нужно загрузить. Потокобезопасна: воркеры спрашивают, нужен ли запрос, перед стартом."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Dict[Tuple, ThumbRequest] = {}
        self._wanted: Set[Tuple] = set()
        self._in_flight: Set[Tuple] = set()
        self.generation = 0

    def set_wanted(self, requests: Iterable[ThumbRequest]) -> None:
        """Новый набор нужных картинок: лишние ожидающие отменяются, идущие не дублируются."""
        with self._lock:
            self.generation += 1
            self._pending = {}
            self._wanted = set()
            for request in requests:
                cache_key = request.cache_key
                self._wanted.add(cache_key)
                if cache_key in self._in_flight:
                    continue
                known = self._pending.get(cache_key)
                if known is None or request.priority < known.priority:
                    self._pending[cache_key] = request

    def pop(self, limit: int) -> List[ThumbRequest]:
        """Следующие запросы по приоритету; они помечаются идущими."""
        with self._lock:
            ordered = sorted(self._pending.values(), key=lambda r: (r.priority, r.rel))[:max(0, limit)]
            for request in ordered:
                del self._pending[request.cache_key]
                self._in_flight.add(request.cache_key)
            return ordered

    def still_wanted(self, request: ThumbRequest) -> bool:
        """Воркер зовёт перед стартом: запрос отменён, если набор поменялся."""
        with self._lock:
            return request.cache_key in self._wanted

    def done(self, request: ThumbRequest) -> None:
        with self._lock:
            self._in_flight.discard(request.cache_key)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def clear(self) -> None:
        with self._lock:
            self.generation += 1
            self._pending.clear()
            self._wanted.clear()
