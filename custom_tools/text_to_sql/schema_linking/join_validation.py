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

from ..deprecations import TextToSQLDeprecationWarning
from ..join_builder import JoinBuilder
from ..schema_metadata import ColumnMetadataHelper, get_type, is_fk
from ..utils import get_table_columns
from .resolution import (
    _resolve_column_name,
    _resolve_table_name,
)

logger = logging.getLogger(__name__)


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
    ) -> List[Dict[str, Any]]:
        """Извлекает возможные JOIN'ы на основе FK связей в схеме.

        4.21: для пары required-таблиц без прямого FK между ними ищется
        single-hop bridge-таблица X (любая таблица в ``db_schema``, у
        которой есть FK на обе required-таблицы). Тогда добавляются рёбра
        ``A↔X↔B`` с флагом ``via_bridge=True``. Глубина 1.
        """
        joins: List[Dict[str, Any]] = []

        # 1) Прямые FK от required-таблиц.
        for table_name in required_tables:
            resolved_table = _resolve_table_name(table_name, db_schema)
            if not resolved_table:
                continue

            table_columns = get_table_columns(db_schema[resolved_table])

            for col_name, col_info in table_columns.items():
                if isinstance(col_info, dict) and is_fk(col_info):
                    references = col_info.get("references", "")
                    if references:
                        ref_table, ref_column = _parse_fk_reference(references)
                        resolved_ref_table = _resolve_table_name(ref_table, db_schema)
                        resolved_ref_column = (
                            _resolve_column_name(ref_column, resolved_ref_table, db_schema)
                            if resolved_ref_table
                            else None
                        )
                        if resolved_ref_table and resolved_ref_column:
                            joins.append({
                                "from_table": resolved_table,
                                "from_column": col_name,
                                "to_table": resolved_ref_table,
                                "to_column": resolved_ref_column,
                                "join_type": "INNER",
                            })

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
                    x_columns = get_table_columns(x_body)
                    # Mapping: ref_table -> [(x_col, x_ref_col)]
                    fk_targets: Dict[str, List[Tuple[str, str]]] = {}
                    for x_col, x_info in x_columns.items():
                        if not (isinstance(x_info, dict) and is_fk(x_info)):
                            continue
                        x_references = x_info.get("references", "")
                        if not x_references:
                            continue
                        x_ref_table, x_ref_col = _parse_fk_reference(x_references)
                        resolved_x_ref_table = _resolve_table_name(x_ref_table, db_schema)
                        resolved_x_ref_col = (
                            _resolve_column_name(x_ref_col, resolved_x_ref_table, db_schema)
                            if resolved_x_ref_table
                            else None
                        )
                        if resolved_x_ref_table and resolved_x_ref_col:
                            fk_targets.setdefault(resolved_x_ref_table, []).append(
                                (x_col, resolved_x_ref_col)
                            )

                    for a, b in list(unconnected_pairs):
                        if a in fk_targets and b in fk_targets:
                            for a_col, a_ref_col in fk_targets[a]:
                                joins.append({
                                    "from_table": x_table,
                                    "from_column": a_col,
                                    "to_table": a,
                                    "to_column": a_ref_col,
                                    "join_type": "INNER",
                                    "via_bridge": True,
                                })
                            for b_col, b_ref_col in fk_targets[b]:
                                joins.append({
                                    "from_table": x_table,
                                    "from_column": b_col,
                                    "to_table": b,
                                    "to_column": b_ref_col,
                                    "join_type": "INNER",
                                    "via_bridge": True,
                                })
                            # Пара покрыта — больше не ищем bridge для неё.
                            unconnected_pairs.remove((a, b))
                            if not unconnected_pairs:
                                break
                    if not unconnected_pairs:
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
        joins_from_schema = self._extract_fk_joins(required_tables, db_schema)
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
        convention_joins = self.join_builder.infer_joins_by_convention(required_tables)
        merged_joins: List[Dict[str, Any]] = list(joins_from_schema)
        for candidate in convention_joins:
            pair = frozenset((candidate.get("from_table"), candidate.get("to_table")))
            if pair in fk_pairs:
                # Для этой пары уже есть FK-join — convention пропускаем.
                continue
            if not self._is_duplicate_join(candidate, merged_joins):
                merged_joins.append(candidate)

        result = self.join_builder.build_joins(main_table, required_tables, merged_joins)

        return {
            "joins": result.get("joins", []),
            "success": result.get("success", False),
            "unconnected_tables": list(result.get("unconnected_tables", set())),
            "main_table": main_table,
        }

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

            # Локальный импорт, чтобы избежать циклической зависимости
            # при импорте модуля.
            from .resolution import _get_column_meta

            from_meta = _get_column_meta(resolved_from_table, resolved_from_column, db_schema)
            to_meta = _get_column_meta(resolved_to_table, resolved_to_column, db_schema)

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
    def _join_endpoints(join: Dict[str, Any]) -> frozenset:
        """Симметричный ключ для дедупа JOIN.

        4.20: пара (A.x, B.y) и пара (B.y, A.x) описывают один и тот же
        JOIN и должны считаться дубликатами.
        """
        return frozenset(
            (
                (join.get("from_table"), join.get("from_column")),
                (join.get("to_table"), join.get("to_column")),
            )
        )

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
