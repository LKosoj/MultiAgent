"""
Ограничители для работы со схемой БД при подготовке промптов.
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..schema_metadata import is_fk
from ..utils import get_table_columns

# Опциональный импорт sqlglot — для AST-пути enforce_row_limit.
try:
    import sqlglot as _sqlglot
    from sqlglot import expressions as _exp
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _sqlglot = None
    _exp = None
    _SQLGLOT_AVAILABLE = False

# Wall-time-защищённый parser shared с safety.py/schema_aware.py — иначе
# pathological SQL подвешивал бы enforce_row_limit. См. ``_parse_with_timeout``.
from .safety import _parse_with_timeout, _ParseTimeoutError

logger = logging.getLogger(__name__)

_VALID_STRATEGIES = ("relevance", "fk_centrality", "insertion")


class SchemaLimiter:
    """Ограничители для работы со схемой БД."""

    def __init__(
        self,
        priority_strategy: Optional[str] = None,
        max_tables: Optional[int] = None,
    ):
        self.desc_limit = int(os.getenv("SCHEMA_DESC_LIMIT", "120"))
        if max_tables is None:
            self.max_tables = int(os.getenv("SCHEMA_MAX_TABLES", "50"))
        else:
            self.max_tables = int(max_tables)
        self.max_columns = int(os.getenv("SCHEMA_MAX_COLUMNS", "20"))

        env_strategy = os.getenv("SCHEMA_PRIORITY_STRATEGY")
        # Argument > env > default("relevance")
        if priority_strategy is not None:
            strategy = priority_strategy
        elif env_strategy is not None and env_strategy.strip():
            strategy = env_strategy.strip()
        else:
            strategy = "relevance"

        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Unknown priority_strategy={strategy!r}. "
                f"Allowed: {list(_VALID_STRATEGIES)}"
            )
        self.priority_strategy = strategy

    # ------------------------------------------------------------------
    # Priority scoring helpers
    # ------------------------------------------------------------------
    def _score_by_relevance(
        self, db_schema: Dict[str, Any]
    ) -> Optional[Dict[str, float]]:
        """Берёт score из table_schema[relevance|weight|score].

        Если ни у одной таблицы нет такого поля — возвращает None
        (триггер для fallback на fk_centrality).
        """
        scores: Dict[str, float] = {}
        found_any = False
        for table_name, table_schema in db_schema.items():
            value: Any = None
            if isinstance(table_schema, dict):
                for key in ("relevance", "weight", "score"):
                    if key in table_schema:
                        value = table_schema.get(key)
                        break
            if value is None:
                scores[table_name] = 0.0
                continue
            try:
                scores[table_name] = float(value)
                found_any = True
            except (TypeError, ValueError):
                scores[table_name] = 0.0
        if not found_any:
            return None
        return scores

    def _score_by_fk_centrality(
        self, db_schema: Dict[str, Any]
    ) -> Dict[str, float]:
        """Считает inbound + outbound FK references на таблицу."""
        scores: Dict[str, float] = {name: 0.0 for name in db_schema}

        # outbound: сколько FK-колонок у самой таблицы
        # inbound: сколько FK-колонок других таблиц ссылается на неё
        for table_name, table_schema in db_schema.items():
            columns = get_table_columns(table_schema)
            for _col, meta in columns.items():
                if not isinstance(meta, dict):
                    continue
                if not is_fk(meta):
                    continue
                # outbound: эта таблица ссылается на кого-то
                scores[table_name] = scores.get(table_name, 0.0) + 1.0
                # inbound: distill target table name из references
                ref = meta.get("references", "")
                target = self._parse_reference_target(ref)
                if target:
                    # Поддержим и точное совпадение, и suffix-match
                    # (схема может содержать `schema.table` или `table`).
                    matched = self._match_target_table(target, db_schema)
                    if matched is not None:
                        scores[matched] = scores.get(matched, 0.0) + 1.0
        return scores

    @staticmethod
    def _parse_reference_target(ref: Any) -> Optional[str]:
        """Извлекает имя целевой таблицы из строки references.

        Поддерживает форматы:
          * "table"
          * "table(col)"
          * "schema.table"
          * "schema.table(col)"
        """
        if not isinstance(ref, str):
            return None
        s = ref.strip()
        if not s:
            return None
        # Отбрасываем "(col)" суффикс
        paren = s.find("(")
        if paren >= 0:
            s = s[:paren].strip()
        return s or None

    @staticmethod
    def _match_target_table(
        target: str, db_schema: Dict[str, Any]
    ) -> Optional[str]:
        """Находит имя таблицы в схеме по строке-таргету."""
        if target in db_schema:
            return target
        # Сравним по последнему сегменту (table) и по полному имени.
        target_short = target.split(".")[-1]
        for name in db_schema:
            if name == target:
                return name
            if name.split(".")[-1] == target_short:
                return name
        return None

    def _score_by_insertion(
        self, db_schema: Dict[str, Any]
    ) -> Dict[str, float]:
        """Сохраняет порядок вставки: чем раньше — тем выше score."""
        n = len(db_schema)
        return {name: float(n - idx) for idx, name in enumerate(db_schema)}

    # ------------------------------------------------------------------
    # Dispatch / ordering
    # ------------------------------------------------------------------
    def _order_tables(
        self,
        db_schema: Dict[str, Any],
        priority_strategy: Optional[str],
    ) -> List[Tuple[str, Any]]:
        strategy = priority_strategy or self.priority_strategy
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Unknown priority_strategy={strategy!r}. "
                f"Allowed: {list(_VALID_STRATEGIES)}"
            )

        original_index = {name: idx for idx, name in enumerate(db_schema)}

        if strategy == "insertion":
            # Дешёвый путь — порядок уже корректен.
            return list(db_schema.items())

        if strategy == "relevance":
            scores = self._score_by_relevance(db_schema)
            if scores is None:
                logger.warning(
                    "SchemaLimiter: priority_strategy='relevance' but no "
                    "relevance/weight/score field on tables; falling back to "
                    "'fk_centrality'."
                )
                scores = self._score_by_fk_centrality(db_schema)
        elif strategy == "fk_centrality":
            scores = self._score_by_fk_centrality(db_schema)
        else:  # pragma: no cover - guarded above
            raise ValueError(f"Unknown priority_strategy={strategy!r}")

        items = list(db_schema.items())
        items.sort(
            key=lambda kv: (-scores.get(kv[0], 0.0), original_index[kv[0]])
        )
        return items

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def limit_schema_for_prompt(
        self,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        priority_strategy: Optional[str] = None,
    ) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Ограничивает схему для включения в LLM промпт."""
        if not db_schema:
            return {}

        limited_schema = {}
        tables_items = self._order_tables(db_schema, priority_strategy)

        if self.max_tables > 0:
            tables_items = tables_items[: self.max_tables]

        for table_name, table_schema in tables_items:
            table_description = ""
            if isinstance(table_schema, dict):
                table_description = str(table_schema.get("description", "") or "")
            limited_columns = {}
            columns_items = list(get_table_columns(table_schema).items())

            if self.max_columns > 0:
                columns_items = columns_items[:self.max_columns]

            for col_name, meta in columns_items:
                if not isinstance(meta, dict):
                    # Пропускаем некорректную колонку
                    continue
                limited_meta = dict(meta)

                # Обрезаем описание если нужно
                if self.desc_limit > 0 and "description" in limited_meta:
                    desc = str(limited_meta["description"] or "")
                    if len(desc) > self.desc_limit:
                        # Обрезаем по границе слова и добавляем многоточие
                        trimmed = desc[:self.desc_limit].rsplit(" ", 1)[0] if " " in desc[:self.desc_limit] else desc[:self.desc_limit]
                        limited_meta["description"] = trimmed + "…"
                    else:
                        limited_meta["description"] = desc

                limited_columns[col_name] = limited_meta

            if isinstance(table_schema, dict) and "columns" in table_schema:
                limited_schema[table_name] = {
                    "description": table_description,
                    "columns": limited_columns,
                }
            else:
                limited_schema[table_name] = limited_columns

        return limited_schema

    def enforce_row_limit(
        self,
        sql: str,
        default_limit: Optional[int] = None,
        dialect: str = "postgres",
    ) -> str:
        """Гарантирует наличие LIMIT в SQL-запросе.

        UNUSED utility, designed for future caller (db_exec row-cap
        enforcement); not currently wired into pipeline. Сохраняем как
        контрактную функцию — её ValueError-контракт на multi-statement и
        timeout-семантика покрыты тестами, чтобы будущая интеграция в db_exec
        не сломала ожидания caller'а.

        Утилита для caller'а (например, db_exec) — НЕ вызывается автоматически
        внутри SchemaLimiter. Если запрос уже содержит LIMIT — возвращает как
        есть. Иначе дописывает ``LIMIT {default_limit}`` в конец.

        default_limit берётся из ``SQL_DEFAULT_ROW_LIMIT`` env (default 1000),
        если параметр явно не передан.
        """
        if default_limit is None:
            try:
                default_limit = int(os.getenv("SQL_DEFAULT_ROW_LIMIT", "1000"))
            except ValueError:
                default_limit = 1000

        if not isinstance(sql, str) or not sql.strip():
            return sql

        # AST-путь: парсим через sqlglot и смотрим на наличие limit-арга у
        # последнего statement (включая UNION/CTE).
        if _SQLGLOT_AVAILABLE and _sqlglot is not None and _exp is not None:
            # Wall-time таймаут парсинга — как в safety.py/schema_aware.py.
            # Без таймаута pathological SQL подвешивает enforce_row_limit.
            try:
                parse_timeout = float(
                    os.getenv("SQL_VALIDATE_PARSE_TIMEOUT_SEC", "5")
                )
            except ValueError:
                parse_timeout = 5.0
            try:
                parsed = _parse_with_timeout(
                    sql,
                    None if dialect == "ansi" else dialect,
                    parse_timeout,
                )
            except _ParseTimeoutError as e:
                # Timeout — критичный сигнал DoS-вектора, пробрасываем
                # наружу как RuntimeError, регресс к regex недопустим.
                raise RuntimeError(
                    "enforce_row_limit timed out parsing SQL"
                ) from e
            except Exception as e:
                # ParseError или другие ошибки sqlglot — сохраняем
                # ранее существовавший fail-graceful контракт: warning
                # + regex fallback. Timeout уже обработан выше.
                logger.warning(
                    "enforce_row_limit: sqlglot parse failed (%s); using regex fallback.",
                    e,
                )
                parsed = []
            statements = [s for s in parsed if s is not None]

            # Multi-statement guard — контрактная ошибка ДО внутреннего
            # try-блока, чтобы ValueError не оборачивалась в warning-fallback.
            if len(statements) > 1:
                raise ValueError(
                    "enforce_row_limit does not support multi-statement input"
                )

            try:
                if statements:
                    target = statements[0]
                    # DESCRIBE/EXPLAIN/SHOW и любые exp.Command не принимают
                    # LIMIT — возвращаем исходный SQL без модификаций.
                    introspection_classes = tuple(
                        cls for cls in (
                            getattr(_exp, name, None)
                            for name in ("Describe", "Command")
                        ) if cls is not None
                    )
                    if introspection_classes and isinstance(target, introspection_classes):
                        return sql
                    # Проверяем LIMIT ТОЛЬКО на корневом стейтменте (включая
                    # верхний Select/Union/With). `find_all(Limit)` обошёл бы
                    # подзапросы и тела CTE: `... WHERE id IN (SELECT id LIMIT
                    # 10)` или `WITH x AS (SELECT ... LIMIT 5) SELECT * FROM x`
                    # ложно считались бы уже лимитированными, и top-level
                    # row-cap не дописывался.
                    # Для WITH ... <body> сам limit обычно сидит в body
                    # (Select/Union), а не в With-ноде. Спускаемся в this,
                    # пока тип допускает это.
                    has_limit = False
                    if hasattr(target, "args") and target.args.get("limit") is not None:
                        has_limit = True
                    else:
                        # Аккуратное построение with_classes: если у текущей
                        # версии sqlglot нет ``exp.With`` — оставляем пустой
                        # tuple. ``isinstance(x, ())`` валидно и всегда False,
                        # а старый паттерн ``(getattr(...,()),)`` бросал бы
                        # TypeError, т.к. tuple в isinstance не может содержать
                        # сам tuple.
                        with_cls = getattr(_exp, "With", None)
                        with_classes = (with_cls,) if with_cls is not None else ()
                        inner = getattr(target, "this", None) if isinstance(target, with_classes) else None
                        if inner is not None and hasattr(inner, "args") and inner.args.get("limit") is not None:
                            has_limit = True
                    if has_limit:
                        return sql
                    rendered = target.sql(dialect=dialect)
                    return f"{rendered.rstrip(';').rstrip()} LIMIT {default_limit}"
            except Exception as e:
                logger.warning(
                    "enforce_row_limit: sqlglot path failed (%s); using regex fallback.",
                    e,
                )

        # Regex fallback: грубо ищем word-boundary LIMIT.
        # Скипаем introspection-команды, которые не принимают LIMIT.
        # AST-путь выше уже отсёк их через isinstance(exp.Describe/Command),
        # этот regex покрывает fallback (sqlglot недоступен или parse упал).
        if re.match(
            r"^\s*(DESCRIBE|DESC|EXPLAIN|SHOW)\b",
            sql,
            flags=re.IGNORECASE,
        ):
            return sql
        if re.search(r"\bLIMIT\b", sql, flags=re.IGNORECASE):
            return sql
        return f"{sql.rstrip(';').rstrip()} LIMIT {default_limit}"

    def build_schema_summary(self, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> str:
        """Строит краткое описание схемы для промпта."""
        limited_schema = self.limit_schema_for_prompt(db_schema)
        schema_summary = []

        for table_name, table_schema in limited_schema.items():
            # Используем get_table_columns для правильного извлечения колонок
            columns = get_table_columns(table_schema)
            col_parts = []
            for col_name, meta in columns.items():
                col_type = meta.get("type", "")
                col_desc = meta.get("description", "")
                if col_desc:
                    col_parts.append(f"{col_name}:{col_type} '{col_desc}'")
                else:
                    col_parts.append(f"{col_name}:{col_type}")
            cols_str = ", ".join(col_parts)
            schema_summary.append(f"{table_name}({cols_str})")

        return ", ".join(schema_summary)
