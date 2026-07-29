"""
Join-validation and join-building logic for schema linking.

Split out of the legacy ``schema_linking_core.py`` as part of Phase 7 (T7.3)
decomposition. The validator owns:

  * ``build_joins``                 — heuristic FK-based join builder
  * ``validate_llm_joins``          — checks LLM-suggested joins against schema
  * ``_is_join_valid_against_schema`` — single-join validation entry point
  * ``_is_duplicate_join``          — set-like dedup of join dicts
  * ``_extract_fk_joins``           — pulls FK relationships from schema
  * ``_parse_fk_reference``         — parses ``table(col)`` / ``table.col`` syntax
  * ``_parse_fk_reference_table``   — convenience wrapper returning table part

Resolution of table/column names is delegated to :mod:`resolution`.
"""
import logging
import warnings
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from workflow.deadline import WorkflowDeadlineExceeded

from ..deprecations import TextToSQLDeprecationWarning
from ..join_builder import INVERSE_JOIN_TYPES, SYMMETRIC_JOIN_TYPES, JoinBuilder
from ..join_config import resolve_max_terminals, resolve_path_algo
from ..join_path import JoinPathTooManyTerminals, build_join_path
from ..schema_metadata import (
    ColumnMetadataHelper,
    get_foreign_key_constraints,
    get_type,
    is_fk,
)
from ..utils import get_runtime_context_dsn
from .resolution import (
    _resolve_column_name,
    _resolve_table_name,
)

logger = logging.getLogger(__name__)

# Базовые веса рёбер для граф-джойна (раздел 3.1 дизайна). Меньше = лучше.
_BASE_WEIGHT = {
    "fk": 1.0,
    "bridge": 1.0,
    "convention_fk": 3.0,
    "convention_legacy": 5.0,
}


def compute_required_tables(
    linked_metrics: List[Dict[str, Any]],
    linked_dimensions: List[Dict[str, Any]],
    linked_filters: Dict[str, Any],
    validated_joins: Optional[List[Dict[str, Any]]] = None,
) -> set:
    """W4-T2: единое определение required-таблиц для schema-linking pipeline.

    Базовое множество — таблицы из linked metrics/dimensions/filters. Если
    переданы ``validated_joins``, в required дополнительно попадают bridge-
    таблицы (from_table/to_table обоих концов каждого ребра): без них
    транзитивная связность не достижима, и downstream `success` ошибочно
    репортит unconnected.

    Используется в:
      * ``linking_orchestrator._linked_required_tables`` (linked-only вариант),
      * ``JoinValidator.build_joins`` (entities + filters),
      * ``SchemaContextBuilder.build_relevant_schema_context`` (entities + joins).

    Контракт: возвращается ``set[str]``; ``None``/нестроки/пустые имена
    отфильтрованы.
    """
    required: set = set()

    for entity in list(linked_metrics or []) + list(linked_dimensions or []):
        if isinstance(entity, dict):
            table = entity.get("table")
            if isinstance(table, str) and table:
                required.add(table)

    if isinstance(linked_filters, dict):
        for filter_item in linked_filters.values():
            if isinstance(filter_item, dict):
                table = filter_item.get("table")
                if isinstance(table, str) and table:
                    required.add(table)

    if validated_joins:
        for join in validated_joins:
            if not isinstance(join, dict):
                continue
            for key in ("from_table", "to_table"):
                table = join.get(key)
                if isinstance(table, str) and table:
                    required.add(table)

    return required


def _has_cycle(joins: List[Dict[str, Any]]) -> bool:
    """Проверяет, образуют ли joins цикл в неориентированном графе таблиц.

    JOIN'ы трактуются как неориентированные рёбра (A↔B), потому что
    SQL-JOIN симметричен по семантике (направление "from"/"to" — стилистика).
    DFS-обход с rec_stack; вход обратно в parent игнорируется (это не цикл,
    а просто симметричное ребро в неориентированном графе).
    """
    graph: Dict[str, set] = defaultdict(set)
    for j in joins:
        a = j.get("from_table")
        b = j.get("to_table")
        if not a or not b:
            continue
        if a == b:
            # Self-loop (A.x = A.y) — обычно индикатор ошибки в LLM-выводе
            # или конфликта алиасов. Cycle-DFS такие рёбра не находит
            # (фильтровались тихо), поэтому логируем явно.
            logger.warning(
                "Self-loop join detected: %s.%s = %s.%s",
                a,
                j.get("from_column"),
                a,
                j.get("to_column"),
            )
            continue
        graph[a].add(b)
        graph[b].add(a)

    visited: set = set()

    for start in list(graph.keys()):
        if start in visited:
            continue
        # Итеративный DFS с явным стеком: (node, parent).
        # rec_stack хранит узлы на текущем пути от корня — аналог
        # call-stack рекурсивной версии.
        stack = [(start, None)]
        rec_stack: set = set()
        while stack:
            node, parent = stack[-1]
            if node not in visited:
                visited.add(node)
                rec_stack.add(node)
            # Ищем необработанного соседа
            advanced = False
            for neighbor in graph[node]:
                if neighbor == parent:
                    continue
                if neighbor in rec_stack:
                    return True
                if neighbor not in visited:
                    stack.append((neighbor, node))
                    advanced = True
                    break
            if not advanced:
                rec_stack.discard(node)
                stack.pop()
    return False


def _parse_fk_reference(references: str) -> Tuple[Optional[str], Optional[str]]:
    """Парсит ссылку FK для получения таблицы и колонки.

    Поддерживаемые форматы:
      * ``table_name(column_name)``
      * ``table_name.column_name``
      * ``table_name`` (предполагается PK == ``id``)
    """
    try:
        if not references or not isinstance(references, str):
            return None, None

        references = references.strip()

        if "(" in references and references.endswith(")"):
            table_part, col_part = references.split("(", 1)
            ref_table = table_part.strip()
            ref_column = col_part.rstrip(")").strip()
            return ref_table, ref_column

        if "." in references:
            parts = references.rsplit(".", 1)
            if len(parts) == 2:
                ref_table = parts[0].strip()
                ref_column = parts[1].strip()
                return ref_table, ref_column

        return references.strip(), "id"

    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to parse FK reference '%s': %s", references, e)
        return None, None


def _parse_fk_reference_table(references: str) -> Optional[str]:
    """Возвращает только имя таблицы из FK-references (без колонки)."""
    if not references or not isinstance(references, str):
        return None
    ref_table, _ref_column = _parse_fk_reference(references)
    return ref_table


class JoinValidator:
    """Building and validating joins between tables in a database schema."""

    def __init__(
        self,
        schema_limiter: Any = None,
        memory_manager: Any = None,
        **legacy_kwargs: Any,
    ) -> None:
        # 4.22: schema_limiter / memory_manager убраны из бизнес-логики —
        # они были зарезервированы «на будущее», но не использовались.
        # Сигнатура оставлена ради backward-compat (легаси-вызовы могли
        # передавать их позиционно или по имени). При передаче ненулевых
        # значений — DeprecationWarning. ``**legacy_kwargs`` глотает прочие
        # old-args с тем же warning'ом.
        if schema_limiter is not None:
            warnings.warn(
                "JoinValidator.__init__ больше не использует schema_limiter; "
                "аргумент игнорируется и будет удалён в будущей версии.",
                TextToSQLDeprecationWarning,
                stacklevel=2,
            )
        if memory_manager is not None:
            warnings.warn(
                "JoinValidator.__init__ больше не использует memory_manager; "
                "аргумент игнорируется и будет удалён в будущей версии.",
                TextToSQLDeprecationWarning,
                stacklevel=2,
            )
        if legacy_kwargs:
            warnings.warn(
                "JoinValidator.__init__: неизвестные legacy-аргументы "
                f"{sorted(legacy_kwargs.keys())} проигнорированы.",
                TextToSQLDeprecationWarning,
                stacklevel=2,
            )
        self.join_builder: Optional[JoinBuilder] = None

    # ------------------------------------------------------------------
    # FK extraction & inference
    # ------------------------------------------------------------------
    def _extract_fk_joins(
        self,
        required_tables: set,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        *,
        include_all_bridges: bool = False,
    ) -> List[Dict[str, Any]]:
        """Извлекает возможные JOIN'ы на основе FK связей в схеме.

        4.21: для пары required-таблиц без прямого FK между ними ищется
        single-hop bridge-таблица X (любая таблица в ``db_schema``, у
        которой есть FK на обе required-таблицы). Тогда добавляются рёбра
        ``A↔X↔B`` с флагом ``via_bridge=True``. Глубина 1.
        """
        joins: List[Dict[str, Any]] = []

        def constraint_join(
            source_table: str,
            constraint: Dict[str, object],
            *,
            legacy: bool,
            via_bridge: bool = False,
        ) -> Dict[str, Any]:
            pairs = constraint["column_pairs"]
            if not isinstance(pairs, list) or not pairs:
                raise ValueError("validated foreign key has no column pairs")
            first = pairs[0]
            if not isinstance(first, dict):
                raise TypeError("validated foreign key pair must be a mapping")
            join: Dict[str, Any] = {
                "from_table": source_table,
                "from_column": first["from_column"],
                "to_table": constraint["to_table"],
                "to_column": first["to_column"],
                "join_type": "INNER",
            }
            if not legacy:
                join["constraint_id"] = constraint["constraint_id"]
                join["column_pairs"] = [dict(pair) for pair in pairs]
            if via_bridge:
                join["via_bridge"] = True
            return join

        # 1) Прямые FK от required-таблиц.
        for table_name in required_tables:
            resolved_table = _resolve_table_name(table_name, db_schema)
            if not resolved_table:
                continue

            table_body = db_schema[resolved_table]
            legacy = "foreign_keys" not in table_body
            for constraint in get_foreign_key_constraints(resolved_table, db_schema):
                joins.append(
                    constraint_join(
                        resolved_table,
                        constraint,
                        legacy=legacy,
                    )
                )

        # 2) 4.21: bridge-инференс. Для пары required-таблиц (A, B) без
        # прямого FK между ними ищем X в db_schema, у которой FK->A и
        # FK->B. Добавляем рёбра X->A и X->B (помечая via_bridge).
        resolved_required: List[str] = [
            t for t in (
                _resolve_table_name(name, db_schema) for name in required_tables
            ) if t
        ]
        if len(resolved_required) >= 2:
            direct_pairs: set = set()
            for j in joins:
                direct_pairs.add(
                    frozenset((j.get("from_table"), j.get("to_table")))
                )

            # required pairs, у которых нет прямого FK ни в одну, ни в другую сторону
            unconnected_pairs: List[Tuple[str, str]] = []
            for i in range(len(resolved_required)):
                for k in range(i + 1, len(resolved_required)):
                    a, b = resolved_required[i], resolved_required[k]
                    if frozenset((a, b)) in direct_pairs:
                        continue
                    unconnected_pairs.append((a, b))

            if unconnected_pairs:
                # Ищем bridge X среди ВСЕХ таблиц схемы (не только required).
                for x_table, x_body in db_schema.items():
                    if x_table in resolved_required:
                        # Bridge не должен быть одной из required — иначе
                        # ребро дублирует прямой FK / создаёт цикл.
                        continue
                    x_legacy = "foreign_keys" not in x_body
                    fk_targets: Dict[str, List[Dict[str, Any]]] = {}
                    for constraint in get_foreign_key_constraints(x_table, db_schema):
                        join = constraint_join(
                            x_table,
                            constraint,
                            legacy=x_legacy,
                            via_bridge=True,
                        )
                        fk_targets.setdefault(str(join["to_table"]), []).append(join)

                    for a, b in list(unconnected_pairs):
                        if a in fk_targets and b in fk_targets:
                            joins.extend(fk_targets[a])
                            joins.extend(fk_targets[b])
                            if not include_all_bridges:
                                # Legacy/fallback mode keeps its historical
                                # first bridge. FK-only mode collects all
                                # candidates so ambiguity can fail closed.
                                unconnected_pairs.remove((a, b))
                                if not unconnected_pairs:
                                    break
                    if not unconnected_pairs and not include_all_bridges:
                        break

        return joins

    # ------------------------------------------------------------------
    # Build / validate joins
    # ------------------------------------------------------------------
    def build_joins(
        self,
        linked_metrics: List[Dict[str, Any]],
        linked_dimensions: List[Dict[str, Any]],
        linked_filters: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        main_table: Optional[str] = None,
        *,
        include_conventions: bool = True,
    ) -> Dict[str, Any]:
        """Строит JOIN связи между таблицами.

        W4-T1: ``main_table`` теперь передаётся caller'ом (orchestrator),
        чтобы JoinValidator не знал про scoring. Если параметр не задан,
        для backward-compat используется первая metric (как раньше).

        W4-T2: required_tables вычисляется через :func:`compute_required_tables`
        — единый источник истины для всех call-sites.
        """
        if self.join_builder is None:
            self.join_builder = JoinBuilder(db_schema)

        required_tables: set = compute_required_tables(
            linked_metrics, linked_dimensions, linked_filters
        )
        requested_tables = set(required_tables)

        # Backward-compat: если main_table не передан, берём первую metric
        # (исторический поведение JoinValidator.build_joins). Caller'у
        # (orchestrator) рекомендуется передавать main_table явно — через
        # scoring в _pick_main_table_from_linked.
        if main_table is None:
            warnings.warn(
                "Passing main_table=None to JoinValidator.build_joins is "
                "deprecated; orchestrator should resolve main_table.",
                TextToSQLDeprecationWarning,
                stacklevel=2,
            )
            for metric in linked_metrics or []:
                if isinstance(metric, dict) and metric.get("table"):
                    main_table = metric["table"]
                    break

        if not main_table:
            return {
                "joins": [],
                "success": False,
                "unconnected_tables": sorted(required_tables),
                "main_table": None,
            }

        # 4.18: union FK-метаданных с convention-инференсом + симметричный
        # дедуп (см. 4.20). Раньше convention использовался только когда
        # FK-список был пуст, и партиционная схема с частичными FK теряла
        # convention-ребра.
        joins_from_schema = self._extract_fk_joins(
            required_tables,
            db_schema,
            include_all_bridges=not include_conventions,
        )
        # T5-linking / #6 HIGH: только bridge-таблицы (via_bridge=True) расширяют
        # required_tables. Прямые FK-цели (via_bridge отсутствует) намеренно
        # исключаются: они могут указывать на таблицы, которых исходный запрос
        # не требует (например students.dept_id → departments при запросе
        # students+courses), и добавление их в required приводит к лишним JOIN-ам.
        bridge_joins = [j for j in joins_from_schema if j.get("via_bridge")]
        if bridge_joins:
            required_tables = compute_required_tables(
                linked_metrics, linked_dimensions, linked_filters,
                validated_joins=bridge_joins,
            )
        # W4-T4 / A11: convention-joins добавляются ТОЛЬКО для пар таблиц,
        # которые ещё не связаны FK-ребром. Иначе symbolic FK-валидация
        # перебивается convention-fallback'ом и порождает silent
        # "incorrect SQL" (см. AGENTS.md: silent fallback запрещён).
        fk_pairs: set = {
            frozenset((j.get("from_table"), j.get("to_table")))
            for j in joins_from_schema
            if j.get("from_table") and j.get("to_table")
        }
        merged_joins: List[Dict[str, Any]] = list(joins_from_schema)
        if include_conventions:
            convention_joins = self.join_builder.infer_joins_by_convention(
                required_tables
            )
            for candidate in convention_joins:
                pair = frozenset(
                    (candidate.get("from_table"), candidate.get("to_table"))
                )
                if pair in fk_pairs:
                    # Для этой пары уже есть FK-join — convention пропускаем.
                    continue
                if not self._is_duplicate_join(candidate, merged_joins):
                    # Провенанс convention-ребра для графа: fk-aware таблица →
                    # convention_fk (вес 3), иначе legacy-convention (вес 5).
                    # greedy игнорирует лишний ключ `_source` (см. _normalize_edges).
                    candidate = dict(candidate)
                    candidate["_source"] = (
                        "convention_fk"
                        if self.join_builder._table_has_fk_metadata(
                            candidate["from_table"]
                        )
                        else "convention_legacy"
                    )
                    merged_joins.append(candidate)

        if not include_conventions:
            ambiguous_targets = self._ambiguous_fk_targets(
                main_table, requested_tables, merged_joins
            )
            if ambiguous_targets:
                return {
                    "joins": [],
                    "success": False,
                    "unconnected_tables": sorted(required_tables - {main_table}),
                    "main_table": main_table,
                    "ambiguous_join_paths": ambiguous_targets,
                }

        # T4: развилка по path_algo (env > yaml). greedy = текущая цепочка
        # (verbatim), graph = KMB Steiner-tree с полным fallback-контуром.
        algo = resolve_path_algo()
        if algo == "greedy":
            result = self.join_builder.build_joins(main_table, required_tables, merged_joins)
        else:
            result = self._build_joins_graph(main_table, required_tables, merged_joins)
        join_semantics = self._join_semantics(result.get("joins", []), db_schema)

        return {
            "joins": result.get("joins", []),
            "success": result.get("success", False),
            "unconnected_tables": list(result.get("unconnected_tables", set())),
            "main_table": main_table,
            "join_semantics": join_semantics,
            "join_warnings": [
                item["warning"] for item in join_semantics if item.get("warning")
            ],
        }

    @staticmethod
    def _ambiguous_fk_targets(
        main_table: str,
        required_tables: set,
        joins: List[Dict[str, Any]],
    ) -> List[str]:
        """Return required targets with more than one simple FK path."""
        adjacency: Dict[str, List[tuple[int, str]]] = {}
        unique_joins: List[Dict[str, Any]] = []
        for join in joins:
            if JoinValidator._is_duplicate_join(join, unique_joins):
                continue
            unique_joins.append(join)
            left = join.get("from_table")
            right = join.get("to_table")
            if not isinstance(left, str) or not isinstance(right, str):
                continue
            edge_id = len(unique_joins) - 1
            adjacency.setdefault(left, []).append((edge_id, right))
            adjacency.setdefault(right, []).append((edge_id, left))

        def path_count(target: str) -> int:
            count = 0

            def visit(node: str, visited: set[str]) -> None:
                nonlocal count
                if count > 1:
                    return
                if node == target:
                    count += 1
                    return
                for _, neighbor in adjacency.get(node, []):
                    if neighbor not in visited:
                        visit(neighbor, visited | {neighbor})

            visit(main_table, {main_table})
            return count

        return sorted(
            target
            for target in required_tables
            if target != main_table and path_count(target) > 1
        )

    def _join_semantics(
        self,
        joins: List[Dict[str, Any]],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        semantics: List[Dict[str, Any]] = []
        for join in joins or []:
            cardinality = self._join_cardinality(join, db_schema)
            source = (
                "bridge"
                if join.get("via_bridge")
                else str(join.get("_source") or "fk")
            )
            warning = None
            if cardinality in {"one_to_many", "unknown"}:
                raw_pairs = join.get("column_pairs")
                pairs = (
                    raw_pairs
                    if isinstance(raw_pairs, list)
                    else [
                        {
                            "from_column": join.get("from_column"),
                            "to_column": join.get("to_column"),
                        }
                    ]
                )
                pair_text = " AND ".join(
                    f"{join.get('from_table')}.{pair.get('from_column')} -> "
                    f"{join.get('to_table')}.{pair.get('to_column')}"
                    for pair in pairs
                    if isinstance(pair, dict)
                )
                warning = (
                    f"{pair_text} has {cardinality} cardinality; "
                    "aggregation may fan out"
                )
            semantics.append({
                "from_table": join.get("from_table"),
                "to_table": join.get("to_table"),
                "join_type": join.get("join_type"),
                "source": source,
                "cardinality": cardinality,
                "warning": warning,
            })
        return semantics

    def _join_cardinality(
        self,
        join: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> str:
        from_table = _resolve_table_name(join.get("from_table"), db_schema)
        to_table = _resolve_table_name(join.get("to_table"), db_schema)
        if not from_table or not to_table:
            return "unknown"
        raw_pairs = join.get("column_pairs")
        if raw_pairs is None:
            raw_pairs = [
                {
                    "from_column": join.get("from_column"),
                    "to_column": join.get("to_column"),
                }
            ]
        if not isinstance(raw_pairs, list) or not raw_pairs:
            return "unknown"
        pairs: list[tuple[str, str]] = []
        for pair in raw_pairs:
            if not isinstance(pair, dict):
                return "unknown"
            from_column = _resolve_column_name(
                pair.get("from_column"), from_table, db_schema
            )
            to_column = _resolve_column_name(
                pair.get("to_column"), to_table, db_schema
            )
            if not from_column or not to_column:
                return "unknown"
            pairs.append((from_column, to_column))
        if self._constraint_references(
            from_table,
            to_table,
            pairs,
            join.get("constraint_id"),
            db_schema,
        ):
            return "many_to_one"
        if self._constraint_references(
            to_table,
            from_table,
            [(target, source) for source, target in pairs],
            join.get("constraint_id"),
            db_schema,
        ):
            return "one_to_many"
        return "unknown"

    def _constraint_references(
        self,
        source_table: str,
        target_table: str,
        pairs: list[tuple[str, str]],
        constraint_id: object,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> bool:
        authoritative = "foreign_keys" in db_schema[source_table]
        for constraint in get_foreign_key_constraints(source_table, db_schema):
            if constraint["to_table"] != target_table:
                continue
            if authoritative and constraint_id and constraint["constraint_id"] != constraint_id:
                continue
            constraint_pairs = [
                (pair["from_column"], pair["to_column"])
                for pair in constraint["column_pairs"]
            ]
            if constraint_pairs == pairs:
                return True
        return False

    # ------------------------------------------------------------------
    # Graph join-path (KMB Steiner) — opt-in через path_algo=graph
    # ------------------------------------------------------------------
    def _to_candidate_edges(
        self, merged_joins: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Маппит merged-joins в candidate-edges для build_join_path.

        source: via_bridge → "bridge"; иначе ``_source`` (convention_fk /
        convention_legacy) если есть; иначе "fk". jt нормализуется как в
        ``JoinBuilder._normalize_edges`` ((join_type or "LEFT").upper().strip()).
        weight берётся из :data:`_BASE_WEIGHT` по source.
        """
        edges: List[Dict[str, Any]] = []
        for j in merged_joins or []:
            if j.get("via_bridge"):
                source = "bridge"
            elif j.get("_source"):
                source = j["_source"]
            else:
                source = "fk"
            jt = (j.get("join_type") or "LEFT").upper().strip()
            edge = {
                "a": j.get("from_table"),
                "b": j.get("to_table"),
                "a_col": j.get("from_column"),
                "b_col": j.get("to_column"),
                "jt": jt,
                "weight": _BASE_WEIGHT.get(source, _BASE_WEIGHT["convention_legacy"]),
                "source": source,
            }
            if "column_pairs" in j:
                edge["column_pairs"] = [
                    dict(pair) for pair in j.get("column_pairs", [])
                ]
                edge["constraint_id"] = j.get("constraint_id")
            edges.append(edge)
        return edges

    def _apply_containment(
        self,
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Применяет containment-валидатор к неуверенным рёбрам (раздел 4-5).

        mode=off → рёбра без изменений (ни одного БД-вызова). FK/bridge не
        сэмплируются (referential integrity гарантирована). validate → drop
        ребра ниже min_containment; weight → понижение веса хорошего ребра.
        fail-open: c is None / нет dsn → ребро остаётся.
        """
        from ..join_config import (
            resolve_containment,
            resolve_containment_sample,
            resolve_min_containment,
        )

        mode = resolve_containment()
        if mode == "off":
            return edges

        from ..join_containment import estimate_join_containment

        dsn = self.join_builder.dsn or get_runtime_context_dsn()
        if not dsn:
            # fail-open: без dsn сэмплировать нечем — рёбра не трогаем.
            return edges

        min_containment = resolve_min_containment()
        sample_size = resolve_containment_sample()

        result: List[Dict[str, Any]] = []
        for e in edges:
            # FK/bridge не сэмплируем — оставляем как есть.
            if e["source"] in {"fk", "bridge"}:
                result.append(e)
                continue
            c = estimate_join_containment(
                dsn,
                e["a"],
                e["a_col"],
                e["b"],
                e["b_col"],
                sample_size=sample_size,
            )
            if c is None:
                # fail-open: containment неопределим — ребро остаётся.
                result.append(e)
                continue
            if mode == "validate":
                if c < min_containment:
                    logger.warning(
                        "Dropping low-containment join edge %s.%s -> %s.%s "
                        "(containment=%.3f < min=%.3f, source=%s)",
                        e["a"], e["a_col"], e["b"], e["b_col"],
                        c, min_containment, e["source"],
                    )
                    continue
                result.append(e)
            else:  # mode == "weight"
                e = dict(e)
                e["weight"] *= (1.0 + (1.0 - c))
                result.append(e)
        return result

    def _unsafe_shared_parent_steiner_nodes(
        self,
        selected_edges: List[Dict[str, Any]],
        terminals: set,
    ) -> List[str]:
        """Find non-required same-direction Steiner nodes that can create fan-out."""
        if not selected_edges:
            return []

        incident: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for edge in selected_edges:
            incident[edge.get("a")].append(edge)
            incident[edge.get("b")].append(edge)

        unsafe_nodes: List[str] = []
        for node, node_edges in incident.items():
            if node in terminals or len(node_edges) < 2:
                continue
            if all(edge.get("source") == "bridge" for edge in node_edges):
                continue

            roles = {
                "a" if edge.get("a") == node else "b"
                for edge in node_edges
            }
            if len(roles) != 1:
                continue

            components_with_terminals = 0
            for edge in node_edges:
                start = edge.get("b") if edge.get("a") == node else edge.get("a")
                stack = [start]
                seen = {node}
                component_has_terminal = False
                while stack:
                    current = stack.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    if current in terminals:
                        component_has_terminal = True
                    for next_edge in incident.get(current, []):
                        other = (
                            next_edge.get("b")
                            if next_edge.get("a") == current
                            else next_edge.get("a")
                        )
                        if other not in seen:
                            stack.append(other)
                if component_has_terminal:
                    components_with_terminals += 1
            if components_with_terminals >= 2:
                unsafe_nodes.append(node)

        return sorted(unsafe_nodes)

    def _build_joins_graph(
        self,
        main_table: str,
        required_tables: set,
        merged_joins: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Граф-джойн (KMB Steiner) с полным fallback-контуром на greedy.

        Любой откат логируется явно (AGENTS.md: no silent fallback) и
        возвращает greedy на НЕфильтрованном ``merged_joins`` (граф не должен
        отнять рёбра, что взял бы greedy).
        """
        try:
            edges = self._to_candidate_edges(merged_joins)
            edges = self._apply_containment(edges)
            parallel_edges: Dict[frozenset, List[Dict[str, Any]]] = defaultdict(list)
            for edge in edges:
                parallel_edges[frozenset((edge.get("a"), edge.get("b")))].append(edge)
            for table_pair, candidates in parallel_edges.items():
                if len(candidates) > 1:
                    logger.warning(
                        "Multiple FK edges between %s: graph path will choose one "
                        "constraint deterministically; table aliases are required "
                        "to use both",
                        sorted(str(table) for table in table_pair),
                    )
            result = build_join_path(
                main_table,
                required_tables,
                edges,
                max_terminals=resolve_max_terminals(),
                dsn=self.join_builder.dsn,
            )
            if not result.get("success"):
                logger.warning(
                    "Graph join-path did not connect all required tables "
                    "(unconnected=%s); falling back to greedy build_joins",
                    sorted(result.get("unconnected_tables", set())),
                )
                return self.join_builder.build_joins(
                    main_table, required_tables, merged_joins
                )
            terminals = set(required_tables) | {main_table}
            unsafe_nodes = self._unsafe_shared_parent_steiner_nodes(
                result.get("_selected_edges", []),
                terminals,
            )
            if unsafe_nodes:
                logger.warning(
                    "Graph join-path avoided unsafe shared-parent fan-out via "
                    "non-required Steiner nodes %s; falling back to greedy build_joins",
                    unsafe_nodes,
                )
                return self.join_builder.build_joins(
                    main_table, required_tables, merged_joins
                )
            return result
        except JoinPathTooManyTerminals as e:
            logger.warning(
                "Graph join-path too many terminals (%s); falling back to "
                "greedy build_joins", e,
            )
            return self.join_builder.build_joins(
                main_table, required_tables, merged_joins
            )
        except WorkflowDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning(
                "Graph join-path failed (%s); falling back to greedy build_joins",
                type(e).__name__,
            )
            return self.join_builder.build_joins(
                main_table, required_tables, merged_joins
            )

    def validate_llm_joins(
        self,
        llm_joins: List[Dict[str, Any]],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Валидирует JOIN'ы от LLM против схемы БД."""
        validated_joins: List[Dict[str, Any]] = []

        for join in llm_joins:
            validation_result = self._is_join_valid_against_schema(join, db_schema)

            if validation_result.get("valid", False):
                validated_joins.append(validation_result.get("join", join))
                logger.debug("Valid LLM join: %s", join)
            else:
                logger.warning(
                    "Invalid LLM join: %s - %s",
                    join,
                    validation_result.get("error", "Unknown error"),
                )

        logger.info("LLM joins validation: %d/%d valid", len(validated_joins), len(llm_joins))

        # Cycle detection — циклический join-граф обычно индикатор того,
        # что LLM придумал лишнее ребро (или есть конфликт FK-цепочек).
        # Сами joins при этом не отбрасываем (могут быть валидны как
        # отдельные рёбра), но логируем warning для аудита.
        if validated_joins and _has_cycle(validated_joins):
            logger.warning(
                "Cycle detected in validated LLM joins graph (joins=%d); "
                "this typically indicates a redundant or conflicting join edge.",
                len(validated_joins),
            )

        return validated_joins

    def _is_join_valid_against_schema(
        self,
        join: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Проверяет валидность JOIN'а против схемы."""
        try:
            from_table = join.get("from_table")
            to_table = join.get("to_table")
            from_column = join.get("from_column")
            to_column = join.get("to_column")

            if not all([from_table, to_table, from_column, to_column]):
                return {"valid": False, "error": "Missing required fields"}

            resolved_from_table = _resolve_table_name(from_table, db_schema)
            resolved_to_table = _resolve_table_name(to_table, db_schema)
            if not resolved_from_table:
                return {"valid": False, "error": f"Table {from_table} not found in schema"}

            if not resolved_to_table:
                return {"valid": False, "error": f"Table {to_table} not found in schema"}

            resolved_from_column = _resolve_column_name(from_column, resolved_from_table, db_schema)
            resolved_to_column = _resolve_column_name(to_column, resolved_to_table, db_schema)

            if not resolved_from_column:
                return {"valid": False, "error": f"Column {from_column} not found in table {from_table}"}

            if not resolved_to_column:
                return {"valid": False, "error": f"Column {to_column} not found in table {to_table}"}

            from .resolution import _get_column_meta

            from_meta = _get_column_meta(resolved_from_table, resolved_from_column, db_schema)
            to_meta = _get_column_meta(resolved_to_table, resolved_to_column, db_schema)

            requested_pairs: list[tuple[str, str]] = []
            if "column_pairs" in join:
                raw_pairs = join.get("column_pairs")
                if not isinstance(raw_pairs, list) or not raw_pairs:
                    return {
                        "valid": False,
                        "error": "column_pairs must be a non-empty list",
                    }
                for pair in raw_pairs:
                    if not isinstance(pair, dict):
                        return {
                            "valid": False,
                            "error": "column_pairs members must be mappings",
                        }
                    source = _resolve_column_name(
                        pair.get("from_column"), resolved_from_table, db_schema
                    )
                    target = _resolve_column_name(
                        pair.get("to_column"), resolved_to_table, db_schema
                    )
                    if not source or not target:
                        return {
                            "valid": False,
                            "error": "column_pairs contains a column not found in schema",
                        }
                    requested_pairs.append((source, target))
            else:
                requested_pairs.append((resolved_from_column, resolved_to_column))

            candidates: list[tuple[str, Dict[str, object], bool]] = []
            authoritative_sides = (
                (resolved_from_table, resolved_to_table, False),
                (resolved_to_table, resolved_from_table, True),
            )
            for source_table, target_table, reverse in authoritative_sides:
                source_body = db_schema[source_table]
                if "foreign_keys" not in source_body:
                    continue
                constraints = get_foreign_key_constraints(source_table, db_schema)
                for constraint in constraints:
                    if constraint["to_table"] != target_table:
                        continue
                    if (
                        join.get("constraint_id")
                        and constraint["constraint_id"] != join.get("constraint_id")
                    ):
                        continue
                    canonical_pairs = [
                        (pair["from_column"], pair["to_column"])
                        for pair in constraint["column_pairs"]
                    ]
                    oriented_pairs = (
                        [(target, source) for source, target in canonical_pairs]
                        if reverse
                        else canonical_pairs
                    )
                    if "column_pairs" in join:
                        matches = requested_pairs == oriented_pairs
                    else:
                        matches = requested_pairs[0] in oriented_pairs
                    if matches:
                        candidates.append((source_table, constraint, reverse))

            if len(candidates) > 1:
                return {
                    "valid": False,
                    "error": "Join matches multiple authoritative foreign keys",
                }
            if "column_pairs" in join and not candidates:
                return {
                    "valid": False,
                    "error": "column_pairs must equal one complete foreign key constraint",
                }
            if candidates:
                source_table, constraint, reverse = candidates[0]
                target_table = str(constraint["to_table"])
                pairs = [dict(pair) for pair in constraint["column_pairs"]]
                for pair in pairs:
                    source_meta = _get_column_meta(
                        source_table, pair["from_column"], db_schema
                    )
                    target_meta = _get_column_meta(
                        target_table, pair["to_column"], db_schema
                    )
                    source_type = get_type(source_meta)
                    target_type = get_type(target_meta)
                    if not source_type or not target_type:
                        return {
                            "valid": False,
                            "error": "Cannot validate foreign key with missing column type",
                        }
                    if not ColumnMetadataHelper.check_type_compatibility(
                        source_type, target_type
                    ):
                        return {
                            "valid": False,
                            "error": f"Type mismatch: {source_type} vs {target_type}",
                        }
                join_type = str(join.get("join_type") or "LEFT").upper().strip()
                if reverse:
                    if join_type in INVERSE_JOIN_TYPES:
                        join_type = INVERSE_JOIN_TYPES[join_type]
                    elif join_type not in SYMMETRIC_JOIN_TYPES:
                        return {
                            "valid": False,
                            "error": f"unsupported join_type: {join.get('join_type')!r}",
                        }
                normalized_join = dict(join)
                normalized_join.update(
                    {
                        "from_table": source_table,
                        "from_column": pairs[0]["from_column"],
                        "to_table": target_table,
                        "to_column": pairs[0]["to_column"],
                        "join_type": join_type,
                        "constraint_id": constraint["constraint_id"],
                        "column_pairs": pairs,
                    }
                )
                return {"valid": True, "join": normalized_join}

            authoritative_selected_fk = (
                "foreign_keys" in db_schema[resolved_from_table]
                and isinstance(from_meta, dict)
                and is_fk(from_meta)
            ) or (
                "foreign_keys" in db_schema[resolved_to_table]
                and isinstance(to_meta, dict)
                and is_fk(to_meta)
            )
            if authoritative_selected_fk:
                return {
                    "valid": False,
                    "error": "FK column is absent from the authoritative foreign key envelope",
                }

            if from_meta and to_meta:
                from_type = get_type(from_meta)
                to_type = get_type(to_meta)

                if not from_type or not to_type:
                    # 4.19: ранее missing types молча принимались как
                    # valid — это silent fallback (LLM мог придумать
                    # JOIN по колонкам, которые в схеме без типа). Теперь
                    # fail-fast: возвращаем invalid, чтобы дальше
                    # отрабатывала convention/bridge инференция.
                    error = (
                        "Cannot validate join: missing column type "
                        f"({resolved_from_table}.{resolved_from_column}: "
                        f"{from_type or 'missing'} vs "
                        f"{resolved_to_table}.{resolved_to_column}: "
                        f"{to_type or 'missing'})"
                    )
                    logger.warning(error)
                    return {"valid": False, "error": error}

                if not ColumnMetadataHelper.check_type_compatibility(from_type, to_type):
                    return {"valid": False, "error": f"Type mismatch: {from_type} vs {to_type}"}

            normalized_join = dict(join)
            normalized_join.update({
                "from_table": resolved_from_table,
                "from_column": resolved_from_column,
                "to_table": resolved_to_table,
                "to_column": resolved_to_column,
            })
            return {"valid": True, "join": normalized_join}

        except Exception as e:
            return {"valid": False, "error": f"Validation error: {str(e)}"}

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    @staticmethod
    def _join_endpoints(join: Dict[str, Any]) -> tuple[object, ...]:
        """Симметричный ключ для дедупа JOIN.

        4.20: пара (A.x, B.y) и пара (B.y, A.x) описывают один и тот же
        JOIN и должны считаться дубликатами.
        """
        raw_pairs = join.get("column_pairs")
        if isinstance(raw_pairs, list):
            pairs = tuple(
                frozenset(
                    (
                        (join.get("from_table"), pair.get("from_column")),
                        (join.get("to_table"), pair.get("to_column")),
                    )
                )
                for pair in raw_pairs
                if isinstance(pair, dict)
            )
        else:
            pairs = (
                frozenset(
                    (
                        (join.get("from_table"), join.get("from_column")),
                        (join.get("to_table"), join.get("to_column")),
                    )
                ),
            )
        return (str(join.get("constraint_id") or ""), pairs)

    @staticmethod
    def _is_duplicate_join(join: Dict[str, Any], existing_joins: List[Dict[str, Any]]) -> bool:
        """Проверяет, является ли JOIN дубликатом.

        4.20: сравнение симметричное — поменянная местами пара (from/to)
        тоже считается дубликатом.
        """
        target = JoinValidator._join_endpoints(join)
        for existing in existing_joins:
            if JoinValidator._join_endpoints(existing) == target:
                return True
        return False
