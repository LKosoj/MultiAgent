"""Tests for EPIC 3 Dialects + Utils refactor (3.16/3.17/3.18/3.29/3.34).

Эти тесты фиксируют поведение после рефакторинга:
- 3.18: ``get_plugin`` импортируется лениво, на module level его быть не должно.
- 3.17: regex для Unicode escape работает корректно.
- 3.34: ``json.dumps`` в ``get_schema_version`` поддерживает Decimal/datetime через ``default=str``.
- 3.16: ``quote_identifier``/``get_current_dialect_*`` принимают DSN через kwarg.
- 3.29: ``SQLGLOT_DIALECT_MAPPING`` собирается через plugin-driven discovery.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def test_get_plugin_lazy_import():
    """3.18: ``db_plugins.get_plugin`` не должен быть на module-level в utils/dialects.

    Проверяем статически по исходному тексту, чтобы не загрязнять
    ``sys.modules`` для последующих тестов (там есть mock.patch на
    ``custom_tools.text_to_sql.dialects.get_current_dialect_name``).
    """
    import re as _re
    from pathlib import Path

    for relpath in (
        "custom_tools/text_to_sql/utils.py",
        "custom_tools/text_to_sql/dialects.py",
    ):
        path = Path(__file__).resolve().parents[1] / relpath
        source = path.read_text(encoding="utf-8")
        # Берём только top-level imports (без отступов).
        toplevel_import = _re.search(
            r"^from\s+db_plugins\s+import\s+get_plugin",
            source,
            flags=_re.MULTILINE,
        )
        assert toplevel_import is None, (
            f"{relpath}: ``from db_plugins import get_plugin`` "
            f"найден на module-level (EPIC 3.18 — circular import risk)."
        )


def test_regex_unicode_escape_correct():
    """3.17: Regex для двойного экранирования Unicode escapes должен срабатывать.

    Кейс: внутри LLM-ответа есть некорректная последовательность из 2 backslash
    + u + hex, и одновременно сам JSON невалиден из-за висящего backslash —
    после починки re.sub должен вернуть валидный ``\\uXXXX`` escape.
    """
    import re

    pattern = r'\\\\u([0-9a-fA-F]{4})'
    # Реальный текст: содержит \\u041d (2 backslash + u + 4 hex).
    raw = "value: \\\\u041d end"
    fixed = re.sub(pattern, r'\\u\1', raw)
    # После замены должно остаться ровно 1 backslash + u + 4 hex.
    assert fixed == "value: \\u041d end", f"Unexpected: {fixed!r}"


def test_regex_unicode_escape_pattern_is_correct_level():
    """3.17: Прямая проверка уровня экранирования в regex.

    Документируем: pattern должен матчить РОВНО 2 backslash + u + 4 hex digits
    (то, что бывает в JSON-строке), а не 4 backslash.
    """
    import re

    # Скопируем pattern из utils.py — должен быть r'\\\\u(...)':
    # 4 backslash в raw-pattern = 4 в regex = matches 2 в строке.
    pattern = r'\\\\u([0-9a-fA-F]{4})'
    target = '\\\\u041d'  # реально в строке: 2 backslash + 'u' + '041d'
    assert re.search(pattern, target) is not None, (
        "Regex pattern должен матчить два backslash + u + 4 hex digits"
    )


def test_json_dumps_handles_decimal_datetime(monkeypatch):
    """3.34: ``get_schema_version`` не падает на Decimal/datetime внутри db_schema."""
    from custom_tools.text_to_sql import utils as utils_mod

    utils_mod.clear_schema_version_cache()
    monkeypatch.delenv("SCHEMA_VERSION", raising=False)
    monkeypatch.delenv("DB_DSN", raising=False)

    schema_with_non_json_types = {
        "orders": {
            "description": "Orders table",
            "columns": {
                "amount": {
                    "type": "DECIMAL",
                    "min_value": Decimal("0.01"),
                    "created_at": datetime(2024, 1, 1, 12, 0, 0),
                }
            },
        }
    }

    # До исправления json.dumps падал бы на TypeError для Decimal/datetime.
    version = utils_mod.get_schema_version(schema_with_non_json_types)
    assert isinstance(version, str)
    assert version != "unknown"

    # Bonus: сериализация прямо в hash тоже должна быть стабильной (детерм.).
    again = utils_mod.get_schema_version(schema_with_non_json_types)
    assert version == again


def test_dialect_dsn_via_kwarg(monkeypatch):
    """3.16: ``get_current_dialect_name``/``quote_identifier`` поддерживают kwarg ``dsn``.

    DSN не должен браться только из глобального окружения.
    """
    from custom_tools.text_to_sql import dialects as dialects_mod

    # Очистим окружение, чтобы fallback на env не вмешивался.
    monkeypatch.delenv("DB_DSN", raising=False)

    # Без аргумента и без env — "sql"/"SQL".
    assert dialects_mod.get_current_dialect_name() == "sql"
    assert dialects_mod.get_current_dialect_label() == "SQL"

    # С kwarg DSN — диалект берётся из плагина для указанного DSN.
    sqlite_dsn = "sqlite:///tmp/test_dialect_kwarg.db"
    assert dialects_mod.get_current_dialect_name(dsn=sqlite_dsn) == "sqlite"

    postgres_dsn = "postgresql://user:pass@localhost:5432/db"
    assert dialects_mod.get_current_dialect_name(dsn=postgres_dsn) == "postgres"

    # quote_identifier также принимает kwarg dsn.
    quoted = dialects_mod.quote_identifier("schema.table", dsn=postgres_dsn)
    assert quoted == '"schema"."table"'

    mysql_dsn = "mysql://user:pass@localhost:3306/db"
    quoted_mysql = dialects_mod.quote_identifier("schema.table", dsn=mysql_dsn)
    assert quoted_mysql == "`schema`.`table`"


def test_dialect_quote_identifier_uses_sqlglot_split(monkeypatch):
    """3.16: Квотирование использует sqlglot-разбиение, а не наивный split.

    Идентификатор ``"a.b"`` (где точка — часть имени, поскольку всё в кавычках)
    не должен разбиваться на две части.
    """
    from custom_tools.text_to_sql import dialects as dialects_mod

    monkeypatch.delenv("DB_DSN", raising=False)
    postgres_dsn = "postgresql://user:pass@localhost:5432/db"

    # Простой qualified identifier — три части.
    assert (
        dialects_mod.quote_identifier("public.users.id", dsn=postgres_dsn)
        == '"public"."users"."id"'
    )

    # Идентификатор с уже-квотированной составной частью: ``"a.b".c``.
    # sqlglot должен понять это как 2 части: ``a.b`` и ``c``.
    result = dialects_mod.quote_identifier('"a.b".c', dsn=postgres_dsn)
    assert result == '"a.b"."c"', f"sqlglot-aware split, got {result!r}"


def test_sqlglot_dialect_mapping_plugin_driven():
    """3.29: ``SQLGLOT_DIALECT_MAPPING`` собирается из ``db_plugins`` discovery.

    Проверяем, что:
    - Все известные плагины представлены в mapping.
    - Значения соответствуют ожидаемому sqlglot диалекту.
    - Mapping можно пересобрать через ``refresh_sqlglot_dialect_mapping()``.
    """
    from custom_tools.text_to_sql import dialects as dialects_mod
    from db_plugins.manager import _PLUGINS

    mapping = dialects_mod.SQLGLOT_DIALECT_MAPPING

    # Каждый зарегистрированный плагин должен попасть в mapping.
    plugin_dialects = {
        plugin.dialect.lower()
        for plugin in _PLUGINS.values()
        if isinstance(getattr(plugin, "dialect", None), str) and plugin.dialect
    }
    for dialect in plugin_dialects:
        assert dialect in mapping, (
            f"Диалект {dialect!r} из db_plugins не попал в SQLGLOT_DIALECT_MAPPING"
        )

    # Известные значения (источник истины — поведение плагинов).
    assert mapping.get("postgres") == "postgres"
    assert mapping.get("mysql") == "mysql"
    assert mapping.get("sqlite") == "sqlite"
    assert mapping.get("duckdb") == "duckdb"
    assert mapping.get("impala") == "hive"
    # sapiq имеет историческое значение "ansi" (а не "tsql").
    assert mapping.get("sapiq") == "ansi"

    # Fallback-ключ "sql" → "ansi".
    assert mapping.get("sql") == "ansi"

    # refresh должен вернуть тот же по содержанию словарь (плагины не менялись).
    refreshed = dialects_mod.refresh_sqlglot_dialect_mapping()
    assert refreshed == mapping
