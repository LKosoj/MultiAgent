"""Human-readable provenance footer for the Text-to-SQL terminal contract.

W4-4.1: the terminal contract's ``provenance`` evidence field (see
``workflow.text_to_sql_contract``) is a machine-shaped trace of one executed
run — which tables/columns the final SQL touched, how many rows came back
against the configured limit, and what the safety/result-review checks said.
This module renders that mapping into a short Russian-language footer an
operator can read alongside the answer. Purely presentational: it never
raises and never mutates its input.
"""

from __future__ import annotations

from typing import Any, Mapping


def format_text_to_sql_provenance_footer(provenance: Mapping[str, Any]) -> str:
    """Render one ``provenance`` mapping as a short Russian-language footer.

    Returns ``""`` for an empty/absent/malformed mapping (e.g. the run never
    reached execution — ABSTAINED/FAILED before the query ran). Any
    unexpected shape degrades to ``""`` rather than raising, since this is a
    best-effort display helper, not a contract validator.
    """
    if not isinstance(provenance, Mapping) or not provenance:
        return ""
    try:
        run_id = provenance.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return ""

        tables = provenance.get("tables")
        table_names = (
            [table for table in tables if isinstance(table, str) and table]
            if isinstance(tables, list)
            else []
        )
        if provenance.get("parse_error"):
            tables_clause = "таблицы не определены (не удалось разобрать SQL)"
        elif table_names:
            tables_clause = "таблицы " + ", ".join(table_names)
            if provenance.get("has_derived_tables"):
                tables_clause += " (через промежуточные подзапросы)"
        elif provenance.get("has_derived_tables"):
            tables_clause = "таблицы не использованы напрямую (только промежуточные подзапросы)"
        else:
            tables_clause = "таблицы не использованы"

        clauses = [f"Источник: {tables_clause}"]

        row_count = provenance.get("row_count")
        row_limit = provenance.get("row_limit")
        if (
            isinstance(row_count, int)
            and isinstance(row_limit, int)
            and row_limit > 0
        ):
            truncated = ", возможно усечено" if provenance.get("possibly_truncated") else ""
            clauses.append(f"строк: {row_count} (лимит {row_limit}{truncated})")

        checks = []
        safety_llm_audit = provenance.get("safety_llm_audit")
        if isinstance(safety_llm_audit, str) and safety_llm_audit:
            checks.append(f"safety={safety_llm_audit}")
        result_review_verdict = provenance.get("result_review_verdict")
        if isinstance(result_review_verdict, str) and result_review_verdict:
            checks.append(f"review={result_review_verdict}")
        if checks:
            clauses.append("проверки: " + ", ".join(checks))

        clauses.append(f"run {run_id}")

        return "; ".join(clauses) + "."
    except Exception:
        return ""
