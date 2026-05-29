"""Schema Linking API подмодуль core (Phase 7 декомпозиция).

Реализация schema_linking и _normalize_schema_linking_entities.
"""
import logging
import warnings as _warnings
from typing import Dict, List, Optional

from ..deprecations import TextToSQLDeprecationWarning

logger = logging.getLogger(__name__)


def _normalize_schema_linking_entities(entities: Dict[str, object]) -> tuple[Dict[str, object], List[str]]:
    """Возвращает canonical payload metrics/dimensions/filters для schema_linking."""
    warnings: List[str] = []
    if not isinstance(entities, dict):
        warnings.append("schema_linking entities must be a dict")
        return {}, warnings

    canonical_keys = {"metrics", "dimensions", "filters"}
    if not canonical_keys.intersection(entities.keys()) and isinstance(entities.get("entities"), dict):
        warnings.append("schema_linking received intent wrapper; using nested entities payload")
        entities = entities["entities"]  # type: ignore[assignment]

    if not isinstance(entities, dict):
        warnings.append("schema_linking nested entities payload must be a dict")
        return {}, warnings

    normalized = {
        "metrics": entities.get("metrics", []),
        "dimensions": entities.get("dimensions", []),
        "filters": entities.get("filters", {}),
    }

    if not isinstance(normalized["metrics"], list):
        warnings.append("schema_linking metrics must be a list")
        return {}, warnings
    if not isinstance(normalized["dimensions"], list):
        warnings.append("schema_linking dimensions must be a list")
        return {}, warnings
    if not isinstance(normalized["filters"], dict):
        warnings.append("schema_linking filters must be a dict")
        return {}, warnings

    return normalized, warnings


def schema_linking(
    entities: Dict[str, object],
    session_id: Optional[str] = None,
    schema_info: Optional[Dict[str, object]] = None,
    dsn: Optional[str] = None,
    *,
    schema_limiter,
) -> Dict[str, object]:
    """LLM-схемный линкинг с авто-интроспекцией через плагины БД и кэшированием.

    Args:
        entities: Словарь с извлечёнными сущностями (metrics/dimensions/filters).
        session_id: ID сессии для контекста (опционально).
        schema_info: Явная схема БД для линкинга; если None — берётся из кэша/
            интроспекции через SchemaLinker.
        dsn: DSN целевой БД для загрузки sqlrag-схемы и интроспекции.

    Returns:
        Словарь с привязанными к схеме сущностями.

    Note:
        Backward-compat shim: исторически второй позиционный аргумент мог
        быть dict-схемой (вместо session_id). Такой вызов сейчас работает,
        но выдаёт DeprecationWarning. Используйте `schema_info=` kwarg.
    """
    # Backward-compat: позиционно переданный dict в session_id — это
    # legacy-форма передачи schema_info. См. EPIC 1.7.
    if isinstance(session_id, dict):
        if schema_info is not None:
            logger.error(
                "schema_linking: ambiguous call — session_id is a dict "
                "and schema_info kwarg also provided"
            )
            raise TypeError(
                "schema_linking: ambiguous call — session_id is a dict "
                "and schema_info kwarg also provided"
            )
        _warnings.warn(
            "schema_linking: passing schema_info via session_id positional "
            "argument is deprecated; use schema_info= kwarg",
            TextToSQLDeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "schema_linking: deprecated positional dict passed as session_id; "
            "treating as schema_info (use schema_info= kwarg instead)"
        )
        schema_info = session_id
        session_id = None

    if schema_info is None:
        schema_info = {}

    from ..schema_linker import SchemaLinker
    from ..utils import get_runtime_context_dsn

    effective_dsn = dsn or get_runtime_context_dsn()
    entities, input_warnings = _normalize_schema_linking_entities(entities)
    if not entities:
        return {
            "error": "Invalid schema_linking entities payload",
            "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
            "joins": [],
            "join_success": False,
            "unlinked_entities": [],
            "schema_info": {},
            "input_warnings": input_warnings,
        }
    linker = SchemaLinker.with_defaults(schema_limiter)
    result = linker.link_entities_to_schema(entities, schema_info, dsn=effective_dsn)
    if input_warnings:
        result["input_warnings"] = input_warnings
    return result
