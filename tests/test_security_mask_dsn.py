"""EPIC 7.24: централизованный dry-run check + интеграция DSN-маскировки.

Покрывает `is_dry_run_only` из `custom_tools.text_to_sql.utils` и проверяет,
что `sql_explain` маскирует DSN в error_message (через общий хелпер
``mask_dsn`` из EPIC 7.3).

Подробные unit-тесты для самих `mask_dsn`/`mask_dsn_value` живут в
EPIC 7.3 (db_exec блок). Здесь — только smoke + контракт интеграции для
сценариев 7.24.
"""
from __future__ import annotations

import logging
import json
import urllib.parse

import pytest

from custom_tools.text_to_sql.utils import (
    coerce_strict_bool,
    dsn_to_sanitized_name,
    is_dry_run_only,
    mask_dsn,
    mask_dsn_value,
)


# ---------------------------------------------------------------------------
# mask_dsn / mask_dsn_value smoke (контракт для EPIC 7.24)
# ---------------------------------------------------------------------------
def test_mask_dsn_value_postgres_with_password():
    masked = mask_dsn_value("postgresql://user:secret@host:5432/db")
    assert "secret" not in masked
    assert "user" not in masked
    assert "***:***@host:5432/db" in masked


def test_mask_dsn_uri_empty_username_password_masked():
    masked = mask_dsn("postgresql://:topsecret@host:5432/db")

    assert "topsecret" not in masked
    assert "://:topsecret@" not in masked
    assert "postgresql://***:***@host:5432/db" in masked


def test_mask_dsn_in_arbitrary_text_strips_password():
    text = "connection failed: postgresql://alice:topsecret@db:5432/app — timeout"
    masked = mask_dsn(text)
    assert "topsecret" not in masked
    assert "alice" not in masked
    assert "***:***@db:5432/app" in masked


def test_mask_dsn_no_password_passthrough():
    assert mask_dsn("duckdb:///tmp/x.db") == "duckdb:///tmp/x.db"
    assert mask_dsn("") == ""


def test_mask_dsn_url_encoded_password():
    encoded = urllib.parse.quote("p@ssw0rd!", safe="")
    dsn = f"postgresql://alice:{encoded}@example.com:5432/app"
    masked = mask_dsn(dsn)
    assert "p%40ssw0rd%21" not in masked
    assert "p@ssw0rd!" not in masked
    assert "alice" not in masked
    assert "***:***@example.com:5432/app" in masked


def test_mask_dsn_in_text_uses_db_dsn_env(monkeypatch):
    """Если в текст ошибки попал env DB_DSN целиком — он тоже маскируется."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:secret@h:5432/db")
    text = "fatal: postgresql://u:secret@h:5432/db is down"
    masked = mask_dsn(text)
    assert "secret" not in masked
    assert "***:***@" in masked


# ---------------------------------------------------------------------------
# W7-T1: libpq keyword/value-форма (host=... password=...)
# ---------------------------------------------------------------------------
def test_mask_dsn_libpq_format_password_masked():
    """libpq форма ``host=... password=secret`` маскируется наравне с URI."""
    dsn = "host=db.example.com port=5432 user=alice password=topsecret dbname=app"
    masked = mask_dsn(dsn)
    assert "topsecret" not in masked
    assert "password=***" in masked
    # Не пароль — оставляем для диагностики.
    assert "host=db.example.com" in masked
    assert "user=***" in masked
    assert "user=alice" not in masked
    assert "dbname=app" in masked


def test_dsn_to_sanitized_name_libpq_omits_credentials():
    dsn = "host=db.example.com port=5432 user=alice password=topsecret dbname=app"

    sanitized = dsn_to_sanitized_name(dsn)

    assert "alice" not in sanitized
    assert "topsecret" not in sanitized
    assert "password" not in sanitized
    assert "db_example_com" in sanitized
    assert "app" in sanitized


def test_dsn_to_sanitized_name_odbc_omits_credentials_and_tokens():
    dsn = (
        "Driver={ODBC Driver 17};Server=db.example.com;Database=app;"
        "UID=alice;PWD=topsecret;AccessToken=rawtoken"
    )

    sanitized = dsn_to_sanitized_name(dsn)

    assert "alice" not in sanitized
    assert "topsecret" not in sanitized
    assert "rawtoken" not in sanitized
    assert "db_example_com" in sanitized
    assert "app" in sanitized


def test_dsn_to_sanitized_name_pyodbc_url_uses_odbc_connect_identity():
    odbc = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;"
        "Database=orders;UID=alice;PWD=topsecret"
    )
    dsn = f"mssql+pyodbc:///?odbc_connect={odbc}"

    sanitized = dsn_to_sanitized_name(dsn)

    assert "db1_example_com" in sanitized
    assert "orders" in sanitized
    assert "alice" not in sanitized
    assert "topsecret" not in sanitized


def test_dsn_to_sanitized_name_pyodbc_url_separates_databases():
    orders = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;Database=orders;UID=a;PWD=one"
    )
    analytics = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;Database=analytics;UID=b;PWD=two"
    )

    assert dsn_to_sanitized_name(f"mssql+pyodbc:///?odbc_connect={orders}") != (
        dsn_to_sanitized_name(f"mssql+pyodbc:///?odbc_connect={analytics}")
    )


def test_mask_dsn_value_pyodbc_url_redacts_encoded_odbc_connect():
    odbc = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;"
        "Database=orders;UID=alice;PWD=topsecret"
    )

    masked = mask_dsn_value(f"mssql+pyodbc:///?odbc_connect={odbc}&driver=ODBC+Driver+17")

    assert "alice" not in masked
    assert "topsecret" not in masked
    assert "UID%3D" not in masked
    assert "PWD%3D" not in masked
    assert "odbc_connect=***" in masked
    assert "driver=ODBC+Driver+17" in masked


def test_dsn_to_sanitized_name_unrecognized_dsn_is_non_reversible_hash():
    raw = "opaque tenant alice topsecret"

    sanitized = dsn_to_sanitized_name(raw)

    assert sanitized.startswith("db_")
    assert "alice" not in sanitized
    assert "topsecret" not in sanitized


def test_mask_dsn_libpq_semicolon_password_masks_whole_value():
    dsn = "host=db.example.com port=5432 user=alice password=top;secret dbname=app"

    masked = mask_dsn(dsn)

    assert "top;secret" not in masked
    assert ";secret" not in masked
    assert "password=***" in masked
    assert "dbname=app" in masked


def test_mask_dsn_libpq_format_passwd_alias_masked():
    """Исторический libpq-алиас ``passwd=`` тоже маскируется."""
    dsn = "host=h passwd=oldsecret user=u"
    masked = mask_dsn(dsn)
    assert "oldsecret" not in masked
    assert "passwd=***" in masked


def test_mask_dsn_keyword_user_fields_masked():
    text = (
        "Driver={ODBC Driver 17};Server=db;UID=alice;PWD=topsecret "
        "host=db user=bob username=carol user_id=tenant42 password=secret dbname=app"
    )

    masked = mask_dsn(text)

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
        "secret",
    ):
        assert raw_fragment not in masked
    assert "UID=***" in masked
    assert "PWD=***" in masked
    assert "user=***" in masked
    assert "username=***" in masked
    assert "user_id=***" in masked
    assert "password=***" in masked


def test_mask_dsn_plain_secret_assignments_masked():
    text = (
        "driver failed: token=abc123 api_key=xyz987 secret=hunter "
        "auth=authsecret key=keysecret access_token=accesssecret "
        "api%5Fkey=encodedsecret access%5Ftoken=encodedtoken "
        "refresh_token=refreshsecret client_secret=clientsecret "
        "accessToken=camel-access refreshToken: camel-refresh "
        "clientSecret=camel-client dbPassword: camel-password "
        "AWS_SECRET_ACCESS_KEY=aws-secret secret_key: secret-key privateKey=private-key "
        "OPENAI_API_KEY=sk-openai OPENAI_API_KEY_DB=sk-openai-db "
        "https://example.com/path?openai_api_key=sk-query&max_tokens=32768 "
        "https://example.com/path?clientSecret=query-client&refreshToken=query-refresh "
        "api_key: colon-api password: colon-pass token: colon-token "
        "max_tokens: 32768 token_count: 12 "
        "Authorization: Bearer headersecret max_tokens=32768 token_count=12 "
        '{"password": "jsonpass", "token": "jsontoken", "secret": "jsonsecret", '
        '"authorization": "Bearer jsonauth", "openai_api_key": "sk-json", '
        '"accessToken": "json-access", "privateKey": "json-private", '
        '"max_tokens": 32768}'
    )

    masked = mask_dsn(text)

    for raw in (
        "abc123",
        "xyz987",
        "hunter",
        "authsecret",
        "keysecret",
        "accesssecret",
        "encodedsecret",
        "encodedtoken",
        "refreshsecret",
        "clientsecret",
        "camel-access",
        "camel-refresh",
        "camel-client",
        "camel-password",
        "aws-secret",
        "secret-key",
        "private-key",
        "query-client",
        "query-refresh",
        "headersecret",
        "sk-openai",
        "sk-openai-db",
        "sk-query",
        "colon-api",
        "colon-pass",
        "colon-token",
        "jsonpass",
        "jsontoken",
        "jsonsecret",
        "jsonauth",
        "sk-json",
        "json-access",
        "json-private",
    ):
        assert raw not in masked
    assert "token=***" in masked
    assert "api_key=***" in masked
    assert "secret=***" in masked
    assert "auth=***" in masked
    assert "key=***" in masked
    assert "access_token=***" in masked
    assert "api%5Fkey=***" in masked
    assert "access%5Ftoken=***" in masked
    assert "refresh_token=***" in masked
    assert "client_secret=***" in masked
    assert "accessToken=***" in masked
    assert "refreshToken: ***" in masked
    assert "clientSecret=***" in masked
    assert "dbPassword: ***" in masked
    assert "AWS_SECRET_ACCESS_KEY=***" in masked
    assert "secret_key: ***" in masked
    assert "privateKey=***" in masked
    assert "OPENAI_API_KEY=***" in masked
    assert "OPENAI_API_KEY_DB=***" in masked
    assert "openai_api_key=***" in masked
    assert "clientSecret=***" in masked
    assert "refreshToken=***" in masked
    assert "api_key: ***" in masked
    assert "password: ***" in masked
    assert "token: ***" in masked
    assert "Authorization: ***" in masked
    assert '"password": "***"' in masked
    assert '"token": "***"' in masked
    assert '"secret": "***"' in masked
    assert '"authorization": "***"' in masked
    assert '"openai_api_key": "***"' in masked
    assert '"accessToken": "***"' in masked
    assert '"privateKey": "***"' in masked
    assert "max_tokens=32768" in masked
    assert "token_count=12" in masked
    assert "max_tokens: 32768" in masked
    assert "token_count: 12" in masked
    assert '"max_tokens": 32768' in masked


def test_mask_dsn_libpq_in_error_text():
    """OperationalError с libpq-строкой в тексте: пароль не уходит."""
    text = (
        "could not connect to server: host=db port=5432 "
        "user=svc password=ZXcvbn123! sslmode=require"
    )
    masked = mask_dsn(text)
    assert "ZXcvbn123!" not in masked
    assert "password=***" in masked


def test_mask_dsn_libpq_case_insensitive_key():
    """libpq case-insensitive: PASSWORD= в верхнем регистре тоже ловится."""
    dsn = "host=h PASSWORD=Secret123 user=u"
    masked = mask_dsn(dsn)
    assert "Secret123" not in masked
    # Регистр исходного ключа сохраняется (для читаемости логов).
    assert "PASSWORD=***" in masked


def test_mask_dsn_libpq_does_not_break_uri_logic():
    """URI-форма продолжает работать после введения libpq-regex."""
    masked = mask_dsn_value("postgresql://user:secret@host:5432/db")
    assert "secret" not in masked
    assert "***:***@host:5432/db" in masked


def test_mask_dsn_libpq_passfile_not_masked():
    """passfile=path — это путь к pgpass-файлу, не пароль; маскировать не надо."""
    dsn = "host=h passfile=/home/user/.pgpass user=u"
    masked = mask_dsn(dsn)
    # passfile значение остаётся — это путь, полезный для диагностики.
    assert "passfile=/home/user/.pgpass" in masked


def test_tool_manager_redacts_dsn_runtime_metadata(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "postgresql://alice:topsecret@db.example.com:5432/app"

    def _tool(**kwargs):
        assert kwargs["dsn"] == raw_dsn
        return {"ok": True, "dsn": raw_dsn}

    manager = ToolManager()
    manager.run_tool(
        tool_name="secure_db_executor",
        tool_function=_tool,
        task_description="run secure db executor",
        session_id="run-redacted",
        dsn=raw_dsn,
    )

    active_run_text = str(manager.active_runs["run-redacted"])
    span_text = str(telemetry_manager.span.attrs)
    assert "topsecret" not in active_run_text
    assert "alice" not in active_run_text
    assert "topsecret" not in span_text
    assert "alice" not in span_text
    assert "***" in active_run_text


def test_tool_manager_redacts_task_description_before_runtime_and_telemetry(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def set_attributes(self, attrs):
            pass

        def set_attribute(self, key, value):
            pass

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.start_kwargs = None

        def start_run_trace(self, **kwargs):
            self.start_kwargs = kwargs
            return _Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "postgresql://alice:topsecret@db.example.com:5432/app"

    def _tool(**_kwargs):
        active_text = str(manager.active_runs["run-task-redacted"])
        telemetry_text = str(telemetry_manager.start_kwargs)
        assert "topsecret" not in active_text
        assert "alice" not in active_text
        assert "topsecret" not in telemetry_text
        assert "alice" not in telemetry_text
        return {"ok": True}

    manager = ToolManager()
    manager.run_tool(
        tool_name="secure_db_executor",
        tool_function=_tool,
        task_description=f"run against {raw_dsn}",
        session_id="run-task-redacted",
    )


def test_smolagents_telemetry_redacts_shared_dsn_credentials(tmp_path):
    from telemetry.smolagents_telemetry import LocalJSONLExporter, StatusCode

    class _Context:
        trace_id = 0x123
        span_id = 0x456

    class _Status:
        status_code = StatusCode.ERROR
        description = (
            "failed postgresql://alice:topsecret@db/app?clientSecret=query-client "
            "Driver={ODBC Driver 17};UID=odbc_user;PWD=odbc_pass "
            "host=db user=libpq_user password=libpq_pass refreshToken=refresh-token"
        )

    class _Event:
        name = "db.error"
        timestamp = 1_700_000_000_000_000_000
        attributes = {
            "message": (
                "postgresql://bob:pgpass@db/app "
                "clientSecret=event-client refreshToken=event-refresh "
                "UID=event_uid PWD=event_pwd user_id=tenant42"
            )
        }

    class _Span:
        name = "run postgresql://carol:namepass@db/app"
        attributes = {
            "run_id": "run-telemetry-redaction",
            "dsn": "postgresql://dave:spanpass@db/app?api_key=span-key",
            "message": "username=span_user user=span_libpq password=span_pw",
            "UID": "direct-uid",
            "user": "direct-user",
            "username": "direct-username",
            "user_id": "direct-user-id",
            "clientSecret": "span-client-secret",
            "refreshToken": "span-refresh-token",
        }
        context = _Context()
        parent = None
        status = _Status()
        events = [_Event()]
        start_time = 1_700_000_000_000_000_000
        end_time = 1_700_000_001_000_000_000

    trace_event = LocalJSONLExporter(str(tmp_path))._convert_span_to_event(_Span())

    assert trace_event is not None
    serialized = json.dumps(trace_event.to_dict(), ensure_ascii=False)
    for raw in (
        "alice",
        "topsecret",
        "query-client",
        "odbc_user",
        "odbc_pass",
        "libpq_user",
        "libpq_pass",
        "refresh-token",
        "bob",
        "pgpass",
        "event-client",
        "event-refresh",
        "event_uid",
        "event_pwd",
        "tenant42",
        "carol",
        "namepass",
        "dave",
        "spanpass",
        "span-key",
        "span_user",
        "span_libpq",
        "span_pw",
        "direct-uid",
        "direct-user",
        "direct-username",
        "direct-user-id",
        "span-client-secret",
        "span-refresh-token",
    ):
        assert raw not in serialized
    assert "***:***@db" in serialized
    assert "UID=***" in serialized
    assert "PWD=***" in serialized
    assert "user=***" in serialized
    assert "username=***" in serialized
    assert "user_id=***" in serialized
    assert "clientSecret=***" in serialized
    assert "refreshToken=***" in serialized
    assert trace_event.attributes["run_id"] == "run-telemetry-redaction"
    assert trace_event.attributes["UID"] == "<redacted>"
    assert trace_event.attributes["user"] == "<redacted>"
    assert trace_event.attributes["username"] == "<redacted>"
    assert trace_event.attributes["user_id"] == "<redacted>"
    assert trace_event.attributes["clientSecret"] == "<redacted>"
    assert trace_event.attributes["refreshToken"] == "<redacted>"


def test_smolagents_telemetry_redacts_secret_shaped_correlation_attributes():
    from telemetry.smolagents_telemetry import _redact_payload

    redacted = _redact_payload({
        "run_id": "postgresql://alice:topsecret@db/app?api_key=raw-key",
        "thread_id": "host=db user=bob password=secret",
        "request_id": "clientSecret=req-client refreshToken=req-refresh",
    })
    serialized = json.dumps(redacted, ensure_ascii=False)

    for raw in (
        "alice",
        "topsecret",
        "raw-key",
        "bob",
        "secret",
        "req-client",
        "req-refresh",
    ):
        assert raw not in serialized
    assert "***:***@db" in serialized
    assert "api_key=***" in serialized
    assert "user=***" in serialized
    assert "password=***" in serialized
    assert "clientSecret=***" in serialized
    assert "refreshToken=***" in serialized


def test_smolagents_telemetry_redacts_top_level_trace_run_id(tmp_path):
    from telemetry.smolagents_telemetry import LocalJSONLExporter

    raw_run_id = "postgresql://alice:topsecret@db/app?api_key=raw-key"

    class _Context:
        trace_id = 0x321
        span_id = 0x654

    class _Span:
        name = "safe span"
        attributes = {"run_id": raw_run_id}
        context = _Context()
        parent = None
        status = None
        events = []
        start_time = 1_700_000_000_000_000_000
        end_time = 1_700_000_001_000_000_000

    exporter = LocalJSONLExporter(str(tmp_path))
    trace_event = exporter._convert_span_to_event(_Span())

    assert trace_event is not None
    event_dict = trace_event.to_dict()
    serialized = json.dumps(event_dict, ensure_ascii=False)
    for raw in ("alice", "topsecret", "raw-key", raw_run_id):
        assert raw not in serialized
    assert event_dict["run_id"].startswith("redacted-run-")


def _quote_plus_depth(value: str, depth: int) -> str:
    encoded = value
    for _ in range(depth):
        encoded = urllib.parse.quote_plus(encoded)
    return encoded


def _assert_smolagents_telemetry_redacts_url_encoded_secret_values(
    tmp_path,
    encode_depth: int,
    *,
    expect_decoded_mask: bool = True,
):
    from telemetry.smolagents_telemetry import LocalJSONLExporter, StatusCode

    raw_dsn = "postgresql://alice:topsecret@db/app?api_key=raw-key"
    raw_assign = "host=db user=bob password=secret"
    encoded_dsn = _quote_plus_depth(raw_dsn, encode_depth)
    encoded_assign = _quote_plus_depth(raw_assign, encode_depth)

    class _Context:
        trace_id = 0x421
        span_id = 0x754

    class _Status:
        status_code = StatusCode.ERROR
        description = f"failed {encoded_assign}"

    class _Event:
        name = encoded_dsn
        timestamp = 1_700_000_000_000_000_000
        attributes = {"message": encoded_assign, "dsn": encoded_dsn}

    class _Span:
        name = f"span {encoded_dsn}"
        attributes = {"run_id": encoded_dsn, "message": encoded_assign}
        context = _Context()
        parent = None
        status = _Status()
        events = [_Event()]
        start_time = 1_700_000_000_000_000_000
        end_time = 1_700_000_001_000_000_000

    exporter = LocalJSONLExporter(str(tmp_path))
    trace_event = exporter._convert_span_to_event(_Span())

    assert trace_event is not None
    exporter.export([_Span()])
    serialized = json.dumps(trace_event.to_dict(), ensure_ascii=False)
    filenames = " ".join(path.name for path in tmp_path.glob("*.jsonl"))
    for raw in (
        encoded_dsn,
        encoded_assign,
        urllib.parse.quote_plus(raw_dsn),
        urllib.parse.quote_plus(raw_assign),
        "alice",
        "topsecret",
        "raw-key",
        "bob",
        "secret",
    ):
        assert raw not in serialized
        assert raw not in filenames
    assert trace_event.run_id.startswith("redacted-run-")
    if expect_decoded_mask:
        assert "***:***@db" in serialized
        assert "user=***" in serialized
        assert "password=***" in serialized


def test_smolagents_telemetry_redacts_url_encoded_secret_values(tmp_path):
    _assert_smolagents_telemetry_redacts_url_encoded_secret_values(
        tmp_path,
        encode_depth=1,
    )


def test_smolagents_telemetry_redacts_double_url_encoded_secret_values(tmp_path):
    _assert_smolagents_telemetry_redacts_url_encoded_secret_values(
        tmp_path,
        encode_depth=2,
    )


def test_smolagents_telemetry_redacts_decode_depth_exhausted_secret_values(tmp_path):
    _assert_smolagents_telemetry_redacts_url_encoded_secret_values(
        tmp_path,
        encode_depth=6,
        expect_decoded_mask=False,
    )


def test_smolagents_telemetry_sanitizes_run_id_filename_escape(tmp_path):
    from telemetry.smolagents_telemetry import LocalJSONLExporter

    class _Context:
        trace_id = 0x777
        span_id = 0x888

    class _Span:
        name = "safe span"
        attributes = {"run_id": "../escape"}
        context = _Context()
        parent = None
        status = None
        events = []
        start_time = 1_700_000_000_000_000_000
        end_time = 1_700_000_001_000_000_000

    exporter = LocalJSONLExporter(str(tmp_path / "traces"))
    trace_event = exporter._convert_span_to_event(_Span())

    assert trace_event is not None
    assert trace_event.run_id.startswith("safe-run-")
    exporter.export([_Span()])
    assert not (tmp_path / "escape.jsonl").exists()
    assert list((tmp_path / "traces").glob("safe-run-*.jsonl"))


def test_smolagents_telemetry_write_sink_rejects_run_id_filename_escape(tmp_path, caplog):
    from datetime import datetime, timezone
    from telemetry.smolagents_telemetry import LocalJSONLExporter, TraceEvent

    exporter = LocalJSONLExporter(str(tmp_path / "traces"))
    event = TraceEvent(
        run_id="../escape",
        span_id="1",
        parent_span_id=None,
        name="escaped",
        start_time=datetime.now(timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )

    exporter._write_trace_event(event)

    assert not (tmp_path / "escape.jsonl").exists()
    assert not list((tmp_path / "traces").glob("*.jsonl"))

    secret_run_id = "postgresql://alice:topsecret@db/app?api_key=raw-key"
    secret_event = TraceEvent(
        run_id=secret_run_id,
        span_id="2",
        parent_span_id=None,
        name="escaped",
        start_time=datetime.now(timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="telemetry.smolagents_telemetry"):
        exporter._write_trace_event(secret_event)
    for raw in (secret_run_id, "alice", "topsecret", "raw-key"):
        assert raw not in caplog.text


def test_smolagents_telemetry_safe_trace_ids_remain_readable(tmp_path):
    from datetime import datetime, timezone
    from telemetry.smolagents_telemetry import LocalJSONLExporter, SmolagentsTelemetryManager, TraceEvent

    traces_dir = tmp_path / "traces"
    run_id = "safe-run-readable_1.2"
    event = TraceEvent(
        run_id=run_id,
        span_id="1",
        parent_span_id=None,
        name="readable",
        start_time=datetime.now(timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )
    LocalJSONLExporter(str(traces_dir))._write_trace_event(event)
    manager = SmolagentsTelemetryManager(str(traces_dir), enabled=False)

    assert manager.read_trace_events(run_id)[0].run_id == run_id
    assert manager.load_trace_file(run_id)["run_id"] == run_id
    assert manager._read_raw_trace_events(run_id)[0]["run_id"] == run_id


def test_smolagents_telemetry_rejects_trace_read_filename_escape(tmp_path, caplog):
    from telemetry.smolagents_telemetry import SmolagentsTelemetryManager

    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    (tmp_path / "escape.jsonl").write_text(
        json.dumps({
            "run_id": "../escape",
            "span_id": "1",
            "parent_span_id": None,
            "name": "escaped",
            "start_time": "2026-05-25T00:00:00+00:00",
            "end_time": None,
            "duration_ms": None,
            "status": "ok",
            "attributes": {},
            "events": [],
        })
        + "\n",
        encoding="utf-8",
    )
    original_external = (tmp_path / "escape.jsonl").read_text(encoding="utf-8")
    manager = SmolagentsTelemetryManager(str(traces_dir), enabled=False)

    for method_name in ("read_trace_events", "load_trace_file", "_read_raw_trace_events"):
        method = getattr(manager, method_name)
        with pytest.raises(ValueError, match="invalid trace run_id"):
            method("../escape")
    assert manager._mark_trace_as_error("../escape", "must not write") is False
    assert (tmp_path / "escape.jsonl").read_text(encoding="utf-8") == original_external

    secret_run_id = "postgresql://alice:topsecret@db/app?api_key=raw-key"
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="telemetry.smolagents_telemetry"):
        assert manager._mark_trace_as_error(secret_run_id, "must not write") is False
    for raw in (secret_run_id, "alice", "topsecret", "raw-key"):
        assert raw not in caplog.text


def test_smolagents_telemetry_mark_trace_logs_redacted_reason(tmp_path, caplog):
    from datetime import datetime, timezone
    from telemetry.smolagents_telemetry import LocalJSONLExporter, SmolagentsTelemetryManager, TraceEvent

    traces_dir = tmp_path / "traces"
    run_id = "safe-run-debug"
    raw_reason = "failed postgresql://alice:topsecret@db/app?api_key=raw-key"
    event = TraceEvent(
        run_id=run_id,
        span_id="1",
        parent_span_id=None,
        name="debug",
        start_time=datetime.now(timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )
    LocalJSONLExporter(str(traces_dir))._write_trace_event(event)
    manager = SmolagentsTelemetryManager(str(traces_dir), enabled=False)

    with caplog.at_level(logging.DEBUG, logger="telemetry.smolagents_telemetry"):
        assert manager._mark_trace_as_error(run_id, raw_reason) is True

    for raw in (raw_reason, "alice", "topsecret", "raw-key"):
        assert raw not in caplog.text
        assert raw not in (traces_dir / f"{run_id}.jsonl").read_text(encoding="utf-8")


def test_tool_manager_context_redacts_task_description_before_telemetry(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def set_attributes(self, attrs):
            pass

        def set_attribute(self, key, value):
            pass

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.start_kwargs = None

        def start_run_trace(self, **kwargs):
            self.start_kwargs = kwargs
            return _Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "postgresql://alice:topsecret@db.example.com:5432/app"
    manager = ToolManager()

    with manager.tool_context(
        "secure_db_executor",
        f"run against {raw_dsn}",
        session_id="run-context-task-redacted",
    ) as _ctx:
        active_text = str(manager.active_runs["run-context-task-redacted"])
        telemetry_text = str(telemetry_manager.start_kwargs)
        assert "topsecret" not in active_text
        assert "alice" not in active_text
        assert "topsecret" not in telemetry_text
        assert "alice" not in telemetry_text


def test_tool_manager_context_redacts_sensitive_metadata(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "postgresql://alice:topsecret@db.example.com:5432/app"
    manager = ToolManager()

    with manager.tool_context(
        "secure_db_executor",
        "run secure db executor",
        session_id="run-context-redacted",
        dsn=raw_dsn,
        refresh_token="secret-token",
    ) as ctx:
        ctx.add_metadata("dsn", raw_dsn)
        ctx.run_data["result"] = {"dsn": raw_dsn, "refresh_token": "secret-token"}
        ctx.run_data["accessToken"] = "raw-access-token"
        ctx.run_data["nested"] = {"clientSecret": "raw-client-secret"}

    active_run_text = str(manager.active_runs["run-context-redacted"])
    span_text = str(telemetry_manager.span.attrs)
    assert "topsecret" not in active_run_text
    assert "alice" not in active_run_text
    assert "secret-token" not in active_run_text
    assert "raw-access-token" not in active_run_text
    assert "raw-client-secret" not in active_run_text
    assert manager.active_runs["run-context-redacted"]["accessToken"] == "[REDACTED]"
    assert manager.active_runs["run-context-redacted"]["nested"]["clientSecret"] == "[REDACTED]"
    assert "topsecret" not in span_text
    assert "alice" not in span_text
    assert "secret-token" not in span_text


def test_tool_manager_context_redacts_run_data_on_exception(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def set_attributes(self, attrs):
            pass

        def set_attribute(self, key, value):
            pass

        def end(self):
            pass

    class _Telemetry:
        def start_run_trace(self, **kwargs):
            return _Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    manager = ToolManager()
    with pytest.raises(RuntimeError):
        with manager.tool_context(
            "secure_db_executor",
            "run secure db executor",
            session_id="run-context-error-redacted",
        ) as ctx:
            ctx.run_data["result"] = {"password": "raw-password"}
            ctx.run_data["clientSecret"] = "raw-client-secret"
            raise RuntimeError("failed api_key=raw-api-key")

    active_run = manager.active_runs["run-context-error-redacted"]
    serialized = str(active_run)
    assert "raw-password" not in serialized
    assert "raw-client-secret" not in serialized
    assert "raw-api-key" not in serialized
    assert active_run["result"]["password"] == "[REDACTED]"
    assert active_run["clientSecret"] == "[REDACTED]"
    assert "api_key=***" in active_run["error"]


def test_tool_manager_redacts_dsn_query_string_secrets(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "mssql+pyodbc://srv/db?password=top;secret&driver=ODBC+Driver+17"

    def _tool(**kwargs):
        assert kwargs["dsn"] == raw_dsn
        return {"dsn": raw_dsn}

    manager = ToolManager()
    manager.run_tool(
        tool_name="secure_db_executor",
        tool_function=_tool,
        task_description="run secure db executor",
        session_id="run-query-redacted",
        dsn=raw_dsn,
    )

    combined = str(manager.active_runs["run-query-redacted"]) + str(telemetry_manager.span.attrs)
    assert "top;secret" not in combined
    assert ";secret" not in combined
    assert "password=***" in combined
    assert "driver=ODBC+Driver+17" in combined


def test_tool_manager_redacts_encoded_odbc_connect(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    odbc = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;"
        "Database=orders;UID=alice;PWD=topsecret"
    )
    raw_dsn = f"mssql+pyodbc:///?odbc_connect={odbc}&driver=ODBC+Driver+17"

    def _tool(**kwargs):
        assert kwargs["dsn"] == raw_dsn
        return {"dsn": raw_dsn}

    manager = ToolManager()
    manager.run_tool(
        tool_name="secure_db_executor",
        tool_function=_tool,
        task_description=f"run against {raw_dsn}",
        session_id="run-odbc-connect-redacted",
        dsn=raw_dsn,
    )

    combined = str(manager.active_runs["run-odbc-connect-redacted"]) + str(telemetry_manager.span.attrs)
    assert "alice" not in combined
    assert "topsecret" not in combined
    assert "UID%3D" not in combined
    assert "PWD%3D" not in combined
    assert "odbc_connect=***" in combined
    assert "driver=ODBC+Driver+17" in combined


def test_tool_manager_redacts_semicolon_query_string_secrets(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "mssql+pyodbc://srv/db?password=top;secret;driver=ODBC+Driver+17"

    def _tool(**kwargs):
        assert kwargs["dsn"] == raw_dsn
        return {"dsn": raw_dsn}

    manager = ToolManager()
    manager.run_tool(
        tool_name="secure_db_executor",
        tool_function=_tool,
        task_description="run secure db executor",
        session_id="run-query-redacted-semicolon",
        dsn=raw_dsn,
    )

    combined = str(manager.active_runs["run-query-redacted-semicolon"]) + str(telemetry_manager.span.attrs)
    assert "top;secret" not in combined
    assert ";secret" not in combined
    assert "password=***" in combined
    assert ";driver=ODBC+Driver+17" in combined


def test_tool_manager_logs_redact_braced_odbc_password(monkeypatch, caplog):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def set_attributes(self, attrs):
            pass

        def set_attribute(self, key, value):
            pass

        def end(self):
            pass

    class _Telemetry:
        def start_run_trace(self, **kwargs):
            return _Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "Driver={ODBC Driver 17};Server=db;Pwd={top;secret};Password={other;secret};UID=alice"

    def _tool(**kwargs):
        raise RuntimeError(f"driver failed: {kwargs['dsn']}")

    manager = ToolManager()
    with caplog.at_level(logging.DEBUG, logger="tool_manager"):
        with pytest.raises(RuntimeError):
            manager.run_tool(
                tool_name="secure_db_executor",
                tool_function=_tool,
                task_description="run secure db executor",
                session_id="run-log-redacted",
                dsn=raw_dsn,
            )

    assert "top;secret" not in caplog.text
    assert "other;secret" not in caplog.text
    assert "Pwd=***" in caplog.text
    assert "Password=***" in caplog.text


def test_tool_manager_logs_redact_semicolon_key_value_password(monkeypatch, caplog):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def set_attributes(self, attrs):
            pass

        def set_attribute(self, key, value):
            pass

        def end(self):
            pass

    class _Telemetry:
        def start_run_trace(self, **kwargs):
            return _Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    raw_dsn = "host=db user=alice password=top;secret dbname=app"

    def _tool(**kwargs):
        raise RuntimeError(f"driver failed: {kwargs['dsn']}")

    manager = ToolManager()
    with caplog.at_level(logging.DEBUG, logger="tool_manager"):
        with pytest.raises(RuntimeError):
            manager.run_tool(
                tool_name="secure_db_executor",
                tool_function=_tool,
                task_description="run secure db executor",
                session_id="run-log-redacted-semicolon",
                dsn=raw_dsn,
            )

    assert "top;secret" not in caplog.text
    assert ";secret" not in caplog.text
    assert "password=***" in caplog.text


def test_tool_manager_runtime_redacts_encoded_query_keys_and_dict_keys():
    from tool_manager import _redact_runtime_value

    raw_encoded = "postgresql://host/db?api%5Fkey=rawsecret&sslmode=require"
    raw_key = "postgresql://alice:secret@example.com/db?api_key=raw"
    raw_empty_user = "postgresql://:emptypass@example.net/db"
    raw_assignment = (
        "failed api%5Fkey=plainraw access%5Ftoken=encodedaccesssecret "
        "client_secret=clientraw OPENAI_API_KEY=sk-openai "
        "OPENAI_API_KEY_DB=sk-openai-db api_key: colon-api "
        "accessToken=camel-access refreshToken: camel-refresh "
        "clientSecret=camel-client dbPassword: camel-password "
        "AWS_SECRET_ACCESS_KEY=aws-secret secret_key: secret-key privateKey=private-key "
        "password: colon-pass token: colon-token "
        "max_tokens: 32768 token_count: 12 Authorization: Bearer headerraw"
    )
    raw_json = (
        '{"password": "jsonpass", "token": "jsontoken", "secret": "jsonsecret", '
        '"authorization": "Bearer jsonauth", "openai_api_key": "sk-json", '
        '"accessToken": "json-access", "privateKey": "json-private", '
        '"max_tokens": 32768}'
    )

    redacted = _redact_runtime_value({
        raw_key: "value",
        "result": raw_encoded,
        "message": raw_assignment,
        "json": raw_json,
        "empty_user": raw_empty_user,
        "api%5Fkey": "dictencodedsecret",
        "api_key": "https://tokens.example/sk-live-secret",
        "refresh_token": "dictrefreshsecret",
        "accessToken": "dict-access",
        "clientSecret": "dict-client",
        "dbPassword": "dict-password",
        "AWS_SECRET_ACCESS_KEY": "dict-aws",
        "secret_key": "dict-secret-key",
        "privateKey": "dict-private",
        "max_tokens": 32768,
        "token_count": 12,
    })
    serialized = str(redacted)

    assert "rawsecret" not in serialized
    assert "alice:secret" not in serialized
    assert "api_key=raw" not in serialized
    assert "emptypass" not in serialized
    assert "plainraw" not in serialized
    assert "encodedaccesssecret" not in serialized
    assert "clientraw" not in serialized
    assert "sk-openai" not in serialized
    assert "sk-openai-db" not in serialized
    assert "camel-access" not in serialized
    assert "camel-refresh" not in serialized
    assert "camel-client" not in serialized
    assert "camel-password" not in serialized
    assert "aws-secret" not in serialized
    assert "secret-key" not in serialized
    assert "private-key" not in serialized
    assert "colon-api" not in serialized
    assert "colon-pass" not in serialized
    assert "colon-token" not in serialized
    assert "headerraw" not in serialized
    assert "jsonpass" not in serialized
    assert "jsontoken" not in serialized
    assert "jsonsecret" not in serialized
    assert "jsonauth" not in serialized
    assert "sk-json" not in serialized
    assert "json-access" not in serialized
    assert "json-private" not in serialized
    assert "dictencodedsecret" not in serialized
    assert "sk-live-secret" not in serialized
    assert "tokens.example" not in serialized
    assert "dictrefreshsecret" not in serialized
    assert "dict-access" not in serialized
    assert "dict-client" not in serialized
    assert "dict-password" not in serialized
    assert "dict-aws" not in serialized
    assert "dict-secret-key" not in serialized
    assert "dict-private" not in serialized
    assert "api%5Fkey=***" in serialized
    assert "access%5Ftoken=***" in serialized
    assert "client_secret=***" in serialized
    assert "accessToken=***" in serialized
    assert "refreshToken: ***" in serialized
    assert "clientSecret=***" in serialized
    assert "dbPassword: ***" in serialized
    assert "AWS_SECRET_ACCESS_KEY=***" in serialized
    assert "secret_key: ***" in serialized
    assert "privateKey=***" in serialized
    assert "OPENAI_API_KEY=***" in serialized
    assert "OPENAI_API_KEY_DB=***" in serialized
    assert "api_key: ***" in serialized
    assert "password: ***" in serialized
    assert "token: ***" in serialized
    assert "Authorization: ***" in serialized
    assert "'api%5Fkey': '[REDACTED]'" in serialized
    assert "'api_key': '[REDACTED]'" in serialized
    assert "'refresh_token': '[REDACTED]'" in serialized
    assert "'accessToken': '[REDACTED]'" in serialized
    assert "'clientSecret': '[REDACTED]'" in serialized
    assert "'dbPassword': '[REDACTED]'" in serialized
    assert "'AWS_SECRET_ACCESS_KEY': '[REDACTED]'" in serialized
    assert "'secret_key': '[REDACTED]'" in serialized
    assert "'privateKey': '[REDACTED]'" in serialized
    assert "***:***@example.com" in serialized
    assert "***:***@example.net" in serialized
    assert "sslmode=require" in serialized
    assert '"password": "***"' in serialized
    assert '"token": "***"' in serialized
    assert '"secret": "***"' in serialized
    assert '"authorization": "***"' in serialized
    assert '"openai_api_key": "***"' in serialized
    assert '"accessToken": "***"' in serialized
    assert '"privateKey": "***"' in serialized
    assert '"max_tokens": 32768' in serialized
    assert "'max_tokens': 32768" in serialized
    assert "'token_count': 12" in serialized
    assert "max_tokens: 32768" in serialized
    assert "token_count: 12" in serialized


def test_tool_manager_run_tool_handles_cyclic_result(monkeypatch):
    import telemetry
    from tool_manager import ToolManager

    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attributes(self, attrs):
            self.attrs.update(attrs)

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def end(self):
            pass

    class _Telemetry:
        def __init__(self):
            self.span = _Span()

        def start_run_trace(self, **kwargs):
            return self.span

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_manager = _Telemetry()
    monkeypatch.setattr(telemetry, "get_telemetry_manager", lambda: telemetry_manager)

    result: dict[str, object] = {"password": "raw-secret"}
    result["self"] = result
    child: list[object] = ["token=raw-token"]
    child.append(child)
    result["child"] = child

    def _tool(**_kwargs):
        return result

    manager = ToolManager()
    returned = manager.run_tool(
        tool_name="cyclic_tool",
        tool_function=_tool,
        task_description="return cyclic object",
        session_id="run-cyclic-result",
    )

    assert returned is result
    active_run = manager.active_runs["run-cyclic-result"]
    assert active_run["status"] == "completed"
    active_run_text = str(active_run)
    assert "raw-secret" not in active_run_text
    assert "raw-token" not in active_run_text
    assert "***" in active_run_text
    assert "[Circular]" in active_run_text
    output_value = telemetry_manager.span.attrs["output.value"]
    json.loads(output_value)
    assert "raw-secret" not in output_value
    assert "raw-token" not in output_value
    assert "[Circular]" in output_value


# ---------------------------------------------------------------------------
# is_dry_run_only (EPIC 7.24)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_is_dry_run_only_true_env_values(monkeypatch, value):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", value)
    assert is_dry_run_only() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_dry_run_only_false_env_values(monkeypatch, value):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", value)
    assert is_dry_run_only() is False


def test_is_dry_run_only_unset_env(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_DRY_RUN_ONLY", raising=False)
    assert is_dry_run_only() is False


def test_is_dry_run_only_invalid_env_fails_fast(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "maybe")
    with pytest.raises(ValueError, match="TEXT_TO_SQL_DRY_RUN_ONLY"):
        is_dry_run_only()


def test_is_dry_run_only_payload_flag_overrides_env_false(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "0")
    assert is_dry_run_only(payload_flag=True) is True
    assert is_dry_run_only(payload_flag=False) is False


def test_is_dry_run_only_env_true_with_payload_false(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    # ENV-1 не отключается payload_flag=False — безопасный OR.
    assert is_dry_run_only(payload_flag=False) is True


# ---------------------------------------------------------------------------
# coerce_strict_bool (общая утилита для EPIC 7.22 и 7.23)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("1", True),
    ("0", False),
    ("true", True),
    ("False", False),
    ("YES", True),
    ("no", False),
    ("on", True),
    ("off", False),
    ("", False),
    ("  ", False),
])
def test_coerce_strict_bool_accepts_canonical(value, expected):
    assert coerce_strict_bool(value, field_name="flag") is expected


def test_coerce_strict_bool_none_uses_default():
    assert coerce_strict_bool(None, default=True, field_name="x") is True
    assert coerce_strict_bool(None, default=False, field_name="x") is False


@pytest.mark.parametrize("bad", ["maybe", "2", "yesnt", 2, -1, 1.5, [], {}, object()])
def test_coerce_strict_bool_rejects_invalid(bad):
    with pytest.raises(ValueError, match="my_field"):
        coerce_strict_bool(bad, field_name="my_field")


# ---------------------------------------------------------------------------
# sql_explain integration: error message не содержит DSN/пароль (EPIC 7.24)
# ---------------------------------------------------------------------------
def test_sql_explain_error_masks_dsn(monkeypatch):
    """При ошибке подключения plugin str(exc) может содержать DSN."""
    from custom_tools.text_to_sql import core as core_facade
    from custom_tools.text_to_sql.core import _sql_generation_api

    leaked_dsn = "postgresql://alice:topsecret@db.example.com:5432/app"
    monkeypatch.setenv("DB_DSN", leaked_dsn)
    monkeypatch.delenv("TEXT_TO_SQL_DRY_RUN_ONLY", raising=False)

    class _FakePlugin:
        def connect(self, dsn):
            raise RuntimeError(f"could not connect to {dsn}")

        def close(self, conn):  # pragma: no cover - never reached
            pass

        def explain(self, conn, q):  # pragma: no cover - never reached
            return {}

    monkeypatch.setattr(core_facade, "get_plugin", lambda dsn: _FakePlugin())

    class _PassthroughValidator:
        forbidden_keywords: list[str] = []

        def _mask_string_literals(self, s):
            return s

        def validate(self, q):
            return {"is_safe": True, "issues": []}

    # Заставляем sql_safety_check вернуть safe (минуя LLM-audit) через monkeypatch
    monkeypatch.setattr(
        _sql_generation_api,
        "sql_safety_check",
        lambda q, *, sql_validator, dsn=None: {"is_safe": True, "issues": []},
    )

    result = _sql_generation_api.sql_explain(
        "SELECT 1",
        dsn=leaked_dsn,
        sql_validator=_PassthroughValidator(),
    )
    issues = result.get("issues") or []
    descriptions = " ".join(i.get("description", "") for i in issues)
    assert "topsecret" not in descriptions
    assert "alice" not in descriptions
    assert "***" in descriptions
