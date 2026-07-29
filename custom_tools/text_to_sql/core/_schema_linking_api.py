"""Schema Linking API подмодуль core (Phase 7 декомпозиция).

Реализация schema_linking и _normalize_schema_linking_entities.
"""
import logging
import re
import warnings as _warnings
from typing import Dict, List, Optional

from ..deprecations import TextToSQLDeprecationWarning

logger = logging.getLogger(__name__)

_ENTITY_LIST_TYPES = (("metrics", "metric"), ("dimensions", "dimension"))
_ENTITY_IDENTITY_KEYS = ("source_entity_id", "entity", "name", "column", "expression")
_FILTER_QUERY_CONTROLS = {
    "aggregation",
    "distinct",
    "limit",
    "offset",
    "order",
    "order_by",
}
_FILTER_METADATA_KEYS = {
    "comparison",
    "condition",
    "end",
    "operator",
    "start",
    "value",
    "values",
}
_AGGREGATE_EXPRESSION_RE = re.compile(
    r"^\s*(count|sum|avg|min|max)\s*\(\s*(?:[^.()]+\.)?([^()]+)\s*\)\s*$",
    re.IGNORECASE,
)


def _canonical_entity_name(value: object) -> Optional[str]:
    if isinstance(value, str):
        name = value.strip()
        return name or None
    if isinstance(value, dict):
        for key in ("entity", "name", "column"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _entity_identity_names(value: object) -> set[str]:
    if isinstance(value, str):
        name = value.strip()
        return {name.casefold()} if name else set()
    if not isinstance(value, dict):
        return set()
    return {
        candidate.strip().casefold()
        for key in _ENTITY_IDENTITY_KEYS
        if isinstance((candidate := value.get(key)), str) and candidate.strip()
    }


def _table_identity(value: object) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    table = value.get("table")
    if not isinstance(table, str) or not table.strip():
        return None
    unqualified = table.rsplit(".", 1)[-1].strip().strip('"`[]')
    return unqualified.casefold() or None


def _aggregate_binding(value: object) -> Optional[tuple[str, str]]:
    if not isinstance(value, dict):
        return None
    aggregation = value.get("aggregation")
    column = value.get("column")
    if not isinstance(aggregation, str) or not aggregation.strip():
        return None
    if not isinstance(column, str) or not column.strip():
        return None
    return aggregation.strip().casefold(), column.strip().strip('"`[]').casefold()


def _unresolved_entities(
    entities: Dict[str, object],
    linked_entities: object,
) -> List[Dict[str, str]]:
    linked = linked_entities if isinstance(linked_entities, dict) else {}
    resolved: set[tuple[str, str]] = set()
    physical_bindings: set[str] = set()
    aggregate_bindings: set[tuple[str, str]] = set()
    for key, entity_type in _ENTITY_LIST_TYPES:
        items = linked.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            identity_names = _entity_identity_names(item)
            resolved.update((entity_type, name) for name in identity_names)
            if (
                isinstance(item, dict)
                and isinstance(item.get("table"), str)
                and isinstance(item.get("column"), str)
            ):
                physical_bindings.update(identity_names)
            table_name = _table_identity(item)
            if table_name:
                resolved.add((entity_type, table_name))
            if entity_type == "metric":
                aggregate = _aggregate_binding(item)
                if aggregate:
                    aggregate_bindings.add(aggregate)

    linked_filters = linked.get("filters")
    linked_filter_metadata: Dict[str, List[object]] = {}
    if isinstance(linked_filters, dict):
        for name, value in linked_filters.items():
            resolved.add(("filter", str(name).casefold()))
            identity_names = _entity_identity_names(value)
            resolved.update(("filter", identity) for identity in identity_names)
            if (
                isinstance(value, dict)
                and isinstance(value.get("table"), str)
                and isinstance(value.get("column"), str)
            ):
                physical_bindings.add(str(name).casefold())
                physical_bindings.update(identity_names)
            table_name = _table_identity(value)
            if table_name:
                resolved.add(("filter", table_name))
            if isinstance(value, dict):
                for metadata_key in _FILTER_METADATA_KEYS:
                    if metadata_key in value:
                        linked_filter_metadata.setdefault(
                            metadata_key, []
                        ).append(value[metadata_key])

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
            is_resolved = any(
                (entity_type, candidate) in resolved
                for candidate in _entity_identity_names(value)
            )
            if not is_resolved and entity_type == "metric":
                aggregate_match = _AGGREGATE_EXPRESSION_RE.match(name)
                if aggregate_match:
                    aggregation = aggregate_match.group(1).casefold()
                    column = aggregate_match.group(2).strip().strip('"`[]').casefold()
                    is_resolved = (aggregation, column) in aggregate_bindings
                elif name.casefold() in {"count", "sum", "avg", "min", "max"}:
                    is_resolved = any(
                        aggregation == name.casefold()
                        for aggregation, _ in aggregate_bindings
                    )
            if not is_resolved and identity not in seen:
                unresolved.append({"entity_type": entity_type, "name": name})
                seen.add(identity)
    filters = entities.get("filters")
    if isinstance(filters, dict):
        for value, original_value in filters.items():
            name = str(value)
            identity = ("filter", name.casefold())
            is_resolved = (
                identity in resolved or name.casefold() in physical_bindings
            )
            if name.casefold() in _FILTER_QUERY_CONTROLS:
                is_resolved = True
            metadata_values = linked_filter_metadata.get(name.casefold(), [])
            if any(candidate == original_value for candidate in metadata_values):
                is_resolved = True
            if not is_resolved and identity not in seen:
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
