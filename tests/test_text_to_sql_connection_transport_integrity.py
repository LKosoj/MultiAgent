from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import textwrap

import pytest
from fastapi import HTTPException

from backend.fastapi_app.agui.auth import Principal
from streamlit_app.text_to_sql_client import TextToSqlApiClient, TextToSqlRunRequest


@pytest.fixture(autouse=True)
def _isolate_gateway_state_database(monkeypatch, tmp_path):
    """Tests below do ``import backend.fastapi_app.main`` inside the test body.

    At module load time ``main.py`` calls
    ``workflow.state_files.default_state_database_path(APP_ROOT, ...)``, which
    enforces that the real repo's ``data/`` directory is owned by the current
    user (a legitimate production safety check we must not weaken). In this
    checkout that directory is root-owned, so a fresh import of ``main``
    always raises ``PermissionError`` regardless of which test triggers it.

    Point the resolver at a ``tmp_path``-owned database instead, and drop any
    cached ``backend.fastapi_app.main`` module so each test gets a fresh
    import that uses the patched resolver. This mirrors the existing pattern
    in tests/test_ag_ui_gateway.py::_load_gateway_with_runner_stub.
    """
    state_files = importlib.import_module("workflow.state_files")
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "agui_events.db",
    )
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)
    yield
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)


def test_connection_registration_does_not_import_memory_rag_api() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib.abc
                import sys
                from types import SimpleNamespace

                class BlockMemoryRagApi(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "memory.streamlit_api":
                            raise RuntimeError("memory RAG API was imported")
                        return None

                sys.meta_path.insert(0, BlockMemoryRagApi())

                from backend.fastapi_app.agui import service
                from backend.fastapi_app.agui.auth import Principal

                service._register_connection = lambda *_args, **_kwargs: SimpleNamespace(
                    to_public_dict=lambda: {"connection_ref": "conn-test"}
                )
                result = service.handle_service_action(
                    "db.connections.register",
                    {
                        "display_name": "warehouse",
                        "dsn": "sqlite:///warehouse.db",
                        "owner_subject": "alice",
                        "tenant_id": "tenant-a",
                    },
                    Principal("alice", "tenant-a", frozenset({"admin"})),
                )
                assert result == {"connection": {"connection_ref": "conn-test"}}
                """
            ),
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "connection_ref",
    (
        "conn-123e4567-e89b-42d3-a456-426614174000",
        "conn-00000000-0000-4000-8000-000000000007",
    ),
)
def test_trusted_connection_response_preserves_ref_byte_for_byte_into_start(
    connection_ref: str,
) -> None:
    calls: list[tuple[str, str, object]] = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body))
        if path == "/v1/text-to-sql/connections":
            return {
                "connection": {
                    "connection_ref": connection_ref,
                    "display_name": "warehouse",
                }
            }
        return {
            "runId": "run-1",
            "threadId": "thread-1",
            "statusUrl": "/v1/runs/run-1",
            "resultUrl": "/v1/runs/run-1/result",
            "cancelUrl": "/v1/runs/run-1/cancel",
        }

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer token"},
    )
    registered = client.register_connection(
        display_name="warehouse",
        dsn="postgresql://svc:transport-secret@db.test/warehouse",
        owner_subject="alice",
        tenant_id="tenant-a",
    )
    client.start(TextToSqlRunRequest(query="show orders", connection_ref=registered.connection_ref))

    assert registered.connection_ref == connection_ref
    assert calls[1][2]["forwardedProps"]["service_payload"]["connection_ref"] == connection_ref


def test_connection_endpoints_delegate_authenticated_owner_and_project_metadata(
    monkeypatch,
) -> None:
    import backend.fastapi_app.agui.service as service
    import backend.fastapi_app.main as main

    owner = Principal("alice", "tenant-a", frozenset({"admin", "user"}))
    calls = []
    connection_ref = "conn-123e4567-e89b-42d3-a456-426614174000"

    def handle(action, payload, principal):
        calls.append((action, payload, principal))
        if action == "db.connections.register":
            return {
                "connection": {
                    "connection_ref": connection_ref,
                    "display_name": "warehouse",
                    "tenant_id": "tenant-a",
                    "dsn": "postgresql://svc:transport-secret@db.test/warehouse",
                }
            }
        return {
            "connections": [
                {
                    "connection_ref": connection_ref,
                    "display_name": "warehouse",
                    "tenant_id": "tenant-a",
                    "dsn": "postgresql://svc:transport-secret@db.test/warehouse",
                }
            ]
        }

    monkeypatch.setattr(main, "authenticate_request", lambda _request: owner)
    monkeypatch.setattr(service, "handle_service_action", handle)

    registered = asyncio.run(
        main.register_text_to_sql_connection(
            {
                "display_name": "warehouse",
                "dsn": "postgresql://svc:transport-secret@db.test/warehouse",
                "owner_subject": "alice",
                "tenant_id": "tenant-a",
            },
            object(),
        )
    )
    listed = asyncio.run(main.list_text_to_sql_connections(object()))

    assert [call[0] for call in calls] == [
        "db.connections.register",
        "db.connections.list",
    ]
    assert all(call[2] == owner for call in calls)
    assert registered["connection"]["connection_ref"] == connection_ref
    assert listed["connections"][0]["connection_ref"] == connection_ref
    assert "dsn" not in repr(registered)
    assert "transport-secret" not in repr(listed)


def test_connection_registration_keeps_existing_authorization_scope(monkeypatch) -> None:
    import backend.fastapi_app.agui.service as service
    import backend.fastapi_app.main as main

    caller = Principal("alice", "tenant-a", frozenset({"user"}))
    received = []

    def reject(action, payload, principal):
        received.append((action, principal))
        raise PermissionError("service action 'db.connections.register' requires role 'admin'")

    monkeypatch.setattr(main, "authenticate_request", lambda _request: caller)
    monkeypatch.setattr(service, "handle_service_action", reject)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main.register_text_to_sql_connection(
                {
                    "display_name": "warehouse",
                    "dsn": "postgresql://svc:transport-secret@db.test/warehouse",
                    "owner_subject": "alice",
                    "tenant_id": "tenant-a",
                },
                object(),
            )
        )

    assert exc_info.value.status_code == 403
    assert received == [("db.connections.register", caller)]


def test_connection_registration_maps_service_validation_error_to_422(monkeypatch) -> None:
    import backend.fastapi_app.agui.service as service
    import backend.fastapi_app.main as main

    admin = Principal("admin", "tenant-a", frozenset({"admin"}))

    monkeypatch.setattr(main, "authenticate_request", lambda _request: admin)
    monkeypatch.setattr(
        service,
        "handle_service_action",
        lambda *_args: (_ for _ in ()).throw(ValueError("dsn is required")),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main.register_text_to_sql_connection(
                {
                    "display_name": "warehouse",
                    "owner_subject": "alice",
                    "tenant_id": "tenant-a",
                },
                object(),
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "dsn is required"


def test_connection_registration_does_not_map_unexpected_value_error(monkeypatch) -> None:
    import backend.fastapi_app.agui.service as service
    import backend.fastapi_app.main as main

    admin = Principal("admin", "tenant-a", frozenset({"admin"}))

    monkeypatch.setattr(main, "authenticate_request", lambda _request: admin)
    monkeypatch.setattr(
        service,
        "handle_service_action",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("connection reference already exists in persistence")
        ),
    )

    with pytest.raises(ValueError, match="already exists in persistence"):
        asyncio.run(main.register_text_to_sql_connection({}, object()))


@pytest.mark.parametrize("connection_ref", (None, 7))
@pytest.mark.parametrize("action", ("register", "list"))
def test_connection_endpoints_reject_missing_or_non_string_connection_ref(
    monkeypatch,
    action: str,
    connection_ref: object,
) -> None:
    import backend.fastapi_app.agui.service as service
    import backend.fastapi_app.main as main

    admin = Principal("admin", "tenant-a", frozenset({"admin"}))
    connection = {"display_name": "warehouse"}
    if connection_ref is not None:
        connection["connection_ref"] = connection_ref
    response = (
        {"connection": connection}
        if action == "register"
        else {"connections": [connection]}
    )

    monkeypatch.setattr(main, "authenticate_request", lambda _request: admin)
    monkeypatch.setattr(service, "handle_service_action", lambda *_args: response)

    with pytest.raises(HTTPException) as exc_info:
        if action == "register":
            asyncio.run(main.register_text_to_sql_connection({}, object()))
        else:
            asyncio.run(main.list_text_to_sql_connections(object()))

    assert exc_info.value.status_code == 500
