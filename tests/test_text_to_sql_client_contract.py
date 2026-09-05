from __future__ import annotations

import copy
from dataclasses import fields
import json
from pathlib import Path

import pytest

from streamlit_app.text_to_sql_client import (
    TERMINAL_RUN_STATUSES,
    TextToSqlApiClient,
    TextToSqlApiError,
)


_TERMINAL_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "text_to_sql_terminal_contract_vectors.json")
    .read_text(encoding="utf-8")
)


def _deep_merge(base, overrides):
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and set(value) == {"$replace"}:
            merged[key] = copy.deepcopy(value["$replace"])
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _successful_terminal(run_id="run-1"):
    return _deep_merge(_TERMINAL_VECTORS["base"], {"run_id": run_id})


def test_start_uses_authenticated_run_api_and_opaque_connection_ref() -> None:
    from streamlit_app.text_to_sql_client import (
        TextToSqlApiClient,
        TextToSqlRunRequest,
    )

    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "runId": "run-1",
            "threadId": "thread-1",
            "statusUrl": "/v1/runs/run-1",
            "resultUrl": "/v1/runs/run-1/result",
            "cancelUrl": "/v1/runs/run-1/cancel",
        }

    request_fields = {field.name for field in fields(TextToSqlRunRequest)}
    assert "dsn" not in request_fields
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    handle = client.start(
        TextToSqlRunRequest(
            query="show monthly sales",
            connection_ref="connection-7",
            idempotency_key="request-9",
            enable_telemetry=True,
        )
    )

    assert handle.run_id == "run-1"
    method, path, body, headers = calls[0]
    assert (method, path) == ("POST", "/v1/runs")
    assert headers == {"Authorization": "Bearer token"}
    forwarded = body["forwardedProps"]
    assert forwarded["service_action"] == "presets.text_to_sql.generate"
    assert forwarded["service_payload"]["connection_ref"] == "connection-7"
    assert forwarded["service_payload"]["idempotency_key"] == "request-9"
    assert forwarded["service_payload"]["enable_telemetry"] is True
    assert "dsn" not in repr(body).lower()


def test_status_result_cancel_connections_and_schema_use_typed_http_contract() -> None:
    from streamlit_app.text_to_sql_client import TextToSqlApiClient

    calls = []
    responses = iter(
        [
            {"runId": "run-1", "threadId": "thread-1", "status": "running"},
            {
                "result": {
                    "terminal_outcome": _successful_terminal(),
                }
            },
            {"cancelled": False},
            {
                "connections": [
                    {"connection_ref": "conn-123e4567-e89b-42d3-a456-426614174000"}
                ]
            },
            {
                "action": "text_to_sql.schema.load",
                "ok": True,
                "data": {"tables": [{"name": "orders"}]},
            },
        ]
    )

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return next(responses)

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    assert client.get_run("run-1").status == "running"
    assert client.get_result("run-1").sql == "SELECT 1 AS value"
    assert client.cancel("run-1").status == "not_cancelled"
    assert (
        client.list_connections()[0].connection_ref
        == "conn-123e4567-e89b-42d3-a456-426614174000"
    )
    assert client.load_schema("connection-7").tables[0]["name"] == "orders"
    assert [call[:2] for call in calls[:3]] == [
        ("GET", "/v1/runs/run-1"),
        ("GET", "/v1/runs/run-1/result"),
        ("POST", "/v1/runs/run-1/cancel"),
    ]
    assert calls[3][:2] == ("GET", "/v1/text-to-sql/connections")
    assert calls[4][:2] == ("POST", "/v1/runs")


def test_streamlit_client_preserves_workflow_artifact_final_output_for_benchmark_receipt() -> None:
    terminal = _successful_terminal()
    expected = {"receipt": "research-stagnated"}
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {
            "result": {
                "terminal_outcome": terminal,
                "artifacts": {"final_output": expected},
            }
        },
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    assert client.get_result(terminal["run_id"]).final_output == expected


def test_register_connection_uses_authenticated_connection_endpoint() -> None:
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "connection": {
                "connection_ref": "conn-123e4567-e89b-42d3-a456-426614174000",
                "display_name": "release-postgres",
            }
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    connection = client.register_connection(
        display_name="release-postgres",
        dsn="postgresql://database.internal/release",
        owner_subject="text2sql-release-eval",
        tenant_id="text2sql-release",
    )

    assert connection.connection_ref.startswith("conn-")
    method, path, body, headers = calls[0]
    assert (method, path) == ("POST", "/v1/text-to-sql/connections")
    assert headers == {"Authorization": "Bearer token"}
    assert body == {
        "display_name": "release-postgres",
        "dsn": "postgresql://database.internal/release",
        "owner_subject": "text2sql-release-eval",
        "tenant_id": "text2sql-release",
        "enabled_for_user": True,
    }
    assert "dsn" not in connection.raw


@pytest.mark.parametrize("connection_ref", (None, 7))
def test_connection_endpoint_client_rejects_missing_or_non_string_ref(
    connection_ref: object,
) -> None:
    response = {"connection": {"display_name": "release-postgres"}}
    if connection_ref is not None:
        response["connection"]["connection_ref"] = connection_ref
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: response,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    with pytest.raises(TextToSqlApiError):
        client.register_connection(
            display_name="release-postgres",
            dsn="postgresql://database.internal/release",
            owner_subject="text2sql-release-eval",
            tenant_id="text2sql-release",
        )


@pytest.mark.parametrize("connection_ref", (None, 7))
def test_connection_list_client_rejects_missing_or_non_string_ref(
    connection_ref: object,
) -> None:
    connection = {"display_name": "release-postgres"}
    if connection_ref is not None:
        connection["connection_ref"] = connection_ref
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {"connections": [connection]},
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    with pytest.raises(TextToSqlApiError):
        client.list_connections()


def test_history_methods_use_owner_scoped_service_actions() -> None:
    calls = []
    responses = iter(
        [
            {
                "action": "text_to_sql.history.list",
                "ok": True,
                "data": {"entries": [{"run_id": "run-1"}]},
            },
            {
                "action": "text_to_sql.history.analytics",
                "ok": True,
                "data": {"result": {"total": 1}},
            },
            {
                "action": "text_to_sql.history.clear",
                "ok": True,
                "data": {"cleared": 1},
            },
        ]
    )

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return next(responses)

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    assert client.list_history(limit=20, offset=4) == [{"run_id": "run-1"}]
    assert client.history_analytics() == {"total": 1}
    assert client.clear_history() == 1
    actions = [call[2]["forwardedProps"]["service_action"] for call in calls]
    assert actions == [
        "text_to_sql.history.list",
        "text_to_sql.history.analytics",
        "text_to_sql.history.clear",
    ]


def test_streamlit_client_rejects_partial_terminal_result() -> None:
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {
            "result": {"terminal_outcome": {"run_id": "run-1", "status": "succeeded"}}
        },
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    with pytest.raises(TextToSqlApiError):
        client.get_result("run-1")


def test_streamlit_client_rejects_unsupported_status() -> None:
    terminal = _deep_merge(_TERMINAL_VECTORS["base"], {"status": "not_a_real_status"})
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {"result": {"terminal_outcome": terminal}},
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    with pytest.raises(TextToSqlApiError):
        client.get_result(terminal["run_id"])


def test_streamlit_client_allows_unknown_forward_compatible_field() -> None:
    terminal = _deep_merge(
        _TERMINAL_VECTORS["base"], {"future_diagnostic_field": "unused-by-client"}
    )
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {"result": {"terminal_outcome": terminal}},
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    result = client.get_result(terminal["run_id"])

    assert result.status == terminal["status"]
    assert result.raw["future_diagnostic_field"] == "unused-by-client"


def test_streamlit_client_accepts_unrecognized_reason_code_as_forward_compatible() -> None:
    terminal = _deep_merge(
        _TERMINAL_VECTORS["base"], {"reason_code": "FUTURE_RESERVED_REASON_CODE"}
    )
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {"result": {"terminal_outcome": terminal}},
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    result = client.get_result(terminal["run_id"])

    assert result.reason_code == "FUTURE_RESERVED_REASON_CODE"
    assert result.reason_code_recognized is False


def test_terminal_run_statuses_match_backend_run_status_terminal_subset() -> None:
    from backend.fastapi_app.agui.run_manager import _TERMINAL_STATUSES

    assert TERMINAL_RUN_STATUSES == {status.value for status in _TERMINAL_STATUSES}


def test_default_transport_preserves_absolute_result_url(monkeypatch) -> None:
    from streamlit_app import text_to_sql_client as client_module

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(api_request, timeout):
        captured["url"] = api_request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(client_module.request, "urlopen", fake_urlopen)
    client = TextToSqlApiClient(
        base_url="http://api.test",
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    client._urllib_transport(
        "GET",
        "https://api.example/v1/runs/run-1/result",
        json_body=None,
        headers={"Authorization": "Bearer token"},
    )

    assert captured == {
        "url": "https://api.example/v1/runs/run-1/result",
        "timeout": 30,
    }


def test_default_transport_uses_configured_request_timeout(monkeypatch) -> None:
    from streamlit_app import text_to_sql_client as client_module

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(api_request, timeout):
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(client_module.request, "urlopen", fake_urlopen)
    client = TextToSqlApiClient(
        base_url="http://api.test",
        auth_headers=lambda: {"Authorization": "Bearer token"},
        request_timeout_seconds=600,
    )

    client._urllib_transport(
        "GET",
        "/v1/runs/run-1",
        json_body=None,
        headers={"Authorization": "Bearer token"},
    )

    assert captured["timeout"] == 600


def test_load_metadata_uses_service_action_and_parses_tables_and_glossary() -> None:
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "action": "text_to_sql.metadata.load",
            "ok": True,
            "data": {
                "connection_ref": "connection-7",
                "dsn_dialect": "postgresql",
                "schema_digest": "digest-1",
                "editable_file_enabled": True,
                "tables": {
                    "public.orders": {
                        "description": "orders table",
                        "description_source": "file",
                        "columns": {
                            "status": {
                                "type": "text",
                                "description": "order status",
                                "description_source": "file",
                                "examples": ["new", "paid"],
                                "examples_source": "file",
                            }
                        },
                    }
                },
                "glossary": {
                    "digest": "glossary-digest-1",
                    "profile_exists": True,
                    "dsn_fingerprint": "postgresql://host:5432/db",
                    "schema_namespace_version": "ns-1",
                    "entries": [
                        {
                            "term": "выручка",
                            "synonyms": ["revenue"],
                            "table": "public.orders",
                            "column": "amount",
                            "kind": "measure",
                            "note": None,
                        }
                    ],
                },
                "facts": [
                    {
                        "fact_key": "text2sql-semantic-fact-v1-abc",
                        "subject": "column",
                        "table_fqn": "public.orders",
                        "column": "status",
                        "fact_kind": "example",
                        "value": "paid",
                        "status": "approved",
                    }
                ],
            },
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    metadata = client.load_metadata("connection-7")

    method, path, body, headers = calls[0]
    assert (method, path) == ("POST", "/v1/runs")
    assert body["forwardedProps"]["service_action"] == "text_to_sql.metadata.load"
    assert body["forwardedProps"]["service_payload"] == {
        "connection_ref": "connection-7"
    }
    assert metadata.schema_digest == "digest-1"
    assert metadata.glossary_digest == "glossary-digest-1"
    assert len(metadata.tables) == 1
    table = metadata.tables[0]
    assert table.table_fqn == "public.orders"
    assert table.columns[0].name == "status"
    assert table.columns[0].examples == ["new", "paid"]
    assert metadata.glossary_entries[0].term == "выручка"
    assert metadata.facts[0].fact_key == "text2sql-semantic-fact-v1-abc"


def test_save_metadata_descriptions_sends_expected_digest() -> None:
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "action": "text_to_sql.metadata.save_descriptions",
            "ok": True,
            "data": {"saved": True, "schema_digest": "digest-2"},
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    new_digest = client.save_metadata_descriptions(
        connection_ref="connection-7",
        expected_schema_digest="digest-1",
        tables=[
            {"table_fqn": "public.orders", "description": "updated", "columns": []}
        ],
    )

    assert new_digest == "digest-2"
    payload = calls[0][2]["forwardedProps"]["service_payload"]
    assert payload == {
        "connection_ref": "connection-7",
        "expected_schema_digest": "digest-1",
        "tables": [
            {"table_fqn": "public.orders", "description": "updated", "columns": []}
        ],
    }


def test_save_metadata_descriptions_surfaces_version_conflict_as_api_error() -> None:
    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=lambda *_args, **_kwargs: {
            "action": "text_to_sql.metadata.save_descriptions",
            "ok": False,
            "error": "metadata version conflict: reload table/column metadata before saving",
        },
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    with pytest.raises(TextToSqlApiError, match="version conflict"):
        client.save_metadata_descriptions(
            connection_ref="connection-7",
            expected_schema_digest="stale-digest",
            tables=[{"table_fqn": "public.orders", "description": "x", "columns": []}],
        )


def test_save_metadata_glossary_replaces_full_entry_list() -> None:
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "action": "text_to_sql.metadata.save_glossary",
            "ok": True,
            "data": {"saved": True, "glossary_digest": "glossary-digest-2"},
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    entries = [
        {
            "term": "выручка",
            "synonyms": ["revenue", "оборот"],
            "table": "public.orders",
            "column": "amount",
            "kind": "measure",
            "note": None,
        }
    ]

    new_digest = client.save_metadata_glossary(
        connection_ref="connection-7",
        expected_glossary_digest="glossary-digest-1",
        entries=entries,
    )

    assert new_digest == "glossary-digest-2"
    payload = calls[0][2]["forwardedProps"]["service_payload"]
    assert payload == {
        "connection_ref": "connection-7",
        "expected_glossary_digest": "glossary-digest-1",
        "entries": entries,
    }


def test_set_metadata_fact_status_round_trips_status() -> None:
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        return {
            "action": "text_to_sql.metadata.set_fact_status",
            "ok": True,
            "data": {
                "saved": True,
                "fact_key": "text2sql-semantic-fact-v1-abc",
                "status": "rejected",
            },
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )

    new_status = client.set_metadata_fact_status(
        connection_ref="connection-7",
        fact_key="text2sql-semantic-fact-v1-abc",
        status="rejected",
    )

    assert new_status == "rejected"
    payload = calls[0][2]["forwardedProps"]["service_payload"]
    assert payload == {
        "connection_ref": "connection-7",
        "fact_key": "text2sql-semantic-fact-v1-abc",
        "status": "rejected",
    }
