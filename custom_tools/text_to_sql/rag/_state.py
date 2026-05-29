"""
EPIC 8.10: process-global shared state для RAG-индексации.

Раньше реестр индексации (``_index_registry``) и пер-сессионные локи
(``_per_session_locks``) жили class-level атрибутами на ``RAGSearcher`` —
это сохраняло их общими между всеми экземплярами (см. 4.5).

После перехода RAGSearcher на композицию (вместо multi-inheritance из
mixins) общее состояние вынесено в отдельный класс ``SharedIndexState``.
Чтобы инвариант "registry/locks общие на ВЕСЬ процесс" сохранялся,
поля объявлены ClassVar — единственный set на сам класс ``SharedIndexState``
шарится между всеми экземплярами этого класса.
"""
from __future__ import annotations

import threading
from typing import Any, ClassVar, Dict


class SharedIndexState:
    """Process-global реестр индексации + пер-сессионные локи.

    Контракт:
      * ``_index_registry`` хранит состояние сессий: ``session_id -> {"scanned": bool,
        "files": {filename: {"sig": (mtime_ns, size), "hash": str}}}``.
      * ``_per_session_locks`` даёт RLock на сессию — конкурирующие вызовы
        индексации/очистки для одной сессии идут последовательно
        независимо от того, через какой RAGSearcher они пришли.
      * ``_registry_lock`` защищает инициализацию записи в ``_per_session_locks``.

    Поля — ClassVar: общие на класс, шарятся между экземплярами. Это
    сохраняет инвариант 4.5: один лок на (session_id, process), даже если
    создано N экземпляров RAGSearcher.
    """

    # ClassVar: общие на весь класс, не на экземпляр. Это сохраняет
    # process-global семантику, которая была у class-level атрибутов
    # RAGSearcher до композиции.
    _index_registry: ClassVar[Dict[str, Dict[str, Any]]] = {}
    _per_session_locks: ClassVar[Dict[str, threading.RLock]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    @property
    def registry(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает class-level реестр индексации (process-global)."""
        return type(self)._index_registry

    def get_session_lock(self, session_id: str) -> threading.RLock:
        """Возвращает class-level RLock для конкретного session_id.

        4.5: лок хранится в class-level dict, поэтому несколько экземпляров
        SharedIndexState (и RAGSearcher) для одной session_id получат
        один и тот же RLock.
        """
        cls = type(self)
        with cls._registry_lock:
            return cls._per_session_locks.setdefault(session_id, threading.RLock())
