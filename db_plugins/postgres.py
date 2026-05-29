from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from .base import BaseDBPlugin

logger = logging.getLogger(__name__)


class PostgresPlugin(BaseDBPlugin):
    dialect = "postgres"
    dialect_label = "PostgreSQL"

    def connect(self, dsn: str):
        import psycopg  # type: ignore
        from psycopg import sql  # type: ignore
        from urllib.parse import parse_qsl, urlparse

        connection_dsn, explicit_schema = self.split_connection_dsn_and_schema(dsn)
        driver_dsn = self.strip_plugin_query_options(connection_dsn)
        u = urlparse(driver_dsn)
        connect_kwargs = {"autocommit": True}
        if u.hostname:
            connect_kwargs["host"] = u.hostname
        if u.port:
            connect_kwargs["port"] = u.port
        if u.path and u.path != "/":
            connect_kwargs["dbname"] = (u.path or "/").lstrip("/")
        user = self.decode_url_part(u.username)
        password = self.decode_url_part(u.password)
        if user is not None:
            connect_kwargs["user"] = user
        if password is not None:
            connect_kwargs["password"] = password
        for key, value in parse_qsl(u.query, keep_blank_values=True):
            connect_kwargs[key] = value

        conn = psycopg.connect(**connect_kwargs)
        try:
            with conn.cursor() as cur:
                if explicit_schema:
                    cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(explicit_schema)))
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
        except Exception as e:
            if self.read_only_fail_open_enabled(dsn):
                return conn
            try:
                conn.close()
            except Exception:
                pass
            raise RuntimeError("Failed to enable PostgreSQL read-only session") from e
        return conn

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def explain(self, conn, sql: str) -> Dict[str, Any]:
        with self._cursor(conn) as cur:
            try:
                # Используем параметризованный запрос для безопасности
                cur.execute("EXPLAIN (FORMAT JSON) " + sql)
                rows = cur.fetchall()
                plan_text = json.dumps(rows, ensure_ascii=False)
            except Exception:
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
            where_conditions.append("c.table_schema = %s")
            params.append(schema)
        else:
            where_conditions.append("c.table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast')")
        
        if table_name:
            where_conditions.append("c.table_name = %s")
            params.append(table_name)
        
        where_clause = "WHERE " + " AND ".join(where_conditions)
        
        # Единый запрос с динамическими условиями
        query = f"""
            SELECT 
                c.table_schema, 
                c.table_name, 
                c.column_name, 
                c.data_type,
                (SELECT pd.description
                 FROM pg_catalog.pg_description pd
                 JOIN pg_catalog.pg_class pc ON pc.oid = pd.objoid
                 JOIN pg_catalog.pg_namespace pn ON pn.oid = pc.relnamespace
                 JOIN pg_catalog.pg_attribute pa ON pa.attrelid = pc.oid AND pa.attnum = pd.objsubid
                 WHERE pn.nspname = c.table_schema AND pc.relname = c.table_name AND pa.attname = c.column_name
                ) AS column_comment,
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
                SELECT tc.table_schema, tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_catalog = kcu.constraint_catalog
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
            ) pk ON c.table_schema = pk.table_schema AND c.table_name = pk.table_name AND c.column_name = pk.column_name
            -- Foreign keys
            LEFT JOIN (
                SELECT 
                    kcu.table_schema, kcu.table_name, kcu.column_name,
                    ref_kcu.table_schema AS foreign_table_schema,
                    ref_kcu.table_name AS foreign_table_name,
                    ref_kcu.column_name AS foreign_column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_catalog = kcu.constraint_catalog
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                JOIN information_schema.referential_constraints rc
                  ON tc.constraint_catalog = rc.constraint_catalog
                 AND tc.constraint_schema = rc.constraint_schema
                 AND tc.constraint_name = rc.constraint_name
                JOIN information_schema.key_column_usage ref_kcu
                  ON ref_kcu.constraint_catalog = rc.unique_constraint_catalog
                 AND ref_kcu.constraint_schema = rc.unique_constraint_schema
                 AND ref_kcu.constraint_name = rc.unique_constraint_name
                 AND ref_kcu.ordinal_position = kcu.position_in_unique_constraint
                WHERE tc.constraint_type = 'FOREIGN KEY'
            ) fk ON c.table_schema = fk.table_schema AND c.table_name = fk.table_name AND c.column_name = fk.column_name
            -- Unique constraints
            LEFT JOIN (
                SELECT tc.table_schema, tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_catalog = kcu.constraint_catalog
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'UNIQUE'
            ) uc ON c.table_schema = uc.table_schema AND c.table_name = uc.table_name AND c.column_name = uc.column_name
            {where_clause}
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """

        # Формируем схему в новом формате
        schema_result: Dict[str, Dict[str, Any]] = {}
        with self._cursor(conn) as cur:
            cur.execute(query, params)

            for schema_name, table, col, dtype, comment, constraint_type, references in cur.fetchall():
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
                    "constraint_type": constraint_type or "",
                    "references": references or ""
                }

                schema_result[key]["columns"][col] = self.normalize_column_info(col_info)

        return schema_result

    def estimate_row_count(self, conn, table_name: str) -> int:
        """Estimate number of rows using PostgreSQL statistics."""
        # Разбираем schema.table если задано
        if '.' in table_name:
            schema_name, table_name_only = table_name.split('.', 1)
        else:
            schema_name = self.get_default_schema()
            table_name_only = table_name

        try:
            # Try pg_class statistics first (fastest) — фильтруем по namespace,
            # иначе при одинаковых именах таблиц в разных схемах вернётся случайная.
            sql = (
                "SELECT CAST(c.reltuples AS BIGINT) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE c.relname = %s AND n.nspname = %s"
            )
            with self._cursor(conn) as cur:
                cur.execute(sql, (table_name_only, schema_name))
                row = cur.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0])
        except Exception as e:
            logger.warning("PostgreSQL estimate_row_count pg_class failed for %s: %s", table_name, e)

        # Fallback to exact count for smaller tables
        try:
            quoted_table = self.quote_identifier(table_name)
            sql = f"SELECT COUNT(*) FROM {quoted_table}"
            with self._cursor(conn) as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return int(row[0]) if row else 1000000
        except Exception as e:
            logger.warning("PostgreSQL estimate_row_count COUNT(*) failed for %s: %s", table_name, e)

        return 1000000  # Conservative fallback

    def get_basic_column_stats(self, conn, table_name: str) -> Dict[str, Dict[str, Any]]:
        """Get column statistics from pg_stats."""
        stats = {}
        try:
            # Extract schema and table name
            if '.' in table_name:
                schema_name, table_name_only = table_name.split('.', 1)
            else:
                schema_name = 'public'
                table_name_only = table_name

            # Get stats from pg_stats
            sql = (
                "SELECT attname, null_frac, n_distinct, most_common_vals[1:3] as sample_vals "
                "FROM pg_stats WHERE schemaname = %s AND tablename = %s"
            )

            with self._cursor(conn) as cur:
                cur.execute(sql, (schema_name, table_name_only))
                rows = cur.fetchall()

            for row in rows:
                col_name = row[0]
                stats[col_name] = {
                    'null_frac': float(row[1] or 0),
                    'distinct_count': int(row[2] or 0),
                    'sample_values': [str(v)[:50] for v in (row[3] or []) if v is not None]
                }

        except Exception as e:
            logger.warning("PostgreSQL get_basic_column_stats failed for %s: %s", table_name, e)

        return stats

    def sample_rows_smart(self, conn, table_name: str, strategy: str, max_rows: int = 10) -> Dict[str, Any]:
        """Smart sampling for PostgreSQL."""
        quoted_table = self.quote_identifier(table_name)
        limit = int(max_rows)
        try:
            if strategy == 'small':
                sql = f"SELECT * FROM {quoted_table} ORDER BY random() LIMIT {limit}"
            elif strategy == 'medium':
                sql = f"SELECT * FROM {quoted_table} TABLESAMPLE SYSTEM (1) LIMIT {limit}"
            else:  # large
                sql = f"SELECT * FROM {quoted_table} TABLESAMPLE SYSTEM (0.1) LIMIT {limit}"

            return self.execute_select(conn, sql, row_limit=max_rows)

        except Exception as e:
            logger.warning("PostgreSQL sample_rows_smart failed for %s: %s", table_name, e)
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
        """Get FK preview for PostgreSQL."""
        # Получаем max_rows из переменной окружения если не задано
        if max_rows is None:
            import os
            max_rows = int(os.getenv("SCHEMA_FK_PREVIEW_ROWS", "2"))
            
        try:
            if '.' in table_name:
                table_schema, table_name_only = table_name.split('.', 1)
            else:
                table_schema = 'public'
                table_name_only = table_name

            # Parse reference table name
            if '.' in ref_table:
                ref_schema, ref_table_only = ref_table.split('.', 1)
            else:
                ref_schema = 'public'
                ref_table_only = ref_table

            with self._cursor(conn) as cur:
                # Resolve the exact referenced column from FK metadata unless caller supplied it.
                if ref_column is None:
                    fk_sql = (
                        "SELECT ref_kcu.column_name FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "ON tc.constraint_catalog = kcu.constraint_catalog "
                        "AND tc.constraint_schema = kcu.constraint_schema "
                        "AND tc.constraint_name = kcu.constraint_name "
                        "AND tc.table_schema = kcu.table_schema "
                        "AND tc.table_name = kcu.table_name "
                        "JOIN information_schema.referential_constraints rc "
                        "ON tc.constraint_catalog = rc.constraint_catalog "
                        "AND tc.constraint_schema = rc.constraint_schema "
                        "AND tc.constraint_name = rc.constraint_name "
                        "JOIN information_schema.key_column_usage ref_kcu "
                        "ON ref_kcu.constraint_catalog = rc.unique_constraint_catalog "
                        "AND ref_kcu.constraint_schema = rc.unique_constraint_schema "
                        "AND ref_kcu.constraint_name = rc.unique_constraint_name "
                        "AND ref_kcu.ordinal_position = kcu.position_in_unique_constraint "
                        "WHERE tc.constraint_type = 'FOREIGN KEY' "
                        "AND tc.table_schema = %s AND tc.table_name = %s "
                        "AND kcu.column_name = %s "
                        "AND ref_kcu.table_schema = %s AND ref_kcu.table_name = %s "
                        "ORDER BY kcu.ordinal_position LIMIT 1"
                    )
                    cur.execute(
                        fk_sql,
                        (table_schema, table_name_only, fk_column, ref_schema, ref_table_only),
                    )
                    fk_result = cur.fetchone()
                    if fk_result:
                        ref_column = fk_result[0]

                import os
                if ref_column is None:
                    allow_inferred = os.getenv(
                        "SCHEMA_FK_PREVIEW_ALLOW_INFERRED_REF_COLUMN",
                        "0",
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if not allow_inferred:
                        return {
                            "success": False,
                            "data": [],
                            "columns": [],
                            "rows_affected": 0,
                            "execution_time_ms": 0,
                            "error_message": (
                                "Referenced FK column is unknown. Pass ref_column from schema metadata "
                                "or set SCHEMA_FK_PREVIEW_ALLOW_INFERRED_REF_COLUMN=1 to allow inferred previews."
                            ),
                        }

                    pk_sql = (
                        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "ON tc.constraint_catalog = kcu.constraint_catalog "
                        "AND tc.constraint_schema = kcu.constraint_schema "
                        "AND tc.constraint_name = kcu.constraint_name "
                        "AND tc.table_schema = kcu.table_schema "
                        "AND tc.table_name = kcu.table_name "
                        "WHERE tc.table_schema = %s AND tc.table_name = %s "
                        "AND tc.constraint_type = 'PRIMARY KEY' ORDER BY kcu.ordinal_position LIMIT 1"
                    )
                    cur.execute(pk_sql, (ref_schema, ref_table_only))
                    pk_result = cur.fetchone()
                    if pk_result:
                        ref_column = pk_result[0]
                    else:
                        id_sql = (
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = %s AND table_name = %s AND column_name = 'id'"
                        )
                        cur.execute(id_sql, (ref_schema, ref_table_only))
                        id_result = cur.fetchone()
                        if id_result:
                            ref_column = 'id'
                        else:
                            first_col_sql = (
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = %s AND table_name = %s "
                                "ORDER BY ordinal_position LIMIT 1"
                            )
                            cur.execute(first_col_sql, (ref_schema, ref_table_only))
                            first_result = cur.fetchone()
                            ref_column = first_result[0] if first_result else fk_column

                # Get readable columns from reference table
                sample_limit = int(os.getenv("SCHEMA_COLUMN_SAMPLES", "3"))
                sql = (
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s "
                    "AND (LOWER(column_name) LIKE '%%name%%' "
                    "     OR LOWER(column_name) LIKE '%%title%%' "
                    "     OR LOWER(column_name) LIKE '%%description%%') "
                    f"ORDER BY ordinal_position LIMIT {int(sample_limit)}"
                )

                cur.execute(sql, (ref_schema, ref_table_only))
                readable_cols = [row[0] for row in cur.fetchall()]

                if not readable_cols:
                    # Fallback to first few columns
                    sql = (
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s "
                        "ORDER BY ordinal_position LIMIT 2"
                    )
                    cur.execute(sql, (ref_schema, ref_table_only))
                    readable_cols = [row[0] for row in cur.fetchall()]

                # Limit to 3 columns max
                readable_cols = readable_cols[:3]

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
        """Нормализует имена таблиц для PostgreSQL: извлекает схему из DSN и добавляет её к таблицам без схемы."""
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {}
        
        # Извлекаем схему из DSN
        try:
            schema_arg = self.parse_schema_from_dsn(dsn)
        except Exception:
            schema_arg = None
        
        # Если схема не определена в DSN, используем public как default
        default_schema = schema_arg or "public"
        
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
        """PostgreSQL SELECT * с LIMIT."""
        quoted_table = self.quote_identifier(table_name)
        return f"SELECT * FROM {quoted_table} LIMIT {self._normalize_row_limit(limit)}"

    def get_default_schema(self) -> str:
        """PostgreSQL использует схему 'public' по умолчанию."""
        return "public"

    def validate_dsn_specific(self, dsn: str, parsed_dsn) -> tuple[list[str], list[str]]:
        """Валидация DSN специфичная для PostgreSQL."""
        errors = []
        warnings = []
        
        if not parsed_dsn.hostname:
            errors.append("PostgreSQL DSN должен содержать hostname")
        if not parsed_dsn.path or parsed_dsn.path == "/":
            errors.append("PostgreSQL DSN должен содержать имя базы данных")
        if not parsed_dsn.port:
            warnings.append("Не указан порт, будет использован порт по умолчанию (5432)")
            
        return errors, warnings
