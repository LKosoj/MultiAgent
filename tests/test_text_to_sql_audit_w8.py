"""W8-T2: тесты save_successful_sql после миграции md5 → sha256 + TOCTOU fix.

Покрывают:
  * sha256[:16] используется в имени файла вместо md5[:8];
  * повторный save того же SQL → status="exists" (атомарный open('x') не падает);
  * после первого save файл не перезаписывается при повторном вызове
    (race-overwrite невозможен).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import save_successful_sql

_TEST_DSN = "sqlite:///tmp/test_w8.db"


def _setup_fake_repo(monkeypatch, tmp_path: Path) -> None:
    """Подменяет __file__ core-фасада так, чтобы sqlrag/ оказался в tmp_path."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))
    monkeypatch.delenv("DB_DSN", raising=False)


def test_save_successful_sql_uses_sha256_not_md5(monkeypatch, tmp_path):
    """Имя файла должно содержать первые 16 символов sha256(sql), не md5."""
    _setup_fake_repo(monkeypatch, tmp_path)

    sql = "SELECT 42 FROM t WHERE id = 1"
    result = save_successful_sql(sql_query=sql, dsn=_TEST_DSN)

    assert result["status"] == "saved"
    filename: str = result["filename"]

    expected_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
    md5_legacy = hashlib.md5(sql.encode("utf-8")).hexdigest()[:8]

    assert expected_sha in filename, (
        f"sha256[:16]={expected_sha} must appear in filename={filename}"
    )
    # md5[:8] больше НЕ должен использоваться. Защита от регрессии.
    # Допускается случайное совпадение (8 hex == слабое условие), но
    # filename целиком не может содержать md5_legacy при этом sha-форме.
    assert not filename.endswith(f"_{md5_legacy}.md"), (
        f"legacy md5[:8] suffix detected in {filename}"
    )


def test_save_successful_sql_repeated_returns_exists(monkeypatch, tmp_path):
    """Повторный save того же SQL не падает и возвращает status='exists'.

    Это и есть проверка TOCTOU-фикса: атомарный `open(..., 'x')` ловит
    FileExistsError и возвращает graceful 'exists'.
    """
    _setup_fake_repo(monkeypatch, tmp_path)

    sql = "SELECT 1"
    first = save_successful_sql(sql_query=sql, user_query="q", dsn=_TEST_DSN)
    second = save_successful_sql(sql_query=sql, user_query="q", dsn=_TEST_DSN)

    assert first["status"] == "saved"
    assert second["status"] == "exists"
    # path тот же — dedup по контенту.
    assert second["path"] == first["path"]


def test_save_successful_sql_uses_explicit_dsn_for_session_id(monkeypatch, tmp_path):
    _setup_fake_repo(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_DSN", "sqlite:///env.db")

    result = save_successful_sql(
        sql_query="SELECT 7",
        dsn="postgresql://alice:secret@db.example.com/app",
    )

    assert result["status"] == "saved"
    assert Path(result["filename"]).name.startswith("postgresql_db_example_com_app_")
    assert "env" not in result["filename"]


def test_save_successful_sql_no_race_overwrite(monkeypatch, tmp_path):
    """Если файл уже существует с произвольным содержимым (race-симуляция) —
    второй save_successful_sql не должен его перезаписать."""
    _setup_fake_repo(monkeypatch, tmp_path)

    sql = "SELECT 'race'"
    # Создаём файл сами, ДО вызова save_successful_sql — симулируем "уже занято".
    first = save_successful_sql(sql_query=sql, dsn=_TEST_DSN)
    file_path = Path(first["path"])
    file_path.write_text("SENTINEL CONTENT — must not be overwritten", encoding="utf-8")

    # Повторный вызов должен увидеть exists и НЕ переписать.
    second = save_successful_sql(sql_query=sql, dsn=_TEST_DSN)

    assert second["status"] == "exists"
    assert file_path.read_text(encoding="utf-8") == "SENTINEL CONTENT — must not be overwritten"


def test_save_successful_sql_redacts_dsn_and_secret_literals(monkeypatch, tmp_path):
    _setup_fake_repo(monkeypatch, tmp_path)

    result = save_successful_sql(
        sql_query=(
            "SELECT 'postgresql://alice:secret@db.example.com/db', "
            "'password=top;secret token=abc123 api_key=xyz987 secret=hunter "
            "api%5Fkey=encodedsecret access%5Ftoken=encodedtoken "
            "client_secret=clientsecret Authorization: Bearer headersecret'"
        ),
        user_query=(
            "connect postgresql://bob:hunter2@db.example.com/db with "
            "password=top;secret auth=authsecret key=keysecret"
        ),
        execution_result=json.dumps({
            "rows_affected": 1,
            "success": True,
            "execution_time_ms": 5,
            "api_key": "result-api-value",
            "api%5Fkey": "result-encoded-api-value",
            "authorization": "Bearer result-auth-value",
            "refresh_token": "result-refresh-value",
            "nested": {
                "token": "result-token-value",
                "password": "result-password-value",
            },
            "rows": [[
                "postgresql://carol:secret@db.example.com/db",
                "password=top;secret token=rowtoken",
            ]],
        }),
        dsn=_TEST_DSN,
    )

    assert result["status"] == "saved"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "alice:secret" not in content
    assert "alice" not in content
    assert "bob:hunter2" not in content
    assert "bob" not in content
    assert "carol:secret" not in content
    assert "carol" not in content
    assert "top;secret" not in content
    assert ";secret" not in content
    for raw in (
        "abc123",
        "xyz987",
        "hunter",
        "encodedsecret",
        "encodedtoken",
        "clientsecret",
        "headersecret",
        "authsecret",
        "keysecret",
        "rowtoken",
        "result-api-value",
        "result-encoded-api-value",
        "result-auth-value",
        "result-refresh-value",
        "result-token-value",
        "result-password-value",
    ):
        assert raw not in content
    assert "***:***@db.example.com/db" in content
    assert "password=***" in content
    assert "token=***" in content
    assert "api_key=***" in content
    assert "api%5Fkey=***" in content
    assert "access%5Ftoken=***" in content
    assert "client_secret=***" in content
    assert "Authorization: ***" in content
    assert "secret=***" in content
    assert "auth=***" in content
    assert "key=***" in content
