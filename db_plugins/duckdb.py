from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from .base import BaseDBPlugin

logger = logging.getLogger(__name__)


class DuckDBPlugin(BaseDBPlugin):
    dialect = "duckdb"
    dialect_label = "DuckDB"

    def connect(self, dsn: str):
        # dsn формат: duckdb:///abs/path/to.duckdb
        import duckdb  # type: ignore
        from urllib.parse import urlparse
        
        if dsn.startswith("duckdb:"):
            connection_dsn, _ = self.split_connection_dsn_and_schema(dsn)
            driver_dsn = self.strip_plugin_query_options(connection_dsn)
            path = urlparse(driver_dsn).path or driver_dsn[7:]
        else:
            path = dsn
        if path == "/:memory:":
            path = ":memory:"
            
        # Для in-memory БД нельзя использовать read_only режим
        if path == ":memory:" or path == "":
            conn = duckdb.connect(path, read_only=False)
        else:
            try:
                conn = duckdb.connect(path, read_only=True)
            except Exception as e:
                if not self.read_only_fail_open_enabled(dsn):
                    raise RuntimeError("Failed to open DuckDB database in read-only mode") from e
                conn = duckdb.connect(path, read_only=False)
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        # sql уже проверен выше по стеку; EXPLAIN не поддерживает параметризацию.
        with self._cursor(conn) as cur:
            cur.execute("EXPLAIN " + sql)
            rows = cur.fetchall()
        plan_text = "\n".join(" ".join(str(x) for x in r) for r in rows)
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
        # Строим WHERE условия динамически
        where_conditions = []
        params = []
        
        if schema:
            where_conditions.append("c.table_schema = ?")
            params.append(schema)
        else:
            where_conditions.append("c.table_schema NOT IN ('information_schema', 'pg_catalog')")
        
        if table_name:
            where_conditions.append("c.table_name = ?")
            params.append(table_name)
        
        where_clause = "WHERE " + " AND ".join(where_conditions)
        
        # Сначала пытаемся получить информацию с constraints через information_schema
        try:
            query = f"""
                SELECT 
                    c.table_schema, 
                    c.table_name, 
                    c.column_name, 
                    c.data_type,
                    c.is_nullable,
                    c.column_default,
                    -- Определяем тип ограничения
                    CASE 
                        WHEN pk.column_name IS NOT NULL THEN 'PK'
                        WHEN fk.column_name IS NOT NULL THEN 'FK'
                        WHEN uc.column_name IS NOT NULL THEN 'UNIQUE'
                        ELSE ''
                    END AS constraint_type,
                    -- Для FK определяем ссылку
                    CASE 
                        WHEN fk.column_name IS NOT NULL THEN 
                            CONCAT(fk.foreign_table_schema, '.', fk.foreign_table_name, '.', fk.foreign_column_name)
                        ELSE ''
                    END AS references
                FROM information_schema.columns c
                -- Primary keys
                LEFT JOIN (
                    SELECT kcu.table_schema, kcu.table_name, kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_schema = kcu.constraint_schema
                     AND tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                ) pk ON c.table_schema = pk.table_schema AND c.table_name = pk.table_name AND c.column_name = pk.column_name
                -- Foreign keys
                LEFT JOIN (
                    SELECT 
                        kcu.table_schema, kcu.table_name, kcu.column_name,
                        ccu.table_schema AS foreign_table_schema,
                        ccu.table_name AS foreign_table_name,
                        ccu.column_name AS foreign_column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_schema = kcu.constraint_schema
                     AND tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON tc.constraint_schema = ccu.constraint_schema
                     AND tc.constraint_name = ccu.constraint_name
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                ) fk ON c.table_schema = fk.table_schema AND c.table_name = fk.table_name AND c.column_name = fk.column_name
                -- Unique constraints
                LEFT JOIN (
                    SELECT kcu.table_schema, kcu.table_name, kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_schema = kcu.constraint_schema
                     AND tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'UNIQUE'
                ) uc ON c.table_schema = uc.table_schema AND c.table_name = uc.table_name AND c.column_name = uc.column_name
                {where_clause}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
        except Exception:
            # Fallback к базовому запросу без constraints
            query = f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM information_schema.columns c
                {where_clause}
            """

        # Формируем схему в новом формате
        schema_result: Dict[str, Dict[str, Any]] = {}
        with self._cursor(conn) as cur:
            cur.execute(query, params)
            for row in cur.fetchall():
                if len(row) >= 8:  # Расширенный формат с constraints
                    schema_name, table, col, dtype, is_nullable, default_val, constraint_type, references = row
                    col_info = {
                        "type": dtype,
                        "description": "",
                        "not_null": str(is_nullable == "NO") if is_nullable else "",
                        "default_value": str(default_val) if default_val is not None else "",
                        "constraint_type": constraint_type or "",
                        "references": references or ""
                    }
                else:  # Fallback к базовому формату
                    schema_name, table, col, dtype = row[:4]
                    col_info = {"type": dtype, "description": "", "not_null": "", "default_value": "", "constraint_type": "", "references": ""}

                key = f"{schema_name}.{table}"

                # Создаем таблицу если её нет
                if key not in schema_result:
                    schema_result[key] = {
                        "description": "",  # Будет заполнено LLM
                        "columns": {}
                    }

                schema_result[key]["columns"][col] = self.normalize_column_info(col_info)

            # Если information_schema не поддерживает constraints, используем fallback к PRAGMA
            try:
                # Обогащаем информацией из PRAGMA table_info только если constraints не найдены
                for table_key in schema_result.keys():
                    _, table_name = table_key.split(".", 1)
                    has_constraints = any(col.get("constraint_type") for col in schema_result[table_key]["columns"].values())

                    if not has_constraints:  # Только если constraints не найдены через information_schema
                        try:
                            # DuckDB PRAGMA не поддерживает позиционные параметры (?),
                            # поэтому используем строковый литерал с эскейпом одинарных кавычек.
                            # Это предотвращает SQL-инъекцию через имена таблиц со спец-символами
                            # (T1.6c). table_name приходит из information_schema, но может содержать
                            # кавычки/точки/прочие символы — экранируем удвоением апострофа.
                            escaped_table_name = table_name.replace("'", "''")
                            pragma_info = cur.execute(
                                f"PRAGMA table_info('{escaped_table_name}')"
                            ).fetchall()
                            for cid, col_name, col_type, not_null, default_val, is_pk in pragma_info:
                                if col_name in schema_result[table_key]["columns"]:
                                    # Обновляем и нормализуем расширенную информацию
                                    existing = schema_result[table_key]["columns"][col_name].copy()
                                    existing["not_null"] = str(bool(not_null))
                                    existing["default_value"] = str(default_val) if default_val is not None else ""
                                    existing["constraint_type"] = "PK" if is_pk else existing.get("constraint_type", "")
                                    schema_result[table_key]["columns"][col_name] = self.normalize_column_info(existing)

                        except Exception:
                            # Если PRAGMA table_info не работает, продолжаем без расширенной информации
                            pass

            except Exception:
                pass

        return schema_result
    

    
    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows in table using DuckDB statistics."""
        try:
            quoted_table = self.quote_identifier(table_name)
            sql = f"SELECT COUNT(*) FROM {quoted_table}"
            result = conn.execute(sql).fetchone()
            if result and result[0] is not None:
                return int(result[0])
        except Exception as e:
            logger.warning("DuckDB estimate_row_count failed for %s: %s", table_name, e)
        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get basic column statistics for DuckDB."""
        stats = {}
        try:
            # PRAGMA не поддерживает параметризацию; используем параметризованный
            # запрос к information_schema для безопасности.
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = 'main'
                table_name_only = table_name
            columns_info_raw = conn.execute(
                "SELECT ordinal_position, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                [schema_name, table_name_only],
            ).fetchall()
            # Приводим к формату PRAGMA table_info: (cid, name, type, ...)
            columns_info = [(row[0], row[1], row[2]) for row in columns_info_raw]

            quoted_table = self.quote_identifier(table_name)
            for col_info in columns_info:
                col_name = col_info[1]  # name
                col_type = col_info[2]  # type

                # Basic stats structure
                stats[col_name] = {
                    'type': col_type,
                    'null_count': 0,
                    'distinct_count': 0,
                    'sample_values': []
                }

                # Try to get some quick stats
                try:
                    quoted_col = self.quote_identifier(col_name)
                    stats_sql = (
                        f"SELECT COUNT(*) - COUNT({quoted_col}) as null_count, "
                        f"COUNT(DISTINCT {quoted_col}) as distinct_count "
                        f"FROM {quoted_table}"
                    )
                    result = conn.execute(stats_sql).fetchone()
                    if result:
                        stats[col_name]['null_count'] = int(result[0] or 0)
                        stats[col_name]['distinct_count'] = int(result[1] or 0)

                    # Get a few sample values (with proper quoting)
                    import os
                    sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                    sample_sql = (
                        f"SELECT DISTINCT {quoted_col} FROM {quoted_table} "
                        f"WHERE {quoted_col} IS NOT NULL LIMIT {int(sample_limit)}"
                    )
                    samples = conn.execute(sample_sql).fetchall()
                    stats[col_name]['sample_values'] = [str(row[0])[:50] for row in samples if row[0] is not None]

                except Exception:
                    # Skip stats for problematic columns
                    continue

        except Exception as e:
            logger.warning("DuckDB get_basic_column_stats failed for %s: %s", table_name, e)

        return stats
    
    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for DuckDB based on table size strategy."""
        quoted_table = self.quote_identifier(table_name)
        limit = int(max_rows)
        try:
            if strategy == 'small':
                sql = f"SELECT * FROM {quoted_table} ORDER BY random() LIMIT {limit}"
            elif strategy == 'medium':
                sql = f"SELECT * FROM {quoted_table} WHERE random() < 0.01 LIMIT {limit}"
            else:  # large strategy
                sql = f"SELECT * FROM {quoted_table} WHERE random() < 0.001 LIMIT {limit}"

            # Use existing execute_select method
            return self.execute_select(conn, sql, row_limit=max_rows)

        except Exception:
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
        """Get FK preview - sample JOIN with referenced table."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            # Try to find readable columns in reference table (name, title, description, etc.)
            # Для DuckDB используем только имя таблицы (без схемы)
            if '.' in ref_table:
                ref_schema_name, clean_ref_table = ref_table.split('.', 1)
            else:
                ref_schema_name = 'main'
                clean_ref_table = ref_table
            # Эмулируем формат PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
            ref_columns_raw = conn.execute(
                "SELECT c.ordinal_position, c.column_name, c.data_type, "
                "CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END, "
                "c.column_default, "
                "CASE WHEN EXISTS ("
                "  SELECT 1 FROM information_schema.table_constraints tc "
                "  JOIN information_schema.key_column_usage kcu "
                "    ON tc.constraint_schema = kcu.constraint_schema "
                "   AND tc.constraint_name = kcu.constraint_name "
                "   AND tc.table_schema = kcu.table_schema "
                "   AND tc.table_name = kcu.table_name "
                "  WHERE tc.constraint_type = 'PRIMARY KEY' "
                "    AND kcu.table_schema = c.table_schema "
                "    AND kcu.table_name = c.table_name "
                "    AND kcu.column_name = c.column_name) THEN 1 ELSE 0 END "
                "FROM information_schema.columns c "
                "WHERE c.table_schema = ? AND c.table_name = ? "
                "ORDER BY c.ordinal_position",
                [ref_schema_name, clean_ref_table],
            ).fetchall()
            ref_columns = ref_columns_raw
            
            # Look for readable column names and find PK
            readable_col_names = []
            pk_col = None
            for col_info in ref_columns:
                col_name = col_info[1]  # name
                is_pk = col_info[5]     # pk
                
                if is_pk:
                    pk_col = col_name
                    
                # Look for readable columns
                col_lower = col_name.lower()
                if any(keyword in col_lower for keyword in ['name', 'title', 'description', 'label']):
                    readable_col_names.append(col_name)
            
            if ref_column is None:
                ref_column = pk_col if pk_col else ('id' if any(c[1] == 'id' for c in ref_columns) else ref_columns[0][1] if ref_columns else fk_column)
            
            # If no readable columns found, use PK or first few columns
            if not readable_col_names:
                if pk_col:
                    readable_col_names = [pk_col]
                else:
                    readable_col_names = [col_info[1] for col_info in ref_columns[:2]]
            
            # Limit to 3 columns max
            readable_col_names = readable_col_names[:3]
            
            qfk = self.quote_identifier(fk_column)
            qref_col = self.quote_identifier(ref_column)
            quoted_table = self.quote_identifier(table_name)
            quoted_ref = self.quote_identifier(ref_table)
            select_cols = [f't.{qfk}'] + [f'r.{self.quote_identifier(col)}' for col in readable_col_names]
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
                result = conn.execute(join_sql).fetchall()
                columns = [row[1] for row in conn.execute(
                    "SELECT ordinal_position, column_name FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    [ref_schema_name, clean_ref_table],
                ).fetchall()][:len(select_cols)]
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
            import time
            return {
                "success": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 0,
                "error_message": f"FK preview error: {str(e)}"
            }

    def normalize_schema_names(self, dsn: str, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Нормализует имена таблиц для DuckDB: извлекает схему из DSN поддерживая разные форматы."""
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # Извлекаем схему из DSN (поддерживаем DuckDB-специфичные форматы)
        try:
            schema_arg = self.parse_schema_from_dsn(dsn)
        except Exception:
            schema_arg = None
        
        # DuckDB использует main как схему по умолчанию
        default_schema = schema_arg or "main"
        
        for table_name, columns in db_schema.items():
            if "." not in table_name:
                # Добавляем схему если её нет
                normalized_name = f"{default_schema}.{table_name}"
            else:
                # Уже квалифицированное имя
                normalized_name = table_name
            normalized[normalized_name] = columns
        
        return normalized

    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """Специализированная реализация для DuckDB."""
        try:
            _, schema = self.split_connection_dsn_and_schema(dsn)
            return schema
        except Exception:
            return None

    def get_default_schema(self) -> str:
        """DuckDB использует схему 'main' по умолчанию."""
        return "main"

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для DuckDB."""
        errors = []
        warnings = []
        clean_dsn, _ = self.split_connection_dsn_and_schema(dsn)
        parsed_dsn = urlparse(clean_dsn)
        
        if not parsed_dsn.path:
            errors.append("DuckDB DSN должен содержать путь к файлу БД")
        else:
            path = parsed_dsn.path
            if path != ":memory:" and not path.endswith('.duckdb'):
                warnings.append("DuckDB файл обычно имеет расширение .duckdb")
                
        return errors, warnings

    def quote_identifier(self, identifier: str) -> str:
        """DuckDB использует двойные кавычки для квотирования."""
        if not identifier:
            return identifier
        
        # DuckDB специфика: убираем префикс main. так как он не поддерживается
        if identifier.startswith("main."):
            identifier = identifier[5:]  # Убираем "main."

        return super().quote_identifier(identifier)

    def build_select_all(self, table_name: str, limit: int) -> str:
        """DuckDB SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"
