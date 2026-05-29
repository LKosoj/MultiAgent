"""
Тесты для ``sqlite_chroma_transaction`` (W9-A15).

Покрывают:
  * happy path: на выходе SQLite commit;
  * rollback на исключение внутри блока;
  * strict-mode: compensation callbacks для Chroma вызываются;
  * non-strict: compensations регистрируются, но не вызываются (warning лог);
  * compensation сам бросает → logger.critical, оригинальная ошибка raise;
  * env TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK переключает режим;
  * explicit strict=... override env;
  * register_chroma_compensation требует callable.
"""

from unittest.mock import MagicMock

import pytest

from custom_tools.text_to_sql._sqlite_chroma_tx import sqlite_chroma_transaction


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", raising=False)


def _mock_conn():
    return MagicMock(name="sqlite_conn")


def _mock_collection():
    return MagicMock(name="chroma_collection")


def test_happy_path_commits_sqlite_no_chroma_compensation():
    conn = _mock_conn()
    coll = _mock_collection()

    with sqlite_chroma_transaction(conn, coll) as tx:
        tx.sqlite_conn.execute("INSERT INTO t VALUES (1)")
        coll.upsert(ids=["a"], documents=["doc-a"])

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()
    # Chroma compensation НЕ должна вызываться на happy path.
    coll.delete.assert_not_called()


def test_exception_inside_block_triggers_sqlite_rollback():
    conn = _mock_conn()
    coll = _mock_collection()

    with pytest.raises(RuntimeError, match="boom"):
        with sqlite_chroma_transaction(conn, coll) as tx:
            tx.sqlite_conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_strict_mode_runs_chroma_compensation(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", "1")
    conn = _mock_conn()
    coll = _mock_collection()
    comp = MagicMock(name="compensation")

    with pytest.raises(ValueError):
        with sqlite_chroma_transaction(conn, coll) as tx:
            coll.upsert(ids=["x"], documents=["doc"])
            tx.register_chroma_compensation(comp)
            raise ValueError("after chroma upsert")

    conn.rollback.assert_called_once()
    comp.assert_called_once()


def test_non_strict_skips_compensation_but_still_rollbacks(monkeypatch, caplog):
    monkeypatch.delenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", raising=False)
    conn = _mock_conn()
    coll = _mock_collection()
    comp = MagicMock(name="compensation")

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            with sqlite_chroma_transaction(conn, coll) as tx:
                tx.register_chroma_compensation(comp)
                raise RuntimeError("kaboom")

    conn.rollback.assert_called_once()
    comp.assert_not_called()
    # Должно быть warning о пропущенных compensations.
    assert any(
        "compensation" in rec.message.lower() and "strict=False" in rec.message
        for rec in caplog.records
    )


def test_explicit_strict_override_wins_over_env(monkeypatch):
    """strict=False явно должен побеждать env=1, и наоборот."""
    monkeypatch.setenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", "1")
    conn = _mock_conn()
    coll = _mock_collection()
    comp = MagicMock()

    with pytest.raises(RuntimeError):
        with sqlite_chroma_transaction(conn, coll, strict=False) as tx:
            tx.register_chroma_compensation(comp)
            raise RuntimeError("explicit override")

    conn.rollback.assert_called_once()
    comp.assert_not_called()  # strict=False → compensation skip


def test_compensation_failure_logged_and_original_exception_raised(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_SQLITE_CHROMA_STRICT_ROLLBACK", "1")
    conn = _mock_conn()
    coll = _mock_collection()

    def bad_comp():
        raise IOError("chroma unreachable")

    with caplog.at_level("CRITICAL"):
        with pytest.raises(ValueError, match="original"):
            with sqlite_chroma_transaction(conn, coll) as tx:
                tx.register_chroma_compensation(bad_comp)
                raise ValueError("original")

    # SQLite rollback всё равно отработал
    conn.rollback.assert_called_once()
    # critical-лог о падении compensation
    assert any(
        "compensation" in rec.message.lower() for rec in caplog.records
    )


def test_register_compensation_rejects_non_callable():
    conn = _mock_conn()
    coll = _mock_collection()

    with pytest.raises(TypeError, match="callable"):
        with sqlite_chroma_transaction(conn, coll) as tx:
            tx.register_chroma_compensation("not a function")  # type: ignore[arg-type]


def test_sqlite_rollback_failure_logged_but_original_raised(caplog):
    """Если rollback сам падает (например, connection закрыт) — логируем,
    исходное исключение пробрасывается, не маскируется."""
    conn = _mock_conn()
    conn.rollback.side_effect = RuntimeError("rollback fail")
    coll = _mock_collection()

    with caplog.at_level("ERROR"):
        with pytest.raises(ValueError, match="original"):
            with sqlite_chroma_transaction(conn, coll):
                raise ValueError("original")

    assert any("rollback failed" in rec.message.lower() for rec in caplog.records)


def test_none_chroma_collection_is_acceptable():
    """Если chroma_collection=None, helper не должен пытаться её трогать."""
    conn = _mock_conn()

    with sqlite_chroma_transaction(conn, None) as tx:
        assert tx.chroma_collection is None
        tx.sqlite_conn.execute("INSERT INTO t VALUES (1)")

    conn.commit.assert_called_once()
