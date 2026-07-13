"""Strict policy for ephemeral SQLite optimizer-statistics tables."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from sqlite3 import Connection
from typing import Optional


_SQLITE_ANALYZE_TABLES = {
    "sqlite_stat1": (
        None,
        "CREATE TABLE SQLITE_STAT1(TBL,IDX,STAT)",
        (("tbl", ""), ("idx", ""), ("stat", "")),
    ),
    "sqlite_stat2": (
        "ENABLE_STAT2",
        "CREATE TABLE SQLITE_STAT2(TBL,IDX,SAMPLENO,SAMPLE)",
        (("tbl", ""), ("idx", ""), ("sampleno", ""), ("sample", "")),
    ),
    "sqlite_stat3": (
        "ENABLE_STAT3",
        "CREATE TABLE SQLITE_STAT3(TBL,IDX,NEQ,NLT,NDLT,SAMPLE)",
        (
            ("tbl", ""),
            ("idx", ""),
            ("neq", ""),
            ("nlt", ""),
            ("ndlt", ""),
            ("sample", ""),
        ),
    ),
    "sqlite_stat4": (
        "ENABLE_STAT4",
        "CREATE TABLE SQLITE_STAT4(TBL,IDX,NEQ,NLT,NDLT,SAMPLE)",
        (
            ("tbl", ""),
            ("idx", ""),
            ("neq", ""),
            ("nlt", ""),
            ("ndlt", ""),
            ("sample", ""),
        ),
    ),
}

SqliteMasterObject = tuple[str, str, Optional[str], int]


def normalize_sqlite_schema_sql(sql: str) -> str:
    normalized = " ".join(sql.upper().split())
    return re.sub(r"\s*([(),])\s*", r"\1", normalized)


def validate_sqlite_optimizer_statistics(
    connection: Connection,
    actual_objects: Mapping[str, SqliteMasterObject],
    *,
    owner: str,
) -> tuple[str, ...]:
    """Return exact, runtime-supported optimizer-statistics table names."""
    compile_options = {
        str(row[0]).upper() for row in connection.execute("PRAGMA compile_options")
    }
    supported = {
        name
        for name, (compile_option, _sql, _signature) in (_SQLITE_ANALYZE_TABLES.items())
        if compile_option is None or compile_option in compile_options
    }
    present = set(actual_objects) & set(_SQLITE_ANALYZE_TABLES)
    unsupported = present - supported
    if unsupported:
        raise RuntimeError(
            f"{owner} has unsupported SQLite statistics tables: "
            + ", ".join(sorted(unsupported))
        )

    rootpage_owners: dict[int, set[str]] = {}
    for object_name, (_type, _table, _sql, rootpage) in actual_objects.items():
        if rootpage > 0:
            rootpage_owners.setdefault(rootpage, set()).add(object_name)

    for name in sorted(present):
        _compile_option, expected_sql, expected_signature = _SQLITE_ANALYZE_TABLES[name]
        object_type, table_name, normalized_sql, rootpage = actual_objects[name]
        rows: Sequence[tuple] = list(
            connection.execute(
                "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
                (name,),
            )
        )
        signature = tuple((str(row[1]), str(row[2]).upper()) for row in rows)
        if (
            object_type != "table"
            or table_name != name
            or normalized_sql != normalize_sqlite_schema_sql(expected_sql)
            or signature != expected_signature
            or any(
                int(row[3]) != 0
                or row[4] is not None
                or int(row[5]) != 0
                or int(row[6]) != 0
                for row in rows
            )
            or rootpage <= 0
            or rootpage_owners.get(rootpage) != {name}
        ):
            raise RuntimeError(f"{owner} SQLite statistics table is malformed: {name}")
    return tuple(sorted(present))


def discard_validated_sqlite_optimizer_statistics(
    connection: Connection,
    table_names: Sequence[str],
) -> None:
    """Discard validated stats so an indistinguishable forgery cannot persist."""
    for name in sorted(set(table_names), reverse=True):
        if name not in _SQLITE_ANALYZE_TABLES:
            raise ValueError("unsupported SQLite statistics table")
        connection.execute(f'DROP TABLE "{name}"')
