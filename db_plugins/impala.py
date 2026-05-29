from __future__ import annotations

import json
import time
import os
import logging
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlparse
from .base import BaseDBPlugin

logger = logging.getLogger(__name__)


class ImpalaPlugin(BaseDBPlugin):
    dialect = "impala"
    dialect_label = "Impala"

    def connect(self, dsn: str):
        # dsn: impala://user:pass@host:21050/db?auth_mechanism=GSSAPI
        try:
            import impala.dbapi as impaladb  # type: ignore
        except Exception as e:
            raise RuntimeError("impyla is not installed. pip install impyla") from e
        connection_dsn, _ = self.split_connection_dsn_and_schema(dsn)
        u = urlparse(self.strip_plugin_query_options(connection_dsn))
        host = u.hostname or "localhost"
        port = u.port or 21050
        database = (u.path or "/").lstrip("/") or None
        user = self.decode_url_part(u.username)
        password = self.decode_url_part(u.password)
        # Параметры аутентификации из query
        params = dict(parse_qsl(u.query, keep_blank_values=True))
        conn = impaladb.connect(host=host, port=port, user=user, password=password, database=database, auth_mechanism=params.get("auth_mechanism"))
        # Impala не поддерживает SET TRANSACTION READ ONLY, поэтому read-only режим не enforced на уровне сессии.
        # Решение о возврате соединения принимается строго по явному opt-in через DSN-параметр read_only_fail_open.
        fail_open_explicit = self.read_only_fail_open_enabled(dsn)
        if not fail_open_explicit:
            try:
                conn.close()
            finally:
                raise RuntimeError(
                    "Impala read-only session enforcement is not implemented. "
                    "Add read_only_fail_open=true to the DSN to explicitly allow an unenforced read-only connection."
                )
        logger.warning(
            "Impala connection returned WITHOUT read-only enforcement: "
            "read_only_fail_open=true was explicitly set in the DSN, user opted in to unenforced read-only access. "
            "Any INSERT/UPDATE/DELETE will execute without restriction."
        )
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        with self._cursor(conn) as cur:
            cur.execute(f"EXPLAIN {sql}")
            rows = cur.fetchall()
        # В impala строки EXPLAIN — текстовые
        plan_text = "\n".join(str(r[0]) if isinstance(r, (list, tuple)) else str(r) for r in rows)
        return {"plan": plan_text, "estimated_cost": None, "rows_to_scan": None, "issues": []}

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
        # Используем INFORMATION_SCHEMA при наличии, иначе SHOW TABLES/DESCRIBE
        result: Dict[str, Dict[str, Dict[str, str]]] = {}

        # Строим фильтрацию для table_name
        include = {x.strip() for x in (os.getenv("SCHEMA_INCLUDE_TABLES", "").split(",")) if x.strip()}
        try:
            # Строим WHERE условия динамически
            where_conditions = []
            params = []
            
            if schema:
                where_conditions.append("table_schema = %s")
                params.append(schema)
            
            if table_name:
                where_conditions.append("table_name = %s")
                params.append(table_name)
            
            where_clause = ""
            if where_conditions:
                where_clause = "WHERE " + " AND ".join(where_conditions)
            
            # Impala НЕ ПОДДЕРЖИВАЕТ constraints (PK/FK/UNIQUE)
            # Используем только базовую информацию о колонках
            query = f"""
                SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
                FROM INFORMATION_SCHEMA.COLUMNS {where_clause}
                ORDER BY table_schema, table_name, ordinal_position
            """
            with self._cursor(conn) as cur:
                cur.execute(query, params)

                for row in cur.fetchall():
                    if len(row) >= 6:  # Расширенный формат
                        schema_name, table, col, dtype, is_nullable, default_val = row
                    else:  # Базовый формат
                        schema_name, table, col, dtype = row[:4]
                        is_nullable = None
                        default_val = None

                    key = f"{schema_name}.{table}"
                    if include and (table not in include and key not in include):
                        continue

                    col_info = {
                        "type": dtype,
                        "description": "",
                        "not_null": str(is_nullable == "NO") if is_nullable else "",
                        "default_value": str(default_val) if default_val is not None else "",
                        "constraint_type": "",  # Impala не поддерживает constraints
                        "references": ""  # Impala не поддерживает FK
                    }
                    result.setdefault(key, {"description": "", "columns": {}})["columns"][col] = self.normalize_column_info(col_info)
            return result
        except Exception as information_schema_error:
            try:
                with self._cursor(conn) as cur:
                    if schema:
                        dbs = [schema]
                    else:
                        cur.execute("SHOW DATABASES")
                        all_dbs = [r[0] for r in cur.fetchall()]
                        # Фильтруем системные базы данных Impala
                        dbs = [db for db in all_dbs if db.lower() not in
                               ('information_schema', 'sys', '_impala_builtins')]
                    for db in dbs:
                        cur.execute(f"SHOW TABLES IN {self.quote_identifier(db)}")
                        tables = [r[0] for r in cur.fetchall()]
                        for t in tables:
                            key = f"{db}.{t}"
                            if include and (t not in include and key not in include):
                                continue
                            cur.execute(f"DESCRIBE {self.quote_identifier(key)}")
                            cols = cur.fetchall()
                            for c in cols:
                                if len(c) >= 2:
                                    col_info = {
                                        "type": c[1],
                                        "description": "",
                                        "not_null": "",
                                        "default_value": "",
                                        "constraint_type": "",
                                        "references": ""
                                    }
                                    result.setdefault(key, {"description": "", "columns": {}})["columns"][c[0]] = self.normalize_column_info(col_info)
                return result
            except Exception as describe_error:
                raise RuntimeError(
                    "Impala schema introspection failed via INFORMATION_SCHEMA and DESCRIBE; "
                    f"INFORMATION_SCHEMA error: {information_schema_error}"
                ) from describe_error
    
    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows for Impala tables."""
        try:
            with self._cursor(conn) as cur:
                # Parse table name
                if '.' in table_name:
                    schema_name, table_name_only = table_name.split('.', 1)
                else:
                    schema_name = None
                    table_name_only = table_name

                # Try to get table stats from Impala's SHOW TABLE STATS
                try:
                    if schema_name:
                        cur.execute(f"USE {self.quote_identifier(schema_name)}")
                    cur.execute(f"SHOW TABLE STATS {self.quote_identifier(table_name_only)}")
                    stats = cur.fetchall()

                    # Look for #Rows column in stats output
                    if stats and len(stats[0]) > 1:
                        # Usually column 1 or 2 contains row count
                        for row in stats:
                            if len(row) > 2 and str(row[2]).isdigit():
                                return int(row[2])
                            elif len(row) > 1 and str(row[1]).isdigit():
                                return int(row[1])
                except Exception as e:
                    logger.warning("Impala SHOW TABLE STATS failed for %s: %s", table_name, e)

                # Fallback to exact count with LIMIT for safety
                try:
                    count_sql = f"SELECT COUNT(*) FROM {table_name} LIMIT 1"
                    cur.execute(count_sql)
                    row = cur.fetchone()
                    return int(row[0]) if row else 1000000
                except Exception as e:
                    logger.warning("Impala COUNT(*) fallback failed for %s: %s", table_name, e)

        except Exception as e:
            logger.warning("Impala estimate_row_count failed for %s: %s", table_name, e)

        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get basic column statistics for Impala."""
        stats = {}
        try:
            with self._cursor(conn) as cur:
                # Parse table name
                if '.' in table_name:
                    schema_name, table_name_only = table_name.split('.', 1)
                else:
                    schema_name = None
                    table_name_only = table_name

                # Switch to the correct database if needed
                if schema_name:
                    cur.execute(f"USE {self.quote_identifier(schema_name)}")

                # Get column information using DESCRIBE
                cur.execute(f"DESCRIBE {self.quote_identifier(table_name_only)}")
                columns_info = cur.fetchall()

                for col_info in columns_info:
                    if len(col_info) < 2:
                        continue

                    col_name = col_info[0]
                    col_type = col_info[1]

                    stats[col_name] = {
                        'type': col_type,
                        'null_count': 0,
                        'distinct_count': 0,
                        'sample_values': []
                    }

                    # Get basic statistics for this column
                    try:
                        # Get null count and distinct count
                        q_col = self.quote_identifier(col_name)
                        stats_sql = f"""
                        SELECT
                            SUM(CASE WHEN {q_col} IS NULL THEN 1 ELSE 0 END) as null_count,
                            COUNT(DISTINCT {q_col}) as distinct_count
                        FROM {table_name}
                        """
                        cur.execute(stats_sql)
                        result = cur.fetchone()
                        if result:
                            stats[col_name]['null_count'] = int(result[0] or 0)
                            stats[col_name]['distinct_count'] = int(result[1] or 0)

                        # Get sample values using LIMIT
                        import os
                        sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                        sample_sql = f"SELECT DISTINCT {q_col} FROM {table_name} WHERE {q_col} IS NOT NULL LIMIT {sample_limit}"
                        cur.execute(sample_sql)
                        samples = cur.fetchall()
                        stats[col_name]['sample_values'] = [str(row[0])[:50] for row in samples if row[0] is not None]

                    except Exception:
                        # Skip problematic columns
                        continue

        except Exception as e:
            logger.warning("Impala get_basic_column_stats failed for %s: %s", table_name, e)

        return stats
    
    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for Impala with enhanced TABLESAMPLE support."""
        try:
            # Parse table name for database switching
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
                with self._cursor(conn) as cur:
                    cur.execute(f"USE {self.quote_identifier(schema_name)}")
                table_for_query = table_name_only
            else:
                table_for_query = table_name
            
            # Enhanced sampling strategies with Impala TABLESAMPLE
            if strategy == 'small':
                # For small tables, use simple ORDER BY RAND() for diversity
                sql = f"""
                SELECT * FROM {table_for_query} 
                ORDER BY RAND() 
                LIMIT {max_rows}
                """
            elif strategy == 'medium':
                # For medium tables, use TABLESAMPLE with fallback options
                sampling_options = [
                    f"SELECT * FROM {table_for_query} TABLESAMPLE(SYSTEM(5)) LIMIT {max_rows}",
                    f"SELECT * FROM {table_for_query} TABLESAMPLE(1 PERCENT) LIMIT {max_rows}",
                    f"SELECT * FROM {table_for_query} WHERE RAND() < 0.01 LIMIT {max_rows}"
                ]
                
                for sql in sampling_options:
                    try:
                        result = self.execute_select(conn, sql, row_limit=max_rows)
                        if result.get('success', True) and result.get('data'):
                            return result
                    except Exception as e:
                        logger.debug(f"Impala sampling attempt failed: {sql}, error: {e}")
                        continue
                
                # Final fallback
                sql = f"SELECT * FROM {table_for_query} LIMIT {max_rows}"
                
            else:  # large strategy
                # For large tables, aggressive sampling with multiple fallbacks
                sampling_options = [
                    f"SELECT * FROM {table_for_query} TABLESAMPLE(SYSTEM(1)) LIMIT {max_rows}",
                    f"SELECT * FROM {table_for_query} TABLESAMPLE(0.1 PERCENT) LIMIT {max_rows}",
                    f"SELECT * FROM {table_for_query} WHERE RAND() < 0.001 LIMIT {max_rows}",
                    f"SELECT * FROM {table_for_query} WHERE RAND() < 0.01 LIMIT {max_rows}"
                ]
                
                for sql in sampling_options:
                    try:
                        result = self.execute_select(conn, sql, row_limit=max_rows)
                        if result.get('success', True) and result.get('data'):
                            return result
                    except Exception as e:
                        logger.debug(f"Impala sampling attempt failed: {sql}, error: {e}")
                        continue
                
                # Final fallback
                sql = f"SELECT * FROM {table_for_query} LIMIT {max_rows}"
            
            return self.execute_select(conn, sql, row_limit=max_rows)
            
        except Exception as e:
            logger.warning(f"Impala smart sampling failed: {e}")
            # Ultimate fallback to simple LIMIT
            sql = f"SELECT * FROM {table_name} LIMIT {max_rows}"
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
        """Get FK preview for Impala."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            # Parse table names for database switching
            if '.' in table_name:
                source_schema, source_table = table_name.split('.', 1)
            else:
                source_schema = None
                source_table = table_name

            if '.' in ref_table:
                ref_schema, ref_table_only = ref_table.split('.', 1)
            else:
                ref_schema = source_schema  # Use same schema as source table
                ref_table_only = ref_table

            if ref_column is None:
                return {
                    "success": False,
                    "data": [],
                    "columns": [],
                    "rows_affected": 0,
                    "execution_time_ms": 0,
                    "error_message": "Impala FK preview requires ref_column because Impala does not expose FK metadata"
                }

            with self._cursor(conn) as cur:
                # Switch to appropriate database
                if ref_schema:
                    cur.execute(f"USE {ref_schema}")

                # Get columns from reference table using DESCRIBE
                cur.execute(f"DESCRIBE {ref_table_only}")
                ref_columns = cur.fetchall()

                # Look for readable column names
                readable_col_names = []

                for col_info in ref_columns:
                    if len(col_info) < 1:
                        continue
                    col_name = col_info[0]

                    # Look for readable columns
                    col_lower = col_name.lower()
                    if any(keyword in col_lower for keyword in ['name', 'title', 'description', 'label']):
                        readable_col_names.append(col_name)

                # If no readable columns found, use first few columns
                if not readable_col_names:
                    readable_col_names = [col_info[0] for col_info in ref_columns[:2] if len(col_info) >= 1]

                # Limit to 3 columns max
                readable_col_names = readable_col_names[:3]

                # Build JOIN query
                qfk = self.quote_identifier(fk_column)
                qref_col = self.quote_identifier(ref_column)
                select_cols = [f't.{qfk}'] + [f'r.{self.quote_identifier(col)}' for col in readable_col_names]
                select_str = ', '.join(select_cols)

                # Use appropriate table references
                source_table_ref = self.quote_identifier(f"{source_schema}.{source_table}" if source_schema else source_table)
                ref_table_ref = self.quote_identifier(f"{ref_schema}.{ref_table_only}" if ref_schema else ref_table_only)

                join_sql = f"""
                SELECT {select_str}
                FROM {source_table_ref} t
                JOIN {ref_table_ref} r ON t.{qfk} = r.{qref_col}
                WHERE t.{qfk} IS NOT NULL
                LIMIT {max_rows}
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
        """Нормализует имена таблиц для Impala: извлекает схему из DSN и добавляет её к таблицам без схемы."""
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
        
        # Если схема не определена, используем default как default
        default_schema = schema_arg or "default"
        
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
        """Impala использует бэктики для квотирования.

        Если идентификатор составной (schema.table[.column]), квотируем каждую часть.
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
        """Impala SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"

    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """Impala uses the connection database as the active schema."""
        connection_dsn, explicit_schema = self.split_connection_dsn_and_schema(dsn)
        if explicit_schema:
            return explicit_schema
        return (urlparse(connection_dsn).path or "").strip("/") or None

    def get_default_schema(self) -> str:
        """Impala использует схему 'default' по умолчанию."""
        return "default"

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для Impala."""
        errors = []
        warnings = []
        
        if not parsed_dsn.hostname:
            errors.append("Impala DSN должен содержать hostname")
        if not parsed_dsn.port:
            warnings.append("Не указан порт, будет использован порт по умолчанию (21050)")
            
        return errors, warnings
