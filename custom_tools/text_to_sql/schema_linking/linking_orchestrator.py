"""
Schema linking orchestrator — композиция heuristic / LLM / join-validator.

Расщеплено из ``strategies.py`` (EPIC 8.2). ``SchemaLinkingCore`` теперь
тонкий фасад, делегирующий heuristic-pipeline в ``HeuristicLinker``,
LLM-pipeline в ``LLMLinker``, join-валидацию в ``JoinValidator``.

Публичный API сохранён: ``perform_linking``, ``llm_linking``,
``heuristic_linking``, ``find_main_table``, ``best_column_for``,
``validate_llm_joins``, ``join_builder`` property — все доступны на
инстансе ``SchemaLinkingCore``.

Boilerplate-делегаторы для underscore-методов резолверов
(``_resolve_table_name`` и т.д.) удалены — у них не было callers
ни в production code, ни в тестах. Использовать функции напрямую
через ``schema_linking.resolution``.
"""
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from .heuristic_linker import HeuristicLinker
from .join_validation import JoinValidator, compute_required_tables
from .llm_linker import LLMLinker, _has_linked_entities

logger = logging.getLogger(__name__)


class SchemaLinkingCore:
    """Основной фасад связывания сущностей со схемой.

    Композирует:
      * :class:`HeuristicLinker` — heuristic pipeline,
      * :class:`LLMLinker` — LLM pipeline,
      * :class:`JoinValidator` — построение и валидация joins.
    """

    def __init__(
        self,
        schema_limiter,
        memory_manager,
        llm_caller: Optional[Callable[..., Any]] = None,
    ):
        self.schema_limiter = schema_limiter
        self.memory_manager = memory_manager
        self._heuristic = HeuristicLinker(memory_manager)
        self._llm = LLMLinker(
            schema_limiter=schema_limiter,
            memory_manager=memory_manager,
            entity_term_collector=self._heuristic.collect_entity_terms,
            llm_caller=llm_caller,
        )
        self._join_validator = JoinValidator()

    # ------------------------------------------------------------------
    # Backward-compat property: join_builder
    # ------------------------------------------------------------------
    @property
    def join_builder(self):  # pragma: no cover - simple delegator
        return self._join_validator.join_builder

    @join_builder.setter
    def join_builder(self, value):  # pragma: no cover - simple delegator
        self._join_validator.join_builder = value

    # ------------------------------------------------------------------
    # Public heuristic-facade forwards
    # ------------------------------------------------------------------
    def heuristic_linking(self, entities, db_schema, dsn: Optional[str] = None):
        return self._heuristic.heuristic_linking(entities, db_schema, dsn=dsn)

    def find_main_table(self, db_schema, semantic_tables=None):
        return self._heuristic.find_main_table(db_schema, semantic_tables)

    def best_column_for(self, name, table, table_schema):
        return self._heuristic.best_column_for(name, table, table_schema)

    def link_filters(self, filters_in, linked_dimensions, main_table, db_schema):
        return self._heuristic.link_filters(
            filters_in, linked_dimensions, main_table, db_schema
        )

    # ------------------------------------------------------------------
    # Public LLM-facade forwards
    # ------------------------------------------------------------------
    def llm_linking(self, entities, db_schema, dsn: Optional[str] = None):
        return self._llm.llm_linking(entities, db_schema, dsn=dsn)

    # ------------------------------------------------------------------
    # Public join-validator forwards
    # ------------------------------------------------------------------
    def validate_llm_joins(self, llm_joins, db_schema):
        return self._join_validator.validate_llm_joins(llm_joins, db_schema)

    def validate_join(
        self,
        join: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Публичный делегат к JoinValidator._is_join_valid_against_schema.

        T5-linking / #12 MEDIUM: предоставляет стабильный публичный API для
        single-join валидации, чтобы внешние shim'ы (schema_linker.py) не
        обращались к приватному _join_validator напрямую.
        """
        return self._join_validator._is_join_valid_against_schema(join, db_schema)

    def build_joins(
        self, linked_metrics, linked_dimensions, linked_filters, db_schema,
        main_table=None,
    ):
        """W4-T1: ``main_table`` опциональный параметр (передаётся caller'ом).

        Backward-compat: при ``main_table=None`` поведение остаётся прежним
        (JoinValidator сам берёт первую metric).
        """
        return self._join_validator.build_joins(
            linked_metrics, linked_dimensions, linked_filters, db_schema,
            main_table=main_table,
        )

    # ------------------------------------------------------------------
    # Linking orchestration
    # ------------------------------------------------------------------
    def _has_linked_entities(self, linked_entities: Dict[str, Any]) -> bool:
        return _has_linked_entities(linked_entities)

    def perform_linking(
        self,
        entities: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Выполняет связывание сущностей со схемой."""
        from ..schema_loader import SchemaFilter

        db_schema = SchemaFilter.filter_schema_by_include_list(db_schema)

        linked_metrics: List[Dict[str, Any]] = []
        linked_dimensions: List[Dict[str, Any]] = []
        linked_filters: Dict[str, Any] = {}
        unlinked: List[str] = []

        llm_joins: List[Dict[str, Any]] = []
        allow_fallbacks = os.getenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "0") == "1"

        # Флаги для linking_strategy в итоговом результате:
        # llm_succeeded — LLM-вызов отработал и вернул linked_entities;
        # heuristic_used — heuristic-fallback фактически выполнился.
        llm_succeeded = False
        heuristic_used = False

        # 4.12: heuristic-fallback вызывается ровно один раз — в явных
        # точках, где это осмысленно, и только при allow_fallbacks=1.
        def _try_heuristic_fallback(reason: str) -> bool:
            nonlocal linked_metrics, linked_dimensions, linked_filters, unlinked
            nonlocal heuristic_used
            if not allow_fallbacks:
                return False
            (
                linked_metrics,
                linked_dimensions,
                linked_filters,
                unlinked,
            ) = self.heuristic_linking(entities, db_schema, dsn=dsn)
            heuristic_used = True
            logger.info(
                "Heuristic schema-linking fallback engaged after: %s", reason
            )
            return True

        if self._llm.active_llm_caller() and db_schema and os.getenv(
            "SCHEMA_LINKING_USE_LLM", "1"
        ) != "0":
            llm_result = self.llm_linking(entities, db_schema, dsn=dsn)
            if not llm_result:
                message = "LLM schema linking returned no result"
                # HIGH #6: `unlinked` здесь содержит DIAGNOSTIC MESSAGE
                # (не имя сущности). Формат поля гибридный — см. финальный
                # return: ключ `unlinked_entities` сохраняет публичный
                # контракт API (список строк, любая семантика).
                unlinked = [message]
                logger.warning(message)
                _try_heuristic_fallback(message)
            elif llm_result.get("error"):
                # HIGH #6: DIAGNOSTIC MESSAGE (error string).
                unlinked = [llm_result["error"]]
                logger.warning("LLM schema linking failed: %s", llm_result["error"])
                _try_heuristic_fallback(llm_result["error"])
            else:
                linked_entities = llm_result.get("linked_entities", {})
                if not self._has_linked_entities(linked_entities):
                    message = "LLM schema linking returned no linked entities"
                    # HIGH #6: смешанный формат — либо ENTITY NAMES от LLM
                    # (если llm_result их вернул), либо DIAGNOSTIC MESSAGE
                    # как единственный элемент. Потребители трактуют как
                    # opaque список строк.
                    unlinked = llm_result.get("unlinked_entities") or [message]
                    logger.warning(message)
                    _try_heuristic_fallback(message)
                else:
                    linked_metrics = linked_entities.get("metrics", [])
                    linked_dimensions = linked_entities.get("dimensions", [])
                    linked_filters = linked_entities.get("filters", {})
                    llm_joins = llm_result.get("joins", [])
                    # HIGH #6: ENTITY NAMES — имена сущностей, которые LLM
                    # явно не смогла прилинковать к схеме.
                    unlinked = llm_result.get("unlinked_entities", [])
                    llm_succeeded = True
        else:
            # HIGH #4: ветка `if not (linked_metrics or linked_dimensions)`
            # удалена — на этой точке linked_metrics/dimensions гарантированно
            # пусты (инициализированы выше и до сюда не модифицируются).
            # Различаем три причины ухода в else: для логов/аудита это разные failure modes.
            use_llm_flag = os.getenv("SCHEMA_LINKING_USE_LLM", "1")
            if use_llm_flag == "0":
                cause = "SCHEMA_LINKING_USE_LLM=0 (explicitly disabled)"
            elif not self._llm.active_llm_caller():
                cause = "llm_caller is not configured"
            elif not db_schema:
                cause = "db_schema is empty"
            else:
                cause = "unknown"
            gate_message = f"LLM schema linking unavailable or disabled: {cause}"
            if not _try_heuristic_fallback(gate_message):
                message = (
                    f"{gate_message}; heuristic fallbacks are disabled"
                )
                # HIGH #6: DIAGNOSTIC MESSAGE.
                unlinked = [message]
                logger.warning(message)

        if llm_joins:
            validated_joins = self.validate_llm_joins(llm_joins, db_schema)
            # W4-T2: required_tables учитывают и validated_joins (bridge-таблицы
            # из FK-цепочек должны попасть в required, иначе success ошибочно
            # репортит unconnected на собственный bridge).
            required_tables = compute_required_tables(
                linked_metrics, linked_dimensions, linked_filters,
                validated_joins=validated_joins,
            )
            main_table = self._pick_main_table_from_linked(
                linked_metrics, linked_dimensions, required_tables, db_schema
            )
            unconnected_tables = self._unconnected_tables_from_joins(
                main_table, required_tables, validated_joins
            )
            # W4-T3: единый критерий success — все required связаны, т.е.
            # main_table выбран и unconnected_tables пуст. Раньше для LLM
            # path дополнительно требовалось ``len(validated)==len(llm_joins)``,
            # что после hybrid fallback ослаблялось; теперь критерий один и
            # тот же для обоих веток (см. W4-T3 / AGENTS.md: silent fallback
            # запрещён).
            joins_info = {
                "joins": validated_joins,
                "success": bool(main_table) and not unconnected_tables,
                "unconnected_tables": unconnected_tables,
                "main_table": main_table,
            }

            if len(validated_joins) < len(llm_joins):
                logger.warning(
                    f"Some LLM joins failed validation: "
                    f"{len(llm_joins) - len(validated_joins)} invalid"
                )
                if allow_fallbacks:
                    # W4-T1: передаём main_table параметром — JoinValidator
                    # не знает про scoring.
                    fallback_joins = self.build_joins(
                        linked_metrics, linked_dimensions, linked_filters,
                        db_schema, main_table=main_table,
                    )
                    for fallback_join in fallback_joins["joins"]:
                        if not JoinValidator._is_duplicate_join(fallback_join, validated_joins):
                            validated_joins.append(fallback_join)
                    joins_info["joins"] = validated_joins
                    # W4-T2: после hybrid merge — пересчитываем required с учётом
                    # новых bridge-таблиц.
                    required_tables = compute_required_tables(
                        linked_metrics, linked_dimensions, linked_filters,
                        validated_joins=validated_joins,
                    )
                    joins_info["unconnected_tables"] = self._unconnected_tables_from_joins(
                        main_table, required_tables, validated_joins
                    )
                    # W4-T3: тот же единый критерий success.
                    joins_info["success"] = (
                        bool(main_table) and not joins_info["unconnected_tables"]
                    )
                    # MEDIUM #7: LLM-результат + heuristic build_joins для
                    # восполнения отбракованных joins → реальный hybrid.
                    # Без этого флага HIGH #5 "hybrid" был бы недостижим.
                    heuristic_used = True
        else:
            if allow_fallbacks:
                # W4-T1: вычисляем main_table через scoring в orchestrator и
                # передаём в JoinValidator. JoinValidator больше не выбирает
                # main_table сам (раньше — первая metric).
                pre_required = compute_required_tables(
                    linked_metrics, linked_dimensions, linked_filters
                )
                main_table = self._pick_main_table_from_linked(
                    linked_metrics, linked_dimensions, pre_required, db_schema
                )
                joins_info = self.build_joins(
                    linked_metrics, linked_dimensions, linked_filters,
                    db_schema, main_table=main_table,
                )
                # W4-T3: единый критерий success.
                joins_info["success"] = (
                    bool(joins_info.get("main_table"))
                    and not joins_info.get("unconnected_tables")
                )
                # build_joins — это heuristic-движок join-валидатора;
                # его явное использование тоже считается heuristic-вкладом.
                heuristic_used = True
            else:
                linked_tables = compute_required_tables(
                    linked_metrics, linked_dimensions, linked_filters
                )
                main_table = self._pick_main_table_from_linked(
                    linked_metrics, linked_dimensions, linked_tables, db_schema
                )
                unconnected_tables = self._unconnected_tables_from_joins(
                    main_table, linked_tables, []
                )
                # W4-T3: единый критерий success — main_table выбран и
                # unconnected_tables пуст. Раньше тут было
                # ``bool(linked_tables) and not unconnected_tables`` — это
                # тот же критерий с учётом, что при main_table=None
                # _unconnected_tables_from_joins возвращает все required.
                joins_info = {
                    "joins": [],
                    "success": bool(main_table) and not unconnected_tables,
                    "unconnected_tables": unconnected_tables,
                    "main_table": main_table,
                }

        # Определяем фактически использованную стратегию.
        # hybrid — LLM отдал результат И heuristic дополнительно подключался
        # (merge join'ов через build_joins после llm-success — см. MEDIUM #7).
        # llm — только LLM. heuristic — только heuristic-fallback / build_joins.
        # none — ничего не отработало.
        # HIGH #5: ветка "fallback" удалена как dead code: при текущей логике
        # любой output формируется либо через llm_succeeded, либо через
        # heuristic_used (heuristic_linking или build_joins), поэтому
        # `has_output && !llm_succeeded && !heuristic_used` структурно
        # недостижимо.
        if llm_succeeded and heuristic_used:
            linking_strategy = "hybrid"
        elif llm_succeeded:
            linking_strategy = "llm"
        elif heuristic_used:
            linking_strategy = "heuristic"
        else:
            linking_strategy = "none"

        # T5-linking / #12 LOW: filters-only результат тоже валиден —
        # не выставляем error если прилинкованы хотя бы filters.
        has_linked_filters = bool(
            linked_filters
            and any(
                isinstance(v, dict) and v.get("table") and v.get("column")
                for v in linked_filters.values()
            )
        )
        return {
            "linked_entities": {
                "metrics": linked_metrics,
                "dimensions": linked_dimensions,
                "filters": linked_filters,
            },
            "joins": joins_info["joins"],
            "join_success": joins_info["success"],
            "unconnected_tables": joins_info["unconnected_tables"],
            "main_table": joins_info["main_table"],
            "unlinked_entities": unlinked,
            "linking_strategy": linking_strategy,
            "error": unlinked[0] if unlinked and not self._has_linked_entities({
                "metrics": linked_metrics,
                "dimensions": linked_dimensions,
                "filters": linked_filters,
            }) and not has_linked_filters else None,
        }

    def _pick_main_table_from_linked(
        self,
        linked_metrics: List[Dict[str, Any]],
        linked_dimensions: List[Dict[str, Any]],
        required_tables: set,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Optional[str]:
        """Выбирает main_table через scoring (4.16).

        W4-T5: fallback при scored=None ограничен пересечением
        ``required_tables ∩ db_schema.keys()``: иначе мы могли вернуть
        first-linked-entity table, отсутствующую в db_schema (т.е.
        отсутствующую в фактически валидной схеме), и downstream
        получал бы main_table, на который потом строить SQL нельзя.
        Tie-break: алфавитно (детерминированно).
        """
        if not required_tables:
            return None

        candidate_schema = {
            table_name: db_schema[table_name]
            for table_name in required_tables
            if table_name in db_schema
        }
        if not candidate_schema:
            # Ни одна required-таблица не присутствует в db_schema —
            # main_table выбрать нельзя. Раньше silent-возврат первой
            # linked-таблицы тут давал invalid main_table в downstream.
            logger.warning(
                "_pick_main_table_from_linked: ни одна из required_tables "
                "не присутствует в db_schema (required=%s); main_table=None",
                sorted(required_tables),
            )
            return None

        scored = self.find_main_table(
            candidate_schema,
            semantic_tables=sorted(required_tables),
        )
        if scored:
            return scored

        # W4-T5 fallback: берём только таблицы из required_tables, которые
        # реально есть в db_schema, и tie-break — алфавитно.
        eligible = sorted(candidate_schema.keys())
        if not eligible:
            return None
        chosen = eligible[0]
        logger.warning(
            "_pick_main_table_from_linked: find_main_table вернул None "
            "(все linked-таблицы score < min_score_for_pick); "
            "используем алфавитно-первую таблицу из required ∩ db_schema "
            "как детерминированный fallback: %s",
            chosen,
        )
        return chosen

    def _linked_required_tables(
        self,
        linked_metrics: List[Dict[str, Any]],
        linked_dimensions: List[Dict[str, Any]],
        linked_filters: Dict[str, Any],
    ) -> set:
        """W4-T2: тонкая обёртка над :func:`compute_required_tables` (без joins).

        Делегат сохранён ради внутренних call-sites и читаемости пайплайна.
        """
        return compute_required_tables(
            linked_metrics, linked_dimensions, linked_filters
        )

    def _unconnected_tables_from_joins(
        self,
        main_table: Optional[str],
        required_tables: set,
        joins: List[Dict[str, Any]],
    ) -> List[str]:
        if not required_tables:
            return []
        if not main_table:
            return sorted(required_tables)
        reachable = {main_table}
        changed = True
        while changed:
            changed = False
            for join in joins:
                left = join.get("from_table")
                right = join.get("to_table")
                if left in reachable and right and right not in reachable:
                    reachable.add(right)
                    changed = True
                if right in reachable and left and left not in reachable:
                    reachable.add(left)
                    changed = True
        return sorted(table for table in required_tables if table not in reachable)
