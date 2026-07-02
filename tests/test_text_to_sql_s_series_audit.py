from custom_tools.text_to_sql import significance_config, type_categories_config
from custom_tools.text_to_sql.schema_enricher import SchemaEnricher
from custom_tools.text_to_sql.schema_filtering import SchemaContextBuilder
from custom_tools.text_to_sql.schema_loader import SchemaFilter


def test_s5_schema_include_schema_qualified_env_matches_unqualified_schema(monkeypatch):
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "public.orders")
    db_schema = {
        "orders": {"columns": {"id": {"type": "int"}}},
        "customers": {"columns": {"id": {"type": "int"}}},
    }

    filtered = SchemaFilter.filter_schema_by_include_list(db_schema)

    assert set(filtered) == {"orders"}


def test_s5_schema_include_schema_qualified_env_does_not_overmatch(monkeypatch):
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "public.orders")
    db_schema = {
        "public.orders": {"columns": {"id": {"type": "int"}}},
        "archive.orders": {"columns": {"id": {"type": "int"}}},
    }

    filtered = SchemaFilter.filter_schema_by_include_list(db_schema)

    assert set(filtered) == {"public.orders"}


def test_s6_schema_enricher_parses_schema_table_fk_without_column():
    enricher = object.__new__(SchemaEnricher)

    assert enricher._parse_fk_reference("public.orders") == ("public.orders", None)
    assert enricher._parse_fk_reference("sales.orders") == ("sales.orders", None)
    assert enricher._parse_fk_reference("public.orders.id") == ("public.orders", "id")
    assert enricher._parse_fk_reference("users.id") == ("users", "id")


def test_s7_type_category_int_token_does_not_match_point_or_interval(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_TYPE_CATEGORIES_PATH", raising=False)
    type_categories_config.reset_cache()

    cfg = type_categories_config.load_type_categories_config()

    assert cfg.get_category("integer") == "integer"
    assert cfg.get_category("int4") == "integer"
    assert cfg.get_category("point") != "integer"
    assert cfg.get_category("interval") != "integer"

    type_categories_config.reset_cache()


class _MemoryReturnsAuditLog:
    def find_semantic_relevant_tables(self, terms, dsn=None):
        return ["audit_log"]


def test_s8_schema_context_skips_table_with_no_materialized_columns(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SIGNIFICANCE_PROFILE", raising=False)
    significance_config.reset_cache()

    builder = SchemaContextBuilder(_MemoryReturnsAuditLog())
    full_schema = {
        "audit_log": {
            "description": "Raw audit payloads",
            "columns": {
                "payload": {"type": "JSONB", "description": ""},
            },
        },
    }

    context = builder.build_relevant_schema_context(
        linked_metrics=[{"name": "audit"}],
        linked_dimensions=[],
        linked_filters={},
        joins=[],
        full_schema=full_schema,
    )

    assert context == {}

    significance_config.reset_cache()
