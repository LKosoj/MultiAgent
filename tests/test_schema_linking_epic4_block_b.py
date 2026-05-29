"""
Тесты для EPIC 4 / Block B — Schema linking fixes (4.12 — 4.26).

Каждый тест соответствует конкретной задаче из блока (см. AGENTS.md и
issue). Pre-existing failures других тестов EPIC 4 не дублируем — здесь
только новые контракты.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from custom_tools.text_to_sql import (
    column_aliases_config,
    llm_models_config,
    main_table_scoring_config,
    nlu_config,
)
from custom_tools.text_to_sql.schema_linking.join_validation import JoinValidator
from custom_tools.text_to_sql.schema_linking.resolution import _resolve_table_name
from custom_tools.text_to_sql.schema_linking import SchemaLinkingCore
from custom_tools.text_to_sql.validators import SchemaLimiter


_ALIAS_PATH_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PATH"
_ALIAS_PROFILE_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PROFILE"
_LLM_MODELS_PATH_VAR = "TEXT_TO_SQL_LLM_MODELS_PATH"
_LLM_MODELS_PROFILE_VAR = "TEXT_TO_SQL_LLM_MODELS_PROFILE"
_NLU_PROFILE_VAR = "TEXT_TO_SQL_NLU_PROFILE"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    column_aliases_config.reset_cache()
    main_table_scoring_config.reset_cache()
    llm_models_config.reset_cache()
    nlu_config.reset_cache()
    yield
    monkeypatch.delenv(_ALIAS_PATH_VAR, raising=False)
    monkeypatch.delenv(_ALIAS_PROFILE_VAR, raising=False)
    monkeypatch.delenv(_LLM_MODELS_PATH_VAR, raising=False)
    monkeypatch.delenv(_LLM_MODELS_PROFILE_VAR, raising=False)
    monkeypatch.delenv(_NLU_PROFILE_VAR, raising=False)
    column_aliases_config.reset_cache()
    main_table_scoring_config.reset_cache()
    llm_models_config.reset_cache()
    nlu_config.reset_cache()


@pytest.fixture
def _muni_ru_nlu(monkeypatch):
    """W3-T1: активирует profiles.muni_ru в nlu_morphemes.yaml.

    Тесты RU-лемматизации (4.13 — детект FK по metadata vs суффикс,
    4.14 — морфема ``выруч`` → canonical ``revenue``) опираются на
    канонические RU-морфемы. В default-профиле NLU они отсутствуют
    (W3-T1: профиль default нейтральный), поэтому нужно явно
    переключиться на ``muni_ru``. Cache nlu_config учитывает имя
    профиля в cache key (см. NLUMorphemesRegistry.get_or_load),
    но autouse fixture всё равно сбрасывает кэш до и после теста.
    """
    monkeypatch.setenv(_NLU_PROFILE_VAR, "muni_ru")
    nlu_config.reset_cache()
    yield


class _FakeMemory:
    """Минимальный stub schema memory."""

    def __init__(self, relevant_tables=None):
        self.relevant_tables = list(relevant_tables or [])
        self.last_search_status = "ok"
        self.last_search_error = None

    def find_semantic_relevant_tables(self, terms, dsn=None):
        return list(self.relevant_tables)


def _make_core(relevant_tables=None, *, llm_caller=None) -> SchemaLinkingCore:
    return SchemaLinkingCore(
        SchemaLimiter(),
        _FakeMemory(relevant_tables),
        llm_caller=llm_caller,
    )


# ---------------------------------------------------------------------- #
# pre-req: llm_models_config loader
# ---------------------------------------------------------------------- #


def test_llm_models_config_loader(tmp_path, monkeypatch):
    """Loader читает yaml, возвращает активный профиль, и поддерживает env-override."""
    cfg_path = tmp_path / "llm.yaml"
    # W6-T3: REQUIRED_SECTIONS включает `nlu` (intent_max_tokens / nlp_max_tokens),
    # поэтому тестовый default-профиль обязан содержать эту секцию.
    cfg_path.write_text(
        "version: 1\n"
        "profiles:\n"
        "  default:\n"
        "    schema_linking:\n"
        "      max_tokens: 4242\n"
        "    sql_generation:\n"
        "      max_tokens: 8000\n"
        "    nlu:\n"
        "      intent_max_tokens: 700\n"
        "      nlp_max_tokens: 1500\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_LLM_MODELS_PATH_VAR, str(cfg_path))
    llm_models_config.reset_cache()

    profile = llm_models_config.load_llm_models_config()
    assert profile.name == "default"
    assert profile.get("schema_linking", "max_tokens") == 4242
    assert profile.get("sql_generation", "max_tokens") == 8000
    assert profile.get("nlu", "intent_max_tokens") == 700
    assert profile.get("nlu", "nlp_max_tokens") == 1500


def test_llm_models_config_fails_fast_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(_LLM_MODELS_PATH_VAR, str(tmp_path / "missing.yaml"))
    llm_models_config.reset_cache()
    with pytest.raises(FileNotFoundError):
        llm_models_config.load_llm_models_config()


# ---------------------------------------------------------------------- #
# 4.12 — мёртвый блок «дубликат heuristic_linking» убран
# ---------------------------------------------------------------------- #


def test_heuristic_linking_no_dead_fallback():
    """В perform_linking больше нет двойного вызова heuristic.

    Контракт: heuristic срабатывает ровно один раз и только в явных
    точках (LLM unavailable / error / empty). Проверяем источник на
    отсутствие двойного `self.heuristic_linking(` подряд.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_tools"
        / "text_to_sql"
        / "schema_linking"
        / "strategies.py"
    ).read_text(encoding="utf-8")
    # «if not (linked_metrics or linked_dimensions) and allow_fallbacks:» —
    # маркер мертвого блока. После 4.12 такой строки быть не должно.
    assert (
        "if not (linked_metrics or linked_dimensions) and allow_fallbacks:"
        not in source
    ), "Мёртвый блок второго heuristic-fallback не должен остаться в strategies.py"


# ---------------------------------------------------------------------- #
# 4.13 — heuristic_linking использует is_fk(meta) вместо суффикса _id
# ---------------------------------------------------------------------- #


def test_fk_detection_via_metadata_not_suffix(_muni_ru_nlu):
    """Heuristic-выбор измерения избегает FK-колонок по метаданным,
    даже если у них нет суффикса «_id».

    W3-T1: требует RU-лемматизацию (``регион`` → matches ``region``),
    которая теперь живёт в профиле ``muni_ru`` — см. fixture
    ``_muni_ru_nlu``.
    """
    core = _make_core(relevant_tables=["orders"])

    db_schema = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                # FK без суффикса «_id» — старая эвристика бы пропустила.
                "region_ref": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "regions(id)",
                    "description": "ссылка на регион",
                },
                "amount": {"type": "DECIMAL", "description": "сумма заказа"},
            }
        },
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "name": {"type": "TEXT", "description": "название региона"},
            }
        },
    }
    # Выбор измерения «регион»: best_column_for в other-table вернёт
    # region.name (с description-сигналом) — FK без суффикса в orders
    # должна быть пропущена.
    entities = {"metrics": ["amount"], "dimensions": ["регион"], "filters": {}}
    metrics, dimensions, _filters, _unlinked = core.heuristic_linking(entities, db_schema)
    # Метрика — нашли amount в orders.
    assert any(m.get("column") == "amount" for m in metrics)
    # Измерение должно попасть в regions.name (other-table), а НЕ в
    # orders.region_ref (FK).
    matched = [d for d in dimensions if d.get("name") == "регион"]
    assert matched, dimensions
    assert matched[0]["table"] == "regions"
    assert matched[0]["column"] == "name"


# ---------------------------------------------------------------------- #
# 4.14 — лемматизация через nlu_morphemes.yaml
# ---------------------------------------------------------------------- #


def test_token_matching_with_morpheme_lemmatization(_muni_ru_nlu):
    """Query-токен «выручка» матчит description «revenue» через канон.

    nlu_morphemes.yaml в профиле ``muni_ru`` содержит intent
    canonical=revenue с морфемами [«выруч», «revenue», «amount», «сумм», ...].
    После 4.14 query «выручка» и description-токен «revenue» должны
    нормализоваться в один canonical.

    W3-T1: default-профиль NLU теперь нейтральный (без RU-морфем),
    поэтому тест активирует ``muni_ru`` через fixture ``_muni_ru_nlu``.
    """
    core = _make_core()
    table_schema = {
        "columns": {
            "gross_take": {"type": "DECIMAL", "description": "revenue per period"},
            "qty": {"type": "INTEGER", "description": "items"},
        }
    }
    # default-профиль column_aliases пуст; единственный сигнал — лемматизация.
    assert core.best_column_for("выручка", "orders", table_schema) == "gross_take"


# ---------------------------------------------------------------------- #
# 4.15 — link_filters через column_aliases.yaml
# ---------------------------------------------------------------------- #


def test_link_filters_via_column_aliases(monkeypatch):
    """В профиле muni_ru фильтр ``revenue`` должен матчить измерение
    ``amount`` через алиасы (раньше использовался strict lowercase match)."""
    monkeypatch.setenv(_ALIAS_PROFILE_VAR, "muni_ru")
    column_aliases_config.reset_cache()

    core = _make_core()
    linked_dimensions = [
        {"name": "amount", "table": "orders", "column": "amount"},
    ]
    db_schema = {
        "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
    }
    result = core.link_filters(
        {"revenue": 100},
        linked_dimensions,
        main_table="orders",
        db_schema=db_schema,
    )
    assert "revenue" in result
    assert result["revenue"]["source"] == "dimension_match"
    assert result["revenue"]["column"] == "amount"


# ---------------------------------------------------------------------- #
# 4.16 — main_table выбирается через scoring (а не первый linked)
# ---------------------------------------------------------------------- #


def test_main_table_via_scoring(monkeypatch):
    """main_table в perform_linking определяется через find_main_table,
    а не как linked_metrics[0].get('table')."""
    core = _make_core(relevant_tables=["orders"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    db_schema = {
        "tiny_dim": {
            "columns": {"id": {"type": "INTEGER", "constraint_type": "PK"}}
        },
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "amount": {"type": "DECIMAL"},
                "region_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "regions(id)",
                },
            }
        },
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "name": {"type": "TEXT", "description": "регион"},
            }
        },
    }
    result = core.perform_linking(
        {"metrics": ["amount"], "dimensions": ["регион"], "filters": {}},
        db_schema,
    )
    # find_main_table со scoring выберет orders (semantic + structural
    # signal). Это совпадает с первым linked_entity table тут — но
    # важно, что main_table не None.
    assert result["main_table"] == "orders"


# ---------------------------------------------------------------------- #
# 4.17 — max_tokens читается из llm_models.yaml
# ---------------------------------------------------------------------- #


def test_max_tokens_from_llm_models_config(monkeypatch, tmp_path):
    """LLM-вызов schema_linking использует max_tokens из llm_models.yaml."""
    cfg_path = tmp_path / "llm.yaml"
    # W6-T3: REQUIRED_SECTIONS теперь включает `nlu`.
    cfg_path.write_text(
        "version: 1\n"
        "profiles:\n"
        "  default:\n"
        "    schema_linking:\n"
        "      max_tokens: 1234\n"
        "    sql_generation:\n"
        "      max_tokens: 9999\n"
        "    nlu:\n"
        "      intent_max_tokens: 700\n"
        "      nlp_max_tokens: 1500\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_LLM_MODELS_PATH_VAR, str(cfg_path))
    llm_models_config.reset_cache()

    captured = {}

    def fake_llm(**kwargs):
        captured.update(kwargs)
        # Возвращаем «никаких связей» — пайплайну этого хватит, чтобы
        # завершить тестовый вызов; нам важен только max_tokens.
        import json as _json
        return _json.dumps({
            "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
            "joins": [],
            "unlinked_entities": [],
        })

    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    core = _make_core(relevant_tables=["orders"], llm_caller=fake_llm)
    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    core.perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        db_schema,
    )

    assert captured.get("max_tokens") == 1234


# ---------------------------------------------------------------------- #
# 4.18 — FK joins + convention joins union с дедупом
# ---------------------------------------------------------------------- #


def test_fk_joins_union_with_convention():
    """build_joins объединяет FK-extracted и convention-inferred рёбра
    с симметричным дедупом.

    W4-T4 обновление: convention-join работает либо когда у конкретной
    колонки явный ``is_fk`` (constraint_type=FK / references), либо когда
    у всей схемы НЕТ FK-аннотаций (legacy fallback). Случай «partial-FK»
    (часть колонок с FK-маркером, часть без) — convention для колонок без
    маркера запрещён (silent fallback). Здесь обе ``*_id``-колонки —
    is_fk, проверяем симметричный дедуп и наличие обоих рёбер.
    """
    validator = JoinValidator()
    db_schema = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "user_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "users(id)",
                },
                # W4-T4: для FK-aware схемы convention-only без маркера
                # запрещён. Помечаем явно.
                "region_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "regions(id)",
                },
            }
        },
        "users": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
            }
        },
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
            }
        },
    }
    linked_metrics = [{"name": "x", "table": "orders", "column": "id"}]
    linked_dimensions = [
        {"name": "user", "table": "users", "column": "id"},
        {"name": "region", "table": "regions", "column": "id"},
    ]
    result = validator.build_joins(
        linked_metrics, linked_dimensions, {}, db_schema, main_table="orders"
    )
    join_pairs = {
        (j.get("from_table"), j.get("to_table"))
        for j in result["joins"]
    }
    assert ("orders", "users") in join_pairs or ("users", "orders") in join_pairs
    assert ("orders", "regions") in join_pairs or ("regions", "orders") in join_pairs


def test_convention_join_blocked_for_unmarked_fk_column_in_fk_aware_schema():
    """W4-T4: в схеме с FK-аннотациями convention-fallback по суффиксу _id
    НЕ применяется для колонок без is_fk-маркера (silent fallback запрещён)."""
    builder_schema = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                # Эта колонка — FK явно, поэтому схема считается FK-aware.
                "user_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "users(id)",
                },
                # Эта — без маркера. Convention для неё запрещён.
                "region_id": {"type": "INTEGER"},
            }
        },
        "users": {"columns": {"id": {"type": "INTEGER", "constraint_type": "PK"}}},
        "regions": {"columns": {"id": {"type": "INTEGER", "constraint_type": "PK"}}},
    }
    from custom_tools.text_to_sql.join_builder import JoinBuilder

    builder = JoinBuilder(db_schema=builder_schema)
    convention = builder.infer_joins_by_convention({"orders", "users", "regions"})
    pairs = {(j["from_table"], j["to_table"]) for j in convention}
    # region_id не должен породить convention-join в FK-aware схеме (нет
    # маркера is_fk на колонке). user_id может присутствовать в convention
    # (он is_fk), но в финальном build_joins дедуп пары orders↔users
    # уберёт его как дубль к FK-extracted join.
    assert ("orders", "regions") not in pairs

    # И сквозной build_joins не порождает orders↔regions:
    from custom_tools.text_to_sql.schema_linking.join_validation import JoinValidator

    validator = JoinValidator()
    result = validator.build_joins(
        [{"name": "x", "table": "orders", "column": "id"}],
        [{"name": "region", "table": "regions", "column": "id"}],
        {},
        builder_schema,
        main_table="orders",
    )
    bj_pairs = {(j["from_table"], j["to_table"]) for j in result["joins"]}
    assert ("orders", "regions") not in bj_pairs
    assert ("regions", "orders") not in bj_pairs


def test_convention_join_works_when_schema_has_no_fk_metadata():
    """W4-T4: если в схеме НЕТ FK-метаданных вообще, convention-fallback
    по суффиксу _id сохраняется (legacy поведение)."""
    from custom_tools.text_to_sql.join_builder import JoinBuilder

    schema_without_fk = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "user_id": {"type": "INTEGER"},
                "region_id": {"type": "INTEGER"},
            }
        },
        "users": {"columns": {"id": {"type": "INTEGER", "constraint_type": "PK"}}},
        "regions": {"columns": {"id": {"type": "INTEGER", "constraint_type": "PK"}}},
    }
    builder = JoinBuilder(db_schema=schema_without_fk)
    convention = builder.infer_joins_by_convention({"orders", "users", "regions"})
    pairs = {(j["from_table"], j["to_table"]) for j in convention}
    # Без FK-аннотаций — convention применяется ко всем _id-колонкам.
    assert ("orders", "users") in pairs
    assert ("orders", "regions") in pairs


# ---------------------------------------------------------------------- #
# 4.19 — join validate возвращает invalid при missing type
# ---------------------------------------------------------------------- #


def test_join_with_missing_types_invalid():
    validator = JoinValidator()
    db_schema = {
        "a": {"columns": {"x": {"type": "", "constraint_type": "FK"}}},
        "b": {"columns": {"y": {"type": "INTEGER", "constraint_type": "PK"}}},
    }
    result = validator._is_join_valid_against_schema(
        {"from_table": "a", "from_column": "x", "to_table": "b", "to_column": "y"},
        db_schema,
    )
    assert result["valid"] is False
    assert "type" in result["error"].lower()


# ---------------------------------------------------------------------- #
# 4.20 — _is_duplicate_join симметричен (frozenset endpoints)
# ---------------------------------------------------------------------- #


def test_is_duplicate_join_symmetric():
    j_ab = {
        "from_table": "a", "from_column": "id",
        "to_table": "b", "to_column": "a_id",
    }
    j_ba = {
        "from_table": "b", "from_column": "a_id",
        "to_table": "a", "to_column": "id",
    }
    # Симметричное сравнение: reverse — это дубликат.
    assert JoinValidator._is_duplicate_join(j_ba, [j_ab]) is True
    # Несовпадение колонок — НЕ дубликат.
    j_other = {
        "from_table": "a", "from_column": "id",
        "to_table": "b", "to_column": "other",
    }
    assert JoinValidator._is_duplicate_join(j_other, [j_ab]) is False


# ---------------------------------------------------------------------- #
# 4.21 — bridge join для M:N (single-hop)
# ---------------------------------------------------------------------- #


def test_fk_bridge_m_n():
    """Между required-таблицами A и B нет прямого FK, но есть bridge X
    с FK на обе. Тогда _extract_fk_joins добавляет рёбра X↔A и X↔B
    с флагом via_bridge=True."""
    validator = JoinValidator()
    db_schema = {
        "students": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "name": {"type": "TEXT"},
            }
        },
        "courses": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "title": {"type": "TEXT"},
            }
        },
        # bridge
        "enrollments": {
            "columns": {
                "student_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "students(id)",
                },
                "course_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "courses(id)",
                },
            }
        },
    }
    joins = validator._extract_fk_joins({"students", "courses"}, db_schema)
    bridge_joins = [j for j in joins if j.get("via_bridge")]
    assert len(bridge_joins) >= 2
    bridge_targets = {j.get("to_table") for j in bridge_joins}
    assert bridge_targets == {"students", "courses"}
    assert all(j.get("from_table") == "enrollments" for j in bridge_joins)


# ---------------------------------------------------------------------- #
# T5-linking / #6 HIGH — M:N bridge полностью достижим через build_joins
# ---------------------------------------------------------------------- #


def test_build_joins_m_n_bridge_success():
    """T5-linking / #6 HIGH: путь students↔enrollments↔courses должен
    строиться через build_joins (success=True, enrollments в joins).

    Баг: required_tables считался без via_bridge-таблиц, поэтому JoinBuilder
    не включал enrollments как «нужную» таблицу и путь был недостижим.
    После фикса: compute_required_tables пересчитывается с учётом
    joins_from_schema перед вызовом JoinBuilder.
    """
    validator = JoinValidator()
    db_schema = {
        "students": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "name": {"type": "TEXT"},
            }
        },
        "courses": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "title": {"type": "TEXT"},
            }
        },
        "enrollments": {
            "columns": {
                "student_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "students(id)",
                },
                "course_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "courses(id)",
                },
            }
        },
    }

    linked_metrics = [{"name": "count", "table": "students", "column": "id"}]
    linked_dimensions = [{"name": "title", "table": "courses", "column": "title"}]

    result = validator.build_joins(
        linked_metrics=linked_metrics,
        linked_dimensions=linked_dimensions,
        linked_filters={},
        db_schema=db_schema,
        main_table="students",
    )

    assert result["success"] is True, (
        f"Ожидался success=True, получили: {result}"
    )
    join_tables = {j.get("from_table") for j in result["joins"]} | {
        j.get("to_table") for j in result["joins"]
    }
    assert "enrollments" in join_tables, (
        f"enrollments должна быть в joins, но joins={result['joins']}"
    )
    assert "courses" in join_tables


# ---------------------------------------------------------------------- #
# 4.22 — JoinValidator() без мёртвых init-аргументов
# ---------------------------------------------------------------------- #


def test_join_validator_no_dead_init_args(monkeypatch):
    """JoinValidator больше не использует schema_limiter / memory_manager
    в бизнес-логике, но принимает их в сигнатуре ради backward-compat —
    с DeprecationWarning при ненулевых значениях (4.22)."""
    from custom_tools.text_to_sql.schema_linking import join_validation

    # Прямой вызов без аргументов — должен работать.
    validator = JoinValidator()
    assert validator.join_builder is None

    caught = []

    def capture_warning(message, category=None, **kwargs):
        caught.append((str(message), category or UserWarning))

    monkeypatch.setattr(join_validation.warnings, "warn", capture_warning)

    # Передача None по legacy-именам — не warning, аргументы тихо игнорим.
    JoinValidator(schema_limiter=None, memory_manager=None)
    assert caught == []

    # Ненулевые значения — DeprecationWarning, но не TypeError.
    JoinValidator(schema_limiter=object())
    assert any(
        issubclass(category, DeprecationWarning) and "schema_limiter" in message
        for message, category in caught
    )

    caught.clear()
    JoinValidator(memory_manager=object())
    assert any(
        issubclass(category, DeprecationWarning) and "memory_manager" in message
        for message, category in caught
    )

    # Неизвестные legacy-kwargs — тоже DeprecationWarning, без TypeError.
    caught.clear()
    JoinValidator(some_old_arg=123)  # type: ignore[call-arg]
    assert any(issubclass(category, DeprecationWarning) for _message, category in caught)


# ---------------------------------------------------------------------- #
# 4.23 — короткие имена с >1 матчей логируют warning
# ---------------------------------------------------------------------- #


def test_short_matches_logged_warning(caplog):
    """_resolve_table_name на ambiguous коротком имени возвращает None
    и логирует WARNING с перечислением matches."""
    schema = {
        "shop1.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "shop2.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }
    with caplog.at_level(
        logging.WARNING,
        logger="custom_tools.text_to_sql.schema_linking.resolution",
    ):
        result = _resolve_table_name("orders", schema)
    assert result is None
    warning_messages = [
        rec.message for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert warning_messages, "Ожидался WARNING для ambiguous short name"
    msg = warning_messages[0]
    assert "shop1.orders" in msg
    assert "shop2.orders" in msg


# ---------------------------------------------------------------------- #
# 4.24 / EPIC 8.6 — shim удалён
# ---------------------------------------------------------------------- #


def test_schema_linking_core_shim_removed():
    """EPIC 8.6: ``schema_linking_core.py`` удалён. Импорт должен падать —
    все callers мигрированы на ``schema_linking`` package + конструкторный
    DI ``llm_caller``.
    """
    with pytest.raises(ImportError):
        import custom_tools.text_to_sql.schema_linking_core  # noqa: F401


# ---------------------------------------------------------------------- #
# 4.25 — DI llm_caller через конструктор
# ---------------------------------------------------------------------- #


def test_schema_linking_di_llm_caller(monkeypatch):
    """SchemaLinkingCore принимает явный llm_caller через конструктор
    (EPIC 8.6: shim удалён, DI — единственный путь)."""
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        import json as _json
        return _json.dumps({
            "linked_entities": {
                "metrics": [
                    {"name": "amount", "table": "orders", "column": "amount"},
                ],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "unlinked_entities": [],
        })

    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)

    core = _make_core(relevant_tables=["orders"], llm_caller=fake_llm)
    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}
    result = core.perform_linking(
        {"metrics": ["amount"], "dimensions": [], "filters": {}},
        db_schema,
    )
    assert len(calls) == 1
    assert result["linked_entities"]["metrics"][0]["column"] == "amount"


def test_schema_linking_no_llm_caller_is_explicit_error(monkeypatch):
    """EPIC 8.6 + AGENTS.md «НЕТ silent fallback»: если ``llm_caller=None`` и
    ``SCHEMA_LINKING_USE_LLM=1`` (включён LLM), и fallbacks выключены,
    ``perform_linking`` возвращает explicit error, а не молча падает в
    heuristic.
    """
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)

    core = _make_core(relevant_tables=["orders"], llm_caller=None)
    result = core.perform_linking(
        {"metrics": ["amount"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )
    assert "LLM schema linking unavailable or disabled" in result["error"]
    assert "llm_caller is not configured" in result["error"]
    assert "heuristic fallbacks are disabled" in result["error"]


# ---------------------------------------------------------------------- #
# 4.26 — type-hint бонус работает изолированно от name match (new format)
# ---------------------------------------------------------------------- #


def test_type_hint_works_without_name_match(tmp_path, monkeypatch):
    """С новым форматом type_hints (dict с weight_solo) колонка с
    числовым типом получает бонус, даже если name/description-сигнала нет.

    Сценарий: query=«revenue», column=«gross_take: DECIMAL» — ни exact,
    ни alias, ни description-сигнала нет. С новым форматом
    numeric.weight_solo=2, weight_with_signal=4 — type-bonus сработает.
    """
    yaml_path = tmp_path / "aliases.yaml"
    yaml_path.write_text(
        "version: 2\n"
        "policy:\n"
        "  type_hint_categories: [numeric, temporal, identifier]\n"
        "  required_profiles: [default]\n"
        "  default_profile_must_be_empty: true\n"
        "profiles:\n"
        "  default:\n"
        "    aliases: {}\n"
        "    type_hints:\n"
        "      numeric:\n"
        "        tokens: [revenue, amount]\n"
        "        weight_solo: 2\n"
        "        weight_with_signal: 4\n"
        "      temporal: []\n"
        "      identifier: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ALIAS_PATH_VAR, str(yaml_path))
    column_aliases_config.reset_cache()

    core = _make_core()
    table_schema = {
        "columns": {
            "gross_take": {"type": "DECIMAL", "description": ""},
            "qty": {"type": "INTEGER", "description": ""},  # тоже numeric type
        }
    }
    # «revenue» — ни exact, ни alias, ни description-сигнала. Type-bonus
    # solo даёт +2 обеим numeric-колонкам. Любая из них может выиграть —
    # важно, что хоть какая-то найдена (раньше было None).
    result = core.best_column_for("revenue", "orders", table_schema)
    assert result in {"gross_take", "qty"}
