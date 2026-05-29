"""
SQL Generator - генерация SQL запросов из linked entities.

EPIC 8.1: SQLGenerator превращён в тонкий orchestrator. Деревообразный билдер
переехал в `sql_builder.py`, диалект-специфичное квотирование — в
`sql_postprocess.py`. Здесь остался only orchestration, LLM I/O и backward-compat
shims на приватные методы (тесты вызывают `gen._apply_dialect_quoting`,
`gen._filter_value_conditions`, `gen._generate_from_linked_entities` и т.д.).

ВАЖНО: `call_openai_api` импортируется на module-level — тесты monkeypatch'ат
`custom_tools.text_to_sql.sql_generator.call_openai_api` напрямую.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

from utils import call_openai_api

from .dialects import get_current_dialect_label
from .validators import SQLSchemaValidator, SQLSafetyValidator
from .utils import dsn_to_sanitized_name, redact_text_to_sql_value
from . import sql_builder
from . import sql_postprocess


def _redact_sql_generation_value(value: Any) -> Any:
    return redact_text_to_sql_value(value)


class SQLGenerator:
    """Генератор SQL запросов из связанных сущностей."""

    def __init__(self):
        self.schema_validator = SQLSchemaValidator()
        self.safety_validator = SQLSafetyValidator()
        self.max_retries = int(os.getenv("TEXT_TO_SQL_MAX_RETRIES", "3"))

    # ----- LLM-конфиг и тексты промптов (yaml source of truth) -----
    # W6-T2/W6-T4: тексты промптов из config/text_to_sql/prompts.yaml.
    # W6-T3: max_tokens из config/text_to_sql/llm_models.yaml. В .py-файле
    # ни одного длинного текста промпта и ни одного магического числа.

    def _load_sql_generation_prompts(self) -> tuple[str, str]:
        """Возвращает (rules_text, system_prompt_template) из prompts.yaml.

        ``rules_text`` — строка вида "правило1; правило2; ..." готовая к
        подстановке в user prompt. ``system_prompt_template`` содержит
        плейсхолдер ``{dialect_label}``, который заполняется на месте
        вызова.
        """
        from .prompts_config import load_prompts_config

        profile = load_prompts_config()
        rules = profile.get_list("sql_generation", "user_prompt_rules")
        template = profile.get_text("sql_generation", "system_prompt")
        return "; ".join(rules), template

    def _sql_generation_max_tokens(self) -> int:
        """``max_tokens`` для LLM-вызовов sql_generator из llm_models.yaml."""
        from .llm_models_config import load_llm_models_config

        return int(load_llm_models_config().get("sql_generation", "max_tokens"))

    def _apply_safety_validation(
        self,
        sql_query: str,
        dsn: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Прогоняет финальный SQL через SQLSafetyValidator.

        Возвращает структуру ошибки в формате существующего error-path generate_sql,
        либо None если запрос безопасен.
        """
        if not sql_query:
            return None
        if dsn is None:
            safety_result = self.safety_validator.validate(sql_query)
        else:
            safety_result = self.safety_validator.validate(sql_query, dsn=dsn)
        if safety_result.get("is_safe", False):
            return None
        safety_issues = safety_result.get("issues", [])
        safe_safety_issues = _redact_sql_generation_value(safety_issues)
        logger.warning("SQL safety validation failed: %s", safe_safety_issues)
        return {
            "error": "Generated SQL failed safety validation",
            "safety_issues": safe_safety_issues,
            "sql_query": sql_query,
        }

    def _schema_validation_enabled(self) -> bool:
        return os.getenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "1").lower() not in {"0", "false", "no", "off"}

    def _get_schema_from_cache(
        self,
        dsn: str | None = None,
    ) -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
        """Получает схему БД из кэша памяти."""
        try:
            try:
                from memory.tools import get_memory
            except ImportError:
                logger.warning("Memory tools not available - cannot retrieve schema from cache")
                return None

            if not dsn:
                logger.warning("DSN is required - cannot retrieve schema from cache")
                return None

            session_id = dsn_to_sanitized_name(dsn)

            memory_results = get_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                include_historical=False
            )

            schema = {}
            for result in memory_results:
                if isinstance(result, dict):
                    data = result.get("data", {})
                    if isinstance(data, dict) and data.get("cache_kind") == "schema_table":
                        table_info = data.get("table_info")
                        if isinstance(table_info, dict):
                            table_name = table_info.get("table_name")
                            if table_name:
                                columns = {}
                                for col in table_info.get("columns", []):
                                    if isinstance(col, dict) and "name" in col:
                                        col_data = {
                                            "type": col.get("type", ""),
                                            "description": col.get("description", "")
                                        }
                                        for field in ["not_null", "default_value", "constraint_type", "references"]:
                                            if col.get(field):
                                                col_data[field] = col[field]
                                        columns[col["name"]] = col_data

                                schema[table_name] = {
                                    "description": table_info.get("description", ""),
                                    "columns": columns
                                }

            if schema:
                logger.info(f"Retrieved schema from cache: {len(schema)} tables")
                return schema
            else:
                logger.info("No schema found in cache")
                return None

        except Exception as e:
            logger.warning(f"Failed to retrieve schema from cache: {e}")
            return None

    def generate_sql(
        self,
        context: str,
        user_query: str,
        dsn: str | None = None,
    ) -> Dict[str, str]:
        """Генерирует SQL запрос из контекста и пользовательского запроса через прямой вызов LLM."""
        logger.info("Generating SQL query")

        safe_context_preview = _redact_sql_generation_value(context)
        if isinstance(safe_context_preview, str):
            safe_context_preview = safe_context_preview[:500]
        logger.info("Context received (redacted first 500 chars): %s", safe_context_preview if context else "None")
        logger.info(f"Context type: {type(context)}")
        logger.info(f"Context length: {len(context) if context else 0}")

        structured_context = self._parse_structured_context(context)
        effective_dsn = dsn or self._get_dsn_from_context(structured_context)
        linked_entities = self._get_linked_entities(structured_context)
        schema_from_context = self._get_schema_from_context(structured_context)
        schema_validation_enabled = self._schema_validation_enabled()
        db_schema = schema_from_context or self._get_schema_from_cache(effective_dsn)

        if os.getenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "0") == "1":
            if effective_dsn is None:
                structured_result = self._generate_from_linked_entities(structured_context)
            else:
                structured_result = self._generate_from_linked_entities(
                    structured_context,
                    dsn=effective_dsn,
                )
            if structured_result:
                if schema_validation_enabled and structured_result.get("sql_query") and not db_schema:
                    return {
                        "error": "Schema validation is enabled, but no database schema is available",
                    }
                structured_sql = structured_result.get("sql_query")
                if structured_sql and not structured_result.get("error"):
                    safety_error = self._apply_safety_validation(structured_sql, dsn=effective_dsn)
                    if safety_error is not None:
                        return safety_error
                elif structured_sql and structured_result.get("error"):
                    # Error-path с sql_query: SQL уже забракован выше (например,
                    # schema validation). Прогоняем safety, чтобы safety_issues
                    # пробросились — не оставляем unsafe SQL в propagated ошибке
                    # без флага.
                    safety_error = self._apply_safety_validation(structured_sql, dsn=effective_dsn)
                    if safety_error is not None:
                        merged = dict(structured_result)
                        merged.setdefault("safety_issues", safety_error.get("safety_issues", []))
                        return merged
                return structured_result
            return {
                "error": "Structured SQL builder is enabled, but structured context is missing or unsupported."
            }

        if schema_validation_enabled and not db_schema:
            return {
                "error": "Schema validation is enabled, but no database schema is available",
            }

        # Прямая LLM-генерация с контекстом без парсинга JSON
        for attempt in range(self.max_retries):
            logger.info(f"Direct LLM generation attempt {attempt + 1}/{self.max_retries}")

            if effective_dsn is None:
                sql_result = self._llm_generation_direct(context, user_query, attempt)
            else:
                sql_result = self._llm_generation_direct(
                    context,
                    user_query,
                    attempt,
                    dsn=effective_dsn,
                )
            if not sql_result:
                logger.debug(f"LLM returned empty result on attempt {attempt + 1}")
                continue

            if sql_result and isinstance(sql_result.get("sql_query"), str) and sql_result["sql_query"].strip():
                sql_query = sql_result["sql_query"]
                if linked_entities:
                    try:
                        if effective_dsn is None:
                            sql_query = self._apply_dialect_quoting(
                                sql_query,
                                linked_entities,
                            )
                        else:
                            sql_query = self._apply_dialect_quoting(
                                sql_query,
                                linked_entities,
                                dsn=effective_dsn,
                            )
                        sql_result["sql_query"] = sql_query
                    except RuntimeError as exc:
                        safe_error = str(_redact_sql_generation_value(str(exc)))
                        logger.warning("SQL dialect quoting failed on attempt %s: %s", attempt + 1, safe_error)
                        if attempt == self.max_retries - 1:
                            return {
                                "error": safe_error,
                                "sql_query": sql_query,
                            }
                        continue

                # Валидация против схемы БД
                if schema_validation_enabled:
                    schema_validation = self.schema_validator.validate_sql_against_schema(
                        sql_query,
                        db_schema,
                        dsn=effective_dsn,
                    )

                    if not schema_validation.get("is_valid", True):
                        schema_issues = schema_validation.get("issues", [])
                        logger.warning(f"SQL schema validation failed on attempt {attempt + 1}: {schema_issues}")

                        if self._schema_validation_requires_fail_fast(schema_issues) or attempt == self.max_retries - 1:
                            return {
                                "error": "Generated SQL failed schema validation",
                                "schema_issues": schema_issues,
                                "sql_query": sql_query
                            }
                        continue

                    logger.info("SQL generation and schema validation successful")
                elif not db_schema:
                    logger.warning(
                        "SQL generation completed but no schema was available for validation — result may be invalid"
                    )

                safety_error = self._apply_safety_validation(sql_query, dsn=effective_dsn)
                if safety_error is not None:
                    return safety_error

                return sql_result

        logger.error("All LLM generation attempts failed")
        return {"error": "Failed to generate SQL after all attempts"}

    def _parse_structured_context(self, context: str) -> Optional[Dict[str, Any]]:
        if isinstance(context, dict):
            return context
        if not isinstance(context, str) or not context.strip():
            return None
        try:
            parsed = json.loads(context)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _get_linked_entities(self, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(context, dict):
            return {}
        linked = context.get("linked_entities", context)
        return linked if isinstance(linked, dict) else {}

    def _get_schema_from_context(self, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
        if not isinstance(context, dict):
            return None
        schema_info = context.get("schema_info")
        return schema_info if isinstance(schema_info, dict) and schema_info else None

    def _get_dsn_from_context(self, context: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(context, dict):
            return None
        for key in ("dsn", "database_dsn", "db_dsn"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return value
        metadata = context.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("dsn")
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _schema_validation_requires_fail_fast(self, schema_issues: List[Dict[str, Any]]) -> bool:
        return any(
            isinstance(issue, dict)
            and issue.get("issue_type") == "SQLGLOT_DISABLED_FOR_SCHEMA_VALIDATION"
            for issue in schema_issues
        )

    # ----- Backward-compat shims: structured builder (EPIC 8.1) -----
    # Тесты дёргают эти методы как instance-методы. Каждый — 1-line делегат
    # в `sql_builder` с инъекцией `self.schema_validator`.

    def _generate_from_linked_entities(
        self,
        context: Optional[Dict[str, Any]],
        dsn: str | None = None,
    ) -> Dict[str, str]:
        return sql_builder.build_sql_from_linked_entities(
            context,
            schema_validator=self.schema_validator,
            dsn=dsn,
        )

    def _metric_aggregate(self, metric: Dict[str, Any]) -> Optional[str]:
        return sql_builder.metric_aggregate(metric)

    def _build_filter_clauses(
        self,
        filters: Any,
        available_tables: Optional[Set[str]] = None,
        dsn: str | None = None,
    ) -> Dict[str, Any]:
        return sql_builder.build_filter_clauses(filters, available_tables, dsn=dsn)

    def _filter_value_conditions(
        self,
        expr: str,
        value: Any,
        filter_info: Dict[str, Any],
        dsn: str | None = None,
    ) -> Optional[List[str]]:
        return sql_builder.filter_value_conditions(expr, value, filter_info, dsn=dsn)

    def _sql_literal(self, value: Any, dsn: str | None = None) -> str:
        return sql_builder.sql_literal(value, dsn=dsn)

    # ----- Backward-compat shims: post-processing quoting (EPIC 8.1) -----

    # Module-level regex остаётся доступен через классовый атрибут для
    # тестов, обращающихся к `SQLGenerator._SAFE_IDENTIFIER_RE`.
    _SAFE_IDENTIFIER_RE = sql_postprocess._SAFE_IDENTIFIER_RE

    @classmethod
    def _should_quote_name(
        cls,
        name: str,
        dialect_name: str,
        known_tables: Set[str],
        known_columns: Set[str],
        table_aliases: Set[str],
    ) -> bool:
        return sql_postprocess.should_quote_name(
            name, dialect_name, known_tables, known_columns, table_aliases
        )

    @staticmethod
    def _is_reserved_keyword(name: str, dialect_name: str) -> bool:
        return sql_postprocess.is_reserved_keyword(name, dialect_name)

    def _apply_dialect_quoting(
        self,
        sql_query: str,
        linked_entities: Dict[str, Any],
        dsn: str | None = None,
    ) -> str:
        return sql_postprocess.apply_dialect_quoting(sql_query, linked_entities, dsn=dsn)

    def _quote_via_ast(self, sql_query: str, linked_entities: Dict[str, Any], dialect: str) -> str:
        return sql_postprocess.quote_via_ast(sql_query, linked_entities, dialect)

    def _apply_manual_quoting(
        self,
        sql_query: str,
        linked_entities: Dict[str, Any],
        dsn: str | None = None,
    ) -> str:
        return sql_postprocess.apply_manual_quoting(sql_query, linked_entities, dsn=dsn)

    # ----- LLM I/O -----
    # `call_openai_api` намеренно используется как module-global, чтобы
    # monkeypatch.setattr("...sql_generator.call_openai_api", ...) перехватывал
    # вызов. Не превращать в self.<method>.

    def _llm_generation_direct(
        self,
        context: str,
        user_query: str,
        attempt: int,
        dsn: str | None = None,
    ) -> Dict[str, str]:
        """Прямая LLM-генерация SQL из текстового контекста без парсинга JSON."""
        try:
            dialect_label = get_current_dialect_label(dsn, strict=bool(dsn and str(dsn).strip()))

            rules_text, system_prompt_template = self._load_sql_generation_prompts()

            feedback_context = ""
            if attempt > 0:
                feedback_context = f"\n\nЭто попытка {attempt + 1}. Предыдущие попытки содержали ошибки валидации схемы. Убедись, что все таблицы и колонки существуют в схеме и правильно написаны."

            # ``.replace`` (а не ``.format``): system_prompt содержит литералы
            # ``{`` и ``}``, которые ``str.format`` примет за плейсхолдеры.
            system_prompt = (
                system_prompt_template.replace("{dialect_label}", dialect_label)
                + feedback_context
            )

            # json.dumps экранирует кавычки/переводы строк в user_query
            # (W6-T1: prompt injection protection).
            safe_user_query = _redact_sql_generation_value(user_query)
            escaped_user_query = json.dumps(safe_user_query, ensure_ascii=False)
            safe_context = _redact_sql_generation_value(context)
            # W2-T7 (security): safe_context экранируется через json.dumps, как и
            # user_query, чтобы вредоносные имена/описания колонок не инжектировали
            # инструкции в промпт.
            escaped_context = json.dumps(safe_context, ensure_ascii=False)
            prompt = (
                f"Сгенерируй ОДИН безопасный SQL SELECT по {dialect_label} на основе контекста. "
                f"Обязательные правила: {rules_text}; "
                "верни ТОЛЬКО JSON {\"sql_query\": \"...\"}.\n\n"
                f"Описание схемы БД: {escaped_context}\n"
                f"Запрос пользователя (в JSON-кавычках): {escaped_user_query}"
            )

            resp = call_openai_api(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=self._sql_generation_max_tokens(),
                response_format={"type": "json_object"}
            )

            from .utils import parse_llm_json_response
            parsed = parse_llm_json_response(resp)
            sql_query = self._extract_sql_query(parsed)
            if sql_query:
                return {"sql_query": sql_query}

        except Exception as e:
            logger.warning(
                "Direct LLM SQL generation failed on attempt %s: %s",
                attempt + 1,
                _redact_sql_generation_value(str(e)),
            )

        return {}

    def _extract_sql_query(self, parsed: Any) -> str:
        """Нормализует успешный LLM output к единому полю sql_query."""
        if not isinstance(parsed, dict):
            return ""
        value = parsed.get("sql_query")
        if value is None:
            value = parsed.get("sql")
        if not isinstance(value, str):
            return ""
        return value.strip()
