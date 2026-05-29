from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from .base import BaseDBPlugin


logger = logging.getLogger(__name__)


class SAPIQReadOnlyEnforcementError(RuntimeError):
    """Raised when SAP IQ connection cannot be returned with read-only enforcement."""


class SAPIQPlugin(BaseDBPlugin):
    dialect = "sapiq"
    dialect_label = "SAP IQ"
    # Public source of truth для маппинга в sqlglot. Историческое значение
    # "ansi" сохраняется (protected helper отдаёт "tsql" для quoting, но
    # для глобального dialect mapping используется ANSI как наиболее
    # безопасный общий парсер).
    sqlglot_dialect = "ansi"

    def _odbc_value(self, value: object) -> str:
        text = str(value or "")
        return "{" + text.replace("}", "}}") + "}"

    def _build_odbc_conn_str(self, dsn: str):
        dsn, _ = self.split_connection_dsn_and_schema(dsn)
        u = urlparse(self.strip_plugin_query_options(dsn))
        host = u.hostname or "localhost"
        port = u.port or 2638
        db = (u.path or "/").lstrip("/") or ""  # DBN
        user = self.decode_url_part(u.username) or ""
        password = self.decode_url_part(u.password) or ""
        # Попробуем несколько драйверов SAP IQ / SQL Anywhere
        base = (
            f"HOST={self._odbc_value(f'{host}:{port}')};"
            f"DBN={self._odbc_value(db)};"
            f"UID={self._odbc_value(user)};"
            f"PWD={self._odbc_value(password)}"
        )
        candidates = [
            "SQL Anywhere 17",
            "SQL Anywhere 16",
            "SAP IQ",
            "Sybase IQ",
        ]
        return candidates, base

    def connect(self, dsn: str):
        try:
            import pyodbc  # type: ignore
        except Exception as e:
            raise RuntimeError("pyodbc is not installed. pip install pyodbc") from e
        drivers, base = self._build_odbc_conn_str(dsn)
        fail_open = self.read_only_fail_open_enabled(dsn)
        driver_errors: list[str] = []
        conn = None
        for drv in drivers:
            conn_str = f"DRIVER={{{drv}}};{base}"
            try:
                conn = pyodbc.connect(conn_str, autocommit=True)
                break
            except Exception as e:
                driver_errors.append(f"{drv}: {e}")
                continue
        if conn is None:
            raise RuntimeError(
                "Cannot connect to SAP IQ with provided DSN. Driver errors: "
                + "; ".join(driver_errors)
            )
        # SAP IQ has no portable driver-level read-only switch via pyodbc.
        # Honor fail_open: if disabled, refuse the connection; if enabled,
        # return it but loudly warn that read-only is NOT enforced.
        if not fail_open:
            try:
                conn.close()
            finally:
                raise SAPIQReadOnlyEnforcementError(
                    "SAP IQ read-only session enforcement is not implemented. "
                    "Add read_only_fail_open=true to the DSN to explicitly allow an unenforced read-only connection."
                )
        logger.warning(
            "SAP IQ connection returned WITHOUT read-only enforcement "
            "(read_only_fail_open=true in DSN). Driver-level read-only is not "
            "implemented for SAP IQ; the session is writable. "
            "Any INSERT/UPDATE/DELETE will execute without restriction."
        )
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        try:
            with self._cursor(conn) as cur:
                cur.execute(f"EXPLAIN {sql}")
                rows = cur.fetchall()
            plan_text = "\n".join(" ".join(str(x) for x in r) for r in rows)
            return {"plan": plan_text, "estimated_cost": None, "rows_to_scan": None, "issues": []}
        except Exception as e:
            return {"plan": None, "estimated_cost": None, "rows_to_scan": None, "issues": [{"issue_type": "EXPLAIN_UNSUPPORTED", "description": str(e)}]}

    def execute_select(self, conn, sql: str, row_limit: int = 500) -> Dict[str, Any]:
        start = time.time()
        q = sql.strip().rstrip(";")
        q = self.limit_select_sql(q, row_limit)
        with self._cursor(conn) as cur:
            cur.execute(q)
            rows = self.fetch_rows_with_limit(cur, row_limit)
            columns = [d[0] for d in cur.description] if cur.description else []
        elapsed = int((time.time() - start) * 1000)
        return {"success": True, "data": rows, "columns": columns, "rows_affected": len(rows), "execution_time_ms": elapsed, "error_message": None}

    def introspect_schema(self, conn, schema: str | None = None, table_name: str | None = None) -> Dict[str, Dict[str, Dict[str, str]]]:
        try:
            # Строим WHERE условия динамически
            where_conditions = []
            params = []
            
            if schema:
                where_conditions.append("c.table_schema = ?")
                params.append(schema)
            else:
                # Исключаем системные схемы SAP IQ по умолчанию
                where_conditions.append("c.table_schema NOT IN ('INFORMATION_SCHEMA', 'DBA', 'SA')")
                where_conditions.append("c.table_schema NOT LIKE 'SYS%'")
                where_conditions.append("c.table_schema NOT LIKE 'RS_%'")
                where_conditions.append("c.table_schema NOT LIKE 'IQSYS%'")
            
            if table_name:
                where_conditions.append("c.table_name = ?")
                params.append(table_name)
            
            where_clause = ""
            if where_conditions:
                where_clause = "WHERE " + " AND ".join(where_conditions)
            
            # SAP IQ: используем правильные системные таблицы для constraints
            query = f"""
                SELECT 
                    c.table_schema, 
                    c.table_name, 
                    c.column_name, 
                    c.data_type,
                    c.is_nullable,
                    c.column_default,
                    -- Определяем тип ограничения через системные таблицы SAP IQ
                    CASE 
                        WHEN pk.column_name IS NOT NULL THEN 'PRIMARY KEY'
                        WHEN fk.column_name IS NOT NULL THEN 'FOREIGN KEY'
                        WHEN uk.column_name IS NOT NULL THEN 'UNIQUE'
                        ELSE ''
                    END AS constraint_type,
                    -- Для FK определяем ссылку
                    CASE 
                        WHEN fk.primary_table IS NOT NULL THEN 
                            fk.primary_table + '.' + fk.primary_column
                        ELSE ''
                    END AS references
                FROM INFORMATION_SCHEMA.COLUMNS c
                -- Primary keys через SYSTABKEYS
                LEFT JOIN (
                    SELECT table_name, column_name
                    FROM SYSTABKEYS 
                    WHERE key_type = 'P'  -- Primary key
                ) pk ON UPPER(c.table_name) = UPPER(pk.table_name) AND UPPER(c.column_name) = UPPER(pk.column_name)
                -- Foreign keys через SYSFOREIGNKEYS  
                LEFT JOIN (
                    SELECT foreign_table, foreign_column, primary_table, primary_column
                    FROM SYSFOREIGNKEYS
                ) fk ON UPPER(c.table_name) = UPPER(fk.foreign_table) AND UPPER(c.column_name) = UPPER(fk.foreign_column)
                -- Unique constraints через SYSTABKEYS
                LEFT JOIN (
                    SELECT table_name, column_name
                    FROM SYSTABKEYS 
                    WHERE key_type = 'U'  -- Unique key
                ) uk ON UPPER(c.table_name) = UPPER(uk.table_name) AND UPPER(c.column_name) = UPPER(uk.column_name)
                {where_clause}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
            schema_result: Dict[str, Dict[str, Any]] = {}
            with self._cursor(conn) as cur:
                cur.execute(query, params)

                for row in cur.fetchall():
                    if len(row) >= 6:  # Расширенный формат
                        schema_name, table, col, dtype, is_nullable, default_val = row[:6]
                        constraint_type = row[6] if len(row) > 6 else ""
                        references = row[7] if len(row) > 7 else ""
                    else:  # Базовый формат
                        schema_name, table, col, dtype = row[:4]
                        is_nullable = None
                        default_val = None
                        constraint_type = ""
                        references = ""

                    key = f"{schema_name}.{table}" if schema_name else table

                    # Создаем таблицу если её нет
                    if key not in schema_result:
                        schema_result[key] = {
                            "description": "",  # Будет заполнено LLM
                            "columns": {}
                        }

                    col_info = {
                        "type": dtype,
                        "description": "",
                        "not_null": str(is_nullable == "NO") if is_nullable else "",
                        "default_value": str(default_val) if default_val is not None else "",
                        "constraint_type": constraint_type or "",
                        "references": references or ""
                    }

                    schema_result[key]["columns"][col] = self.normalize_column_info(col_info)
            return schema_result
        except Exception as e:
            raise RuntimeError("SAP IQ schema introspection failed") from e
    
    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows using SAP IQ statistics."""
        try:
            # Parse table name (schema.table)
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = None
                table_name_only = table_name
            
            # Try SAP IQ system tables for row estimates
            with self._cursor(conn) as cur:
                # First try SYSTABLE for row count estimates
                if schema_name:
                    sql = """
                    SELECT row_count
                    FROM SYSTABLE t
                    JOIN SYSUSER u ON t.creator = u.user_id
                    WHERE u.user_name = ? AND t.table_name = ?
                    """
                    cur.execute(sql, (schema_name.upper(), table_name_only.upper()))
                else:
                    sql = """
                    SELECT row_count
                    FROM SYSTABLE
                    WHERE table_name = ?
                    """
                    cur.execute(sql, (table_name_only.upper(),))

                row = cur.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0])

                # Fallback to exact count
                sql = f"SELECT COUNT(*) FROM {self.quote_identifier(table_name)}"
                cur.execute(sql)
                row = cur.fetchone()
                return int(row[0]) if row else 1000000

        except Exception as e:
            logger.warning("SAP IQ estimate_row_count failed for %s: %s", table_name, e)

        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get basic column statistics for SAP IQ."""
        stats = {}
        try:
            # Parse table name
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = None
                table_name_only = table_name
            
            with self._cursor(conn) as cur:
                # Get column information from INFORMATION_SCHEMA
                if schema_name:
                    sql = """
                    SELECT
                        column_name,
                        data_type,
                        is_nullable,
                        column_default
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE table_schema = ? AND table_name = ?
                    ORDER BY ordinal_position
                    """
                    cur.execute(sql, (schema_name.upper(), table_name_only.upper()))
                else:
                    sql = """
                    SELECT
                        column_name,
                        data_type,
                        is_nullable,
                        column_default
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE table_name = ?
                    ORDER BY ordinal_position
                    """
                    cur.execute(sql, (table_name_only.upper(),))

                columns = cur.fetchall()

                for col_name, data_type, is_nullable, col_default in columns:
                    stats[col_name] = {
                        'type': data_type,
                        'nullable': is_nullable == 'YES',
                        'default': str(col_default) if col_default else None,
                        'null_count': 0,
                        'distinct_count': 0,
                        'sample_values': []
                    }

                    # Get basic statistics for this column
                    try:
                        # SAP IQ specific functions for statistics
                        quoted_col = self.quote_identifier(col_name)
                        quoted_table = self.quote_identifier(table_name)
                        stats_sql = f"""
                        SELECT
                            SUM(CASE WHEN {quoted_col} IS NULL THEN 1 ELSE 0 END) as null_count,
                            COUNT(DISTINCT {quoted_col}) as distinct_count
                        FROM {quoted_table}
                        """
                        cur.execute(stats_sql)
                        result = cur.fetchone()
                        if result:
                            stats[col_name]['null_count'] = int(result[0] or 0)
                            stats[col_name]['distinct_count'] = int(result[1] or 0)

                        # Get sample values using TOP (SAP IQ style)
                        import os
                        sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                        sample_sql = (
                            f'SELECT DISTINCT TOP {sample_limit} {quoted_col} '
                            f'FROM {quoted_table} WHERE {quoted_col} IS NOT NULL'
                        )
                        cur.execute(sample_sql)
                        samples = cur.fetchall()
                        stats[col_name]['sample_values'] = [str(row[0])[:50] for row in samples if row[0] is not None]

                    except Exception:
                        # Skip problematic columns
                        continue

        except Exception as e:
            logger.warning("SAP IQ get_basic_column_stats failed for %s: %s", table_name, e)

        return stats

    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for SAP IQ."""
        quoted_table = self.quote_identifier(table_name)
        if strategy == 'small':
            # For small tables, use simple TOP
            sql = f"SELECT TOP {max_rows} * FROM {quoted_table}"
        elif strategy == 'medium':
            # For medium tables, use sampling with RAND()
            sql = f"""
            SELECT TOP {max_rows} *
            FROM {quoted_table}
            WHERE RAND() < 0.01
            """
        else:  # large strategy
            # For large tables, minimal sampling
            sql = f"""
            SELECT TOP {max_rows} *
            FROM {quoted_table}
            WHERE RAND() < 0.001
            """

        return self.execute_select(conn, sql, row_limit=max_rows)
    
    def get_fk_preview(
        self,
        conn,
        table_name: str,
        fk_column: str,
        ref_table: str,
        max_rows: Optional[int] = None,
        ref_column: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get FK preview for SAP IQ."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            # Parse reference table name
            if '.' in ref_table:
                ref_schema, ref_table_only = ref_table.split('.', 1)
            else:
                ref_schema = None
                ref_table_only = ref_table
            
            with self._cursor(conn) as cur:
                if ref_column is None:
                    ref_column_sql = """
                    SELECT primary_column
                    FROM SYSFOREIGNKEYS
                    WHERE UPPER(foreign_table) = UPPER(?)
                      AND UPPER(foreign_column) = UPPER(?)
                      AND UPPER(primary_table) = UPPER(?)
                    """
                    source_table_only = table_name.split('.', 1)[1] if '.' in table_name else table_name
                    cur.execute(ref_column_sql, (source_table_only, fk_column, ref_table_only))
                    ref_column_row = cur.fetchone()
                    if ref_column_row:
                        ref_column = ref_column_row[0]
                    else:
                        return {
                            "success": False,
                            "data": [],
                            "columns": [],
                            "rows_affected": 0,
                            "execution_time_ms": 0,
                            "error_message": f"Referenced column for {table_name}.{fk_column} -> {ref_table} was not found"
                        }

                # Get readable columns from reference table
                if ref_schema:
                    sql = """
                    SELECT column_name
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE table_schema = ? AND table_name = ?
                      AND (LOWER(column_name) LIKE '%name%'
                           OR LOWER(column_name) LIKE '%title%'
                           OR LOWER(column_name) LIKE '%description%')
                    ORDER BY ordinal_position
                    """
                    cur.execute(sql, (ref_schema.upper(), ref_table_only.upper()))
                else:
                    sql = """
                    SELECT column_name
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE table_name = ?
                      AND (LOWER(column_name) LIKE '%name%'
                           OR LOWER(column_name) LIKE '%title%'
                           OR LOWER(column_name) LIKE '%description%')
                    ORDER BY ordinal_position
                    """
                    cur.execute(sql, (ref_table_only.upper(),))

                readable_cols = [row[0] for row in cur.fetchall()]

                if not readable_cols:
                    # Fallback to first few columns
                    if ref_schema:
                        sql = """
                        SELECT column_name
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE table_schema = ? AND table_name = ?
                        ORDER BY ordinal_position
                        """
                        cur.execute(sql, (ref_schema.upper(), ref_table_only.upper()))
                    else:
                        sql = """
                        SELECT column_name
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE table_name = ?
                        ORDER BY ordinal_position
                        """
                        cur.execute(sql, (ref_table_only.upper(),))

                    readable_cols = [row[0] for row in cur.fetchall()[:2]]

                # Limit to 3 columns max
                readable_cols = readable_cols[:3]

                # Build JOIN query using SAP IQ syntax
                qfk = self.quote_identifier(fk_column)
                qref_col = self.quote_identifier(ref_column)
                quoted_table = self.quote_identifier(table_name)
                quoted_ref = self.quote_identifier(ref_table)
                select_cols = [f't.{qfk}'] + [f'r.{self.quote_identifier(col)}' for col in readable_cols]
                select_str = ', '.join(select_cols)

                join_sql = f"""
                SELECT TOP {max_rows} {select_str}
                FROM {quoted_table} t
                JOIN {quoted_ref} r ON t.{qfk} = r.{qref_col}
                WHERE t.{qfk} IS NOT NULL
                """

                # Выполняем запрос напрямую для FK превью
                import time
                start = time.time()
                try:
                    cur.execute(join_sql)
                    result = cur.fetchall()
                    elapsed = int((time.time() - start) * 1000)
                    return {
                        "success": True,
                        "data": result[:max_rows],
                        "columns": [col.replace('t.', '').replace('r.', '').replace('"', '') for col in select_cols],
                        "rows_affected": len(result),
                        "execution_time_ms": elapsed,
                        "error_message": None
                    }
                except Exception as e:
                    elapsed = int((time.time() - start) * 1000)
                    return {
                        "success": False,
                        "data": [],
                        "columns": [],
                        "rows_affected": 0,
                        "execution_time_ms": elapsed,
                        "error_message": str(e)
                    }
            
        except Exception as e:
            # Return empty result on error with consistent format
            return {
                "success": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 0,
                "error_message": f"FK preview error: {str(e)}"
            }

    def normalize_schema_names(self, dsn: str, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Нормализует имена таблиц для SAP IQ: извлекает схему из DSN и добавляет её к таблицам без схемы."""
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # Извлекаем схему из DSN
        try:
            schema_arg = self.parse_schema_from_dsn(dsn)
        except Exception:
            schema_arg = None
        
        # SAP IQ использует DBA как схему по умолчанию
        default_schema = schema_arg or "DBA"
        
        for table_name, columns in db_schema.items():
            if "." not in table_name:
                # Добавляем схему если её нет
                normalized_name = f"{default_schema}.{table_name}"
            else:
                # Уже квалифицированное имя
                normalized_name = table_name
            normalized[normalized_name] = columns
        
        return normalized

    def build_select_all(self, table_name: str, limit: int) -> str:
        """SAP IQ SELECT * с TOP (вместо LIMIT)."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT TOP {self._normalize_row_limit(limit)} * FROM {quoted_table}"

    def _sql_word_tokens(self, sql: str):
        """Yield SQL word tokens with parenthesis depth, skipping strings/comments."""
        i = 0
        depth = 0
        length = len(sql)
        while i < length:
            ch = sql[i]
            nxt = sql[i + 1] if i + 1 < length else ""
            if ch == "-" and nxt == "-":
                i += 2
                while i < length and sql[i] not in "\r\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < length and not (sql[i] == "*" and sql[i + 1] == "/"):
                    i += 1
                i = min(length, i + 2)
                continue
            if ch in {"'", '"'}:
                quote = ch
                i += 1
                while i < length:
                    if sql[i] == quote:
                        if i + 1 < length and sql[i + 1] == quote:
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                continue
            if ch == "[":
                i += 1
                while i < length and sql[i] != "]":
                    i += 1
                i = min(length, i + 1)
                continue
            if ch == "(":
                depth += 1
                i += 1
                continue
            if ch == ")":
                depth = max(0, depth - 1)
                i += 1
                continue
            if ch.isalpha() or ch == "_":
                start = i
                i += 1
                while i < length and (sql[i].isalnum() or sql[i] in "_$"):
                    i += 1
                yield sql[start:i], depth
                continue
            i += 1

    def _top_level_select_has_row_cap(self, sql: str) -> bool:
        tokens = list(self._sql_word_tokens(sql))
        for index, (token, depth) in enumerate(tokens):
            if depth != 0 or token.upper() != "SELECT":
                continue
            next_index = index + 1
            while (
                next_index < len(tokens)
                and tokens[next_index][1] == 0
                and tokens[next_index][0].upper() in {"ALL", "DISTINCT"}
            ):
                next_index += 1
            return (
                next_index < len(tokens)
                and tokens[next_index][1] == 0
                and tokens[next_index][0].upper() in {"TOP", "FIRST"}
            )
        return False

    def limit_select_sql(self, sql: str, row_limit: int) -> str:
        """SAP IQ applies row caps with TOP instead of LIMIT."""
        limit = self._normalize_row_limit(row_limit)
        q = sql.strip().rstrip(";")
        if self._top_level_select_has_row_cap(q):
            return q
        return f"SELECT TOP {limit} * FROM ({q}) AS limited_subquery"

    def build_distinct_values_query(self, table_name: str, column_name: str, limit: int) -> str:
        """SAP IQ DISTINCT query using TOP syntax."""
        quoted_table = self.quote_identifier(table_name)
        quoted_column = self.quote_identifier(column_name)
        row_limit = self._normalize_row_limit(limit)
        return (
            f"SELECT DISTINCT TOP {row_limit} {quoted_column} "
            f"FROM {quoted_table} "
            f"WHERE {quoted_column} IS NOT NULL "
            f"ORDER BY {quoted_column}"
        )

    def get_default_schema(self) -> str:
        """SAP IQ использует схему 'dba' по умолчанию."""
        return "dba"

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для SAP IQ."""
        errors = []
        warnings = []
        
        if not parsed_dsn.hostname:
            errors.append("SAP IQ DSN должен содержать hostname")
        if not parsed_dsn.path or parsed_dsn.path == "/":
            errors.append("SAP IQ DSN должен содержать имя базы данных и схему")
        if not parsed_dsn.port:
            warnings.append("Не указан порт, будет использован порт по умолчанию (2638)")
        
        # SAP IQ может использовать схему по умолчанию
        if "." not in (parsed_dsn.path or "").strip("/"):
            warnings.append("SAP IQ: схема не указана, будет использована схема по умолчанию 'dba'")
            
        return errors, warnings
