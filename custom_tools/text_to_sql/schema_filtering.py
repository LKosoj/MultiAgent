"""
Schema Filtering - фильтрация и оптимизация схемы для связывания
"""
import os
import logging
from typing import Dict, Iterable, List, Any, Optional, Set
from .utils import (
    coerce_strict_bool,
    get_table_columns,
    get_table_description,
    set_table_description,
)
from .schema_metadata import ColumnMetadataHelper

logger = logging.getLogger(__name__)


def _redact_schema_filtering_error(error: Exception) -> str:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        return str(redact_pii_in_payload(_redact_payload(str(error))))
    except Exception:
        return "<redacted>"


class MorphemesIndexUnavailable(RuntimeError):
    """NLU morphemes index is required but cannot be loaded.

    Поднимается, когда yaml NLU morphemes недоступен (FileNotFoundError,
    невалидный yaml, отсутствие зависимости) и при этом включён режим
    ``TEXT_TO_SQL_MORPHEMES_REQUIRED=1`` (default). Тихий substring-only
    режим разрешён только через явный opt-out
    ``TEXT_TO_SQL_MORPHEMES_REQUIRED=0``.
    """


def _expand_entity_tokens(
    entity_lower: str,
    morphemes_index: Optional[Dict[str, List[str]]],
) -> List[str]:
    """Расширяет entity до набора поисковых токенов через morphemes (3.12).

    Если ``morphemes_index`` передан и entity совпадает с canonical (или
    одной из его морфем) — возвращаются все морфемы канонической группы.
    Это убирает чисто substring-матчинг без учёта словоформ.

    Backward-compat: при ``morphemes_index is None`` возвращается только
    исходная entity, поведение не меняется.
    """
    if not morphemes_index:
        return [entity_lower]

    tokens: List[str] = [entity_lower]
    # entity сам по себе — canonical?
    if entity_lower in morphemes_index:
        tokens.extend(morphemes_index[entity_lower])
        return list(dict.fromkeys(tokens))

    # entity — словоформа одной из морфем?
    for canonical, morphemes in morphemes_index.items():
        if any(m and m in entity_lower for m in morphemes):
            tokens.append(canonical)
            tokens.extend(morphemes)
            break

    return list(dict.fromkeys(tokens))


def _try_load_morphemes_index() -> Optional[Dict[str, List[str]]]:
    """Пытается собрать индекс {canonical: [morphemes]} из yaml.

    По умолчанию (``TEXT_TO_SQL_MORPHEMES_REQUIRED=1``) недоступность yaml —
    это явная ошибка конфигурации: возвращать ``None`` нельзя, поскольку это
    молча деградирует matching до substring-only, что AGENTS.md запрещает.
    Чтобы разрешить старое поведение, нужен явный opt-out
    ``TEXT_TO_SQL_MORPHEMES_REQUIRED=0`` — тогда выводится warning DEGRADED
    и возвращается ``None``.

    Программерские баги (TypeError, AttributeError и т.п.) всегда пробрасываются
    наружу.
    """
    try:
        from .nlu_config import load_nlu_morphemes  # local import to break cycle

        cfg = load_nlu_morphemes()
    except (FileNotFoundError, ValueError, ImportError) as exc:
        required = coerce_strict_bool(
            os.getenv("TEXT_TO_SQL_MORPHEMES_REQUIRED", "1"),
            default=True,
            field_name="TEXT_TO_SQL_MORPHEMES_REQUIRED",
        )
        if required:
            raise MorphemesIndexUnavailable(
                f"NLU morphemes config is required but not available: {exc}"
            ) from exc
        logger.warning(
            "DEGRADED: NLU morphemes config недоступен (%s); substring-only "
            "fallback разрешён через TEXT_TO_SQL_MORPHEMES_REQUIRED=0",
            exc,
        )
        return None
    if not getattr(cfg, "enabled", False):
        return None

    index: Dict[str, List[str]] = {}
    for group in list(cfg.intents) + list(cfg.dimensions):
        canonical = group.get("canonical")
        morphemes = list(group.get("morphemes") or [])
        if isinstance(canonical, str) and canonical:
            index[canonical.lower()] = [m.lower() for m in morphemes if isinstance(m, str)]
    return index or None


def _any_token_matches(tokens: Iterable[str], haystack: str) -> bool:
    return any(token and token in haystack for token in tokens)


class SchemaColumnFilter:
    """Фильтр колонок схемы по релевантности."""
    
    def filter_relevant_columns(
        self, 
        table_name: str,
        full_table_schema: Dict[str, Dict[str, Any]], 
        linked_metrics: List[Dict[str, Any]], 
        linked_dimensions: List[Dict[str, Any]], 
        linked_filters: Dict[str, Any], 
        joins: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Фильтрует колонки таблицы, оставляя только семантически и функционально значимые."""
        
        # Получаем колонки таблицы в новом формате
        table_columns = get_table_columns(full_table_schema)
        
        # 1. Извлекаем прямо связанные колонки (ОБЯЗАТЕЛЬНЫЕ)
        directly_linked = ColumnMetadataHelper.extract_directly_linked_columns(
            table_name, linked_metrics, linked_dimensions, linked_filters, joins
        )
        
        # 2. Извлекаем ключевые колонки (PK/FK)
        key_columns = ColumnMetadataHelper.get_key_columns(full_table_schema)
        
        # 3. Находим семантически значимые колонки
        semantic_columns = set()
        for col_name, col_info in table_columns.items():
            if ColumnMetadataHelper.is_semantic_significant_column(col_name, col_info):
                semantic_columns.add(col_name)
        
        # Объединяем все категории
        selected_columns = directly_linked | key_columns | semantic_columns
        
        # АГРЕССИВНАЯ ФИЛЬТРАЦИЯ: если включено - берем только связанные + минимум ключевых
        aggressive_mode = os.getenv("SCHEMA_AGGRESSIVE_FILTERING", "0") == "1"
        if aggressive_mode:
            from .schema_metadata import is_pk
            # В агрессивном режиме включаем только:
            # 1. Прямо связанные колонки (ОБЯЗАТЕЛЬНО)
            # 2. Только primary keys (для возможных соединений)
            minimal_key_columns = set()
            for col_name, col_info in get_table_columns(full_table_schema).items():
                if isinstance(col_info, dict) and is_pk(col_info):
                    minimal_key_columns.add(col_name)
            
            selected_columns = directly_linked | minimal_key_columns
            logger.debug(f"  Aggressive mode: reduced from {len(directly_linked | key_columns | semantic_columns)} to {len(selected_columns)} columns")
        
        # Если таблица числится среди required (directly_linked непуст),
        # но после фильтрации не осталось ни одной колонки — добавлять её
        # в filtered_schema бессмысленно (пустой контекст не даст LLM ни
        # выбрать колонку, ни построить join). Громко логируем и
        # сигнализируем skip отдельным флагом, чтобы вызывающая сторона
        # могла отфильтровать таблицу из итогового context'а.
        if not selected_columns and directly_linked:
            logger.warning(
                "Table '%s' has 0 columns after filtering — skipping "
                "(was referenced by linked entities/joins)",
                table_name,
            )
            return {"__skip__": True}

        # Строим отфильтрованную схему таблицы
        filtered_table_schema = {}

        # Сохраняем описание таблицы если есть
        table_description = get_table_description(full_table_schema)
        if table_description:
            set_table_description(filtered_table_schema, table_description)

        # Добавляем только отобранные колонки
        filtered_columns = {}
        for col_name in selected_columns:
            if col_name in table_columns:
                filtered_columns[col_name] = table_columns[col_name]

        if filtered_columns:
            filtered_table_schema["columns"] = filtered_columns
        
        # Логируем статистику фильтрации
        total_count = len(table_columns)
        filtered_count = len(filtered_columns)
        linked_count = len(directly_linked)
        key_count = len(key_columns)
        semantic_count = len(semantic_columns)
        
        logger.info(f"Table {table_name}: filtered {filtered_count}/{total_count} columns "
                   f"(linked={linked_count}, keys={key_count}, semantic={semantic_count})")
        logger.debug(f"  Linked columns: {sorted(directly_linked)}")
        logger.debug(f"  Key columns: {sorted(key_columns)}")  
        logger.debug(f"  Semantic columns: {sorted(semantic_columns)}")
        
        return filtered_table_schema


class SchemaContextBuilder:
    """Строитель контекста схемы для связывания."""
    
    def __init__(self, memory_manager):
        self.memory_manager = memory_manager
        self.column_filter = SchemaColumnFilter()
    
    def build_relevant_schema_context(
        self, 
        linked_metrics: List[Dict[str, Any]], 
        linked_dimensions: List[Dict[str, Any]], 
        linked_filters: Dict[str, Any], 
        joins: List[Dict[str, Any]], 
        full_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Строит контекст схемы только с релевантными таблицами и их отфильтрованными колонками."""

        # W4-T2: единый источник истины для required-таблиц
        # (linked entities + bridge-таблицы из joins).
        from .schema_linking.join_validation import compute_required_tables

        relevant_tables = compute_required_tables(
            linked_metrics, linked_dimensions, linked_filters,
            validated_joins=joins,
        )
        
        # Дополнительно ищем семантически релевантные таблицы через память
        # (этот поиск уже был выполнен в _llm_linking, но добавляем для полноты)
        try:
            # Извлекаем ключевые слова из всех сущностей для поиска дополнительных таблиц
            entity_keywords = []
            for metric in linked_metrics:
                if 'name' in metric:
                    entity_keywords.append(metric['name'])
            for dimension in linked_dimensions:
                if 'name' in dimension:
                    entity_keywords.append(dimension['name'])
            
            if entity_keywords:
                memory_relevant_tables = self.memory_manager.find_semantic_relevant_tables(
                    entity_keywords,
                    dsn=dsn,
                )
                relevant_tables.update(memory_relevant_tables)
                logger.info(f"Added {len(memory_relevant_tables)} semantically relevant tables from memory: {memory_relevant_tables}")
        
        except Exception as e:
            logger.warning(
                "Failed to find additional relevant tables through memory: %s",
                _redact_schema_filtering_error(e),
            )
        
        # Строим отфильтрованную схему с отобранными колонками релевантных таблиц
        filtered_schema = {}
        total_original_columns = 0
        total_filtered_columns = 0
        
        for table_name in relevant_tables:
            if table_name in full_schema:
                # Применяем фильтрацию колонок
                filtered_table = self.column_filter.filter_relevant_columns(
                    table_name, full_schema[table_name],
                    linked_metrics, linked_dimensions, linked_filters, joins
                )
                # Skip-маркер от column_filter — таблица не имеет ни одной
                # релевантной колонки, добавлять её в context нельзя.
                if isinstance(filtered_table, dict) and filtered_table.get("__skip__"):
                    continue
                filtered_schema[table_name] = filtered_table

                # Подсчитываем статистику
                original_cols = len(get_table_columns(full_schema[table_name]))
                filtered_cols = len(get_table_columns(filtered_table))
                total_original_columns += original_cols
                total_filtered_columns += filtered_cols
        
        # Общая статистика оптимизации
        if total_original_columns > 0:
            reduction_ratio = (total_original_columns - total_filtered_columns) / total_original_columns * 100
            logger.info(f"Schema context optimized: {total_filtered_columns}/{total_original_columns} columns "
                       f"({reduction_ratio:.1f}% reduction) across {len(filtered_schema)} tables")
        else:
            logger.info(f"Built relevant schema context with {len(filtered_schema)} tables: {list(filtered_schema.keys())}")
        
        return filtered_schema


# ========================================================================================
# УТИЛИТЫ ДЛЯ ФИЛЬТРАЦИИ СХЕМ
# ========================================================================================

class SchemaRelevanceFilter:
    """Фильтр релевантности схем и таблиц.

    ПРИМЕЧАНИЕ (deprecated/unconnected): класс не используется в prod-пайплайне.
    Методы find_relevant_tables_by_entities и score_table_relevance покрыты
    тестами (tests/test_text_to_sql_epic3_block_nlu_config.py, 5 ссылок), поэтому
    класс сохранён, но не подключён к prod-флоу. Перед включением согласовать
    с командой: возможен дублёж с SchemaContextBuilder/SchemaColumnFilter.
    """
    
    @staticmethod
    def find_relevant_tables_by_entities(
        entities: List[str],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        *,
        morphemes_index: Optional[Dict[str, List[str]]] = None,
    ) -> Set[str]:
        """Находит релевантные таблицы по именам сущностей.

        При наличии ``morphemes_index`` (или включённого yaml,
        см. ``_try_load_morphemes_index``) entity расширяется морфемами:
        substring проверяется по каждой словоформе, а не только по самой
        entity (3.12). Без yaml поведение идентично прежнему substring.
        """
        relevant_tables = set()
        index = morphemes_index if morphemes_index is not None else _try_load_morphemes_index()

        for entity in entities:
            entity_lower = entity.lower()
            tokens = _expand_entity_tokens(entity_lower, index)

            # Ищем прямые совпадения в именах таблиц
            for table_name in db_schema.keys():
                table_name_lower = table_name.lower()

                # Точное совпадение
                if entity_lower == table_name_lower:
                    relevant_tables.add(table_name)
                    continue

                # Совпадение части имени по любому из morpheme-токенов
                if (
                    _any_token_matches(tokens, table_name_lower)
                    or table_name_lower in entity_lower
                ):
                    relevant_tables.add(table_name)
                    continue

                # Проверяем описание таблицы
                table_schema = db_schema[table_name]
                table_description = get_table_description(table_schema)
                if table_description and _any_token_matches(
                    tokens, table_description.lower()
                ):
                    relevant_tables.add(table_name)
                    continue

                # Проверяем колонки таблицы
                table_columns = get_table_columns(table_schema)
                for col_name, col_info in table_columns.items():
                    if _any_token_matches(tokens, col_name.lower()):
                        relevant_tables.add(table_name)
                        break

                    if isinstance(col_info, dict):
                        col_desc = col_info.get('description', '')
                        if col_desc and _any_token_matches(tokens, col_desc.lower()):
                            relevant_tables.add(table_name)
                            break

        return relevant_tables
    
    @staticmethod
    def score_table_relevance(
        table_name: str,
        table_schema: Dict[str, Dict[str, Any]],
        entities: List[str],
        *,
        morphemes_index: Optional[Dict[str, List[str]]] = None,
    ) -> float:
        """Вычисляет релевантность таблицы для списка сущностей.

        Substring-матчинг расширён морфемами из yaml (см. 3.12). При
        отключённом yaml поведение остаётся прежним substring-only.
        """
        score = 0.0
        index = morphemes_index if morphemes_index is not None else _try_load_morphemes_index()

        # Проверяем соответствие имени таблицы
        table_name_lower = table_name.lower()
        for entity in entities:
            entity_lower = entity.lower()
            tokens = _expand_entity_tokens(entity_lower, index)

            # Точное совпадение
            if entity_lower == table_name_lower:
                score += 100.0
            # Частичное совпадение по morpheme-токенам
            elif _any_token_matches(tokens, table_name_lower) or table_name_lower in entity_lower:
                score += 50.0

        # Проверяем описание таблицы
        table_description = get_table_description(table_schema)
        if table_description:
            desc_lower = table_description.lower()
            for entity in entities:
                tokens = _expand_entity_tokens(entity.lower(), index)
                if _any_token_matches(tokens, desc_lower):
                    score += 20.0

        # Проверяем колонки
        table_columns = get_table_columns(table_schema)
        matching_columns = 0

        for col_name, col_info in table_columns.items():
            col_name_lower = col_name.lower()

            for entity in entities:
                entity_lower = entity.lower()
                tokens = _expand_entity_tokens(entity_lower, index)

                # Совпадение имени колонки
                if _any_token_matches(tokens, col_name_lower):
                    matching_columns += 1
                    score += 10.0
                    break

                # Совпадение описания колонки
                if isinstance(col_info, dict):
                    col_desc = col_info.get('description', '')
                    if col_desc and _any_token_matches(tokens, col_desc.lower()):
                        matching_columns += 1
                        score += 5.0
                        break
        
        # Бонус за количество совпадающих колонок
        if matching_columns > 0:
            score += matching_columns * 2.0
        
        # Нормализуем по количеству колонок (чтобы большие таблицы не имели преимущества).
        # Применяем только если хотя бы одна колонка совпала — иначе обнуление
        # score умножением на 0 скрыло бы точное совпадение имени таблицы.
        total_columns = len(table_columns)
        if total_columns > 0 and matching_columns > 0:
            score = score * (matching_columns / total_columns) * 100
        
        return score
    
    @staticmethod
    def filter_top_relevant_tables(
        db_schema: Dict[str, Dict[str, Dict[str, Any]]], 
        entities: List[str], 
        max_tables: int = 10
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Фильтрует топ N самых релевантных таблиц."""
        table_scores = []
        
        for table_name, table_schema in db_schema.items():
            score = SchemaRelevanceFilter.score_table_relevance(table_name, table_schema, entities)
            if score > 0:
                table_scores.append((table_name, score))
        
        # Сортируем по релевантности
        table_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Берем топ N таблиц
        top_tables = table_scores[:max_tables]
        
        # Строим отфильтрованную схему
        filtered_schema = {}
        for table_name, score in top_tables:
            filtered_schema[table_name] = db_schema[table_name]
        
        if top_tables:
            logger.info(f"Filtered to top {len(top_tables)} relevant tables:")
            for table_name, score in top_tables:
                logger.info(f"  {table_name}: {score:.1f}")
        
        return filtered_schema
