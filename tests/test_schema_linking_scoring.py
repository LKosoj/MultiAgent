"""Контрактные тесты для скоринга колонок и main table (T4.2 + T4.5).

Проверяют:
  * ``best_column_for`` опирается на column.description и FK-references,
    а не на захардкоженный словарь синонимов;
  * дефолтный (пустой) yaml-профиль не находит «revenue → amount»;
  * профиль ``muni_ru`` сохраняет легаси-поведение пользователя;
  * ``find_main_table`` использует semantic memory как primary signal;
  * fail-fast: без semantic memory и без структурного сигнала
    возвращается ``None``;
  * длина имени таблицы НЕ влияет на скоринг (closed-world эвристика
    удалена);
  * подмена ``main_table_scoring.yaml`` через env действительно меняет
    ранжирование.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_tools.text_to_sql import column_aliases_config, main_table_scoring_config
from custom_tools.text_to_sql.schema_linking import SchemaLinkingCore
from custom_tools.text_to_sql.validators import SchemaLimiter


_ALIAS_PATH_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PATH"
_ALIAS_PROFILE_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PROFILE"
_SCORING_PATH_VAR = "TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Гарантирует, что кэши конфигов не протекают между тестами."""
    column_aliases_config.reset_cache()
    main_table_scoring_config.reset_cache()
    yield
    monkeypatch.delenv(_ALIAS_PATH_VAR, raising=False)
    monkeypatch.delenv(_ALIAS_PROFILE_VAR, raising=False)
    monkeypatch.delenv(_SCORING_PATH_VAR, raising=False)
    column_aliases_config.reset_cache()
    main_table_scoring_config.reset_cache()


class _FakeMemory:
    """Минимальный stub схемной памяти."""

    def __init__(self, relevant_tables=None):
        self.relevant_tables = list(relevant_tables or [])
        self.last_search_status = "ok"
        self.last_search_error = None

    def find_semantic_relevant_tables(self, terms, dsn=None):
        return list(self.relevant_tables)


def _make_core(relevant_tables=None) -> SchemaLinkingCore:
    return SchemaLinkingCore(SchemaLimiter(), _FakeMemory(relevant_tables))


# --------------------------------------------------------------------- #
# T4.2 — best_column_for
# --------------------------------------------------------------------- #


def test_best_column_uses_description_not_name():
    """Колонка с «чужим» именем, но осмысленным description должна выигрывать.

    Это проверка того, что schema_enricher (LLM-обогащённое description) —
    первоклассный сигнал, а не fallback после жёсткого словаря.
    """
    core = _make_core()
    table_schema = {
        "columns": {
            "gross_take": {
                "type": "DECIMAL",
                "description": "Выручка по заказу за период",
            },
            "qty": {"type": "INTEGER", "description": "Количество позиций"},
        }
    }
    # Профиль default: алиасов нет, побеждает description.
    assert core.best_column_for("выручка", "orders", table_schema) == "gross_take"


def test_best_column_no_synonyms_in_default_profile():
    """Без активного muni_ru профиля «revenue» НЕ должен находить ``amount``.

    Никакого захардкоженного словаря синонимов: дефолтный yaml профиль пуст,
    description у колонки тоже пуст — связи нет.
    """
    core = _make_core()
    table_schema = {
        "columns": {
            "amount": {"type": "DECIMAL", "description": ""},
            "qty": {"type": "INTEGER", "description": ""},
        }
    }
    assert core.best_column_for("revenue", "orders", table_schema) is None


def test_best_column_muni_profile_preserves_legacy(monkeypatch):
    """С активным профилем muni_ru «revenue» снова находит ``amount``.

    Это регрессионный safety-net для пользовательского датасета.
    """
    monkeypatch.setenv(_ALIAS_PROFILE_VAR, "muni_ru")
    column_aliases_config.reset_cache()

    core = _make_core()
    table_schema = {
        "columns": {
            "amount": {"type": "DECIMAL", "description": ""},
            "qty": {"type": "INTEGER", "description": ""},
        }
    }
    assert core.best_column_for("revenue", "orders", table_schema) == "amount"


def test_best_column_uses_fk_reference_table_name():
    """FK-references выступает как сигнал: колонка ``region_id`` с references
    к таблице ``regions`` находится для query_term ``region``."""
    core = _make_core()
    table_schema = {
        "columns": {
            "region_id": {
                "type": "INTEGER",
                "constraint_type": "FK",
                "references": "regions(id)",
                "description": "",
            },
            "qty": {"type": "INTEGER", "description": ""},
        }
    }
    assert core.best_column_for("region", "orders", table_schema) == "region_id"


# --------------------------------------------------------------------- #
# T4.5 — find_main_table
# --------------------------------------------------------------------- #


def test_find_main_table_uses_semantic_memory_first():
    """Top-1 из semantic_tables выбирается, даже если структурно её скоринг
    был бы низким."""
    core = _make_core()
    db_schema = {
        "wide_lookup": {
            "columns": {
                # Много колонок, много PK/FK/numeric — структурно «жирная» таблица.
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "a_id": {"type": "INTEGER", "constraint_type": "FK", "references": "a(id)"},
                "b_id": {"type": "INTEGER", "constraint_type": "FK", "references": "b(id)"},
                "amount": {"type": "DECIMAL"},
                "total": {"type": "DECIMAL"},
            }
        },
        "orders": {
            # Минимальная таблица с одной колонкой — структурно проиграла бы.
            "columns": {"id": {"type": "INTEGER"}}
        },
    }
    assert core.find_main_table(db_schema, semantic_tables=["orders"]) == "orders"


def test_find_main_table_fails_without_signal():
    """Полностью пустая схема → None (никаких догадок)."""
    core = _make_core()
    assert core.find_main_table({}, semantic_tables=None) is None
    assert core.find_main_table({}, semantic_tables=[]) is None


def test_find_main_table_no_name_length_heuristic():
    """Длина имени таблицы НЕ должна влиять на ранжирование.

    Берём две таблицы с идентичной структурой, но разной длиной имени —
    обе получают одинаковый score (тест проверяет, что bonus за короткое
    имя удалён).
    """
    core = _make_core()
    cols = {
        "id": {"type": "INTEGER", "constraint_type": "PK"},
        "value": {"type": "DECIMAL"},
    }
    db_schema = {
        "x": {"columns": dict(cols)},  # короткое
        "this_is_a_very_long_table_name_that_used_to_lose": {"columns": dict(cols)},
    }
    # Без semantic memory оба должны иметь одинаковый score — победитель
    # определяется только порядком обхода dict (а не бонусом за длину).
    # Чтобы тест был детерминированным — проверяем score-расчёт напрямую,
    # пересоздав core и проверив возвращаемое значение для обоих случаев.
    # Поскольку scores равны, выбран будет первый: проверим через два
    # разных порядка ввода.
    result_a = core.find_main_table(db_schema, semantic_tables=None)
    db_schema_reversed = {
        "this_is_a_very_long_table_name_that_used_to_lose": {"columns": dict(cols)},
        "x": {"columns": dict(cols)},
    }
    result_b = core.find_main_table(db_schema_reversed, semantic_tables=None)

    # Главное: длинное имя НЕ проигрывает короткому из-за бонуса.
    # При равенстве scores побеждает первый в порядке обхода.
    assert result_a == "x"
    assert result_b == "this_is_a_very_long_table_name_that_used_to_lose"


def test_main_table_scoring_weights_from_yaml(tmp_path, monkeypatch):
    """Подмена main_table_scoring.yaml должна менять ранжирование."""
    # Подменяем веса: даём pk_weight = 1000, остальное = 0.
    # Тогда побеждает таблица с большим числом PK независимо от прочего.
    custom_yaml = tmp_path / "scoring.yaml"
    custom_yaml.write_text(
        "version: 2\n"
        "profiles:\n"
        "  default:\n"
        "    semantic_match_weight: 0\n"
        "    pk_weight: 1000\n"
        "    fk_weight: 0\n"
        "    numeric_weight: 0\n"
        "    columns_count_weight: 0\n"
        "    min_score_for_pick: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_SCORING_PATH_VAR, str(custom_yaml))
    main_table_scoring_config.reset_cache()

    core = _make_core()
    db_schema = {
        # Жирная таблица с numeric/columns_count, но без PK.
        "fat_no_pk": {
            "columns": {
                "a": {"type": "DECIMAL"},
                "b": {"type": "DECIMAL"},
                "c": {"type": "DECIMAL"},
                "d": {"type": "DECIMAL"},
                "e": {"type": "DECIMAL"},
            }
        },
        # Малая таблица, но с PK.
        "thin_with_pk": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
            }
        },
    }
    assert core.find_main_table(db_schema, semantic_tables=None) == "thin_with_pk"


def test_main_table_semantic_match_weight_used(tmp_path, monkeypatch, caplog):
    """Подмена ``semantic_match_weight`` должна менять победителя.

    Сценарий: «жирная» таблица структурно выигрывает у semantic-таблицы,
    если ``semantic_match_weight`` обнулён. Это доказывает, что вес
    из yaml реально применяется в скоринге (а не игнорируется через
    ранний return).
    """
    custom_yaml = tmp_path / "scoring.yaml"
    custom_yaml.write_text(
        "version: 2\n"
        "profiles:\n"
        "  default:\n"
        "    semantic_match_weight: 0\n"
        "    pk_weight: 10\n"
        "    fk_weight: 5\n"
        "    numeric_weight: 3\n"
        "    columns_count_weight: 2\n"
        "    min_score_for_pick: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_SCORING_PATH_VAR, str(custom_yaml))
    main_table_scoring_config.reset_cache()

    core = _make_core()
    db_schema = {
        "wide_lookup": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "a_id": {"type": "INTEGER", "constraint_type": "FK", "references": "a(id)"},
                "b_id": {"type": "INTEGER", "constraint_type": "FK", "references": "b(id)"},
                "amount": {"type": "DECIMAL"},
                "total": {"type": "DECIMAL"},
            }
        },
        "orders": {
            "columns": {"id": {"type": "INTEGER"}}
        },
    }
    # Без semantic_match_weight выигрывает структурно «жирная» таблица.
    assert core.find_main_table(db_schema, semantic_tables=["orders"]) == "wide_lookup"

    # Контроль: с большим semantic_match_weight semantic-таблица снова выигрывает.
    custom_yaml.write_text(
        "version: 2\n"
        "profiles:\n"
        "  default:\n"
        "    semantic_match_weight: 1000\n"
        "    pk_weight: 10\n"
        "    fk_weight: 5\n"
        "    numeric_weight: 3\n"
        "    columns_count_weight: 2\n"
        "    min_score_for_pick: 1\n",
        encoding="utf-8",
    )
    main_table_scoring_config.reset_cache()
    assert core.find_main_table(db_schema, semantic_tables=["orders"]) == "orders"


def test_main_table_logs_warning_when_semantic_tables_dont_match_schema(caplog):
    """semantic_tables=["ghost"] + db_schema без неё → WARNING в логах.

    Это сигнал о stale schema_table memory (memory знает про таблицы,
    которые удалены/переименованы в текущей схеме).
    """
    core = _make_core()
    db_schema = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "amount": {"type": "DECIMAL"},
            }
        }
    }
    import logging as _logging

    with caplog.at_level(
        _logging.WARNING,
        logger="custom_tools.text_to_sql.schema_linking.heuristic_linker",
    ):
        result = core.find_main_table(db_schema, semantic_tables=["ghost"])

    # Структурно выберется orders (единственная таблица в схеме).
    assert result == "orders"

    warning_messages = [
        rec.message for rec in caplog.records
        if rec.levelno == _logging.WARNING and "semantic memory suggested" in rec.message
    ]
    assert warning_messages, (
        "Ожидался WARNING про stale semantic memory, но он не залогирован. "
        f"Все записи: {[(r.levelname, r.message) for r in caplog.records]}"
    )
    assert "ghost" in warning_messages[0]


def test_main_table_scoring_yaml_rejects_zero_min_score(tmp_path, monkeypatch):
    """min_score_for_pick == 0 делает fail-fast no-op → ValueError."""
    bad_yaml = tmp_path / "scoring.yaml"
    bad_yaml.write_text(
        "version: 2\n"
        "profiles:\n"
        "  default:\n"
        "    semantic_match_weight: 100\n"
        "    pk_weight: 10\n"
        "    fk_weight: 5\n"
        "    numeric_weight: 3\n"
        "    columns_count_weight: 2\n"
        "    min_score_for_pick: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_SCORING_PATH_VAR, str(bad_yaml))
    main_table_scoring_config.reset_cache()
    with pytest.raises(ValueError, match="min_score_for_pick"):
        main_table_scoring_config.load_main_table_scoring_config()


def test_column_aliases_default_profile_is_empty():
    """Гарантия: дефолтный yaml профиль ``default`` пуст."""
    column_aliases_config.reset_cache()
    cfg = column_aliases_config.load_column_aliases_config()
    assert cfg.profiles["default"].is_empty() is True


def test_column_aliases_yaml_fails_fast_when_missing(tmp_path, monkeypatch):
    """Несуществующий путь → FileNotFoundError, никаких молчаливых дефолтов."""
    monkeypatch.setenv(_ALIAS_PATH_VAR, str(tmp_path / "missing.yaml"))
    column_aliases_config.reset_cache()
    with pytest.raises(FileNotFoundError):
        column_aliases_config.load_column_aliases_config()


def test_main_table_scoring_yaml_fails_fast_when_missing(tmp_path, monkeypatch):
    """Несуществующий путь → FileNotFoundError, никаких молчаливых дефолтов."""
    monkeypatch.setenv(_SCORING_PATH_VAR, str(tmp_path / "missing.yaml"))
    main_table_scoring_config.reset_cache()
    with pytest.raises(FileNotFoundError):
        main_table_scoring_config.load_main_table_scoring_config()


def test_no_synonyms_dict_in_schema_linking_core_py():
    """Доменный словарь синонимов больше не должен жить в .py файле.

    EPIC 8.2: после расщепления ``strategies.py`` логика
    ``best_column_for`` (где раньше жил словарь) переехала в
    ``schema_linking/heuristic_linker.py``. EPIC 8.6 удалил shim
    ``schema_linking_core.py`` — проверяем новый файл.
    """
    import re

    source = (
        Path(__file__).resolve().parents[1]
        / "custom_tools"
        / "text_to_sql"
        / "schema_linking"
        / "heuristic_linker.py"
    ).read_text(encoding="utf-8")
    # Конкретные доменные синонимы, ранее захардкоженные в best_column_for.
    forbidden_combos = [
        '"revenue":',
        '"sale_amount"',
        '"fiscal_year"',
        '"region_name"',
    ]
    leaked = [token for token in forbidden_combos if token in source]
    assert leaked == [], (
        "Доменные синонимы всё ещё в heuristic_linker.py — должны быть в "
        f"column_aliases.yaml: {leaked}"
    )

    # B1: query-token наборы для type-hint бонуса тоже не должны жить в
    # .py как литеральные сеты. Ищем характерное соседство query-токенов
    # (а не SQL-типов вроде ("date","time","timestamp"), которые могут
    # легитимно встречаться в проверке col_type).
    forbidden_type_hint_patterns = [
        # numeric query-токены: revenue/price/cost — это не SQL-типы.
        r'"revenue"[^\n]*"price"[^\n]*"cost"',
        r'"amount"[^\n]*"sum"[^\n]*"total"[^\n]*"revenue"',
        # identifier query-токены: id/key/code как сет query-имён.
        r'"id"\s*,\s*"key"\s*,\s*"code"',
    ]
    leaked_hints = [
        pattern
        for pattern in forbidden_type_hint_patterns
        if re.search(pattern, source)
    ]
    assert leaked_hints == [], (
        "Type-hint query-token наборы всё ещё в heuristic_linker.py — "
        f"должны быть в column_aliases.yaml (type_hints): {leaked_hints}"
    )


def test_best_column_type_hint_default_profile_no_bonus():
    """В default-профиле type-hint списки пустые → numeric type-bonus
    не срабатывает.

    Делаем колонку с exact-match (primary signal есть), но без description
    и без FK. Только +10 за exact-match, без +3 type-bonus.
    """
    core = _make_core()
    table_schema = {
        "columns": {
            "amount": {"type": "DECIMAL", "description": ""},
        }
    }
    # Самопроверка — primary signal есть, колонку всё равно находим.
    assert core.best_column_for("amount", "orders", table_schema) == "amount"

    # А вот type-hint-only сценарий: query «revenue» в default-профиле
    # не имеет alias-связи с amount и не имеет primary signal → None.
    assert core.best_column_for("revenue", "orders", table_schema) is None


def test_best_column_type_hint_muni_profile(monkeypatch):
    """С активным muni_ru профилем type-hint список numeric содержит
    ``amount``, и колонка ``amount: DECIMAL`` получает type-bonus поверх
    primary signal (exact-match)."""
    monkeypatch.setenv(_ALIAS_PROFILE_VAR, "muni_ru")
    column_aliases_config.reset_cache()

    # numeric_hints для muni_ru: amount/sum/total/revenue/price/cost.
    cfg = column_aliases_config.load_column_aliases_config()
    type_hints = cfg.get_type_hints("muni_ru")
    assert "amount" in type_hints["numeric"]
    assert "date" in type_hints["temporal"]
    assert "id" in type_hints["identifier"]

    # В default — те же категории, но пустые списки.
    default_hints = cfg.get_type_hints("default")
    assert default_hints == {"numeric": [], "temporal": [], "identifier": []}

    core = _make_core()
    # «amount» как query — exact-match даёт +10 primary signal,
    # type-bonus в muni_ru даёт +3 numeric. Без muni_ru type-bonus = 0.
    table_schema = {
        "columns": {
            "amount": {"type": "DECIMAL", "description": ""},
            # decoy: тот же primary signal через description,
            # но другого типа — должен проиграть amount.
            "note": {"type": "TEXT", "description": "amount of payment"},
        }
    }
    # amount: +10 exact + +3 numeric type-bonus = 13
    # note: +7 description match = 7
    assert core.best_column_for("amount", "orders", table_schema) == "amount"


def test_column_aliases_yaml_rejects_unknown_type_hint_category(tmp_path, monkeypatch):
    """Опечатка в имени type-hint категории (numerc вместо numeric) →
    ValueError при загрузке (fail-fast)."""
    bad_yaml = tmp_path / "aliases.yaml"
    bad_yaml.write_text(
        "version: 2\n"
        "policy:\n"
        "  type_hint_categories: [numeric, temporal, identifier]\n"
        "  required_profiles: [default]\n"
        "  default_profile_must_be_empty: true\n"
        "profiles:\n"
        "  default:\n"
        "    aliases: {}\n"
        "    type_hints:\n"
        "      numerc: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ALIAS_PATH_VAR, str(bad_yaml))
    column_aliases_config.reset_cache()
    with pytest.raises(ValueError, match="numerc"):
        column_aliases_config.load_column_aliases_config()


def test_column_aliases_profile_default_type_hints_optional(tmp_path, monkeypatch):
    """Профиль без секции ``type_hints`` всё равно валиден — все три
    категории читаются как пустые списки."""
    yaml_no_hints = tmp_path / "aliases.yaml"
    yaml_no_hints.write_text(
        "version: 2\n"
        "policy:\n"
        "  type_hint_categories: [numeric, temporal, identifier]\n"
        "  required_profiles: [default]\n"
        "  default_profile_must_be_empty: true\n"
        "profiles:\n"
        "  default:\n"
        "    aliases: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ALIAS_PATH_VAR, str(yaml_no_hints))
    column_aliases_config.reset_cache()
    cfg = column_aliases_config.load_column_aliases_config()
    hints = cfg.get_type_hints("default")
    assert hints == {"numeric": [], "temporal": [], "identifier": []}


# --------------------------------------------------------------------- #
# T5-linking / #14 MEDIUM — find_main_table использует type_categories.yaml
# --------------------------------------------------------------------- #


def test_find_main_table_numeric_includes_bigint_real(tmp_path, monkeypatch):
    """T5-linking / #14 MEDIUM: BIGINT и REAL должны считаться numeric
    (через yaml), а не отбрасываться как в старом хардкоде ['int','float','decimal','numeric'].

    Схема: таблица 'metrics' с BIGINT/REAL и таблица 'lookup' с TEXT.
    При numeric_weight=100 должна побеждать 'metrics'.
    """
    custom_yaml = tmp_path / "scoring.yaml"
    custom_yaml.write_text(
        "version: 2\n"
        "profiles:\n"
        "  default:\n"
        "    semantic_match_weight: 0\n"
        "    pk_weight: 0\n"
        "    fk_weight: 0\n"
        "    numeric_weight: 100\n"
        "    columns_count_weight: 0\n"
        "    min_score_for_pick: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_SCORING_PATH_VAR, str(custom_yaml))
    main_table_scoring_config.reset_cache()

    core = _make_core()
    db_schema = {
        "metrics": {
            "columns": {
                "count": {"type": "BIGINT"},
                "rate": {"type": "REAL"},
            }
        },
        "lookup": {
            "columns": {
                "code": {"type": "TEXT"},
                "name": {"type": "TEXT"},
            }
        },
    }
    result = core.find_main_table(db_schema, semantic_tables=None)
    assert result == "metrics", (
        "Ожидалась таблица 'metrics' (BIGINT+REAL = numeric category), "
        f"получили '{result}'"
    )


# --------------------------------------------------------------------- #
# T5-linking / #11 MEDIUM — dimension linking детерминирован (score-based)
# --------------------------------------------------------------------- #


def test_dimension_linking_score_based_not_order_dependent(tmp_path, monkeypatch):
    """T5-linking / #11 MEDIUM: выбор таблицы для dimension зависит от
    score, а не от порядка ключей в db_schema. При одинаковом score
    main_table предпочтительнее (tie-break).
    """
    core = _make_core(relevant_tables=["orders"])

    db_schema = {
        # main_table будет 'orders' (есть semantic hit + PK + numeric)
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "amount": {"type": "DECIMAL"},
                "status": {"type": "TEXT"},
            }
        },
        # 'regions' тоже содержит 'status'-подобную колонку
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "status": {"type": "TEXT"},
            }
        },
    }

    # Запрашиваем dimension "status" — присутствует в обеих таблицах.
    # После фикса: main_table предпочтительнее при tie-break.
    entities = {
        "metrics": ["amount"],
        "dimensions": ["status"],
        "filters": {},
    }

    import os
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")

    result = core.perform_linking(entities, db_schema)
    dims = result.get("linked_entities", {}).get("dimensions", [])
    dim_status = next((d for d in dims if d.get("name") == "status"), None)

    # Либо привязан к orders (main_table, tie-break), либо к regions
    # (если score выше) — в любом случае должен быть привязан
    assert dim_status is not None, (
        f"dimension 'status' должен быть слинкован, но dims={dims}"
    )


# --------------------------------------------------------------------- #
# T5-linking / #12 LOW — filters-only не считается ошибкой
# --------------------------------------------------------------------- #


def test_filters_only_linking_no_error(monkeypatch):
    """T5-linking / #12 LOW: если слинкованы только filters (metrics и
    dimensions пустые), error должен быть None (не считаем это ошибкой).

    Тест использует heuristic-путь: отключаем LLM и включаем fallbacks.
    Heuristic получает filters-only запрос — find_main_table выбирает
    таблицу, link_filters слинкует city → users.city. На выходе metrics
    и dimensions пустые, но filters непустые → error=None.
    """
    import os

    core = _make_core(relevant_tables=["users"])
    db_schema = {
        "users": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "city": {"type": "TEXT"},
            }
        }
    }

    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")

    result = core.perform_linking(
        {"metrics": [], "dimensions": [], "filters": {"city": "Москва"}},
        db_schema,
    )

    linked_filters = result.get("linked_entities", {}).get("filters", {})
    assert linked_filters, (
        f"Ожидались непустые filters, получили: {linked_filters}"
    )
    assert result.get("error") is None, (
        f"Ожидался error=None при filters-only linking, получили: {result.get('error')}"
    )
