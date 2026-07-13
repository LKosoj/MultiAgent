import asyncio
import importlib
import json
import sys
import time
import types
import urllib.parse
import uuid

import anyio.from_thread
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.fastapi_app.agui.events import (
    CustomEvent,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from backend.fastapi_app.agui.models import RunAgentInput
from backend.fastapi_app.agui.store import EventStore


def test_agui_redaction_facade_exports_service_symbols():
    from backend.fastapi_app.agui import redaction

    for name in (
        "_dsn_fingerprint",
        "_is_masked_dsn",
        "_is_sensitive_query_key",
        "_looks_like_dsn",
        "_redact_dsn",
        "_redact_payload",
        "_redact_query_string",
        "_redact_text",
        "_sanitize_report_b64_gzip",
        "redact_pii_in_payload",
    ):
        assert hasattr(redaction, name), f"missing redaction facade export: {name}"


def _make_payload(run_id: str) -> dict:
    return {
        "threadId": f"thread-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [
            {"id": "msg-1", "role": "user", "content": "ping"},
        ],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def test_agui_pii_redaction_does_not_mask_run_ids():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload

    run_id = "run-54f74806070340c2"
    payload = {
        "runId": run_id,
        "workflow_run_id": run_id,
        "phone": "+7 (495) 123-45-67",
    }

    redacted = redact_pii_in_payload(payload)

    assert redacted["runId"] == run_id
    assert redacted["workflow_run_id"] == run_id
    assert redacted["phone"] == "[PHONE]"


def test_agui_pii_redaction_preserves_correlation_ids_with_pii_shape():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "threadId": "thread-user%40example.com",
        "__request_id": "req-person@example.com",
        "parentRunId": "parent-user@example.com",
        "messageId": "msg-+7 (495) 123-45-67",
        "toolCallId": "call-person@example.com",
        "runId": {"email": "nested@example.com"},
        "messages": [
            {
                "id": "msg-user@example.com",
                "toolCallId": "call-user@example.com",
                "tool_calls": [{"id": "tool-user@example.com"}],
                "content": "person@example.com",
            }
        ],
        "name": "service.result",
        "value": {
            "__request_id": "req-person@example.com",
            "message_id": "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken",
            "email": "person@example.com",
            "phone": "+7 (495) 123-45-67",
        },
        "payload": {
            "message_id": "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken",
        },
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))

    assert redacted["threadId"] == "thread-user%40example.com"
    assert redacted["__request_id"] == "req-person@example.com"
    assert redacted["parentRunId"] == "parent-user@example.com"
    assert redacted["messageId"] == "msg-+7 (495) 123-45-67"
    assert redacted["toolCallId"] == "call-person@example.com"
    assert redacted["runId"]["email"] == "[EMAIL]"
    assert redacted["messages"][0]["id"] == "msg-user@example.com"
    assert redacted["messages"][0]["toolCallId"] == "call-user@example.com"
    assert redacted["messages"][0]["tool_calls"][0]["id"] == "tool-user@example.com"
    assert redacted["messages"][0]["content"] == "[EMAIL]"
    assert redacted["value"]["__request_id"] == "req-person@example.com"
    assert "alice:secret" not in redacted["value"]["message_id"]
    assert "rawkeytoken" not in redacted["value"]["message_id"]
    assert "alice:secret" not in redacted["payload"]["message_id"]
    assert "rawkeytoken" not in redacted["payload"]["message_id"]
    assert redacted["value"]["email"] == "[EMAIL]"
    assert redacted["value"]["phone"] == "[PHONE]"


def test_agui_pii_redaction_preserves_run_finished_service_request_id():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "type": "RUN_FINISHED",
        "threadId": "thread-user@example.com",
        "runId": "run-user@example.com",
        "result": {
            "action": "demo.echo",
            "ok": True,
            "data": {"email": "person@example.com"},
            "__request_id": "req-user@example.com",
        },
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))

    assert redacted["threadId"] == "thread-user@example.com"
    assert redacted["runId"] == "run-user@example.com"
    assert redacted["result"]["__request_id"] == "req-user@example.com"
    assert redacted["result"]["data"]["email"] == "[EMAIL]"


def test_agui_pii_redaction_preserves_workflow_event_correlation_ids():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "type": "CUSTOM",
        "name": "workflow.started",
        "value": {
            "workflow_run_id": "run-workflow-user@example.com",
            "workflow_name": "demo_pipeline",
            "session_id": "run-user@example.com",
            "note": "person@example.com",
        },
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))

    assert redacted["value"]["workflow_run_id"] == "run-workflow-user@example.com"
    assert redacted["value"]["session_id"] == "run-user@example.com"
    assert redacted["value"]["note"] == "[EMAIL]"


def test_agui_correlation_ids_mask_secret_shaped_values():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "runId": "postgresql://alice:topsecret@db/app?api_key=raw-key",
        "threadId": "host=db user=bob password=secret",
        "request_id": "clientSecret=req-client refreshToken=req-refresh",
        "name": "workflow.started",
        "value": {
            "workflow_run_id": "postgresql://carol:workflowpass@db/app",
            "session_id": "user=session-user password=session-secret",
        },
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw in (
        "alice",
        "topsecret",
        "raw-key",
        "bob",
        "secret",
        "req-client",
        "req-refresh",
        "carol",
        "workflowpass",
        "session-user",
        "session-secret",
    ):
        assert raw not in serialized
    assert redacted["runId"].startswith("[AGUI_ID:")
    assert redacted["threadId"].startswith("[AGUI_ID:")
    assert redacted["request_id"].startswith("[AGUI_ID:")
    assert redacted["value"]["workflow_run_id"].startswith("[AGUI_ID:")
    assert redacted["value"]["session_id"].startswith("[AGUI_ID:")


def test_agui_pii_redaction_uses_configured_sync_masking_rules(monkeypatch):
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    from custom_tools.text_to_sql import pii_categories_config

    monkeypatch.setenv("PII_JURISDICTION", "eu")
    pii_categories_config.reset_cache()

    redacted = redact_pii_in_payload({
        "passport": "passport C01X00T47",
        "ip": "192.168.1.10",
    })

    assert redacted["passport"] == "passport [PASSPORT]"
    assert redacted["ip"] == "[IP]"

    monkeypatch.delenv("PII_JURISDICTION", raising=False)
    pii_categories_config.reset_cache()


def test_agui_pii_redaction_reuses_sanitized_shared_container():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload

    shared = {"email": "alice@example.com", "phone": "+7 (495) 123-45-67"}
    payload = {"first": shared, "second": shared}

    redacted = redact_pii_in_payload(payload)

    assert redacted["first"] is redacted["second"]
    assert redacted["first"]["email"] == "[EMAIL]"
    assert redacted["second"]["phone"] == "[PHONE]"
    assert "alice@example.com" not in str(redacted)


def test_agui_redaction_masks_extended_ru_pii_and_json_style_secrets():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    json_blob = (
        '{"password": "hunter-json-pass", "token": "json-token-value", '
        '"secret": "json-secret-value", "authorization": "Bearer json-auth-value", '
        '"max_tokens": 32768}'
    )
    payload = {
        "rows": [
            [
                "1234 567890",
                "123-456-789 00",
                "ИНН: 1234567890",
                json_blob,
            ]
        ]
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))
    serialized = json.dumps(redacted, ensure_ascii=False)
    row = redacted["rows"][0]

    for raw in (
        "1234 567890",
        "123-456-789 00",
        "1234567890",
        "hunter-json-pass",
        "json-token-value",
        "json-secret-value",
        "json-auth-value",
    ):
        assert raw not in serialized
    assert row[0] == "[PASSPORT]"
    assert row[1] == "[SNILS]"
    assert row[2] == "ИНН: [INN]"
    assert '"password": "***"' in row[3]
    assert '"token": "***"' in row[3]
    assert '"secret": "***"' in row[3]
    assert '"authorization": "***"' in row[3]
    assert '"max_tokens": 32768' in row[3]


def test_agui_redaction_masks_api_key_marker_names_without_token_counter_false_positive():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "openai_api_key": "sk-scalar",
        "OPENAI_API_KEY_DB": "sk-db",
        "message": (
            "OPENAI_API_KEY=sk-env "
            '{"openai_api_key": "sk-json", "max_tokens": 32768} '
            "https://example.com/path?openai_api_key=sk-query&max_tokens=32768"
        ),
        "max_tokens": 32768,
        "token_count": 12,
    }

    redacted = _redact_payload(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw in ("sk-scalar", "sk-db", "sk-env", "sk-json", "sk-query"):
        assert raw not in serialized
    assert redacted["openai_api_key"] == "<redacted>"
    assert redacted["OPENAI_API_KEY_DB"] == "<redacted>"
    assert "OPENAI_API_KEY=***" in redacted["message"]
    assert '"openai_api_key": "***"' in redacted["message"]
    assert "openai_api_key=***" in redacted["message"]
    assert redacted["max_tokens"] == 32768
    assert redacted["token_count"] == 12
    assert "max_tokens=32768" in redacted["message"]
    assert '"max_tokens": 32768' in redacted["message"]


def test_agui_redaction_masks_dsn_inside_query_field():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "query": (
            "connect postgresql://alice:secret@db/app"
            "?api_key=raw-key&sslmode=require"
        )
    }

    redacted = _redact_payload(payload)

    assert "alice" not in redacted["query"]
    assert "secret" not in redacted["query"]
    assert "raw-key" not in redacted["query"]
    assert "***:***@db" in redacted["query"]
    assert "api_key=***" in redacted["query"]
    assert "sslmode=require" in redacted["query"]


def test_agui_redaction_masks_plain_keyword_dsn_usernames_without_scalar_username_false_positive():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "message": (
            "Driver={ODBC Driver 17};Server=db;UID=alice;PWD=topsecret "
            "host=db user=bob username=carol user_id=tenant42 password=secret dbname=app"
        ),
        "username": "display-name",
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw_fragment in (
        "UID=alice",
        "PWD=topsecret",
        "user=bob",
        "username=carol",
        "user_id=tenant42",
        "password=secret",
        "alice",
        "topsecret",
        "bob",
        "carol",
        "tenant42",
    ):
        assert raw_fragment not in serialized
    assert "UID=***" in redacted["message"]
    assert "PWD=***" in redacted["message"]
    assert "user=***" in redacted["message"]
    assert "username=***" in redacted["message"]
    assert "user_id=***" in redacted["message"]
    assert "password=***" in redacted["message"]
    assert redacted["username"] == "display-name"


def test_agui_redaction_masks_stringified_json_secret_container_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "message": json.dumps({
            "api_key": ["sk-array"],
            "password": {"value": "secret-object"},
            "nested": {
                "dsn": "postgresql://alice:secret@db/app?api_key=raw-key",
            },
            "max_tokens": 32768,
        }),
    }

    redacted = _redact_payload(payload)
    parsed = json.loads(redacted["message"])
    serialized = json.dumps(parsed, ensure_ascii=False)

    for raw in ("sk-array", "secret-object", "alice", "secret", "raw-key"):
        assert raw not in serialized
    assert parsed["api_key"] == "***"
    assert parsed["password"] == "***"
    assert parsed["nested"]["dsn"] == "***"
    assert parsed["max_tokens"] == 32768


def test_agui_redaction_masks_url_encoded_leaf_secret_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    raw = "postgresql://alice:secret@db/app?api_key=raw-key"
    payload = {"message": urllib.parse.quote_plus(raw)}

    redacted = _redact_payload(payload)

    for fragment in (raw, "alice", "secret", "raw-key"):
        assert fragment not in redacted["message"]
    assert "***:***@db" in redacted["message"]
    assert "api_key=***" in redacted["message"]


def test_agui_correlation_ids_mask_double_url_encoded_secret_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    raw = "postgresql://alice:secret@db/app?api_key=raw-key"
    encoded = urllib.parse.quote_plus(urllib.parse.quote_plus(raw))

    redacted = _redact_payload({"run_id": encoded})

    assert redacted["run_id"].startswith("[AGUI_ID:")


def test_agui_redaction_masks_embedded_stringified_json_secret_container_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    message = (
        "failed "
        + json.dumps({"api_key": ["sk-array"], "password": {"value": "secret-object"}})
        + " done"
    )

    redacted = _redact_payload({"message": message})["message"]

    assert "sk-array" not in redacted
    assert "secret-object" not in redacted
    assert '"api_key": "***"' in redacted
    assert '"password": "***"' in redacted
    assert redacted.startswith("failed ")
    assert redacted.endswith(" done")


def test_agui_redaction_preserves_dsn_fingerprint_on_repeat_redaction():
    from backend.fastapi_app.agui.redaction import _dsn_fingerprint, _redact_payload

    dsn = "postgresql://alice:secret@db/app?api_key=raw-key"
    payload = {"dsn": dsn, "db_dsn": dsn, "database_url": dsn}

    redacted_once = _redact_payload(payload)
    redacted_twice = _redact_payload(redacted_once)
    expected = _dsn_fingerprint(dsn)

    assert redacted_once["dsn_fingerprint"] == expected
    assert redacted_twice["dsn_fingerprint"] == expected
    assert redacted_twice["db_dsn_fingerprint"] == expected
    assert redacted_twice["database_url_fingerprint"] == expected


def test_agui_masked_dsn_detector_recognizes_masked_dsn_username_keys():
    from backend.fastapi_app.agui.redaction import _is_masked_dsn

    masked_values = [
        "postgresql://db/app?user=***",
        "postgresql://db/app?username=***",
        "postgresql://db/app?uid=***",
        "postgresql://db/app?userid=***",
        "postgresql://db/app?user_id=***",
        "Driver={ODBC Driver 17};Server=db;UID=***;PWD=***",
        "host=db user=*** username=*** user_id=*** password=*** dbname=app",
    ]

    for value in masked_values:
        assert _is_masked_dsn(value), value


def test_service_serialize_marks_only_real_cycles():
    from backend.fastapi_app.agui.serialization import _serialize

    shared = {"value": 1}
    payload = {"first": shared, "second": shared}

    assert _serialize(payload) == {
        "first": {"value": 1},
        "second": {"value": 1},
    }

    payload["self"] = payload
    serialized = _serialize(payload)
    assert serialized["self"] == "[Circular]"
    assert serialized["second"] == {"value": 1}


def test_agui_redaction_masks_camel_case_secret_keys_and_cycles():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "accessToken": "raw-access",
        "refreshToken": "raw-refresh",
        "clientSecret": "raw-client",
        "password[foo]": "raw-password-indexed",
        "clientSecret.value": "raw-client-nested",
        "access_token.value": "raw-access-nested",
        "refreshToken.value": "raw-refresh-nested",
        "dbPassword": "raw-password",
        "AWS_SECRET_ACCESS_KEY": "raw-aws",
        "secret_key": "raw-secret-key",
        "privateKey": "raw-private",
        "message": (
            "https://example.test?clientSecret=query-client&refreshToken=query-refresh"
            "&password[foo]=query-password&access_token.value=query-access "
            "password[foo]=assign-password clientSecret.value=assign-client "
            '{"accessToken": "json-access", "privateKey": "json-private", '
            '"access_token.value": "json-access-nested", '
            '"password": 123456, "token": true}'
        ),
        "sort_key": "safe-sort",
        "max_tokens": 32768,
    }
    payload["self"] = payload

    redacted = redact_pii_in_payload(_redact_payload(payload))
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw in (
        "raw-access",
        "raw-refresh",
        "raw-client",
        "raw-password-indexed",
        "raw-client-nested",
        "raw-access-nested",
        "raw-refresh-nested",
        "raw-password",
        "raw-aws",
        "raw-secret-key",
        "raw-private",
        "query-client",
        "query-refresh",
        "query-password",
        "query-access",
        "assign-password",
        "assign-client",
        "json-access",
        "json-access-nested",
        "json-private",
        "123456",
    ):
        assert raw not in serialized
    assert redacted["accessToken"] == "<redacted>"
    assert redacted["refreshToken"] == "<redacted>"
    assert redacted["clientSecret"] == "<redacted>"
    assert redacted["password[foo]"] == "<redacted>"
    assert redacted["clientSecret.value"] == "<redacted>"
    assert redacted["access_token.value"] == "<redacted>"
    assert redacted["refreshToken.value"] == "<redacted>"
    assert redacted["dbPassword"] == "<redacted>"
    assert redacted["AWS_SECRET_ACCESS_KEY"] == "<redacted>"
    assert redacted["secret_key"] == "<redacted>"
    assert redacted["privateKey"] == "<redacted>"
    assert "clientSecret=***" in redacted["message"]
    assert "refreshToken=***" in redacted["message"]
    assert "password[foo]=***" in redacted["message"]
    assert "access_token.value=***" in redacted["message"]
    assert "clientSecret.value=***" in redacted["message"]
    assert '"accessToken": "***"' in redacted["message"]
    assert '"privateKey": "***"' in redacted["message"]
    assert '"access_token.value": "***"' in redacted["message"]
    assert '"password": "***"' in redacted["message"]
    assert '"token": "***"' in redacted["message"]
    assert redacted["sort_key"] == "safe-sort"
    assert redacted["max_tokens"] == 32768
    assert redacted["self"] == "[Circular]"


def test_agui_redaction_sanitizes_string_keys():
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    raw_email = "person@example.com"
    raw_email_encoded = "person%40example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken"
    payload = {
        raw_email: "email-key",
        raw_email_encoded: "encoded-email-key",
        raw_phone: "phone-key",
        raw_dsn: "dsn-key",
        "api%5Fkey": "encoded-key-secret",
    }

    redacted = redact_pii_in_payload(_redact_payload(payload))
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert raw_email not in serialized
    assert raw_email_encoded not in serialized
    assert raw_phone not in serialized
    assert raw_dsn not in serialized
    assert "alice:secret" not in serialized
    assert "rawkeytoken" not in serialized
    assert "encoded-key-secret" not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized
    assert '"api%5Fkey": "<redacted>"' in serialized
    assert "api_key=***" in serialized


def test_agui_redaction_masks_encoded_pii_query_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "url": (
            "https://example.test/path?email=person%40example.com"
            "&phone=%2B7+%28495%29+123-45-67"
            "&person%40example.com=ok&+7(495)123-45-67=ok&ok=1"
        ),
        "message": "email=person%40example.com phone=%2B7+%28495%29+123-45-67",
    }

    redacted = _redact_payload(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "person%40example.com" not in serialized
    assert "%2B7" not in serialized
    assert "+7(495)123-45-67" not in serialized
    assert "email=[EMAIL]" in serialized
    assert "phone=[PHONE]" in serialized
    assert "[EMAIL]=ok" in serialized
    assert "[PHONE]=ok" in serialized


def test_agui_redaction_recurses_nested_dsn_payloads_and_encoded_query_keys():
    from backend.fastapi_app.agui.redaction import _redact_payload

    raw_nested = "postgresql://alice:secret@example.com/db?api_key=abc"
    raw_encoded = "postgresql://host/db?api%5Fkey=rawsecret&sslmode=require"
    raw_query_dsn = "https://example.test/run?dsn=postgresql://alice:secret@db/app&ok=1"
    payload = {
        "dsn": {"primary": raw_nested},
        "error": f"failed {raw_encoded}",
        "link": raw_query_dsn,
    }

    redacted = _redact_payload(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "alice:secret" not in serialized
    assert "api_key=abc" not in serialized
    assert "dsn=postgresql" not in serialized
    assert "rawsecret" not in serialized
    assert "***:***@example.com" in serialized
    assert "api%5Fkey=***" in serialized
    assert "sslmode=require" in serialized


def test_agui_redaction_masks_userinfo_and_authorization_values():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "error": (
            "failed postgresql://api-token@example.com/db "
            "postgresql://alice%40example.com:secret@example.net/db "
            "Authorization: Bearer headersecret refresh_token=refreshsecret "
            "client_secret=clientsecret"
        ),
        "authorization": "Bearer scalarsecret",
    }

    redacted = _redact_payload(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw in (
        "api-token",
        "alice%40example.com",
        "headersecret",
        "refreshsecret",
        "clientsecret",
        "scalarsecret",
    ):
        assert raw not in serialized
    assert "***@example.com" in serialized
    assert "***:***@example.net" in serialized
    assert "Authorization: ***" in serialized
    assert "refresh_token=***" in serialized
    assert "client_secret=***" in serialized
    assert redacted["authorization"] == "<redacted>"


def test_agui_redaction_preserves_non_secret_token_counters():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "model_details": {
            "model_code": {
                "max_tokens": 32768,
                "completion_tokens": 42,
                "prompt_tokens": 17,
                "token_count": 59,
                "api_token": "secret-api-token",
                "client_secret": "secret-client",
            }
        }
    }

    redacted = _redact_payload(payload)
    model = redacted["model_details"]["model_code"]

    assert model["max_tokens"] == 32768
    assert model["completion_tokens"] == 42
    assert model["prompt_tokens"] == 17
    assert model["token_count"] == 59
    assert model["api_token"] == "<redacted>"
    assert model["client_secret"] == "<redacted>"


@pytest.mark.asyncio
async def test_service_action_preserves_llm_provider_token_limits(monkeypatch):
    async def _run():
        runner = _load_runner_with_service_stub(
            monkeypatch,
            lambda _action, _payload: {
                "providers": {
                    "openai": {
                        "model_details": {
                            "model_code": {
                                "max_tokens": 32768,
                                "token_count": 123,
                                "refresh_token": "provider-secret",
                            }
                        }
                    }
                }
            },
        )
        payload = _make_payload(f"run-{uuid.uuid4().hex[:8]}")
        payload["forwardedProps"] = {
            "service_action": "config.llm_providers",
            "service_payload": {},
        }
        events = [event async for event in runner.run_agent(RunAgentInput(**payload))]
        service_event = next(event for event in events if event.type == EventType.CUSTOM)
        model = service_event.value["data"]["providers"]["openai"]["model_details"]["model_code"]
        assert model["max_tokens"] == 32768
        assert model["token_count"] == 123
        assert model["refresh_token"] == "<redacted>"
        assert "provider-secret" not in json.dumps(service_event.value, ensure_ascii=False)

    await _run()


def test_agui_assignment_redaction_masks_semicolon_tail():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {
        "message": (
            "driver failed: password=top;secret token=abc "
            "api%5Fkey=rawsecret access%5Ftoken=encodedaccesssecret"
        )
    }

    redacted = _redact_payload(payload)

    assert "top;secret" not in redacted["message"]
    assert ";secret" not in redacted["message"]
    assert "rawsecret" not in redacted["message"]
    assert "encodedaccesssecret" not in redacted["message"]
    assert "password=***" in redacted["message"]
    assert "token=***" in redacted["message"]
    assert "api%5Fkey=***" in redacted["message"]
    assert "access%5Ftoken=***" in redacted["message"]


def test_agui_dsn_query_redaction_preserves_semicolon_param():
    from backend.fastapi_app.agui.redaction import _redact_payload

    payload = {"dsn": "mssql+pyodbc://srv/db?password=top;secret;driver=ODBC+Driver+17"}

    redacted = _redact_payload(payload)

    assert "top;secret" not in redacted["dsn"]
    assert ";secret" not in redacted["dsn"]
    assert "password=***" in redacted["dsn"]
    assert ";driver=ODBC+Driver+17" in redacted["dsn"]


def test_agui_redaction_masks_encoded_odbc_connect():
    from backend.fastapi_app.agui.redaction import _redact_payload

    raw_dsn = (
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dalice%3BPWD%3Dtopsecret"
        "&driver=ODBC+Driver+17"
    )

    redacted = _redact_payload({"dsn": raw_dsn, "message": f"failed {raw_dsn}"})
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "alice" not in serialized
    assert "topsecret" not in serialized
    assert "UID%3D" not in serialized
    assert "PWD%3D" not in serialized
    assert "odbc_connect=***" in serialized
    assert "driver=ODBC+Driver+17" in serialized


def _read_sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = json.loads(line[len("data: ") :])
        events.append(payload)
    return events


def _text_to_sql_result(
    run_id: str,
    incarnation: str,
    *,
    status: str,
) -> dict:
    from workflow.result_identity import workflow_result_event_key

    succeeded = status == "succeeded"
    failed = status == "failed"
    reason = {
        "succeeded": "",
        "failed": "EXECUTION_FAILED",
        "cancelled": "CANCELLED",
    }[status]
    execution = {}
    audit = {}
    if succeeded or failed:
        execution = {
            "success": succeeded,
            "sql_query": "SELECT 1",
            "data": [[1]] if succeeded else [],
            "columns": ["value"] if succeeded else [],
            "rows_affected": 1 if succeeded else 0,
            "execution_time_ms": 1,
            "dry_run_only": False,
            "skipped_execution": False,
            "applied_row_limit": 100,
        }
        if failed:
            execution["error_message"] = "execution failed"
        audit = {"status": "logged", "log_id": "audit-1"}
    terminal = {
        "run_id": run_id,
        "status": status,
        "reason_code": reason,
        "sql": "SELECT 1",
        "generated": True,
        "approved": succeeded or failed,
        "executed": succeeded or failed,
        "dry_run": False,
        "audited": succeeded or failed,
        "data": [[1]] if succeeded else [],
        "columns": ["value"] if succeeded else [],
        "rows_affected": 1 if succeeded else 0,
        "error": (
            None
            if succeeded
            else "execution failed"
            if failed
            else "cancelled"
        ),
        "execution": execution,
        "audit": audit,
        "persistence": (
            {
                "status": "saved",
                "filename": "query.md",
                "path": "/tmp/query.md",
            }
            if succeeded
            else {"status": "not_attempted"}
        ),
    }
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "completed" if succeeded else status,
        "success": succeeded,
        "terminal_outcome": terminal,
    }


def _failed_text_to_sql_result(run_id: str, incarnation: str) -> dict:
    return _text_to_sql_result(
        run_id,
        incarnation,
        status="failed",
    )


def _drop_imported_module(monkeypatch, module_name: str) -> None:
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    parent_name, _, child_name = module_name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is not None:
        monkeypatch.delattr(parent, child_name, raising=False)


def _load_gateway_with_runner_stub(monkeypatch, run_agent, tmp_path):
    old_gateway = sys.modules.get("backend.fastapi_app.main")
    old_store = getattr(old_gateway, "store", None)
    if isinstance(old_store, EventStore):
        old_store.close()

    state_files = importlib.import_module("workflow.state_files")
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "gateway-main.db",
    )
    stub_runner = types.ModuleType("backend.fastapi_app.agui.runner")
    stub_runner.run_agent = run_agent
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.runner", stub_runner)

    stub_logging_setup = types.ModuleType("logging_setup")
    stub_logging_setup.setup_comprehensive_logging = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "logging_setup", stub_logging_setup)

    _drop_imported_module(monkeypatch, "backend.fastapi_app.agui.run_manager")
    _drop_imported_module(monkeypatch, "backend.fastapi_app.main")
    return importlib.import_module("backend.fastapi_app.main")


def _load_run_manager_with_runner_stub(monkeypatch, run_agent):
    stub_runner = types.ModuleType("backend.fastapi_app.agui.runner")
    stub_runner.run_agent = run_agent
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.runner", stub_runner)
    _drop_imported_module(monkeypatch, "backend.fastapi_app.agui.run_manager")
    return importlib.import_module("backend.fastapi_app.agui.run_manager")


def _load_runner_with_service_stub(monkeypatch, handle_service_action):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "disabled")
    stub_agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        async def coordinate(self, *args, **kwargs):
            return ""

    stub_agent_system.DynamicAgentSystem = DynamicAgentSystem
    monkeypatch.setitem(sys.modules, "agent_system", stub_agent_system)

    stub_service = types.ModuleType("backend.fastapi_app.agui.service")
    stub_service.handle_service_action = handle_service_action
    stub_service._require_service_action_role = (
        lambda action, principal: None
        if principal.has_role("admin")
        else (_ for _ in ()).throw(PermissionError(f"service action '{action}' requires role 'admin'"))
    )
    stub_service._redact_payload = lambda value: value
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", stub_service)

    stub_logging = types.ModuleType("unified_logging")

    class RunIdContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    stub_logging.get_logging_manager = lambda *args, **kwargs: types.SimpleNamespace()
    stub_logging.run_id_context = lambda *args, **kwargs: RunIdContext()
    monkeypatch.setitem(sys.modules, "unified_logging", stub_logging)

    stub_utils = types.ModuleType("utils")
    stub_utils.call_openai_api_streaming = lambda *args, **kwargs: ""
    monkeypatch.setitem(sys.modules, "utils", stub_utils)

    _drop_imported_module(monkeypatch, "backend.fastapi_app.agui.runner")
    return importlib.import_module("backend.fastapi_app.agui.runner")


def _install_service_action_stub(monkeypatch, handle_service_action) -> None:
    module_name = "backend.fastapi_app.agui.service"
    stub_service = types.ModuleType(module_name)
    stub_service.handle_service_action = handle_service_action
    monkeypatch.setitem(sys.modules, module_name, stub_service)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AG_UI_AUTH_MODE", "disabled")

    async def stub_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, stub_run_agent, tmp_path)
    from backend.fastapi_app.agui.run_manager import RunManager

    store = EventStore(str(tmp_path / "agui_events.db"))
    run_manager = RunManager(store)
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "run_manager", run_manager)
    test_client = TestClient(gateway.app)
    # Reuse a single persistent event loop/portal across every call this
    # fixture's TestClient makes, instead of TestClient's default behaviour
    # of spinning up (and immediately tearing down) a fresh loop per call.
    # RunManager._execute_run now genuinely awaits store calls dispatched
    # to a worker thread (T2 executor offload), so it must survive across
    # the separate HTTP calls a test issues while polling for run
    # completion; a per-call ephemeral loop would cancel the background
    # run task before the executor's callback can land on it. This does
    # not invoke the app's lifespan handlers (unlike using
    # `with TestClient(...) as client:`), matching this fixture's existing
    # behaviour of not exercising startup/shutdown.
    with anyio.from_thread.start_blocking_portal(backend="asyncio") as portal:
        test_client.portal = portal
        yield test_client
    run_manager.close()


def test_text_to_sql_terminal_retry_stream_is_request_scoped_and_winner_consistent(
    client,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode

    def seed_terminal_run(
        *,
        run_id: str,
        idempotency_key: str,
        historical_terminal: EventType,
    ) -> None:
        service_payload = {
            "query": "count orders",
            "dsn": "sqlite:///example.db",
            "idempotency_key": idempotency_key,
        }
        gateway.store.create_or_get_run(
            principal=system_principal_for_disabled_mode(),
            run_kind="text_to_sql",
            idempotency_key=idempotency_key,
            request_fingerprint=canonical_text_to_sql_start_fingerprint(
                service_payload
            ),
            proposed_run_id=run_id,
            thread_id=f"thread-{run_id}",
        )
        incarnation = f"inc-{run_id}"
        assert gateway.store.reserve_workflow_run(
            run_id,
            incarnation,
            f"thread-{run_id}",
            "text_to_sql_pipeline",
        )
        result = _failed_text_to_sql_result(run_id, incarnation)
        gateway.store.finalize_run_with_event(run_id, result)
        if historical_terminal is EventType.RUN_ERROR:
            event = RunErrorEvent(
                type=EventType.RUN_ERROR,
                message="historical failure",
                code="historical_failure",
                timestamp=1,
            )
        else:
            event = RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=f"thread-{run_id}",
                run_id=run_id,
                result={"stale": "success"},
                timestamp=1,
            )
        gateway.store.append(
            run_id,
            event.type.value,
            event.model_dump(by_alias=True, exclude_none=True),
        )

    def retry(idempotency_key: str, request_id: str) -> list[dict]:
        payload = _make_payload(f"proposal-{request_id}")
        payload["forwardedProps"] = {
            "service_action": "presets.text_to_sql.generate",
            "service_payload": {
                "query": "count orders",
                "dsn": "sqlite:///example.db",
                "idempotency_key": idempotency_key,
                "__request_id": request_id,
            },
        }
        response = client.post("/agent", json=payload)
        assert response.status_code == 200
        return _read_sse_events(response.text)

    seed_terminal_run(
        run_id="failed-run",
        idempotency_key="failed-key",
        historical_terminal=EventType.RUN_ERROR,
    )
    for request_id in ("request-a", "request-b"):
        events = retry("failed-key", request_id)
        assert [event["type"] for event in events] == [
            "CUSTOM",
            "CUSTOM",
            "RUN_ERROR",
        ]
        assert events[0]["name"] == "service.result"
        assert events[0]["value"]["__request_id"] == request_id
        assert events[1]["name"] == "workflow.result"
        assert events[1]["value"]["terminal_outcome"]["status"] == "failed"

    seed_terminal_run(
        run_id="mismatched-run",
        idempotency_key="mismatched-key",
        historical_terminal=EventType.RUN_FINISHED,
    )
    mismatched = retry("mismatched-key", "request-c")
    assert [event["type"] for event in mismatched] == [
        "CUSTOM",
        "CUSTOM",
        "RUN_ERROR",
    ]
    assert all(event["type"] != "RUN_FINISHED" for event in mismatched)


def test_terminal_text_to_sql_get_replay_survives_run_manager_cache_loss(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager

    run_id = "primary-only-terminal"
    service_payload = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "idempotency_key": "primary-only-key",
    }
    gateway.store.create_or_get_run(
        principal=system_principal_for_disabled_mode(),
        run_kind="text_to_sql",
        idempotency_key="primary-only-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(
            service_payload
        ),
        proposed_run_id=run_id,
        thread_id="thread-primary-only",
    )
    incarnation = "inc-primary-only"
    assert gateway.store.reserve_workflow_run(
        run_id,
        incarnation,
        "thread-primary-only",
        "text_to_sql_pipeline",
    )
    result = _failed_text_to_sql_result(run_id, incarnation)
    result_seq = gateway.store.finalize_run_with_event(run_id, result)
    assert result_seq == 1
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))

    for path in (
        f"/agent/{run_id}/events",
        f"/v1/runs/{run_id}/events",
    ):
        response = client.get(path, params={"after": 0, "follow": True})
        assert response.status_code == 200
        events = _read_sse_events(response.text)
        assert [event["type"] for event in events] == ["CUSTOM", "RUN_ERROR"]
        assert events[0]["name"] == "workflow.result"
        assert events[0]["value"] == result
        assert events[1]["code"] == "text_to_sql_failed"

        after_result = client.get(
            path,
            params={"after": result_seq, "follow": True},
        )
        assert after_result.status_code == 200
        assert _read_sse_events(after_result.text) == []

    workflow_projection = CustomEvent(
        type=EventType.CUSTOM,
        name="workflow.result",
        value=result,
        timestamp=2,
    )
    terminal_projection = RunErrorEvent(
        type=EventType.RUN_ERROR,
        message="EXECUTION_FAILED",
        code="text_to_sql_failed",
        timestamp=3,
    )
    projection_seqs = [
        gateway.store.append(
            run_id,
            projection.type.value,
            projection.model_dump(by_alias=True, exclude_none=True),
        )
        for projection in (workflow_projection, terminal_projection)
    ]

    persisted_after_winner = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": result_seq, "follow": True},
    )
    assert persisted_after_winner.status_code == 200
    persisted_after_winner_events = _read_sse_events(
        persisted_after_winner.text
    )
    assert [event["type"] for event in persisted_after_winner_events] == [
        "CUSTOM",
        "RUN_ERROR",
    ]

    persisted = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": 0, "follow": True},
    )
    assert persisted.status_code == 200
    persisted_events = _read_sse_events(persisted.text)
    assert [event["type"] for event in persisted_events] == [
        "CUSTOM",
        "RUN_ERROR",
    ]
    assert persisted_events[0]["value"] == result

    after_workflow_projection = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": projection_seqs[0], "follow": True},
    )
    assert after_workflow_projection.status_code == 200
    assert [
        event["type"]
        for event in _read_sse_events(after_workflow_projection.text)
    ] == ["RUN_ERROR"]

    after_terminal_projection = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": projection_seqs[1], "follow": True},
    )
    assert after_terminal_projection.status_code == 200
    assert _read_sse_events(after_terminal_projection.text) == []


def test_terminal_text_to_sql_get_replay_replaces_conflicting_transport_terminal(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager

    run_id = "conflicting-terminal"
    service_payload = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "idempotency_key": "conflicting-terminal-key",
    }
    gateway.store.create_or_get_run(
        principal=system_principal_for_disabled_mode(),
        run_kind="text_to_sql",
        idempotency_key="conflicting-terminal-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(
            service_payload
        ),
        proposed_run_id=run_id,
        thread_id="thread-conflicting",
    )
    incarnation = "inc-conflicting"
    assert gateway.store.reserve_workflow_run(
        run_id,
        incarnation,
        "thread-conflicting",
        "text_to_sql_pipeline",
    )
    result = _failed_text_to_sql_result(run_id, incarnation)
    gateway.store.finalize_run_with_event(run_id, result)
    stale = RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id="thread-conflicting",
        run_id=run_id,
        result={"stale": "success"},
        timestamp=1,
    )
    gateway.store.append(
        run_id,
        stale.type.value,
        stale.model_dump(by_alias=True, exclude_none=True),
    )
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))

    response = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": 0, "follow": False},
    )
    assert response.status_code == 200
    events = _read_sse_events(response.text)
    assert [event["type"] for event in events] == ["CUSTOM", "RUN_ERROR"]
    assert events[0]["value"] == result
    assert events[1]["code"] == "text_to_sql_failed"
    assert all(event["type"] != "RUN_FINISHED" for event in events)


def test_terminal_text_to_sql_get_replay_reports_resultless_restart_failure(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager

    run_id = "restart-without-result"
    gateway.store.create_or_get_run(
        principal=system_principal_for_disabled_mode(),
        run_kind="text_to_sql",
        idempotency_key="restart-without-result-key",
        request_fingerprint="a" * 64,
        proposed_run_id=run_id,
        thread_id="thread-restart",
    )
    assert gateway.store.reconcile_non_terminal_runs() == 1
    stored = gateway.store.get_run(run_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.terminal_reason == "SERVER_RESTARTED"
    assert stored.result_seq is None
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))

    response = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": 0, "follow": True},
    )
    assert response.status_code == 200
    events = _read_sse_events(response.text)
    assert [event["type"] for event in events] == ["RUN_ERROR"]
    assert events[0]["code"] == "text_to_sql_server_restarted"

    resumed = client.get(
        f"/v1/runs/{run_id}/events",
        params={"after": 7, "follow": True},
    )
    assert resumed.status_code == 200
    resumed_events = _read_sse_events(resumed.text)
    assert [event["type"] for event in resumed_events] == ["RUN_ERROR"]
    assert resumed_events[0]["code"] == "text_to_sql_server_restarted"


def test_agui_auth_defaults_to_required_and_fails_startup_without_tokens(
    tmp_path,
    monkeypatch,
):
    for env_name in (
        "AG_UI_AUTH_MODE",
        "AG_UI_AUTH_TOKEN",
        "AG_UI_ADMIN_TOKEN",
        "AG_UI_USER_TOKEN",
        "AG_UI_MEMORY_ARCHIVIST_TOKEN",
        "AG_UI_AUTH_TOKEN_MAP",
    ):
        monkeypatch.delenv(env_name, raising=False)

    async def fake_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    monkeypatch.setattr(
        gateway,
        "_drain_workflow_results_before_reconcile",
        lambda: pytest.fail("startup continued past invalid auth configuration"),
    )

    with pytest.raises(HTTPException) as exc_info:
        with TestClient(gateway.app):
            pass

    assert exc_info.value.status_code == 500
    assert "requires at least one authentication token" in exc_info.value.detail


def test_token_map_entry_without_subject_fails_configuration(monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv(
        "AG_UI_AUTH_TOKEN_MAP",
        json.dumps(
            {
                "browser-token": {
                    "tenant_id": "tenant-1",
                    "roles": ["user"],
                }
            }
        ),
    )

    from backend.fastapi_app.agui.auth import validate_auth_configuration

    with pytest.raises(HTTPException) as exc_info:
        validate_auth_configuration()

    assert exc_info.value.status_code == 500
    assert "subject" in exc_info.value.detail


def test_token_map_entry_with_blank_subject_fails_configuration(monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv(
        "AG_UI_AUTH_TOKEN_MAP",
        json.dumps(
            {
                "browser-token": {
                    "subject": "   ",
                    "tenant_id": "tenant-1",
                    "roles": ["user"],
                }
            }
        ),
    )

    from backend.fastapi_app.agui.auth import validate_auth_configuration

    with pytest.raises(HTTPException) as exc_info:
        validate_auth_configuration()

    assert exc_info.value.status_code == 500
    assert "subject" in exc_info.value.detail


@pytest.mark.parametrize(
    ("token_map", "extra_tokens", "expected_detail"),
    (
        (
            json.dumps(
                {
                    "   ": {
                        "subject": "alice",
                        "tenant_id": "tenant-1",
                        "roles": ["user"],
                    }
                }
            ),
            {},
            "token must not be blank",
        ),
        (
            json.dumps(
                {
                    "browser-token": {
                        "subject": "alice",
                        "tenant_id": "   ",
                        "roles": ["user"],
                    }
                }
            ),
            {},
            "tenant_id",
        ),
        (
            json.dumps(
                {
                    "browser-token": {
                        "subject": "alice",
                        "tenant_id": "tenant-1",
                        "roles": {"user": True},
                    }
                }
            ),
            {},
            "roles must be a string or list",
        ),
        (
            '{"shared":{"subject":"alice"},'
            '"shared":{"subject":"bob"}}',
            {},
            "duplicate token",
        ),
        (
            json.dumps({"shared": {"subject": "alice"}}),
            {"AG_UI_AUTH_TOKEN": "shared"},
            "duplicate authentication token",
        ),
    ),
)
def test_invalid_auth_token_configuration_fails_validation(
    monkeypatch,
    token_map,
    extra_tokens,
    expected_detail,
):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN_MAP", token_map)
    for env_name, token in extra_tokens.items():
        monkeypatch.setenv(env_name, token)

    from backend.fastapi_app.agui.auth import validate_auth_configuration

    with pytest.raises(HTTPException) as exc_info:
        validate_auth_configuration()

    assert exc_info.value.status_code == 500
    assert expected_detail in exc_info.value.detail


def test_blank_environment_token_fails_auth_configuration(monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "optional")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "   ")

    from backend.fastapi_app.agui.auth import validate_auth_configuration

    with pytest.raises(HTTPException) as exc_info:
        validate_auth_configuration()

    assert exc_info.value.status_code == 500
    assert "AG_UI_AUTH_TOKEN must not be blank" in exc_info.value.detail


def test_invalid_auth_mode_fails_auth_configuration(monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "unexpected")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "browser-token")

    from backend.fastapi_app.agui.auth import validate_auth_configuration

    with pytest.raises(HTTPException) as exc_info:
        validate_auth_configuration()

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "invalid AG_UI_AUTH_MODE: unexpected"


def test_whoami_accepts_valid_bearer_and_rejects_missing_or_bad_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv(
        "AG_UI_AUTH_TOKEN_MAP",
        json.dumps(
            {
                "browser-token": {
                    "subject": "alice",
                    "tenant_id": "tenant-1",
                    "roles": ["user", "memory_archivist"],
                }
            }
        ),
    )

    async def fake_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    monkeypatch.setattr(
        gateway,
        "_drain_workflow_results_before_reconcile",
        lambda: frozenset(),
    )

    with TestClient(gateway.app) as local_client:
        missing = local_client.get("/v1/auth/me")
        bad = local_client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer wrong-token"},
        )
        valid = local_client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer browser-token"},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert bad.status_code == 403
    assert valid.status_code == 200
    assert valid.json() == {
        "subject": "alice",
        "tenant_id": "tenant-1",
        "roles": ["memory_archivist", "user"],
    }


def test_current_principal_without_context_is_anonymous_when_auth_required(monkeypatch):
    for env_name in (
        "AG_UI_AUTH_MODE",
        "AG_UI_AUTH_TOKEN",
        "AG_UI_ADMIN_TOKEN",
        "AG_UI_USER_TOKEN",
        "AG_UI_MEMORY_ARCHIVIST_TOKEN",
        "AG_UI_AUTH_TOKEN_MAP",
    ):
        monkeypatch.delenv(env_name, raising=False)

    from backend.fastapi_app.agui.auth import current_principal

    principal = current_principal()

    assert principal.subject == "anonymous"
    assert principal.roles == frozenset({"user"})


def test_v1_runs_lifecycle(client, monkeypatch):
    async def fake_run_agent(input_data):
        yield RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            input=input_data,
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"status": "ok"},
            timestamp=int(time.time() * 1000),
        )

    from backend.fastapi_app.agui import run_manager as rm

    monkeypatch.setattr(rm, "run_agent", fake_run_agent)

    client_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(client_run_id)
    response = client.post("/v1/runs", json=payload)
    assert response.status_code == 200
    body = response.json()
    run_id = body["runId"]
    assert run_id != client_run_id
    assert run_id.startswith("run-")
    assert "/v1/runs/" in body["eventsUrl"]

    for _ in range(20):
        status = client.get(f"/v1/runs/{run_id}").json()
        if status["status"] == "finished":
            break
        time.sleep(0.05)

    events_resp = client.get(f"/v1/runs/{run_id}/events", params={"after": 0, "follow": False})
    assert events_resp.status_code == 200
    events = _read_sse_events(events_resp.text)
    event_types = {e.get("type") for e in events}
    assert "RUN_STARTED" in event_types
    assert "RUN_FINISHED" in event_types

    result_resp = client.get(f"/v1/runs/{run_id}/result")
    assert result_resp.status_code == 200
    assert result_resp.json()["result"] == {"status": "ok"}


def test_agui_auth_required_rejects_missing_and_bad_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "secret-token")

    async def fake_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    from backend.fastapi_app.agui.run_manager import RunManager

    store = EventStore(str(tmp_path / "agui_events.db"))
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "run_manager", RunManager(store))
    local_client = TestClient(gateway.app)

    payload = _make_payload("client-run")

    assert local_client.post("/v1/runs", json=payload).status_code == 401
    assert local_client.post(
        "/v1/runs",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403

    response = local_client.post(
        "/v1/runs",
        json=payload,
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    assert response.json()["runId"].startswith("run-")


def test_agui_run_access_is_bound_to_authenticated_principal(tmp_path, monkeypatch):
    token_map = {
        "token-a": {"subject": "alice", "tenant_id": "tenant-1", "roles": ["user"]},
        "token-b": {"subject": "bob", "tenant_id": "tenant-1", "roles": ["user"]},
    }
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN_MAP", json.dumps(token_map))

    async def fake_run_agent(input_data):
        yield RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            input=input_data,
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"owner": "alice"},
            timestamp=int(time.time() * 1000),
        )

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    from backend.fastapi_app.agui.run_manager import RunManager

    store = EventStore(str(tmp_path / "agui_events.db"))
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "run_manager", RunManager(store))
    local_client = TestClient(gateway.app)

    response = local_client.post(
        "/v1/runs",
        json=_make_payload("client-selected-run"),
        headers={"Authorization": "Bearer token-a"},
    )
    assert response.status_code == 200
    run_id = response.json()["runId"]
    assert run_id != "client-selected-run"

    for _ in range(20):
        status = local_client.get(
            f"/v1/runs/{run_id}",
            headers={"Authorization": "Bearer token-a"},
        ).json()
        if status["status"] == "finished":
            break
        time.sleep(0.05)

    assert local_client.get(
        f"/v1/runs/{run_id}/result",
        headers={"Authorization": "Bearer token-a"},
    ).status_code == 200
    assert local_client.get(
        f"/v1/runs/{run_id}/result",
        headers={"Authorization": "Bearer token-b"},
    ).status_code == 404
    assert local_client.post(
        f"/v1/runs/{run_id}/cancel",
        headers={"X-AGUI-Token": "token-b"},
    ).status_code == 404


def test_generic_auth_token_is_not_admin_for_legacy_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "user-token")
    monkeypatch.setenv("AG_UI_ADMIN_TOKEN", "admin-token")

    async def fake_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    from backend.fastapi_app.agui.run_manager import RunManager

    store = EventStore(str(tmp_path / "agui_events.db"))
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "run_manager", RunManager(store))
    local_client = TestClient(gateway.app)
    store.append(
        "legacy-run",
        EventType.RUN_FINISHED.value,
        {"type": "RUN_FINISHED", "runId": "legacy-run", "result": {"ok": True}},
    )

    assert local_client.get(
        "/v1/runs/legacy-run/result",
        headers={"Authorization": "Bearer user-token"},
    ).status_code == 404
    response = local_client.get(
        "/v1/runs/legacy-run/result",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == {"ok": True}


def test_optional_auth_missing_token_is_anonymous_not_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "optional")

    async def fake_run_agent(_input_data):
        if False:
            yield None

    gateway = _load_gateway_with_runner_stub(monkeypatch, fake_run_agent, tmp_path)
    from backend.fastapi_app.agui.run_manager import RunManager

    store = EventStore(str(tmp_path / "agui_events.db"))
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "run_manager", RunManager(store))
    local_client = TestClient(gateway.app)
    store.append(
        "legacy-run",
        EventType.RUN_FINISHED.value,
        {"type": "RUN_FINISHED", "runId": "legacy-run", "result": {"ok": True}},
    )

    assert local_client.get("/v1/runs/legacy-run/result").status_code == 404


def test_v1_run_result_falls_back_to_latest_service_result(client):
    from backend.fastapi_app import main as gateway

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    first_envelope = {
        "action": "agents.list",
        "ok": True,
        "data": {"profiles": []},
        "__request_id": "req-1",
    }
    latest_envelope = {
        "action": "agents.get",
        "ok": True,
        "data": {"profile": {"name": "demo"}},
        "__request_id": "req-2",
    }
    gateway.store.append(
        run_id,
        EventType.CUSTOM.value,
        {"type": "CUSTOM", "name": "service.result", "value": first_envelope},
    )
    gateway.store.append(
        run_id,
        EventType.CUSTOM.value,
        {"type": "CUSTOM", "name": "service.result", "value": latest_envelope},
    )
    gateway.store.append(
        run_id,
        EventType.RUN_FINISHED.value,
        {"type": "RUN_FINISHED", "threadId": "thread-1", "runId": run_id, "result": None},
    )

    result_resp = client.get(f"/v1/runs/{run_id}/result")

    assert result_resp.status_code == 200
    assert result_resp.json()["result"] == latest_envelope


def test_replay_events_redacts_pii_from_raw_persisted_events(client):
    from backend.fastapi_app import main as gateway

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken"
    gateway.store.append(
        run_id,
        EventType.CUSTOM.value,
        {
            "type": "CUSTOM",
            "name": "demo.event",
            "value": {
                raw_email: "email-key",
                raw_phone: "phone-key",
                raw_dsn: "dsn-key",
                "message": f"contact {raw_email} {raw_phone}",
            },
        },
    )

    events_resp = client.get(f"/v1/runs/{run_id}/events", params={"after": 0, "follow": False})

    assert events_resp.status_code == 200
    assert raw_email not in events_resp.text
    assert raw_phone not in events_resp.text
    assert raw_dsn not in events_resp.text
    assert "alice:secret" not in events_resp.text
    assert "rawkeytoken" not in events_resp.text
    assert "[EMAIL]" in events_resp.text
    assert "[PHONE]" in events_resp.text
    assert "api_key=***" in events_resp.text


def test_v1_run_result_redacts_pii_from_raw_persisted_result(client):
    from backend.fastapi_app import main as gateway

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken"
    gateway.store.append(
        run_id,
        EventType.CUSTOM.value,
        {
            "type": "CUSTOM",
            "name": "service.result",
            "value": {
                raw_email: "email-key",
                raw_phone: "phone-key",
                raw_dsn: "dsn-key",
                "message": f"contact {raw_email} {raw_phone}",
            },
        },
    )

    result_resp = client.get(f"/v1/runs/{run_id}/result")

    assert result_resp.status_code == 200
    serialized = json.dumps(result_resp.json(), ensure_ascii=False)
    assert raw_email not in serialized
    assert raw_phone not in serialized
    assert raw_dsn not in serialized
    assert "alice:secret" not in serialized
    assert "rawkeytoken" not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized
    assert "api_key=***" in serialized


def test_v1_run_result_redacts_pii_from_raw_run_finished_result_keys(client):
    from backend.fastapi_app import main as gateway

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkeytoken"
    gateway.store.append(
        run_id,
        EventType.RUN_FINISHED.value,
        {
            "type": "RUN_FINISHED",
            "threadId": "thread-1",
            "runId": run_id,
            "result": {
                raw_email: "email-key",
                raw_phone: "phone-key",
                raw_dsn: "dsn-key",
            },
        },
    )

    result_resp = client.get(f"/v1/runs/{run_id}/result")

    assert result_resp.status_code == 200
    serialized = json.dumps(result_resp.json(), ensure_ascii=False)
    assert raw_email not in serialized
    assert raw_phone not in serialized
    assert raw_dsn not in serialized
    assert "alice:secret" not in serialized
    assert "rawkeytoken" not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized
    assert "api_key=***" in serialized


@pytest.mark.asyncio
async def test_service_action_finished_result_contains_service_result_envelope(monkeypatch):
    def handle_service_action(action, payload):
        return {"echo": payload["value"]}

    runner = _load_runner_with_service_stub(monkeypatch, handle_service_action)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(run_id)
    payload["forwardedProps"] = {
        "service_action": "demo.echo",
        "service_payload": {"value": 7, "__request_id": "req-1"},
    }
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    service_event = next(event for event in events if event.type == EventType.CUSTOM)
    finished_event = next(event for event in events if event.type == EventType.RUN_FINISHED)
    expected = {
        "action": "demo.echo",
        "ok": True,
        "data": {"echo": 7},
        "__request_id": "req-1",
    }
    assert service_event.name == "service.result"
    assert service_event.value == expected
    assert finished_event.result == expected


@pytest.mark.asyncio
async def test_service_payload_non_dict_returns_explicit_error(monkeypatch):
    runner = _load_runner_with_service_stub(monkeypatch, lambda _action, _payload: {})
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(run_id)
    payload["forwardedProps"] = {
        "service_action": "demo.echo",
        "service_payload": ["not", "an", "object"],
    }
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    error_event = next(event for event in events if event.type == EventType.RUN_ERROR)
    assert error_event.code == "service_payload_invalid"
    assert error_event.message == "service_payload must be an object"


@pytest.mark.asyncio
async def test_timeout_safe_service_action_returns_explicit_error(monkeypatch):
    def handle_service_action(_action, _payload):
        time.sleep(0.05)
        return {"late": True}

    runner = _load_runner_with_service_stub(monkeypatch, handle_service_action)
    monkeypatch.setenv("AG_UI_SERVICE_ACTION_TIMEOUT_SECONDS", "0.001")
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(run_id)
    payload["forwardedProps"] = {
        "service_action": "utils.text.analyze",
        "service_payload": {"__request_id": "req-slow"},
    }

    events = [event async for event in runner.run_agent(RunAgentInput(**payload))]

    error_event = next(event for event in events if event.type == EventType.RUN_ERROR)
    service_event = next(event for event in events if event.type == EventType.CUSTOM)
    assert error_event.code == "service_action_error"
    assert "utils.text.analyze" in error_event.message
    assert "timeout" in error_event.message
    assert service_event.value["ok"] is False


@pytest.mark.asyncio
async def test_mutating_service_action_is_not_reported_timed_out_while_thread_continues(monkeypatch):
    def handle_service_action(action, _payload):
        assert action == "agents.run"
        time.sleep(0.02)
        return {"run_id": "workflow-run"}

    runner = _load_runner_with_service_stub(monkeypatch, handle_service_action)
    monkeypatch.setenv("AG_UI_SERVICE_ACTION_TIMEOUT_SECONDS", "0.001")
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(run_id)
    payload["forwardedProps"] = {
        "service_action": "agents.run",
        "service_payload": {"__request_id": "req-mutating"},
    }

    events = [event async for event in runner.run_agent(RunAgentInput(**payload))]

    assert not [event for event in events if event.type == EventType.RUN_ERROR]
    service_event = next(event for event in events if event.type == EventType.CUSTOM)
    finished_event = next(event for event in events if event.type == EventType.RUN_FINISHED)
    assert service_event.value["ok"] is True
    assert finished_event.result["ok"] is True


@pytest.mark.asyncio
async def test_service_action_streams_and_errors_are_redacted(monkeypatch):
    raw_dsn = "postgresql://alice:secret@example.com/app"

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return value.replace("alice:secret@", "alice:***@").replace("secret", "***")
        return value

    runner = _load_runner_with_service_stub(monkeypatch, lambda _action, _payload: (_ for _ in ()).throw(RuntimeError(raw_dsn)))
    monkeypatch.setattr(runner, "_redact_payload", redact)

    class LoggingManager:
        def subscribe_run_logs(self, run_id, callback):
            callback(
                run_id,
                "error",
                f"driver failed {raw_dsn}; contact user@example.com +7 (495) 123-45-67",
                "2026-01-01T00:00:00",
            )

        def unsubscribe_run_logs(self, run_id, callback):
            return None

        def subscribe_run_progress(self, run_id, callback):
            callback(
                run_id,
                "progress",
                {"dsn": raw_dsn, "email": "user@example.com", "phone": "+7 (495) 123-45-67"},
            )

        def unsubscribe_run_progress(self, run_id, callback):
            return None

    monkeypatch.setattr(runner, "get_logging_manager", lambda *args, **kwargs: LoggingManager())

    logs_payload = _make_payload(f"run-{uuid.uuid4().hex[:8]}")
    logs_payload["forwardedProps"] = {
        "service_action": "logs.stream",
        "service_payload": {"run_id": "run-logs", "duration_seconds": 0.05},
    }
    log_events = [event async for event in runner.run_agent(RunAgentInput(**logs_payload))]
    log_event = next(event for event in log_events if event.type == EventType.CUSTOM)
    assert "secret" not in json.dumps(log_event.value)
    assert "user@example.com" not in json.dumps(log_event.value)
    assert "+7 (495) 123-45-67" not in json.dumps(log_event.value)

    progress_payload = _make_payload(f"run-{uuid.uuid4().hex[:8]}")
    progress_payload["forwardedProps"] = {
        "service_action": "progress.stream",
        "service_payload": {"run_id": "run-progress", "duration_seconds": 0.05},
    }
    progress_events = [event async for event in runner.run_agent(RunAgentInput(**progress_payload))]
    progress_event = next(event for event in progress_events if event.type == EventType.CUSTOM)
    assert "secret" not in json.dumps(progress_event.value)
    assert "user@example.com" not in json.dumps(progress_event.value)
    assert "+7 (495) 123-45-67" not in json.dumps(progress_event.value)

    error_payload = _make_payload(f"run-{uuid.uuid4().hex[:8]}")
    error_payload["forwardedProps"] = {
        "service_action": "demo.fail",
        "service_payload": {"__request_id": "req-redact"},
    }
    error_events = [event async for event in runner.run_agent(RunAgentInput(**error_payload))]
    error_event = next(event for event in error_events if event.type == EventType.RUN_ERROR)
    service_event = next(event for event in error_events if event.type == EventType.CUSTOM)
    assert "secret" not in error_event.message
    assert "secret" not in json.dumps(service_event.value)


@pytest.mark.asyncio
async def test_stream_service_actions_require_admin_role(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal, principal_context

    runner = _load_runner_with_service_stub(monkeypatch, lambda _action, _payload: {})

    class LoggingManager:
        def subscribe_all_logs(self, _callback):
            raise AssertionError("user must not subscribe to all logs")

        def unsubscribe_all_logs(self, _callback):
            return None

    monkeypatch.setattr(runner, "get_logging_manager", lambda *args, **kwargs: LoggingManager())

    payload = _make_payload(f"run-{uuid.uuid4().hex[:8]}")
    payload["forwardedProps"] = {
        "service_action": "logs.stream",
        "service_payload": {"run_id": "*", "duration_seconds": 0.01},
    }
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    with principal_context(user):
        events = [event async for event in runner.run_agent(RunAgentInput(**payload))]

    error_event = next(event for event in events if event.type == EventType.RUN_ERROR)
    assert error_event.code == "service_action_error"
    assert "requires role" in error_event.message


@pytest.mark.asyncio
async def test_live_stream_has_independent_followers(tmp_path, monkeypatch):
    async def fake_run_agent(input_data):
        yield RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            input=input_data,
            timestamp=int(time.time() * 1000),
        )
        await asyncio.sleep(0.01)
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="demo.event",
            value={"step": 1},
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"status": "ok"},
            timestamp=int(time.time() * 1000),
        )

    _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)
    from backend.fastapi_app.agui.run_manager import RunManager

    manager = RunManager(EventStore(str(tmp_path / "agui_events.db")))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    input_data = RunAgentInput(**_make_payload(run_id))
    await manager.start_run(input_data)

    async def collect():
        return [event.type.value async for event in manager.stream_live(run_id)]

    first, second = await asyncio.wait_for(
        asyncio.gather(asyncio.create_task(collect()), asyncio.create_task(collect())),
        timeout=2,
    )

    assert first == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
    assert second == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_run_manager_redacts_persisted_and_live_events(tmp_path, monkeypatch):
    raw_dsn = "postgresql://alice:secret@example.com/app"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"

    async def fake_run_agent(input_data):
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="demo.event",
            value={
                "error": f"driver failed {raw_dsn}; contact {raw_email} {raw_phone}",
                "password": "secret",
            },
            timestamp=int(time.time() * 1000),
        )
        yield RunErrorEvent(
            type=EventType.RUN_ERROR,
            message=f"driver failed {raw_dsn}; contact {raw_email} {raw_phone}",
            code="demo_error",
            timestamp=int(time.time() * 1000),
        )

    rm = _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return value.replace("alice:secret@", "alice:***@").replace("secret", "***")
        return value

    monkeypatch.setattr(rm, "_redact_payload", redact)

    manager = rm.RunManager(EventStore(str(tmp_path / "agui_events.db")))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    events = [event async for event in manager.stream_live(run_id)]
    stored = [event.payload for event in manager._store.list_after(run_id, 0)]
    serialized = json.dumps(
        {
            "live": [event.model_dump(mode="json", by_alias=True) for event in events],
            "stored": stored,
        },
        ensure_ascii=False,
    )

    assert "secret" not in serialized
    assert raw_email not in serialized
    assert raw_phone not in serialized
    assert "alice:***@example.com" in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized


@pytest.mark.asyncio
async def test_run_manager_redacts_cyclic_custom_event_json_safe(tmp_path, monkeypatch):
    async def fake_run_agent(input_data):
        value = {"password": "raw-secret"}
        value["self"] = value
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="demo.event",
            value=value,
            timestamp=int(time.time() * 1000),
        )

    rm = _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)
    manager = rm.RunManager(EventStore(str(tmp_path / "agui_events.db")))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    events = [event async for event in manager.stream_live(run_id)]
    stored = [event.payload for event in manager._store.list_after(run_id, 0)]
    serialized = json.dumps(
        {
            "live": [event.model_dump(mode="json", by_alias=True) for event in events],
            "stored": stored,
        },
        ensure_ascii=False,
    )

    assert "raw-secret" not in serialized
    assert "[Circular]" in serialized
    assert "<redacted>" in serialized


def test_replay_follow_after_boundary_has_no_duplicates(client, monkeypatch):
    async def fake_run_agent(input_data):
        yield RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            input=input_data,
            timestamp=int(time.time() * 1000),
        )
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="demo.event",
            value={"step": 1},
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"status": "ok"},
            timestamp=int(time.time() * 1000),
        )

    from backend.fastapi_app.agui import run_manager as rm

    monkeypatch.setattr(rm, "run_agent", fake_run_agent)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    create_resp = client.post("/v1/runs", json=_make_payload(run_id))
    assert create_resp.status_code == 200
    run_id = create_resp.json()["runId"]

    for _ in range(20):
        status = client.get(f"/v1/runs/{run_id}").json()
        if status["status"] == "finished":
            break
        time.sleep(0.05)

    events_resp = client.get(f"/v1/runs/{run_id}/events", params={"after": 1, "follow": True})
    assert events_resp.status_code == 200
    events = _read_sse_events(events_resp.text)

    assert [event.get("type") for event in events] == ["CUSTOM", "RUN_FINISHED"]


def test_v1_runs_cancel(client, monkeypatch):
    async def fake_run_agent(input_data):
        await asyncio.sleep(5)
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"status": "late"},
            timestamp=int(time.time() * 1000),
        )

    from backend.fastapi_app.agui import run_manager as rm

    monkeypatch.setattr(rm, "run_agent", fake_run_agent)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(run_id)
    create_resp = client.post("/v1/runs", json=payload)
    assert create_resp.status_code == 200
    run_id = create_resp.json()["runId"]

    cancel_resp = client.post(f"/v1/runs/{run_id}/cancel")
    assert cancel_resp.status_code == 200

    for _ in range(50):
        status = client.get(f"/v1/runs/{run_id}").json()
        if status["status"] == "cancelled":
            break
        time.sleep(0.05)

    assert status["status"] == "cancelled"


@pytest.mark.parametrize(
    "path_template",
    ("/agent/{run_id}/cancel", "/v1/runs/{run_id}/cancel"),
)
@pytest.mark.parametrize(
    ("outcome", "expected_cancelled"),
    (("cancelled", True), ("completed", False), ("survives", False)),
)
def test_uncached_text_to_sql_cancel_alias_uses_one_owner_service_attempt(
    client,
    monkeypatch,
    path_template,
    outcome,
    expected_cancelled,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager
    import backend.fastapi_app.agui.run_manager as run_manager_module

    run_id = f"uncached-{outcome}-{uuid.uuid4().hex[:8]}"
    incarnation = f"inc-{run_id}"
    service_payload = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "idempotency_key": f"key-{run_id}",
    }
    gateway.store.create_or_get_run(
        principal=system_principal_for_disabled_mode(),
        run_kind="text_to_sql",
        idempotency_key=service_payload["idempotency_key"],
        request_fingerprint=canonical_text_to_sql_start_fingerprint(
            service_payload
        ),
        proposed_run_id=run_id,
        thread_id=f"thread-{run_id}",
    )
    assert gateway.store.reserve_workflow_run(
        run_id,
        incarnation,
        f"thread-{run_id}",
        "text_to_sql_pipeline",
    )
    assert gateway.store.set_worker_pid(run_id, 54321)
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.05)
    calls = []

    def fake_handle(action, payload, principal=None, **_kwargs):
        calls.append((action, payload, principal))
        if outcome in {"cancelled", "completed"}:
            status = "cancelled" if outcome == "cancelled" else "succeeded"
            gateway.store.finalize_run_with_event(
                run_id,
                _text_to_sql_result(
                    run_id,
                    incarnation,
                    status=status,
                ),
            )
        return {"cancelled": outcome == "cancelled"}

    _install_service_action_stub(monkeypatch, fake_handle)

    response = client.post(path_template.format(run_id=run_id))

    assert response.status_code == 200
    assert response.json() == {"cancelled": expected_cancelled}
    assert calls == [
        (
            "workflows.cancel",
            {"run_id": run_id},
            system_principal_for_disabled_mode(),
        )
    ]


def test_uncached_cancel_skips_terminal_and_non_text_to_sql_runs(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager

    principal = system_principal_for_disabled_mode()
    terminal_run = f"terminal-{uuid.uuid4().hex[:8]}"
    incarnation = f"inc-{terminal_run}"
    service_payload = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "idempotency_key": f"key-{terminal_run}",
    }
    gateway.store.create_or_get_run(
        principal=principal,
        run_kind="text_to_sql",
        idempotency_key=service_payload["idempotency_key"],
        request_fingerprint=canonical_text_to_sql_start_fingerprint(
            service_payload
        ),
        proposed_run_id=terminal_run,
        thread_id=f"thread-{terminal_run}",
    )
    assert gateway.store.reserve_workflow_run(
        terminal_run,
        incarnation,
        f"thread-{terminal_run}",
        "text_to_sql_pipeline",
    )
    gateway.store.finalize_run_with_event(
        terminal_run,
        _text_to_sql_result(
            terminal_run,
            incarnation,
            status="succeeded",
        ),
    )
    generic_run = f"generic-{uuid.uuid4().hex[:8]}"
    gateway.store.create_run(
        generic_run,
        f"thread-{generic_run}",
        principal,
    )
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))
    calls = []
    _install_service_action_stub(
        monkeypatch,
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    terminal_response = client.post(f"/agent/{terminal_run}/cancel")
    generic_response = client.post(f"/v1/runs/{generic_run}/cancel")

    assert terminal_response.status_code == 200
    assert terminal_response.json() == {"cancelled": False}
    assert generic_response.status_code == 200
    assert generic_response.json() == {"cancelled": False}
    assert calls == []


def test_cached_text_to_sql_cancel_failure_does_not_use_uncached_fallback(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import system_principal_for_disabled_mode
    from backend.fastapi_app.agui.run_manager import RunManager

    run_id = f"cached-{uuid.uuid4().hex[:8]}"
    service_payload = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "idempotency_key": f"key-{run_id}",
    }
    stored, created = gateway.store.create_or_get_run(
        principal=system_principal_for_disabled_mode(),
        run_kind="text_to_sql",
        idempotency_key=service_payload["idempotency_key"],
        request_fingerprint=canonical_text_to_sql_start_fingerprint(
            service_payload
        ),
        proposed_run_id=run_id,
        thread_id=f"thread-{run_id}",
    )
    assert created is True
    manager = RunManager(gateway.store)
    manager._runs[run_id] = manager._info_from_stored(stored)
    monkeypatch.setattr(gateway, "run_manager", manager)
    calls = []
    _install_service_action_stub(
        monkeypatch,
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = client.post(f"/agent/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": False}
    assert calls == []


def test_uncached_text_to_sql_cancel_preserves_owner_and_admin_access(
    client,
    monkeypatch,
):
    from backend.fastapi_app import main as gateway
    from backend.fastapi_app.agui._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
    )
    from backend.fastapi_app.agui.auth import Principal
    from backend.fastapi_app.agui.run_manager import RunManager

    owner = Principal(
        subject="alice",
        tenant_id="tenant-1",
        roles=frozenset({"user"}),
    )
    admin = Principal(
        subject="admin",
        tenant_id="other-tenant",
        roles=frozenset({"admin", "user"}),
    )
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv(
        "AG_UI_AUTH_TOKEN_MAP",
        json.dumps(
            {
                "owner-token": {
                    "subject": "alice",
                    "tenant_id": "tenant-1",
                    "roles": ["user"],
                },
                "admin-token": {
                    "subject": "admin",
                    "tenant_id": "other-tenant",
                    "roles": ["admin", "user"],
                },
                "other-token": {
                    "subject": "bob",
                    "tenant_id": "tenant-1",
                    "roles": ["user"],
                },
            }
        ),
    )
    incarnations = {}

    def seed(run_id):
        service_payload = {
            "query": "count orders",
            "dsn": "sqlite:///example.db",
            "idempotency_key": f"key-{run_id}",
        }
        gateway.store.create_or_get_run(
            principal=owner,
            run_kind="text_to_sql",
            idempotency_key=service_payload["idempotency_key"],
            request_fingerprint=canonical_text_to_sql_start_fingerprint(
                service_payload
            ),
            proposed_run_id=run_id,
            thread_id=f"thread-{run_id}",
        )
        incarnation = f"inc-{run_id}"
        incarnations[run_id] = incarnation
        assert gateway.store.reserve_workflow_run(
            run_id,
            incarnation,
            f"thread-{run_id}",
            "text_to_sql_pipeline",
        )

    owner_run = f"owner-{uuid.uuid4().hex[:8]}"
    admin_run = f"admin-{uuid.uuid4().hex[:8]}"
    denied_run = f"denied-{uuid.uuid4().hex[:8]}"
    for run_id in (owner_run, admin_run, denied_run):
        seed(run_id)
    monkeypatch.setattr(gateway, "run_manager", RunManager(gateway.store))
    calls = []

    def fake_handle(action, payload, principal=None, **_kwargs):
        run_id = payload["run_id"]
        calls.append((action, run_id, principal))
        gateway.store.finalize_run_with_event(
            run_id,
            _text_to_sql_result(
                run_id,
                incarnations[run_id],
                status="cancelled",
            ),
        )
        return {"cancelled": True}

    _install_service_action_stub(monkeypatch, fake_handle)

    owner_response = client.post(
        f"/agent/{owner_run}/cancel",
        headers={"Authorization": "Bearer owner-token"},
    )
    admin_response = client.post(
        f"/v1/runs/{admin_run}/cancel",
        headers={"Authorization": "Bearer admin-token"},
    )
    denied_response = client.post(
        f"/agent/{denied_run}/cancel",
        headers={"Authorization": "Bearer other-token"},
    )

    assert owner_response.status_code == 200
    assert owner_response.json() == {"cancelled": True}
    assert admin_response.status_code == 200
    assert admin_response.json() == {"cancelled": True}
    assert denied_response.status_code == 404
    assert calls == [
        ("workflows.cancel", owner_run, owner),
        ("workflows.cancel", admin_run, admin),
    ]


def test_v1_run_result_uses_workflow_result_event(client):
    from backend.fastapi_app import main as gateway

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    envelope = {
        "type": "workflow_result",
        "workflow_run_id": "run-workflow-123",
        "workflow_name": "demo_pipeline",
        "status": "cancelled",
        "final_output": None,
        "artifacts_ref": None,
        "code": "cancelled",
    }
    gateway.store.append(
        run_id,
        EventType.CUSTOM.value,
        {"type": "CUSTOM", "name": "workflow.result", "value": envelope},
    )

    resp = client.get(f"/v1/runs/{run_id}/result")

    assert resp.status_code == 200
    assert resp.json()["result"] == envelope
