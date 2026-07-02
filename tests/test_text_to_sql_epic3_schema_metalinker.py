"""EPIC 3 / Schema-Meta + Linker — регрессионные тесты для правок:

* 3.1 — единый ``_check_type_compatibility`` с fail-fast для пустых типов.
* 3.2 — ``SchemaLinker._get_database_schema`` делегирует в ``SchemaLoader``.
* 3.3 / 3.21 — ``optimize_schema_for_storage`` lossless (round-trip).
* 3.33 — ``_matches_exactly`` трактует ``_`` как границу слова.

Файл вынесен отдельно, чтобы не смешиваться с историческими тестами
``test_schema_linker_improvements.py`` (часть из которых отражает legacy
поведение, например silent-True для пустых типов — что заменено fail-fast'ом).
"""
import pytest
from pathlib import Path

from custom_tools.text_to_sql.schema_linker import SchemaLinker
from custom_tools.text_to_sql.schema_metadata import (
    ColumnMetadataHelper,
    SchemaStatsHelper,
)
from custom_tools.text_to_sql.utils import (
    get_table_columns,
    get_table_description,
)
from custom_tools.text_to_sql.validators import SchemaLimiter
from custom_tools.text_to_sql import significance_config

_ENV_PATH_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PROFILE"

_MINIMAL_YAML = """
version: 2
profiles:
  default:
    high_priority_exact: []
    high_priority_compound: []
    medium_priority_patterns: []
    critical_description_keywords:
      - "важный"
"""


# ---------------------------------------------------------------------------
# 3.1 — type-compatibility unified fail-fast
# ---------------------------------------------------------------------------


def test_type_compatibility_unified_fail_fast_on_empty():
    """Единый метод проверки совместимости fail-fast'ит на пустых типах.

    Ранее ``SchemaLinker._check_type_compatibility`` молча возвращал True
    для ``""`` / ``None`` (silent fallback), а helper возвращал False.
    Теперь оба пути ведут в один и тот же ``ColumnMetadataHelper`` который
    поднимает ``ValueError`` — соответствует AGENTS.md "Никакого silent fallback".
    """
    linker = SchemaLinker(SchemaLimiter())

    # 1. Helper fail-fast'ит на пустых типах.
    with pytest.raises(ValueError):
        ColumnMetadataHelper.check_type_compatibility("", "INT")
    with pytest.raises(ValueError):
        ColumnMetadataHelper.check_type_compatibility("INT", "")
    with pytest.raises(ValueError):
        ColumnMetadataHelper.check_type_compatibility(None, "INT")  # type: ignore[arg-type]

    # 2. Shim в schema_linker делегирует БЕЗ собственного fallback.
    with pytest.raises(ValueError):
        linker._check_type_compatibility("", "INT")
    with pytest.raises(ValueError):
        linker._check_type_compatibility("INT", "")

    # 3. Sanity: непустые типы по-прежнему работают.
    assert linker._check_type_compatibility("INT", "BIGINT") is True
    assert linker._check_type_compatibility("INT", "VARCHAR") is False


# ---------------------------------------------------------------------------
# 3.2 — SchemaLinker делегирует в SchemaLoader (SoT)
# ---------------------------------------------------------------------------


def test_get_database_schema_single_source_of_truth(monkeypatch):
    """``SchemaLinker._get_database_schema`` делегирует в ``SchemaLoader``.

    Source of truth — ``SchemaLoader.get_database_schema``. Линкер не
    дублирует resolve-логику (sqlrag-файл / introspection), а только
    добавляет side-effects (индексация в память).
    """
    linker = SchemaLinker(SchemaLimiter())

    calls = []

    def fake_loader_get(schema_info, dsn=None):
        calls.append((schema_info, dsn))
        return {"public.users": {"columns": {"id": {"type": "INT"}}}}

    monkeypatch.setattr(linker.loader, "get_database_schema", fake_loader_get)

    # 1. С in-memory schema_info → loader зовётся ровно один раз, БЕЗ
    # enricher (он применяется только при introspection-пути внутри loader).
    schema_in = {"public.users": {"columns": {"id": {"type": "INT"}}}}
    out = linker._get_database_schema(schema_in)
    assert out == {"public.users": {"columns": {"id": {"type": "INT"}}}}
    assert len(calls) == 1
    assert calls[0][0] == schema_in
    assert calls[0][1] is None

    # 2. Пустой schema_info → loader снова, теперь с явно переданным DSN.
    calls.clear()
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@localhost:5432/db.test")
    monkeypatch.setattr(
        linker.memory_manager,
        "ensure_schema_indexed_in_memory",
        lambda dsn, schema: False,
    )
    linker._get_database_schema({}, dsn="postgresql://u:p@localhost:5432/db.test")
    assert len(calls) == 1
    assert calls[0][0] == {}
    assert calls[0][1] == "postgresql://u:p@localhost:5432/db.test"


def test_schema_loader_does_not_fallback_to_db_dsn_env(monkeypatch):
    from custom_tools.text_to_sql.schema_loader import SchemaLoader

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/env.db")
    loader = SchemaLoader(Path(__file__).resolve().parents[1])

    with pytest.raises(RuntimeError, match="DSN is required"):
        loader.get_database_schema({})


# ---------------------------------------------------------------------------
# 3.3 / 3.21 — lossless optimize_schema_for_storage
# ---------------------------------------------------------------------------


def test_optimize_schema_lossless_round_trip():
    """``optimize_schema_for_storage`` lossless: сохраняет ВСЕ поля.

    Ранее функция удаляла ``not_null=False``, ``constraint_type=""``,
    ``references=""`` как "дефолты" — что было indistinguishable от
    "поле не задано". Теперь round-trip ``optimize → restore`` идемпотентен.
    """
    original = {
        "orders": {
            "description": "Заказы",
            "columns": {
                "id": {
                    "type": "INT",
                    "constraint_type": "PK",
                    "references": "",
                    "not_null": True,
                },
                "amount": {
                    "type": "DECIMAL",
                    "constraint_type": "",
                    "references": "",
                    "not_null": False,
                },
                "customer_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "customers.id",
                    "not_null": False,
                },
            },
        },
    }

    optimized = SchemaStatsHelper.optimize_schema_for_storage(original)

    # 1. Все колонки сохранены.
    assert set(get_table_columns(optimized["orders"])) == {"id", "amount", "customer_id"}

    # 2. ВСЕ поля колонок сохранены (включая falsy ``not_null=False`` и
    # пустые строки ``constraint_type=""``/``references=""``).
    opt_cols = get_table_columns(optimized["orders"])
    for col_name in ("id", "amount", "customer_id"):
        assert opt_cols[col_name] == original["orders"]["columns"][col_name], (
            f"Lossy field for column {col_name}: "
            f"expected {original['orders']['columns'][col_name]!r}, "
            f"got {opt_cols[col_name]!r}"
        )

    # 3. Описание таблицы сохранено.
    assert get_table_description(optimized["orders"]) == "Заказы"
    assert "metadata" not in optimized["orders"]

    # 4. Round-trip идемпотентность: повторный optimize не меняет результат.
    twice = SchemaStatsHelper.optimize_schema_for_storage(optimized)
    assert twice == optimized


def test_optimize_schema_preserves_table_level_metadata_and_unknown_keys():
    """S-3: canonical table-level metadata/unknown keys survive autosave shape."""
    original = {
        "orders": {
            "description": "Заказы",
            "metadata": {"owner": "analytics"},
            "x-source": {"kind": "introspection"},
            "columns": {
                "id": {"type": "INT"},
            },
        },
    }

    optimized = SchemaStatsHelper.optimize_schema_for_storage(original)

    assert optimized["orders"]["metadata"] == {"owner": "analytics"}
    assert optimized["orders"]["x-source"] == {"kind": "introspection"}
    assert optimized["orders"]["columns"] == {"id": {"type": "INT"}}
    assert SchemaStatsHelper.optimize_schema_for_storage(optimized) == optimized


# ---------------------------------------------------------------------------
# 3.33 — _matches_exactly: underscore as word boundary
# ---------------------------------------------------------------------------


def test_matches_exactly_word_boundary_underscore():
    """``_matches_exactly`` трактует ``_`` как границу слова.

    ``user`` должно матчиться в ``user_id`` / ``id_user`` / ``user.id``,
    но НЕ в ``superuser`` / ``username`` (где ``_`` отсутствует и слово
    продолжается alphanumeric-символом).
    """
    match = ColumnMetadataHelper._matches_exactly

    # Положительные случаи — ``_`` или иной разделитель.
    assert match("user", "user") is True  # точное совпадение
    assert match("user_id", "user") is True
    assert match("id_user", "user") is True
    assert match("user.id", "user") is True
    assert match("public.user", "user") is True
    assert match("a_user_b", "user") is True

    # Негативные случаи — alphanumeric-продолжение слова.
    assert match("superuser", "user") is False
    assert match("username", "user") is False
    assert match("users", "user") is False
    assert match("useragent", "user") is False


# ---------------------------------------------------------------------------
# T1 — description=None краш и кириллическая граница слова
# ---------------------------------------------------------------------------


def test_is_semantic_significant_column_description_none_no_crash(
    tmp_path, monkeypatch
):
    """``is_semantic_significant_column`` не падает с AttributeError при description=None.

    Фикс T1(a): ``col_info.get('description') or ''`` вместо
    ``col_info.get('description', '')``, чтобы явный None не приводил к
    ``None.lower()`` → AttributeError.
    """
    cfg_file = tmp_path / "significance.yaml"
    cfg_file.write_text(_MINIMAL_YAML, encoding="utf-8")
    monkeypatch.setenv(_ENV_PATH_VAR, str(cfg_file))
    significance_config.reset_cache()

    result = ColumnMetadataHelper.is_semantic_significant_column(
        "foobar", {"description": None}
    )
    assert result is False


def test_is_semantic_significant_column_description_none_vs_missing(
    tmp_path, monkeypatch
):
    """description=None и отсутствующий description дают одинаковый результат."""
    cfg_file = tmp_path / "significance.yaml"
    cfg_file.write_text(_MINIMAL_YAML, encoding="utf-8")
    monkeypatch.setenv(_ENV_PATH_VAR, str(cfg_file))
    significance_config.reset_cache()

    result_none = ColumnMetadataHelper.is_semantic_significant_column(
        "foobar", {"description": None}
    )
    result_missing = ColumnMetadataHelper.is_semantic_significant_column(
        "foobar", {}
    )
    assert result_none == result_missing


def test_matches_exactly_cyrillic_adjacent_does_not_match():
    """Кириллический символ вплотную к паттерну → не матчится.

    Фикс T1(b): lookaround расширен диапазоном ``Ѐ-ӿ``, чтобы
    ``значениеvalue`` не давало ложный матч по ``value``.
    """
    match = ColumnMetadataHelper._matches_exactly

    # Кириллица непосредственно предшествует паттерну.
    assert match("значениеvalue", "value") is False
    # Кириллица непосредственно следует за паттерном.
    assert match("valueзначение", "value") is False


def test_matches_exactly_ascii_boundary_preserved():
    """ASCII-границы после добавления кириллического диапазона не сломаны."""
    match = ColumnMetadataHelper._matches_exactly

    assert match("user", "user") is True      # точное совпадение
    assert match("user_id", "user") is True   # _ — разделитель
    assert match("superuser", "user") is False
    assert match("username", "user") is False
    assert match("users", "user") is False
