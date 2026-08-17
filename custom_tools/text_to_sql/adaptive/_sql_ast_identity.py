"""Independent identities for exact SQL bytes and normalized AST facts."""

from __future__ import annotations

from hashlib import sha256

from ._sql_ast_expression import digest
from ._sql_ast_models import ParsedSqlCandidate


def source_sql_digest(sql: str) -> str:
    if type(sql) is not str:
        raise TypeError("sql must be exact text")
    return f"sha256:{sha256(sql.encode('utf-8')).hexdigest()}"


def semantic_candidate_digest(candidate: ParsedSqlCandidate) -> str:
    if type(candidate) is not ParsedSqlCandidate:
        raise TypeError("candidate must be ParsedSqlCandidate")
    payload = {
        "dialect": candidate.dialect,
        "scopes": candidate.scopes,
        "ctes": candidate.ctes,
        "table_scans": candidate.table_scans,
        "cte_references": candidate.cte_references,
        "derived_relations": candidate.derived_relations,
        "expression_relations": candidate.expression_relations,
        "set_operations": candidate.set_operations,
        "subquery_refs": candidate.subquery_refs,
        "joins": candidate.joins,
        "projections": candidate.projections,
        "predicates": candidate.predicates,
        "aggregates": candidate.aggregates,
        "groupings": candidate.groupings,
        "orderings": candidate.orderings,
        "limits": candidate.limits,
    }
    return f"sha256:{digest(payload)}"


__all__ = ["semantic_candidate_digest", "source_sql_digest"]
