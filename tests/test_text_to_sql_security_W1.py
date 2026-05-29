"""Pin-тесты для Группы A (Security) Волны 1.

Покрывают:
  * A1 — backward-compatible загрузка default safety profile.
  * A2 — расширение pii_mask_sync (passport / snils / inn).
  * A3 — сужение phone-regex (исключение OKTMO/OKATO/ИНН).
  * A4 — атомарный chmod 0o600 для audit.log.
  * A5 — mask_dsn для ODBC + URL-query форм.
  * A6 — PII-маскировка в финальных AG-UI payload'ах.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

# A1-тесты сами управляют env через monkeypatch, чтобы проверять
# documented default profile без глобального test-only override.


# ============================================================================
# A1 — safety.yaml default profile remains loadable
# ============================================================================


def _reload_safety_module():
    """Сбрасывает кэш модуля safety_config (для теста с изменённым env)."""
    from custom_tools.text_to_sql.validators import safety_config
    safety_config.reset_cache()
    return safety_config


def test_a1_default_profile_loads_without_test_env_override(monkeypatch):
    """default profile — документированный профиль и не требует opt-in env."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config = _reload_safety_module()

    profile = safety_config.load_safety_profile()

    assert profile.profile_name == "default"
    assert profile.forbidden_functions == []


def test_a1_extended_profile_loads(monkeypatch):
    """extended-профиль имеет непустой forbidden_functions."""
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    safety_config = _reload_safety_module()
    profile = safety_config.load_safety_profile()
    assert profile.profile_name == "extended"
    assert profile.forbidden_functions


# ============================================================================
# A2 — extended PII regex (passport / snils / inn)
# ============================================================================


@pytest.fixture(autouse=True)
def _disable_fullname_globally(monkeypatch):
    """ФИО-маскировка зависит от yaml; в тестах A2/A3 отключаем её принудительно,
    чтобы regex'ы не пересекались. Локальный тест A2 на ФИО снимает override.
    """
    from custom_tools.text_to_sql.core import _pii
    # W4: _ru_fullname_enabled принимает опциональный pre-loaded jur
    # (pii_mask_sync вызывает его как _ru_fullname_enabled(jur)).
    monkeypatch.setattr(_pii, "_ru_fullname_enabled", lambda *_a, **_k: False)


def test_a2_passport_masked():
    """Паспорт-regex требует пробел между серией и номером —
    это разводит его с голым 10-цифровым кодом (ОГРН/КПП без префикса)."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("серия 4514 123456") == "серия [PASSPORT]"
    # Голые 10 цифр БЕЗ пробела — НЕ паспорт (могут быть ИНН/КПП/ОГРН-fragment).
    assert pii_mask_sync("4514123456") == "4514123456"


def test_a2_snils_masked():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("СНИЛС 123-456-789 01") == "СНИЛС [SNILS]"
    assert pii_mask_sync("123-456-789-01") == "[SNILS]"


def test_a2_inn_masked_with_context():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    masked = pii_mask_sync("ИНН: 1234567890")
    assert "[INN]" in masked
    assert "1234567890" not in masked
    # 12-цифровой ИНН (физлицо).
    masked12 = pii_mask_sync("ИНН 123456789012")
    assert "[INN]" in masked12
    assert "123456789012" not in masked12


def test_a2_inn_without_context_not_masked():
    """Без префикса ИНН/INN голые 10/12 цифр не должны маскироваться:
    они неотличимы от других кодов (ОГРН/КПП/ОКАТО-fragments)."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    # 10 цифр без префикса — не ИНН.
    assert pii_mask_sync("1234567890") == "1234567890"


def test_a2_oktmo_not_masked():
    """ОКТМО (8 или 11 цифр) не должен маскироваться ни как ИНН, ни как СНИЛС.
    Это рабочие данные муниципального датасета."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("OKTMO=45000000") == "OKTMO=45000000"
    assert pii_mask_sync("OKTMO=45000000000") == "OKTMO=45000000000"


def test_a2_inn_english_prefix():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    masked = pii_mask_sync("INN: 7707083893")
    assert "[INN]" in masked
    assert "7707083893" not in masked


def test_a2_email_still_masked():
    """Регресс: email-категория не должна была сломаться при расширении."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("user@example.com") == "[EMAIL]"


def test_a2_fullname_masked_when_enabled(monkeypatch):
    from custom_tools.text_to_sql.core import _pii
    monkeypatch.setattr(_pii, "_ru_fullname_enabled", lambda *_a, **_k: True)
    masked = _pii.pii_mask_sync("Иванов Иван")
    assert "[FULLNAME]" in masked


def test_a2_fullname_enabled_by_yaml_env(monkeypatch):
    import importlib
    from custom_tools.text_to_sql.core import _pii

    pii_mod = importlib.reload(_pii)
    monkeypatch.setenv("PII_MASK_FULLNAME", "1")

    assert pii_mod._ru_fullname_enabled() is True
    assert pii_mod.pii_mask_sync("Иванов Иван") == "[FULLNAME]"


def test_a2_fullname_exclusions_loaded_from_yaml(monkeypatch):
    from custom_tools.text_to_sql.core import _pii
    monkeypatch.setattr(_pii, "_ru_fullname_enabled", lambda *_a, **_k: True)
    assert _pii.pii_mask_sync("Республика Башкортостан") == "Республика Башкортостан"


def test_a2_fullname_skipped_when_disabled():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("Иванов Иван") == "Иванов Иван"


def test_a2_fullname_opt_in_fails_fast_when_config_unavailable(monkeypatch):
    """PII_MASK_FULLNAME=1 не должен обходить yaml source of truth."""
    import importlib
    from custom_tools.text_to_sql.core import _pii
    from custom_tools.text_to_sql import pii_categories_config

    pii_mod = importlib.reload(_pii)
    monkeypatch.setenv("PII_MASK_FULLNAME", "1")
    monkeypatch.setattr(
        pii_categories_config,
        "load_pii_categories_config",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing pii yaml")),
    )

    with pytest.raises(FileNotFoundError, match="missing pii yaml"):
        pii_mod._ru_fullname_enabled()
    with pytest.raises(FileNotFoundError, match="missing pii yaml"):
        pii_mod.pii_mask_sync("Иванов Иван")


# ============================================================================
# A3 — phone regex sharpening
# ============================================================================


def test_a3_phone_plus7_matched():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("+7 (495) 123-45-67") == "[PHONE]"
    assert pii_mask_sync("+74951234567") == "[PHONE]"


def test_a3_phone_8_prefix_matched():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("8(495)123-45-67") == "[PHONE]"
    assert pii_mask_sync("8 495 123 45 67") == "[PHONE]"


def test_a3_oktmo_not_a_phone():
    """ОКТМО 45000000 — 8 цифр, длиннее 7-цифрового RU-телефона без префикса,
    но короче целевого 11-цифрового RU-номера. Не должен матчиться как телефон."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("OKTMO=45000000") == "OKTMO=45000000"


def test_a3_okato_not_a_phone():
    """ОКАТО — 11 цифр. Без префикса +7/8/7 и lookahead защищает от
    срабатывания телефонного regex."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("OKATO=12345678901") == "OKATO=12345678901"


@pytest.mark.parametrize(
    "code",
    [
        "80000000000",
        "83000000000",  # Ненецкий АО
        "86000000000",  # Ханты-Мансийский АО
        "89000000000",  # Ямало-Ненецкий АО
    ],
)
def test_a3_oktmo_8x_prefix_not_a_phone(code):
    """Голый 11-значный ОКТМО регионов 80–89 (начинается с 8) НЕ должен
    маскироваться как телефон: ветка `8...` требует разделитель/`(` после 8
    (lookahead `(?=[\\s\\-(])`), иначе голый код ловился бы как [PHONE]."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync(code) == code


def test_a3_random_long_digit_string_not_matched():
    """20-цифровая последовательность не должна стать [PHONE]."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("12345678901234567890") == "12345678901234567890"


def test_a3_phone_inside_run_id_not_matched():
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    run_id = "run-54f74806070340c2"
    assert pii_mask_sync(run_id) == run_id


# ============================================================================
# A4 — atomic audit.log chmod
# ============================================================================


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod only")
def test_a4_audit_log_created_with_0600(monkeypatch, tmp_path):
    """audit.log при первом вызове создаётся с правами 0o600.

    Регресс относительно прежней реализации (post-emit chmod): теперь
    chmod выполняется ДО RotatingFileHandler и не зависит от выполнения emit.
    """
    from custom_tools.text_to_sql import core as core_module
    from custom_tools.text_to_sql.core._audit import audit_logger

    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    result = audit_logger({"session_id": "s_a4", "action": "select"})
    assert result["status"] == "logged"

    audit_log = tmp_path / "repo" / "logs" / "audit.log"
    assert audit_log.exists()
    mode = audit_log.stat().st_mode & 0o777
    assert mode == 0o600, f"audit.log mode {oct(mode)} != 0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod only")
def test_a4_existing_loose_file_tightened(monkeypatch, tmp_path):
    """Если audit.log существует с правами 0o644 — реализация ужесточает
    до 0o600 при следующем вызове."""
    from custom_tools.text_to_sql import core as core_module
    from custom_tools.text_to_sql.core._audit import audit_logger

    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    log_dir = tmp_path / "repo" / "logs"
    log_dir.mkdir(parents=True)
    audit_log = log_dir / "audit.log"
    audit_log.write_text("", encoding="utf-8")
    os.chmod(audit_log, 0o644)
    assert audit_log.stat().st_mode & 0o777 == 0o644

    # Сбросим кэш handler'а, чтобы ensure-helper отработал на свежем пути.
    from custom_tools.text_to_sql.core import _audit
    with _audit._audit_handlers_lock:
        _audit._audit_handlers.pop(str(audit_log), None)

    audit_logger({"session_id": "s_a4b", "action": "select"})
    mode = audit_log.stat().st_mode & 0o777
    assert mode == 0o600, f"existing audit.log mode {oct(mode)} != 0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod only")
def test_a4_audit_log_after_rollover_stays_0600(monkeypatch, tmp_path):
    from custom_tools.text_to_sql import core as core_module
    from custom_tools.text_to_sql.core import _audit
    from custom_tools.text_to_sql.core._audit import audit_logger

    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "1")
    monkeypatch.setenv("AUDIT_LOG_BACKUPS", "1")

    audit_log = tmp_path / "repo" / "logs" / "audit.log"
    with _audit._audit_handlers_lock:
        _audit._audit_handlers.pop(str(audit_log), None)

    old_umask = os.umask(0o022)
    try:
        audit_logger({"session_id": "s_rollover_1", "action": "select", "payload": "x" * 100})
        audit_logger({"session_id": "s_rollover_2", "action": "select", "payload": "y" * 100})
    finally:
        os.umask(old_umask)

    assert audit_log.exists()
    assert audit_log.stat().st_mode & 0o777 == 0o600
    rotated = audit_log.with_name("audit.log.1")
    assert rotated.exists()
    assert rotated.stat().st_mode & 0o777 == 0o600


def test_a4_audit_log_redacts_dsn_and_secret_assignments(monkeypatch, tmp_path):
    from custom_tools.text_to_sql import core as core_module
    from custom_tools.text_to_sql.core._audit import audit_logger

    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    result = audit_logger({
        "session_id": "s_audit",
        "dsn": "postgresql://alice:secret@db.example.com/db",
        "postgresql://keyuser:keysecret@db.example.com/db": "key value",
        "message": (
            "failed with password=top;secret user=svc token=abc123 "
            "api_key=xyz987 secret=hunter auth=authsecret key=keysecret"
        ),
        "api_key": "direct-api-value",
        "nested": {
            "url": "mssql+pyodbc://srv/db?password=top;secret;driver=ODBC+Driver+17",
            "token": "nested-token-value",
            "password": "nested-password-value",
        },
    })

    assert result["status"] == "logged"
    text = (tmp_path / "repo" / "logs" / "audit.log").read_text(encoding="utf-8")
    assert "alice:secret" not in text
    assert "keyuser" not in text
    assert "keysecret" not in text
    for raw in (
        "abc123",
        "xyz987",
        "hunter",
        "authsecret",
        "direct-api-value",
        "nested-token-value",
        "nested-password-value",
    ):
        assert raw not in text
    assert "top;secret" not in text
    assert ";secret" not in text
    assert "***:***@db.example.com/db" in text
    assert "token=***" in text
    assert "api_key=***" in text
    assert "secret=***" in text
    assert "auth=***" in text
    assert "key=***" in text
    assert "password=***;driver=ODBC+Driver+17" in text


# ============================================================================
# A5 — mask_dsn for ODBC + URL-query
# ============================================================================


def test_a5_odbc_password_masked():
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "Driver={ODBC Driver 17};Server=db.example.com;UID=admin;Pwd=topsecret123"
    masked = mask_dsn_value(dsn)
    assert "topsecret123" not in masked
    assert "Pwd=***" in masked


def test_a5_odbc_password_capital_key():
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "Driver={SQL};Server=db;UID=admin;Password=hunter2"
    masked = mask_dsn_value(dsn)
    assert "hunter2" not in masked
    assert "Password=***" in masked


def test_a5_odbc_braced_password_with_semicolon():
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "Driver={ODBC Driver 17};Server=db;Pwd={top;secret};Password={other;secret};UID=admin"

    masked = mask_dsn_value(dsn)

    assert "top;secret" not in masked
    assert "other;secret" not in masked
    assert "Pwd=***" in masked
    assert "Password=***" in masked
    assert "UID=admin" not in masked
    assert "UID=***" in masked


def test_a5_sqlalchemy_query_password():
    """SQLAlchemy-форма с password в query: ?password=...&driver=..."""
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "mssql+pyodbc://srv/db?password=topsecret&driver=ODBC+Driver+17"
    masked = mask_dsn_value(dsn)
    assert "topsecret" not in masked
    assert "password=***" in masked
    assert "driver=ODBC+Driver+17" in masked, "driver query param должен сохраниться"


def test_a5_semicolon_query_password_preserves_following_param():
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "mssql+pyodbc://srv/db?password=top;secret;driver=ODBC+Driver+17"

    masked = mask_dsn_value(dsn)

    assert "top;secret" not in masked
    assert ";secret" not in masked
    assert "password=***" in masked
    assert ";driver=ODBC+Driver+17" in masked


def test_a5_url_query_token_masked():
    from custom_tools.text_to_sql.utils import mask_dsn_value
    dsn = "https://api.example.com/v1?token=abc123secret&format=json"
    masked = mask_dsn_value(dsn)
    assert "abc123secret" not in masked
    assert "token=***" in masked


def test_a5_url_encoded_query_secret_key_masked():
    from custom_tools.text_to_sql.utils import mask_dsn_value

    dsn = "postgresql://host/db?api%5Fkey=rawsecret&sslmode=require"
    masked = mask_dsn_value(dsn)

    assert "rawsecret" not in masked
    assert "api%5Fkey=***" in masked
    assert "sslmode=require" in masked


def test_a5_existing_uri_form_still_works():
    """Регресс: classic URI scheme://user:pwd@host продолжает работать."""
    from custom_tools.text_to_sql.utils import mask_dsn_value
    masked = mask_dsn_value("postgresql://admin:s3cret@db.example.com:5432/app")
    assert "s3cret" not in masked
    assert "admin" not in masked
    assert "***:***@" in masked


def test_a5_existing_libpq_form_still_works():
    """Регресс: libpq keyword form ``host=... password=secret`` продолжает работать."""
    from custom_tools.text_to_sql.utils import mask_dsn_value
    masked = mask_dsn_value("host=db.example.com user=admin password=s3cret dbname=app")
    assert "s3cret" not in masked
    assert "password=***" in masked


# ============================================================================
# A6 — AG-UI redact_pii_in_payload integration
# ============================================================================


def test_a6_redact_pii_helper_masks_email_in_rows():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {
        "ok": True,
        "data": {
            "rows": [
                ["user@example.com", 42],
                ["other@example.org", 7],
            ],
            "columns": ["email", "count"],
        },
    }
    redacted = redact_pii_in_payload(payload)
    rows = redacted["data"]["rows"]
    assert rows[0][0] == "[EMAIL]"
    assert rows[1][0] == "[EMAIL]"
    # Числовые значения остаются intact (mask_pii_in_obj не трогает int).
    assert rows[0][1] == 42


def test_a6_redact_pii_in_result_rows():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {
        "data": {"text": "Звоните +7 (495) 123-45-67 или пишите a@b.com"},
    }
    redacted = redact_pii_in_payload(payload)
    text = redacted["data"]["text"]
    assert "[PHONE]" in text
    assert "[EMAIL]" in text
    assert "+7" not in text
    assert "a@b.com" not in text


def test_a6_dsn_and_pii_combined():
    """Полный pipeline: _redact_payload маскирует DSN, redact_pii_in_payload — PII.
    Применённые последовательно — ни один секрет не утекает."""
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

    payload = {
        "dsn": "postgresql://admin:s3cret@db.example.com:5432/app",
        "rows": [["user@example.com", "+7 495 1234567"]],
    }
    stage1 = _redact_payload(payload)
    stage2 = redact_pii_in_payload(stage1)
    assert "s3cret" not in str(stage2)
    assert "[EMAIL]" in str(stage2)
    assert "[PHONE]" in str(stage2)


def test_a6_oktmo_value_preserved():
    """Числовой ОКТМО в payload не должен пострадать ни от DSN-redactor, ни от PII."""
    from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload
    payload = {
        "data": {
            "rows": [[45000000, "Москва"]],
            "columns": ["oktmo", "name"],
        }
    }
    redacted = redact_pii_in_payload(_redact_payload(payload))
    rows = redacted["data"]["rows"]
    assert rows[0][0] == 45000000
    assert rows[0][1] == "Москва"


def test_a6_oktmo_string_preserved():
    """ОКТМО как строка (часто хранится в БД как varchar) — также не маскируется."""
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {"rows": [["45000000"]]}
    redacted = redact_pii_in_payload(payload)
    assert redacted["rows"][0][0] == "45000000"


def test_a6_okato_like_11_digit_string_preserved():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {"rows": [["45000000000"], ["12345678901"]]}

    redacted = redact_pii_in_payload(payload)

    assert redacted["rows"][0][0] == "45000000000"
    assert redacted["rows"][1][0] == "12345678901"


def test_a6_random_long_digit_string_preserved():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {"rows": [["12345678901234567890"]]}

    redacted = redact_pii_in_payload(payload)

    assert redacted["rows"][0][0] == "12345678901234567890"


def test_a6_phone_like_value_masked():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {"rows": [["phone", "+7 (495) 123-45-67"]]}

    redacted = redact_pii_in_payload(payload)

    assert redacted["rows"][0][1] == "[PHONE]"


def test_a6_iso_timestamp_preserved():
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {"rows": [["2026-05-23T10:05:27+03:00"]]}

    redacted = redact_pii_in_payload(payload)

    assert redacted["rows"][0][0] == "2026-05-23T10:05:27+03:00"


def test_a6_assignment_with_braced_secret_redacted():
    from backend.fastapi_app.agui.redaction import _redact_payload
    payload = {"message": "ODBC failed: Pwd={top;secret};UID=admin"}

    redacted = _redact_payload(payload)

    assert "top;secret" not in redacted["message"]
    assert "Pwd=***" in redacted["message"]
    assert "UID=admin" not in redacted["message"]
    assert "UID=***" in redacted["message"]


def test_a6_redact_pii_recursive():
    """Walker идёт во вложенные dict/list и tuple."""
    from backend.fastapi_app.agui.redaction import redact_pii_in_payload
    payload = {
        "level1": {
            "level2": [
                {"email": "x@y.zz"},
                ("contact: test@example.com",),
            ]
        }
    }
    redacted = redact_pii_in_payload(payload)
    assert redacted["level1"]["level2"][0]["email"] == "[EMAIL]"
    assert "[EMAIL]" in redacted["level1"]["level2"][1][0]


def test_a6_text_to_sql_mask_pii_shared_container_reuses_sanitized_copy():
    from custom_tools.text_to_sql.core._pii import mask_pii_in_obj

    shared = {"email": "alice@example.com", "phone": "+7 (495) 123-45-67"}
    payload = {"first": shared, "second": shared}

    redacted = mask_pii_in_obj(payload)

    assert redacted["first"] is redacted["second"]
    assert redacted["first"]["email"] == "[EMAIL]"
    assert redacted["second"]["phone"] == "[PHONE]"
    assert "alice@example.com" not in str(redacted)
