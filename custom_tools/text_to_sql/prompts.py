"""
LLM промпт-билдеры для различных этапов Text-to-SQL пайплайна
"""
import json
from typing import Dict, Any, List, Optional
from .dialects import get_current_dialect_label
from .schema_linking_examples_config import compose_schema_linking_domain_examples
from .pii_categories_config import compose_pii_description
from .utils import redact_text_to_sql_value


def _redact_prompt_value(value: Any) -> Any:
    return redact_text_to_sql_value(value)


def build_nlu_prompt(text: str) -> str:
    """Промпт для извлечения intent и entities из текста."""
    # JSON-обёртка для user-text: устраняет prompt injection через кавычки/переносы.
    escaped_text = json.dumps(_redact_prompt_value(text), ensure_ascii=False)
    return (
        "Определи намерение и извлеки сущности для Text-to-SQL. Верни ТОЛЬКО JSON вида\n"
        '{"intent": "...", "entities": {"metrics": [], "dimensions": [], "filters": {}}}.\n'
        "metrics: имена показателей (напр. revenue, count), dimensions: измерения (напр. date, region).\n"
        "filters: диапазоны дат (start,end YYYY-MM-DD), конкретные значения (region).\n\n"
        f"Текст (в JSON-кавычках):\n{escaped_text}"
    )


def build_nlp_prompt(text: str) -> str:
    """Промпт для токенизации и POS-тегирования."""
    # JSON-обёртка для user-text: устраняет prompt injection через кавычки/переносы.
    escaped_text = json.dumps(_redact_prompt_value(text), ensure_ascii=False)
    return (
        "Выдели токены и базовые POS-теги для русского текста. Верни ТОЛЬКО JSON вида "
        '{"tokens": [..], "pos_tags": [..]} '
        "где pos_tags строго такой же длины, как tokens, теги из набора [NOUN, VERB, NUM, DATE, ADP, OTHER].\n\n"
        f"Текст (в JSON-кавычках):\n{escaped_text}"
    )


def build_schema_linking_prompt(
    entities: Dict[str, Any],
    schema_str: str,
    profile: Optional[str] = None,
    dsn: Optional[str] = None,
) -> str:
    """Промпт для сопоставления сущностей со схемой БД.

    Конкретные имена колонок конкретного датасета в шаблоне не зашиты —
    они подгружаются из yaml-конфига
    ``config/text_to_sql/prompts/schema_linking_examples.yaml`` через
    выбранный профиль. По умолчанию используется пустой профиль
    ``default`` (доменных строк в промпт не добавляется); другие профили
    выбираются через аргумент ``profile`` или env
    ``TEXT_TO_SQL_SCHEMA_LINKING_PROFILE``.
    """
    dialect_label = get_current_dialect_label(dsn, strict=bool(dsn and str(dsn).strip()))
    domain_examples_block = compose_schema_linking_domain_examples(profile)
    safe_entities = _redact_prompt_value(entities)
    safe_schema_str = _redact_prompt_value(schema_str)
    return (
        f"Ты эксперт по схемам БД. Сопоставь сущности с таблицами и колонками схемы {dialect_label}.\n"
        "СТРОГОЕ ТРЕБОВАНИЕ ФОРМАТА: верни ТОЛЬКО ВАЛИДНЫЙ JSON, который НАЧИНАЕТСЯ с { и ЗАКАНЧИВАЕТСЯ на }.\n"
        "НЕ добавляй пояснений, текста, маркдауна. НЕ используй тройные кавычки.\n\n"
        'ОБЯЗАТЕЛЬНАЯ СТРУКТУРА: {"linked_entities": {"metrics": [], "dimensions": [], "filters": {}}, "joins": [], "unlinked_entities": []}\n\n'

        "КРИТИЧЕСКИ ВАЖНО ПРО JOINS:\n"
        "- Если сущности (metrics/dimensions) находятся в РАЗНЫХ таблицах → joins НЕ ДОЛЖЕН БЫТЬ ПУСТЫМ!\n"
        "- Найди хотя бы одну связь между таблицами через общие колонки\n"
        "- Пустой массив joins [] допустим ТОЛЬКО если все сущности в одной таблице\n\n"

        "АЛГОРИТМ ПОИСКА СВЯЗЕЙ (В ПОРЯДКЕ ПРИОРИТЕТА):\n"
        "1. Foreign Keys: ищи в описаниях колонок слова 'references', 'FK', 'foreign'.\n"
        "2. Колонки-идентификаторы (ВЫСШИЙ ПРИОРИТЕТ): имена, оканчивающиеся на _id/_code/_key, "
        "ссылки FK на первичный ключ другой таблицы.\n"
        "3. Одинаковые имена ID в обеих таблицах (table1.<x>_id = table2.<x>_id).\n"
        "4. Текстовые поля (НИЗШИЙ ПРИОРИТЕТ): колонки с суффиксом _name/_title — "
        "только если соответствующих ID-колонок не найдено.\n\n"

        "ПРИОРИТЕТ КОЛОНОК:\n"
        "- ВСЕГДА предпочитай числовые/символьные ID-колонки текстовым (description/name).\n"
        "- ID-поля надёжнее для джойнов: короче, индексируются, не зависят от регистра/локали.\n"
        "- Используй текстовые поля только если нет соответствующих ID.\n"
        "- ОДИН джойн на пару таблиц: не дублируй условие парой ID+name.\n\n"

        "ПРИМЕРЫ ШАБЛОНОВ ДЖОЙНОВ (структура, не доменные имена):\n"
        '+ ПРАВИЛЬНО: {{"from_table": "schema.fact", "from_column": "<ref>_id", "to_table": "schema.dim", "to_column": "id", "join_type": "LEFT"}}\n'
        '+ ПРАВИЛЬНО: {{"from_table": "schema.t1", "from_column": "<key>_id", "to_table": "schema.t2", "to_column": "<key>_id", "join_type": "LEFT"}}\n'
        '- НЕПРАВИЛЬНО: несколько джойнов между одной парой таблиц.\n'
        '- НЕПРАВИЛЬНО: <key>_id AND <key>_name одновременно в условии джойна.\n\n'

        f"{domain_examples_block}"

        f"ВХОДНЫЕ ДАННЫЕ:\nСущности: {json.dumps(safe_entities, ensure_ascii=False)}\n"
        # W2-T7 (security): schema_str — plain-text строка (результат build_schema_summary).
        # json.dumps оборачивает её в внешние кавычки и экранирует \n/\t/спецсимволы,
        # что даёт двойное кодирование: LLM видит "\"table(col:int...)\"" вместо
        # "table(col:int...)". Это принято осознанно ради единообразной injection-защиты
        # (тот же метод, что и для dict-entities): злонамеренные \n или " в именах
        # колонок не нарушают структуру промпта. При деградации качества schema-linking
        # рассмотреть замену на schema_str.replace('\n', ' ').
        f"Схема (JSON): {json.dumps(safe_schema_str, ensure_ascii=False)}\n\n"

        "ЗАДАЧА: Заполни linked_entities корректными table/column, а joins - связями между таблицами.\n"
        "ПРАВИЛА ДЖОЙНОВ:\n"
        "1. ПРИОРИТЕТ: <key>_id > id > <key>_name.\n"
        "2. ОДИН джойн между двумя таблицами — выбери ЛУЧШУЮ колонку.\n"
        "3. Если есть ID-колонка, парная name-колонка в джойне не нужна."
    )


def build_sql_safety_prompt(sql_query: str, dsn: str | None = None) -> str:
    """Промпт для LLM-аудита безопасности SQL."""
    dialect_label = get_current_dialect_label(dsn, strict=bool(dsn and str(dsn).strip()))
    # JSON-обёртка для SQL: устраняет risk оф prompt injection через кавычки.
    escaped_sql = json.dumps(_redact_prompt_value(sql_query), ensure_ascii=False)
    return (
        f"Проанализируй SQL на риски (безопасность/производительность) для {dialect_label}. Верни ТОЛЬКО JSON вида "
        '{"issues": [{"issue_type": "...", "description": "..."}]}. '
        f"Не упоминай DDL/DML если их нет.\nSQL (в JSON-кавычках):\n{escaped_sql}"
    )


def build_pii_detection_prompt(column_names: List[str], sensitivity: str = "medium") -> str:
    """Промпт для автоматического определения PII колонок.

    Список PII-категорий и их соответствие уровню чувствительности — это
    compliance-критичный QA-слой; он не должен быть вшит в Python (см.
    AGENTS.md, T4.6). Категории читаются из ``config/pii/categories.yaml``
    через :func:`compose_pii_description`. Юрисдикция выбирается через env
    ``PII_JURISDICTION`` (либо ``default_jurisdiction`` из yaml). Уровень
    ``sensitivity`` обязан быть одним из ``{low, medium, high}``, иначе
    поднимается ``ValueError``.
    """
    pii_description = compose_pii_description(sensitivity)
    return (
        f"Определи, какие из колонок содержат PII ({pii_description}). "
        'Верни ТОЛЬКО JSON {"columns": ["col1", "col2", ...]} с названиями колонок для маскирования.\n'
        f"Колонки: {json.dumps(column_names, ensure_ascii=False)}"
    )


def build_column_description_prompt_with_context(
    full_table_schema: Dict[str, Dict[str, Dict[str, str]]],
    cols_to_describe: Dict[str, Dict[str, str]],
    table_context: List[str],
    sample_data: List[Dict[str, Any]] = None,
    column_stats: Dict[str, Dict[str, Any]] = None,
    fk_previews: Dict[str, Dict[str, Any]] = None
) -> str:
    """Промпт для генерации описаний колонок и таблицы с полным контекстом базы данных."""
    
    table_name = list(full_table_schema.keys())[0]
    table_columns = full_table_schema[table_name]
    
    # Получаем текущее описание таблицы (если есть)
    current_table_description = table_columns.get("description", "").strip()
    
    # Получаем колонки таблицы в новом формате
    table_cols = table_columns.get("columns", {})
    
    # Формируем информацию о всех колонках таблицы для контекста
    all_columns_info = {}
    for col, meta in table_cols.items():
        if isinstance(meta, dict):
            all_columns_info[col] = {
                "type": meta.get("type", "UNKNOWN"),
                "description": meta.get("description", ""),
                "constraint": meta.get("constraint_type", ""),
                "references": meta.get("references", "")
            }
    safe_all_columns_info = _redact_prompt_value(all_columns_info)
    
    # Указываем какие колонки нужно описать
    columns_to_describe = list(cols_to_describe.keys())
    
    # Формируем секцию с примерами данных
    sample_data_section = ""
    if sample_data:
        safe_sample_data = _redact_prompt_value(sample_data)
        sample_data_section = (
            f"ПРИМЕРЫ РЕАЛЬНЫХ ДАННЫХ (топ-{len(sample_data)} записей):\n"
            f"{json.dumps(safe_sample_data, ensure_ascii=False, indent=2)}\n\n"
        )
    
    # Формируем секцию со статистиками колонок
    stats_section = ""
    if column_stats:
        stats_section = "СТАТИСТИКИ КОЛОНОК (для включения в описания):\n"
        for col_name, stats in column_stats.items():
            if col_name in columns_to_describe:
                col_info = cols_to_describe.get(col_name, {})
                col_type = col_info.get('type', 'UNKNOWN') if isinstance(col_info, dict) else 'UNKNOWN'
                
                stats_section += f"  {col_name} ({col_type}):\n"
                if 'distinct_count' in stats:
                    stats_section += f"    - Уникальных значений: {stats['distinct_count']}\n"
                if 'null_frac' in stats:
                    stats_section += f"    - Null значений: {stats['null_frac']*100:.1f}%\n"
                if 'sample_values' in stats and stats['sample_values']:
                    # Ограничиваем количество примеров для читаемости
                    sample_vals = _redact_prompt_value(stats['sample_values'][:5])  # Первые 5 значений
                    stats_section += f"    - Типичные значения: {', '.join(str(v) for v in sample_vals)}\n"
                    if len(stats['sample_values']) > 5:
                        stats_section += f"    - (всего {len(stats['sample_values'])} различных значений)\n"
        stats_section += "\n"

    # Формируем секцию с FK превью
    fk_section = ""
    if fk_previews:
        fk_section = "СВЯЗИ С ДРУГИМИ ТАБЛИЦАМИ (FK ПРЕВЬЮ):\n"
        for col_name, fk_info in fk_previews.items():
            if col_name in columns_to_describe:
                ref_table = fk_info.get('ref_table', '')
                preview_data = fk_info.get('preview_data', [])
                preview_columns = fk_info.get('preview_columns', [])
                
                fk_section += f"  {col_name} -> {ref_table}:\n"
                if preview_data and preview_columns:
                    fk_section += f"    Колонки: {', '.join(preview_columns)}\n"
                    fk_section += f"    Примеры связей:\n"
                    for i, row in enumerate(preview_data[:2]):
                        if isinstance(row, (list, tuple)):
                            safe_row = _redact_prompt_value(list(row))
                            row_str = ' | '.join([str(v)[:30] for v in safe_row])
                            fk_section += f"      {i+1}: {row_str}\n"
        fk_section += "\n"
    
    # Определяем, нужно ли описание таблицы
    need_table_description = not current_table_description.strip()
    
    return (
        f"Ты эксперт по базам данных. Анализируй таблицу '{table_name}' в контексте всей базы данных.\n\n"
        
        f"КОНТЕКСТ БАЗЫ ДАННЫХ:\n"
        f"Доступные таблицы: {', '.join(table_context)}\n\n"
        
        f"ПОЛНАЯ СХЕМА ТАБЛИЦЫ '{table_name}':\n"
        f"{json.dumps(safe_all_columns_info, ensure_ascii=False, indent=2)}\n\n"
        
        f"{sample_data_section}"
        f"{stats_section}"
        f"{fk_section}"
        
        f"ЗАДАЧА:\n"
        f"1. {'Создай описание таблицы (назначение, что хранит)' if need_table_description else 'Описание таблицы уже есть'}\n"
        f"2. Создай описания только для этих колонок: {columns_to_describe}\n\n"
        
        f"Учитывай:\n"
        f"- Связи между колонками внутри таблицы\n"
        f"- Foreign key связи с другими таблицами (используй FK превью)\n"
        f"- Назначение таблицы в общем контексте БД\n"
        f"- Стандартные конвенции именования\n"
        f"- Реальные значения данных для понимания содержимого\n"
        f"- Статистики колонок для понимания распределения данных\n\n"
        
        f"ВАЖНО для не текстовых колонок (числовые, даты, boolean):\n"
        f"- Включай конкретные примеры значений в описание\n"
        f"- Указывай диапазоны (если видны из данных)\n"
        f"- Примеры: 'Код территории (например: 2336, 2321, 2463)', 'Год отчета (2023)', 'Возрастная группа (10-14, 20-24, 30-34)'\n\n"
        
        f"Верни ТОЛЬКО JSON вида:\n"
        f"{{\n" +
        (f"  \"table_description\": {{\"{table_name}\": \"описание назначения таблицы\"}},\n" if need_table_description else "  \"table_description\": {},\n") +
        f"  \"descriptions\": {{\"{table_name}\": {{\"column\": \"описание с примерами\"}}}}\n"
        f"}}\n\n"
        f"Описания должны быть информативными (8-20 слов для колонок, 15-30 слов для таблицы) на русском языке и включать примеры для числовых/категориальных колонок."
    )
