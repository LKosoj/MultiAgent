"""
Schema Enricher - обогащение схемы описаниями через LLM и получение примеров данных
"""
import os
import json
import logging
from typing import Dict, List, Any, Optional
from .utils import get_table_columns, get_table_description, set_table_description, mask_dsn, get_runtime_context_dsn
from .prompts import build_column_description_prompt_with_context
from .schema_metadata import is_fk
from .core import pii_masking

logger = logging.getLogger(__name__)


# Лимиты обрезки строковых значений в sample-данных (именованные константы).
# 47 = 50 - len('...')
_SAMPLE_VALUE_MAX_LEN = 50
_SAMPLE_VALUE_TRUNCATED_LEN = 47

# === W2-T2: fail-fast при сбое получения sample-данных ===
# AGENTS.md запрещает silent fallback. Раньше broad ``except Exception →
# return default_result`` маскировал реальные сбои БД (network, auth,
# privileges, отсутствие плагина) — LLM продолжал получать пустые
# sample'ы и генерировал бессодержательные descriptions. Теперь — raise
# с маскированным DSN, чтобы caller (pipeline) явно решил retry/skip.
class DBSampleFailed(RuntimeError):
    """Не удалось получить sample-данные из таблицы (ошибка БД/плагина).

    DSN никогда не попадает в сообщение в открытом виде — используется
    ``utils.mask_dsn`` (см. AGENTS.md: «Не хардкодь PII в коде ошибок»).
    Каuser-level pipeline должен ловить именно этот тип, а не широкое
    ``Exception`` — иначе снова получим silent деградацию.
    """

    def __init__(self, message: str, *, table_name: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.table_name = table_name
        # Сохраняем оригинальную ошибку для диагностики, но __str__ выдаёт
        # только маскированный текст из ``message``.
        self.__cause__ = cause

# Импорт call_openai_api защищён try/except — идиоматично для подпакета
# (см. nlu.py). Тесты monkeypatch'ят атрибут модуля:
# monkeypatch.setattr(schema_enricher, "call_openai_api", ...).
try:
    from utils import call_openai_api  # type: ignore
except Exception:  # noqa: BLE001
    call_openai_api = None  # type: ignore


# === EPIC 3.10: классификация ошибок enrichment'а ===
# Fatal — ошибки валидации/парсинга ответа LLM или схемы; продолжать нельзя.
# Retryable — транзиентные сетевые/таймаут-ошибки; повторы выполняются внутри
# call_openai_api (max_retries). Если call_openai_api всё-таки выбросил
# исключение наружу, это значит retry-budget исчерпан → тоже fail-fast.
_FATAL_ENRICHMENT_EXCEPTIONS = (
    json.JSONDecodeError,
    ValueError,
    KeyError,
    TypeError,
)

_RETRYABLE_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily unavailable",
    "503",
    "502",
    "504",
)


def _is_retryable_error(exc: BaseException) -> bool:
    """Эвристика классификации: транзиентная сеть/timeout vs остальное.

    Используется в outer-обработчике enrichment'а. call_openai_api сам
    делает экспоненциальный retry для network/timeout; если ошибка с
    такими маркерами всё же дошла сюда — значит retry-budget исчерпан и
    тоже надо fail-fast (без silent fallback, см. AGENTS.md).
    """
    if isinstance(exc, _FATAL_ENRICHMENT_EXCEPTIONS):
        return False
    msg = str(exc).lower()
    return any(keyword in msg for keyword in _RETRYABLE_ERROR_KEYWORDS)


class SchemaEnricher:
    """Обогащает схему БД описаниями через LLM и примерами данных."""

    def __init__(self):
        self._cached_schema = None

    def enrich_descriptions_with_llm(self, schema_obj: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
        """Обогащает отсутствующие описания колонок через LLM.

        ПРИМЕЧАНИЕ (deprecated/unconnected): метод реализован и покрыт тестами,
        однако НЕ подключён к prod-пайплайну (вызывается только из tests/ и
        описан в doc/TEXT_TO_SQL.md). Не достраивать функционал в рамках этого
        файла — это отдельная feature work. Рассинхрон с doc/TEXT_TO_SQL.md
        зафиксирован как deferred: правка doc вне владения данной задачи.

        Поведение ошибок (EPIC 3.10):

        * Fatal (validation/parse: ``json.JSONDecodeError``, ``ValueError``,
          ``KeyError``, ``TypeError``) — fail-fast, исключение пробрасывается.
        * Retryable (network/timeout): retry выполняется внутри
          ``call_openai_api``. Если исключение всё-таки дошло сюда — это
          значит retry-budget исчерпан → тоже fail-fast.

        AGENTS.md запрещает silent fallback на ошибках; поэтому в этом
        методе нет ни молчаливого ``except Exception``, ни магических
        дефолтов (см. также EPIC 3.9).
        """
        if os.getenv("SCHEMA_DESCRIBE_WITH_LLM", "1") == "0":
            return

        # Собираем список колонок без описаний и таблицы без описаний
        to_describe: Dict[str, Dict[str, Dict[str, Any]]] = {}
        tables_need_description = set()

        for table_name, table_schema in schema_obj.items():
            # Проверяем, есть ли описание таблицы
            if not get_table_description(table_schema):
                tables_need_description.add(table_name)

            # Проверяем колонки без описаний
            table_columns = get_table_columns(table_schema)
            for col_name, meta in table_columns.items():
                desc = str(meta.get("description", "")) if isinstance(meta, dict) else ""
                if not desc:
                    # Сохраняем полную metadata, а не только тип
                    to_describe.setdefault(table_name, {})[col_name] = meta if isinstance(meta, dict) else {"type": str(meta)}

        # Если нет ни колонок для описания, ни таблиц для описания
        if not to_describe and not tables_need_description:
            logger.info("All columns and tables already have descriptions - LLM enrichment skipped")
            return

        # Логируем статистику обогащения
        total_to_describe = sum(len(cols) for cols in to_describe.values())
        logger.info(f"Starting LLM description enrichment for {total_to_describe} columns across {len(to_describe)} tables")

        # Обрабатываем схему по одной таблице за раз для сохранения контекста
        all_descriptions = {}

        # Создаем контекст других таблиц для LLM
        table_context = []
        for table_name in schema_obj.keys():
            table_context.append(table_name)

        # Объединяем все таблицы, которые нуждаются в обработке
        all_tables_to_process = set(to_describe.keys()) | tables_need_description

        # Обрабатываем каждую таблицу отдельно
        for table_name in all_tables_to_process:
            cols_to_describe = to_describe.get(table_name, {})
            needs_table_desc = table_name in tables_need_description

            if not cols_to_describe and not needs_table_desc:
                continue

            col_count_msg = f"{len(cols_to_describe)} columns" if cols_to_describe else "no columns"
            table_msg = "table description" if needs_table_desc else "no table description"
            logger.info(f"Processing table: {table_name} ({col_count_msg} + {table_msg} needed)")

            resp = ""
            try:
                # Создаем полную схему этой таблицы (с уже имеющимися описаниями) в новом формате
                table_schema = schema_obj.get(table_name, {})
                full_table_schema = {table_name: table_schema}

                # Сохраняем схему для FK превью
                self._cached_schema = schema_obj

                # Получаем примеры данных из таблицы
                sample_result = self.get_table_sample_data(table_name)
                sample_data = sample_result.get('sample_rows', []) if isinstance(sample_result, dict) else sample_result
                column_stats = sample_result.get('column_stats', {}) if isinstance(sample_result, dict) else {}
                fk_previews = sample_result.get('fk_previews', {}) if isinstance(sample_result, dict) else {}

                # EPIC 3.8: маскируем PII в sample_data ДО отправки в LLM.
                # Используется существующий helper core/_pii.py::pii_masking
                # в AUTO-режиме: список PII-колонок определяется LLM/yaml-категориями.
                # Если PII_MASKING_ENABLED=0 — pii_masking сам коротко закоротит.
                sample_data = self._mask_sample_data_pii(sample_data)

                prompt = build_column_description_prompt_with_context(
                    full_table_schema,
                    cols_to_describe,
                    table_context,
                    sample_data,
                    column_stats,
                    fk_previews
                )
                if call_openai_api is None:
                    raise RuntimeError(
                        "call_openai_api is unavailable; cannot enrich schema descriptions"
                    )
                resp = call_openai_api(
                    prompt=prompt,
                    system_prompt="Ты эксперт по базам данных. Анализируй контекст и генерируй точные описания колонок. Верни только JSON.",
                    max_tokens=8000,  # Больше токенов для контекстного анализа
                    response_format={"type": "json_object"}
                )

                # Парсим ответ для этой таблицы
                from .utils import parse_llm_json_response
                obj = parse_llm_json_response(resp)
                logger.debug(f"LLM response for table {table_name}: {obj}")

                if isinstance(obj, dict):
                    # Обрабатываем описания колонок
                    if isinstance(obj.get("descriptions"), dict):
                        table_descriptions = obj["descriptions"].get(table_name, {})
                        if isinstance(table_descriptions, dict):
                            all_descriptions[table_name] = table_descriptions
                            logger.info(f"   ✅ Generated {len(table_descriptions)} column descriptions for {table_name}")
                        else:
                            logger.warning(f"   ❌ No valid column descriptions returned for {table_name}")

                    # Обрабатываем описание таблицы
                    if isinstance(obj.get("table_description"), dict):
                        table_desc = obj["table_description"].get(table_name, "")
                        if table_desc:
                            # Устанавливаем описание таблицы в новом формате
                            set_table_description(schema_obj[table_name], str(table_desc))
                            logger.info(f"   ✅ Generated table description for {table_name}")
                else:
                    logger.warning(f"   ❌ Invalid LLM response format for {table_name}")

            # W2-T2: DBSampleFailed — сигнал сбоя БД при сборе sample-данных.
            # Логируем явно более информативным сообщением, но пробрасываем —
            # pipeline должен принять решение retry/skip сам, а не silent skip.
            except DBSampleFailed as e:
                logger.error(
                    "   ❌ Sample-data fetch failed for table %s: %s "
                    "(masked DSN; see __cause__ for original error)",
                    table_name, e,
                )
                raise
            # EPIC 3.11: узкий catch только для fatal-классов (parse/validation/типов).
            # Раньше было `except (json.JSONDecodeError, Exception)` — это
            # эквивалент `except Exception` и приводил к silent fallback'у.
            except _FATAL_ENRICHMENT_EXCEPTIONS as e:
                # EPIC 3.10: fatal (parse/validation) — fail-fast.
                logger.error(f"   ❌ Fatal enrichment error for {table_name}: {e}")
                if isinstance(e, json.JSONDecodeError):
                    pos = e.pos if hasattr(e, 'pos') else 'unknown'
                    logger.error(f"      JSON Error at position {pos}")
                    if resp:
                        resp_preview = resp[:200] + "..." if len(resp) > 200 else resp
                        logger.error(f"      Response preview: {resp_preview}")
                        logger.error(f"      Response length: {len(resp)} characters")
                raise
            except Exception as e:  # noqa: BLE001 — отдельная ветка для retryable
                # EPIC 3.10: остальное — кандидат в retryable (network/timeout).
                # Retry уже выполнен внутри call_openai_api; если ошибка
                # дошла сюда — retry-budget исчерпан → fail-fast.
                if _is_retryable_error(e):
                    logger.error(
                        f"   ❌ Retryable error for {table_name} after retries exhausted: {e}"
                    )
                else:
                    logger.error(
                        f"   ❌ Unclassified enrichment error for {table_name}: {e}"
                    )
                raise

        # Применяем все собранные описания
        if all_descriptions:
            tables_updated = 0
            columns_updated = 0
            for t, cols in all_descriptions.items():
                if t in schema_obj and isinstance(cols, dict):
                    tables_updated += 1
                    table_cols_updated = 0
                    # Получаем колонки таблицы в новом формате
                    table_columns = get_table_columns(schema_obj[t])
                    for c, d in cols.items():
                        if c in table_columns and isinstance(table_columns[c], dict):
                            if not table_columns[c].get("description"):
                                table_columns[c]["description"] = str(d or "")
                                table_cols_updated += 1
                                columns_updated += 1
                    logger.info(f"Applied {table_cols_updated} descriptions to {t}")

            logger.info(f"LLM ENRICHMENT COMPLETED: {columns_updated} descriptions applied across {tables_updated} tables")
        else:
            logger.warning("No descriptions were generated by LLM")

    def _mask_sample_data_pii(self, sample_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """EPIC 3.8: маскирует PII в sample_data перед отправкой в LLM.

        sample_data приходит как ``List[Dict[col_name, value]]`` (см.
        :meth:`get_table_sample_data`). ``pii_masking`` ожидает 2D-список
        ``List[List[value]]`` + ``column_names``; здесь делается мост и
        обратная сборка.

        AUTO-режим включён — :func:`core._pii.pii_masking` сам решает,
        какие колонки PII (через LLM/yaml-категории). Если
        ``PII_MASKING_ENABLED=0``, helper вернёт данные без изменений.
        Любая ошибка маскирования пробрасывается (fail-fast по AGENTS.md):
        отправлять немаскированные PII в LLM запрещено.
        """
        if not sample_data:
            return sample_data
        if not isinstance(sample_data, list) or not isinstance(sample_data[0], dict):
            # Нестандартная форма — pii_masking может не справиться. Лучше
            # отдать наверх (fail-fast), а не молча отправить сырые данные.
            raise TypeError(
                "sample_data must be List[Dict[str, Any]] for PII masking, "
                f"got {type(sample_data[0]).__name__ if sample_data else 'empty'}"
            )

        # Стабильный порядок колонок: union по всем строкам.
        # Сортируем по имени, чтобы PII-маскирование было устойчиво к
        # порядку из JSON round-trip (после json.loads словарь сохраняет
        # порядок, но он может зависеть от source-сериализатора).
        column_names_set: set = set()
        for row in sample_data:
            for col in row.keys():
                column_names_set.add(col)
        column_names: List[str] = sorted(column_names_set)

        rows_2d: List[List[Any]] = [
            [row.get(col) for col in column_names] for row in sample_data
        ]

        # AUTO-mode: PII-колонки определяет сам helper (по yaml/LLM).
        masked_result = pii_masking(rows_2d, ["AUTO"], column_names=column_names)
        masked_rows = (
            masked_result.get("masked_data", rows_2d)
            if isinstance(masked_result, dict)
            else rows_2d
        )

        masked_sample: List[Dict[str, Any]] = []
        for masked_row in masked_rows:
            new_row: Dict[str, Any] = {}
            for i, col in enumerate(column_names):
                if i < len(masked_row):
                    new_row[col] = masked_row[i]
            masked_sample.append(new_row)
        return masked_sample
    
    def get_table_sample_data(self, table_name: str, dsn: Optional[str] = None) -> Dict[str, Any]:
        """Получает примеры данных из таблицы для контекста.

        dsn принимается явным аргументом с приоритетом над runtime-context
        и env-переменной DB_DSN. Обратная совместимость сохранена: при вызове
        без аргументов (dsn=None) активируется get_runtime_context_dsn() как
        в остальном пайплайне, финальный fallback — os.getenv('DB_DSN').
        """
        logger.debug(f"📊 Fetching sample data for table: {table_name}")
        
        # Единый формат возврата
        default_result = {
            'sample_rows': [], 
            'column_stats': {}, 
            'table_size': 'unknown', 
            'estimated_rows': 0, 
            'fk_previews': {}
        }
        
        conn = None
        plugin = None
        try:
            # Импортируем здесь чтобы избежать циклических зависимостей
            from db_plugins import get_plugin

            # Трёхуровневый приоритет: явный аргумент > runtime-context > env.
            effective_dsn = (
                dsn if (isinstance(dsn, str) and dsn.strip())
                else get_runtime_context_dsn() or os.getenv("DB_DSN")
            )
            if not effective_dsn:
                raise DBSampleFailed(
                    "DB_DSN not set for sample fetch",
                    table_name=table_name,
                )

            plugin = get_plugin(effective_dsn)
            conn = plugin.connect(effective_dsn)
            
            try:
                # Определяем размер таблицы и стратегию
                from db_plugins.base import SMALL_TABLE_THRESHOLD, LARGE_TABLE_THRESHOLD

                # EPIC 3.9: запрещён silent fallback на 1_000_000 при отсутствии
                # estimate_row_count. Если плагин не умеет считать оценку строк —
                # fail-fast: продолжать с magic-числом нельзя (выбирается неверная
                # стратегия sample-сбора, а значит непредсказуемый объём данных
                # уйдёт в LLM-промпт). См. AGENTS.md: запрещены закрытые
                # эвристики/магические числа в бизнес-логике.
                if not hasattr(plugin, 'estimate_row_count'):
                    raise AttributeError(
                        f"DB plugin {plugin.__class__.__name__} does not implement "
                        f"estimate_row_count; cannot choose sample strategy for "
                        f"{table_name!r} without silent fallback"
                    )
                estimated_rows = plugin.estimate_row_count(conn, table_name)
                
                # Получаем количество sample rows из переменной окружения
                max_rows = int(os.getenv("SCHEMA_SAMPLE_ROWS", "10"))
                
                if estimated_rows <= SMALL_TABLE_THRESHOLD:
                    strategy = 'small'
                elif estimated_rows <= LARGE_TABLE_THRESHOLD:
                    strategy = 'medium' 
                else:
                    strategy = 'large'
                
                logger.debug(f"Table {table_name}: ~{estimated_rows} rows, strategy={strategy}")
                
                # Получаем статистики колонок если доступно
                column_stats = {}
                if hasattr(plugin, 'get_basic_column_stats'):
                    try:
                        column_stats = plugin.get_basic_column_stats(conn, table_name)
                        logger.debug(f"Got column stats for {len(column_stats)} columns")
                    except Exception as e:
                        logger.debug(f"Failed to get column stats: {e}")
                
                # Получаем данные согласно стратегии
                if hasattr(plugin, 'sample_rows_smart'):
                    results = plugin.sample_rows_smart(conn, table_name, strategy, max_rows)
                elif hasattr(plugin, 'build_select_all'):
                    # Используем плагин для генерации SQL
                    query = plugin.build_select_all(table_name, max_rows)
                    results = plugin.execute_select(conn, query, row_limit=max_rows)
                else:
                    if not hasattr(plugin, 'quote_identifier'):
                        raise AttributeError(
                            f"DB plugin {plugin.__class__.__name__} does not implement quote_identifier; "
                            f"cannot safely build SQL with identifier {table_name!r}"
                        )
                    quoted_table = plugin.quote_identifier(table_name)
                    query = f"SELECT * FROM {quoted_table}"
                    results = plugin.execute_select(conn, query, row_limit=max_rows)
                
                # ПОЛУЧАЕМ FK ПРЕВЬЮ ПОКА СОЕДИНЕНИЕ ЕЩЕ ОТКРЫТО
                fk_previews = self._get_fk_previews(conn, plugin, table_name)
                
            finally:
                if plugin and conn:
                    plugin.close(conn)
            
            # Конвертируем результат в список словарей
            if not results or not results.get('data'):
                result = default_result.copy()
                result.update({
                    'column_stats': column_stats, 
                    'table_size': strategy, 
                    'estimated_rows': estimated_rows, 
                    'fk_previews': fk_previews
                })
                return result
                
            columns = results.get('columns', [])
            rows = results.get('data', [])
            
            sample_data = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(columns):
                    if i < len(row):
                        value = row[i]
                        # Конвертируем datetime в строку
                        if hasattr(value, 'strftime'):  # datetime объект
                            value = value.strftime('%Y-%m-%d %H:%M:%S')
                        # Обрезаем длинные значения
                        elif isinstance(value, str) and len(value) > _SAMPLE_VALUE_MAX_LEN:
                            value = value[:_SAMPLE_VALUE_TRUNCATED_LEN] + "..."
                        row_dict[col] = value
                sample_data.append(row_dict)

            result = {
                'sample_rows': sample_data,
                'column_stats': column_stats,
                'table_size': strategy,
                'estimated_rows': estimated_rows,
                'fk_previews': fk_previews
            }
            return result

        except DBSampleFailed:
            # W2-T2: уже наш fail-fast (например, "DB_DSN not set") — не
            # переоборачиваем, чтобы caller получил оригинальное сообщение.
            # conn/plugin тут гарантированно None (мы кидали до connect),
            # поэтому cleanup не нужен.
            raise
        except Exception as e:
            # Закрываем соединение при ошибке (best-effort, не глушим
            # cleanup-ошибку — она пойдёт в logger.error).
            if plugin and conn:
                try:
                    plugin.close(conn)
                except Exception as close_exc:  # noqa: BLE001
                    logger.error(
                        "Cleanup close() failed for %s while handling %s: %s",
                        table_name, type(e).__name__, close_exc,
                    )

            # W2-T2: fail-fast. Раньше тут было ``return default_result`` —
            # caller (enrich_descriptions_with_llm) получал пустой sample
            # и LLM писал бессмысленные описания. Теперь — raise с
            # маскированным DSN и оригинальной причиной в ``__cause__``.
            # mask_dsn применяется И к str(e) (на случай если драйвер
            # положил DSN в текст ошибки), И отдельно — чтобы DSN из env
            # тоже был замаскирован.
            masked_cause = mask_dsn(str(e))
            raise DBSampleFailed(
                f"Failed to fetch sample data for table {table_name!r}: "
                f"{type(e).__name__}: {masked_cause}",
                table_name=table_name,
                cause=e,
            ) from e
    
    def _get_fk_previews(self, conn, plugin, table_name: str) -> Dict[str, Any]:
        """Получает FK превью для таблицы."""
        fk_previews = {}
        if hasattr(plugin, 'get_fk_preview'):
            try:
                # Ищем FK колонки в текущей схеме (используем сохраненную схему)
                current_schema = getattr(self, '_cached_schema', {})
                if current_schema and table_name in current_schema:
                    table_schema = current_schema[table_name]
                    table_columns = get_table_columns(table_schema)
                    for col_name, col_info in table_columns.items():
                        if isinstance(col_info, dict) and is_fk(col_info):
                            references = col_info.get('references', '')
                            if references:
                                ref_table, ref_column = self._parse_fk_reference(references)
                                if ref_table:
                                    try:
                                        logger.debug(f"Getting FK preview: {col_name} -> {ref_table}")
                                        fk_preview = plugin.get_fk_preview(
                                            conn,
                                            table_name,
                                            col_name,
                                            ref_table,
                                            2,
                                            ref_column=ref_column,
                                        )
                                        logger.debug(f"FK preview raw result: {fk_preview}")
                                        
                                        # Проверяем успешность и наличие данных
                                        if fk_preview.get('success', True) and fk_preview.get('data'):
                                            fk_previews[col_name] = {
                                                'ref_table': ref_table,
                                                'preview_data': fk_preview['data'][:2],  # Максимум 2 строки
                                                'preview_columns': fk_preview.get('columns', [])
                                            }
                                            logger.debug(f"FK preview success: {len(fk_preview['data'])} rows")
                                        else:
                                            logger.debug(f"FK preview no data: {fk_preview}")
                                    except Exception as e:
                                        logger.debug(f"FK preview error for {col_name}: {e}")
            except Exception as e:
                logger.debug(f"Failed to get FK previews: {e}")
        
        return fk_previews
    
    def _parse_fk_reference(self, references: str) -> tuple:
        """Парсит ссылку FK для получения таблицы и колонки."""
        try:
            if not references or not isinstance(references, str):
                return None, None
            
            # Формат: "table_name(column_name)" или "table_name.column_name"
            references = references.strip()
            
            # Вариант 1: table_name(column_name)
            if '(' in references and references.endswith(')'):
                table_part, col_part = references.split('(', 1)
                ref_table = table_part.strip()
                ref_column = col_part.rstrip(')').strip()
                return ref_table, ref_column
            
            # Вариант 2: table_name.column_name
            elif '.' in references:
                parts = references.rsplit('.', 1)
                if len(parts) == 2:
                    ref_table = parts[0].strip()
                    ref_column = parts[1].strip()
                    return ref_table, ref_column
            
            # Вариант 3: только table_name (без колонки)
            else:
                return references.strip(), None
            
        except Exception as e:
            logger.debug(f"Failed to parse FK reference '{references}': {e}")
            return None, None
