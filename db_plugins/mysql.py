from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from .base import BaseDBPlugin

logger = logging.getLogger(__name__)


class MySQLPlugin(BaseDBPlugin):
    dialect = "mysql"
    dialect_label = "MySQL"

    def connect(self, dsn: str):
        # dsn формат: mysql://user:pass@host:port/db
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PyMySQL is required for MySQL connections. Install it with `pip install PyMySQL` "
                "or include it from requirements.txt."
            ) from exc
        from urllib.parse import urlparse

        connection_dsn, explicit_schema = self.split_connection_dsn_and_schema(dsn)
        u = urlparse(self.strip_plugin_query_options(connection_dsn))
        # В MySQL schema IS database: если в DSN есть суффикс db.schema,
        # split_connection_dsn_and_schema вернёт explicit_schema=<schema>, и мы
        # подключаемся к нему как к БД — это согласовано с normalize_schema_names
        # (квалификация имён таблиц). Обычный DSN без суффикса
        # (mysql://host/dbname) даёт explicit_schema=None → db = dbname из path,
        # поведение прежнее, регрессии для общего случая нет.
        conn = pymysql.connect(
            host=u.hostname or "localhost",
            port=u.port or 3306,
            user=self.decode_url_part(u.username) or "root",
            password=self.decode_url_part(u.password) or "",
            db=explicit_schema or (u.path or "/").lstrip("/") or "mysql",
            cursorclass=pymysql.cursors.Cursor,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                # NOTE: SET SESSION TRANSACTION READ ONLY blocks DML (INSERT/UPDATE/DELETE)
                # but does NOT block DDL (DROP, ALTER, CREATE) in MySQL 8.x.
                # For full protection, use a MySQL user with only SELECT privileges (GRANT SELECT ON ...).
                cur.execute("SET SESSION TRANSACTION READ ONLY;")
        except Exception as e:
            if self.read_only_fail_open_enabled(dsn):
                return conn
            try:
                conn.close()
            except Exception:
                pass
            raise RuntimeError("Failed to enable MySQL read-only session") from e
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        # sql уже прошёл верификацию выше по стеку — это SELECT-запрос,
        # EXPLAIN в MySQL не выполняет мутаций и не поддерживает параметризацию.
        with self._cursor(conn) as cur:
            cur.execute("EXPLAIN " + sql)
            rows = cur.fetchall()
        return {"plan": json.dumps(rows, ensure_ascii=False), "estimated_cost": None, "rows_to_scan": None, "issues": []}

    def execute_select(self, conn, sql: str, row_limit: int = 500) -> Dict[str, Any]:
        start = time.time()
        q = self.limit_select_sql(sql, row_limit)
        with self._cursor(conn) as cur:
            cur.execute(q)
            rows = self.fetch_rows_with_limit(cur, row_limit)
            columns = [d[0] for d in cur.description] if cur.description else []
        elapsed = int((time.time() - start) * 1000)
        return {"success": True, "data": rows, "columns": columns, "rows_affected": len(rows), "execution_time_ms": elapsed, "error_message": None}

    def introspect_schema(self, conn, schema: str | None = None, table_name: str | None = None) -> Dict[str, Dict[str, Dict[str, str]]]:
        # Строим WHERE условия динамически
        where_conditions = []
        params = []
        
        if schema:
            where_conditions.append("c.TABLE_SCHEMA = %s")
            params.append(schema)
        else:
            where_conditions.append("c.TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')")
        
        if table_name:
            where_conditions.append("c.TABLE_NAME = %s")
            params.append(table_name)
        
        where_clause = "WHERE " + " AND ".join(where_conditions)
        
        # Единый запрос с динамическими условиями
        query = f"""
            SELECT 
                c.TABLE_SCHEMA, 
                c.TABLE_NAME, 
                c.COLUMN_NAME, 
                c.DATA_TYPE, 
                c.COLUMN_COMMENT,
                c.IS_NULLABLE,
                c.COLUMN_DEFAULT,
                CASE 
                    WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRIMARY KEY'
                    WHEN fk.COLUMN_NAME IS NOT NULL THEN 'FOREIGN KEY'
                    WHEN uc.COLUMN_NAME IS NOT NULL THEN 'UNIQUE'
                    ELSE ''
                END AS constraint_type,
                CASE 
                    WHEN fk.COLUMN_NAME IS NOT NULL THEN 
                        CONCAT(fk.REFERENCED_TABLE_SCHEMA, '.', fk.REFERENCED_TABLE_NAME, '.', fk.REFERENCED_COLUMN_NAME)
                    ELSE ''
                END AS references
            FROM INFORMATION_SCHEMA.COLUMNS c
            -- Primary Keys
            LEFT JOIN (
                SELECT kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                 AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                 AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
                 AND kcu.TABLE_NAME = tc.TABLE_NAME
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            ) pk ON c.TABLE_SCHEMA = pk.TABLE_SCHEMA AND c.TABLE_NAME = pk.TABLE_NAME AND c.COLUMN_NAME = pk.COLUMN_NAME
            -- Foreign Keys
            LEFT JOIN (
                SELECT 
                    kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME,
                    kcu.REFERENCED_TABLE_SCHEMA, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                 AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                 AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
                 AND kcu.TABLE_NAME = tc.TABLE_NAME
                WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
            ) fk ON c.TABLE_SCHEMA = fk.TABLE_SCHEMA AND c.TABLE_NAME = fk.TABLE_NAME AND c.COLUMN_NAME = fk.COLUMN_NAME
            -- Unique constraints
            LEFT JOIN (
                SELECT kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                 AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                 AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
                 AND kcu.TABLE_NAME = tc.TABLE_NAME
                WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
            ) uc ON c.TABLE_SCHEMA = uc.TABLE_SCHEMA AND c.TABLE_NAME = uc.TABLE_NAME AND c.COLUMN_NAME = uc.COLUMN_NAME
            {where_clause}
            ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
        """

        # Формируем схему в новом формате
        schema_result: Dict[str, Dict[str, Any]] = {}
        with self._cursor(conn) as cur:
            cur.execute(query, params)

            for schema_name, table, col, dtype, comment, is_nullable, default_val, constraint_type, references in cur.fetchall():
                key = f"{schema_name}.{table}"

                # Создаем таблицу если её нет
                if key not in schema_result:
                    schema_result[key] = {
                        "description": "",  # Будет заполнено LLM
                        "columns": {}
                    }

                # Нормализуем информацию о колонке
                col_info = {
                    "type": dtype,
                    "description": comment or "",
                    "not_null": str(is_nullable == "NO"),
                    "default_value": str(default_val) if default_val is not None else "",
                    "constraint_type": constraint_type or "",
                    "references": references or ""
                }

                schema_result[key]["columns"][col] = self.normalize_column_info(col_info)

        return schema_result
    
    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows using MySQL statistics."""
        try:
            # Try information_schema first (fastest)
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = conn.database  # Current database
                table_name_only = table_name

            sql = (
                "SELECT table_rows FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s"
            )
            with self._cursor(conn) as cur:
                cur.execute(sql, (schema_name, table_name_only))
                row = cur.fetchone()
            if row and row[0] is not None and row[0] > 0:
                return int(row[0])
        except Exception as e:
            logger.warning("MySQL estimate_row_count information_schema failed for %s: %s", table_name, e)

        # Fallback to exact count
        try:
            quoted_table = self.quote_identifier(table_name)
            sql = f"SELECT COUNT(*) FROM {quoted_table}"
            with self._cursor(conn) as cur:
                cur.execute(sql)
                row = cur.fetchone()
            return int(row[0]) if row else 1000000
        except Exception as e:
            logger.warning("MySQL estimate_row_count COUNT(*) failed for %s: %s", table_name, e)

        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get column statistics for MySQL."""
        stats = {}
        try:
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = conn.database
                table_name_only = table_name

            # Get basic column info
            sql = (
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s"
            )

            with self._cursor(conn) as cur:
                cur.execute(sql, (schema_name, table_name_only))
                columns = cur.fetchall()

                quoted_table = self.quote_identifier(table_name)
                for col_name, data_type, is_nullable, col_default in columns:
                    stats[col_name] = {
                        'type': data_type,
                        'nullable': is_nullable == 'YES',
                        'default': str(col_default) if col_default else None,
                        'sample_values': []
                    }

                    # Get sample values
                    try:
                        import os
                        sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                        quoted_col = self.quote_identifier(col_name)
                        sample_sql = (
                            f"SELECT DISTINCT {quoted_col} FROM {quoted_table} "
                            f"WHERE {quoted_col} IS NOT NULL LIMIT {int(sample_limit)}"
                        )
                        cur.execute(sample_sql)
                        samples = cur.fetchall()
                        stats[col_name]['sample_values'] = [str(row[0])[:50] for row in samples if row[0] is not None]
                    except Exception:
                        continue

        except Exception as e:
            logger.warning("MySQL get_basic_column_stats failed for %s: %s", table_name, e)

        return stats

    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for MySQL."""
        quoted_table = self.quote_identifier(table_name)
        limit = int(max_rows)
        try:
            if strategy == 'small':
                # For small tables, use ORDER BY RAND()
                sql = f"SELECT * FROM {quoted_table} ORDER BY RAND() LIMIT {limit}"
            elif strategy == 'medium':
                # For medium tables, use sampling
                sql = f"SELECT * FROM {quoted_table} WHERE RAND() < 0.01 LIMIT {limit}"
            else:  # large
                # For large tables, minimal sampling
                sql = f"SELECT * FROM {quoted_table} WHERE RAND() < 0.001 LIMIT {limit}"

            return self.execute_select(conn, sql, row_limit=max_rows)

        except Exception as e:
            logger.warning("MySQL sample_rows_smart failed for %s: %s", table_name, e)
            # Fallback to simple LIMIT
            sql = f"SELECT * FROM {quoted_table}"
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
        """Get FK preview for MySQL."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            if '.' in ref_table:
                ref_schema, ref_table_only = ref_table.split('.', 1)
            else:
                ref_schema = conn.database
                ref_table_only = ref_table
            if '.' in table_name:
                source_schema, source_table = table_name.split('.', 1)
            else:
                source_schema = conn.database
                source_table = table_name
                
            # Get readable columns from reference table
            import os
            sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
            sql = (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "AND (LOWER(column_name) LIKE '%%name%%' "
                "     OR LOWER(column_name) LIKE '%%title%%' "
                "     OR LOWER(column_name) LIKE '%%description%%') "
                f"LIMIT {int(sample_limit)}"
            )

            with self._cursor(conn) as cur:
                if ref_column is None:
                    ref_column_sql = (
                        "SELECT referenced_column_name FROM information_schema.key_column_usage "
                        "WHERE table_schema = %s AND table_name = %s AND column_name = %s "
                        "AND referenced_table_schema = %s AND referenced_table_name = %s "
                        "AND referenced_column_name IS NOT NULL LIMIT 1"
                    )
                    cur.execute(ref_column_sql, (source_schema, source_table, fk_column, ref_schema, ref_table_only))
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

                cur.execute(sql, (ref_schema, ref_table_only))
                readable_cols = [row[0] for row in cur.fetchall()]

                if not readable_cols:
                    # Fallback to first few columns
                    sql = (
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s LIMIT 2"
                    )
                    cur.execute(sql, (ref_schema, ref_table_only))
                    readable_cols = [row[0] for row in cur.fetchall()]

                # Build JOIN query
                qfk = self.quote_identifier(fk_column)
                qref_col = self.quote_identifier(ref_column)
                quoted_table = self.quote_identifier(table_name)
                quoted_ref = self.quote_identifier(ref_table)
                select_cols = [f't.{qfk}'] + [f'r.{self.quote_identifier(col)}' for col in readable_cols]
                select_str = ', '.join(select_cols)

                join_sql = (
                    f"SELECT {select_str} FROM {quoted_table} t "
                    f"JOIN {quoted_ref} r ON t.{qfk} = r.{qref_col} "
                    f"WHERE t.{qfk} IS NOT NULL LIMIT {int(max_rows)}"
                )

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
                        "columns": [col.replace('t.', '').replace('r.', '').replace('`', '') for col in select_cols],
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
        """Нормализует имена таблиц для MySQL: извлекает схему из DSN и добавляет её к таблицам без схемы."""
        from urllib.parse import urlparse
        
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # Извлекаем схему (database) из DSN
        try:
            connection_dsn, schema_arg = self.split_connection_dsn_and_schema(dsn)
            if not schema_arg:
                p = urlparse(connection_dsn)
                schema_arg = (p.path or "").strip("/") or None
        except Exception:
            schema_arg = None
        
        # Если схема не определена, используем mysql как default
        default_schema = schema_arg or "mysql"
        
        for table_name, columns in db_schema.items():
            if "." not in table_name:
                # Добавляем схему если её нет
                normalized_name = f"{default_schema}.{table_name}"
            else:
                # Уже квалифицированное имя
                normalized_name = table_name
            normalized[normalized_name] = columns
        
        return normalized

    def quote_identifier(self, identifier: str) -> str:
        """MySQL использует бэктики для квотирования.

        Если идентификатор составной (schema.table[.column]), квотируем каждую часть отдельно.
        """
        if not identifier:
            return identifier

        parts = str(identifier).split(".")
        quoted_parts = []
        for part in parts:
            if part == "":
                continue
            if not self._identifier_needs_quoting(part):
                quoted_parts.append(part)
            else:
                escaped = part.replace("`", "``")
                quoted_parts.append(f"`{escaped}`")

        return ".".join(quoted_parts)

    def build_select_all(self, table_name: str, limit: int) -> str:
        """MySQL SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"

    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """MySQL uses the connection database as the active schema."""
        from urllib.parse import urlparse

        connection_dsn, explicit_schema = self.split_connection_dsn_and_schema(dsn)
        if explicit_schema:
            return explicit_schema
        return (urlparse(connection_dsn).path or "").strip("/") or None

    def get_default_schema(self) -> str:
        """MySQL не использует схемы в традиционном смысле, база данных является схемой."""
        return "mysql"  # Fallback значение

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для MySQL."""
        errors = []
        warnings = []
        
        if not parsed_dsn.hostname:
            errors.append("MySQL DSN должен содержать hostname")
        if not parsed_dsn.path or parsed_dsn.path == "/":
            errors.append("MySQL DSN должен содержать имя базы данных")
        if not parsed_dsn.port:
            warnings.append("Не указан порт, будет использован порт по умолчанию (3306)")
            
        return errors, warnings
