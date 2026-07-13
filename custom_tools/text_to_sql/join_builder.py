"""
Логика построения JOIN между таблицами
"""
import logging
from typing import Dict, List, Any, Set, Optional, Tuple
from .dialects import quote_identifier
from .utils import get_table_columns

logger = logging.getLogger(__name__)


INVERSE_JOIN_TYPES = {"LEFT": "RIGHT", "RIGHT": "LEFT"}
SYMMETRIC_JOIN_TYPES = {"INNER", "FULL", "FULL OUTER", "CROSS", "NATURAL"}
# VALID_JOIN_TYPES: объединение асимметричных (LEFT/RIGHT) и симметричных типов.
# «JOIN» без префикса нормализован sql_builder'ом ДО попадания сюда,
# поэтому здесь не присутствует.
VALID_JOIN_TYPES = set(INVERSE_JOIN_TYPES.keys()) | SYMMETRIC_JOIN_TYPES


class JoinBuilder:
    """Строитель JOIN клауз на основе эвристик и схемы БД."""

    def __init__(
        self,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        *,
        inflector: Optional[List[Tuple[str, str]]] = None,
        dsn: Optional[str] = None,
    ):
        """
        Args:
            db_schema: схема БД.
            inflector: явный список ``[suffix, plural_suffix]``-правил
                для match_table_base. Если ``None`` — правила берутся из
                ``nlu_morphemes.yaml::table_name_inflections`` (yaml — source
                of truth). Пустой список ``[]`` отключает плюрализацию.
            dsn: явный DSN для диалект-aware квотирования идентификаторов
                в build_joins. Если не передан — используется ANSI-диалект.
        """
        self.db_schema = db_schema
        self._inflector_override = inflector
        self.dsn = dsn
    
    def build_joins(
        self, 
        main_table: str, 
        required_tables: Set[str], 
        joins_from_schema: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Строит JOIN клаузы для соединения всех требуемых таблиц.
        
        Returns:
            Dict с ключами:
            - success: bool - удалось ли соединить все таблицы
            - join_clauses: List[str] - список JOIN клауз
            - used_tables: Set[str] - множество подключенных таблиц
            - unconnected_tables: Set[str] - несоединенные таблицы
        """
        used_tables = {main_table}
        join_clauses: List[str] = []
        join_edges: List[Dict[str, Any]] = []
        
        # Нормализуем список ребер из schema_linking
        norm_edges = self._normalize_edges(joins_from_schema)
        
        # Алгоритм greedy подключения таблиц
        progress = True
        while progress and (required_tables - used_tables):
            progress = False
            for edge in norm_edges:
                a, b, a_col, b_col, jt = edge["a"], edge["b"], edge["a_col"], edge["b_col"], edge["jt"]
                predicates = " AND ".join(
                    f"{quote_identifier(a, dsn=self.dsn)}.{quote_identifier(source, dsn=self.dsn)} = "
                    f"{quote_identifier(b, dsn=self.dsn)}.{quote_identifier(target, dsn=self.dsn)}"
                    for source, target in edge["pairs"]
                )
                join_entry = {
                    "from_table": a,
                    "from_column": a_col,
                    "to_table": b,
                    "to_column": b_col,
                    "join_type": jt,
                }
                if edge["grouped"]:
                    if edge["constraint_id"]:
                        join_entry["constraint_id"] = edge["constraint_id"]
                    join_entry["column_pairs"] = [
                        {"from_column": source, "to_column": target}
                        for source, target in edge["pairs"]
                    ]
                
                # если одно из окончаний уже подключено, а другое требуется — подключаем
                if a in used_tables and (b in (required_tables - used_tables)):
                    join_clauses.append(
                        f"{jt} JOIN {quote_identifier(b, dsn=self.dsn)} ON {predicates}"
                    )
                    join_edges.append(join_entry)
                    used_tables.add(b)
                    progress = True
                elif b in used_tables and (a in (required_tables - used_tables)):
                    effective_jt = self._invert_join_type(jt)
                    join_clauses.append(
                        f"{effective_jt} JOIN {quote_identifier(a, dsn=self.dsn)} ON {predicates}"
                    )
                    reverse_entry = dict(join_entry)
                    reverse_entry["join_type"] = effective_jt
                    join_edges.append(reverse_entry)
                    used_tables.add(a)
                    progress = True
                elif (
                    a in used_tables
                    and b in used_tables
                    and (a in required_tables or b in required_tables)
                ):
                    logger.warning(
                        "Multiple FK edges between %s and %s: constraint %s with pairs %s "
                        "skipped because both %s and %s are already joined; "
                        "join ambiguity may produce incorrect SQL",
                        a, b, edge["constraint_id"], edge["pairs"], a, b,
                    )
        
        unconnected_tables = required_tables - used_tables
        
        return {
            "success": len(unconnected_tables) == 0,
            "join_clauses": join_clauses,
            "joins": join_edges,
            "used_tables": used_tables,
            "unconnected_tables": unconnected_tables
        }
    
    def _normalize_edges(self, joins_in: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Нормализует ребра JOIN из входных данных.

        Валидные join_type: LEFT, RIGHT, INNER, FULL, FULL OUTER, CROSS, NATURAL.
        Неизвестный join_type -> ValueError (fail-fast, без silent fallback).
        """
        norm_edges: List[Dict[str, Any]] = []
        for j in joins_in or []:
            a = j.get("from_table")
            a_col = j.get("from_column")
            b = j.get("to_table")
            b_col = j.get("to_column")
            jt = (j.get("join_type") or "LEFT").upper().strip()
            if jt not in VALID_JOIN_TYPES:
                raise ValueError(f"unsupported join_type: {j.get('join_type')!r}")
            grouped = "column_pairs" in j
            if grouped:
                raw_pairs = j.get("column_pairs")
                if not isinstance(raw_pairs, list) or not raw_pairs:
                    raise ValueError("column_pairs must be a non-empty list")
                pairs: list[tuple[str, str]] = []
                for pair in raw_pairs:
                    if not isinstance(pair, dict):
                        raise ValueError("column_pairs members must be mappings")
                    source = pair.get("from_column")
                    target = pair.get("to_column")
                    if not isinstance(source, str) or not source:
                        raise ValueError("column_pairs.from_column is required")
                    if not isinstance(target, str) or not target:
                        raise ValueError("column_pairs.to_column is required")
                    pairs.append((source, target))
                if a_col != pairs[0][0] or b_col != pairs[0][1]:
                    raise ValueError("scalar join aliases must equal the first column pair")
            else:
                pairs = [(a_col, b_col)] if a_col and b_col else []
            if a and b and a_col and b_col:
                norm_edges.append({
                    "a": a,
                    "a_col": a_col,
                    "b": b,
                    "b_col": b_col,
                    "jt": jt,
                    "pairs": tuple(pairs),
                    "constraint_id": j.get("constraint_id"),
                    "grouped": grouped,
                })
        return norm_edges

    def _invert_join_type(self, jt: str) -> str:
        """Инвертирует join_type для reverse-edge (LEFT<->RIGHT, симметричные не меняются).

        Поднимает ValueError для неизвестных типов (fail-fast).
        """
        jt_norm = jt.upper().strip()
        if jt_norm in INVERSE_JOIN_TYPES:
            return INVERSE_JOIN_TYPES[jt_norm]
        if jt_norm in SYMMETRIC_JOIN_TYPES:
            return jt_norm
        raise ValueError(f"unsupported join_type for inversion: {jt!r}")
    
    def infer_joins_by_convention(self, required_tables: Set[str]) -> List[Dict[str, Any]]:
        """
        Выводит JOIN связи по соглашениям именования (*_id → id).
        Используется как fallback если нет явных связей.

        EPIC 5.1: английская плюрализация (user↔users, category↔categories)
        больше не хардкодится в коде — правила лежат в
        ``config/text_to_sql/nlu_morphemes.yaml::table_name_inflections``.
        Можно подменить через ctor-параметр ``inflector``.

        W4-T4 (per-table): если КОНКРЕТНАЯ table_a имеет хоть одну FK-
        аннотированную колонку (``constraint_type=FK`` / ``references``),
        она считается fk-aware — convention-join разрешён только для тех
        её колонок, у которых есть явный ``is_fk(col_info)``. Если у
        table_a FK-метаданных нет вообще — сохраняем legacy convention-
        fallback по суффиксу ``_id`` (иначе на схемах без FK-аннотаций мы
        потеряли бы все joins). Глобальный флаг по всей схеме здесь не
        подходит: partial-FK schema (часть таблиц fk-aware, часть нет)
        иначе теряет convention для всех таблиц одновременно.
        """
        from .schema_metadata import is_fk

        joins = []

        if not self.db_schema:
            return joins

        pluralizers = self._resolve_pluralizers()

        # Простые эвристики: ищем *_id → id паттерны
        for table_a in required_tables:
            if table_a not in self.db_schema:
                continue

            table_a_fk_aware = self._table_has_fk_metadata(table_a)
            authoritative_fk_columns: Optional[Set[str]] = None
            table_body = self.db_schema[table_a]
            if "foreign_keys" in table_body:
                from .schema_metadata import get_foreign_key_constraints

                envelope = table_body["foreign_keys"]
                if not isinstance(envelope, dict):
                    raise TypeError("foreign_keys must be a mapping")
                if envelope.get("complete") is False:
                    continue
                authoritative_fk_columns = {
                    pair["from_column"]
                    for constraint in get_foreign_key_constraints(
                        table_a, self.db_schema
                    )
                    for pair in constraint["column_pairs"]
                }
            table_a_columns = get_table_columns(self.db_schema[table_a])
            for col_name, col_info in table_a_columns.items():
                if col_name.lower().endswith("_id"):
                    # W4-T4 guard: если table_a — fk-aware (есть хоть одна
                    # FK-колонка), convention-join разрешён только для
                    # колонок с is_fk(col_info)=True. Иначе molchanije
                    # ломает symbolic FK-валидацию (см. A11).
                    if authoritative_fk_columns is not None:
                        if col_name not in authoritative_fk_columns:
                            continue
                    elif table_a_fk_aware:
                        if not (isinstance(col_info, dict) and is_fk(col_info)):
                            continue
                    base = col_name[:-3]  # убираем '_id'

                    # Ищем таблицу с подходящим именем
                    for table_b in self.db_schema.keys():
                        if table_b == table_a:
                            continue

                        table_b_base = self._get_base_table_name(table_b)
                        if self._match_base(base, table_b_base, pluralizers):

                            # Определяем PK целевой таблицы из schema, fallback "id".
                            target_pk = self._get_primary_key(table_b)
                            if target_pk is None:
                                target_columns = get_table_columns(self.db_schema.get(table_b, {}))
                                if "id" not in target_columns:
                                    continue
                                logger.warning(
                                    "Primary key not found for %s, using 'id' as fallback",
                                    table_b,
                                )
                                target_pk = "id"
                            joins.append({
                                "from_table": table_a,
                                "from_column": col_name,
                                "to_table": table_b,
                                "to_column": target_pk,
                                "join_type": "LEFT"
                            })
                            break

        return joins

    def _table_has_fk_metadata(self, table_name: str, db_schema: Optional[Dict[str, Any]] = None) -> bool:
        """W4-T4 (per-table): помечена ли хоть одна колонка таблицы как FK.

        Проверяется именно та таблица, у которой есть колонка ``*_id``
        (table_a в convention-инференсе). Если у table_a есть хоть одна
        FK-аннотированная колонка — таблица считается «fk-aware», и
        convention-fallback требует явного ``is_fk(col_info)`` для каждой
        конкретной колонки. Если у table_a FK-метаданных нет вообще —
        сохраняется legacy convention по суффиксу ``_id`` (иначе на схемах
        без FK-аннотаций мы потеряли бы все joins).
        """
        from .schema_metadata import is_fk

        schema = db_schema if db_schema is not None else self.db_schema
        if not schema or table_name not in schema:
            return False
        table_body = schema.get(table_name, {})
        if "foreign_keys" in table_body:
            from .schema_metadata import get_foreign_key_constraints

            return bool(get_foreign_key_constraints(table_name, schema))
        columns = get_table_columns(schema.get(table_name, {}))
        for _, meta in columns.items():
            if isinstance(meta, dict) and is_fk(meta):
                return True
        return False

    def _resolve_pluralizers(self) -> List[Tuple[str, str]]:
        """Откуда брать список pluralizers.

        Приоритет:
          * явный ctor-параметр ``inflector`` (включая ``[]``);
          * иначе — yaml ``nlu_morphemes.table_name_inflections``.
            Если в yaml ``enabled: false`` — возвращаем пустой список.
        """
        if self._inflector_override is not None:
            return self._inflector_override
        # Импорт ленивый: JoinBuilder используется и в сценариях, где
        # yaml-пути могут переопределяться поздно (тесты с monkeypatch).
        from .nlu_config import load_nlu_morphemes

        cfg = load_nlu_morphemes()
        if not cfg.table_name_inflections_enabled:
            return []
        return list(cfg.table_name_pluralizers)

    def _match_base(
        self,
        fk_base: str,
        table_base: str,
        pluralizers: List[Tuple[str, str]],
    ) -> bool:
        from .nlu_config import match_table_base

        return match_table_base(fk_base, table_base, pluralizers)

    def _get_base_table_name(self, table_name: str) -> str:
        """Извлекает базовое имя таблицы (без схемы)."""
        return table_name.split(".")[-1]

    def _get_primary_key(self, table_name: str) -> Optional[str]:
        """Определяет имя PK-колонки таблицы из schema.

        Возвращает имя единственной колонки, помеченной как PK (см.
        :func:`schema_metadata.is_pk`). Если PK явно не помечен — ``None``;
        caller должен сам решить, использовать ли fallback ``"id"``.

        Для composite PK (≥2 колонки) возвращает ``None`` с warning: иначе
        выбор первой колонки из dict-iteration был бы недетерминированным и
        давал бы неполный JOIN на молчанку.
        """
        if not self.db_schema or table_name not in self.db_schema:
            return None
        from .schema_metadata import is_pk

        columns = get_table_columns(self.db_schema.get(table_name, {}))
        pk_columns = [
            col_name
            for col_name, meta in columns.items()
            if isinstance(meta, dict) and is_pk(meta)
        ]
        if not pk_columns:
            return None
        if len(pk_columns) > 1:
            logger.warning(
                "Composite primary key on %s (%s): skipping convention-based JOIN "
                "to avoid silently incorrect SQL",
                table_name, pk_columns,
            )
            return None
        return pk_columns[0]
