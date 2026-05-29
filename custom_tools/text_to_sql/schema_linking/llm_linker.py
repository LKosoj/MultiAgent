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
import os
from typing import Any, Callable, Dict, List, Optional

from memory.manager import EmbeddingUnavailableError, EmbeddingFailedError

from ..prompts import build_schema_linking_prompt
from ..utils import parse_llm_json_response

logger = logging.getLogger(__name__)


def _redact_linking_value(value: Any) -> Any:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

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
        try:
            entity_names = self._collect_entity_terms(entities)
            if not entity_names:
                return {
                    "error": "No schema-linking entity values provided",
                    "suggestion": "Pass canonical entities with metrics, dimensions, or filters.",
                    "query_entities": [],
                }

            relevant_tables = self.memory_manager.find_semantic_relevant_tables(
                entity_names,
                dsn=dsn,
            )
            logger.info(
                f"Found {len(relevant_tables)} relevant tables for LLM linking: {relevant_tables}"
            )
            memory_status = getattr(self.memory_manager, "last_search_status", None)
            memory_error = getattr(self.memory_manager, "last_search_error", None)
            safe_memory_error = str(_redact_linking_value(memory_error or "unknown reason"))
            if not relevant_tables and memory_status == "memory_unavailable":
                return {
                    "error": f"Schema memory unavailable: {safe_memory_error}",
                    "memory_status": memory_status,
                    "suggestion": "Ensure schema_table records are indexed before schema linking.",
                    "query_entities": _redact_linking_value(entity_names),
                }

            filtered_schema: Dict[str, Any] = {}
            for table_name in relevant_tables:
                if table_name in db_schema:
                    filtered_schema[table_name] = db_schema[table_name]

            if not filtered_schema:
                n_fallback = int(os.getenv("SCHEMA_LLM_USE_FIRST_N_FALLBACK", "0") or "0")
                if n_fallback > 0:
                    logger.warning(
                        f"No relevant tables found via memory search, "
                        f"using first {n_fallback} tables as fallback"
                    )
                    table_names = list(db_schema.keys())[:n_fallback]
                    for table_name in table_names:
                        filtered_schema[table_name] = db_schema[table_name]
                else:
                    # T5-linking / LOW: если memory вернула таблицы, которых нет
                    # в db_schema — это признак устаревшего индекса или переименования.
                    # Даём явный диагностический сигнал.
                    if relevant_tables:
                        logger.warning(
                            "Memory suggested tables %s but none found in db_schema "
                            "(schema tables sample: %s); possible schema rename or stale "
                            "index — re-index schema",
                            relevant_tables[:10],
                            list(db_schema.keys())[:10],
                        )
                    else:
                        logger.warning(
                            "No relevant tables via memory and fallback disabled; "
                            "schema likely lacks data for this query domain"
                        )
                    memory_status = getattr(self.memory_manager, "last_search_status", None)
                    memory_error = getattr(self.memory_manager, "last_search_error", None)
                    return {
                        "error": "No semantically relevant tables found",
                        "memory_status": memory_status or "no_hits",
                        "memory_error": _redact_linking_value(memory_error),
                        "suggestion": (
                            "Database schema may not contain data relevant to the requested "
                            "query domain"
                        ),
                        "available_tables": list(db_schema.keys())[:10],
                        "query_entities": _redact_linking_value(entity_names),
                    }

            logger.info(
                f"Using {len(filtered_schema)} tables for LLM linking "
                f"(from {len(db_schema)} total)"
            )

            schema_str = self.schema_limiter.build_schema_summary(filtered_schema)
            prompt = build_schema_linking_prompt(entities, schema_str, dsn=dsn)

            logger.info(f"Schema linking prompt length: {len(prompt)}")
            logger.debug(
                "Schema linking prompt preview: %s...",
                _redact_linking_value(prompt[:300]),
            )

            # active_llm_caller проверен в начале llm_linking (fail-fast).
            # Здесь дополнительной проверки не нужно: caller гарантированно callable.
            call_openai_api = self.active_llm_caller()
            # max_tokens приходит из llm_models.yaml (4.17).
            from ..llm_models_config import load_llm_models_config

            llm_profile = load_llm_models_config()
            max_tokens = llm_profile.get("schema_linking", "max_tokens")

            # system_prompt приходит из prompts.yaml (W6-T2): runtime-код не
            # содержит длинных промпт-текстов — единый source of truth.
            from ..prompts_config import load_prompts_config

            prompts_profile = load_prompts_config()
            system_prompt = prompts_profile.get_text("schema_linking", "system_prompt")

            resp = call_openai_api(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )

            parsed = parse_llm_json_response(resp)

            logger.info(f"LLM schema linking response length: {len(resp) if resp else 0}")
            logger.info(f"LLM parsed result type: {type(parsed)}")

            if isinstance(parsed, dict) and "linked_entities" in parsed:
                joins_from_llm = parsed.get("joins", [])
                logger.info(f"LLM returned {len(joins_from_llm)} joins: {joins_from_llm}")
                linked_entities = parsed.get("linked_entities", {})
                if not _has_linked_entities(linked_entities):
                    return {
                        "error": "LLM schema linking returned no linked entities",
                        "linked_entities": linked_entities if isinstance(linked_entities, dict) else {},
                        "joins": joins_from_llm,
                        "unlinked_entities": parsed.get("unlinked_entities", []),
                    }

                return {
                    "linked_entities": linked_entities,
                    "joins": joins_from_llm,
                    "unlinked_entities": parsed.get("unlinked_entities", []),
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
                }
        except (EmbeddingUnavailableError, EmbeddingFailedError) as e:
            # T3: find_semantic_relevant_tables пробрасывает эти ошибки, когда
            # модель эмбеддингов не настроена/недоступна. Не маскируем их под
            # generic "LLM schema linking failed" — даём caller'у (orchestrator)
            # явную причину и actionable-подсказку. Orchestrator по наличию
            # ключа "error" уйдёт в heuristic-fallback (string matching), но
            # причина теперь видима в логах/ответе, а не молча проглочена.
            safe_error = _redact_linking_error(e)
            logger.warning(
                "LLM linking: модель эмбеддингов недоступна для поиска "
                "релевантных таблиц: %s",
                safe_error,
            )
            return {
                "error": f"Schema memory unavailable (embeddings): {safe_error}",
                "memory_status": "embedding_unavailable",
                "suggestion": "Check OPENAI_API_KEY_DB / embedding config; ensure schema_table records are indexed.",
                "query_entities": _redact_linking_value(self._collect_entity_terms(entities)),
            }
        except Exception as e:
            safe_error = _redact_linking_error(e)
            logger.warning("LLM linking failed: %s", safe_error)
            return {
                "error": f"LLM schema linking failed: {safe_error}",
                "suggestion": "Retry schema linking after fixing the LLM response or API failure.",
                "query_entities": _redact_linking_value(self._collect_entity_terms(entities)),
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

    return has_binding(linked_entities.get("metrics")) or has_binding(
        linked_entities.get("dimensions")
    )
