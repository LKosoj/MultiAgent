"""Schema Linking API подмодуль core (Phase 7 декомпозиция).

Реализация schema_linking и _normalize_schema_linking_entities.
"""
import logging
import warnings as _warnings
from typing import Dict, List, Optional

from ..deprecations import TextToSQLDeprecationWarning

logger = logging.getLogger(__name__)

_ENTITY_LIST_TYPES = (("metrics", "metric"), ("dimensions", "dimension"))


def _canonical_entity_name(value: object) -> Optional[str]:
    if isinstance(value, str):
        name = value.strip()
        return name or None
    if isinstance(value, dict):
        for key in ("name", "column"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _unresolved_entities(
    entities: Dict[str, object],
    linked_entities: object,
) -> List[Dict[str, str]]:
    linked = linked_entities if isinstance(linked_entities, dict) else {}
    resolved: set[tuple[str, str]] = set()
    for key, entity_type in _ENTITY_LIST_TYPES:
        items = linked.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            name = _canonical_entity_name(item)
            if name:
                resolved.add((entity_type, name.casefold()))
    linked_filters = linked.get("filters")
    if isinstance(linked_filters, dict):
        resolved.update(("filter", str(name).casefold()) for name in linked_filters)

    unresolved: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entity_type in _ENTITY_LIST_TYPES:
        values = entities.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            name = _canonical_entity_name(value)
            if not name:
                continue
            identity = (entity_type, name.casefold())
            if identity not in resolved and identity not in seen:
                unresolved.append({"entity_type": entity_type, "name": name})
                seen.add(identity)
    filters = entities.get("filters")
    if isinstance(filters, dict):
        for value in filters:
            name = str(value)
            identity = ("filter", name.casefold())
            if identity not in resolved and identity not in seen:
                unresolved.append({"entity_type": "filter", "name": name})
                seen.add(identity)
    return unresolved


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
    value_grounding: Optional[bool] = None,
    *,
    schema_scope: Optional[Dict[str, object]] = None,
    schema_limiter,
) -> Dict[str, object]:
    """LLM-схемный линкинг с авто-интроспекцией через плагины БД и кэшированием.

    Args:
        entities: Словарь с извлечёнными сущностями (metrics/dimensions/filters).
        session_id: ID сессии для контекста (опционально).
        schema_info: Явная схема БД для линкинга; если None — берётся из кэша/
            интроспекции через SchemaLinker.
        dsn: DSN целевой БД для загрузки sqlrag-схемы и интроспекции.
        value_grounding: opt-in DB lookup для уточнения значений linked filters.

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
    from ..schema_namespace import SchemaScope
    from ..utils import get_runtime_context_dsn

    effective_dsn = dsn or get_runtime_context_dsn()
    entities, input_warnings = _normalize_schema_linking_entities(entities)
    if not entities:
        result = {
            "error": "Invalid schema_linking entities payload",
            "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
            "joins": [],
            "join_success": False,
            "sql_generation_allowed": False,
            "unlinked_entities": [],
            "unresolved_entities": [],
            "ambiguous_bindings": [],
            "schema_info": {},
            "input_warnings": input_warnings,
        }
        from ..quality import schema_linking_quality

        result.update(schema_linking_quality(result))
        return result
    linker = SchemaLinker.with_defaults(schema_limiter)
    link_kwargs = {
        "dsn": effective_dsn,
        "session_id": session_id,
        "value_grounding": value_grounding,
    }
    if schema_scope is not None:
        link_kwargs["schema_scope"] = SchemaScope.from_mapping(schema_scope)
    result = linker.link_entities_to_schema(entities, schema_info, **link_kwargs)
    from ..quality import schema_linking_quality

    result["unresolved_entities"] = _unresolved_entities(
        entities,
        result.get("linked_entities"),
    )
    if not isinstance(result.get("ambiguous_bindings"), list):
        result["ambiguous_bindings"] = []
    quality = schema_linking_quality(result)
    result.update(quality)
    if input_warnings:
        result["input_warnings"] = input_warnings
    return result
