"""Тесты для EPIC 7 блока audit (задачи 7.9–7.14).

Покрывают:
- 7.9: RotatingFileHandler — ротация по размеру + ограничение по backupCount.
- 7.10: secrets.token_hex(16) вместо md5 — log_id уникален при одинаковых session_id+time.
- 7.11: транзакция SQLite + compensation Chroma + agent_name параметризован.
- 7.12: json.dumps(..., default=str) — устойчивость к datetime/Decimal.
- 7.13: согласованная семантика при битом JSON в execution_result.
- 7.14: get_facade_repo_root() / get_repo_root() — единая точка вычисления.
"""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import (
    audit_logger,
    purge_schema_linking_rag_cache,
    save_successful_sql,
)
from custom_tools.text_to_sql.core._audit import (
    _audit_handlers,
    _audit_handlers_lock,
    _stale_handlers,
)
from custom_tools.text_to_sql.utils import (
    get_facade_repo_root,
    get_repo_root,
)


def _reset_audit_handlers():
    """Очищает кэш handler'ов между тестами (изолирует от других тестов).

    W7-T3: дополнительно сбрасываем `_stale_handlers`, чтобы FD не утекали
    между тестами при пересоздании handler'а с новыми параметрами.
    """
    with _audit_handlers_lock:
        for h in list(_audit_handlers.values()):
            try:
                h.close()
            except Exception:
                pass
        _audit_handlers.clear()
        for h in _stale_handlers:
            try:
                h.close()
            except Exception:
                pass
        _stale_handlers.clear()


@pytest.fixture(autouse=True)
def _isolate_audit_handlers():
    _reset_audit_handlers()
    yield
    _reset_audit_handlers()


# === 7.14: get_facade_repo_root / get_repo_root ===========================


def test_get_repo_root_returns_repo_root_path():
    root = get_repo_root()
    assert isinstance(root, Path)
    # utils.py живёт в custom_tools/text_to_sql/utils.py → parents[2] = repo.
    assert (root / "custom_tools" / "text_to_sql" / "utils.py").exists()


def test_get_facade_repo_root_handles_file_facade(monkeypatch, tmp_path):
    """Тест контракта: для core.py (файл) — parents[2]."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    root = get_facade_repo_root()
    assert root == tmp_path / "repo"


def test_get_facade_repo_root_handles_package_facade(monkeypatch, tmp_path):
    """Тест контракта: для core/__init__.py (package) — parents[3]."""
    fake_init = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core" / "__init__.py"
    fake_init.parent.mkdir(parents=True)
    fake_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_init))

    root = get_facade_repo_root()
    assert root == tmp_path / "repo"


# === 7.10: secrets.token_hex(16) ==========================================


def test_audit_logger_log_id_is_unique_for_same_session(monkeypatch, tmp_path):
    """Два последовательных вызова с одинаковым session_id дают разные log_id."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    r1 = audit_logger({"session_id": "s1", "action": "select"})
    r2 = audit_logger({"session_id": "s1", "action": "select"})

    assert r1["status"] == "logged"
    assert r2["status"] == "logged"
    assert r1["log_id"] != r2["log_id"]
    # token_hex(16) → 32 hex-символа.
    assert len(r1["log_id"]) == 32
    assert all(c in "0123456789abcdef" for c in r1["log_id"])


# === 7.12: json.dumps(..., default=str) ===================================


def test_audit_logger_accepts_datetime_and_decimal(monkeypatch, tmp_path):
    """datetime/Decimal/Path не валят сериализацию — default=str."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    entry = {
        "session_id": "s1",
        "timestamp": datetime(2026, 5, 22, 12, 0, 0),
        "amount": Decimal("3.14"),
        "path": Path("/tmp/foo"),
    }
    result = audit_logger(entry)
    assert result["status"] == "logged"

    audit_log = tmp_path / "repo" / "logs" / "audit.log"
    assert audit_log.exists()
    lines = [ln for ln in audit_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "audit.log must contain at least one line"
    last = json.loads(lines[-1])
    assert last["session_id"] == "s1"
    # Все нестандартные типы сериализованы как строки.
    assert isinstance(last["timestamp"], str)
    assert isinstance(last["amount"], str)
    assert isinstance(last["path"], str)


# === 7.9: RotatingFileHandler =============================================


def test_audit_logger_rotates_when_size_exceeds_max_bytes(monkeypatch, tmp_path):
    """При превышении AUDIT_LOG_MAX_BYTES создаётся audit.log.1."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    # Очень маленький лимит — каждая запись > лимита → ротация на каждом вызове.
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "100")
    monkeypatch.setenv("AUDIT_LOG_BACKUPS", "3")

    log_dir = tmp_path / "repo" / "logs"
    # Делаем несколько записей с длинным payload, чтобы спровоцировать ротацию.
    big_payload = "x" * 200
    for _ in range(2):
        audit_logger({"session_id": "s1", "payload": big_payload})

    # audit.log существует и audit.log.1 тоже (был ротирован).
    assert (log_dir / "audit.log").exists()
    assert (log_dir / "audit.log.1").exists()


def test_audit_logger_respects_backup_count_limit(monkeypatch, tmp_path):
    """При backups=2 файл audit.log.3 не создаётся (RotatingFileHandler ограничивает)."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "100")
    monkeypatch.setenv("AUDIT_LOG_BACKUPS", "2")

    log_dir = tmp_path / "repo" / "logs"
    big_payload = "x" * 200
    # Достаточно много записей, чтобы спровоцировать > 2 ротаций.
    for _ in range(6):
        audit_logger({"session_id": "s1", "payload": big_payload})

    assert (log_dir / "audit.log").exists()
    assert (log_dir / "audit.log.1").exists()
    assert (log_dir / "audit.log.2").exists()
    # backups=2 → .3 не должен существовать.
    assert not (log_dir / "audit.log.3").exists()


def test_audit_logger_env_validation_fail_fast(monkeypatch, tmp_path):
    """Невалидный AUDIT_LOG_MAX_BYTES → fail-fast RuntimeError, без маскировки.

    Раньше RuntimeError ловился внешним except и превращался в status="error" —
    это было silent degradation (AGENTS.md). Теперь env-валидация вынесена ВЫШЕ
    try-block: конфигурационная ошибка пробрасывается наружу без подмены статуса.
    """
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "not_an_int")
    with pytest.raises(RuntimeError, match="AUDIT_LOG_MAX_BYTES must be an integer"):
        audit_logger({"session_id": "s1"})


# === 7.13: save_successful_sql — bad JSON ================================


def test_save_successful_sql_bad_json_writes_unknown_not_true(monkeypatch, tmp_path):
    """При невалидном execution_result пишется 'unknown', а не 'True'."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    result = save_successful_sql(
        sql_query="SELECT 1",
        user_query="test",
        execution_result="not valid json{",
        dsn="sqlite:///tmp/test.db",
    )
    assert result["status"] == "saved"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Успешно: unknown" in content
    assert "Успешно: True" not in content
    assert "Предупреждение:" in content
    assert "не удалось распарсить как JSON" in content


def test_save_successful_sql_good_json_preserves_fields(monkeypatch, tmp_path):
    """При валидном execution_result заполняются настоящие поля."""
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test2.db")

    result = save_successful_sql(
        sql_query="SELECT 2",
        user_query="ok",
        execution_result=json.dumps({"rows_affected": 5, "success": True, "execution_time_ms": 42}),
        dsn="sqlite:///tmp/test2.db",
    )
    assert result["status"] == "saved"
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "Успешно: True" in content
    assert "Строк получено: 5" in content
    assert "Время выполнения: 42ms" in content
    assert "Предупреждение:" not in content


# === 7.11: transaction + compensation + agent_name =======================


def _build_test_conn():
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
    return conn


class _NoCloseConn:
    """Wrapper, который игнорирует close() — нужен, чтобы тест мог проверить
    состояние БД после выхода из purge (purge закрывает соединение в finally)."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        # no-op: оставляем underlying conn открытым для тестового assert.
        pass


def test_purge_supports_custom_agent_name(monkeypatch):
    """agent_name параметризован — фильтрует именно по нему."""
    raw_conn = _build_test_conn()
    raw_conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Custom-Agent",
            10,
            json.dumps({"cache_source": "schema_linking", "cache_kind": "schema_linking"}),
        ),
    )
    # Запись от другого агента не должна быть деактивирована.
    raw_conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Schema-RAG-Agent",
            11,
            json.dumps({"cache_source": "schema_linking", "cache_kind": "schema_linking"}),
        ),
    )
    raw_conn.commit()
    conn = _NoCloseConn(raw_conn)

    class TacticalCollection:
        def __init__(self):
            self.deleted_ids = None

        def delete(self, ids):
            self.deleted_ids = list(ids)

    tactical = TacticalCollection()

    class DBHandler:
        tactical_collection = tactical

        def get_connection(self):
            return conn

    monkeypatch.setattr(core_module, "memory_manager", SimpleNamespace(db_handler=DBHandler()))

    count = purge_schema_linking_rag_cache(session_id="s1", agent_name="Custom-Agent")

    assert count == 1
    assert tactical.deleted_ids == ["s1-Custom-Agent-10"]

    # Запись Schema-RAG-Agent осталась активной.
    cur = raw_conn.cursor()
    cur.execute("SELECT valid_to FROM agent_memory WHERE agent_name = ? AND step = ?", ("Schema-RAG-Agent", 11))
    row = cur.fetchone()
    assert row[0] is None


def test_purge_strict_chroma_failure_compensates_sqlite(monkeypatch):
    """strict=1 + Chroma raise → SQLite-запись остаётся активной (compensation)."""
    raw_conn = _build_test_conn()
    raw_conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Schema-RAG-Agent",
            20,
            json.dumps({"cache_source": "schema_linking", "cache_kind": "schema_linking"}),
        ),
    )
    raw_conn.commit()
    conn = _NoCloseConn(raw_conn)

    class FailingTactical:
        def delete(self, ids):
            raise RuntimeError("chroma broken")

    class DBHandler:
        tactical_collection = FailingTactical()

        def get_connection(self):
            return conn

    monkeypatch.setattr(core_module, "memory_manager", SimpleNamespace(db_handler=DBHandler()))
    monkeypatch.setenv("TEXT_TO_SQL_STRICT_CHROMA_CLEANUP", "1")

    with pytest.raises(RuntimeError, match="chroma broken"):
        purge_schema_linking_rag_cache(session_id="s1")

    # Compensation сработал: valid_to снова NULL.
    cur = raw_conn.cursor()
    cur.execute("SELECT valid_to FROM agent_memory WHERE step = ?", (20,))
    row = cur.fetchone()
    assert row[0] is None, "compensation должна была откатить deactivation"


def test_purge_nonstrict_chroma_failure_keeps_sqlite_committed(monkeypatch):
    """strict=0 (default) + Chroma raise → SQLite остаётся коммитнутым, ошибка не пробрасывается."""
    raw_conn = _build_test_conn()
    raw_conn.execute(
        "INSERT INTO agent_memory VALUES (?, ?, ?, ?, NULL, NULL)",
        (
            "s1",
            "Schema-RAG-Agent",
            30,
            json.dumps({"cache_source": "schema_linking", "cache_kind": "schema_linking"}),
        ),
    )
    raw_conn.commit()
    conn = _NoCloseConn(raw_conn)

    class FailingTactical:
        def delete(self, ids):
            raise RuntimeError("chroma broken")

    class DBHandler:
        tactical_collection = FailingTactical()

        def get_connection(self):
            return conn

    monkeypatch.setattr(core_module, "memory_manager", SimpleNamespace(db_handler=DBHandler()))
    monkeypatch.delenv("TEXT_TO_SQL_STRICT_CHROMA_CLEANUP", raising=False)

    count = purge_schema_linking_rag_cache(session_id="s1")
    assert count == 1

    # SQLite-запись деактивирована, не откачена.
    cur = raw_conn.cursor()
    cur.execute("SELECT valid_to FROM agent_memory WHERE step = ?", (30,))
    row = cur.fetchone()
    assert row[0] is not None, "SQLite deactivation must persist in non-strict mode"


# === T2-pii-audit: дополнительные тесты =====================================


def test_purge_warns_when_session_id_none_and_env_dsn_set(monkeypatch, caplog):
    """purge_schema_linking_rag_cache логирует warning при использовании DB_DSN из env."""
    import logging
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test_purge_warn.db")

    class DBHandler:
        tactical_collection = None

        def get_connection(self):
            raise RuntimeError("no real db in test")

    monkeypatch.setattr(core_module, "memory_manager", None)

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.core._audit"):
        purge_schema_linking_rag_cache(session_id=None)

    assert any(
        "DB_DSN из env" in record.message
        for record in caplog.records
    ), "должен быть warning о использовании DB_DSN из env"


def test_purge_no_warn_when_session_id_explicit(monkeypatch, caplog):
    """Явный session_id — warning НЕ логируется."""
    import logging
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test_purge_nowarn.db")
    monkeypatch.setattr(core_module, "memory_manager", None)

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.core._audit"):
        purge_schema_linking_rag_cache(session_id="explicit_session")

    dsn_warns = [
        r for r in caplog.records
        if "DB_DSN из env" in r.message
    ]
    assert not dsn_warns, "при явном session_id warning не должен появляться"


def test_save_successful_sql_logs_exception_type(monkeypatch, tmp_path, caplog):
    """save_successful_sql логирует debug с типом исключения при ошибке."""
    import logging
    from custom_tools.text_to_sql.core import _audit
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    # Провоцируем ошибку внутри try-блока через monkeypatch dsn_to_sanitized_name
    monkeypatch.setattr(
        _audit, "dsn_to_sanitized_name",
        lambda dsn: (_ for _ in ()).throw(OSError("disk full")),
    )

    with caplog.at_level(logging.DEBUG, logger="custom_tools.text_to_sql.core._audit"):
        result = save_successful_sql(
            sql_query="SELECT 1",
            dsn="sqlite:///tmp/test_exc_type.db",
        )

    assert result["status"] == "error"
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "OSError" in debug_msgs


def test_save_successful_sql_card_number_in_user_query_masked(monkeypatch, tmp_path):
    """card-числа в user_query маскируются перед записью в sqlrag (via _sanitize_audit_text, #23+#5).

    save_successful_sql не пишет data-строки в файл — только метаданные
    (rows_affected/success/execution_time_ms). Поэтому проверяем маскировку
    через user_query, который включается в файл.
    """
    fake_core = tmp_path / "repo" / "custom_tools" / "text_to_sql" / "core.py"
    fake_core.parent.mkdir(parents=True)
    fake_core.write_text("", encoding="utf-8")
    monkeypatch.setattr(core_module, "__file__", str(fake_core))

    result = save_successful_sql(
        sql_query="SELECT card_no FROM users",
        user_query="покажи данные карты 4111 1111 1111 1111",
        dsn="sqlite:///tmp/test_card_user_query.db",
    )
    assert result["status"] == "saved"
    content = Path(result["path"]).read_text(encoding="utf-8")
    # card_number в user_query должен быть замаскирован через _sanitize_audit_text
    assert "4111 1111 1111 1111" not in content
    assert "[CARD]" in content


def test_sanitize_audit_obj_masks_card_number_in_result_data():
    """_sanitize_audit_obj применяет card_number-правило к строкам внутри result_data (#23+#5)."""
    from custom_tools.text_to_sql.core._audit import _sanitize_audit_obj
    result_data = {
        "rows_affected": 1,
        "success": True,
        "data": [["4111 1111 1111 1111", "test@example.com"]],
    }
    sanitized = _sanitize_audit_obj(result_data)
    assert sanitized["data"][0][0] == "[CARD]"
    assert sanitized["data"][0][1] == "[EMAIL]"
    # Числовые значения не тронуты
    assert sanitized["rows_affected"] == 1
    assert sanitized["success"] is True
