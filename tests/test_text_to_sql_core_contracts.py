import json
import logging
import sqlite3
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from custom_tools import sql_tools
from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import audit_logger, purge_schema_linking_rag_cache, schema_linking
from custom_tools.text_to_sql.core import secure_db_executor, sql_explain
from custom_tools.text_to_sql.nlu import NLUProcessor
from custom_tools.text_to_sql.rag import RAGSearcher
from custom_tools.text_to_sql.schema_enricher import SchemaEnricher
from custom_tools.text_to_sql.schema_linking import SchemaLinkingCore
from custom_tools.text_to_sql.schema_linker import SchemaLinker
from custom_tools.text_to_sql.schema_memory import SchemaCacheManager, SchemaMemoryManager
from custom_tools.text_to_sql.sql_generator import SQLGenerator
from custom_tools.text_to_sql.validators import SchemaLimiter, SQLSchemaValidator


class _FakeMemory:
    def __init__(self, relevant_tables=None):
        self.relevant_tables = relevant_tables or []
        self.terms = None
        self.last_search_status = "ok"
        self.last_search_error = None

    def find_semantic_relevant_tables(self, terms, dsn=None):
        self.terms = list(terms)
        self.dsn = dsn
        return list(self.relevant_tables)


class _FkPreviewPlugin:
    def __init__(self):
        self.calls = []

    def get_fk_preview(self, conn, table_name, fk_column, ref_table, max_rows=None, ref_column=None):
        self.calls.append({
            "table_name": table_name,
            "fk_column": fk_column,
            "ref_table": ref_table,
            "max_rows": max_rows,
            "ref_column": ref_column,
        })
        return {
            "success": True,
            "data": [(1, "Alice")],
            "columns": ["user_id", "name"],
        }


def _raw_pyodbc_dsn() -> str:
    odbc_connect = urllib.parse.quote_plus(
        "DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret"
    )
    return f"mssql+pyodbc:///?odbc_connect={odbc_connect}&driver=ODBC+Driver+17"


def test_nlu_fails_fast_without_llm_unless_fallback_opted_in(monkeypatch):
    from custom_tools.text_to_sql import nlu, nlu_config

    # W3-T1: default NLU-профиль теперь нейтральный (RU-морфемы переехали
    # в profiles.muni_ru). Контракт ALLOW_FALLBACKS=1 проверяем на
    # активном muni_ru-профиле — для пользовательского РФ-датасета это
    # рабочий конфиг.
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    nlu_config.reset_cache()

    processor = NLUProcessor()
    monkeypatch.setattr(nlu, "call_openai_api", None)
    monkeypatch.delenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", raising=False)

    with pytest.raises(RuntimeError, match="TEXT_TO_SQL_NLU_ALLOW_FALLBACKS=1"):
        processor.extract_intent("покажи выручку по регионам")

    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")

    result = processor.extract_intent("покажи выручку по регионам")

    assert result["intent"] == "query"
    assert result["entities"]["metrics"] == ["revenue"]

    nlu_config.reset_cache()


def test_nlu_invalid_llm_response_does_not_fallback_without_opt_in(monkeypatch):
    from custom_tools.text_to_sql import nlu

    processor = NLUProcessor()
    monkeypatch.setattr(nlu, "call_openai_api", lambda **kwargs: json.dumps({"unexpected": []}))
    monkeypatch.delenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", raising=False)

    with pytest.raises(RuntimeError, match="LLM intent extraction"):
        processor.extract_intent("сколько заказов")


def test_schema_linking_wrapper_input_is_explicitly_normalized(monkeypatch):
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    # После T4.2 «revenue → amount» не является дефолтной эвристикой:
    # для этого нужен явный профиль доменных алиасов (или LLM-обогащённое
    # description колонки). Включаем muni_ru — это регрессионный safety-net
    # для пользовательского датасета.
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PROFILE", "muni_ru")
    from custom_tools.text_to_sql import column_aliases_config
    column_aliases_config.reset_cache()

    wrapper = {
        "intent": "aggregation",
        "entities": {"metrics": ["revenue"], "dimensions": ["region"], "filters": {}},
    }
    schema_info = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "description": ""},
                "region_id": {"type": "INTEGER", "description": ""},
                "amount": {"type": "DECIMAL", "description": ""},
            }
        },
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "description": ""},
                "region_name": {"type": "TEXT", "description": ""},
            }
        },
    }

    out = schema_linking(wrapper, schema_info=schema_info, dsn="sqlite:///tmp/test.db")

    assert out.get("input_warnings") == [
        "schema_linking received intent wrapper; using nested entities payload"
    ]
    assert out["linked_entities"]["metrics"]


def test_schema_linking_empty_llm_result_is_explicit_error(monkeypatch):
    memory = _FakeMemory(["orders"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
        "joins": [],
        "unlinked_entities": [],
    })
    core = SchemaLinkingCore(SchemaLimiter(), memory, llm_caller=fake_llm)

    result = core.perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert result["error"] == "LLM schema linking returned no linked entities"
    assert result["linked_entities"] == {"metrics": [], "dimensions": [], "filters": {}}


def test_schema_linking_empty_llm_result_uses_explicit_fallback(monkeypatch):
    memory = _FakeMemory(["orders"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
        "joins": [],
        "unlinked_entities": [],
    })
    core = SchemaLinkingCore(SchemaLimiter(), memory, llm_caller=fake_llm)

    result = core.perform_linking(
        {"metrics": ["amount"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert result.get("error") is None
    assert result["linked_entities"]["metrics"][0]["column"] == "amount"


def test_schema_linking_filter_only_llm_result_is_explicit_error(monkeypatch):
    memory = _FakeMemory(["orders"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {
            "metrics": [],
            "dimensions": [],
            "filters": {"status": {"table": "orders", "column": "status", "value": "paid"}},
        },
        "joins": [],
        "unlinked_entities": [],
    })
    core = SchemaLinkingCore(SchemaLimiter(), memory, llm_caller=fake_llm)

    result = core.perform_linking(
        {"metrics": [], "dimensions": [], "filters": {"status": "paid"}},
        {"orders": {"columns": {"status": {"type": "TEXT"}}}},
    )

    assert result["error"] == "LLM schema linking returned no linked entities"


def test_schema_linker_does_not_cache_empty_error_results(monkeypatch):
    linker = SchemaLinker(SchemaLimiter())
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    monkeypatch.setattr(linker.loader, "_normalize_table_names", lambda schema, dsn: schema)
    saved = {"called": False}

    class Cache:
        def prepare_cache_info(self, entities, db_schema, dsn=None):
            return {"cache_key": "k"}

        def load_from_cache(self, cache_info):
            return None

        def save_to_cache(self, cache_info, result):
            saved["called"] = True

    linker.cache_manager = Cache()
    monkeypatch.setattr(linker, "_ensure_initialized", lambda dsn=None: None)
    seen = {}

    def _perform_linking(entities, db_schema, dsn=None):
        seen["dsn"] = dsn
        return {
            "error": "LLM schema linking returned no linked entities",
            "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
            "joins": [],
            "join_success": False,
            "unconnected_tables": [],
            "main_table": None,
            "unlinked_entities": ["LLM schema linking returned no linked entities"],
        }

    monkeypatch.setattr(
        linker.linking_core,
        "perform_linking",
        _perform_linking,
    )

    result = linker.link_entities_to_schema(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
        dsn="sqlite:///tmp/test.db",
    )

    assert result["error"] == "LLM schema linking returned no linked entities"
    assert seen["dsn"] == "sqlite:///tmp/test.db"
    assert saved["called"] is False


def test_schema_linker_masks_dsn_in_schema_loading_errors(monkeypatch):
    linker = SchemaLinker(SchemaLimiter())
    raw_dsn = "postgresql://alice:topsecret@db.example.com/app?api_key=rawkey"

    monkeypatch.setattr(linker, "_ensure_initialized", lambda dsn=None: None)

    def fail_load(schema_info, dsn=None):
        raise RuntimeError(f"connect failed with {raw_dsn}")

    monkeypatch.setattr(linker.loader, "get_database_schema", fail_load)

    result = linker.link_entities_to_schema(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {},
        dsn=raw_dsn,
    )

    assert "alice:topsecret" not in result["error"]
    assert "rawkey" not in result["error"]
    assert "***:***@db.example.com" in result["error"]


def test_schema_linking_memory_unavailable_is_distinct_from_no_hits():
    class MemoryUnavailable(_FakeMemory):
        def find_semantic_relevant_tables(self, terms, dsn=None):
            self.terms = list(terms)
            self.dsn = dsn
            self.last_search_status = "memory_unavailable"
            self.last_search_error = "tactical collection is missing"
            return []

    class NoHits(_FakeMemory):
        def find_semantic_relevant_tables(self, terms, dsn=None):
            self.terms = list(terms)
            self.dsn = dsn
            self.last_search_status = "no_hits"
            self.last_search_error = None
            return []

    def must_not_call_llm(**kwargs):
        raise AssertionError("LLM must not be called without schema context")

    unavailable = SchemaLinkingCore(
        SchemaLimiter(), MemoryUnavailable(), llm_caller=must_not_call_llm
    ).llm_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )
    no_hits = SchemaLinkingCore(
        SchemaLimiter(), NoHits(), llm_caller=must_not_call_llm
    ).llm_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert unavailable["memory_status"] == "memory_unavailable"
    assert unavailable["error"].startswith("Schema memory unavailable")
    assert no_hits["memory_status"] == "no_hits"
    assert no_hits["error"] == "No semantically relevant tables found"


def test_schema_linking_cache_key_includes_linking_env(monkeypatch):
    cache = SchemaCacheManager()
    entities = {"metrics": ["revenue"], "dimensions": [], "filters": {}}
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    # DSN обязателен — SchemaCacheManager fail-fast при пустом DSN,
    # чтобы исключить cross-tenant cache leak (см. T2.4 фикс #6).
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    monkeypatch.setenv("SCHEMA_TABLE_MIN_SCORE", "0.2")
    first = cache.prepare_cache_info(entities, schema, dsn="sqlite:///tmp/test.db")
    monkeypatch.setenv("SCHEMA_TABLE_MIN_SCORE", "0.7")
    second = cache.prepare_cache_info(entities, schema, dsn="sqlite:///tmp/test.db")

    assert first["cache_key"] != second["cache_key"]
    assert first["linking_env_hash"] != second["linking_env_hash"]


def test_search_examples_passes_sqlrag_example_cache_kind(monkeypatch):
    # T8 / #17: проиндексированные sqlrag-примеры пишутся под
    # cache_kind='sqlrag_example' (indexing.py:449); 'vector_db_search' —
    # отдельный namespace кэша поисковой выдачи (retrieval.py:262), а не
    # примеры. search_examples_by_query обязан запрашивать namespace примеров,
    # иначе проиндексированные примеры не находятся вовсе.
    from custom_tools.text_to_sql import rag

    captured = {}

    def fake_get_memory(**kwargs):
        captured.update(kwargs)
        return [{"data": {"sql_query": "SELECT amount FROM orders;"}}]

    searcher = RAGSearcher()
    monkeypatch.setattr(searcher, "_ensure_sqlrag_files_indexed", lambda: None)
    monkeypatch.setattr(searcher, "_rerank_results_by_text", lambda query, items, top_k: items[:top_k])
    monkeypatch.setattr(rag, "get_memory", fake_get_memory)
    monkeypatch.setattr(rag, "memory_manager", object())
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    result = searcher.search_examples_by_query("выручка", top_k=1)

    assert captured["cache_kind"] == "sqlrag_example"
    assert captured["agent_name"] == "Schema-RAG-Agent"
    assert captured["include_historical"] is False
    assert result == [{"sql_example": "SELECT amount FROM orders;"}]


def test_llm_linking_uses_entity_values_for_semantic_search():
    memory = _FakeMemory(["orders"])

    def fake_call_openai_api(**kwargs):
        return json.dumps({
            "linked_entities": {
                "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "unlinked_entities": [],
        })

    core = SchemaLinkingCore(SchemaLimiter(), memory, llm_caller=fake_call_openai_api)

    result = core.llm_linking(
        {"metrics": ["revenue"], "dimensions": ["region"], "filters": {"year": 2024}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert memory.terms == ["revenue", "region", "year", "2024"]
    assert result["linked_entities"]["metrics"][0]["column"] == "amount"


def test_schema_limiter_limits_real_columns_in_new_schema(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    limiter = SchemaLimiter()
    schema = {
        "orders": {
            "description": "Orders table",
            "columns": {
                "id": {"type": "INTEGER", "description": "Primary key"},
                "amount": {"type": "DECIMAL", "description": "Order amount"},
            },
        }
    }

    limited = limiter.limit_schema_for_prompt(schema)

    assert limited["orders"]["description"] == "Orders table"
    assert list(limited["orders"]["columns"].keys()) == ["id"]


def test_sql_generator_validates_against_schema_info_from_context(monkeypatch):
    generator = SQLGenerator()
    captured = {}

    class CapturingValidator:
        def validate_sql_against_schema(self, sql_query, db_schema, dsn=None):
            captured["sql_query"] = sql_query
            captured["db_schema"] = db_schema
            captured["validator_dsn"] = dsn
            return {"is_valid": True, "issues": []}

    def fail_cache_lookup(*_args, **_kwargs):
        raise AssertionError("schema cache should not be used when context has schema_info")

    def fake_call_openai_api(**kwargs):
        return json.dumps({"sql": "SELECT amount FROM orders"})

    generator.schema_validator = CapturingValidator()
    monkeypatch.setattr(generator, "_get_schema_from_cache", fail_cache_lookup)
    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", fake_call_openai_api)

    context = {
        "linked_entities": {},
        "schema_info": {
            "orders": {
                "columns": {"amount": {"type": "DECIMAL", "description": ""}}
            }
        },
    }

    result = generator.generate_sql(json.dumps(context), "Доход")

    assert result["sql_query"] == "SELECT amount FROM orders"
    assert captured["db_schema"] == context["schema_info"]
    assert captured["validator_dsn"] is None


def test_sql_generator_fails_when_schema_validation_enabled_without_schema(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.delenv("TEXT_TO_SQL_VALIDATE_SCHEMA", raising=False)
    monkeypatch.setattr(generator, "_get_schema_from_cache", lambda *_args, **_kwargs: None)

    result = generator.generate_sql("{}", "Доход")

    assert result["error"] == "Schema validation is enabled, but no database schema is available"


def test_sql_generator_uses_explicit_dsn_for_schema_cache(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.delenv("TEXT_TO_SQL_VALIDATE_SCHEMA", raising=False)
    monkeypatch.setenv("DB_DSN", "sqlite:///env.db")
    captured = {}

    class CapturingValidator:
        def validate_sql_against_schema(self, sql_query, db_schema, dsn=None):
            captured["db_schema"] = db_schema
            captured["validator_dsn"] = dsn
            return {"is_valid": True, "issues": []}

    def fake_schema_from_cache(dsn=None):
        captured["dsn"] = dsn
        return {"orders": {"columns": {"id": {"type": "INTEGER"}}}}

    def fake_call_openai_api(**kwargs):
        return json.dumps({"sql": "SELECT id FROM orders"})

    generator.schema_validator = CapturingValidator()
    monkeypatch.setattr(generator, "_get_schema_from_cache", fake_schema_from_cache)
    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", fake_call_openai_api)

    result = generator.generate_sql("{}", "orders", dsn="sqlite:///explicit.db")

    assert result["sql_query"] == "SELECT id FROM orders"
    assert captured["dsn"] == "sqlite:///explicit.db"
    assert captured["validator_dsn"] == "sqlite:///explicit.db"
    assert "orders" in captured["db_schema"]


def test_sql_generator_schema_cache_does_not_use_db_dsn_env(monkeypatch, caplog):
    import memory.tools as memory_tools

    generator = SQLGenerator()
    monkeypatch.setenv("DB_DSN", "sqlite:///env.db")
    monkeypatch.setattr(
        memory_tools,
        "get_memory",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("DB_DSN fallback must not be used")),
    )

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.sql_generator"):
        schema = generator._get_schema_from_cache(None)

    assert schema is None
    assert "DSN is required" in caplog.text


def test_sql_generator_direct_dialect_path_does_not_use_db_dsn_env(monkeypatch):
    import db_plugins

    generator = SQLGenerator()
    monkeypatch.setenv("DB_DSN", "mysql://env_user:env_pass@host/env_db")
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "0")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.sql_generator.call_openai_api",
        lambda **_kwargs: json.dumps({"sql_query": "SELECT amount FROM orders"}),
    )
    monkeypatch.setattr(
        db_plugins,
        "get_plugin",
        lambda _dsn: (_ for _ in ()).throw(AssertionError("DB_DSN fallback must not be used")),
    )

    class SafeValidator:
        def validate(self, sql_query, dsn=None):
            return {"is_safe": True, "issues": []}

    generator.safety_validator = SafeValidator()
    context = json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {},
        }
    })

    result = generator.generate_sql(context, "show revenue")

    assert "error" not in result
    assert "SELECT" in result["sql_query"]


def test_schema_validator_uses_explicit_dsn_for_sqlglot_dialect(monkeypatch):
    from custom_tools.text_to_sql.validators import schema_aware

    captured = {}
    validator = SQLSchemaValidator()
    monkeypatch.setenv("DB_DSN", "mysql://env_user:env_pass@host/env_db")
    monkeypatch.setenv("USE_SQLGLOT", "1")

    def get_dialect(dsn=None, strict=False):
        captured["dialect_args"] = (dsn, strict)
        return "sqlite"

    monkeypatch.setattr(schema_aware, "get_sqlglot_dialect", get_dialect)
    monkeypatch.setattr(schema_aware, "_parse_with_timeout", lambda sql, dialect, timeout: [])

    result = validator.validate_sql_against_schema(
        "SELECT id FROM orders",
        {"orders": {"columns": {"id": {"type": "INTEGER"}}}},
        dsn="sqlite:///runtime.db",
    )

    assert result["is_valid"] is True
    assert captured["dialect_args"] == ("sqlite:///runtime.db", True)


def test_schema_linking_core_passes_explicit_dsn_to_llm_prompt(monkeypatch):
    captured = {}
    dsn = "postgresql://runtime_user:runtime_pass@db.example.com/runtime_db"

    def build_prompt(entities, schema_str, profile=None, dsn=None):
        captured["prompt_dsn"] = dsn
        return "prompt"

    def llm_caller(**kwargs):
        return json.dumps({
            "linked_entities": {
                "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "unlinked_entities": [],
        })

    monkeypatch.setenv("DB_DSN", "mysql://env_user:env_pass@host/env_db")
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linking.llm_linker.build_schema_linking_prompt",
        build_prompt,
    )
    memory = _FakeMemory(relevant_tables=["orders"])
    core = SchemaLinkingCore(
        SchemaLimiter(),
        memory,
        llm_caller=llm_caller,
    )

    result = core.perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
        dsn=dsn,
    )

    assert captured["prompt_dsn"] == dsn
    assert memory.dsn == dsn
    assert result["linked_entities"]["metrics"][0]["table"] == "orders"


def test_schema_linking_redacts_llm_exception_boundary(monkeypatch, caplog):
    odbc_connect = urllib.parse.quote_plus(
        "DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret"
    )
    raw_error_dsn = f"mssql+pyodbc:///?odbc_connect={odbc_connect}&driver=ODBC+Driver+17"

    def fail_llm(**kwargs):
        raise RuntimeError(f"connect failed {raw_error_dsn} person@example.com")

    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    core = SchemaLinkingCore(
        SchemaLimiter(),
        _FakeMemory(relevant_tables=["orders"]),
        llm_caller=fail_llm,
    )

    with caplog.at_level(logging.WARNING):
        result = core.perform_linking(
            {"metrics": ["revenue"], "dimensions": [], "filters": {}},
            {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
            dsn="sqlite:///runtime.db",
        )

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_schema_memory_search_status_redacts_raw_memory_error():
    manager = object.__new__(SchemaMemoryManager)
    raw_dsn = _raw_pyodbc_dsn()

    manager._set_search_status(
        "memory_unavailable",
        f"semantic search failed for {raw_dsn} person@example.com",
    )

    assert manager.last_search_status == "memory_unavailable"
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in manager.last_search_error
    assert "odbc_connect=***" in manager.last_search_error
    assert "[EMAIL]" in manager.last_search_error


def test_schema_memory_search_requires_explicit_or_runtime_dsn(monkeypatch, tmp_path):
    import memory.manager as memory_manager_module

    manager = SchemaMemoryManager(tmp_path)
    fake_memory_manager = SimpleNamespace(get_tactical_collection=lambda: object())
    monkeypatch.setattr(memory_manager_module, "memory_manager", fake_memory_manager)
    monkeypatch.setenv("DB_DSN", "sqlite:///env-tenant.db")

    result = manager.find_semantic_relevant_tables(["orders"], dsn=None)

    assert result == []
    assert manager.last_search_status == "memory_unavailable"
    assert "runtime DSN is required" in manager.last_search_error


def test_text_to_sql_prompt_builders_redact_user_controlled_values(monkeypatch):
    from custom_tools.text_to_sql.prompts import (
        build_column_description_prompt_with_context,
        build_schema_linking_prompt,
        build_sql_safety_prompt,
    )

    raw_dsn = _raw_pyodbc_dsn()
    raw_odbc_keyword_dsn = "DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret"
    raw_libpq_dsn = "host=db.example.com user=alice password=topsecret dbname=prod"
    entities = {
        "metrics": ["revenue"],
        "filters": {
            "email": "person@example.com",
            "dsn": raw_dsn,
            "person@example.com": raw_libpq_dsn,
        },
        f"lookup-{raw_odbc_keyword_dsn}": {
            "postgresql://alice:topsecret@db.example.com/prod": "person@example.com",
        },
    }
    schema_str = (
        f"orders(email text, dsn text default '{raw_dsn}', "
        f"odbc text default '{raw_odbc_keyword_dsn}', libpq text default '{raw_libpq_dsn}')"
    )

    schema_prompt = build_schema_linking_prompt(
        entities,
        schema_str,
        dsn="sqlite:///runtime.db",
    )
    safety_prompt = build_sql_safety_prompt(
        (
            "SELECT * FROM orders WHERE email='person@example.com' "
            f"AND dsn='{raw_dsn}' AND odbc='{raw_odbc_keyword_dsn}' "
            f"AND libpq='{raw_libpq_dsn}'"
        ),
        dsn="sqlite:///runtime.db",
    )
    column_prompt = build_column_description_prompt_with_context(
        {"orders": {"columns": {"email": {"type": "TEXT", "description": ""}}}},
        {"email": {"type": "TEXT"}},
        ["orders"],
        sample_data=[{"email": "person@example.com"}],
        column_stats={"email": {"sample_values": ["person@example.com"]}},
        fk_previews={
            "email": {
                "ref_table": "users",
                "preview_columns": ["email"],
                "preview_data": [("person@example.com",)],
            }
        },
    )
    serialized = "\n".join([schema_prompt, safety_prompt, column_prompt])

    for raw_fragment in (
        "UID=alice",
        "PWD=topsecret",
        "user=alice",
        "password=topsecret",
        "alice",
        "topsecret",
        "person@example.com",
    ):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_text_to_sql_redaction_sanitizes_dict_keys_across_helpers():
    from custom_tools.text_to_sql.core._sql_generation_api import _redact_sql_api_value
    from custom_tools.text_to_sql.prompts import _redact_prompt_value
    from custom_tools.text_to_sql.sql_generator import _redact_sql_generation_value

    value = {
        "postgresql://alice:topsecret@db.example.com/prod": {
            "person@example.com": "host=db.example.com user=alice password=topsecret dbname=prod",
        },
        "DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret": "ok",
    }

    for helper in (_redact_prompt_value, _redact_sql_generation_value, _redact_sql_api_value):
        serialized = json.dumps(helper(value), ensure_ascii=False)
        for raw_fragment in (
            "alice",
            "topsecret",
            "person@example.com",
            "UID=alice",
            "PWD=topsecret",
            "user=alice",
            "password=topsecret",
        ):
            assert raw_fragment not in serialized
        assert "[EMAIL]" in serialized


def test_sql_generator_context_preview_redacts_runtime_context(caplog, monkeypatch):
    raw_dsn = _raw_pyodbc_dsn()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = json.dumps(
        {
            "dsn": raw_dsn,
            "note": "contact person@example.com +7 (495) 123-45-67",
            "linked_entities": {},
        },
        ensure_ascii=False,
    )

    with caplog.at_level(logging.INFO, logger="custom_tools.text_to_sql.sql_generator"):
        result = SQLGenerator().generate_sql(context, "show revenue", dsn="sqlite:///runtime.db")

    assert result["error"] == "Structured SQL builder is enabled, but structured context is missing or unsupported."
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com", "+7 (495) 123-45-67"):
        assert raw_fragment not in caplog.text
    assert '"dsn": "***"' in caplog.text
    assert "[EMAIL]" in caplog.text
    assert "[PHONE]" in caplog.text


def test_sql_generator_direct_prompt_redacts_runtime_context(monkeypatch):
    raw_dsn = _raw_pyodbc_dsn()
    captured = {}

    def fake_call_openai_api(**kwargs):
        captured.update(kwargs)
        return json.dumps({"sql_query": "SELECT 1"})

    monkeypatch.setenv("TEXT_TO_SQL_MAX_RETRIES", "1")
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "0")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.sql_generator.call_openai_api",
        fake_call_openai_api,
    )

    user_query = (
        f"show revenue for person@example.com with dsn {raw_dsn} "
        "+7 (495) 123-45-67"
    )
    context = (
        f"dsn={raw_dsn}; contact person@example.com +7 (495) 123-45-67; "
        "table orders(id int)"
    )
    result = SQLGenerator().generate_sql(context, user_query, dsn="sqlite:///runtime.db")

    prompt = captured["prompt"]
    assert result["sql_query"] == "SELECT 1"
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com", "+7 (495) 123-45-67"):
        assert raw_fragment not in prompt
    assert "dsn=***" in prompt
    assert "[EMAIL]" in prompt
    assert "[PHONE]" in prompt


def test_sql_generator_safety_error_redacts_issues(monkeypatch, caplog):
    raw_dsn = _raw_pyodbc_dsn()
    generator = SQLGenerator()

    class UnsafeValidator:
        def validate(self, sql_query, dsn=None):
            return {
                "is_safe": False,
                "issues": [
                    {
                        "issue_type": "SQL_PARSE_ERROR",
                        "description": f"Failed to parse SQL: {raw_dsn} person@example.com",
                    }
                ],
            }

    generator.safety_validator = UnsafeValidator()

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.sql_generator"):
        result = generator._apply_safety_validation("SELECT 1", dsn="sqlite:///runtime.db")

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_sql_postprocess_redacts_parser_error(monkeypatch):
    from custom_tools.text_to_sql import sql_postprocess

    raw_dsn = _raw_pyodbc_dsn()

    def fail_quote(sql_query, linked_entities, dialect):
        raise ValueError(f"parse failed {raw_dsn} person@example.com")

    monkeypatch.setattr(sql_postprocess, "quote_via_ast", fail_quote)
    monkeypatch.delenv("SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK", raising=False)

    with pytest.raises(sql_postprocess.SQLPostprocessError) as exc_info:
        sql_postprocess.apply_dialect_quoting(
            "SELECT amount FROM orders",
            {"metrics": [{"table": "orders", "column": "amount"}]},
            dsn="sqlite:///runtime.db",
        )

    serialized = str(exc_info.value)
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_sql_postprocess_manual_quoting_redacts_parser_error(monkeypatch):
    from custom_tools.text_to_sql import sql_postprocess

    raw_dsn = _raw_pyodbc_dsn()

    def fail_quote(sql_query, linked_entities, dialect):
        raise ValueError(f"parse failed {raw_dsn} person@example.com")

    monkeypatch.setattr(sql_postprocess, "quote_via_ast", fail_quote)

    with pytest.raises(sql_postprocess.SQLPostprocessError) as exc_info:
        sql_postprocess.apply_manual_quoting(
            "SELECT amount FROM orders",
            {"metrics": [{"table": "orders", "column": "amount"}]},
            dsn="sqlite:///runtime.db",
        )

    serialized = str(exc_info.value)
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_sql_postprocess_quote_logs_redacted_sql(monkeypatch, caplog):
    from custom_tools.text_to_sql import sql_postprocess

    raw_dsn = _raw_pyodbc_dsn()
    sql_query = f"SELECT '{raw_dsn}' AS dsn, 'person@example.com' AS email"

    monkeypatch.setattr("sqlglot.parse", lambda *_args, **_kwargs: [])

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.sql_postprocess"):
        with pytest.raises(RuntimeError, match="empty AST"):
            sql_postprocess.quote_via_ast(sql_query, {}, "sqlite")

    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in caplog.text
    assert "odbc_connect=***" in caplog.text
    assert "[EMAIL]" in caplog.text


def test_sql_generator_redacts_postprocess_error(monkeypatch, caplog):
    from custom_tools.text_to_sql import sql_postprocess

    raw_dsn = _raw_pyodbc_dsn()

    def fake_call_openai_api(**kwargs):
        return json.dumps({"sql_query": "SELECT amount FROM orders"})

    def fail_quote(sql_query, linked_entities, dialect):
        raise ValueError(f"parse failed {raw_dsn} person@example.com")

    monkeypatch.setenv("TEXT_TO_SQL_MAX_RETRIES", "1")
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "0")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.sql_generator.call_openai_api",
        fake_call_openai_api,
    )
    monkeypatch.setattr(sql_postprocess, "quote_via_ast", fail_quote)

    context = json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {},
        }
    })

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.sql_generator"):
        result = SQLGenerator().generate_sql(context, "show revenue", dsn="sqlite:///runtime.db")

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_schema_validator_redacts_parse_exception(monkeypatch, caplog):
    from custom_tools.text_to_sql.validators import schema_aware

    raw_dsn = _raw_pyodbc_dsn()

    def fail_parse(sql, dialect, timeout):
        raise ValueError(f"parse failed {raw_dsn} person@example.com")

    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(schema_aware, "_parse_with_timeout", fail_parse)

    with caplog.at_level(logging.ERROR, logger="custom_tools.text_to_sql.validators.schema_aware"):
        result = SQLSchemaValidator().validate_sql_against_schema(
            "SELECT id FROM orders",
            {"orders": {"columns": {"id": {"type": "INTEGER"}}}},
            dsn="sqlite:///runtime.db",
        )

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    assert result["is_valid"] is False
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_sql_safety_validator_redacts_parse_exception(monkeypatch, caplog):
    from custom_tools.text_to_sql.validators import safety

    raw_dsn = _raw_pyodbc_dsn()

    def fail_parse(sql, dialect, timeout):
        raise ValueError(f"parse failed {raw_dsn} person@example.com")

    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(safety, "_parse_with_timeout", fail_parse)

    with caplog.at_level(logging.ERROR, logger="custom_tools.text_to_sql.validators.safety"):
        result = safety.SQLSafetyValidator().validate("SELECT id FROM orders", dsn="sqlite:///runtime.db")

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    assert result["is_safe"] is False
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_structured_sql_builder_supports_filters_and_aggregation(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@localhost:5432/sales.analytics")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "avg_amount", "table": "orders", "column": "amount", "aggregation": "avg"}
            ],
            "dimensions": [
                {"name": "region", "table": "orders", "column": "region"}
            ],
            "filters": {
                "date_range": {
                    "table": "orders",
                    "column": "created_at",
                    "value": {"start": "2024-01-01", "end": "2024-12-31"},
                },
                "status": {
                    "table": "orders",
                    "column": "status",
                    "value": "paid",
                },
            },
        },
        "joins": [],
        "schema_info": {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "region": {"type": "TEXT"},
                    "created_at": {"type": "DATE"},
                    "status": {"type": "TEXT"},
                }
            }
        },
    }

    result = generator.generate_sql(json.dumps(context), "средний чек по регионам за 2024")
    sql = result["sql_query"]

    assert 'AVG("orders"."amount") AS "avg_amount"' in sql
    assert 'WHERE "orders"."created_at" >= ' in sql
    assert '"orders"."created_at" <= ' in sql
    assert '"orders"."status" = ' in sql
    assert "GROUP BY" in sql


def test_structured_sql_builder_joins_from_connected_side(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@localhost:5432/sales.analytics")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "dimensions": [
                {"name": "region", "table": "regions", "column": "name"}
            ],
        },
        "joins": [
            {
                "from_table": "regions",
                "from_column": "id",
                "to_table": "orders",
                "to_column": "region_id",
                "join_type": "LEFT",
            }
        ],
        "schema_info": {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "region_id": {"type": "INTEGER"},
                }
            },
            "regions": {
                "columns": {
                    "id": {"type": "INTEGER"},
                    "name": {"type": "TEXT"},
                }
            },
        },
    }

    result = generator.generate_sql(json.dumps(context), "выручка по регионам")
    sql = result["sql_query"]

    assert 'FROM "orders"' in sql
    assert 'LEFT JOIN "regions" ON "regions"."id" = "orders"."region_id"' in sql
    assert 'LEFT JOIN "orders"' not in sql


def test_structured_sql_builder_rejects_unknown_join_type(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [{"name": "amount", "table": "orders", "column": "amount", "aggregation": "sum"}],
            "dimensions": [{"name": "region", "table": "regions", "column": "name"}],
        },
        "joins": [
            {
                "from_table": "orders",
                "from_column": "region_id",
                "to_table": "regions",
                "to_column": "id",
                "join_type": "SIDEWAYS",
            }
        ],
    }

    result = generator.generate_sql(json.dumps(context), "выручка по регионам")

    assert "unsupported join_type" in result["error"]


def test_structured_sql_builder_does_not_fallback_to_llm_when_enabled(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.sql_generator.call_openai_api",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM fallback must not run")),
    )

    result = generator.generate_sql(json.dumps({"linked_entities": {}}), "show orders")

    assert "Structured SQL builder is enabled" in result["error"]


def test_structured_sql_builder_rejects_unsupported_dict_filter(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "filters": {
                "status": {
                    "table": "orders",
                    "column": "status",
                    "value": {"equals": "paid"},
                }
            },
        },
        "schema_info": {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "status": {"type": "TEXT"},
                }
            }
        },
    }

    result = generator.generate_sql(json.dumps(context), "paid amount")

    assert result["error"] == "Structured SQL builder received unsupported filter operator"
    assert "sql_query" not in result


def test_structured_sql_builder_rejects_filter_without_value(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "filters": {
                "status": {
                    "table": "orders",
                    "column": "status",
                }
            },
        },
        "schema_info": {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "status": {"type": "TEXT"},
                }
            }
        },
    }

    result = generator.generate_sql(json.dumps(context), "paid amount")

    assert result["error"] == "Structured SQL builder filter must include value"
    assert "sql_query" not in result


def test_structured_sql_builder_rejects_nested_operator_without_value(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "filters": {
                "status": {
                    "table": "orders",
                    "column": "status",
                    "value": {"operator": "="},
                }
            },
        },
        "schema_info": {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "status": {"type": "TEXT"},
                }
            }
        },
    }

    result = generator.generate_sql(json.dumps(context), "paid amount")

    assert result["error"] == "Structured SQL builder received unsupported filter operator"
    assert "sql_query" not in result


def test_structured_sql_builder_rejects_filter_on_unjoined_table(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "filters": {
                "customer_status": {
                    "table": "customers",
                    "column": "status",
                    "value": "vip",
                }
            },
        },
    }

    result = generator.generate_sql(json.dumps(context), "vip customer amount")

    assert result["error"] == "Structured SQL builder filter table is not connected to the FROM table"
    assert result["table"] == "customers"


def test_structured_sql_builder_rejects_entity_table_without_join(monkeypatch):
    generator = SQLGenerator()
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")

    context = {
        "linked_entities": {
            "metrics": [
                {"name": "total_amount", "table": "orders", "column": "amount", "aggregation": "sum"}
            ],
            "dimensions": [
                {"name": "region", "table": "regions", "column": "name"}
            ],
        },
    }

    result = generator.generate_sql(json.dumps(context), "amount by region")

    assert result["error"] == "Structured SQL builder entity tables are not connected to the FROM table"
    assert result["tables"] == ["regions"]


def test_sql_generator_dialect_quoting_fails_closed_when_sqlglot_enabled(monkeypatch):
    import sqlglot

    generator = SQLGenerator()
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.delenv("SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK", raising=False)
    monkeypatch.setattr(sqlglot, "parse", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad sql")))

    with pytest.raises(RuntimeError, match="SQLGlot dialect quoting failed"):
        generator._apply_dialect_quoting(
            "SELECT amount FROM orders",
            {"metrics": [{"table": "orders", "column": "amount"}]},
        )


def test_schema_validator_checks_aggregate_arguments():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema("SELECT SUM(missing_amount) FROM orders", schema)

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])


def test_schema_validator_flags_ambiguous_unqualified_columns():
    validator = SQLSchemaValidator()
    schema = {
        "orders": {"columns": {"id": {"type": "INTEGER"}, "customer_id": {"type": "INTEGER"}}},
        "customers": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema(
        "SELECT id FROM orders JOIN customers ON orders.customer_id = customers.id",
        schema,
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "AMBIGUOUS_COLUMN" for issue in result["issues"])


def test_schema_validator_accepts_short_names_for_qualified_schema():
    validator = SQLSchemaValidator()
    schema = {"public.orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema("SELECT orders.amount FROM orders", schema)

    assert result["is_valid"] is True


def test_schema_validator_rejects_qualified_column_not_in_from():
    validator = SQLSchemaValidator()
    schema = {
        "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
        "customers": {"columns": {"name": {"type": "TEXT"}}},
    }

    result = validator.validate_sql_against_schema("SELECT orders.amount FROM customers", schema)

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_TABLE_REFERENCE" for issue in result["issues"])


def test_schema_validator_does_not_leak_aliases_from_subqueries():
    validator = SQLSchemaValidator()
    schema = {
        "orders": {
            "columns": {
                "amount": {"type": "DECIMAL"},
                "customer_id": {"type": "INTEGER"},
            }
        },
        "customers": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema(
        "SELECT orders.amount FROM customers "
        "WHERE EXISTS (SELECT 1 FROM orders WHERE orders.customer_id = customers.id)",
        schema,
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_TABLE_REFERENCE" for issue in result["issues"])


def test_schema_validator_resolves_unqualified_columns_in_current_scope_before_outer():
    validator = SQLSchemaValidator()
    schema = {
        "orders": {"columns": {"id": {"type": "INTEGER"}, "customer_id": {"type": "INTEGER"}}},
        "customers": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema(
        "SELECT customers.id FROM customers "
        "WHERE EXISTS (SELECT 1 FROM orders WHERE id = customers.id)",
        schema,
    )

    assert result["is_valid"] is True


def test_schema_validator_accepts_cte_and_derived_table_alias_columns():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    cte_result = validator.validate_sql_against_schema(
        "WITH sub AS (SELECT amount FROM orders) SELECT sub.amount FROM sub",
        schema,
    )
    derived_result = validator.validate_sql_against_schema(
        "SELECT sub.amount FROM (SELECT amount FROM orders) AS sub",
        schema,
    )

    assert cte_result["is_valid"] is True
    assert derived_result["is_valid"] is True


def test_schema_validator_validates_columns_named_like_aggregates():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema("SELECT orders.sum FROM orders", schema)

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])


def test_schema_validator_does_not_treat_where_column_as_select_alias():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema("SELECT amount AS id FROM orders WHERE id > 1", schema)

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])


def test_schema_validator_does_not_leak_outer_aliases_into_derived_table_body():
    validator = SQLSchemaValidator()
    schema = {
        "orders": {"columns": {"id": {"type": "INTEGER"}}},
        "customers": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema(
        "SELECT sub.id FROM customers, (SELECT customers.id FROM orders) AS sub",
        schema,
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_TABLE_REFERENCE" for issue in result["issues"])


def test_schema_validator_accepts_explicit_cte_and_derived_column_alias_lists():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    cte_result = validator.validate_sql_against_schema(
        "WITH sub(total) AS (SELECT amount FROM orders) SELECT sub.total FROM sub",
        schema,
    )
    derived_result = validator.validate_sql_against_schema(
        "SELECT sub.total FROM (SELECT amount FROM orders) AS sub(total)",
        schema,
    )

    assert cte_result["is_valid"] is True
    assert derived_result["is_valid"] is True


def test_schema_validator_expands_star_in_derived_row_source():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    valid_result = validator.validate_sql_against_schema(
        "SELECT sub.amount FROM (SELECT * FROM orders) AS sub",
        schema,
    )
    invalid_result = validator.validate_sql_against_schema(
        "SELECT sub.missing FROM (SELECT * FROM orders) AS sub",
        schema,
    )

    assert valid_result["is_valid"] is True
    assert invalid_result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in invalid_result["issues"])


def test_schema_validator_expands_qualified_star_in_derived_row_source():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    valid_result = validator.validate_sql_against_schema(
        "SELECT sub.amount FROM (SELECT orders.* FROM orders) AS sub",
        schema,
    )
    invalid_result = validator.validate_sql_against_schema(
        "SELECT sub.missing FROM (SELECT orders.* FROM orders) AS sub",
        schema,
    )

    assert valid_result["is_valid"] is True
    assert invalid_result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in invalid_result["issues"])


def test_schema_linking_requires_resolved_table_and_column_binding():
    core = SchemaLinkingCore(schema_limiter=None, memory_manager=None)

    assert core._has_linked_entities({"metrics": [{"name": "revenue"}]}) is False
    assert core._has_linked_entities({"dimensions": [{"table": "orders", "column": "region"}]}) is True


def test_schema_linking_single_table_without_joins_is_success(monkeypatch):
    memory = _FakeMemory(["orders"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {},
        },
        "joins": [],
        "unlinked_entities": [],
    })

    result = SchemaLinkingCore(
        SchemaLimiter(), memory, llm_caller=fake_llm
    ).perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {}},
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert result["error"] is None
    assert result["join_success"] is True


def test_schema_linking_filter_table_counts_for_join_success(monkeypatch):
    memory = _FakeMemory(["orders", "customers"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {"status": {"table": "customers", "column": "status", "value": "vip"}},
        },
        "joins": [],
        "unlinked_entities": [],
    })

    result = SchemaLinkingCore(
        SchemaLimiter(), memory, llm_caller=fake_llm
    ).perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {"status": "vip"}},
        {
            "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
            "customers": {"columns": {"status": {"type": "TEXT"}}},
        },
    )

    assert result["error"] is None
    assert result["join_success"] is False


def test_schema_linking_llm_join_success_checks_filter_table_connectivity(monkeypatch):
    memory = _FakeMemory(["orders", "customers", "regions"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.delenv("SCHEMA_LINKING_ALLOW_FALLBACKS", raising=False)
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {"status": {"table": "customers", "column": "status", "value": "vip"}},
        },
        "joins": [
            {
                "from_table": "orders",
                "from_column": "region_id",
                "to_table": "regions",
                "to_column": "id",
                "join_type": "LEFT",
            }
        ],
        "unlinked_entities": [],
    })

    result = SchemaLinkingCore(
        SchemaLimiter(), memory, llm_caller=fake_llm
    ).perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {"status": "vip"}},
        {
            "orders": {
                "columns": {
                    "amount": {"type": "DECIMAL"},
                    "region_id": {"type": "INTEGER", "constraint_type": "FK", "references": "regions(id)"},
                }
            },
            "regions": {"columns": {"id": {"type": "INTEGER"}}},
            "customers": {"columns": {"status": {"type": "TEXT"}}},
        },
    )

    assert result["join_success"] is False
    assert result["unconnected_tables"] == ["customers"]


def test_schema_linking_without_joins_reports_unconnected_filter_tables(monkeypatch):
    memory = _FakeMemory(["orders", "customers"])
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "0")
    fake_llm = lambda **kwargs: json.dumps({
        "linked_entities": {
            "metrics": [{"name": "revenue", "table": "orders", "column": "amount"}],
            "dimensions": [],
            "filters": {"status": {"table": "customers", "column": "status", "value": "vip"}},
        },
        "joins": [],
        "unlinked_entities": [],
    })

    result = SchemaLinkingCore(
        SchemaLimiter(), memory, llm_caller=fake_llm
    ).perform_linking(
        {"metrics": ["revenue"], "dimensions": [], "filters": {"status": "vip"}},
        {
            "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
            "customers": {"columns": {"status": {"type": "TEXT"}}},
        },
    )

    assert result["join_success"] is False
    assert result["unconnected_tables"] == ["customers"]


def test_schema_info_is_cache_only_by_default(monkeypatch):
    dsn = "sqlite:///tmp/test.db"
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/stale.db")
    monkeypatch.delenv("TEXT_TO_SQL_SCHEMA_INFO_ALLOW_INTROSPECTION", raising=False)
    monkeypatch.setattr("custom_tools.text_to_sql.schema_loader.SchemaLoader._load_sqlrag_schema", lambda self, dsn: None)

    result = sql_tools.schema_info("orders", dsn=dsn)

    assert result["success"] is False
    assert "Schema cache file not found" in result["error_message"]


def test_schema_info_live_introspection_is_explicit_opt_in(monkeypatch):
    called = {"loader": False}

    def fake_get_database_schema(self, schema_info, dsn=None):
        called["loader"] = True
        called["dsn"] = dsn
        return {
            "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
            "archive.orders": {"columns": {"id": {"type": "INTEGER"}}},
        }

    dsn = "sqlite:///tmp/test.db"
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/stale.db")
    monkeypatch.setenv("TEXT_TO_SQL_SCHEMA_INFO_ALLOW_INTROSPECTION", "1")
    monkeypatch.setattr("custom_tools.text_to_sql.schema_loader.SchemaLoader._load_sqlrag_schema", lambda self, dsn: None)
    monkeypatch.setattr(SchemaLinker, "_get_database_schema", fake_get_database_schema)

    result = sql_tools.schema_info("orders", dsn=dsn)

    assert called["loader"] is True
    assert called["dsn"] == dsn
    assert result["success"] is False
    assert "Ambiguous table name" in result["error_message"]
    assert "public.orders" in result["error_message"]
    assert "archive.orders" in result["error_message"]


def test_secure_db_executor_dry_run_skips_database_connection(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.delenv("DB_DSN", raising=False)
    # EPIC 1.9: LLM-аудит fail-fast. Stub call_openai_api на пустой issues-набор,
    # иначе sql_safety_check вернёт is_safe=False и executor не дойдёт до dry-run.
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **kwargs: '{"issues": []}',
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.get_plugin",
        lambda dsn: (_ for _ in ()).throw(AssertionError("database must not be opened in dry run")),
    )

    result = secure_db_executor("SELECT 1")

    assert result["success"] is True
    assert result["dry_run_only"] is True
    assert result["skipped_execution"] is True
    assert result["data"] == []


def test_secure_db_executor_dry_run_passes_empty_dsn_sentinel(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://env_user:env_pass@db.example.com/env_db")
    seen = {}

    def fake_safety(sql_query, dsn=None):
        seen["dsn"] = dsn
        return {"is_safe": True, "issues": []}

    monkeypatch.setattr("custom_tools.text_to_sql.core.sql_safety_check", fake_safety)
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.get_plugin",
        lambda dsn: (_ for _ in ()).throw(AssertionError("database must not be opened in dry run")),
    )

    result = secure_db_executor("SELECT 1")

    assert result["success"] is True
    assert result["dry_run_only"] is True
    assert seen["dsn"] == ""


def test_secure_db_executor_invalid_dry_run_env_fails_fast(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "maybe")
    monkeypatch.setenv("USE_SQLGLOT", "1")

    with pytest.raises(ValueError, match="TEXT_TO_SQL_DRY_RUN_ONLY"):
        secure_db_executor("SELECT 1", dsn="sqlite:///tmp/test.db")


def test_secure_db_executor_invalid_env_row_limit_returns_contract(monkeypatch):
    monkeypatch.setenv("DB_EXECUTOR_ROW_LIMIT", "not-an-int")

    result = secure_db_executor("SELECT 1")

    assert result["success"] is False
    assert result["data"] == []
    assert result["columns"] == []
    assert result["rows_affected"] == 0
    assert "Invalid DB_EXECUTOR_ROW_LIMIT" in result["error_message"]


def test_secure_db_executor_rejects_non_positive_row_limit_before_dry_run(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("DB_EXECUTOR_ROW_LIMIT", "0")

    result = secure_db_executor("SELECT 1")

    assert result["success"] is False
    assert result["dry_run_only"] is False
    assert "row_limit must be a positive integer" in result["error_message"]


def test_secure_db_executor_describe_resolves_unique_short_table(monkeypatch):
    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def introspect_schema(self, conn, schema=None, table_name=None):
            assert table_name == "orders"
            return {"public.orders": {"columns": {"id": {"type": "INTEGER"}}}}

    dsn = "postgresql://user:pass@example.com/db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kwargs: '{"issues": []}')

    result = secure_db_executor("DESCRIBE orders", dsn=dsn)

    assert result["success"] is True
    assert result["rows_affected"] == 1
    assert result["sql_query"] == "DESCRIBE orders"
    assert result["applied_row_limit"] is not None


def test_secure_db_executor_describe_reports_ambiguous_short_table(monkeypatch):
    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def introspect_schema(self, conn, schema=None, table_name=None):
            return {
                "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
                "archive.orders": {"columns": {"id": {"type": "INTEGER"}}},
            }

    dsn = "postgresql://user:pass@example.com/db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kwargs: '{"issues": []}')

    result = secure_db_executor("DESCRIBE orders", dsn=dsn)

    assert result["success"] is False
    assert "Ambiguous table name" in result["error_message"]


def test_secure_db_executor_describe_qualified_table_not_ambiguous(monkeypatch):
    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def introspect_schema(self, conn, schema=None, table_name=None):
            assert schema == "public"
            assert table_name == "orders"
            return {
                "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
                "archive.orders": {"columns": {"id": {"type": "INTEGER"}}},
            }

    dsn = "postgresql://user:pass@example.com/db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kwargs: '{"issues": []}')

    result = secure_db_executor("DESCRIBE public.orders", dsn=dsn)

    assert result["success"] is True
    assert result["rows_affected"] == 1


def test_secure_db_executor_explain_requires_plugin_support(monkeypatch):
    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

    dsn = "sqlite:///tmp/app.db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kwargs: '{"issues": []}')

    result = secure_db_executor("EXPLAIN SELECT 1", dsn=dsn)

    assert result["success"] is False
    assert result["error_message"] == "EXPLAIN requires plugin explain support"


def test_secure_db_executor_select_normalizes_output_contract(monkeypatch):
    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def execute_select(self, conn, sql, row_limit=500):
            return {
                "success": True,
                "data": [(1, None)],
                "columns": ["id", "optional"],
                "rows_affected": 1,
                "execution_time_ms": 3,
                "error_message": None,
            }

    dsn = "sqlite:///tmp/app.db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kwargs: '{"issues": []}')

    result = secure_db_executor("SELECT id, optional FROM orders", row_limit=5, dsn=dsn)

    assert result["success"] is True
    assert result["data"] == [[1, None]]
    assert result["safety_issues"] == []
    assert result["dry_run_only"] is False
    assert result["skipped_execution"] is False
    assert result["sql_query"] == "SELECT id, optional FROM orders"
    assert result["applied_row_limit"] == 5


def test_sql_generator_returns_error_when_dialect_quoting_fails_after_retries(monkeypatch):
    generator = SQLGenerator()
    generator.max_retries = 2
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")
    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", lambda **kwargs: "{}")
    monkeypatch.setattr(
        generator,
        "_llm_generation_direct",
        lambda context, user_query, attempt: {"sql_query": "SELECT amount FROM orders"},
    )
    monkeypatch.setattr(
        generator,
        "_apply_dialect_quoting",
        lambda sql, linked_entities: (_ for _ in ()).throw(RuntimeError("SQLGlot dialect quoting failed: bad sql")),
    )

    result = generator.generate_sql(
        json.dumps({"linked_entities": {"metrics": [{"table": "orders", "column": "amount"}]}}),
        "amount",
    )

    assert result["error"] == "SQLGlot dialect quoting failed: bad sql"
    assert result["sql_query"] == "SELECT amount FROM orders"


def test_audit_logger_uses_repository_root_parents_2(monkeypatch, tmp_path):
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    result = audit_logger({"session_id": "s1", "action": "select"})

    audit_path = tmp_path / "repo" / "logs" / "audit.log"
    assert result["status"] == "logged"
    assert audit_path.exists()
    assert not (tmp_path / "logs" / "audit.log").exists()


def test_purge_schema_linking_rag_cache_deletes_only_collected_chroma_ids(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE agent_memory (
            session_id TEXT,
            agent_name TEXT,
            step INTEGER,
            data TEXT,
            valid_to TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Schema-RAG-Agent",
            7,
            json.dumps({"cache_source": "schema_linking", "cache_kind": "schema_linking"}),
        ),
    )
    conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Schema-RAG-Agent",
            8,
            json.dumps({"cache_source": "schema_metadata", "cache_kind": "schema_ready"}),
        ),
    )
    conn.commit()

    class TacticalCollection:
        def __init__(self):
            self.deleted_ids = None

        def get(self, where=None):
            raise AssertionError("purge must not re-query broad Chroma metadata")

        def delete(self, ids):
            self.deleted_ids = list(ids)

    tactical = TacticalCollection()

    class DBHandler:
        tactical_collection = tactical

        def get_connection(self):
            return conn

    monkeypatch.setattr(core_module, "memory_manager", SimpleNamespace(db_handler=DBHandler()))

    count = purge_schema_linking_rag_cache(session_id="s1")

    assert count == 1
    assert tactical.deleted_ids == ["s1-Schema-RAG-Agent-7"]


def test_schema_autosave_disabled_still_indexes_memory_without_json(monkeypatch, tmp_path):
    captured = {}
    memory_tools = SimpleNamespace(save_memory=lambda **kwargs: None, get_memory=lambda **kwargs: [])
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)
    monkeypatch.setenv("SCHEMA_AUTOSAVE", "0")

    manager = SchemaMemoryManager(tmp_path)
    monkeypatch.setattr(manager, "is_schema_indexed", lambda session_id, file_hash, **kwargs: False)
    monkeypatch.setattr(manager, "remove_old_schema_records", lambda session_id, filename: None)
    monkeypatch.setattr(
        manager,
        "index_schema_in_memory",
        lambda session_id, filename, db_schema, file_hash: captured.update({
            "session_id": session_id,
            "filename": filename,
            "db_schema": db_schema,
            "file_hash": file_hash,
        }),
    )
    schema = {"orders": {"columns": {"id": {"type": "INTEGER"}}}}

    indexed = manager.ensure_schema_indexed_in_memory("sqlite:///tmp/no-file.db", schema)

    assert captured["db_schema"] == schema
    assert captured["filename"].endswith(".json")
    assert not (tmp_path / "sqlrag").exists()
    assert indexed is False


def test_schema_memory_returns_true_for_already_indexed_schema(monkeypatch, tmp_path):
    memory_tools = SimpleNamespace(save_memory=lambda **kwargs: None, get_memory=lambda **kwargs: [])
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)
    manager = SchemaMemoryManager(tmp_path)
    session_id = "sqlite_tmp_app_db"
    sqlrag_dir = tmp_path / "sqlrag"
    sqlrag_dir.mkdir()
    (sqlrag_dir / f"{session_id}.json").write_text(
        json.dumps({"enable": True, "schema_info": {"orders": {"columns": {"id": {"type": "INTEGER"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "is_schema_indexed", lambda session_id, file_hash, **kwargs: True)
    monkeypatch.setattr(
        manager,
        "index_schema_in_memory",
        lambda session_id, filename, db_schema, file_hash: (_ for _ in ()).throw(AssertionError("must not reindex")),
    )

    indexed = manager.ensure_schema_indexed_in_memory("sqlite:///tmp/app.db", {"orders": {"columns": {}}})

    assert indexed is True


def test_sql_explain_dry_run_skips_database_connection(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.delenv("DB_DSN", raising=False)
    # EPIC 1.9: чтобы проверять именно dry-run shortcut (а не cascade unsafe
    # из-за упавшего LLM-аудита), stub call_openai_api на валидный JSON.
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **kwargs: '{"issues": []}',
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.get_plugin",
        lambda dsn: (_ for _ in ()).throw(AssertionError("database must not be opened in dry run")),
    )

    result = sql_explain("SELECT 1")

    assert result["dry_run_only"] is True
    assert result["skipped_execution"] is True
    assert result["plan"] is None


def test_sql_explain_dry_run_passes_empty_dsn_sentinel(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://env_user:env_pass@db.example.com/env_db")
    seen = {}

    def fake_safety(sql_query, dsn=None):
        seen["dsn"] = dsn
        return {"is_safe": True, "issues": []}

    monkeypatch.setattr("custom_tools.text_to_sql.core.sql_safety_check", fake_safety)
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.get_plugin",
        lambda dsn: (_ for _ in ()).throw(AssertionError("database must not be opened in dry run")),
    )

    result = sql_explain("SELECT 1")

    assert result["dry_run_only"] is True
    assert result["skipped_execution"] is True
    assert seen["dsn"] == ""


def test_sql_generator_schema_validation_sqlglot_disabled_fails_fast(monkeypatch):
    generator = SQLGenerator()
    calls = {"count": 0}

    def fake_call_openai_api(**kwargs):
        calls["count"] += 1
        return json.dumps({"sql_query": "SELECT missing_amount FROM orders"})

    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.delenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", raising=False)
    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", fake_call_openai_api)

    result = generator.generate_sql(
        json.dumps({
            "schema_info": {
                "orders": {"columns": {"amount": {"type": "DECIMAL"}}}
            }
        }),
        "show amount",
    )

    assert calls["count"] == 1
    assert result["schema_issues"][0]["issue_type"] == "SQLGLOT_DISABLED_FOR_SCHEMA_VALIDATION"


def test_schema_enricher_passes_ref_column_to_fk_preview():
    enricher = SchemaEnricher()
    enricher._cached_schema = {
        "orders": {
            "columns": {
                "user_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "users(id)",
                }
            }
        }
    }
    plugin = _FkPreviewPlugin()

    previews = enricher._get_fk_previews(object(), plugin, "orders")

    assert plugin.calls == [{
        "table_name": "orders",
        "fk_column": "user_id",
        "ref_table": "users",
        "max_rows": 2,
        "ref_column": "id",
    }]
    assert previews["user_id"]["preview_columns"] == ["user_id", "name"]


def test_schema_enricher_parses_schema_qualified_fk_reference():
    enricher = object.__new__(SchemaEnricher)

    assert enricher._parse_fk_reference("public.users.id") == ("public.users", "id")
    assert enricher._parse_fk_reference("public.users(id)") == ("public.users", "id")


# === EPIC 1.8: code_formatter уважает FORBIDDEN_KEYWORDS инжектированного валидатора ===


class _StubValidator:
    """Минимальный stub SQL-валидатора для контрактных тестов code_formatter.

    Содержит только публичный/полупубличный surface, нужный code_formatter:
    forbidden_keywords (instance attr) и _mask_string_literals (no-op).
    """

    def __init__(self, forbidden_keywords):
        self.forbidden_keywords = list(forbidden_keywords)

    def _mask_string_literals(self, sql_query):
        return sql_query


def test_code_formatter_uses_injected_validator_keywords():
    """W2-T7: forbidden keyword → SQLForbiddenStatementError, БЕЗ leak SQL."""
    from custom_tools.text_to_sql.core._sql_generation_api import (
        code_formatter,
        SQLForbiddenStatementError,
    )

    validator = _StubValidator(["FOOBAR"])

    with pytest.raises(SQLForbiddenStatementError) as exc_info:
        code_formatter("SELECT FOOBAR FROM x", sql_validator=validator)

    assert exc_info.value.forbidden_keyword == "FOOBAR"
    # Исходник доступен только через атрибут exception (для логирования
    # caller'ом) — но не возвращается в formatted_sql_query, который мог
    # утечь в downstream-форматировщик.
    assert exc_info.value.sql_query == "SELECT FOOBAR FROM x"


def test_code_formatter_escapes_regex_metacharacters_in_keyword():
    """W2-T7: точка в keyword не должна играть роль regex-wildcard.

    Safe-вариант проходит штатно, flagged — поднимает SQLForbiddenStatementError.
    """
    from custom_tools.text_to_sql.core._sql_generation_api import (
        code_formatter,
        SQLForbiddenStatementError,
    )

    validator = _StubValidator(["DROP.TABLE"])

    # Safe: содержит "DROPxTABLE" pattern, но точка-в-keyword не wildcard'ит.
    safe_result = code_formatter("SELECT FROM x", sql_validator=validator)
    assert "formatted_sql_query" in safe_result

    with pytest.raises(SQLForbiddenStatementError) as exc_info:
        code_formatter("DROP.TABLE FOO", sql_validator=validator)
    assert exc_info.value.forbidden_keyword == "DROP.TABLE"


def test_code_formatter_detects_multi_word_keyword():
    """W2-T7: multi-word keyword детектится через любой whitespace; raise."""
    from custom_tools.text_to_sql.core._sql_generation_api import (
        code_formatter,
        SQLForbiddenStatementError,
    )

    validator = _StubValidator(["INSERT INTO"])

    with pytest.raises(SQLForbiddenStatementError) as exc1:
        code_formatter("INSERT  INTO t VALUES(1)", sql_validator=validator)
    assert exc1.value.forbidden_keyword == "INSERT INTO"

    with pytest.raises(SQLForbiddenStatementError) as exc2:
        code_formatter("INSERT\nINTO t VALUES(1)", sql_validator=validator)
    assert exc2.value.forbidden_keyword == "INSERT INTO"


# === EPIC 1.9: sql_safety_check fail-fast при LLM-сбое ===


def test_sql_safety_check_llm_failure_sets_failed_status(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("custom_tools.text_to_sql.core.call_openai_api", boom)

    result = sql_tools.sql_safety_check("SELECT 1")

    assert result["safety_status"] == "failed"
    assert result["llm_audit"] == "failed"
    assert result["is_safe"] is False
    issue_types = {issue.get("issue_type") for issue in result.get("issues", [])}
    assert "LLM_AUDIT_FAILED" in issue_types


def test_sql_safety_check_llm_success_marks_audit_ok(monkeypatch):
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **kwargs: '{"issues": []}',
    )

    result = sql_tools.sql_safety_check("SELECT 1")

    assert result["safety_status"] == "safe"
    assert result["llm_audit"] == "ok"
    assert result["is_safe"] is True


def test_sql_explain_cascades_llm_audit_failure(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")

    def boom(**kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr("custom_tools.text_to_sql.core.call_openai_api", boom)
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.get_plugin",
        lambda dsn: (_ for _ in ()).throw(AssertionError("DB must not be opened on audit failure")),
    )

    result = sql_explain("SELECT 1")

    assert result["plan"] is None
    issue_types = {issue.get("issue_type") for issue in result.get("issues", [])}
    assert "LLM_AUDIT_FAILED" in issue_types
