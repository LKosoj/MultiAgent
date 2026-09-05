"""W1-1.2b: DSN-профиль как приоритетный источник эвристических подсказок.

Покрывает:
  * ``dsn_profile_overrides.resolve_column_aliases_profile`` /
    ``resolve_significance_profile`` / ``resolve_nlu_morphemes`` —
    (a) без DSN-профиля результат byte-identical named-профилю;
    (b) с DSN-профилем (monkeypatch ``load_dsn_profile``) — приоритет DSN;
    (c) битый DSN-профиль (``ValueError``) — warning + fallback на named.
  * плюмбинг ``dsn`` через 5 читателей: ``heuristic_linker.best_column_for``,
    ``schema_metadata.is_semantic_significant_column``,
    ``nlu.NLUProcessor.extract_intent`` (fallback-путь),
    ``schema_filtering._try_load_morphemes_index``,
    ``join_builder.JoinBuilder._resolve_pluralizers``.
"""

from __future__ import annotations

import logging

import pytest

import custom_tools.text_to_sql.dsn_profile as dp
from custom_tools.text_to_sql import (
    column_aliases_config,
    core as core_module,
    dsn_profile_overrides as dpo,
    nlu_config,
    schema_filtering,
    schema_metadata,
    significance_config,
)
from custom_tools.text_to_sql.dsn_profile import (
    DsnProfile,
    MetricHints,
    SignificantColumnHints,
)
from custom_tools.text_to_sql.join_builder import JoinBuilder
from custom_tools.text_to_sql.schema_linking import SchemaLinkingCore
from custom_tools.text_to_sql.schema_linking import join_validation as join_validation_module
from custom_tools.text_to_sql.schema_linking.heuristic_linker import HeuristicLinker
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.validators import SchemaLimiter

DSN = "postgresql://host/db"


def _namespace(schema: dict) -> SchemaNamespace:
    return SchemaNamespace(
        scope=SchemaScope.from_mapping({
            "serialization_version": 1,
            "tenant_id": "tenant-a",
            "access_scope_id": "owner:alice",
            "connection_view_id": "registry:db-1",
            "transient": False,
        }),
        schema_fingerprint=canonical_schema_fingerprint(schema),
    )


@pytest.fixture(autouse=True)
def _reset_caches():
    column_aliases_config.reset_cache()
    significance_config.reset_cache()
    nlu_config.reset_cache()
    yield
    column_aliases_config.reset_cache()
    significance_config.reset_cache()
    nlu_config.reset_cache()


class _FakeMemory:
    def find_semantic_relevant_tables(self, terms, dsn=None):
        return []


def _dsn_profile_with(**overrides) -> DsnProfile:
    empty = DsnProfile.empty()
    return DsnProfile(
        version=empty.version,
        dsn_fingerprint=empty.dsn_fingerprint,
        schema_namespace_version=empty.schema_namespace_version,
        captured_at=empty.captured_at,
        glossary=empty.glossary,
        aliases=overrides.get("aliases", empty.aliases),
        type_hints=overrides.get("type_hints", empty.type_hints),
        metric_hints=overrides.get("metric_hints", empty.metric_hints),
        nlu_hints=overrides.get("nlu_hints", empty.nlu_hints),
        few_shots_ref=empty.few_shots_ref,
    )


# --------------------------------------------------------------------- #
# resolve_column_aliases_profile
# --------------------------------------------------------------------- #

def test_column_aliases_no_dsn_byte_identical_to_named():
    named = column_aliases_config.get_active_profile()
    resolved = dpo.resolve_column_aliases_profile(dsn=None)
    assert resolved.name == named.name
    assert resolved.aliases == named.aliases
    assert resolved.type_hints == named.type_hints


def test_column_aliases_dsn_overrides_aliases_only(monkeypatch):
    dsn_profile = _dsn_profile_with(aliases={"revenue": ("amount",)})
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    named = column_aliases_config.get_active_profile()
    resolved = dpo.resolve_column_aliases_profile(dsn=DSN)

    # ColumnAliasesProfile нормализует значения aliases в list[str] lowercase.
    assert resolved.aliases == {"revenue": ["amount"]}
    # type_hints не тронуты DSN-профилем -> наследуются от named (секции независимы).
    assert resolved.type_hints == named.type_hints
    assert resolved.name.endswith("+dsn_override")


def test_column_aliases_broken_dsn_profile_warns_and_falls_back(monkeypatch, caplog):
    def _raise(dsn, **_):
        raise ValueError("dsn-profile-broken")

    monkeypatch.setattr(dp, "load_dsn_profile", _raise)
    named = column_aliases_config.get_active_profile()

    with caplog.at_level(logging.WARNING, logger=dpo.logger.name):
        resolved = dpo.resolve_column_aliases_profile(dsn=DSN)

    assert resolved.aliases == named.aliases
    assert resolved.type_hints == named.type_hints
    assert any("dsn-profile-broken" in rec.message for rec in caplog.records)


def test_column_aliases_dsn_strict_runtime_error_propagates(monkeypatch):
    def _raise(dsn, **_):
        raise RuntimeError("stale schema_namespace_version")

    monkeypatch.setattr(dp, "load_dsn_profile", _raise)
    with pytest.raises(RuntimeError):
        dpo.resolve_column_aliases_profile(dsn=DSN)


# --------------------------------------------------------------------- #
# resolve_significance_profile
# --------------------------------------------------------------------- #

def test_significance_no_dsn_byte_identical_to_named():
    named = significance_config.load_significance_config()
    resolved = dpo.resolve_significance_profile(dsn=None)
    assert resolved.name == named.name
    assert resolved.high_priority_exact == named.high_priority_exact
    assert resolved.high_priority_compound == named.high_priority_compound
    assert resolved.critical_description_keywords == named.critical_description_keywords


def test_significance_dsn_overrides_significant_columns(monkeypatch):
    dsn_profile = _dsn_profile_with(
        metric_hints=MetricHints(
            significant_columns=SignificantColumnHints(
                high_priority_exact=("oktmo",),
                high_priority_compound=("okato", "code"),
                medium_priority_patterns=(("^.*_id$", "id-like"),),
                critical_description_keywords=("идентификатор",),
                important_column_name_substrings=("KEY",),
            )
        )
    )
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    resolved = dpo.resolve_significance_profile(dsn=DSN)

    assert resolved.high_priority_exact == frozenset({"oktmo"})
    assert resolved.high_priority_compound == frozenset({"okato", "code"})
    assert resolved.critical_description_keywords == frozenset({"идентификатор"})
    assert resolved.important_column_name_substrings == frozenset({"key"})
    assert [d for _, d in resolved.medium_priority_patterns] == ["id-like"]
    assert resolved.medium_priority_patterns[0][0].search("some_id") is not None


def test_significance_broken_dsn_profile_warns_and_falls_back(monkeypatch, caplog):
    def _raise(dsn, **_):
        raise ValueError("dsn-significance-broken")

    monkeypatch.setattr(dp, "load_dsn_profile", _raise)
    named = significance_config.load_significance_config()

    with caplog.at_level(logging.WARNING, logger=dpo.logger.name):
        resolved = dpo.resolve_significance_profile(dsn=DSN)

    assert resolved.high_priority_exact == named.high_priority_exact
    assert any("dsn-significance-broken" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------- #
# resolve_nlu_morphemes
# --------------------------------------------------------------------- #

def test_nlu_morphemes_no_dsn_byte_identical_to_named():
    named = nlu_config.load_nlu_morphemes()
    resolved = dpo.resolve_nlu_morphemes(dsn=None)
    assert resolved.default_intent == named.default_intent
    assert resolved.enabled == named.enabled
    assert resolved.intents == named.intents


def test_nlu_morphemes_dsn_overrides_default_intent(monkeypatch):
    dsn_profile = _dsn_profile_with(nlu_hints={"default_intent": "dsn_custom_intent"})
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    named = nlu_config.load_nlu_morphemes()
    resolved = dpo.resolve_nlu_morphemes(dsn=DSN)

    assert resolved.default_intent == "dsn_custom_intent"
    # Остальные ключи (не тронутые nlu_hints) -> из named-профиля.
    assert resolved.enabled == named.enabled
    assert resolved.intents == named.intents


def test_nlu_morphemes_broken_dsn_profile_warns_and_falls_back(monkeypatch, caplog):
    def _raise(dsn, **_):
        raise ValueError("dsn-nlu-broken")

    monkeypatch.setattr(dp, "load_dsn_profile", _raise)
    named = nlu_config.load_nlu_morphemes()

    with caplog.at_level(logging.WARNING, logger=dpo.logger.name):
        resolved = dpo.resolve_nlu_morphemes(dsn=DSN)

    assert resolved.default_intent == named.default_intent


# --------------------------------------------------------------------- #
# Плюмбинг dsn через 5 читателей
# --------------------------------------------------------------------- #

def test_heuristic_linker_best_column_for_uses_dsn_aliases(monkeypatch):
    dsn_profile = _dsn_profile_with(aliases={"revenue": ("amount",)})
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    linker = HeuristicLinker(_FakeMemory())
    table_schema = {
        "columns": {
            "amount": {"type": "DECIMAL", "description": ""},
            "qty": {"type": "INTEGER", "description": ""},
        }
    }
    # Без dsn (default named-профиль пуст) -> не находит.
    assert linker.best_column_for("revenue", "orders", table_schema) is None
    # С dsn -> DSN-alias приоритетнее.
    assert linker.best_column_for("revenue", "orders", table_schema, dsn=DSN) == "amount"


def test_heuristic_linker_scoped_semantic_search_forwards_dsn():
    """1.2 остаточный блокер: heuristic_linker.py в scoped-режиме
    (namespace задан) должен звать find_semantic_relevant_tables с
    dsn=dsn — та же категория бага, что уже исправлена в
    schema_filtering.py (namespace без dsn молча терял DSN-профиль)."""
    captured = {}

    class _SpyMemory:
        def find_semantic_relevant_tables(self, terms, dsn=None, namespace=None):
            captured["dsn"] = dsn
            captured["namespace"] = namespace
            return []

    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}}
    namespace = _namespace(db_schema)

    linker = HeuristicLinker(_SpyMemory())
    linker.heuristic_linking(
        {"metrics": ["amount"]}, db_schema, dsn=DSN, namespace=namespace
    )

    assert captured["dsn"] == DSN
    assert captured["namespace"] is namespace


def test_schema_metadata_is_significant_uses_dsn_significance(monkeypatch):
    dsn_profile = _dsn_profile_with(
        metric_hints=MetricHints(
            significant_columns=SignificantColumnHints(high_priority_exact=("zzz_custom_col",))
        )
    )
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    helper = schema_metadata.ColumnMetadataHelper
    assert helper.is_semantic_significant_column("zzz_custom_col", {}) is False
    assert helper.is_semantic_significant_column("zzz_custom_col", {}, dsn=DSN) is True


def test_nlu_extract_intent_fallback_uses_dsn_nlu_hints(tmp_path, monkeypatch):
    import custom_tools.text_to_sql.nlu as nlu_module

    monkeypatch.setattr(nlu_module, "call_openai_api", None)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    # Named-профиль "default" имеет enabled=false (нейтральный, без RU-морфем).
    # Подсовываем свой минимальный yaml (legacy flat layout, без "profiles")
    # с enabled=true как baseline, чтобы сравнивать только intents, а не
    # наличие/отсутствие самого fallback-пути.
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    cfg_path.write_text(
        """
version: 1
language: ru
enabled: true
intents: []
dimensions: []
relative_date:
  triggers: []
  periods: []
  days_pattern: '(\\d+)\\s*(?:day)'
patterns:
  date_iso: []
  region: []
  amount_greater: []
  amount_less: []
  amount_between: []
  top_n: []
order:
  triggers: []
  desc_triggers: []
intent_rules: []
default_intent: query
top_n_intent: top_n
tokenizer:
  adpositions: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    nlu_config.reset_cache()

    dsn_profile = _dsn_profile_with(
        nlu_hints={
            "intents": [{"canonical": "zzz_custom_metric", "morphemes": ["zzzcustommorph"]}],
        }
    )
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    processor = nlu_module.NLUProcessor()
    result = processor.extract_intent("покажи zzzcustommorph по регионам", dsn=DSN)
    assert "zzz_custom_metric" in result["entities"]["metrics"]

    result_no_dsn = processor.extract_intent("покажи zzzcustommorph по регионам")
    assert "zzz_custom_metric" not in result_no_dsn["entities"]["metrics"]


def test_schema_filtering_morphemes_index_uses_dsn_nlu_hints(monkeypatch):
    dsn_profile = _dsn_profile_with(
        nlu_hints={
            "enabled": True,
            "intents": [{"canonical": "zzz_custom_metric", "morphemes": ["zzzcustommorph"]}],
        }
    )
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    index = schema_filtering._try_load_morphemes_index(dsn=DSN)
    assert index is not None
    assert "zzz_custom_metric" in index
    assert "zzzcustommorph" in index["zzz_custom_metric"]


def test_join_builder_resolve_pluralizers_uses_dsn_nlu_hints(monkeypatch):
    dsn_profile = _dsn_profile_with(
        nlu_hints={
            "table_name_inflections": {
                "enabled": True,
                "pluralizers": [["zzzsuffix", "zzzsuffixes"]],
            }
        }
    )
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    builder = JoinBuilder({}, dsn=DSN)
    pluralizers = builder._resolve_pluralizers()
    assert ("zzzsuffix", "zzzsuffixes") in pluralizers

    builder_no_dsn = JoinBuilder({})
    pluralizers_no_dsn = builder_no_dsn._resolve_pluralizers()
    assert ("zzzsuffix", "zzzsuffixes") not in pluralizers_no_dsn


# --------------------------------------------------------------------- #
# W1-1.2b доп.: проброс dsn через facade-слой (SchemaLinkingCore,
# JoinValidator, core.intent_extraction), не только напрямую через
# HeuristicLinker/JoinBuilder/NLUProcessor.
# --------------------------------------------------------------------- #

def test_schema_linking_core_best_column_for_forwards_dsn(monkeypatch):
    captured = {}
    original_resolve = dpo.resolve_column_aliases_profile

    def _spy(dsn=None):
        captured["dsn"] = dsn
        return original_resolve(dsn=dsn)

    monkeypatch.setattr(dpo, "resolve_column_aliases_profile", _spy)

    core = SchemaLinkingCore(SchemaLimiter(), _FakeMemory())
    table_schema = {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}

    assert core.best_column_for("amount", "orders", table_schema, dsn=DSN) == "amount"
    assert captured["dsn"] == DSN


def test_schema_linking_core_best_column_for_without_dsn_behaves_as_before(monkeypatch):
    core = SchemaLinkingCore(SchemaLimiter(), _FakeMemory())
    table_schema = {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}

    # Без dsn -> тот же результат, что и раньше (named-профиль, dsn=None
    # пробрасывается в heuristic_linker как и до добавления dsn-параметра).
    assert core.best_column_for("amount", "orders", table_schema) == "amount"


def test_join_validator_build_joins_uses_runtime_context_dsn(monkeypatch):
    monkeypatch.setattr(join_validation_module, "get_runtime_context_dsn", lambda: DSN)

    validator = join_validation_module.JoinValidator()
    db_schema = {"orders": {"columns": {"id": {"type": "INTEGER"}}}}

    validator.build_joins([], [], {}, db_schema, main_table="orders")

    assert validator.join_builder is not None
    assert validator.join_builder.dsn == DSN


def test_join_validator_build_joins_without_runtime_dsn_leaves_join_builder_dsn_none(monkeypatch):
    monkeypatch.setattr(join_validation_module, "get_runtime_context_dsn", lambda: None)

    validator = join_validation_module.JoinValidator()
    db_schema = {"orders": {"columns": {"id": {"type": "INTEGER"}}}}

    validator.build_joins([], [], {}, db_schema, main_table="orders")

    # Поведение прежнее: без runtime-context dsn JoinBuilder.dsn остаётся
    # None (как до проброса dsn в JoinBuilder(db_schema)).
    assert validator.join_builder.dsn is None


def test_core_intent_extraction_forwards_dsn(monkeypatch):
    captured = {}

    class _StubProcessor:
        def extract_intent(self, text, session_id=None, *, dsn=None):
            captured["dsn"] = dsn
            return {
                "intent": "query",
                "entities": {"metrics": [], "dimensions": [], "filters": {}},
            }

    monkeypatch.setattr(core_module, "nlu_processor", _StubProcessor())

    core_module.intent_extraction("текст", dsn=DSN)
    assert captured["dsn"] == DSN


def test_core_intent_extraction_without_dsn_passes_none(monkeypatch):
    captured = {}

    class _StubProcessor:
        def extract_intent(self, text, session_id=None, *, dsn=None):
            captured["dsn"] = dsn
            return {
                "intent": "query",
                "entities": {"metrics": [], "dimensions": [], "filters": {}},
            }

    monkeypatch.setattr(core_module, "nlu_processor", _StubProcessor())

    # Поведение прежнее: dsn не передан вызывающим кодом -> None (как и до
    # добавления dsn-параметра в core.intent_extraction).
    core_module.intent_extraction("текст")
    assert captured["dsn"] is None


# --------------------------------------------------------------------- #
# W1-1.2-review Блокер 1: DSN терялся в scoped (namespace) режиме —
# namespace передавался, а dsn молча пропадал (см. schema_linker.py,
# linking_orchestrator.py, schema_filtering.py — все теперь передают
# dsn и namespace ОДНОВРЕМЕННО).
# --------------------------------------------------------------------- #

class _NamespaceMemory:
    """Fake memory-manager, принимающий и dsn, и namespace одновременно.

    В отличие от ``_FakeMemory`` (только ``dsn=``), этот стаб нужен для
    веток кода, которые передают ``namespace`` наравне с ``dsn`` — если
    бы регрессия Блокера 1 вернулась (namespace без dsn), сигнатура ниже
    всё равно бы не упала (оба параметра — keyword с default), поэтому
    сам факт "не упало" здесь ничего не доказывает: проверяем именно
    ЗНАЧЕНИЕ dsn, дошедшее до нижележащих функций (см. тесты ниже).
    """

    def find_semantic_relevant_tables(self, terms, dsn=None, namespace=None):
        return []


def test_perform_linking_llm_branch_forwards_dsn_with_namespace(monkeypatch):
    """Блокер 1: LLM-ветка perform_linking (namespace!=None) должна звать
    self.llm_linking(..., dsn=dsn, namespace=namespace), а не только
    namespace=namespace — иначе DSN-профиль в LLM-промпте scoped-линковки
    молча терялся."""
    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}}
    namespace = _namespace(db_schema)

    core = SchemaLinkingCore(SchemaLimiter(), _NamespaceMemory(), llm_caller=lambda **_: None)

    captured = {}

    def _spy_llm_linking(entities, db_schema, dsn=None, namespace=None):
        captured["dsn"] = dsn
        captured["namespace"] = namespace
        return {
            "linked_entities": {
                "metrics": [{"name": "amount", "table": "orders", "column": "amount"}],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
        }

    monkeypatch.setattr(core, "llm_linking", _spy_llm_linking)

    core.perform_linking({"metrics": ["amount"]}, db_schema, dsn=DSN, namespace=namespace)

    assert captured["dsn"] == DSN
    assert captured["namespace"] is namespace


def test_llm_linker_scoped_semantic_search_forwards_dsn():
    """1.2 остаточный блокер: тот же класс бага в llm_linker.py -- scoped-
    режим (namespace задан) должен звать find_semantic_relevant_tables с
    dsn=dsn."""
    from custom_tools.text_to_sql.schema_linking.llm_linker import LLMLinker

    captured = {}

    class _SpyMemory:
        def find_semantic_relevant_tables(self, terms, dsn=None, namespace=None):
            captured["dsn"] = dsn
            captured["namespace"] = namespace
            return []

    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}}
    namespace = _namespace(db_schema)

    linker = LLMLinker(
        schema_limiter=object(),
        memory_manager=_SpyMemory(),
        entity_term_collector=lambda _entities: ["amount"],
        llm_caller=lambda **_: None,
    )
    linker.llm_linking(
        {"metrics": ["amount"], "dimensions": [], "filters": {}},
        db_schema,
        dsn=DSN,
        namespace=namespace,
    )

    assert captured["dsn"] == DSN
    assert captured["namespace"] is namespace


def test_build_relevant_schema_context_namespace_forwards_dsn_to_significance(monkeypatch):
    """Блокер 1: build_relevant_schema_context(namespace=..., dsn=...) не
    должен терять dsn при резолве значимости колонок (иначе DSN-профиль
    значимости молча откатывается на named-профиль в scoped-режиме)."""
    captured = {}
    original_resolve = dpo.resolve_significance_profile

    def _spy(dsn=None):
        captured["dsn"] = dsn
        return original_resolve(dsn=dsn)

    monkeypatch.setattr(dpo, "resolve_significance_profile", _spy)

    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}}
    namespace = _namespace(db_schema)

    builder = schema_filtering.SchemaContextBuilder(_NamespaceMemory())
    builder.build_relevant_schema_context(
        [{"name": "amount", "table": "orders", "column": "amount"}],
        [],
        {},
        [],
        db_schema,
        dsn=DSN,
        namespace=namespace,
    )

    assert captured["dsn"] == DSN


def test_build_relevant_schema_context_namespace_forwards_dsn_to_memory_search(monkeypatch):
    """Блокер 1: тот же класс бага в поиске таблиц через память — namespace
    без dsn (schema_filtering.py, найдено дополнительно к трём точкам из
    ревью в schema_linker.py/linking_orchestrator.py)."""
    captured = {}

    class _SpyMemory:
        def find_semantic_relevant_tables(self, terms, dsn=None, namespace=None):
            captured["dsn"] = dsn
            captured["namespace"] = namespace
            return []

    db_schema = {"orders": {"columns": {"amount": {"type": "DECIMAL", "description": ""}}}}
    namespace = _namespace(db_schema)

    builder = schema_filtering.SchemaContextBuilder(_SpyMemory())
    builder.build_relevant_schema_context(
        [{"name": "amount", "table": "orders", "column": "amount"}],
        [],
        {},
        [],
        db_schema,
        dsn=DSN,
        namespace=namespace,
    )

    assert captured["dsn"] == DSN
    assert captured["namespace"] is namespace


# --------------------------------------------------------------------- #
# W1-1.2-review Блокер 3: сырой dsn (с паролем) не должен попадать в
# source_path/сообщения об ошибках.
# --------------------------------------------------------------------- #

def test_nlu_morphemes_source_path_does_not_leak_dsn_password(monkeypatch):
    dsn_with_password = "postgresql://u:secret@h/db"
    dsn_profile = _dsn_profile_with(nlu_hints={"default_intent": "dsn_custom_intent"})
    monkeypatch.setattr(dp, "load_dsn_profile", lambda dsn, **_: dsn_profile)

    resolved = dpo.resolve_nlu_morphemes(dsn=dsn_with_password)

    assert "secret" not in resolved.source_path


# --------------------------------------------------------------------- #
# W1-1.2-review Замечание 2: significance-профиль резолвится один раз на
# весь build_relevant_schema_context, а не на каждую колонку.
# --------------------------------------------------------------------- #

def test_build_relevant_schema_context_resolves_significance_profile_once(monkeypatch):
    calls = {"n": 0}
    original_load_dsn_profile = dp.load_dsn_profile

    def _counting_load_dsn_profile(dsn, **kwargs):
        calls["n"] += 1
        return original_load_dsn_profile(dsn, **kwargs)

    monkeypatch.setattr(dp, "load_dsn_profile", _counting_load_dsn_profile)

    tables = {}
    linked_metrics = []
    for t in range(3):
        table_name = f"table_{t}"
        columns = {f"col_{c}": {"type": "TEXT", "description": ""} for c in range(10)}
        tables[table_name] = {"columns": columns}
        linked_metrics.append({"name": "col_0", "table": table_name, "column": "col_0"})

    builder = schema_filtering.SchemaContextBuilder(_FakeMemory())
    builder.build_relevant_schema_context(
        linked_metrics, [], {}, [], tables, dsn=DSN,
    )

    assert calls["n"] <= 1
