"""
LLM-based schema linking.

Расщеплено из ``strategies.py`` (EPIC 8.2). Этот модуль содержит
LLM-driven pipeline ``llm_linking`` плюс DI-каркас для ``llm_caller``.

EPIC 8.6: ``schema_linking_core`` shim удалён, late-binding lookup
выпилен. ``llm_caller`` приходит ТОЛЬКО через конструктор (DI). Если
caller не передан и LLM включён — ``perform_linking`` возвращает
explicit error (silent fallback запрещён, см. AGENTS.md).
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from memory.manager import EmbeddingUnavailableError, EmbeddingFailedError

from ..prompts import build_schema_linking_prompt
from ..schema_namespace import SchemaNamespace
from ..utils import parse_llm_json_response
from ..validators.schema_limiter import SchemaContextBudgetExceeded
from .resolution import _resolve_column_name, _resolve_table_name

logger = logging.getLogger(__name__)

_SEMANTIC_RETRIEVAL_UNAVAILABLE_STATUSES = frozenset(
    {"memory_unavailable", "vector_unavailable", "vector_disabled"}
)


def _redact_linking_value(value: Any) -> Any:
    try:
        from custom_tools.text_to_sql.redaction import _redact_payload, redact_pii_in_payload

        return redact_pii_in_payload(_redact_payload(value))
    except Exception:
        return "<redacted>"


def _redact_linking_error(error: Exception) -> str:
    return str(_redact_linking_value(str(error)))


class LLMLinker:
    """LLM-связывание сущностей со схемой.

    ``llm_caller`` приходит через конструктор (DI, 4.25). EPIC 8.6 удалил
    bridge через ``schema_linking_core`` shim — DI единственный путь.
    """

    def __init__(
        self,
        schema_limiter,
        memory_manager,
        entity_term_collector: Callable[[Dict[str, Any]], List[str]],
        llm_caller: Optional[Callable[..., Any]] = None,
    ):
        self.schema_limiter = schema_limiter
        self.memory_manager = memory_manager
        self._llm_caller = llm_caller
        self._collect_entity_terms = entity_term_collector

    # ------------------------------------------------------------------
    # LLM-caller resolution (DI only, EPIC 8.6)
    # ------------------------------------------------------------------
    def active_llm_caller(self) -> Optional[Callable[..., Any]]:
        """Возвращает активный LLM-клиент (только из DI).

        EPIC 8.6: late-binding lookup через ``schema_linking_core`` shim
        удалён — DI обязателен. Если caller не передан — возвращается
        None, и orchestrator выдаст explicit error.
        """
        return self._llm_caller

    # ------------------------------------------------------------------
    # LLM linking
    # ------------------------------------------------------------------
    def llm_linking(
        self,
        entities: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
        namespace: Optional[SchemaNamespace] = None,
    ) -> Optional[Dict[str, Any]]:
        """LLM-связывание сущностей со схемой."""
        # Fail-fast ДО try/except ниже: иначе RuntimeError будет пойман
        # выходным `except Exception as e` и превратится в невнятный
        # «LLM schema linking failed». DI-конфиг — программерская ошибка
        # сетапа, её нельзя глушить в error-dict.
        if self.active_llm_caller() is None:
            raise RuntimeError(
                "LLM caller is not configured for schema linking. "
                "Check DI setup (pass llm_caller= to constructor)."
            )
        schema_budget: Dict[str, Any] = {"model_calls": 0}
        try:
            entity_names = self._collect_entity_terms(entities)
            if not entity_names:
                return {
                    "error": "No schema-linking entity values provided",
                    "suggestion": "Pass canonical entities with metrics, dimensions, or filters.",
                    "query_entities": [],
                }

            if not db_schema:
                return {
                    "error": "Database schema is empty",
                    "suggestion": "Load the authoritative database schema before schema linking.",
                    "query_entities": _redact_linking_value(entity_names),
                }

            embedding_retrieval_unavailable = False
            try:
                if namespace is None:
                    relevant_tables = self.memory_manager.find_semantic_relevant_tables(
                        entity_names,
                        dsn=dsn,
                    )
                else:
                    # W1-1.2-review Блокер 1 (уточнено финальным ревью): на namespace-ветке
                    # find_semantic_relevant_tables ищет по namespace.version_key и dsn НЕ
                    # читает — DSN-профиль на этот поиск не влияет ни до, ни после правки.
                    # dsn передаём только для симметрии с legacy-веткой выше.
                    relevant_tables = self.memory_manager.find_semantic_relevant_tables(
                        entity_names,
                        dsn=dsn,
                        namespace=namespace,
                    )
                memory_status = getattr(
                    self.memory_manager, "last_search_status", None
                )
                memory_error = getattr(self.memory_manager, "last_search_error", None)
            except (EmbeddingUnavailableError, EmbeddingFailedError) as e:
                relevant_tables = []
                memory_status = "embedding_unavailable"
                memory_error = _redact_linking_error(e)
                embedding_retrieval_unavailable = True
                logger.warning(
                    "LLM linking: embedding retrieval is unavailable; "
                    "using the authoritative schema: %s",
                    memory_error,
                )
            logger.info(
                f"Found {len(relevant_tables)} relevant tables for LLM linking: {relevant_tables}"
            )
            safe_memory_error = str(
                _redact_linking_value(memory_error or "unknown reason")
            )

            include_all_tables = False
            if relevant_tables:
                schema_for_prompt: Dict[str, Any] = {
                    table_name: db_schema[table_name]
                    for table_name in relevant_tables
                    if table_name in db_schema
                }
                if not schema_for_prompt:
                    logger.warning(
                        "Memory suggested tables %s but none found in db_schema "
                        "(schema tables sample: %s); possible schema rename or stale "
                        "index — re-index schema",
                        relevant_tables[:10],
                        list(db_schema.keys())[:10],
                    )
                    return {
                        "error": "Semantic search returned tables absent from the authoritative schema",
                        "memory_status": memory_status or "unknown",
                        "memory_error": _redact_linking_value(memory_error),
                        "suggestion": "Re-index schema metadata before schema linking.",
                        "available_tables": list(db_schema.keys())[:10],
                        "query_entities": _redact_linking_value(entity_names),
                    }
            elif (
                memory_status in _SEMANTIC_RETRIEVAL_UNAVAILABLE_STATUSES
                or embedding_retrieval_unavailable
            ):
                schema_for_prompt = {
                    table_name: db_schema[table_name]
                    for table_name in sorted(db_schema)
                }
                include_all_tables = True
                logger.warning(
                    "Semantic retrieval status=%s; using all %s authoritative "
                    "schema tables: %s",
                    memory_status,
                    len(schema_for_prompt),
                    safe_memory_error,
                )
            elif memory_status == "no_hits":
                return {
                    "error": "No semantically relevant tables found",
                    "memory_status": memory_status,
                    "memory_error": _redact_linking_value(memory_error),
                    "suggestion": (
                        "Database schema may not contain data relevant to the requested "
                        "query domain"
                    ),
                    "available_tables": list(db_schema.keys())[:10],
                    "query_entities": _redact_linking_value(entity_names),
                }
            else:
                return {
                    "error": "Semantic schema retrieval is not ready",
                    "memory_status": memory_status or "unknown",
                    "memory_error": _redact_linking_value(memory_error),
                    "suggestion": "Restore semantic retrieval before schema linking.",
                    "query_entities": _redact_linking_value(entity_names),
                }

            logger.info(
                f"Using {len(schema_for_prompt)} tables for LLM linking "
                f"(from {len(db_schema)} total)"
            )

            from ..llm_models_config import load_llm_models_config

            llm_profile = load_llm_models_config()
            max_tokens = llm_profile.get("schema_linking", "max_tokens")
            hard_max_chars = llm_profile.get(
                "schema_linking", "schema_prompt_hard_max_chars"
            )
            schema_str = self.schema_limiter.build_schema_summary(
                schema_for_prompt,
                query_terms=entity_names,
                hard_max_chars=hard_max_chars,
                diagnostics=schema_budget,
                include_all_tables=include_all_tables,
            )
            # L58: entities могут содержать PII (значения фильтров из
            # пользовательского запроса). LLM-линкеру нужны КЛЮЧИ фильтров
            # (имена колонок), а не фактические значения — редактируем значения
            # filters перед передачей в промпт.
            entities_for_prompt = entities
            if isinstance(entities_for_prompt, dict) and isinstance(
                entities_for_prompt.get("filters"), dict
            ):
                entities_for_prompt = dict(entities_for_prompt)
                entities_for_prompt["filters"] = {
                    k: _redact_linking_value(v)
                    for k, v in entities_for_prompt["filters"].items()
                }
            prompt = build_schema_linking_prompt(
                entities_for_prompt,
                schema_str,
                dsn=dsn,
                schema_fingerprint=(
                    namespace.schema_fingerprint if namespace is not None else None
                ),
            )

            logger.info(f"Schema linking prompt length: {len(prompt)}")
            logger.debug(
                "Schema linking prompt preview: %s...",
                _redact_linking_value(prompt[:300]),
            )

            # active_llm_caller проверен в начале llm_linking (fail-fast).
            # Здесь дополнительной проверки не нужно: caller гарантированно callable.
            call_openai_api = self.active_llm_caller()
            # system_prompt приходит из prompts.yaml (W6-T2): runtime-код не
            # содержит длинных промпт-текстов — единый source of truth.
            from ..prompts_config import load_prompts_config

            prompts_profile = load_prompts_config()
            system_prompt = prompts_profile.get_text("schema_linking", "system_prompt")

            from agent_command import model_mapping
            from ..llm_models_config import step_model_name

            schema_budget["model_calls"] = 1
            resp = call_openai_api(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                model=model_mapping[step_model_name("schema_linking")],
                response_format={"type": "json_object"},
            )

            parsed = parse_llm_json_response(resp)

            logger.info(f"LLM schema linking response length: {len(resp) if resp else 0}")
            logger.info(f"LLM parsed result type: {type(parsed)}")

            if isinstance(parsed, dict) and "linked_entities" in parsed:
                joins_from_llm = parsed.get("joins", [])
                logger.info(f"LLM returned {len(joins_from_llm)} joins: {joins_from_llm}")
                linked_entities = parsed.get("linked_entities", {})
                linked_entities = _normalize_linked_entities(linked_entities, db_schema)
                if not _has_linked_entities(linked_entities):
                    return {
                        "error": "LLM schema linking returned no linked entities",
                        "linked_entities": linked_entities if isinstance(linked_entities, dict) else {},
                        "joins": joins_from_llm,
                        "unlinked_entities": parsed.get("unlinked_entities", []),
                        "schema_budget": schema_budget,
                    }

                return {
                    "linked_entities": linked_entities,
                    "joins": joins_from_llm,
                    "unlinked_entities": parsed.get("unlinked_entities", []),
                    "ambiguous_bindings": parsed.get("ambiguous_bindings", []),
                    "schema_budget": schema_budget,
                }
            else:
                logger.warning(
                    f"LLM response parsing failed. Parsed type: {type(parsed)}, "
                    f"content preview: {_redact_linking_value(str(parsed)[:200]) if parsed else 'None'}"
                )
                logger.debug(
                    "Raw LLM response: %s",
                    _redact_linking_value(resp[:500]) if resp else "None",
                )
                return {
                    "error": "LLM schema linking returned invalid response shape",
                    "suggestion": "Expected JSON object with linked_entities.",
                    "query_entities": _redact_linking_value(entity_names),
                    "schema_budget": schema_budget,
                }
        except SchemaContextBudgetExceeded as e:
            logger.warning(
                "LLM schema linking skipped: mandatory schema context exceeds %s chars",
                e.diagnostics.get("hard_max_chars"),
            )
            return {
                "error": str(e),
                "reason_code": e.reason_code,
                "schema_budget": {**e.diagnostics, "model_calls": 0},
                "query_entities": _redact_linking_value(
                    self._collect_entity_terms(entities)
                ),
            }
        except Exception as e:
            safe_error = _redact_linking_error(e)
            logger.warning("LLM linking failed: %s", safe_error)
            return {
                "error": f"LLM schema linking failed: {safe_error}",
                "suggestion": "Retry schema linking after fixing the LLM response or API failure.",
                "query_entities": _redact_linking_value(self._collect_entity_terms(entities)),
                "schema_budget": schema_budget,
            }


def _normalize_entity_list(
    items: Any,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized

    for item in items:
        if not isinstance(item, dict):
            continue
        table = item.get("table")
        column = item.get("column")
        if not isinstance(table, str) or not isinstance(column, str):
            continue
        resolved_table = _resolve_table_name(table, db_schema)
        if not resolved_table:
            logger.warning("LLM linked entity references unknown table: %s", table)
            continue
        resolved_column = _resolve_column_name(column, resolved_table, db_schema)
        if not resolved_column:
            logger.warning(
                "LLM linked entity references unknown column: %s.%s",
                table,
                column,
            )
            continue
        normalized_item = dict(item)
        normalized_item["table"] = resolved_table
        normalized_item["column"] = resolved_column
        normalized.append(normalized_item)
    return normalized


def _normalize_filter_map(
    filters: Any,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    if not isinstance(filters, dict):
        return normalized

    for filter_name, item in filters.items():
        normalized_items = _normalize_entity_list([item], db_schema)
        if normalized_items:
            normalized[filter_name] = normalized_items[0]
    return normalized


def _normalize_linked_entities(
    linked_entities: Any,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    if not isinstance(linked_entities, dict):
        return {}
    return {
        "metrics": _normalize_entity_list(linked_entities.get("metrics"), db_schema),
        "dimensions": _normalize_entity_list(linked_entities.get("dimensions"), db_schema),
        "filters": _normalize_filter_map(linked_entities.get("filters"), db_schema),
    }


def _has_linked_entities(linked_entities: Dict[str, Any]) -> bool:
    if not isinstance(linked_entities, dict):
        return False

    def has_binding(items: Any) -> bool:
        if not isinstance(items, list):
            return False
        return any(
            isinstance(item, dict) and bool(item.get("table")) and bool(item.get("column"))
            for item in items
        )

    filters = linked_entities.get("filters")
    has_filter_binding = isinstance(filters, dict) and any(
        isinstance(item, dict) and bool(item.get("table")) and bool(item.get("column"))
        for item in filters.values()
    )
    return (
        has_binding(linked_entities.get("metrics"))
        or has_binding(linked_entities.get("dimensions"))
        or has_filter_binding
    )
