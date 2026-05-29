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
    ):
        """
        Args:
            db_schema: схема БД.
            inflector: явный список ``[suffix, plural_suffix]``-правил
                для match_table_base. Если ``None`` — правила берутся из
                ``nlu_morphemes.yaml::table_name_inflections`` (yaml — source
                of truth). Пустой список ``[]`` отключает плюрализацию.
        """
        self.db_schema = db_schema
        self._inflector_override = inflector
    
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
                
                # если одно из окончаний уже подключено, а другое требуется — подключаем
                if a in used_tables and (b in (required_tables - used_tables)):
                    join_clauses.append(
                        f"{jt} JOIN {quote_identifier(b)} ON {quote_identifier(a)}.{quote_identifier(a_col)} = {quote_identifier(b)}.{quote_identifier(b_col)}"
                    )
                    join_edges.append({
                        "from_table": a,
                        "from_column": a_col,
                        "to_table": b,
                        "to_column": b_col,
                        "join_type": jt,
                    })
                    used_tables.add(b)
                    progress = True
                elif b in used_tables and (a in (required_tables - used_tables)):
                    effective_jt = self._invert_join_type(jt)
                    join_clauses.append(
                        f"{effective_jt} JOIN {quote_identifier(a)} ON {quote_identifier(a)}.{quote_identifier(a_col)} = {quote_identifier(b)}.{quote_identifier(b_col)}"
                    )
                    join_edges.append({
                        "from_table": a,
                        "from_column": a_col,
                        "to_table": b,
                        "to_column": b_col,
                        "join_type": effective_jt,
                    })
                    used_tables.add(a)
                    progress = True
        
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
            if a and b and a_col and b_col:
                norm_edges.append({
                    "a": a, "a_col": a_col, "b": b, "b_col": b_col, "jt": jt
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
            table_a_columns = get_table_columns(self.db_schema[table_a])
            for col_name, col_info in table_a_columns.items():
                if col_name.lower().endswith("_id"):
                    # W4-T4 guard: если table_a — fk-aware (есть хоть одна
                    # FK-колонка), convention-join разрешён только для
                    # колонок с is_fk(col_info)=True. Иначе molchanije
                    # ломает symbolic FK-валидацию (см. A11).
                    if table_a_fk_aware:
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
