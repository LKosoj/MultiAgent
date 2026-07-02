"""Result comparison for Text-to-SQL evals."""

from __future__ import annotations

from typing import Any


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        normalized.append({str(key): row[key] for key in sorted(row)})
    return normalized


def rows_exact_match(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    return normalize_rows(actual) == normalize_rows(expected)
