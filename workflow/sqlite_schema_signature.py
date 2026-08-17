"""Shared structural signatures for owned SQLite schema objects."""

from __future__ import annotations

import sqlite3


class SQLiteSchemaSignatureError(ValueError):
    """A stored SQLite schema definition cannot be tokenized safely."""


SchemaTokens = tuple[tuple[str, str], ...]
SchemaSignature = tuple[tuple[str, str, str, SchemaTokens | None], ...]


def sqlite_schema_sql_tokens(sql: str) -> SchemaTokens:
    """Tokenize DDL while ignoring formatting but preserving quoted bytes."""
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                raise SQLiteSchemaSignatureError(
                    "SQLite schema contains an unterminated comment"
                )
            index = comment_end + 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] != quote:
                    index += 1
                    continue
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                index += 1
                break
            else:
                raise SQLiteSchemaSignatureError(
                    "SQLite schema contains an unterminated quoted token"
                )
            token_kind = "string_literal" if quote == "'" else "quoted_identifier"
            tokens.append((token_kind, sql[start:index]))
            continue
        if character == "[":
            start = index
            bracket_end = sql.find("]", index + 1)
            if bracket_end < 0:
                raise SQLiteSchemaSignatureError(
                    "SQLite schema contains an unterminated identifier"
                )
            index = bracket_end + 1
            tokens.append(("quoted_identifier", sql[start:index]))
            continue
        if character.isalnum() or character in {"_", "$"}:
            start = index
            index += 1
            while index < len(sql) and (
                sql[index].isalnum() or sql[index] in {"_", "$"}
            ):
                index += 1
            tokens.append(("word", sql[start:index].casefold()))
            continue
        tokens.append(("symbol", character))
        index += 1
    while tokens and tokens[-1] == ("symbol", ";"):
        tokens.pop()
    return tuple(tokens)


def owned_sqlite_schema_signature(
    connection: sqlite3.Connection,
    *,
    prefix: str,
    table_names: tuple[str, ...],
) -> SchemaSignature:
    """Return every object in an owned namespace using SQLite-like casing."""
    if not table_names:
        raise ValueError("table_names must not be empty")
    table_placeholders = ", ".join("?" for _ in table_names)
    rows = connection.execute(
        f"""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND (
              lower(substr(name, 1, ?)) = ?
              OR lower(tbl_name) IN ({table_placeholders})
          )
        ORDER BY type, name
        """,
        (
            len(prefix),
            prefix.casefold(),
            *(table_name.casefold() for table_name in table_names),
        ),
    ).fetchall()
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            None if row[3] is None else sqlite_schema_sql_tokens(str(row[3])),
        )
        for row in rows
    )
