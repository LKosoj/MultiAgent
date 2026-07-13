from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.fastapi_app.agui.store import EventStore
from workflow.result_identity import workflow_result_event_key
from workflow.result_outbox import WorkflowResultOutbox
from workflow.sqlite_optimizer_stats import (
    discard_validated_sqlite_optimizer_statistics,
    normalize_sqlite_schema_sql,
    validate_sqlite_optimizer_statistics,
)


StoreConstructor = type[EventStore] | type[WorkflowResultOutbox]


class _StatConnection:
    def __init__(self, *, compile_options: tuple[str, ...], columns: tuple[str, ...]):
        self.compile_options = compile_options
        self.columns = columns
        self.drops: list[str] = []

    def execute(self, sql: str, parameters=()):
        if sql == "PRAGMA compile_options":
            return [(option,) for option in self.compile_options]
        if "pragma_table_xinfo" in sql:
            return [
                (cid, name, "", 0, None, 0, 0) for cid, name in enumerate(self.columns)
            ]
        if sql.startswith("DROP TABLE"):
            self.drops.append(sql)
            return []
        raise AssertionError((sql, parameters))


def _owner(owner: str) -> tuple[StoreConstructor, str]:
    if owner == "event-store":
        return EventStore, "agui_events"
    return WorkflowResultOutbox, "workflow_result_outbox"


def _create_database(path: Path, constructor: StoreConstructor) -> None:
    managed = constructor(str(path))
    managed.close()


def _seed_database(path: Path, owner: str) -> dict:
    if owner == "event-store":
        payload = {"seed": 1}
        managed = EventStore(str(path))
        managed.append("seed-run", "RUN_STARTED", payload)
    else:
        run_id = "seed-run"
        run_incarnation = "seed-incarnation"
        payload = {
            "run_id": run_id,
            "run_incarnation": run_incarnation,
            "event_key": workflow_result_event_key(run_id, run_incarnation),
            "status": "completed",
        }
        managed = WorkflowResultOutbox(str(path))
        managed.enqueue(payload)
    managed.close()
    return payload


def _assert_seed_survives(path: Path, owner: str, payload: dict) -> None:
    if owner == "event-store":
        managed = EventStore(str(path))
        assert [event.payload for event in managed.list_after("seed-run")] == [payload]
    else:
        managed = WorkflowResultOutbox(str(path))
        assert managed.latest_payload("seed-run") == payload
    managed.close()


def _stat_table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name GLOB 'sqlite_stat[1-4]'
                """
            )
        }


@pytest.mark.parametrize(
    ("name", "compile_option", "sql", "columns"),
    [
        (
            "sqlite_stat1",
            None,
            "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
            ("tbl", "idx", "stat"),
        ),
        (
            "sqlite_stat2",
            "ENABLE_STAT2",
            "CREATE TABLE sqlite_stat2(tbl,idx,sampleno,sample)",
            ("tbl", "idx", "sampleno", "sample"),
        ),
        (
            "sqlite_stat3",
            "ENABLE_STAT3",
            "CREATE TABLE sqlite_stat3(tbl,idx,neq,nlt,ndlt,sample)",
            ("tbl", "idx", "neq", "nlt", "ndlt", "sample"),
        ),
        (
            "sqlite_stat4",
            "ENABLE_STAT4",
            "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
            ("tbl", "idx", "neq", "nlt", "ndlt", "sample"),
        ),
    ],
)
def test_shared_stats_policy_covers_each_runtime_supported_variant(
    name: str,
    compile_option: str | None,
    sql: str,
    columns: tuple[str, ...],
) -> None:
    connection = _StatConnection(
        compile_options=((compile_option,) if compile_option else ()),
        columns=columns,
    )
    actual = {
        name: ("table", name, normalize_sqlite_schema_sql(sql), 7),
    }

    validated = validate_sqlite_optimizer_statistics(
        connection,
        actual,
        owner="test owner",
    )
    discard_validated_sqlite_optimizer_statistics(connection, validated)

    assert validated == (name,)
    assert connection.drops == [f'DROP TABLE "{name}"']


@pytest.mark.parametrize("name", ["sqlite_stat2", "sqlite_stat3", "sqlite_stat4"])
def test_shared_stats_policy_rejects_variant_missing_runtime_support(
    name: str,
) -> None:
    connection = _StatConnection(compile_options=(), columns=())
    actual = {name: ("table", name, None, 7)}

    with pytest.raises(RuntimeError, match="unsupported"):
        validate_sqlite_optimizer_statistics(
            connection,
            actual,
            owner="test owner",
        )


@pytest.mark.parametrize("owner", ["event-store"])
def test_sqlite_owner_reopens_after_analyze(tmp_path: Path, owner: str) -> None:
    constructor, _table_name = _owner(owner)
    db_path = tmp_path / f"{owner}-analyzed.db"
    payload = _seed_database(db_path, owner)

    with sqlite3.connect(db_path) as connection:
        connection.execute("ANALYZE")
        stat_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name GLOB 'sqlite_stat[1-4]'
                """
            )
        }
    assert "sqlite_stat1" in stat_tables

    reopened = constructor(str(db_path))
    reopened.close()
    assert _stat_table_names(db_path) == set()
    _assert_seed_survives(db_path, owner, payload)


def test_outbox_reopens_after_analyze_without_discarding_stat_tables(
    tmp_path: Path,
) -> None:
    """The outbox no longer special-cases sqlite_stat1-4; ANALYZE survives."""
    constructor, _table_name = _owner("outbox")
    db_path = tmp_path / "outbox-analyzed.db"
    payload = _seed_database(db_path, "outbox")

    with sqlite3.connect(db_path) as connection:
        connection.execute("ANALYZE")
        stat_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name GLOB 'sqlite_stat[1-4]'
                """
            )
        }
    assert "sqlite_stat1" in stat_tables

    reopened = constructor(str(db_path))
    reopened.close()
    assert "sqlite_stat1" in _stat_table_names(db_path)
    _assert_seed_survives(db_path, "outbox", payload)


@pytest.mark.parametrize("owner", ["event-store"])
def test_sqlite_owner_discards_exact_writable_schema_stat_forgery(
    tmp_path: Path,
    owner: str,
) -> None:
    constructor, table_name = _owner(owner)
    db_path = tmp_path / f"{owner}-exact-forged-stat.db"
    payload = _seed_database(db_path, owner)

    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE forged_stats(tbl,idx,stat)")
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            """
            UPDATE sqlite_master
            SET name = 'sqlite_stat1',
                tbl_name = 'sqlite_stat1',
                sql = 'CREATE TABLE sqlite_stat1(tbl,idx,stat)'
            WHERE type = 'table' AND name = 'forged_stats'
            """
        )
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        forged = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master WHERE name = 'sqlite_stat1'
            """
        ).fetchone()
        assert forged == (
            "table",
            "sqlite_stat1",
            "sqlite_stat1",
            "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
        )
        assert connection.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone() == (
            0,
        )

    reopened = constructor(str(db_path))
    reopened.close()

    assert _stat_table_names(db_path) == set()
    _assert_seed_survives(db_path, owner, payload)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
            (table_name,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("owner", "injected_kind"),
    [
        ("event-store", "table"),
        ("event-store", "view"),
        ("event-store", "trigger"),
        ("event-store", "index"),
        ("event-store", "forged-sqlite-stat1"),
        # The outbox no longer enumerates every sqlite_master object; only a
        # trigger on its own tables stays a hard failure (see
        # workflow.result_outbox._reject_outbox_triggers). This covers both
        # tables it protects: the outbox itself and its dead_letter table.
        ("outbox", "trigger"),
        ("outbox", "trigger-dead-letter"),
    ],
)
def test_sqlite_owner_rejects_writable_schema_injection(
    tmp_path: Path,
    owner: str,
    injected_kind: str,
) -> None:
    constructor, table_name = _owner(owner)
    db_path = tmp_path / f"{owner}-{injected_kind}.db"
    _create_database(db_path, constructor)

    with sqlite3.connect(db_path) as connection:
        rootpage = int(
            connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = ?",
                (table_name,),
            ).fetchone()[0]
        )
        column_name = "run_id" if owner == "event-store" else "event_key"
        definitions = {
            "table": (
                "table",
                "injected_table",
                "injected_table",
                rootpage,
                "CREATE TABLE injected_table(value TEXT)",
            ),
            "view": (
                "view",
                "injected_view",
                "injected_view",
                0,
                f"CREATE VIEW injected_view AS SELECT {column_name} FROM {table_name}",
            ),
            "trigger": (
                "trigger",
                "injected_trigger",
                table_name,
                0,
                f"CREATE TRIGGER injected_trigger AFTER INSERT ON {table_name} "
                "BEGIN SELECT 1; END",
            ),
            "trigger-dead-letter": (
                "trigger",
                "injected_trigger",
                "workflow_result_outbox_dead_letter",
                0,
                "CREATE TRIGGER injected_trigger AFTER INSERT ON "
                "workflow_result_outbox_dead_letter BEGIN SELECT 1; END",
            ),
            "index": (
                "index",
                "injected_index",
                table_name,
                rootpage,
                f"CREATE INDEX injected_index ON {table_name}({column_name})",
            ),
            "forged-sqlite-stat1": (
                "table",
                "sqlite_stat1",
                "sqlite_stat1",
                rootpage,
                "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
            ),
        }
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            """
            INSERT INTO sqlite_master(type, name, tbl_name, rootpage, sql)
            VALUES (?, ?, ?, ?, ?)
            """,
            definitions[injected_kind],
        )
        connection.commit()

    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        constructor(str(db_path))
