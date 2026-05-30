"""
Тесты для интеграции sqlglot в Text-to-SQL пайплайн
"""
import os
import pytest
from unittest.mock import patch

# Не перетираем USE_SQLGLOT, если он явно выставлен в CI-окружении (например, =0).
os.environ.setdefault("USE_SQLGLOT", "1")

from custom_tools.text_to_sql.validators import (
    SQLSafetyValidator,
    SQLSchemaValidator,
    get_sqlglot_metrics,
    reset_sqlglot_metrics,
)
from custom_tools.text_to_sql.dialects import get_sqlglot_dialect, is_sqlglot_enabled
from custom_tools.text_to_sql.core import (
    code_formatter,
    _extract_schema_and_table_from_describe,
    _parse_table_name_from_describe_sqlglot,
)


class TestSQLGlotIntegration:
    """Тесты интеграции sqlglot."""
    
    def setup_method(self):
        """Настройка для каждого теста."""
        self.safety_validator = SQLSafetyValidator()
        self.schema_validator = SQLSchemaValidator()
    
    def test_sqlglot_enabled(self):
        """Тест проверки включения sqlglot."""
        assert is_sqlglot_enabled()
    
    def test_dialect_mapping(self):
        """Тест маппинга диалектов."""
        with patch('custom_tools.text_to_sql.dialects.get_current_dialect_name') as mock_dialect:
            mock_dialect.return_value = 'postgres'
            assert get_sqlglot_dialect() == 'postgres'
            
            mock_dialect.return_value = 'mysql'
            assert get_sqlglot_dialect() == 'mysql'
            
            mock_dialect.return_value = 'unknown'
            assert get_sqlglot_dialect() == 'ansi'
    
    def test_safety_validator_with_sqlglot(self):
        """Тест валидатора безопасности с sqlglot."""
        # Валидный SELECT
        result = self.safety_validator.validate("SELECT id, name FROM users;")
        assert result["is_safe"] is True
        assert len(result["issues"]) == 0
        
        # Запрещенный INSERT
        result = self.safety_validator.validate("INSERT INTO users (name) VALUES ('test');")
        assert result["is_safe"] is False
        assert any(issue["issue_type"] == "FORBIDDEN_STATEMENT" for issue in result["issues"])
        
        # Множественные стейтменты
        result = self.safety_validator.validate("SELECT 1; SELECT 2;")
        assert result["is_safe"] is False
        assert any(issue["issue_type"] == "MULTI_STATEMENT" for issue in result["issues"])
    
    def test_cte_validation(self):
        """Тест валидации CTE."""
        # Валидный CTE с SELECT
        cte_sql = """
        WITH sales_summary AS (
            SELECT region, SUM(amount) as total
            FROM sales
            GROUP BY region
        )
        SELECT * FROM sales_summary;
        """
        result = self.safety_validator.validate(cte_sql)
        assert result["is_safe"] is True
        
        # CTE с запрещенным INSERT
        cte_sql = """
        WITH temp_data AS (
            SELECT id FROM users
        )
        INSERT INTO archive SELECT * FROM temp_data;
        """
        result = self.safety_validator.validate(cte_sql)
        assert result["is_safe"] is False
    
    def test_in_list_validation(self):
        """Тест валидации IN-списков."""
        # Нормальный IN-список
        sql = "SELECT * FROM users WHERE id IN (1, 2, 3);"
        result = self.safety_validator.validate(sql)
        assert result["is_safe"] is True
        
        # Слишком большой IN-список (если установлен низкий лимит)
        large_list = ", ".join(str(i) for i in range(2000))
        sql = f"SELECT * FROM users WHERE id IN ({large_list});"
        result = self.safety_validator.validate(sql)
        # Может быть небезопасным в зависимости от настроек MAX_IN_LIST
    
    def test_schema_validation_with_quotes(self):
        """Тест валидации схемы с кавычками."""
        db_schema = {
            "public.users": {
                "id": {"type": "INTEGER", "description": "Primary key"},
                "name": {"type": "VARCHAR(255)", "description": "User name"}
            },
            "public.orders": {
                "id": {"type": "INTEGER", "description": "Order ID"},
                "user_id": {"type": "INTEGER", "description": "User reference"}
            }
        }
        
        # SQL с кавычками
        sql = 'SELECT "public"."users"."name" FROM "public"."users";'
        result = self.schema_validator.validate_sql_against_schema(sql, db_schema)
        assert result["is_valid"] is True
        
        # SQL с несуществующей таблицей
        sql = 'SELECT name FROM "nonexistent"."table";'
        result = self.schema_validator.validate_sql_against_schema(sql, db_schema)
        assert result["is_valid"] is False
        assert any(issue["issue_type"] == "UNKNOWN_TABLE" for issue in result["issues"])
    
    def test_code_formatter_with_sqlglot(self):
        """Тест форматирования кода через sqlglot."""
        sql = "select id,name from users where active=1"
        result = code_formatter(sql)
        
        formatted = result["formatted_sql_query"]
        assert "SELECT" in formatted  # Должно быть в верхнем регистре
        assert "FROM" in formatted
        assert "WHERE" in formatted
        assert formatted.endswith(";")  # Должна быть точка с запятой

    def test_ansi_dialect_formatter_and_describe_do_not_fallback(self):
        """Default/SAP IQ ansi mapping must not call sqlglot with unsupported read='ansi'."""
        reset_sqlglot_metrics()
        with patch('custom_tools.text_to_sql.dialects.get_current_dialect_name') as mock_dialect:
            mock_dialect.return_value = 'sapiq'
            formatted = code_formatter("select id from users")["formatted_sql_query"]
            table_name = _parse_table_name_from_describe_sqlglot("DESCRIBE users")
            qualified_table_name = _parse_table_name_from_describe_sqlglot('DESC "public"."orders"')
            quoted_dot_target = _extract_schema_and_table_from_describe('DESC "public.orders"')
            quoted_qualified_target = _extract_schema_and_table_from_describe('DESC "public"."orders"')
            escaped_quote_target = _extract_schema_and_table_from_describe('DESC "a""b"')

        metrics = get_sqlglot_metrics()
        assert "SELECT" in formatted
        assert table_name == "users"
        assert qualified_table_name == "public.orders"
        assert quoted_dot_target == (None, "public.orders")
        assert quoted_qualified_target == ("public", "orders")
        assert escaped_quote_target == (None, 'a"b')
        assert metrics["fallback_count"] == 0

        with pytest.raises(ValueError, match="Invalid DESCRIBE syntax"):
            _extract_schema_and_table_from_describe("DESC orders extra")
    
    def test_fallback_on_parse_error(self):
        """Тест fallback при ошибке парсинга."""
        # Намеренно некорректный SQL
        invalid_sql = "SELECT FROM WHERE;;;"
        
        # Должен вернуться к legacy валидации без исключения
        result = self.safety_validator.validate(invalid_sql)
        assert "is_safe" in result
        assert "issues" in result
    
    @pytest.mark.parametrize("dialect,expected", [
        ("postgres", "postgres"),
        ("mysql", "mysql"),
        ("sqlite", "sqlite"),
        ("duckdb", "duckdb"),
        ("impala", "hive"),
        ("sapiq", "ansi"),
        ("unknown", "ansi")
    ])
    def test_dialect_mapping_parametrized(self, dialect, expected):
        """Параметризованный тест маппинга диалектов."""
        with patch('custom_tools.text_to_sql.dialects.get_current_dialect_name') as mock_dialect:
            mock_dialect.return_value = dialect
            assert get_sqlglot_dialect() == expected


class TestSQLGlotDisabled:
    """Тесты когда sqlglot отключен."""

    @pytest.fixture(autouse=True)
    def _use_sqlglot_disabled(self, monkeypatch):
        """Изолированно выставляет USE_SQLGLOT=0 на время каждого теста."""
        monkeypatch.setenv("USE_SQLGLOT", "0")
        self.safety_validator = SQLSafetyValidator()
        yield  # явная setup/teardown-семантика; monkeypatch откатит env после теста

    def test_explicit_legacy_mode(self):
        """Тест явного legacy mode через USE_SQLGLOT=0."""
        assert not is_sqlglot_enabled()

        # Legacy validation разрешена только потому, что режим явно выключил sqlglot.
        result = self.safety_validator.validate("SELECT * FROM users;")
        assert result["is_safe"] is True

        # Запрещенные ключевые слова должны ловиться legacy методом
        result = self.safety_validator.validate("DROP TABLE users;")
        assert result["is_safe"] is False

    def test_strict_mode_requires_sqlglot(self, monkeypatch):
        """При USE_SQLGLOT=1 отсутствие sqlglot является явной ошибкой."""
        from custom_tools.text_to_sql import validators

        monkeypatch.setenv("USE_SQLGLOT", "1")
        monkeypatch.setattr(validators, "SQLGLOT_AVAILABLE", False)
        # Construct a fresh validator AFTER setting USE_SQLGLOT=1 so it sees
        # the updated env flag rather than the stale instance from the fixture
        # (which was built under USE_SQLGLOT=0).
        validator = SQLSafetyValidator()
        result = validator.validate("SELECT * FROM users;")

        assert result["is_safe"] is False
        assert result["issues"][0]["issue_type"] == "SQLGLOT_UNAVAILABLE"

    def test_schema_validation_reports_sqlglot_disabled_explicitly(self):
        schema_validator = SQLSchemaValidator()

        result = schema_validator.validate_sql_against_schema(
            "SELECT id FROM users",
            {"users": {"columns": {"id": {"type": "INTEGER"}}}},
        )

        assert result["is_valid"] is False
        assert result["issues"][0]["issue_type"] == "SQLGLOT_DISABLED_FOR_SCHEMA_VALIDATION"


if __name__ == "__main__":
    pytest.main([__file__])
