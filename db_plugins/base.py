from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Protocol, Tuple

# Константы для стратегий семплирования
SMALL_TABLE_THRESHOLD = 100_000
LARGE_TABLE_THRESHOLD = 10_000_000

_READ_ONLY_FAIL_OPEN_PARAM = "read_only_fail_open"
_TRUE_QUERY_VALUES = {"1", "true", "yes", "on"}
_RESERVED_WORD_CACHE: Dict[str, set[str]] = {}


class DBPlugin(Protocol):
    """Интерфейс плагина БД."""

    dialect: str
    dialect_label: str

    def connect(self, dsn: str):
        """Открывает соединение и возвращает connection-объект курсорного типа."""
        ...

    def close(self, conn) -> None:
        ...

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        """Возвращает план выполнения/оценку стоимости/замечания."""
        ...

    def execute_select(self, conn, sql: str, row_limit: int = 500) -> Dict[str, Any]:
        """Выполняет безопасный SELECT с ограничением строк и возвращает данные/колонки/время."""
        ...

    def introspect_schema(self, conn, schema: Optional[str] = None, table_name: Optional[str] = None) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Возвращает схему в новом формате {table: {description, columns: {column: {type, description, constraint_type, references}}}}.

        schema: опционально ограничивает интроспекцию указанной схемой/базой там, где это поддерживается.
        Если не поддерживается СУБД — параметр игнорируется.
        
        table_name: опционально ограничивает интроспекцию указанной таблицей.
        Позволяет получить информацию только об одной таблице для оптимизации производительности.
        
        Возвращаемый формат:
        {
            "table_name": {
                "description": "Описание таблицы (пустая строка по умолчанию)",
                "columns": {
                    "column_name": {
                        "type": "SQL_TYPE",
                        "description": "Comment or empty string",
                        "constraint_type": "PK|FK|UNIQUE|" (пустая строка если нет ограничений),
                        "references": "referenced_table.referenced_column" (для FK, иначе пустая строка)
                    }
                }
            }
        }
        """
        ...

    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows in table (approximate, fast)."""
        ...

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get basic column statistics (type, nulls, examples)."""
        ...

    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling based on table size strategy."""
        ...

    def get_fk_preview(
        self,
        conn,
        table_name: str,
        fk_column: str,
        ref_table: str,
        max_rows: Optional[int] = None,
        ref_column: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get FK preview - sample JOIN with referenced table."""
        ...

    def normalize_schema_names(self, dsn: str, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Нормализует имена таблиц с учетом схемы и особенностей СУБД.
        
        Args:
            dsn: Строка подключения для извлечения информации о схеме
            db_schema: Схема БД в стандартном формате
            
        Returns:
            Схема с нормализованными именами таблиц (обычно schema.table или main.table для SQLite)
        """
        ...

    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """Извлекает имя схемы из DSN строки подключения.
        
        Args:
            dsn: Строка подключения к БД
            
        Returns:
            Имя схемы если найдено, иначе схема по умолчанию для данной БД
        """
        ...
    
    def get_default_schema(self) -> str:
        """Возвращает схему по умолчанию для данной БД.
        
        Returns:
            Имя схемы по умолчанию
        """
        ...

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Выполняет специфичную для БД валидацию DSN.
        
        Args:
            dsn: Строка подключения к БД
            parsed_dsn: Разобранный DSN (результат urlparse)
            
        Returns:
            Кортеж (errors, warnings) где:
            - errors: список критических ошибок валидации
            - warnings: список предупреждений
        """
        ...

    def quote_identifier(self, identifier: str) -> str:
        """Квотирует идентификатор (таблица/колонка) согласно диалекту СУБД.
        
        Args:
            identifier: Имя таблицы или колонки
            
        Returns:
            Квотированный идентификатор
        """
        ...

    def build_select_all(self, table_name: str, limit: int) -> str:
        """Строит SELECT * запрос с лимитом для указанной таблицы.
        
        Args:
            table_name: Имя таблицы
            limit: Максимальное количество строк
            
        Returns:
            SQL запрос для выборки данных
        """
        ...

    def build_distinct_values_query(self, table_name: str, column_name: str, limit: int) -> str:
        """Строит DISTINCT-запрос с лимитом для указанной колонки."""
        ...

    def get_type_category(self, sql_type: str) -> str:
        """Группа SQL-типа (integer/numeric/string/temporal/other).

        Source of truth — yaml ``config/text_to_sql/type_categories.yaml``
        (см. EPIC 5.2). Дефолтная реализация в ``BaseDBPlugin`` тянет
        категорию из этого yaml; диалект-специфичные плагины могут
        переопределить (например, добавив поведенческие особенности
        DuckDB/Impala).
        """
        ...


class BaseDBPlugin:
    """Базовая реализация DBPlugin с общими методами."""
    dialect = "postgres"
    dialect_label = "Generic SQL"

    @contextmanager
    def _cursor(self, conn):
        """Context-manager helper для безопасной работы с курсором.

        Гарантирует, что курсор будет закрыт даже при исключении,
        предотвращая утечку серверных ресурсов (psycopg/pymysql и т.п.).
        """
        cur = conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass  # best-effort close

    def normalize_column_info(self, col_info: Dict[str, Any]) -> Dict[str, str]:
        """Нормализует информацию о колонке к единому формату.
        
        Обеспечивает единообразие полей между разными плагинами:
        - type: тип данных
        - description: комментарий или описание (пустая строка если нет)
        - not_null: "True"/"False" строка
        - default_value: значение по умолчанию (пустая строка если нет)
        - constraint_type: "PK"/"FK"/"UNIQUE"/"" (нормализованные значения)
        - references: "table.column" для FK (пустая строка если нет)
        """
        normalized = {
            "type": str(col_info.get("type", "")).strip(),
            "description": str(col_info.get("description", "")).strip(),
            "not_null": self._normalize_boolean_string(col_info.get("not_null")),
            "default_value": str(col_info.get("default_value", "")).strip(),
            "constraint_type": self._normalize_constraint_type(col_info.get("constraint_type")),
            "references": str(col_info.get("references", "")).strip()
        }
        return normalized
    
    def _normalize_boolean_string(self, value: Any) -> str:
        """Нормализует булево значение в строку "True"/"False"."""
        if value is None or value == "":
            return ""
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, str):
            val_lower = value.lower().strip()
            if val_lower in {"true", "1", "yes", "t", "y"}:
                return "True"
            elif val_lower in {"false", "0", "no", "f", "n"}:
                return "False"
        return ""
    
    def _normalize_constraint_type(self, value: Any) -> str:
        """Нормализует тип ограничения к стандартным значениям."""
        if not value:
            return ""
        
        constraint_str = str(value).upper().strip()
        
        # Нормализация к стандартным значениям
        if "PRIMARY" in constraint_str or constraint_str == "PK":
            return "PK"
        elif "FOREIGN" in constraint_str or constraint_str == "FK":
            return "FK"
        elif "UNIQUE" in constraint_str:
            return "UNIQUE"
        elif constraint_str in {"PK", "FK", "UNIQUE"}:
            return constraint_str
        else:
            return ""
    
    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """Базовая реализация парсинга явной схемы из DSN."""
        try:
            _, schema = self.split_connection_dsn_and_schema(dsn)
            return schema
        except Exception:
            return None

    def split_connection_dsn_and_schema(self, dsn: str) -> Tuple[str, Optional[str]]:
        """Separates schema suffix from the connection target.

        Supported explicit schema forms:
        - postgres://host/database.schema -> postgres://host/database + schema
        - duckdb:///path/file.duckdb.analytics -> duckdb:///path/file.duckdb + analytics
        - duckdb:///path/file.duckdb/analytics -> duckdb:///path/file.duckdb + analytics
        """
        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        raw_path = parsed.path or ""
        path = raw_path.strip("/")
        if not path:
            return dsn, None

        clean_path = raw_path
        schema: Optional[str] = None
        scheme = (parsed.scheme or "").lower()

        if scheme == "duckdb":
            clean_path, schema = self._split_duckdb_connection_path(raw_path)
        else:
            filename = path.rsplit("/", 1)[-1]
            file_exts = (".db", ".duckdb", ".sqlite", ".sqlite3")
            if "." in filename and not filename.endswith(file_exts):
                database, schema_part = filename.split(".", 1)
                if database and schema_part:
                    prefix_len = len(raw_path) - len(filename)
                    clean_path = raw_path[:prefix_len] + database
                    schema = schema_part

        if not schema:
            return dsn, None

        return parsed._replace(path=clean_path).geturl(), schema

    def strip_plugin_query_options(self, dsn: str) -> str:
        """Removes DB-plugin-only query params before passing DSN to drivers."""
        from urllib.parse import parse_qsl, urlencode, urlparse

        parsed = urlparse(dsn)
        if not parsed.query:
            return dsn
        query_pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != _READ_ONLY_FAIL_OPEN_PARAM
        ]
        return parsed._replace(query=urlencode(query_pairs)).geturl()

    def read_only_fail_open_enabled(self, dsn: str) -> bool:
        """Checks explicit opt-in for continuing when read-only setup fails."""
        from urllib.parse import parse_qs, urlparse

        values = parse_qs(urlparse(dsn).query).get(_READ_ONLY_FAIL_OPEN_PARAM, [])
        return any(str(value).strip().lower() in _TRUE_QUERY_VALUES for value in values)

    def decode_url_part(self, value: Optional[str]) -> Optional[str]:
        """URL-decodes userinfo pieces returned by urllib.parse.urlparse."""
        if value is None:
            return None
        from urllib.parse import unquote

        return unquote(value)

    def _split_duckdb_connection_path(self, raw_path: str) -> Tuple[str, Optional[str]]:
        path = raw_path.strip("/")
        if not path:
            return raw_path, None

        if path.endswith((".duckdb", ".db")):
            return raw_path, None

        for ext in (".duckdb", ".db"):
            marker = f"{ext}."
            marker_index = path.rfind(marker)
            if marker_index >= 0:
                schema = path[marker_index + len(marker):]
                database_path = path[:marker_index + len(ext)]
                if schema and "/" not in schema:
                    prefix = "/" if raw_path.startswith("/") else ""
                    return prefix + database_path, schema

        parts = path.split("/")
        if len(parts) >= 2 and parts[-2].endswith((".duckdb", ".db")) and parts[-1]:
            prefix = "/" if raw_path.startswith("/") else ""
            return prefix + "/".join(parts[:-1]), parts[-1]

        return raw_path, None
    
    def quote_identifier(self, identifier: str) -> str:
        """Базовая реализация квотирования (двойные кавычки).
        
        ВАЖНО: если идентификатор содержит `schema.table`, каждая часть
        квотируется отдельно: "schema"."table".
        """
        if not identifier:
            return identifier

        # Поддержка составных идентификаторов вида schema.table[.column]
        parts = str(identifier).split(".")
        quoted_parts = []
        for part in parts:
            if part == "":
                # Пропускаем пустые части, чтобы не генерировать лишние точки
                continue
            if not self._identifier_needs_quoting(part):
                quoted_parts.append(part)
            else:
                escaped = part.replace('"', '""')
                quoted_parts.append(f'"{escaped}"')

        return ".".join(quoted_parts)

    def _identifier_needs_quoting(self, part: str) -> bool:
        """Returns True for reserved, mixed-case, or non-simple identifiers."""
        return (
            re.fullmatch(r"[a-z_][a-z0-9_]*", part) is None
            or part.lower() in self._reserved_words()
        )

    def _sqlglot_dialect_name(self) -> str:
        if self.dialect == "impala":
            return "hive"
        if self.dialect == "sapiq":
            return "tsql"
        return self.dialect

    def _reserved_words(self) -> set[str]:
        dialect = self._sqlglot_dialect_name()
        if dialect in _RESERVED_WORD_CACHE:
            return _RESERVED_WORD_CACHE[dialect]
        try:
            import sqlglot

            tokenizer_class = sqlglot.Dialect.get_or_raise(dialect).tokenizer_class
            words = set()
            for keyword in tokenizer_class.KEYWORDS:
                for word in re.split(r"\s+", str(keyword).strip()):
                    if word:
                        words.add(word.lower())
        except Exception:
            words = set()
        _RESERVED_WORD_CACHE[dialect] = words
        return words

    def _normalize_row_limit(self, row_limit: int) -> int:
        limit = int(row_limit)
        if limit <= 0:
            raise ValueError("row_limit must be a positive integer")
        return limit

    def limit_select_sql(self, sql: str, row_limit: int) -> str:
        """Applies a top-level row cap while preserving top-level ORDER BY."""
        limit = self._normalize_row_limit(row_limit)
        q = sql.strip().rstrip(";")
        try:
            import sqlglot
            from sqlglot import exp

            dialect = self._sqlglot_dialect_name()
            parsed = sqlglot.parse_one(q, read=dialect)
            top_level_limit = parsed.args.get("limit")
            existing_limit = None
            if top_level_limit:
                limit_expression = top_level_limit.args.get("expression") or top_level_limit.args.get("count")
                if limit_expression is not None:
                    to_py = getattr(limit_expression, "to_py", None)
                    try:
                        limit_value = to_py() if callable(to_py) else getattr(limit_expression, "this", None)
                    except (TypeError, ValueError):
                        limit_value = getattr(limit_expression, "this", None)
                    if isinstance(limit_value, int) and not isinstance(limit_value, bool):
                        existing_limit = limit_value
                    elif isinstance(limit_value, str) and limit_value.isdigit():
                        existing_limit = int(limit_value)
            if top_level_limit is not None:
                if existing_limit is not None and 0 <= existing_limit <= limit:
                    return q

            parsed.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
            return parsed.sql(dialect=dialect)
        except Exception:
            logger = getattr(self, "logger", None)
            if logger:
                logger.warning("sqlglot failed to apply row cap; using outer cap wrapper", exc_info=True)
        return f"SELECT * FROM ({q}) AS limited_subquery LIMIT {limit}"

    def fetch_rows_with_limit(self, cur, row_limit: int):
        """Fetches at most row_limit rows even if the DB query was not capped."""
        limit = self._normalize_row_limit(row_limit)
        if hasattr(cur, "fetchmany"):
            return cur.fetchmany(limit)
        return cur.fetchall()[:limit]
    
    def build_select_all(self, table_name: str, limit: int) -> str:
        """Базовая реализация SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"

    def build_distinct_values_query(self, table_name: str, column_name: str, limit: int) -> str:
        """Builds a dialect-aware DISTINCT values query."""
        quoted_table = self.quote_identifier(table_name)
        quoted_column = self.quote_identifier(column_name)
        row_limit = self._normalize_row_limit(limit)
        return (
            f"SELECT DISTINCT {quoted_column} "
            f"FROM {quoted_table} "
            f"WHERE {quoted_column} IS NOT NULL "
            f"ORDER BY {quoted_column} "
            f"LIMIT {row_limit}"
        )

    def get_type_category(self, sql_type: str) -> str:
        """Default get_type_category — читает yaml type_categories.yaml.

        Диалект-специфичные плагины могут переопределить.
        """
        # Ленивый импорт: db_plugins/base.py не должен зависеть от
        # custom_tools на уровне модуля (предотвращает циклы при
        # инициализации).
        from custom_tools.text_to_sql.type_categories_config import (
            load_type_categories_config,
        )

        return load_type_categories_config().get_category(sql_type)
