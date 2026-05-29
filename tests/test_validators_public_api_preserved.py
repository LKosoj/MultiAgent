from custom_tools.text_to_sql import validators


def test_validators_safety_exports_preserved():
    assert callable(validators.SQLSafetyValidator)
    assert callable(validators.get_sqlglot_metrics)
    assert callable(validators.reset_sqlglot_metrics)


def test_validators_schema_limiter_exports_preserved():
    assert callable(validators.SchemaLimiter)


def test_validators_schema_aware_exports_preserved():
    assert callable(validators.SQLSchemaValidator)


def test_validators_direct_import_preserved():
    from custom_tools.text_to_sql.validators import (
        SQLSafetyValidator,
        SchemaLimiter,
        SQLSchemaValidator,
        get_sqlglot_metrics,
        reset_sqlglot_metrics,
    )
    assert SQLSafetyValidator is validators.SQLSafetyValidator
    assert SchemaLimiter is validators.SchemaLimiter
    assert SQLSchemaValidator is validators.SQLSchemaValidator
