"""
Schema Metadata - константы, вспомогательные функции и утилиты для работы с метаданными схемы БД

Списки значимости колонок (high_priority_exact, medium_priority_patterns,
critical_description_keywords) вынесены в config/text_to_sql/significance.yaml
и грузятся через ``significance_config.load_significance_config``. См. T4.3
в AGENTS.md: доменно-зависимые термины не должны быть вшиты в код.
"""
import logging
from collections.abc import Mapping
from typing import Dict, List, Any, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .significance_config import SignificanceProfile

logger = logging.getLogger(__name__)

# ========================================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С МЕТАДАННЫМИ КОЛОНОК
# ========================================================================================

def is_pk(meta: Dict[str, Any]) -> bool:
    """Проверяет, является ли колонка первичным ключом."""
    if not isinstance(meta, dict):
        return False
    
    constraint_type = str(meta.get('constraint_type', '')).strip().upper()
    is_primary_key = meta.get('is_primary_key', False)
    
    # Нормализация булевых значений
    if isinstance(is_primary_key, str):
        is_primary_key = is_primary_key.lower() in ('true', '1', 'yes', 'on')
    
    return constraint_type in ('PK', 'PRIMARY KEY') or bool(is_primary_key)


def is_fk(meta: Dict[str, Any]) -> bool:
    """Проверяет, является ли колонка внешним ключом."""
    if not isinstance(meta, dict):
        return False
    
    constraint_type = str(meta.get('constraint_type', '')).strip().upper()
    references = meta.get('references', '')
    
    return constraint_type in ('FK', 'FOREIGN KEY') or bool(references)


def is_not_null(meta: Dict[str, Any]) -> bool:
    """Проверяет, имеет ли колонка ограничение NOT NULL."""
    if not isinstance(meta, dict):
        return False
    
    not_null = meta.get('not_null', False)
    
    # Нормализация булевых значений
    if isinstance(not_null, str):
        not_null = not_null.lower() in ('true', '1', 'yes', 'on')
    
    return bool(not_null)


def get_type(meta: Dict[str, Any]) -> str:
    """Получает тип колонки."""
    if not isinstance(meta, dict):
        return ""
    
    return str(meta.get('type', ''))


def normalize_constraint_type(constraint_type: str) -> str:
    """Нормализует тип ограничения колонки."""
    if not constraint_type:
        return ""
    
    normalized = str(constraint_type).strip().upper()
    constraint_map = {
        "PRIMARY KEY": "PK",
        "PK": "PK", 
        "FOREIGN KEY": "FK",
        "FK": "FK",
        "UNIQUE": "UNIQUE"
    }
    return constraint_map.get(normalized, "")


def _resolve_mapping_name(name: object, candidates: Mapping[str, object]) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    wanted = name.strip()
    if wanted in candidates:
        return wanted
    matches = [candidate for candidate in candidates if candidate.casefold() == wanted.casefold()]
    if len(matches) == 1:
        return matches[0]
    base_matches = [
        candidate
        for candidate in candidates
        if candidate.rsplit(".", 1)[-1].casefold() == wanted.rsplit(".", 1)[-1].casefold()
    ]
    return base_matches[0] if len(base_matches) == 1 else None


def _parse_reference(reference: object) -> tuple[str | None, str | None]:
    if not isinstance(reference, str) or not reference.strip():
        return None, None
    value = reference.strip()
    if "(" in value and value.endswith(")"):
        table, column = value.split("(", 1)
        return table.strip() or None, column[:-1].strip() or None
    if "." in value:
        table, column = value.rsplit(".", 1)
        return table.strip() or None, column.strip() or None
    return value, "id"


def get_foreign_key_constraints(
    source_table: str,
    db_schema: Mapping[str, object],
) -> list[dict[str, object]]:
    """Return validated FK constraints for one source table.

    An absent table-level envelope is the legacy scalar format. ``complete``
    envelopes are authoritative; an unavailable or invalid envelope never
    falls back to the per-column aliases.
    """
    if not isinstance(db_schema, Mapping):
        raise TypeError("db_schema must be a mapping")
    resolved_source = _resolve_mapping_name(source_table, db_schema)
    if resolved_source is None:
        raise ValueError(f"source table {source_table!r} does not resolve in schema")
    source_body = db_schema[resolved_source]
    if not isinstance(source_body, Mapping):
        raise TypeError(f"schema table {resolved_source!r} must be a mapping")

    from .utils import get_table_columns

    source_columns = get_table_columns(source_body)
    if "foreign_keys" not in source_body:
        constraints: list[dict[str, object]] = []
        for source_column, metadata in source_columns.items():
            if not isinstance(metadata, dict) or not is_fk(metadata):
                continue
            target_name, target_column_name = _parse_reference(metadata.get("references"))
            target_table = _resolve_mapping_name(target_name, db_schema)
            if target_table is None:
                continue
            target_columns = get_table_columns(db_schema[target_table])
            target_column = _resolve_mapping_name(target_column_name, target_columns)
            if target_column is None:
                continue
            constraints.append(
                {
                    "constraint_id": f"{resolved_source}:legacy:{source_column}",
                    "to_table": target_table,
                    "column_pairs": [
                        {
                            "from_column": source_column,
                            "to_column": target_column,
                        }
                    ],
                }
            )
        return constraints

    envelope = source_body["foreign_keys"]
    if not isinstance(envelope, Mapping):
        raise TypeError(f"foreign_keys for {resolved_source!r} must be a mapping")
    complete = envelope.get("complete")
    raw_constraints = envelope.get("constraints")
    if not isinstance(complete, bool):
        raise TypeError("foreign_keys.complete must be a boolean")
    if not isinstance(raw_constraints, list):
        raise TypeError("foreign_keys.constraints must be a list")
    if not complete:
        if raw_constraints:
            raise ValueError("incomplete foreign_keys envelope cannot publish constraints")
        return []

    resolved_constraints: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw_constraint in raw_constraints:
        if not isinstance(raw_constraint, Mapping):
            raise TypeError("foreign key constraint must be a mapping")
        constraint_id = raw_constraint.get("constraint_id")
        if not isinstance(constraint_id, str) or not constraint_id.strip():
            raise ValueError("foreign key constraint_id must be a non-empty string")
        constraint_id = constraint_id.strip()
        if constraint_id in seen_ids:
            raise ValueError(f"duplicate foreign key constraint_id {constraint_id!r}")
        seen_ids.add(constraint_id)

        target_table = _resolve_mapping_name(raw_constraint.get("to_table"), db_schema)
        if target_table is None:
            logger.warning(
                "foreign key %r target table %r does not resolve in schema; "
                "skipping this constraint",
                constraint_id,
                raw_constraint.get("to_table"),
            )
            continue
        target_body = db_schema[target_table]
        if not isinstance(target_body, Mapping):
            raise TypeError(f"schema table {target_table!r} must be a mapping")
        target_columns = get_table_columns(target_body)

        raw_pairs = raw_constraint.get("column_pairs")
        if not isinstance(raw_pairs, list) or not raw_pairs:
            raise ValueError(
                f"foreign key {constraint_id!r} must contain at least one column pair"
            )
        pairs: list[dict[str, str]] = []
        seen_source_columns: set[str] = set()
        seen_target_columns: set[str] = set()
        for raw_pair in raw_pairs:
            if not isinstance(raw_pair, Mapping):
                raise TypeError("foreign key column pair must be a mapping")
            source_column = _resolve_mapping_name(
                raw_pair.get("from_column"), source_columns
            )
            target_column = _resolve_mapping_name(
                raw_pair.get("to_column"), target_columns
            )
            if source_column is None or target_column is None:
                raise ValueError(
                    f"foreign key {constraint_id!r} column pair does not resolve"
                )
            if source_column in seen_source_columns or target_column in seen_target_columns:
                raise ValueError(
                    f"foreign key {constraint_id!r} contains duplicate pair members"
                )
            seen_source_columns.add(source_column)
            seen_target_columns.add(target_column)
            pairs.append(
                {"from_column": source_column, "to_column": target_column}
            )
        resolved_constraints.append(
            {
                "constraint_id": constraint_id,
                "to_table": target_table,
                "column_pairs": pairs,
            }
        )
    return resolved_constraints


# ========================================================================================
# КЛАСС ДЛЯ РАБОТЫ С МЕТАДАННЫМИ КОЛОНОК
# ========================================================================================

class ColumnMetadataHelper:
    """Утилиты для работы с метаданными колонок."""
    
    @staticmethod
    def is_semantic_significant_column(
        col_name: str,
        col_info: Dict[str, Any],
        *,
        dsn: Optional[str] = None,
        significance_profile: Optional["SignificanceProfile"] = None,
    ) -> bool:
        """Определяет, является ли колонка семантически значимой.

        Списки значимости берутся из yaml-конфига (см. significance_config).
        Если конфиг отсутствует — поднимется ``FileNotFoundError``, без
        молчаливых дефолтов.

        ``dsn`` (W1-1.2b): если передан и для него есть непустой
        DSN-профиль в части ``metric_hints.significant_columns``, он
        приоритетнее named significance-профиля — см.
        ``dsn_profile_overrides.resolve_significance_profile``.
        ``medium_priority_patterns`` ТОЖЕ входит в DSN-профиль (см.
        ``resolve_significance_profile``: паттерны компилируются из
        ``dsn_profile.SignificantColumnHints.medium_priority_patterns``) —
        замещается вместе с остальными полями секции ``significant_columns``,
        когда она непуста в DSN-профиле.

        ``significance_profile`` (W1-1.2-review, замечание 2): если передан,
        используется НАПРЯМУЮ вместо резолва по ``dsn`` — вызывающий код,
        которому нужно проверить много колонок подряд (см.
        ``SchemaColumnFilter.filter_relevant_columns`` в цикле
        ``build_relevant_schema_context`` по всем таблицам), резолвит профиль
        ОДИН РАЗ и передаёт готовый объект сюда, вместо повторного похода в
        ``resolve_significance_profile`` → ``load_dsn_profile`` (``stat()``
        на каждый вызов) на каждую колонку. При ``significance_profile is not
        None`` параметр ``dsn`` игнорируется. Без ``significance_profile``
        поведение не меняется (обратная совместимость): резолв по ``dsn`` на
        каждый вызов, как раньше.
        """
        if not col_name:
            return False

        col_name_lower = col_name.lower()
        if significance_profile is not None:
            config = significance_profile
        else:
            from .dsn_profile_overrides import resolve_significance_profile

            config = resolve_significance_profile(dsn=dsn)

        # Проверяем высокоприоритетные точные совпадения
        for pattern in config.high_priority_exact:
            if ColumnMetadataHelper._matches_exactly(col_name_lower, pattern):
                return True

        # Проверяем составные имена
        for pattern in config.high_priority_compound:
            if pattern in col_name_lower:
                return True

        for compiled_pattern, _desc_keyword in config.medium_priority_patterns:
            if compiled_pattern.search(col_name_lower):
                return True

        # ПРОВЕРКА ОПИСАНИЯ - только для действительно важных терминов
        if isinstance(col_info, dict):
            col_desc = col_info.get('description') or ''
            if col_desc:
                description = col_desc.lower()
                for keyword in config.critical_description_keywords:
                    if keyword in description:
                        return True

        return False
    
    @staticmethod
    def _matches_exactly(col_name: str, pattern: str) -> bool:
        """Проверяет точное совпадение или совпадение как отдельного слова.

        EPIC 3.33: ``_`` трактуется как граница слова. Стандартный ``\\b``
        в Python не разделяет alphanumeric от ``_`` (т.к. ``\\w`` включает
        ``_``), поэтому имена типа ``user_id`` не матчились по ``user``.
        Теперь используется кастомная граница ``(?<![A-Za-z0-9])`` /
        ``(?![A-Za-z0-9])``: ``user`` матчится в ``user_id``, ``id_user``,
        ``user.id``, но НЕ в ``superuser`` или ``username``.
        """
        import re
        # Точное совпадение
        if col_name == pattern:
            return True

        # Кастомная граница: alphanumeric не должен примыкать с обеих сторон.
        # ``_`` намеренно НЕ включён в lookaround → выступает разделителем.
        word_pattern = (
            r'(?<![A-Za-z0-9Ѐ-ӿ])'
            + re.escape(pattern)
            + r'(?![A-Za-z0-9Ѐ-ӿ])'
        )
        if re.search(word_pattern, col_name):
            return True

        return False
    
    @staticmethod
    def get_key_columns(table_schema: Dict[str, Dict[str, Any]]) -> Set[str]:
        """Извлекает ключевые колонки (PK/FK) из схемы таблицы."""
        from .utils import get_table_columns
        
        key_columns = set()
        table_columns = get_table_columns(table_schema)
        
        for col_name, col_info in table_columns.items():
            if isinstance(col_info, dict):
                if is_pk(col_info) or is_fk(col_info):
                    key_columns.add(col_name)
        
        return key_columns
    
    @staticmethod
    def extract_directly_linked_columns(
        table_name: str, 
        linked_metrics: List[Dict[str, Any]], 
        linked_dimensions: List[Dict[str, Any]], 
        linked_filters: Dict[str, Any], 
        joins: List[Dict[str, Any]]
    ) -> Set[str]:
        """Извлекает прямо связанные колонки из результатов связывания."""
        linked_columns = set()
        
        # Из связанных метрик
        for metric in linked_metrics:
            if metric.get('table') == table_name and 'column' in metric:
                linked_columns.add(metric['column'])
        
        # Из связанных измерений
        for dimension in linked_dimensions:
            if dimension.get('table') == table_name and 'column' in dimension:
                linked_columns.add(dimension['column'])
        
        # Из фильтров
        for filter_item in (linked_filters.values() if isinstance(linked_filters, dict) else []):
            if isinstance(filter_item, dict):
                if filter_item.get('table') == table_name and 'column' in filter_item:
                    linked_columns.add(filter_item['column'])
        
        # Из джойнов
        for join in joins:
            pairs = join.get("column_pairs")
            if isinstance(pairs, list):
                for pair in pairs:
                    if not isinstance(pair, dict):
                        continue
                    if join.get("from_table") == table_name and pair.get("from_column"):
                        linked_columns.add(pair["from_column"])
                    if join.get("to_table") == table_name and pair.get("to_column"):
                        linked_columns.add(pair["to_column"])
                continue
            if join.get('from_table') == table_name and 'from_column' in join:
                linked_columns.add(join['from_column'])
            if join.get('to_table') == table_name and 'to_column' in join:
                linked_columns.add(join['to_column'])
        
        return linked_columns
    
    @staticmethod
    def check_type_compatibility(
        type1: str,
        type2: str,
        *,
        type_resolver: Optional[Any] = None,
    ) -> bool:
        """Проверяет совместимость типов колонок для джойнов.

        Fail-fast: пустые/``None`` типы считаются ошибкой схемы и приводят
        к ``ValueError``. Callers должны явно проверять наличие типов перед
        вызовом (см. ``schema_linking.join_validation`` и
        ``SchemaLinker._is_join_valid_against_schema``).

        EPIC 5.2: классификация типов и список compatibility-пар вынесены в
        yaml (``config/text_to_sql/type_categories.yaml``). ``type_resolver``
        — необязательный объект с методом ``get_type_category(t)``
        (например, DBPlugin). Если не передан — используется default-конфиг
        из yaml.
        """
        if not type1 or not type2:
            raise ValueError(
                f"check_type_compatibility requires both types to be non-empty; "
                f"got type1={type1!r}, type2={type2!r}"
            )

        from .type_categories_config import load_type_categories_config

        if type_resolver is not None and hasattr(type_resolver, "get_type_category"):
            # Если передан плагин/resolver — категоризируем через него и
            # затем проверяем совместимость по yaml-таблице compatibility
            # для уже резолвленных категорий. Это сохраняет плагинную
            # категоризацию даже для cross-category пар (не падаем обратно
            # на глобальный yaml для перекатегоризации type1/type2).
            cat1 = type_resolver.get_type_category(type1)
            cat2 = type_resolver.get_type_category(type2)
            return load_type_categories_config().is_compatible_categories(cat1, cat2)

        return load_type_categories_config().is_compatible(type1, type2)


# ========================================================================================
# УТИЛИТЫ ДЛЯ СТАТИСТИКИ И ЛОГИРОВАНИЯ СХЕМЫ
# ========================================================================================

class SchemaStatsHelper:
    """Утилиты для статистики и анализа схемы."""
    
    @staticmethod
    def log_schema_statistics(db_schema: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        """Логирует детальную статистику схемы БД."""
        from .utils import get_table_columns, get_table_description
        
        if not db_schema:
            logger.warning("Schema is empty - no tables found")
            return
            
        total_tables = len(db_schema)
        total_columns = 0
        pk_count = 0
        fk_count = 0
        tables_with_descriptions = 0
        columns_with_descriptions = 0
        
        for table_name, table_schema in db_schema.items():
            # Подсчитываем описания таблиц
            if get_table_description(table_schema):
                tables_with_descriptions += 1
            
            # Анализируем колонки
            table_columns = get_table_columns(table_schema)
            total_columns += len(table_columns)
            
            for col_name, col_info in table_columns.items():
                if isinstance(col_info, dict):
                    # Подсчитываем ключи
                    if is_pk(col_info):
                        pk_count += 1
                    if is_fk(col_info):
                        fk_count += 1
                    
                    # Подсчитываем описания колонок
                    if col_info.get('description'):
                        columns_with_descriptions += 1
        
        # Логируем основную статистику
        logger.info(f"📊 SCHEMA STATISTICS:")
        logger.info(f"  Tables: {total_tables}")
        logger.info(f"  Total columns: {total_columns}")
        logger.info(f"  Primary keys: {pk_count}")
        logger.info(f"  Foreign keys: {fk_count}")
        table_desc_pct = (tables_with_descriptions / total_tables * 100) if total_tables else 0.0
        column_desc_pct = (columns_with_descriptions / total_columns * 100) if total_columns else 0.0
        logger.info(f"  Tables with descriptions: {tables_with_descriptions}/{total_tables} "
                   f"({table_desc_pct:.1f}%)")
        logger.info(f"  Columns with descriptions: {columns_with_descriptions}/{total_columns} "
                   f"({column_desc_pct:.1f}%)")
        
        # Топ-10 самых больших таблиц
        table_sizes = []
        for table_name, table_schema in db_schema.items():
            table_columns = get_table_columns(table_schema)
            table_sizes.append((table_name, len(table_columns)))
        
        table_sizes.sort(key=lambda x: x[1], reverse=True)
        logger.info(f"  Largest tables:")
        for table_name, col_count in table_sizes[:10]:
            logger.info(f"    {table_name}: {col_count} columns")
    
    @staticmethod
    def optimize_schema_for_storage(db_schema: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Готовит схему БД к сериализации в каноническом виде ``{table: {"columns": {...}}}``.

        EPIC 3.3 / 3.21: lossless. Ранее функция удаляла поля
        ``constraint_type``, ``references``, ``not_null`` с falsy-значениями
        ("пустые дефолты"). После загрузки из persisted-файла исходную форму
        восстановить было невозможно — теряли seg-нулевые констрейнты, например
        explicit ``not_null=False`` (что отличимо от "поле не задано").

        Теперь все колоночные поля сохраняются ``as is``; единственная
        нормализация — приведение к единой канонической форме table-schema
        ``{"columns": {...}}`` плюс опциональное ``description`` таблицы.
        Round-trip ``optimize → restore`` остаётся идемпотентным.
        """
        from .utils import get_table_columns, get_table_description, set_table_description

        optimized_schema: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for table_name, table_schema in db_schema.items():
            optimized_table: Dict[str, Any] = {}
            has_explicit_columns = (
                isinstance(table_schema, dict)
                and isinstance(table_schema.get("columns"), dict)
            )

            if has_explicit_columns:
                optimized_table.update(
                    {key: value for key, value in table_schema.items() if key != "columns"}
                )
            else:
                # Legacy shape stores columns as top-level dicts. Only keys that
                # get_table_columns does not treat as columns can be copied safely.
                if isinstance(table_schema, dict) and "metadata" in table_schema:
                    optimized_table["metadata"] = table_schema["metadata"]

                table_description = get_table_description(table_schema)
                if table_description:
                    set_table_description(optimized_table, table_description)

            # Оптимизируем колонки — БЕЗ удаления каких-либо ключей (lossless)
            table_columns = get_table_columns(table_schema)
            optimized_columns: Dict[str, Any] = {}

            for col_name, col_info in table_columns.items():
                if isinstance(col_info, dict):
                    # shallow copy — атомарно сохраняем все поля, включая
                    # not_null=False / constraint_type="" / references="".
                    optimized_columns[col_name] = dict(col_info)
                else:
                    optimized_columns[col_name] = col_info

            if optimized_columns:
                optimized_table["columns"] = optimized_columns

            optimized_schema[table_name] = optimized_table

        return optimized_schema
