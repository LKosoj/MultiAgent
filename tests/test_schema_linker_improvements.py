"""
Тест для проверки улучшений Schema Linking модуля.
"""

import pytest
from custom_tools.text_to_sql.schema_linker import (
    is_pk, is_fk, is_not_null, get_type, normalize_constraint_type,
    SchemaLinker
)
from custom_tools.text_to_sql.validators import SchemaLimiter


class TestSchemaLinkerHelpers:
    """Тесты вспомогательных функций для метаданных колонок."""
    
    def test_is_pk(self):
        """Тест проверки первичного ключа."""
        assert is_pk({"constraint_type": "PK"}) == True
        assert is_pk({"constraint_type": "PRIMARY KEY"}) == True
        assert is_pk({"is_primary_key": True}) == True
        assert is_pk({"is_primary_key": "true"}) == True
        assert is_pk({"is_primary_key": "1"}) == True
        assert is_pk({"constraint_type": "FK"}) == False
        assert is_pk({"is_primary_key": False}) == False
        assert is_pk("not_dict") == False
        assert is_pk({}) == False
    
    def test_is_fk(self):
        """Тест проверки внешнего ключа."""
        assert is_fk({"constraint_type": "FK"}) == True
        assert is_fk({"constraint_type": "FOREIGN KEY"}) == True
        assert is_fk({"references": "users.id"}) == True
        assert is_fk({"constraint_type": "PK"}) == False
        assert is_fk({"references": ""}) == False
        assert is_fk("not_dict") == False
        assert is_fk({}) == False
    
    def test_is_not_null(self):
        """Тест проверки NOT NULL ограничения."""
        assert is_not_null({"not_null": True}) == True
        assert is_not_null({"not_null": "true"}) == True
        assert is_not_null({"not_null": "1"}) == True
        assert is_not_null({"not_null": False}) == False
        assert is_not_null({"not_null": "false"}) == False
        assert is_not_null("not_dict") == False
        assert is_not_null({}) == False
    
    def test_get_type(self):
        """Тест получения типа колонки."""
        assert get_type({"type": "VARCHAR(255)"}) == "VARCHAR(255)"
        assert get_type({"type": "INT"}) == "INT"
        assert get_type({}) == ""
        assert get_type("not_dict") == ""
    
    def test_normalize_constraint_type(self):
        """Тест нормализации типа ограничения."""
        assert normalize_constraint_type("PRIMARY KEY") == "PK"
        assert normalize_constraint_type("pk") == "PK"
        assert normalize_constraint_type("FOREIGN KEY") == "FK"
        assert normalize_constraint_type("fk") == "FK"
        assert normalize_constraint_type("UNIQUE") == "UNIQUE"
        assert normalize_constraint_type("unknown") == ""
        assert normalize_constraint_type("") == ""


class TestSchemaLinker:
    """Тесты основного класса SchemaLinker."""
    
    @pytest.fixture
    def schema_linker(self):
        """Создает экземпляр SchemaLinker для тестов."""
        limiter = SchemaLimiter()
        return SchemaLinker.with_defaults(limiter)
    
    def test_linker_initialization(self, schema_linker):
        """Проверяет, что линкер корректно инициализируется."""
        assert hasattr(schema_linker, 'schema_limiter')
        assert schema_linker.repo_root is not None
    
    def test_type_compatibility_check(self, schema_linker):
        """Тест проверки совместимости типов для JOIN."""
        # Совместимые числовые типы
        assert schema_linker._check_type_compatibility("INT", "BIGINT") == True
        assert schema_linker._check_type_compatibility("DECIMAL", "NUMERIC") == True
        
        # Совместимые строковые типы
        assert schema_linker._check_type_compatibility("VARCHAR", "TEXT") == True
        assert schema_linker._check_type_compatibility("CHAR", "STRING") == True
        
        # Совместимые типы дат
        assert schema_linker._check_type_compatibility("DATE", "DATETIME") == True
        assert schema_linker._check_type_compatibility("TIMESTAMP", "TIME") == True
        
        # Несовместимые типы
        assert schema_linker._check_type_compatibility("INT", "VARCHAR") == False
        assert schema_linker._check_type_compatibility("DATE", "DECIMAL") == False

        # Пустые типы — fail-fast (см. EPIC 3.1): нет silent True, чтобы не маскировать
        # сломанную схему. Callers обязаны проверять наличие типов до вызова.
        with pytest.raises(ValueError):
            schema_linker._check_type_compatibility("", "INT")
        with pytest.raises(ValueError):
            schema_linker._check_type_compatibility("INT", "")
    
    def test_get_column_meta(self, schema_linker):
        """Тест получения метаданных колонки."""
        schema = {
            "users": {
                "id": {"type": "INT", "constraint_type": "PK"},
                "name": {"type": "VARCHAR(255)", "not_null": True}
            }
        }
        
        # Точное совпадение
        meta = schema_linker._get_column_meta("users", "id", schema)
        assert meta == {"type": "INT", "constraint_type": "PK"}
        
        # Совпадение без учета регистра
        meta = schema_linker._get_column_meta("USERS", "ID", schema)
        assert meta == {"type": "INT", "constraint_type": "PK"}
        
        # Несуществующая таблица
        meta = schema_linker._get_column_meta("nonexistent", "id", schema)
        assert meta is None
        
        # Несуществующая колонка
        meta = schema_linker._get_column_meta("users", "nonexistent", schema)
        assert meta is None
    
    def test_join_validation(self, schema_linker):
        """Тест валидации JOIN."""
        schema = {
            "users": {
                "id": {"type": "INT", "constraint_type": "PK"},
                "name": {"type": "VARCHAR(255)"}
            },
            "orders": {
                "id": {"type": "INT", "constraint_type": "PK"},
                "user_id": {"type": "INT", "constraint_type": "FK", "references": "users.id"}
            }
        }
        
        # Валидный FK->PK JOIN
        join = {
            "from_table": "orders",
            "from_column": "user_id",
            "to_table": "users",
            "to_column": "id"
        }
        result = schema_linker._is_join_valid_against_schema(join, schema)
        assert result["valid"] == True
        assert result["score"] > 100  # Бонус за FK->PK
        assert "FK->PK relationship" in result["notes"]
        
        # Невалидный JOIN (несуществующая таблица)
        join_invalid = {
            "from_table": "nonexistent",
            "from_column": "id",
            "to_table": "users",
            "to_column": "id"
        }
        result = schema_linker._is_join_valid_against_schema(join_invalid, schema)
        assert result["valid"] == False
        # T5-linking / #12 MEDIUM: шим теперь делегирует в package JoinValidator,
        # который возвращает "not found in schema" вместо "does not exist".
        # Контракт: reasons непустой и содержит информацию о проблеме.
        assert result["reasons"], f"Ожидался непустой список reasons, получили: {result}"
        combined = " ".join(result["reasons"]).lower()
        assert "nonexistent" in combined or "not found" in combined or "does not exist" in combined

    def test_llm_join_validation_rejects_when_type_missing(self, schema_linker):
        """EPIC 4 / 4.19: пропуск type-проверки при missing type был silent
        fallback. Теперь validate_llm_joins возвращает invalid (и не
        включает такой join в результат) — пайплайн дальше может попытать
        convention/bridge-инференцию (4.18/4.21)."""
        schema = {
            "orders": {
                "columns": {
                    "customer_id": {"type": "", "constraint_type": "FK", "references": "customers.id"},
                }
            },
            "customers": {
                "columns": {
                    "id": {"type": "VARCHAR", "constraint_type": "PK"},
                }
            },
        }
        joins = [{
            "from_table": "orders",
            "from_column": "customer_id",
            "to_table": "customers",
            "to_column": "id",
        }]

        validated = schema_linker.linking_core.validate_llm_joins(joins, schema)

        assert validated == []
    
    def test_parse_fk_reference(self, schema_linker):
        """Тест парсинга FK references."""
        # Формат table(column)
        table, column = schema_linker._parse_fk_reference("users(id)")
        assert table == "users"
        assert column == "id"
        
        # Формат table.column
        table, column = schema_linker._parse_fk_reference("users.id")
        assert table == "users"
        assert column == "id"
        
        # Формат schema.table.column
        table, column = schema_linker._parse_fk_reference("public.users.id")
        assert table == "public.users"
        assert column == "id"
        
        # Пустая строка
        table, column = schema_linker._parse_fk_reference("")
        assert table is None
        assert column is None
    
    def test_cache_info_includes_params(self, schema_linker):
        """Тест включения параметров окружения в кэш-ключ."""
        import os
        
        # Устанавливаем тестовые параметры окружения. DB_DSN обязателен после
        # Phase 6 fail-fast (см. doc/TEXT_TO_SQL_CHANGELOG.md): без него
        # SchemaCacheManager.prepare_cache_info бросает ValueError.
        os.environ["SCHEMA_LINKING_USE_LLM"] = "1"
        os.environ["SCHEMA_TABLE_CANDIDATES_K"] = "3"
        dsn = os.environ.setdefault("DB_DSN", "postgresql://test/test")
        
        entities = {"metrics": ["revenue"], "dimensions": ["date"]}
        schema = {
            "sales": {
                "id": {"type": "INT", "constraint_type": "PK"},
                "revenue": {"type": "DECIMAL"},
                "date": {"type": "DATE"}
            }
        }
        
        cache_info = schema_linker._prepare_cache_info(entities, schema, dsn=dsn)
        
        # Проверяем, что ключ включает основные поля
        assert "session_id" in cache_info
        assert "cache_kind" in cache_info
        assert "cache_key" in cache_info
        assert "schema_version" in cache_info
        
        # Ключ должен измениться при изменении параметров окружения
        cache_key_1 = cache_info["cache_key"]
        
        os.environ["SCHEMA_TABLE_CANDIDATES_K"] = "7"
        cache_info_2 = schema_linker._prepare_cache_info(entities, schema, dsn=dsn)
        cache_key_2 = cache_info_2["cache_key"]
        
        assert cache_key_1 != cache_key_2
        
        # Очищаем переменные окружения
        os.environ.pop("SCHEMA_LINKING_USE_LLM", None)
        os.environ.pop("SCHEMA_TABLE_CANDIDATES_K", None)


class TestDBPluginMethods:
    """Тесты новых методов в плагинах БД."""
    
    def test_base_plugin_methods(self):
        """Тест базовых методов плагина."""
        from db_plugins.base import BaseDBPlugin
        
        plugin = BaseDBPlugin()
        
        # Тест парсинга схемы из DSN
        assert plugin.parse_schema_from_dsn("postgres://user:pass@host:5432/db.schema") == "schema"
        assert plugin.parse_schema_from_dsn("postgres://user:pass@host:5432/db") is None
        assert plugin.parse_schema_from_dsn("sqlite:///path/file.db") is None
        
        # Тест квотирования
        assert plugin.quote_identifier("simple_name") == "simple_name"
        assert plugin.quote_identifier("name with spaces") == '"name with spaces"'
        assert plugin.quote_identifier('name"with"quotes') == '"name""with""quotes"'
        
        # Тест построения SELECT
        assert plugin.build_select_all("users", 10) == 'SELECT * FROM users LIMIT 10'
        assert plugin.build_select_all("table with spaces", 5) == 'SELECT * FROM "table with spaces" LIMIT 5'
    
    def test_duckdb_plugin_specialization(self):
        """Тест специализированных методов DuckDB плагина."""
        from db_plugins.duckdb import DuckDBPlugin
        
        plugin = DuckDBPlugin()
        
        # Тест парсинга схемы DuckDB
        assert plugin.parse_schema_from_dsn("duckdb:///path/file.db.analytics") == "analytics"
        assert plugin.parse_schema_from_dsn("duckdb:///path/file.duckdb.analytics") == "analytics"
        assert plugin.parse_schema_from_dsn("duckdb:///path/file.db/analytics") == "analytics"
        assert plugin.parse_schema_from_dsn("duckdb:///path/file.db") is None
        
        # Тест построения SELECT для DuckDB
        assert plugin.build_select_all("sales", 20) == 'SELECT * FROM sales LIMIT 20'
    
    def test_table_description_format(self):
        """Тест формата описания таблиц."""
        from custom_tools.text_to_sql.schema_linker import SchemaLinker
        from custom_tools.text_to_sql.validators import SchemaLimiter
        
        linker = SchemaLinker(SchemaLimiter())
        
        # Тестовая схема с разными типами колонок
        table_cols = {
            "id": {"type": "INT", "constraint_type": "PK", "description": "Уникальный идентификатор"},
            "user_id": {"type": "INT", "constraint_type": "FK", "references": "users.id", "description": "Ссылка на пользователя"},
            "name": {"type": "VARCHAR(255)", "description": "Название"},
            "amount": {"type": "DECIMAL(10,2)", "description": "Сумма"},
            "status": {"type": "VARCHAR(50)", "description": "Статус записи"},
            "created_at": {"type": "TIMESTAMP", "description": "Дата создания"}
        }
        
        description = linker._create_table_description("orders", table_cols)
        
        # Проверяем, что описание на русском языке
        assert "Таблица orders" in description
        assert "Первичные ключи:" in description
        assert "Внешние ключи:" in description
        assert "Колонки:" in description
        
        # Проверяем, что все колонки включены (не ограничены до 8)
        assert "name (VARCHAR(255)): Название" in description
        assert "amount (DECIMAL(10,2)): Сумма" in description
        assert "status (VARCHAR(50)): Статус записи" in description
        assert "created_at (TIMESTAMP): Дата создания" in description
        
        # Проверяем, что нет ограничения "plus N more columns"
        assert "plus" not in description
        assert "more columns" not in description
    
    def test_mysql_plugin_specialization(self):
        """Тест специализированных методов MySQL плагина."""
        from db_plugins.mysql import MySQLPlugin
        
        plugin = MySQLPlugin()
        
        # Тест квотирования MySQL (бэктики)
        assert plugin.quote_identifier("simple_name") == "simple_name"
        assert plugin.quote_identifier("name with spaces") == "`name with spaces`"
        assert plugin.quote_identifier("name`with`backticks") == "`name``with``backticks`"
        
        # Тест построения SELECT для MySQL
        assert plugin.build_select_all("users", 15) == 'SELECT * FROM users LIMIT 15'
    
    def test_sapiq_plugin_specialization(self):
        """Тест специализированных методов SAP IQ плагина."""
        from db_plugins.sapiq import SAPIQPlugin
        
        plugin = SAPIQPlugin()
        
        # Тест построения SELECT для SAP IQ (TOP вместо LIMIT)
        assert plugin.build_select_all("sales", 10) == 'SELECT TOP 10 * FROM sales'


class TestSchemaLinkerDeps:
    """Тесты DI-конструкции через :class:`SchemaLinkerDeps` (EPIC 8.4)."""

    def test_with_defaults_matches_legacy_init(self):
        """``with_defaults(limiter)`` строит тот же набор атрибутов, что
        и legacy ``SchemaLinker(limiter)``."""
        from custom_tools.text_to_sql.schema_linker import SchemaLinkerDeps

        limiter = SchemaLimiter()
        linker = SchemaLinker.with_defaults(limiter)

        assert linker.schema_limiter is limiter
        assert linker.loader is not None
        assert linker.enricher is not None
        assert linker.memory_manager is not None
        assert linker.cache_manager is not None
        assert linker.linking_core is not None
        assert linker.context_builder is not None
        # Linking core должен делить memory_manager с фасадом.
        assert linker.linking_core.memory_manager is linker.memory_manager
        assert isinstance(linker._deps, SchemaLinkerDeps)

    def test_construction_via_deps_injects_fakes(self):
        """SchemaLinker принимает явный :class:`SchemaLinkerDeps` —
        тестам не нужно monkeypatch'ить инстанс."""
        from custom_tools.text_to_sql.schema_linker import (
            SchemaLinker,
            SchemaLinkerDeps,
            _build_default_deps,
        )
        from pathlib import Path

        limiter = SchemaLimiter()
        repo_root = Path(__file__).resolve().parents[1]
        base = _build_default_deps(limiter, repo_root)

        class FakeCache:
            def __init__(self):
                self.calls = []

            def prepare_cache_info(self, entities, schema, dsn=None):
                self.calls.append("prepare")
                return {"key": "fake"}

            def load_from_cache(self, info):
                self.calls.append("load")
                return None

            def save_to_cache(self, info, result):
                self.calls.append("save")

        fake_cache = FakeCache()
        deps = SchemaLinkerDeps(
            schema_limiter=base.schema_limiter,
            loader=base.loader,
            enricher=base.enricher,
            memory_manager=base.memory_manager,
            cache_manager=fake_cache,
            linking_core=base.linking_core,
            context_builder=base.context_builder,
        )
        linker = SchemaLinker(deps)
        assert linker.cache_manager is fake_cache


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
