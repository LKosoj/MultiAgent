"""
Schema Metadata - константы, вспомогательные функции и утилиты для работы с метаданными схемы БД

Списки значимости колонок (high_priority_exact, medium_priority_patterns,
critical_description_keywords) вынесены в config/text_to_sql/significance.yaml
и грузятся через ``significance_config.load_significance_config``. См. T4.3
в AGENTS.md: доменно-зависимые термины не должны быть вшиты в код.
"""
import logging
from typing import Dict, List, Any, Optional, Set

from .significance_config import load_significance_config

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


# ========================================================================================
# КЛАСС ДЛЯ РАБОТЫ С МЕТАДАННЫМИ КОЛОНОК
# ========================================================================================

class ColumnMetadataHelper:
    """Утилиты для работы с метаданными колонок."""
    
    @staticmethod
    def is_semantic_significant_column(col_name: str, col_info: Dict[str, Any]) -> bool:
        """Определяет, является ли колонка семантически значимой.

        Списки значимости берутся из yaml-конфига (см. significance_config).
        Если конфиг отсутствует — поднимется ``FileNotFoundError``, без
        молчаливых дефолтов.
        """
        if not col_name:
            return False

        col_name_lower = col_name.lower()
        config = load_significance_config()

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
            description = col_info.get('description', '').lower()
            if description:
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
            r'(?<![A-Za-z0-9])'
            + re.escape(pattern)
            + r'(?![A-Za-z0-9])'
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

            # Сохраняем описание таблицы если есть
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
