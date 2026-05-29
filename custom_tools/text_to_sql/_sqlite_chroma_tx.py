"""
Helper для координации записей SQLite + ChromaDB в одной логической транзакции
(W9-A15).

Контекст:
  * SQLite — source of truth, ChromaDB — eventually consistent.
  * Исторические места (rag/indexing.py @ W1-T6, core/_audit.py,
    schema_memory_sqlite.py) уже имеют свои compensation pattern'ы. Их
    НЕ переписываем — у них специфическая семантика (reverse-UPDATE,
    snapshot+rollback по valid_to).
  * Этот helper нужен для НОВЫХ мест, где нужна простая пара
    «SQLite INSERT/UPDATE + Chroma upsert/delete». В отличие от ad-hoc
    кода он:
      1) гарантирует SQLite-rollback при любом исключении внутри блока;
      2) в strict-режиме (env ``TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK=1``)
         вызывает пользовательский compensation-callback для Chroma
         (best-effort обратное действие — например delete для upsert);
      3) в non-strict (default) — логирует Chroma-ошибку, не ломает
         выполнение caller'а, оставляет SQLite в исходном состоянии
         (rollback всё равно сделан).

Контракт ошибок (verbatim, no silent fallback):
  * Любое исключение внутри ``with`` блока → SQLite rollback.
  * Если исключение возникло в ``chroma_op`` (а SQLite уже выполнен) и
    strict=True — pythonic re-raise после попытки compensation.
  * Если compensation сама бросает — логируется critical, оригинальное
    исключение всё равно пробрасывается.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, List, Optional

logger = logging.getLogger(__name__)


def _strict_rollback_enabled() -> bool:
    """Строгий режим compensation: env ``TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK``."""
    return os.getenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", "0") == "1"


@dataclass
class SqliteChromaTx:
    """Контекст транзакции SQLite + Chroma.

    Поля для caller'а:
      ``sqlite_conn`` — открытое соединение SQLite (caller'ом, не helper'ом).
      ``chroma_collection`` — Chroma collection (или ``None`` — Chroma-этап
          пропускается, поведение деградирует до простого SQLite-блока).

    Внутри ``with`` блока caller сам выполняет ``cursor.execute(...)`` /
    ``chroma_collection.upsert(...)`` — helper не пытается унифицировать
    эти API. Он обеспечивает только границу транзакции.

    ``register_chroma_compensation(callback)`` — caller регистрирует
    функцию, которую helper вызовет в strict-режиме, если после Chroma-
    операции произошло исключение. Типичный пример: caller сделал
    ``collection.upsert(ids=...)``, регистрирует
    ``lambda: collection.delete(ids=...)``.

    Контракт состояний после exit:
      * нет исключений → SQLite ``commit()``, никаких действий на Chroma
        (caller сам уже сделал нужное);
      * исключение → SQLite ``rollback()``;
      * + strict + есть зарегистрированные compensation → они вызываются
        в порядке регистрации (best-effort).
    """

    sqlite_conn: Any
    chroma_collection: Any
    strict: bool = False
    _compensations: List[Callable[[], None]] = field(default_factory=list)
    _committed: bool = False

    def register_chroma_compensation(self, callback: Callable[[], None]) -> None:
        """Зарегистрировать обратное действие для Chroma.

        Вызывается helper'ом в strict-режиме при exception, ПОСЛЕ
        SQLite-rollback'а. Caller отвечает за идемпотентность callback'а.
        """
        if not callable(callback):
            raise TypeError(
                "register_chroma_compensation expects a callable, got "
                f"{type(callback).__name__}"
            )
        self._compensations.append(callback)


@contextmanager
def sqlite_chroma_transaction(
    sqlite_conn: Any,
    chroma_collection: Any,
    *,
    strict: Optional[bool] = None,
) -> Iterator[SqliteChromaTx]:
    """Контекст-менеджер для пары SQLite + Chroma операций.

    Параметры:
      ``sqlite_conn`` — открытое соединение SQLite (caller отвечает за
          ``close()`` после выхода из контекста).
      ``chroma_collection`` — Chroma collection или ``None``. ``None``
          означает, что Chroma недоступна — caller внутри блока сам решает,
          пропускать ли Chroma-этап.
      ``strict`` — переопределение env ``TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK``.
          ``None`` (default) — читать env; ``True``/``False`` — явный override.

    Использование:

        with sqlite_chroma_transaction(conn, coll) as tx:
            tx.sqlite_conn.execute("INSERT ...")
            ids = ["a", "b"]
            coll.upsert(ids=ids, documents=[...])
            tx.register_chroma_compensation(lambda: coll.delete(ids=ids))

        # На выходе: SQLite commit. Никаких действий на Chroma.

    При исключении внутри блока:
        * SQLite rollback (всегда);
        * strict=True → выполнение всех зарегистрированных compensations.
    """
    effective_strict = strict if strict is not None else _strict_rollback_enabled()

    tx = SqliteChromaTx(
        sqlite_conn=sqlite_conn,
        chroma_collection=chroma_collection,
        strict=effective_strict,
    )

    try:
        yield tx
    except BaseException as exc:
        # SQLite rollback всегда — даже если соединение в плохом состоянии
        # (rollback на закрытом conn даёт ProgrammingError; ловим и логируем).
        try:
            sqlite_conn.rollback()
        except Exception as rb_err:
            logger.error(
                "SqliteChromaTx: SQLite rollback failed (original error: %s): %s",
                exc, rb_err,
            )

        # Compensation для Chroma — только в strict-режиме.
        if effective_strict and tx._compensations:
            for idx, comp in enumerate(tx._compensations):
                try:
                    comp()
                except Exception as comp_err:
                    logger.critical(
                        "SqliteChromaTx: chroma compensation #%d failed "
                        "(original error: %s): %s",
                        idx, exc, comp_err,
                    )
        elif tx._compensations and not effective_strict:
            logger.warning(
                "SqliteChromaTx: %d Chroma compensation(s) registered but "
                "strict=False — skipping. Original error: %s. Set "
                "TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK=1 to enable.",
                len(tx._compensations), exc,
            )
        raise
    else:
        # Happy path: SQLite commit. Chroma уже отработала внутри блока,
        # ничего дополнительно не делаем.
        sqlite_conn.commit()
        tx._committed = True
