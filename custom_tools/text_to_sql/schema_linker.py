"""
Schema Linker - координирующий класс для связывания сущностей со схемой БД
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any, Optional, Union
from .validators import SchemaLimiter
from .schema_loader import SchemaLoader
from .schema_enricher import SchemaEnricher
from .schema_memory import SchemaMemoryManager, SchemaCacheManager, SchemaCacheCorrupted
from .schema_linking import SchemaLinkingCore
from .schema_linking.resolution import (
    _column_exists_in_table,
    _get_column_meta,
    _resolve_column_name,
    _resolve_table_name,
    _table_exists_in_schema,
)
from .schema_filtering import SchemaContextBuilder
from .utils import dsn_to_sanitized_name, get_runtime_context_dsn, get_schema_version, mask_dsn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaLinkerDeps:
    """Явный набор зависимостей для :class:`SchemaLinker` (EPIC 8.4).

    Убирает «magic facade access»: вместо того чтобы конструктор
    инстанцировал все коллабораторы сам, тесты и production-код могут
    передать готовые объекты (моки, кастомные реализации, общие
    instance-ы для shared cache и пр.). Backward-compat factory —
    :meth:`SchemaLinker.with_defaults`.
    """
    schema_limiter: SchemaLimiter
    loader: SchemaLoader
    enricher: SchemaEnricher
    memory_manager: SchemaMemoryManager
    cache_manager: SchemaCacheManager
    linking_core: SchemaLinkingCore
    context_builder: SchemaContextBuilder


class SchemaLinker:
    """Связывает сущности из NLU со схемой базы данных."""

    def __init__(self, schema_limiter_or_deps: Union[SchemaLimiter, SchemaLinkerDeps]):
        """Принимает либо :class:`SchemaLimiter` (legacy), либо
        :class:`SchemaLinkerDeps` (EPIC 8.4 DI).

        Legacy-форма ``SchemaLinker(SchemaLimiter())`` сохранена для
        обратной совместимости — внутренне делегирует в
        :meth:`with_defaults`. Новый код должен использовать
        ``SchemaLinker.with_defaults(limiter)`` явно или собирать
        :class:`SchemaLinkerDeps` с конкретными зависимостями.
        """
        self.repo_root = Path(__file__).resolve().parents[2]

        if isinstance(schema_limiter_or_deps, SchemaLinkerDeps):
            deps = schema_limiter_or_deps
        else:
            deps = _build_default_deps(schema_limiter_or_deps, self.repo_root)

        self._deps = deps
        # Public attributes preserved (tests poke them directly):
        self.schema_limiter = deps.schema_limiter
        self.loader = deps.loader
        self.enricher = deps.enricher
        self.memory_manager = deps.memory_manager
        self.cache_manager = deps.cache_manager
        self.linking_core = deps.linking_core
        self.context_builder = deps.context_builder

    @classmethod
    def with_defaults(cls, schema_limiter: SchemaLimiter) -> "SchemaLinker":
        """Factory с дефолтными зависимостями (EPIC 8.4).

        Точная замена для legacy-вызова ``SchemaLinker(limiter)`` —
        строит :class:`SchemaLinkerDeps` со стандартными loader / enricher /
        memory_manager / cache_manager / linking_core / context_builder.
        """
        repo_root = Path(__file__).resolve().parents[2]
        deps = _build_default_deps(schema_limiter, repo_root)
        return cls(deps)
    
    def link_entities_to_schema(
        self,
        entities: Dict[str, Any],
        schema_info: Dict[str, Any],
        dsn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Основная функция связывания сущностей со схемой."""
        logger.info("Linking entities to database schema")
        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        
        # Сначала убеждаемся, что схема готова
        self._ensure_initialized(dsn=effective_dsn)
        
        # Получаем схему БД
        try:
            db_schema = self._get_database_schema(schema_info, dsn=effective_dsn)
            if not db_schema:
                return {
                    "error": "Failed to load database schema",
                    "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
                    "joins": [],
                    "join_success": False,
                    "unlinked_entities": list(entities.keys()) if isinstance(entities, dict) else [],
                    "schema_info": {}
                }
        except Exception as e:
            safe_error = mask_dsn(str(e))
            logger.error("Schema loading failed: %s", safe_error)
            return {
                "error": f"Schema loading error: {safe_error}",
                "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
                "joins": [],
                "join_success": False,
                "unlinked_entities": list(entities.keys()) if isinstance(entities, dict) else [],
                "schema_info": {}
            }
        
        # Логируем статистику загруженной схемы
        logger.info(f"Loaded schema with {len(db_schema)} tables")
        
        # Проверяем кэш
        cache_info = self.cache_manager.prepare_cache_info(entities, db_schema, dsn=effective_dsn)
        # W2-T4: corruption отделена от miss. Если backend кэша упал, мы
        # явно логируем это как ошибку и продолжаем без кэша — caller
        # видит, что данные были пересчитаны (а не «всё штатно, miss»).
        # При этом дальше пропустим и save: backend на запись тоже
        # вероятно нездоров, ронять основной flow не хотим.
        cache_corrupted = False
        try:
            cached_result = self.cache_manager.load_from_cache(cache_info)
        except SchemaCacheCorrupted as exc:
            logger.error(
                "Schema-linking cache load failed (corruption): %s — rebuilding without cache",
                exc,
            )
            cached_result = None
            cache_corrupted = True
        if cached_result:
            linked = cached_result.get("linked_entities", {})
            if cached_result.get("error") and not (linked.get("metrics") or linked.get("dimensions")):
                cached_result = None
        if cached_result:
            # Добавляем актуальную схему к кэшированному результату
            cached_result["schema_info"] = self.context_builder.build_relevant_schema_context(
                cached_result.get("linked_entities", {}).get("metrics", []),
                cached_result.get("linked_entities", {}).get("dimensions", []),
                cached_result.get("linked_entities", {}).get("filters", {}),
                cached_result.get("joins", []),
                db_schema,
                dsn=effective_dsn,
            )
            return cached_result

        # Выполняем связывание
        result = self.linking_core.perform_linking(entities, db_schema, dsn=effective_dsn)

        # Добавляем контекст схемы
        result["schema_info"] = self.context_builder.build_relevant_schema_context(
            result.get("linked_entities", {}).get("metrics", []),
            result.get("linked_entities", {}).get("dimensions", []),
            result.get("linked_entities", {}).get("filters", {}),
            result.get("joins", []),
            db_schema,
            dsn=effective_dsn,
        )

        # Сохраняем только содержательные результаты, чтобы не закреплять временные ошибки линкинга.
        # При cache_corrupted save тоже пропускаем (backend нездоров).
        linked = result.get("linked_entities", {})
        if (not cache_corrupted) and (not result.get("error") or linked.get("metrics") or linked.get("dimensions")):
            try:
                self.cache_manager.save_to_cache(cache_info, result)
            except SchemaCacheCorrupted as exc:
                # Запись провалилась — логируем, но не ломаем основной
                # поток: linking-результат у нас уже посчитан и валиден.
                logger.error(
                    "Schema-linking cache save failed (corruption): %s — continuing without caching",
                    exc,
                )

        return result
    
    def _get_database_schema(
        self,
        schema_info: Dict[str, Any],
        dsn: Optional[str] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Получает схему БД, делегируя в :class:`SchemaLoader` (SoT) и
        добавляя side-effects: индексацию в память и schema-ready marker.

        См. EPIC 3.2 — устранение дублирования ``get_database_schema`` между
        ``schema_linker`` и ``schema_loader``. Логика resolve-источника
        (schema_info / sqlrag / introspection) полностью живёт в loader;
        здесь — только enricher-hook (для пути introspection) и индексация.
        """
        # При in-memory schema_info индексация не нужна — это уже передано
        # вызывающей стороной и не требует persisting в memory store.
        if schema_info:
            return self.loader.get_database_schema(schema_info, dsn=dsn)

        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        db_schema = self.loader.get_database_schema({}, dsn=effective_dsn)

        # Индексация в память + schema-ready marker (работает для обоих путей:
        # sqlrag-файл и свежая интроспекция).
        if effective_dsn and db_schema:
            # Для пути introspection дополнительно оптимизируем схему перед
            # передачей в memory-индекс (сохраняем lossless семантику).
            from .schema_metadata import SchemaStatsHelper
            indexed = self.memory_manager.ensure_schema_indexed_in_memory(
                effective_dsn, SchemaStatsHelper.optimize_schema_for_storage(db_schema)
            )
            if indexed:
                session_id = dsn_to_sanitized_name(effective_dsn)
                self.memory_manager.set_schema_ready_marker(
                    session_id, get_schema_version(db_schema)
                )
        return db_schema
    
    def _ensure_initialized(self, dsn: Optional[str] = None) -> None:
        """Убеждается, что компоненты инициализированы.

        Fail-fast: раньше любой Exception тихо превращался в "schema
        indexing disabled" — caller не видел, что system broken
        (Phase 6-Extended). Теперь импорт-ошибки и прочее пробрасываются.
        Отсутствие memory system / DB_DSN остаётся мягким warning'ом,
        потому что это нормальный sentinel для CI/non-DB сценариев.
        """
        # Programmer-error guard: memory_manager должен быть подмешан через
        # SchemaLinkerDeps (legacy или factory) ещё в __init__. Если
        # внешний код намеренно занулил атрибут — это поломка контракта
        # и работать дальше нельзя.
        if not hasattr(self, "memory_manager") or self.memory_manager is None:
            raise RuntimeError(
                "SchemaMemoryManager not initialized on SchemaLinker; "
                "this indicates a DI/setup bug."
            )

        # Проверяем наличие системы памяти
        from memory.tools import save_memory, get_memory
        from memory.manager import memory_manager

        if not (save_memory and get_memory and memory_manager):
            logger.warning("Memory system not available - schema indexing disabled")
            return

        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        if not effective_dsn:
            logger.warning("DSN not set - schema operations limited")
            return

        session_id = dsn_to_sanitized_name(effective_dsn)
        logger.debug(f"Schema system initialized for session: {session_id}")

    def _check_type_compatibility(self, type1: str, type2: str) -> bool:
        """Thin delegating shim to :meth:`ColumnMetadataHelper.check_type_compatibility`.

        Сохраняется как публичный API для тестов и внешних callers. Поведение
        полностью идентично helper'у — никаких silent fallback'ов для пустых
        типов: они приводят к ``ValueError`` (см. helper).
        """
        from .schema_metadata import ColumnMetadataHelper

        return ColumnMetadataHelper.check_type_compatibility(type1, type2)

    def _get_column_meta(
        self,
        table_name: str,
        column_name: str,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """Legacy wrapper around the shared case-insensitive column resolver."""
        return _get_column_meta(table_name, column_name, db_schema)

    # EPIC 3.32: instance-методы ``_table_exists_in_schema`` и
    # ``_column_exists_in_table`` удалены как deprecated wrappers без callers
    # (grep подтвердил отсутствие ``schema_linker_instance._table_exists_in_schema(``).
    # Module-level импорты из ``schema_linking.resolution`` (см. шапку файла)
    # покрывают contract-pinning тест ``test_schema_linker_uses_shared_resolution``,
    # т.к. ``SchemaLinker._table_exists_in_schema`` теперь резолвится к module-attribute.

    def _is_join_valid_against_schema(
        self,
        join: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Legacy validation result with scoring metadata.

        T5-linking / #12 MEDIUM: делегирует в package JoinValidator для
        базовой валидации (table/column existence + type check + fail-fast
        при missing types согласно 4.19). Если package отклонил join —
        немедленно возвращаем {valid: False, score: 0, reasons, notes: []}.
        Если valid — продолжаем существующую FK/PK scoring логику над
        normalized_join из package-результата. Это устраняет дублирование
        resolve-логики и синхронизирует fail-fast при missing types.
        Внешний контракт {valid, score, reasons, notes, join} сохранён.
        """
        from .schema_metadata import is_fk as _is_fk, is_pk as _is_pk

        notes: List[str] = []
        score = 100

        if not all([join.get("from_table"), join.get("to_table"),
                    join.get("from_column"), join.get("to_column")]):
            return {"valid": False, "score": 0, "reasons": ["Missing join parameters"], "notes": []}

        # Делегируем базовую валидацию через публичный метод orchestrator'а
        # (T5-linking/#12: не обращаемся к приватному _join_validator напрямую).
        pkg_result = self.linking_core.validate_join(join, db_schema)
        if not pkg_result.get("valid", False):
            error = pkg_result.get("error", "Validation failed")
            return {"valid": False, "score": 0, "reasons": [error], "notes": []}

        # Контракт validate_join: valid=True ⇒ присутствует нормализованный "join".
        # Инвариант сейчас соблюдён реализацией, но не зафиксирован типом, поэтому
        # не обращаемся через [..]: при нарушении (mock/регрессия orchestrator)
        # вернём явную причину, а не сырой KeyError в середине scoring.
        normalized_join = pkg_result.get("join")
        if normalized_join is None:
            return {
                "valid": False,
                "score": 0,
                "reasons": ["validate_join вернул valid=True без нормализованного 'join'"],
                "notes": [],
            }
        resolved_from_table = normalized_join["from_table"]
        resolved_from_column = normalized_join["from_column"]
        resolved_to_table = normalized_join["to_table"]
        resolved_to_column = normalized_join["to_column"]

        from_meta = self._get_column_meta(resolved_from_table, resolved_from_column, db_schema)
        to_meta = self._get_column_meta(resolved_to_table, resolved_to_column, db_schema)

        # Type compatibility scoring (только если типы присутствуют —
        # package уже отклонил missing-type случаи через fail-fast 4.19)
        from .schema_metadata import get_type as _get_type
        from_type = _get_type(from_meta) if from_meta else ""
        to_type = _get_type(to_meta) if to_meta else ""
        # reasons собираем ИЗ ветвей, которые СНИЖАЮТ score (a не по порогу
        # score<=0): пакетный JoinValidator уже отклонил hard-невалидные join'ы
        # выше (early-return с reasons), сюда попадают только valid join'ы, у
        # которых soft-score всегда > 0 (min ~55). Прежний `reasons = notes if
        # score<=0` поэтому всегда давал [] — caller не видел soft-проблем
        # (type mismatch / PK-PK / возможный M:N). Теперь reasons перечисляет
        # именно эти озабоченности, а positive-заметки остаются только в notes.
        reasons: List[str] = []
        if from_type and to_type:
            if self._check_type_compatibility(from_type, to_type):
                notes.append("Compatible types")
            else:
                score -= 30
                msg = f"Type mismatch: {from_type} vs {to_type}"
                notes.append(msg)
                reasons.append(msg)

        if from_meta and to_meta:
            if _is_fk(from_meta) and _is_pk(to_meta):
                score += 20
                notes.append("FK->PK relationship")
            elif _is_pk(from_meta) and _is_fk(to_meta):
                score += 20
                notes.append("PK->FK relationship")
            elif _is_pk(from_meta) and _is_pk(to_meta):
                score -= 10
                msg = "PK->PK join (unusual)"
                notes.append(msg)
                reasons.append(msg)
            elif not (_is_pk(from_meta) or _is_pk(to_meta)):
                score -= 15
                msg = "Potential many-to-many without bridge table"
                notes.append(msg)
                reasons.append(msg)

        return {
            "valid": score > 0,
            "score": max(0, score),
            "reasons": reasons,
            "notes": notes,
            "join": normalized_join,
        }

    def _parse_fk_reference(self, references: str) -> tuple:
        from .schema_linking.join_validation import _parse_fk_reference
        return _parse_fk_reference(references)

    def _prepare_cache_info(
        self,
        entities: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, str]:
        return self.cache_manager.prepare_cache_info(entities, db_schema, dsn=dsn)

    def _create_table_description(self, table_name: str, table_cols: Dict[str, Dict[str, Any]]) -> str:
        from .schema_metadata import get_type as _get_type, is_fk as _is_fk, is_pk as _is_pk

        parts = [f"Таблица {table_name}"]
        pk_columns: List[str] = []
        fk_columns: List[str] = []
        columns: List[str] = []

        for column_name, meta in table_cols.items():
            if not isinstance(meta, dict):
                meta = {"type": str(meta)}
            column_type = _get_type(meta)
            description = str(meta.get("description") or "")
            column_text = f"{column_name} ({column_type})" if column_type else column_name
            if description:
                column_text = f"{column_text}: {description}"
            columns.append(column_text)

            if _is_pk(meta):
                pk_columns.append(f"{column_name} ({column_type})" if column_type else column_name)
            if _is_fk(meta):
                references = meta.get("references")
                fk_columns.append(f"{column_name} -> {references}" if references else column_name)

        if pk_columns:
            parts.append(f"Первичные ключи: {', '.join(pk_columns)}")
        if fk_columns:
            parts.append(f"Внешние ключи: {', '.join(fk_columns)}")
        if columns:
            parts.append(f"Колонки: {', '.join(columns)}")

        return ". ".join(parts) + "."


def _build_default_deps(schema_limiter: SchemaLimiter, repo_root: Path) -> SchemaLinkerDeps:
    """Собирает :class:`SchemaLinkerDeps` со стандартными зависимостями.

    Используется обоими путями конструирования (legacy ``__init__(limiter)``
    и явный :meth:`SchemaLinker.with_defaults`).

    EPIC 8.6: ``call_openai_api`` теперь резолвится явно при сборке Deps —
    раньше это делал ``schema_linking_core.py`` shim как побочный эффект
    импорта. Если ``utils.call_openai_api`` недоступен (например, в CI без
    OpenAI-зависимостей), ``llm_caller=None`` — orchestrator выдаст явный
    error вместо silent fallback.
    """
    try:
        from utils import call_openai_api as _default_llm_caller  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM schema linking is unavailable: %s", exc)
        _default_llm_caller = None  # type: ignore[assignment]

    loader = SchemaLoader(repo_root)
    enricher = SchemaEnricher()
    memory_manager = SchemaMemoryManager(repo_root)
    cache_manager = SchemaCacheManager()
    linking_core = SchemaLinkingCore(
        schema_limiter, memory_manager, llm_caller=_default_llm_caller
    )
    context_builder = SchemaContextBuilder(memory_manager)
    return SchemaLinkerDeps(
        schema_limiter=schema_limiter,
        loader=loader,
        enricher=enricher,
        memory_manager=memory_manager,
        cache_manager=cache_manager,
        linking_core=linking_core,
        context_builder=context_builder,
    )


# ========================================================================================
# BACKWARD COMPATIBILITY
# ========================================================================================

# Source of truth — schema_metadata; здесь только module-level реэкспорт для
# существующих callers (tests/test_schema_linker_improvements.py импортируют
# эти имена напрямую из schema_linker). Дублирующие wrapper-функции удалены —
# см. устранение дублирования helpers между schema_linker и schema_metadata.
from .schema_metadata import (  # noqa: E402,F401
    is_pk,
    is_fk,
    is_not_null,
    get_type,
    normalize_constraint_type,
)


# Явный контракт публичного API модуля. Underscore-функции
# (``_resolve_table_name`` и т.п.), импортированные в шапке, остаются
# приватными — снаружи их использовать не следует, для них предусмотрен
# импорт из ``schema_linking.resolution``.
__all__ = [
    "SchemaLinker",
    "SchemaLinkerDeps",
    "is_pk",
    "is_fk",
    "is_not_null",
    "get_type",
    "normalize_constraint_type",
]
