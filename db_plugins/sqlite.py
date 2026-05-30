from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional
from .base import BaseDBPlugin

logger = logging.getLogger(__name__)


class SQLitePlugin(BaseDBPlugin):
    dialect = "sqlite"
    dialect_label = "SQLite"

    def connect(self, dsn: str):
        # dsn формата: sqlite:///abs/path/to.db или file:/path?mode=ro
        from urllib.parse import parse_qsl, urlencode, urlparse

        fail_open = self.read_only_fail_open_enabled(dsn)
        driver_dsn = self.strip_plugin_query_options(dsn)
        if dsn.startswith("sqlite:///"):
            parsed = urlparse(driver_dsn)
            path = parsed.path
            if path in {":memory:", "/:memory:"}:
                return sqlite3.connect(":memory:")
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        elif dsn.startswith("file:"):
            parsed = urlparse(driver_dsn)
            if parsed.path in {":memory:", "/:memory:"}:
                return sqlite3.connect(":memory:")
            query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
            query = dict(query_pairs)
            mode = query.get("mode")
            if mode and mode != "ro" and not fail_open:
                raise RuntimeError("SQLite file DSN must use mode=ro unless read_only_fail_open=true is set")
            if fail_open:
                conn = sqlite3.connect(driver_dsn, uri=True)
            else:
                query["mode"] = "ro"
                readonly_uri = parsed._replace(query=urlencode(query)).geturl()
                conn = sqlite3.connect(readonly_uri, uri=True)
        else:
            # трактуем как путь
            conn = sqlite3.connect(f"file:{dsn}?mode=ro", uri=True)
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        # sql уже прошёл верификацию; EXPLAIN QUERY PLAN не поддерживает параметризацию команды.
        with self._cursor(conn) as cur:
            cur.execute("EXPLAIN QUERY PLAN " + sql)
            rows = cur.fetchall()
        plan_lines = [" | ".join(str(x) for x in r) for r in rows]
        plan_text = "\n".join(plan_lines)
        issues: List[Dict[str, Any]] = []
        scans = sum(1 for line in plan_lines if "SCAN" in line.upper())
        if scans > 0:
            issues.append({"issue_type": "FULL_SCAN", "description": f"Detected {scans} table scans."})
        estimated_cost = 100.0 * scans if scans else 10.0
        return {"plan": plan_text, "estimated_cost": estimated_cost, "rows_to_scan": None, "issues": issues}

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
        schema_result: Dict[str, Dict[str, Dict[str, str]]] = {}
        with self._cursor(conn) as cur:
            # Фильтруем таблицы по table_name если указан
            if table_name:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ? AND name NOT LIKE 'sqlite_%';", (table_name,))
            else:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            tables = [r[0] for r in cur.fetchall()]

            for table in tables:
                # PRAGMA не поддерживает параметризацию — квотируем идентификатор
                quoted_table_ident = self.quote_identifier(table)
                cur.execute(f"PRAGMA table_info({quoted_table_ident});")
                cols = cur.fetchall()

                # Информация о foreign keys
                try:
                    cur.execute(f"PRAGMA foreign_key_list({quoted_table_ident});")
                    fks = cur.fetchall()
                    fk_map = {fk[3]: f"{fk[2]}.{fk[4]}" for fk in fks}  # from_col -> to_table.to_col
                except Exception:
                    fk_map = {}

                schema_result[table] = {"description": "", "columns": {}}
                for c in cols:
                    col_name = c[1]
                    col_type = c[2]
                    not_null = c[3]
                    default_val = c[4]
                    is_pk = c[5]

                    # Определяем constraint type
                    constraint_type = ""
                    references = ""

                    if is_pk:
                        constraint_type = "PRIMARY KEY"
                    elif col_name in fk_map:
                        constraint_type = "FOREIGN KEY"
                        references = fk_map[col_name]

                    col_info = {
                        "type": col_type,
                        "description": "",
                        "not_null": str(bool(not_null)),
                        "default_value": str(default_val) if default_val is not None else "",
                        "constraint_type": constraint_type,
                        "references": references
                    }
                    schema_result[table]["columns"][col_name] = self.normalize_column_info(col_info)

        return schema_result
    
    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows in SQLite table using sqlite_stat1."""
        try:
            # Check if table is qualified
            if '.' in table_name:
                _, table_name_only = table_name.split('.', 1)
            else:
                table_name_only = table_name
            
            # Try to get estimate from sqlite_stat1 if it exists
            with self._cursor(conn) as cur:
                try:
                    cur.execute("SELECT stat FROM sqlite_stat1 WHERE tbl = ?", (table_name_only,))
                    row = cur.fetchone()
                    if row and row[0]:
                        # sqlite_stat1.stat contains space-separated values, first is row count
                        stats = row[0].split()
                        if stats and stats[0].isdigit():
                            return int(stats[0])
                except Exception:
                    # sqlite_stat1 might not exist
                    pass

                # Fallback to exact count for SQLite (usually fast)
                quoted_table = self.quote_identifier(table_name)
                cur.execute(f"SELECT COUNT(*) FROM {quoted_table}")
                row = cur.fetchone()
                return int(row[0]) if row else 1000000

        except Exception as e:
            logger.warning("SQLite estimate_row_count failed for %s: %s", table_name, e)

        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get basic column statistics for SQLite."""
        stats = {}
        try:
            # Check if table is qualified
            if '.' in table_name:
                _, table_name_only = table_name.split('.', 1)
            else:
                table_name_only = table_name
            
            # Get column info using PRAGMA
            with self._cursor(conn) as cur:
                quoted_table_only = self.quote_identifier(table_name_only)
                cur.execute(f"PRAGMA table_info({quoted_table_only})")
                columns_info = cur.fetchall()

                quoted_table = self.quote_identifier(table_name)
                for col_info in columns_info:
                    col_name = col_info[1]  # name
                    col_type = col_info[2]  # type
                    not_null = col_info[3]  # notnull
                    default_val = col_info[4]  # dflt_value

                    stats[col_name] = {
                        'type': col_type,
                        'not_null': bool(not_null),
                        'default': str(default_val) if default_val is not None else None,
                        'null_count': 0,
                        'distinct_count': 0,
                        'sample_values': []
                    }

                    # Get statistics for this column
                    try:
                        quoted_col = self.quote_identifier(col_name)
                        stats_sql = (
                            f"SELECT (SELECT COUNT(*) FROM {quoted_table} WHERE {quoted_col} IS NULL) as null_count, "
                            f"COUNT(DISTINCT {quoted_col}) as distinct_count "
                            f"FROM {quoted_table} WHERE {quoted_col} IS NOT NULL"
                        )
                        cur.execute(stats_sql)
                        result = cur.fetchone()
                        if result:
                            stats[col_name]['null_count'] = int(result[0] or 0)
                            stats[col_name]['distinct_count'] = int(result[1] or 0)

                        # Get sample values
                        import os
                        sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                        sample_sql = (
                            f"SELECT DISTINCT {quoted_col} FROM {quoted_table} "
                            f"WHERE {quoted_col} IS NOT NULL LIMIT {int(sample_limit)}"
                        )
                        cur.execute(sample_sql)
                        samples = cur.fetchall()
                        stats[col_name]['sample_values'] = [str(row[0])[:50] for row in samples if row[0] is not None]

                    except Exception:
                        # Skip problematic columns
                        continue

        except Exception as e:
            logger.warning("SQLite get_basic_column_stats failed for %s: %s", table_name, e)

        return stats

    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for SQLite."""
        quoted_table = self.quote_identifier(table_name)
        limit = int(max_rows)
        try:
            if strategy == 'small':
                sql = f"SELECT * FROM {quoted_table} ORDER BY RANDOM() LIMIT {limit}"
            elif strategy == 'medium':
                sql = f"SELECT * FROM {quoted_table} WHERE ABS(RANDOM()) % 100 < 1 LIMIT {limit}"
            else:  # large strategy
                sql = f"SELECT * FROM {quoted_table} WHERE ABS(RANDOM()) % 1000 < 1 LIMIT {limit}"

            return self.execute_select(conn, sql, row_limit=max_rows)

        except Exception as e:
            logger.warning("SQLite sample_rows_smart failed for %s: %s", table_name, e)
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
        """Get FK preview for SQLite."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            # Check if ref_table is qualified
            if '.' in ref_table:
                _, ref_table_only = ref_table.split('.', 1)
            else:
                ref_table_only = ref_table
            table_name_only = table_name.split('.', 1)[1] if '.' in table_name else table_name
            
            # Get columns from reference table
            with self._cursor(conn) as cur:
                quoted_ref_only = self.quote_identifier(ref_table_only)
                cur.execute(f"PRAGMA table_info({quoted_ref_only})")
                ref_columns = cur.fetchall()

                if ref_column is None:
                    quoted_table_only = self.quote_identifier(table_name_only)
                    cur.execute(f"PRAGMA foreign_key_list({quoted_table_only})")
                    for fk in cur.fetchall():
                        if fk[3] == fk_column and fk[2] == ref_table_only:
                            ref_column = fk[4]
                            break
                if ref_column is None:
                    return {
                        "success": False,
                        "data": [],
                        "columns": [],
                        "rows_affected": 0,
                        "execution_time_ms": 0,
                        "error_message": f"Referenced column for {table_name}.{fk_column} -> {ref_table} was not found"
                    }

                # Look for readable column names
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

                # If no readable columns found, use PK or first few columns
                if not readable_col_names:
                    if pk_col:
                        readable_col_names = [pk_col]
                    else:
                        readable_col_names = [col_info[1] for col_info in ref_columns[:2]]

                # Limit to 3 columns max
                readable_col_names = readable_col_names[:3]

                # Build JOIN query with proper quoting
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
        """Нормализует имена таблиц для SQLite: добавляет префикс main. если нет схемы."""
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {}
        for table_name, table_data in db_schema.items():
            # SQLite всегда использует main. как схему по умолчанию
            if "." not in table_name:
                normalized_name = f"main.{table_name}"
            else:
                normalized_name = table_name
            normalized[normalized_name] = table_data
        return normalized

    def parse_schema_from_dsn(self, dsn: str) -> Optional[str]:
        """SQLite usually has no explicit schema in DSN."""
        return None

    def get_default_schema(self) -> str:
        """SQLite использует схему 'main' по умолчанию."""
        return "main"

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для SQLite."""
        errors = []
        warnings = []
        
        if not parsed_dsn.path:
            errors.append("SQLite DSN должен содержать путь к файлу БД")
        else:
            path = parsed_dsn.path
            if not path.endswith(('.db', '.sqlite', '.sqlite3')):
                warnings.append("SQLite файл обычно имеет расширение .db, .sqlite или .sqlite3")
                
        return errors, warnings

    def quote_identifier(self, identifier: str) -> str:
        """SQLite использует двойные кавычки или квадратные скобки."""
        return super().quote_identifier(identifier)

    def build_select_all(self, table_name: str, limit: int) -> str:
        """SQLite SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"
