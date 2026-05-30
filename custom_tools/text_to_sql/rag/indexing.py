"""
Индексация sqlrag/*.md файлов в тактическую память для RAGSearcher.

EPIC 8.10: класс перешёл с mixin-режима на сервис (композиция). Получает
зависимости (state, repo_root, embeddings) через ``__init__``.
``IndexingMixin`` оставлен как алиас.
"""
import os
import re
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from memory.manager import memory_manager
from memory.tools import save_memory, get_memory

from ._state import SharedIndexState
from .embedding_utils import EmbeddingUtils

logger = logging.getLogger(__name__)


def _strict_chroma_cleanup_enabled() -> bool:
    """Strict-режим для Chroma cleanup при индексации sqlrag/*.md.

    По умолчанию off (soft): ошибка delete → logger.error, но SQLite-commit не
    откатывается (SQLite — source of truth, Chroma — eventually consistent).
    При `TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP=1` — raise: caller (_index_file_in_memory)
    обязан откатить SQLite-deactivation, чтобы инвариант "SQLite и Chroma согласованы"
    был восстановлен.

    W1-T6: отдельный env для RAG-cleanup (sqlrag/*.md), по аналогии с общим
    `TEXT_TO_SQL_STRICT_CHROMA_CLEANUP` (core/_audit.py:411, schema_memory_sqlite.py:404),
    но независимо — RAG-индексация и schema-memory могут переключаться раздельно.
    """
    return os.getenv("TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP", "0") == "1"


def _mask_chroma_id(chroma_id: Any) -> str:
    """Маскирует tactical_id для лога: показываем только префикс/суффикс.

    Используется в non-strict path `_chroma_safe_delete`, чтобы не лить в
    логи внутренности cache_key (могут содержать part'ы DSN / schema_version).
    """
    s = str(chroma_id)
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-2:]}"


def _chroma_safe_delete(
    collection: Any,
    ids: List[str],
    *,
    strict: bool,
) -> None:
    """Единый helper для Chroma `collection.delete(ids=...)`.

    W8-T3: консолидирует все Chroma-delete в rag/ под один контракт:
      * ``strict=True``  → исключение Chroma пробрасывается как RuntimeError
        (caller должен сделать compensation rollback SQLite — см. W1-T6).
      * ``strict=False`` → ошибка логируется как warning с маскированными id,
        исключение НЕ пробрасывается (eventual consistency, soft path).

    Поведение полностью совместимо с существующими W1-T6 call-site'ами:
      * `_delete_chroma_records` вызывает с ``strict=True`` (caller обрабатывает
        исключение и решает, делать ли compensation).
      * `_cleanup_orphaned_records` вызывает с ``strict`` из env (через
        ``_strict_chroma_cleanup_enabled()``).

    Args:
        collection: Chroma collection (или объект с .delete(ids=...)).
            ``None`` → no-op (collection отсутствует — это штатная ситуация
            при запуске без Chroma).
        ids: список tactical_id для удаления. Пустой → no-op.
        strict: keyword-only. См. описание выше.
    """
    if collection is None or not ids:
        return
    try:
        collection.delete(ids=ids)
    except Exception as e:
        if strict:
            # Сохраняем исходное исключение как __cause__, чтобы caller мог
            # видеть, что именно упало в Chroma.
            raise RuntimeError(
                f"ChromaDB delete failed for {len(ids)} ids "
                f"(strict mode): {e}"
            ) from e
        masked = [_mask_chroma_id(i) for i in ids[:3]]
        suffix = "" if len(ids) <= 3 else f" (+{len(ids) - 3} more)"
        logger.warning(
            "ChromaDB delete failed (non-strict, ids=%s%s): %s",
            masked, suffix, e,
        )


def _escape_like(value: str, escape_char: str = "\\") -> str:
    """Экранирует wildcard-символы в LIKE-паттерне для SQLite.

    Используется вместе с предложением ESCAPE в SQL, чтобы метасимволы
    в имени файла (`%`, `_`, `[`) не превращались в wildcards.
    """
    return (
        value.replace(escape_char, escape_char * 2)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
        .replace("[", escape_char + "[")
    )


def _parse_enable_frontmatter(content: str) -> Optional[bool]:
    """Парсит фронт-маттер на наличие флага `enable`.

    Поддерживаемые форматы:
      * legacy 1-строчный: `enable: true|false` (или `enable: True`) в начале файла.
      * полный YAML фронт-маттер: `---\\n<yaml>\\n---` в начале файла.

    Возвращает:
      * True/False — если флаг enable найден и валиден;
      * None — если флага нет (файл должен быть проигнорирован вызывающей
        стороной точно так же, как при `enable: false`).

    Fail-fast: ValueError на:
      * битый YAML-фронт-маттер;
      * нечитаемое значение enable (не bool/строка true/false).
    """
    if not content:
        return None

    stripped = content.lstrip("﻿")  # BOM
    lines = stripped.splitlines()
    if not lines:
        return None

    first = lines[0].strip()

    # Полный YAML-фронт-маттер: первая непустая строка ровно "---".
    if first == "---":
        # Ищем закрывающую "---" среди следующих строк.
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            raise ValueError(
                "YAML frontmatter is not terminated by a closing '---' delimiter"
            )
        block = "\n".join(lines[1:end_idx])
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML frontmatter: {exc}") from exc
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError(
                f"YAML frontmatter must be a mapping, got {type(data).__name__}"
            )
        if "enable" not in data:
            return None
        raw = data["enable"]
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low == "true":
                return True
            if low == "false":
                return False
        raise ValueError(
            f"Invalid 'enable' value in YAML frontmatter: {raw!r}"
        )

    # Legacy 1-строчный: `enable: true|false`.
    if first.lower().startswith("enable:"):
        rest = first.split(":", 1)[1].strip().lower()
        if rest == "true":
            return True
        if rest == "false":
            return False
        raise ValueError(
            f"Invalid 'enable' value in legacy header: {first!r}"
        )

    return None


class IndexingService:
    """Сервис: индексация sqlrag/*.md и управление их жизненным циклом в памяти.

    EPIC 8.10: получает зависимости через ``__init__`` — больше не наследует
    state/embedding-методы от mixin-родителей.
    """

    def __init__(
        self,
        *,
        state: SharedIndexState,
        repo_root: Path,
        embeddings: EmbeddingUtils,
    ):
        self._state = state
        self._repo_root = repo_root
        self._embeddings = embeddings
        # _host выставляется RAGSearcher после конструктора (см. search.py).
        self._host: Optional[Any] = None

    @property
    def repo_root(self) -> Path:
        """repo_root читается из host (если есть), иначе из self.

        Совместимо с тестами, подменяющими ``searcher.repo_root``.
        """
        host = self._host
        if host is not None and hasattr(host, "repo_root"):
            return host.repo_root
        return self._repo_root

    @repo_root.setter
    def repo_root(self, value: Path) -> None:
        self._repo_root = value

    # --- Прокси для зависимостей (используется методами сервиса) --------------
    def _get_session_id(self) -> str:
        # cross-call через host: monkeypatch на RAGSearcher._get_session_id
        # должен перехватываться (симметрия с _create_embedding_safe и
        # другими host-delegated методами, см. search.py:42-43).
        return (self._host or self._embeddings)._get_session_id()

    def _get_session_lock(self, session_id: str):
        return self._state.get_session_lock(session_id)

    def _hash_key(self, obj: Any) -> str:
        return self._embeddings._hash_key(obj)

    # --- Основные операции ---------------------------------------------------
    def _ensure_sqlrag_files_indexed(self) -> None:
        """Индексация sqlrag/*.md за запуск + инкрементально: читаем файл только при изменении."""
        try:
            session_id = self._get_session_id()
            # 4.5: вся работа с _index_registry и связанная индексация — под per-session RLock.
            with self._get_session_lock(session_id):
                sqlrag_dir = self.repo_root / "sqlrag"
                if not sqlrag_dir.exists():
                    return

                registry = self._state.registry
                state = registry.setdefault(session_id, {"scanned": False, "files": {}})

                # Очистка записей для удалённых файлов (до сканирования — на случай ручного удаления)
                # cross-call через host: monkeypatch на RAGSearcher должен перехватываться.
                (self._host or self)._cleanup_orphaned_records(session_id)

                # Точное совпадение по имени файла: session_id.md.
                # Prefix-glob (session_id*.md) захватывал соседние сессии
                # с общим префиксом (напр. 'prod' матчил 'prod_v2.md') — #low.
                exact_path = sqlrag_dir / f"{session_id}.md"
                current_paths = [exact_path] if exact_path.exists() else []
                current_names: Set[str] = {p.name for p in current_paths}
                known_files: Dict[str, Dict[str, Any]] = state["files"]

                # Определяем удалённые
                removed = set(known_files.keys()) - current_names
                for filename in removed:
                    try:
                        # search.py:42-43 — cross-call идёт через host, чтобы
                        # monkeypatch на инстанс RAGSearcher перехватывал вызов.
                        (self._host or self)._remove_old_file_records(session_id, filename)
                        known_files.pop(filename, None)
                    except Exception as e:
                        logger.warning(f"Failed to remove records for deleted file {filename}: {e}")

                # Если уже сканировали и нет новых файлов и ни у одного файла не поменялся mtime/size — выходим быстро
                changed_candidates: List[Tuple[Path, Tuple[int, int]]] = []
                for p in current_paths:
                    try:
                        st = p.stat()
                        sig = (int(st.st_mtime_ns), int(st.st_size))
                    except Exception:
                        # Если не удалось stat, вынужденно считаем изменённым
                        sig = (-1, -1)
                    prev = known_files.get(p.name, {}).get("sig")
                    if prev != sig:
                        changed_candidates.append((p, sig))

                if state["scanned"] and not changed_candidates:
                    return

                # Обрабатываем только изменённые/новые файлы
                for md_path, sig in changed_candidates:
                    try:
                        content = md_path.read_text(encoding="utf-8", errors="replace")
                        file_hash = self._hash_key({"file": str(md_path), "content": content})

                        # Битый frontmatter одного файла не должен ронять всю
                        # переиндексацию: логируем и пропускаем именно этот файл.
                        try:
                            enable_flag = _parse_enable_frontmatter(content)
                        except ValueError as e:
                            logger.error(
                                f"Skipping {md_path.name}: invalid frontmatter: {e}"
                            )
                            continue
                        if enable_flag is None:
                            # Нет маркера — очищаем любые старые записи и пропускаем
                            (self._host or self)._remove_old_file_records(session_id, md_path.name)
                            known_files[md_path.name] = {"sig": sig, "hash": file_hash, "enabled": False}
                            continue

                        if not enable_flag:
                            logger.info(f"File {md_path.name} has enable: false, removing existing records")
                            (self._host or self)._remove_old_file_records(session_id, md_path.name)
                            known_files[md_path.name] = {"sig": sig, "hash": file_hash, "enabled": False}
                            continue

                        # Если содержимое не изменилось (по хэшу) и записи уже были, пропускаем
                        prev_hash = known_files.get(md_path.name, {}).get("hash")
                        if prev_hash == file_hash and (self._host or self)._is_file_indexed(session_id, file_hash):
                            known_files[md_path.name] = {"sig": sig, "hash": file_hash, "enabled": True}
                            continue

                        # Извлекаем SQL блоки и индексируем
                        sql_snippets: List[str] = []
                        for block in re.findall(r"```sql\s+([\s\S]*?)```", content, flags=re.IGNORECASE):
                            snippet = block.strip()
                            if not snippet.endswith(";"):
                                snippet += ";"
                            sql_snippets.append(snippet)

                        if sql_snippets:
                            # HIGH #3: оборачиваем в try/except, чтобы при ошибке
                            # save_memory НЕ помечать файл проиндексированным
                            # (избегаем registry-vs-memory drift).
                            try:
                                (self._host or self)._index_file_in_memory(
                                    session_id, md_path.name, sql_snippets, file_hash
                                )
                            except Exception as e:
                                logger.error(
                                    "Failed to index %s — leaving registry untouched: %s",
                                    md_path.name, e,
                                )
                                # known_files НЕ обновляем — следующий проход попробует снова.
                                continue
                            known_files[md_path.name] = {"sig": sig, "hash": file_hash, "enabled": True}
                        else:
                            # Нет SQL-блоков — очищаем возможные старые записи
                            (self._host or self)._remove_old_file_records(session_id, md_path.name)
                            known_files[md_path.name] = {"sig": sig, "hash": file_hash, "enabled": True}

                    except (OSError, IOError) as e:
                        # 4.7: ловим только file IO; ValueError от парсинга frontmatter
                        # и прочие ошибки пробрасываются наверх (fail-fast).
                        logger.warning(f"Failed to process {md_path} (file IO error): {e}")

                state["scanned"] = True

        except (OSError, IOError) as e:
            # 4.7: file IO здесь — норма (отсутствует sqlrag_dir и т.п.).
            # ValueError от парсинга frontmatter НЕ глушим — пробрасываем выше.
            logger.warning(f"Failed to ensure sqlrag files indexed (file IO error): {e}")

    def _is_file_indexed(self, session_id: str, file_hash: str) -> bool:
        """Проверяет, проиндексирован ли файл с данным хэшем.

        Идемпотентный check. `return False` означает "записи с таким хэшем
        не найдено" — это НЕ silent fallback, а штатное "нет данных".
        Любые ошибки memory layer пробрасываются наверх (fail-fast).
        """
        # 4.5: под per-session lock — чтение _index_registry/get_memory без гонок.
        with self._get_session_lock(session_id):
            # Ищем записи с таким file_hash в метаданных.
            # Late lookup через фасад rag, чтобы поддерживать monkeypatch на rag.get_memory.
            from custom_tools.text_to_sql import rag as _facade
            cached = _facade.get_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                cache_kind="sqlrag_example"  # #17: соответствует cache_kind при индексации
            )

            for item in cached if isinstance(cached, list) else []:
                data = item.get("data", {})
                if isinstance(data, dict) and data.get("file_hash") == file_hash:
                    return True
            return False

    def _index_file_in_memory(self, session_id: str, filename: str, sql_snippets: List[str], file_hash: str) -> None:
        """Индексирует SQL-сниппеты из файла в тактическую память.

        W1-T6 (атомарность):
          * deactivation старых записей (UPDATE valid_to) выполняется на ВЫДЕЛЕННОМ
            connection (``conn_dx``) БЕЗ commit'а до окончания save_memory-цикла.
          * save_memory открывает СВОЙ connection (см. memory/tools.py:294) и
            коммитит каждую INSERT отдельно — это нельзя объединить в одну SQLite
            tx без рефакторинга save_memory contract. Поэтому используется паттерн
            "compensation snapshot":
              - На любой сбой save_memory: (a) собираем уже-вставленные new_steps
                и UPDATE valid_to=now (новый отдельный conn — compensation INSERT'ов),
                (b) ROLLBACK conn_dx — старые записи остаются ``valid_to IS NULL``.
              - На success: COMMIT conn_dx — старые записи деактивированы только
                после полного цикла.
          * Chroma cleanup для старых записей вынесен ВНУТРЬ этого метода
            (см. ниже), а не внутрь _remove_old_file_records, чтобы повторно
            использовать compensation на ошибке Chroma.

        HIGH #3 (сохраняется): на любую ошибку — НЕ обновляем registry в caller.
        """
        from custom_tools.text_to_sql import rag as _facade

        # 4.5: per-session RLock — реентерабельность для случая, когда уже под локом из ensure().
        with self._get_session_lock(session_id):
            current_time = datetime.now().isoformat()

            # 1) Открываем выделенный conn (conn_dx). Deactivate старые записи,
            #    собираем chroma_ids — но НЕ commit'им до конца save_memory.
            conn_dx = _facade.memory_manager.db_handler._get_connection()
            inserted_steps: List[int] = []
            tactical_ids_old: List[str] = []
            try:
                deactivated_old = self._deactivate_file_records(
                    conn=conn_dx,
                    session_id=session_id,
                    filename=filename,
                    current_time=current_time,
                )

                # Соберём chroma-ids старых записей (для последующего delete на success).
                tactical_ids_old = self._collect_chroma_ids_for_file(
                    session_id=session_id, filename=filename
                )

                # 2) Индексируем новые сниппеты через save_memory.
                #    save_memory открывает свой conn и коммитит каждую запись —
                #    собираем steps, чтобы на failure откатить их вручную.
                for i, snippet in enumerate(sql_snippets):
                    # 4.1: similarity_score не приклеиваем — его считает реранкер на запросе.
                    step = _facade.save_memory(
                        session_id=session_id,
                        agent_name="Schema-RAG-Agent",
                        data={
                            "cache_source": "vector_db_search",
                            "cache_kind": "sqlrag_example",  # #17: отдельный namespace от кэша поиска
                            "source": "sqlrag_file",
                            "filename": filename,
                            "file_hash": file_hash,
                            "snippet_index": i,
                            "sql_example": snippet,
                        }
                    )
                    # save_memory возвращает -1 при internal error (см. memory/tools.py:423).
                    # Не глушим — поднимаем явный RuntimeError, чтобы compensation сработала.
                    if not isinstance(step, int) or step < 0:
                        raise RuntimeError(
                            f"save_memory failed for snippet #{i} of {filename} "
                            f"(returned {step!r}); aborting indexing transaction."
                        )
                    inserted_steps.append(step)

                # 3) Chroma cleanup старых записей. На failure в strict-mode —
                #    rollback всего батча (см. except ниже). В non-strict —
                #    только logger.error, SQLite tx коммитим (eventual consistency).
                strict_chroma = _strict_chroma_cleanup_enabled()
                chroma_cleanup_err: Optional[Exception] = None
                try:
                    self._delete_chroma_records(
                        session_id=session_id,
                        filename=filename,
                        tactical_ids=tactical_ids_old,
                    )
                except Exception as e:
                    if strict_chroma:
                        chroma_cleanup_err = e
                    else:
                        logger.error(
                            "Failed to delete Chroma records for %s during reindex: %s. "
                            "SQLite will be committed; possible inconsistency (orphan "
                            "tactical_ids=%s). Set TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP=1 "
                            "to fail-fast.",
                            filename, e, tactical_ids_old,
                        )

                if chroma_cleanup_err is not None:
                    # strict-режим: поднимаем — попадём в общий except,
                    # сработает compensation для inserted_steps + rollback conn_dx.
                    raise chroma_cleanup_err

                # 4) Всё ок — коммитим deactivation старых записей.
                conn_dx.commit()
                logger.info(
                    "Indexed %d SQL snippets from %s (deactivated %d old SQLite rows)",
                    len(sql_snippets), filename, deactivated_old,
                )

            except Exception:
                # Compensation:
                #   (a) ROLLBACK conn_dx — старые записи остаются valid_to IS NULL.
                #   (b) Для уже-вставленных через save_memory — deactivate их
                #       на отдельном connection (save_memory уже сделал commit
                #       для каждой записи). Это удаляет частично-проиндексированные
                #       новые записи, чтобы registry-vs-memory drift не возник.
                try:
                    conn_dx.rollback()
                except Exception as rb_err:
                    logger.critical(
                        "Failed to rollback conn_dx for %s after indexing error: %s",
                        filename, rb_err,
                    )

                if inserted_steps:
                    try:
                        self._compensate_inserted_steps(
                            session_id=session_id,
                            inserted_steps=inserted_steps,
                            current_time=datetime.now().isoformat(),
                        )
                    except Exception as comp_err:
                        logger.critical(
                            "Compensation failed for %s: inserted_steps=%s, err=%s. "
                            "Memory state inconsistent — manual cleanup required.",
                            filename, inserted_steps, comp_err,
                        )
                raise
            finally:
                try:
                    conn_dx.close()
                except Exception as close_err:
                    logger.warning(
                        "Failed to close conn_dx for %s: %s", filename, close_err
                    )

    def _remove_old_file_records(self, session_id: str, filename: str) -> None:
        """Удаляет старые записи из файла.

        Используется вне индексации (removed/disabled файлы, no-SQL файлы).
        Для случая reindex'а старые записи деактивируются ВНУТРИ
        ``_index_file_in_memory`` под одной транзакцией (W1-T6).

        Chroma cleanup: strict-mode env-флаг ``TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP``
        — при `=1` raise при сбое Chroma delete + compensation rollback
        SQLite-deactivation (по аналогии с core/_audit.py:411).
        В soft-режиме — logger.error с явной пометкой возможной inconsistency.
        """
        # 4.5: per-session RLock защищает консистентность относительно индексации.
        with self._get_session_lock(session_id):
            try:
                from custom_tools.text_to_sql import rag as _facade

                conn = _facade.memory_manager.db_handler._get_connection()
                current_time = datetime.now().isoformat()
                try:
                    deactivated = self._deactivate_file_records(
                        conn=conn,
                        session_id=session_id,
                        filename=filename,
                        current_time=current_time,
                    )

                    # Собираем chroma_ids ДО commit — это не зависит от состояния SQLite.
                    tactical_ids = self._collect_chroma_ids_for_file(
                        session_id=session_id, filename=filename,
                    )

                    conn.commit()
                    if deactivated:
                        logger.info(
                            f"Deactivated {deactivated} old records from {filename}"
                        )

                    # Chroma cleanup со strict-mode.
                    strict_chroma = _strict_chroma_cleanup_enabled()
                    try:
                        self._delete_chroma_records(
                            session_id=session_id,
                            filename=filename,
                            tactical_ids=tactical_ids,
                        )
                    except Exception as e:
                        if strict_chroma:
                            # Compensation: откатываем SQLite-deactivation.
                            try:
                                cursor = conn.cursor()
                                cursor.execute(
                                    """
                                    UPDATE agent_memory
                                    SET valid_to = NULL, updated_at = ?
                                    WHERE session_id = ? AND agent_name = ?
                                      AND valid_to = ?
                                    """,
                                    (current_time, session_id, "Schema-RAG-Agent", current_time),
                                )
                                conn.commit()
                            except Exception as comp_err:
                                logger.critical(
                                    "Chroma cleanup failed AND compensation rollback "
                                    "failed for %s: chroma_err=%s, comp_err=%s",
                                    filename, e, comp_err,
                                )
                            raise
                        logger.error(
                            "Failed to delete old ChromaDB records for %s: %s. "
                            "SQLite committed; possible inconsistency (orphan "
                            "tactical_ids=%s). Set TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP=1 "
                            "to fail-fast.",
                            filename, e, tactical_ids,
                        )

                finally:
                    conn.close()

            except (OSError, sqlite3.DatabaseError) as e:
                # HIGH #4: сужено до IO/DB-ошибок (по AGENTS.md silent fallback запрещён).
                # Остальные исключения (TypeError, AttributeError, ValueError) пробрасываем наружу.
                logger.warning(f"Failed to remove old records for {filename}: {e}")

    # ---------------- Внутренние helper'ы W1-T6 ----------------
    def _deactivate_file_records(
        self,
        *,
        conn,
        session_id: str,
        filename: str,
        current_time: str,
    ) -> int:
        """Помечает все активные записи Schema-RAG-Agent для данного файла как
        ``valid_to=current_time``. НЕ делает commit — это контролирует caller.

        Возвращает количество затронутых записей.
        """
        cursor = conn.cursor()

        # 4.6: компактный JSON-паттерн c json.dumps + escape_char `~`
        # (не трогаем backslashes/кавычки, только LIKE-метасимволы).
        filename_value_json = json.dumps(filename, ensure_ascii=False)
        like_pattern = (
            f'%"filename":{_escape_like(filename_value_json, escape_char="~")}%'
        )

        cursor.execute(
            """
            SELECT step FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
            AND data LIKE ? ESCAPE '~'
            AND data NOT LIKE '%"cache_kind":"schema_table"%'
            """,
            (session_id, "Schema-RAG-Agent", like_pattern),
        )
        steps_to_deactivate = [row[0] for row in cursor.fetchall()]

        if not steps_to_deactivate:
            return 0

        for step in steps_to_deactivate:
            cursor.execute(
                """
                UPDATE agent_memory
                SET valid_to = ?, updated_at = ?
                WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                """,
                (current_time, current_time, session_id, "Schema-RAG-Agent", step),
            )

        return len(steps_to_deactivate)

    def _collect_chroma_ids_for_file(
        self, *, session_id: str, filename: str,
    ) -> List[str]:
        """Возвращает tactical_id'ы Chroma-документов для пары (session_id, filename).

        Используется ДО фактического delete, чтобы caller мог решить, нужна ли
        compensation. На ошибке метаданных — возвращает пустой список и логирует
        warning (collection.get не является source of truth).
        """
        try:
            from custom_tools.text_to_sql import rag as _facade
            tactical = _facade.memory_manager.db_handler.tactical_collection
            if not tactical:
                return []
            results = tactical.get(where={"$and": [
                {"session_id": {"$eq": session_id}},
                {"filename": {"$eq": filename}},
            ]})
            if results and results.get("ids"):
                return list(results["ids"])
            return []
        except Exception as e:
            logger.warning(
                "Failed to collect Chroma ids for %s/%s: %s",
                session_id, filename, e,
            )
            return []

    def _delete_chroma_records(
        self,
        *,
        session_id: str,
        filename: str,
        tactical_ids: Optional[List[str]] = None,
    ) -> None:
        """Удаляет Chroma-документы для пары (session_id, filename).

        Если ``tactical_ids`` передан и непуст — удаляет по ids (быстрее, идемпотентнее).
        Иначе — заново ищет по metadata. Любая ошибка Chroma пробрасывается наверх
        — caller решает, как реагировать (strict raise vs soft log).

        W8-T3: фактический delete делегирован в ``_chroma_safe_delete(strict=True)``,
        чтобы все Chroma-delete в rag/ шли через единый контракт. Поведение
        сохранено: caller (``_index_file_in_memory`` / ``_remove_old_file_records``)
        видит исключение и принимает решение о compensation.
        """
        from custom_tools.text_to_sql import rag as _facade
        tactical = _facade.memory_manager.db_handler.tactical_collection
        if not tactical:
            return

        ids_to_delete: List[str]
        if tactical_ids:
            ids_to_delete = list(tactical_ids)
        else:
            results = tactical.get(where={"$and": [
                {"session_id": {"$eq": session_id}},
                {"filename": {"$eq": filename}},
            ]})
            ids_to_delete = list(results.get("ids") or []) if results else []

        if ids_to_delete:
            _chroma_safe_delete(tactical, ids_to_delete, strict=True)
            logger.info(
                "Deleted %d ChromaDB records for %s",
                len(ids_to_delete), filename,
            )

    def _compensate_inserted_steps(
        self,
        *,
        session_id: str,
        inserted_steps: List[int],
        current_time: str,
    ) -> None:
        """Деактивирует уже-вставленные save_memory шаги для compensation rollback.

        save_memory открывает свой conn и коммитит каждую INSERT отдельно
        (см. memory/tools.py:317). Поэтому при сбое в середине индексации
        нельзя «откатить» их через rollback нашего conn_dx — приходится явно
        UPDATE valid_to для каждого step на отдельном connection.
        """
        if not inserted_steps:
            return
        from custom_tools.text_to_sql import rag as _facade
        conn = _facade.memory_manager.db_handler._get_connection()
        try:
            cursor = conn.cursor()
            for step in inserted_steps:
                cursor.execute(
                    """
                    UPDATE agent_memory
                    SET valid_to = ?, updated_at = ?
                    WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                    """,
                    (current_time, current_time, session_id, "Schema-RAG-Agent", step),
                )
            conn.commit()
            logger.warning(
                "Compensation: deactivated %d partially-inserted records "
                "(steps=%s) after indexing failure.",
                len(inserted_steps), inserted_steps,
            )
        finally:
            try:
                conn.close()
            except Exception as close_err:
                logger.warning(
                    "Failed to close compensation conn: %s", close_err,
                )

    def _cleanup_orphaned_records(self, session_id: str) -> None:
        """Очищает записи в памяти для файлов, которые больше не существуют."""
        # 4.5: per-session RLock.
        with self._get_session_lock(session_id):
            try:
                from datetime import datetime

                # Получаем все записи с source="sqlrag_file" для данной сессии.
                # Late lookup через фасад rag для поддержки monkeypatch на rag.memory_manager.
                from custom_tools.text_to_sql import rag as _facade
                conn = _facade.memory_manager.db_handler._get_connection()
                try:
                    cursor = conn.cursor()

                    # Находим все активные записи sqlrag_file для этой сессии
                    cursor.execute(
                        """
                        SELECT step, data FROM agent_memory
                        WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
                        AND data LIKE '%sqlrag_file%'
                        """,
                        (session_id, "Schema-RAG-Agent")
                    )

                    orphaned_steps = []
                    orphaned_filenames = set()

                    for row in cursor.fetchall():
                        step, data_text = row
                        try:
                            data_obj = json.loads(data_text or "{}")
                            filename = data_obj.get("filename")

                            if filename and isinstance(filename, str):
                                # Проверяем, существует ли файл
                                file_path = self.repo_root / "sqlrag" / filename
                                if not file_path.exists():
                                    orphaned_steps.append(step)
                                    orphaned_filenames.add(filename)
                        except (json.JSONDecodeError, TypeError, KeyError) as e:
                            logger.debug(
                                "Skipping orphan-check for step %s: %s", step, e
                            )
                            continue

                    # Деактивируем осиротевшие записи
                    if orphaned_steps:
                        current_time = datetime.now().isoformat()

                        for step in orphaned_steps:
                            cursor.execute(
                                """
                                UPDATE agent_memory
                                SET valid_to = ?, updated_at = ?
                                WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                                """,
                                (current_time, current_time, session_id, "Schema-RAG-Agent", step)
                            )

                        conn.commit()
                        logger.info(f"Cleaned up {len(orphaned_steps)} orphaned records for {len(orphaned_filenames)} deleted files: {', '.join(orphaned_filenames)}")

                    # Также очищаем из ChromaDB.
                    # W8-T3: используем единый helper `_chroma_safe_delete`.
                    # strict читаем из env (`TEXT_TO_SQL_RAG_STRICT_CHROMA_CLEANUP`)
                    # — same env, что и в `_remove_old_file_records`. Strict-режим
                    # пробрасывает RuntimeError наружу из delete, и оборачивающий
                    # `except (OSError, sqlite3.DatabaseError)` его НЕ ловит —
                    # это намеренно (fail-fast), см. AGENTS.md.
                    if orphaned_filenames:
                        tactical = _facade.memory_manager.db_handler.tactical_collection
                        if tactical:
                            strict_chroma = _strict_chroma_cleanup_enabled()
                            for filename in orphaned_filenames:
                                # Удаляем записи по метаданным filename
                                results = tactical.get(where={"$and": [
                                    {"session_id": {"$eq": session_id}},
                                    {"filename": {"$eq": filename}}
                                ]})

                                if results and results.get("ids"):
                                    ids_to_delete = list(results["ids"])
                                    if ids_to_delete:
                                        _chroma_safe_delete(
                                            tactical, ids_to_delete, strict=strict_chroma,
                                        )
                                        logger.info(
                                            f"Deleted {len(ids_to_delete)} orphaned ChromaDB records for {filename}"
                                        )

                finally:
                    conn.close()

            except (OSError, sqlite3.DatabaseError) as e:
                # HIGH #4: сужено до IO/DB-ошибок (по AGENTS.md silent fallback запрещён).
                # Остальные исключения пробрасываем наверх.
                logger.warning(f"Failed to cleanup orphaned records: {e}")

    def _load_sqlrag_files(self, session_id: str) -> List[str]:
        """Загружает SQL сниппеты из sqlrag/*.md файлов."""
        snippets: List[str] = []

        try:
            sqlrag_dir = self.repo_root / "sqlrag"
            # #low: read-путь не должен создавать директорию (side-effect).
            # Отсутствие директории — штатная ситуация (пустой результат).
            if not sqlrag_dir.exists():
                return []

            # Ищем файл по session_id (санитизированный DSN)
            md_path = sqlrag_dir / f"{session_id}.md"
            if not md_path.exists():
                return []

            text = md_path.read_text(encoding="utf-8", errors="replace")

            # 4.7: единая логика парсинга enable (legacy + YAML фронт-маттер).
            # ValueError от _parse_enable_frontmatter — fail-fast, наружу.
            enable_flag = _parse_enable_frontmatter(text)
            if not enable_flag:
                return []

            # Извлекаем SQL блоки
            for block in re.findall(r"```sql\s+([\s\S]*?)```", text, flags=re.IGNORECASE):
                snippet = block.strip()
                if not snippet.endswith(";"):
                    snippet += ";"
                snippets.append(snippet)

            # Примечание: индексация в память выполняется через _ensure_sqlrag_files_indexed()

        except (OSError, IOError) as e:
            # 4.7: file IO — допустимо логировать и вернуть пусто;
            # ValueError от парсинга frontmatter НЕ глушим.
            logger.warning(f"Failed to load sqlrag files (file IO error): {e}")

        return snippets


# EPIC 8.10: алиас для обратной совместимости.
IndexingMixin = IndexingService
