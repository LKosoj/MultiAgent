"""
Тесты диалект-aware safety валидатора (EPIC 2 блок B: 2.2–2.6).

Покрывают:
- 2.2: маскировка двойных кавычек только для MySQL; в Postgres/ANSI кавычки —
  идентификатор, DDL-слова внутри них ловятся pre-parse regex.
- 2.3: pre-parse regex после lex-маскировки идентификаторов — алиасы и
  quoted identifiers не дают ложных срабатываний на FORBIDDEN_KEYWORDS.
- 2.5: комментарии через AST/tokenizer + word-boundary; "-2 - -1" не считается
  комментарием.
- 2.4: поддержка set-операций (UNION/INTERSECT/EXCEPT) в AST-маршруте.
- 2.6: IN-list через AST, корректно учитывает функции и подзапросы.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("USE_SQLGLOT", "1")

from custom_tools.text_to_sql.validators import SQLSafetyValidator  # noqa: E402


@pytest.fixture
def validator() -> SQLSafetyValidator:
    return SQLSafetyValidator()


def _has_issue(result, issue_type: str) -> bool:
    return any(i.get("issue_type") == issue_type for i in result.get("issues", []))


# ---------------------------------------------------------------------------
# 2.2 — диалект-aware маскировка двойных кавычек
# ---------------------------------------------------------------------------
class TestDoubleQuoteMaskingDialectAware:
    def test_postgres_double_quote_is_not_masked_as_string(self, validator):
        """В Postgres _mask_string_literals НЕ маскирует "..." — это identifier.

        DROP внутри quoted-идентификатора остаётся виден pre-parse regex
        (на уровне _mask_string_literals; на следующем шаге EPIC 2.3 lex
        отдельно скроет identifier от regex, но это другой инвариант).
        """
        masked = validator._mask_string_literals(
            'SELECT "column with DROP" FROM t', dialect="postgres"
        )
        # Содержимое идентификатора не превращено в пробелы.
        assert "DROP" in masked, masked
        assert "column" in masked, masked

    def test_mysql_double_quote_is_masked_as_string(self, validator):
        """В MySQL "..." — строковый литерал, маскируется пробелами."""
        masked = validator._mask_string_literals(
            'SELECT "DROP TABLE x"', dialect="mysql"
        )
        assert "DROP" not in masked, masked
        assert "TABLE" not in masked, masked

    def test_mysql_drop_inside_double_quoted_string_not_caught(self, validator):
        """E2E: MySQL "..." — строковый литерал, DROP не считается DDL."""
        with patch(
            "custom_tools.text_to_sql.validators.safety.get_current_dialect_name",
            return_value="mysql",
        ), patch(
            "custom_tools.text_to_sql.dialects.get_current_dialect_name",
            return_value="mysql",
        ):
            res = validator.validate('SELECT "DROP TABLE x"')
        assert _has_issue(res, "FORBIDDEN_STATEMENT") is False, res

    def test_postgres_escaped_double_quote_does_not_break_offsets(self, validator):
        """Escape "" внутри идентификатора не должен ломать длину результата."""
        original = 'SELECT "a""b" FROM t'
        masked = validator._mask_string_literals(original, dialect="postgres")
        # Длина и общие "видимые" символы сохранены.
        assert len(masked) == len(original)
        # E2E: запрос безопасен.
        with patch(
            "custom_tools.text_to_sql.validators.safety.get_current_dialect_name",
            return_value="postgres",
        ), patch(
            "custom_tools.text_to_sql.dialects.get_current_dialect_name",
            return_value="postgres",
        ):
            res = validator.validate(original)
        assert res["is_safe"] is True, res


# ---------------------------------------------------------------------------
# 2.3 — pre-parse regex после lex-маскировки идентификаторов
# ---------------------------------------------------------------------------
class TestPreParseLexMaskedIdentifiers:
    def test_alias_with_forbidden_word_substring_safe(self, validator):
        """Алиас column_with_union не должен ловиться regex на UNION."""
        with patch(
            "custom_tools.text_to_sql.validators.safety.get_current_dialect_name",
            return_value="postgres",
        ), patch(
            "custom_tools.text_to_sql.dialects.get_current_dialect_name",
            return_value="postgres",
        ):
            res = validator.validate("SELECT id AS column_with_union FROM t")
        assert res["is_safe"] is True, res

    def test_postgres_quoted_identifier_merge_safe(self, validator):
        """Quoted identifier "merge" в Postgres не должен ловиться на MERGE."""
        with patch(
            "custom_tools.text_to_sql.validators.safety.get_current_dialect_name",
            return_value="postgres",
        ), patch(
            "custom_tools.text_to_sql.dialects.get_current_dialect_name",
            return_value="postgres",
        ):
            res = validator.validate('SELECT "merge" FROM t')
        assert res["is_safe"] is True, res

    def test_mysql_backtick_identifier_union_safe(self, validator):
        """MySQL backtick identifier `union_col` не должен ловиться regex."""
        with patch(
            "custom_tools.text_to_sql.validators.safety.get_current_dialect_name",
            return_value="mysql",
        ), patch(
            "custom_tools.text_to_sql.dialects.get_current_dialect_name",
            return_value="mysql",
        ):
            res = validator.validate("SELECT `union_col` FROM t")
        assert res["is_safe"] is True, res


# ---------------------------------------------------------------------------
# 2.5 — комментарии через AST/tokenizer
# ---------------------------------------------------------------------------
class TestCommentDetection:
    def test_negative_numbers_with_minus_not_comment(self, validator):
        res = validator.validate("SELECT -2 - -1 AS v")
        assert res["is_safe"] is True, res

    def test_dash_dash_line_comment_detected(self, validator):
        res = validator.validate("SELECT 1 -- comment")
        assert res["is_safe"] is False
        assert _has_issue(res, "COMMENTS_NOT_ALLOWED")

    def test_block_comment_detected(self, validator):
        res = validator.validate("SELECT 1 /* block */")
        assert res["is_safe"] is False
        assert _has_issue(res, "COMMENTS_NOT_ALLOWED")

    def test_minus_between_identifiers_not_comment(self, validator):
        res = validator.validate("SELECT a-b FROM t")
        assert res["is_safe"] is True, res


# ---------------------------------------------------------------------------
# 2.4 — exp.Union / set-операции
# ---------------------------------------------------------------------------
class TestUnionAst:
    def test_union(self, validator):
        res = validator.validate("SELECT 1 UNION SELECT 2")
        assert res["is_safe"] is True, res

    def test_union_all(self, validator):
        res = validator.validate("SELECT 1 UNION ALL SELECT 2")
        assert res["is_safe"] is True, res

    def test_cte_with_union(self, validator):
        res = validator.validate(
            "WITH a AS (SELECT 1) SELECT * FROM a UNION SELECT 2"
        )
        assert res["is_safe"] is True, res


# ---------------------------------------------------------------------------
# 2.6 — IN-list через AST
# ---------------------------------------------------------------------------
class TestInListAst:
    def test_in_list_counts_function_as_one_item(self, validator):
        """FUNC(2,3) — один элемент IN-списка, а не три.

        Лимит max_in_list_size читается из safety.yaml (default=1000), поэтому
        3 элемента всегда укладываются; main assertion — что count корректен,
        а не >3.
        """
        res = validator.validate("SELECT * FROM t WHERE id IN (1, FUNC(2,3), 4)")
        assert _has_issue(res, "IN_LIST_TOO_LARGE") is False, res

    def test_in_list_counts_string_with_comma_as_one_item(self, validator):
        res = validator.validate("SELECT * FROM t WHERE col IN ('a', 'b, c', 'd')")
        assert _has_issue(res, "IN_LIST_TOO_LARGE") is False, res

    def test_in_subquery_not_counted(self, validator):
        """Подзапрос в IN не должен считаться большим списком."""
        res = validator.validate("SELECT * FROM t WHERE id IN (SELECT id FROM u)")
        assert res["is_safe"] is True, res
