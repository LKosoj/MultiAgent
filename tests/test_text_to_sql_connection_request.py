import pytest

from backend.fastapi_app.agui._t2s_requests import (
    canonical_text_to_sql_start_fingerprint,
    parse_text_to_sql_start,
)


_CONNECTION_REF = "conn-12345678-1234-4123-8123-123456789abc"
_DSN = "sqlite:///example.db"


def test_connection_reference_is_a_first_class_request_input():
    request = parse_text_to_sql_start(
        {"query": "count orders", "connection_ref": _CONNECTION_REF}
    )

    assert request.connection_ref == _CONNECTION_REF
    assert request.dsn is None
    assert request.admin_raw_dsn_compat is False


def test_raw_dsn_remains_a_principal_agnostic_compatibility_input():
    request = parse_text_to_sql_start({"query": "count orders", "dsn": _DSN})

    assert request.connection_ref is None
    assert request.dsn == _DSN
    assert request.admin_raw_dsn_compat is False


def test_connection_reference_authorization_stays_outside_the_model():
    request = parse_text_to_sql_start(
        {"query": "count orders", "connection_ref": "db_config:legacy"}
    )

    assert request.connection_ref == "db_config:legacy"


@pytest.mark.parametrize(
    "connection_fields",
    [
        {},
        {"connection_ref": _CONNECTION_REF, "dsn": _DSN},
    ],
)
def test_request_requires_exactly_one_connection_input(connection_fields):
    with pytest.raises(
        ValueError,
        match="exactly one of connection_ref or dsn is required",
    ):
        parse_text_to_sql_start({"query": "count orders", **connection_fields})


@pytest.mark.parametrize("connection_ref", ["", "   ", 42])
def test_connection_reference_must_be_a_nonempty_string(connection_ref):
    with pytest.raises(ValueError, match="connection_ref"):
        parse_text_to_sql_start(
            {"query": "count orders", "connection_ref": connection_ref}
        )


@pytest.mark.parametrize(
    "connection_ref",
    [
        "postgresql://alice:secret@db.example:5432/app",
        "sqlite:///tmp/app.db",
        "file:/tmp/app.db",
        "/tmp/app.db",
        "relative.db",
        ":memory:",
        "host=db.example port=5432 dbname=app user=alice",
        "Driver={PostgreSQL};Server=db.example;Port=5432;Database=app",
    ],
)
def test_connection_reference_rejects_obvious_raw_dsn_smuggling(connection_ref):
    with pytest.raises(ValueError, match="connection_ref must be an opaque reference"):
        parse_text_to_sql_start(
            {"query": "count orders", "connection_ref": connection_ref}
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False), ("yes", True), ("off", False)],
)
def test_admin_raw_dsn_compat_uses_strict_boolean_coercion(value, expected):
    request = parse_text_to_sql_start(
        {
            "query": "count orders",
            "dsn": _DSN,
            "admin_raw_dsn_compat": value,
        }
    )

    assert request.admin_raw_dsn_compat is expected


@pytest.mark.parametrize("value", [2, "maybe", [], {}])
def test_admin_raw_dsn_compat_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="admin_raw_dsn_compat must be boolean"):
        parse_text_to_sql_start(
            {
                "query": "count orders",
                "dsn": _DSN,
                "admin_raw_dsn_compat": value,
            }
        )


def test_reference_start_fingerprint_is_canonical_and_reference_sensitive():
    first = {
        "query": "  count orders  ",
        "connection_ref": _CONNECTION_REF,
        "max_rows": "100",
        "client_id": "client-a",
        "idempotency_key": "key-a",
        "__request_id": "request-a",
    }
    equivalent = {
        "query": "count orders",
        "connection_ref": _CONNECTION_REF,
        "max_rows": 100,
        "client_id": "client-b",
        "idempotency_key": "key-b",
        "__request_id": "request-b",
    }
    other_reference = {**equivalent, "connection_ref": "conn-other"}

    assert canonical_text_to_sql_start_fingerprint(first) == (
        canonical_text_to_sql_start_fingerprint(equivalent)
    )
    assert canonical_text_to_sql_start_fingerprint(first) != (
        canonical_text_to_sql_start_fingerprint(other_reference)
    )


def test_raw_dsn_start_fingerprint_uses_canonical_typed_contract():
    assert canonical_text_to_sql_start_fingerprint(
        {"query": "count orders", "dsn": _DSN}
    ) == "d0587d9b2f3d43e028a6b0bccf4a61b986c0b3867626b3a569af04d9bb124bd2"
    assert canonical_text_to_sql_start_fingerprint(
        {
            "query": "count orders",
            "dsn": _DSN,
            "admin_raw_dsn_compat": True,
        }
    ) == "d0587d9b2f3d43e028a6b0bccf4a61b986c0b3867626b3a569af04d9bb124bd2"
