from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

# Константы для стратегий семплирования
SMALL_TABLE_THRESHOLD = 100_000
LARGE_TABLE_THRESHOLD = 10_000_000

_READ_ONLY_FAIL_OPEN_PARAM = "read_only_fail_open"
_TRUE_QUERY_VALUES = {"1", "true", "yes", "on"}
_RESERVED_WORD_CACHE: Dict[str, set[str]] = {}
_BOUND_PARAMETER_SENTINEL = "__text_to_sql_bound_parameter__"
_BRACKET_IDENTIFIER_DIALECTS = frozenset({"sqlite", "tsql"})


def _qmark_code_positions(sql: str, *, dialect: str) -> list[int]:
    """Find question marks outside SQL literals, identifiers, and comments."""
    positions: list[int] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        pair = sql[index : index + 2]

        if pair == "--" or (char == "#" and dialect == "mysql"):
            newline = sql.find("\n", index + (2 if pair == "--" else 1))
            index = length if newline < 0 else newline + 1
            continue
        if pair == "/*":
            depth = 1
            index += 2
            while index < length and depth:
                if sql[index : index + 2] == "/*":
                    depth += 1
                    index += 2
                elif sql[index : index + 2] == "*/":
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError("unterminated SQL block comment")
            continue
        if char == "$":
            delimiter_match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if delimiter_match is not None:
                delimiter = delimiter_match.group(0)
                closing = sql.find(delimiter, index + len(delimiter))
                if closing < 0:
                    raise ValueError("unterminated SQL dollar-quoted literal")
                index = closing + len(delimiter)
                continue
        if char in {"'", '"', "`"} or (
            char == "[" and dialect in _BRACKET_IDENTIFIER_DIALECTS
        ):
            closing = "]" if char == "[" else char
            index += 1
            while index < length:
                if sql[index] == "\\" and closing != "]":
                    index += 2
                    continue
                if sql[index] != closing:
                    index += 1
                    continue
                if index + 1 < length and sql[index + 1] == closing:
                    index += 2
                    continue
                index += 1
                break
            else:
                raise ValueError("unterminated SQL quoted value")
            continue
        if char == "?":
            positions.append(index)
        index += 1
    return positions


def _rewrite_qmark_placeholders(
    sql: str,
    *,
    dialect: str,
    parameter_marker: str,
    expected_count: int,
) -> str:
    """Rewrite only parser-confirmed positional placeholders, or fail closed."""
    if not isinstance(sql, str):
        raise TypeError("sql must be text")
    if not isinstance(parameter_marker, str) or not parameter_marker:
        raise ValueError("parameter_marker must be non-empty text")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise TypeError("expected_count must be an integer")
    if expected_count < 0:
        raise ValueError("expected_count must not be negative")
    if _BOUND_PARAMETER_SENTINEL in sql:
        raise ValueError("SQL contains the reserved bound-parameter sentinel")

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError as exc:
        raise ValueError("bound SELECT execution requires sqlglot") from exc

    placeholder_positions = []
    for position in _qmark_code_positions(sql, dialect=dialect):
        candidate = (
            sql[:position]
            + f":{_BOUND_PARAMETER_SENTINEL}"
            + sql[position + 1 :]
        )
        try:
            statements = [
                statement
                for statement in sqlglot.parse(candidate, read=dialect)
                if statement is not None
            ]
        except Exception:
            continue
        if len(statements) != 1:
            continue
        sentinels = [
            placeholder
            for placeholder in statements[0].find_all(exp.Placeholder)
            if placeholder.this == _BOUND_PARAMETER_SENTINEL
        ]
        if len(sentinels) == 1:
            placeholder_positions.append(position)

    if len(placeholder_positions) != expected_count:
        raise ValueError(
            "bound parameter count does not match SQL placeholder count"
        )

    chunks: list[str] = []
    previous = 0
    for position in placeholder_positions:
        chunks.extend((sql[previous:position], parameter_marker))
        previous = position + 1
    chunks.append(sql[previous:])
    return "".join(chunks)


class CapabilityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


class EnforcementMode(str, Enum):
    DATABASE = "database"
    DRIVER = "driver"
    READ_ONLY_FILE = "read_only_file"
    SUPERVISOR = "supervisor"
    NONE = "none"


@dataclass(frozen=True)
class Capability:
    state: CapabilityState
    mode: EnforcementMode
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, CapabilityState):
            raise TypeError("capability state must be a CapabilityState")
        if not isinstance(self.mode, EnforcementMode):
            raise TypeError("capability mode must be an EnforcementMode")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("capability reason_code must be non-empty text")
        if self.state is CapabilityState.SUPPORTED and self.mode is EnforcementMode.NONE:
            raise ValueError("supported capability must declare an enforcement mode")

    @classmethod
    def supported(cls, mode: EnforcementMode, reason_code: str) -> "Capability":
        return cls(CapabilityState.SUPPORTED, mode, reason_code)

    @classmethod
    def unsupported(cls, reason_code: str) -> "Capability":
        return cls(CapabilityState.UNSUPPORTED, EnforcementMode.NONE, reason_code)

    @classmethod
    def unverified(
        cls,
        reason_code: str,
        mode: EnforcementMode = EnforcementMode.NONE,
    ) -> "Capability":
        return cls(CapabilityState.UNVERIFIED, mode, reason_code)

    def to_mapping(self) -> Dict[str, str]:
        return {
            "state": self.state.value,
            "mode": self.mode.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class DatabaseCapabilities:
    dialect: str
    read_only: Capability
    statement_timeout: Capability
    cancellation: Capability
    explain: Capability
    introspection: Capability
    composite_fk_introspection: Capability
    parameter_binding: Capability

    def __post_init__(self) -> None:
        if not isinstance(self.dialect, str) or not self.dialect:
            raise ValueError("database capability dialect must be non-empty text")
        for field_name in _DATABASE_CAPABILITY_FIELDS:
            if not isinstance(getattr(self, field_name), Capability):
                raise TypeError(f"{field_name} must be a Capability")

    def to_mapping(self) -> Dict[str, object]:
        return {
            "dialect": self.dialect,
            **{
                field_name: getattr(self, field_name).to_mapping()
                for field_name in _DATABASE_CAPABILITY_FIELDS
            },
        }

    def downgrade(self, **changes: Capability) -> "DatabaseCapabilities":
        unknown = set(changes) - set(_DATABASE_CAPABILITY_FIELDS)
        if unknown:
            raise ValueError(f"unknown capability fields: {sorted(unknown)}")
        return replace(self, **changes)


_DATABASE_CAPABILITY_FIELDS = (
    "read_only",
    "statement_timeout",
    "cancellation",
    "explain",
    "introspection",
    "composite_fk_introspection",
    "parameter_binding",
)


@dataclass(frozen=True)
class PluginHealth:
    dialect: str
    capabilities: DatabaseCapabilities
    production_ready: bool
    reason_codes: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.dialect != self.capabilities.dialect:
            raise ValueError("health dialect must match capability dialect")
        if type(self.production_ready) is not bool:
            raise TypeError("production_ready must be a boolean")
        if not isinstance(self.reason_codes, tuple) or not all(
            isinstance(item, str) and item for item in self.reason_codes
        ):
            raise TypeError("reason_codes must be a tuple of non-empty strings")

    def to_mapping(self) -> Dict[str, object]:
        return {
            "dialect": self.dialect,
            "production_ready": self.production_ready,
            "reason_codes": list(self.reason_codes),
            "capabilities": self.capabilities.to_mapping(),
        }


@dataclass(frozen=True)
class RequiredDatabaseCapabilities:
    read_only: bool = True
    statement_timeout: bool = True
    cancellation: bool = True
    explain: bool = False
    introspection: bool = False
    composite_fk_introspection: bool = False
    parameter_binding: bool = False
    allow_supervisor: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            *_DATABASE_CAPABILITY_FIELDS,
            "allow_supervisor",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"required capability {field_name} must be boolean")


class UnsupportedCapabilityError(RuntimeError):
    """Raised when a plugin operation has no declared implementation."""


class DatabaseCapabilityError(RuntimeError):
    def __init__(self, capability: str, reason_code: str) -> None:
        self.capability = capability
        self.reason_code = reason_code
        super().__init__(
            f"required database capability {capability!r} is unavailable "
            f"({reason_code})"
        )


def validate_required_capabilities(
    capabilities: DatabaseCapabilities,
    required: RequiredDatabaseCapabilities,
) -> None:
    if not isinstance(capabilities, DatabaseCapabilities):
        raise TypeError("capabilities must be a DatabaseCapabilities instance")
    if not isinstance(required, RequiredDatabaseCapabilities):
        raise TypeError("required must be a RequiredDatabaseCapabilities instance")
    for field_name in _DATABASE_CAPABILITY_FIELDS:
        if not getattr(required, field_name):
            continue
        capability = getattr(capabilities, field_name)
        if capability.state is not CapabilityState.SUPPORTED:
            raise DatabaseCapabilityError(field_name, capability.reason_code)
        if (
            capability.mode is EnforcementMode.SUPERVISOR
            and not required.allow_supervisor
        ):
            raise DatabaseCapabilityError(
                field_name, "SUPERVISOR_ENFORCEMENT_DISALLOWED"
            )


@dataclass(frozen=True)
class ForeignKeyRow:
    """Одна column-row FK-метаданных, как её отдаёт драйвер БД."""

    table_key: str
    column: str
    constraint_schema: Optional[str]
    constraint_name: Optional[str]
    ordinal_position: Any
    references: Optional[str]


class DBPlugin(Protocol):
    """Интерфейс плагина БД."""

    dialect: str
    dialect_label: str

    def get_capabilities(self, dsn: Optional[str] = None) -> DatabaseCapabilities:
        ...

    def probe_capabilities(
        self, conn=None, dsn: Optional[str] = None
    ) -> PluginHealth:
        ...

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

    def execute_select_bound(
        self,
        conn,
        sql: str,
        parameters: tuple[str | int | float | bool | None, ...],
        row_limit: int = 500,
    ) -> Dict[str, Any]:
        """Executes SELECT with driver-bound positional parameters."""
        ...

    def set_statement_timeout(self, conn, timeout_ms: int) -> None:
        """Sets per-statement execution timeout where supported."""
        ...

    def introspect_schema(self, conn, schema: Optional[str] = None, table_name: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Возвращает схему с колонками и table-level ``foreign_keys`` envelope.

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
                },
                "foreign_keys": {
                    "complete": True|False,
                    "constraints": [{
                        "constraint_id": "table:native_constraint",
                        "to_table": "referenced_table",
                        "column_pairs": [{
                            "from_column": "source_column",
                            "to_column": "referenced_column"
                        }]
                    }]
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

    def quote_identifier_part(self, identifier: str) -> str:
        """Квотирует одно имя, не разделяя его по точкам."""
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

    def build_values_membership_query(
        self, table_name: str, column_name: str, values: List[Any]
    ) -> str:
        """Строит DISTINCT-запрос значений колонки, входящих в values."""
        ...

    def build_lookup_values_query(
        self,
        table_name: str,
        lookup_column: str,
        return_column: str,
        values: List[Any],
    ) -> str:
        """Строит DISTINCT lookup-запрос: lookup_column IN values → return_column."""
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

    def get_capabilities(self, dsn: Optional[str] = None) -> DatabaseCapabilities:
        del dsn
        unavailable = Capability.unsupported("CAPABILITY_NOT_DECLARED")
        return DatabaseCapabilities(
            dialect=self.dialect,
            read_only=unavailable,
            statement_timeout=Capability.unsupported(
                "DB_STATEMENT_TIMEOUT_UNSUPPORTED"
            ),
            cancellation=unavailable,
            explain=unavailable,
            introspection=unavailable,
            composite_fk_introspection=unavailable,
            parameter_binding=unavailable,
        )

    def probe_capabilities(
        self, conn=None, dsn: Optional[str] = None
    ) -> PluginHealth:
        capabilities = self.get_capabilities(dsn)
        if conn is None:
            return PluginHealth(
                dialect=self.dialect,
                capabilities=capabilities,
                production_ready=False,
                reason_codes=("CONNECTION_PROBE_REQUIRED",),
            )
        try:
            validate_required_capabilities(
                capabilities,
                RequiredDatabaseCapabilities(allow_supervisor=True),
            )
        except DatabaseCapabilityError as exc:
            return PluginHealth(
                dialect=self.dialect,
                capabilities=capabilities,
                production_ready=False,
                reason_codes=(exc.reason_code,),
            )
        return PluginHealth(
            dialect=self.dialect,
            capabilities=capabilities,
            production_ready=True,
            reason_codes=("CONNECTION_CONTRACT_PROVEN",),
        )

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

    def _group_foreign_key_rows(
        self, rows: Iterable[ForeignKeyRow], *, dialect_label: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Groups per-column FK rows into ordered composite constraints.

        Shared port of the (formerly duplicated) PostgreSQL/MySQL FK-grouping
        logic used by ``introspect_schema``.
        """
        fk_groups: Dict[Tuple[str, str, str], List[Tuple[int, str, str, str]]] = {}
        for row in rows:
            if row.constraint_name is None:
                continue
            if not row.constraint_schema or not row.constraint_name or not isinstance(
                row.ordinal_position, int
            ):
                raise RuntimeError(
                    f"{dialect_label} FK metadata is incomplete for {row.table_key}.{row.column}"
                )
            reference_table, separator, reference_column = str(
                row.references
            ).rpartition(".")
            if not separator or not reference_table or not reference_column:
                raise RuntimeError(
                    f"{dialect_label} FK target is incomplete for {row.table_key}.{row.column}"
                )
            fk_groups.setdefault(
                (row.table_key, str(row.constraint_schema), str(row.constraint_name)), []
            ).append(
                (
                    row.ordinal_position,
                    str(row.column),
                    reference_table,
                    reference_column,
                )
            )

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for (key, _constraint_schema, constraint_name), members in fk_groups.items():
            ordered = sorted(members)
            if [position for position, *_ in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise RuntimeError(
                    f"{dialect_label} FK ordinals are not contiguous for {key}:{constraint_name}"
                )
            target_tables = {target_table for _, _, target_table, _ in ordered}
            if len(target_tables) != 1:
                raise RuntimeError(
                    f"{dialect_label} FK target table is inconsistent for {key}:{constraint_name}"
                )
            grouped.setdefault(key, []).append(
                {
                    "constraint_id": f"{key}:{constraint_name}",
                    "to_table": target_tables.pop(),
                    "column_pairs": [
                        {"from_column": source, "to_column": target}
                        for _, source, _, target in ordered
                    ],
                }
            )
        for constraints in grouped.values():
            constraints.sort(key=lambda constraint: constraint["constraint_id"])
        return grouped

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
            quoted_parts.append(self.quote_identifier_part(part))

        return ".".join(quoted_parts)

    def quote_identifier_part(self, identifier: str) -> str:
        """Quote one trusted identifier part without treating dots as separators."""
        if not identifier:
            return identifier
        part = str(identifier)
        if not self._identifier_needs_quoting(part):
            return part
        escaped = part.replace('"', '""')
        return f'"{escaped}"'

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

    def set_statement_timeout(self, conn, timeout_ms: int) -> None:
        """Reject inherited timeout handling; concrete plugins must override."""
        del conn
        timeout = int(timeout_ms)
        if timeout <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        raise UnsupportedCapabilityError(
            "statement timeout is unsupported by the base database plugin"
        )

    def execute_select_bound(
        self,
        conn,
        sql: str,
        parameters: tuple[str | int | float | bool | None, ...],
        row_limit: int = 500,
    ) -> Dict[str, Any]:
        del conn, sql, parameters, row_limit
        raise UnsupportedCapabilityError(
            "bound SELECT execution is unsupported by the base database plugin"
        )

    def _execute_select_bound_driver(
        self,
        conn,
        sql: str,
        parameters: tuple[str | int | float | bool | None, ...],
        row_limit: int,
        *,
        parameter_marker: str = "?",
    ) -> Dict[str, Any]:
        start = time.time()
        query = self.limit_select_sql(sql, row_limit)
        query = _rewrite_qmark_placeholders(
            query,
            dialect=self._sqlglot_dialect_name(),
            parameter_marker=parameter_marker,
            expected_count=len(parameters),
        )
        with self._cursor(conn) as cursor:
            cursor.execute(query, parameters)
            rows = self.fetch_rows_with_limit(cursor, row_limit)
            columns = [item[0] for item in cursor.description] if cursor.description else []
        elapsed = int((time.time() - start) * 1000)
        return {
            "success": True,
            "data": rows,
            "columns": columns,
            "rows_affected": len(rows),
            "execution_time_ms": elapsed,
            "error_message": None,
        }

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

    def _quote_literal(self, value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def build_values_membership_query(
        self, table_name: str, column_name: str, values: List[Any]
    ) -> str:
        """Builds a query for parent values matching a sampled child value set."""
        quoted_table = self.quote_identifier(table_name)
        quoted_column = self.quote_identifier(column_name)
        non_null_values = [value for value in values if value is not None]
        if not non_null_values:
            return (
                f"SELECT DISTINCT {quoted_column} "
                f"FROM {quoted_table} "
                "WHERE 1 = 0"
            )
        literals = ", ".join(self._quote_literal(value) for value in non_null_values)
        return (
            f"SELECT DISTINCT {quoted_column} "
            f"FROM {quoted_table} "
            f"WHERE {quoted_column} IN ({literals}) "
            f"ORDER BY {quoted_column}"
        )

    def build_lookup_values_query(
        self,
        table_name: str,
        lookup_column: str,
        return_column: str,
        values: List[Any],
    ) -> str:
        """Builds a query mapping lookup values to return-column values."""
        quoted_table = self.quote_identifier(table_name)
        quoted_lookup = self.quote_identifier(lookup_column)
        quoted_return = self.quote_identifier(return_column)
        non_null_values = [value for value in values if value is not None]
        if not non_null_values:
            return (
                f"SELECT DISTINCT {quoted_lookup} AS lookup_value, "
                f"{quoted_return} AS return_value "
                f"FROM {quoted_table} "
                "WHERE 1 = 0"
            )
        literals = ", ".join(self._quote_literal(value) for value in non_null_values)
        return (
            f"SELECT DISTINCT {quoted_lookup} AS lookup_value, "
            f"{quoted_return} AS return_value "
            f"FROM {quoted_table} "
            f"WHERE {quoted_lookup} IN ({literals}) "
            f"ORDER BY {quoted_lookup}, {quoted_return}"
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
