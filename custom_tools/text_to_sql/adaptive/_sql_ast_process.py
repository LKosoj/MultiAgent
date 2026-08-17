"""Изолированный, гарантированно завершаемый процесс разбора W5-02."""

from __future__ import annotations

import json
import math
import mmap
import multiprocessing
from multiprocessing import popen_fork as _popen_fork  # noqa: F401
import os
import time
from typing import Any, Callable, TypeVar

from . import _sql_ast_models as ast
from ._sql_ast_builder import parse_and_build_candidate
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest


_MAX_RESULT_BYTES = 8 * 1024 * 1024
_MAX_FACTS = 50_000
_MAX_TEXT_BYTES = 65_536
_TERMINATE_GRACE_SECONDS = 0.1
_MAX_JOIN_SLICE_SECONDS = 60.0
_WIRE_VERSION = 9
_RESULT_READY = 1
_RESULT_LENGTH_BYTES = 8
_RESULT_HEADER_BYTES = 1 + _RESULT_LENGTH_BYTES
_RESULT_BUFFER_BYTES = _RESULT_HEADER_BYTES + _MAX_RESULT_BYTES


class _MalformedPayload(ValueError):
    pass


def _configuration_error(name: str) -> ast.SqlAstError:
    return ast.SqlAstError(
        ast.SqlAstErrorCode.PARSE_FAILED,
        f"{name} has an invalid positive value",
    )


def _parse_timeout_seconds() -> float:
    name = "TEXT_TO_SQL_SQLGLOT_TIMEOUT_S"
    try:
        value = float(os.getenv(name, "5"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _configuration_error(name) from exc
    if not math.isfinite(value) or value <= 0:
        raise _configuration_error(name)
    return value


def _configured_max_sql_length() -> int:
    name = "TEXT_TO_SQL_MAX_SQL_LENGTH"
    try:
        value = int(os.getenv(name, "50000"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _configuration_error(name) from exc
    if value <= 0:
        raise _configuration_error(name)
    return value


def _validate_input_size(sql: str) -> None:
    max_length = _configured_max_sql_length()
    if len(sql) > max_length:
        raise ast.SqlAstError(
            ast.SqlAstErrorCode.SHAPE_UNSUPPORTED,
            "SQL input exceeds configured parser bounds",
        )


def _validated_ast_limits(max_ast_nodes: Any, max_ast_depth: Any) -> ast.AstLimits:
    values = (
        (max_ast_nodes, ast.MAX_AST_NODES, "MAX_AST_NODES"),
        (max_ast_depth, ast.MAX_AST_DEPTH, "MAX_AST_DEPTH"),
    )
    for value, upper_bound, name in values:
        if type(value) is not int or not 1 <= value <= upper_bound:
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_FAILED,
                f"{name} must be a bounded positive integer",
            )
    return ast.AstLimits(max_nodes=max_ast_nodes, max_depth=max_ast_depth)


def _expression_to_wire(expression: ast.ExpressionFact) -> list[Any]:
    return [
        expression.kind,
        [list(attribute) for attribute in expression.attributes],
        [
            [argument, ordinal, _expression_to_wire(child)]
            for argument, ordinal, child in expression.children
        ],
    ]


def _candidate_to_wire(candidate: ast.ParsedSqlCandidate) -> dict[str, Any]:
    expression = _expression_to_wire
    return {
        "candidate_id": candidate.candidate_id,
        "dialect": candidate.dialect,
        "candidate_digest": candidate.candidate_digest,
        "source_sql_digest": candidate.source_sql_digest,
        "scopes": [
            [
                fact.node_id,
                fact.scope_id,
                fact.parent_scope_id,
                fact.query_role.value,
                expression(fact.distinct) if fact.distinct else None,
                expression(fact.options) if fact.options else None,
            ]
            for fact in candidate.scopes
        ],
        "ctes": [
            [
                fact.node_id,
                fact.declaring_scope_id,
                fact.cte_id,
                fact.query_scope_id,
                fact.recursive,
                list(fact.column_aliases),
            ]
            for fact in candidate.ctes
        ],
        "table_scans": [
            [
                fact.node_id,
                fact.scope_id,
                fact.relation_id,
                [fact.table.catalog, fact.table.schema, fact.table.name],
                fact.source_alias,
                list(fact.column_aliases),
                expression(fact.options) if fact.options else None,
            ]
            for fact in candidate.table_scans
        ],
        "cte_references": [
            [
                fact.node_id,
                fact.scope_id,
                fact.relation_id,
                fact.cte_id,
                fact.source_alias,
                list(fact.column_aliases),
            ]
            for fact in candidate.cte_references
        ],
        "derived_relations": [
            [
                fact.node_id,
                fact.scope_id,
                fact.relation_id,
                fact.query_scope_id,
                fact.lateral,
                fact.source_alias,
                list(fact.column_aliases),
            ]
            for fact in candidate.derived_relations
        ],
        "expression_relations": [
            [
                fact.node_id,
                fact.scope_id,
                fact.relation_id,
                fact.source_alias,
                expression(fact.expression),
                list(fact.input_relation_ids),
                list(fact.column_aliases),
            ]
            for fact in candidate.expression_relations
        ],
        "set_operations": [
            [
                fact.node_id,
                fact.scope_id,
                fact.parent_scope_id,
                fact.query_role.value,
                fact.operation.value,
                fact.left_scope_id,
                fact.right_scope_id,
                fact.distinct,
                expression(fact.options) if fact.options else None,
            ]
            for fact in candidate.set_operations
        ],
        "subquery_refs": [
            [
                fact.node_id,
                fact.scope_id,
                fact.child_scope_id,
                fact.query_role.value,
            ]
            for fact in candidate.subquery_refs
        ],
        "joins": [
            [
                fact.node_id,
                fact.scope_id,
                fact.relation_id,
                list(fact.left_relation_ids),
                fact.output_visible,
                expression(fact.condition) if fact.condition else None,
                list(fact.using_columns),
                expression(fact.options) if fact.options else None,
            ]
            for fact in candidate.joins
        ],
        "projections": [
            [fact.node_id, fact.scope_id, fact.output_id, expression(fact.expression)]
            for fact in candidate.projections
        ],
        "predicates": [
            [
                fact.node_id,
                fact.scope_id,
                fact.location.value,
                fact.owner_node_id,
                expression(fact.expression),
                [
                    [atom.node_id, list(atom.path), expression(atom.expression)]
                    for atom in fact.atoms
                ],
            ]
            for fact in candidate.predicates
        ],
        "aggregates": [
            [
                fact.node_id,
                fact.scope_id,
                fact.function,
                fact.distinct,
                expression(fact.expression),
            ]
            for fact in candidate.aggregates
        ],
        "groupings": [
            [
                fact.node_id,
                fact.scope_id,
                expression(fact.expression),
            ]
            for fact in candidate.groupings
        ],
        "orderings": [
            [
                fact.node_id,
                fact.scope_id,
                expression(fact.expression),
                fact.descending,
                fact.nulls_first,
                expression(fact.options) if fact.options else None,
            ]
            for fact in candidate.orderings
        ],
        "limits": [
            [fact.node_id, fact.scope_id, fact.count, fact.offset]
            for fact in candidate.limits
        ],
    }


def _encode_envelope(candidate: ast.ParsedSqlCandidate) -> bytes:
    raw = json.dumps(
        {
            "version": _WIRE_VERSION,
            "status": "ok",
            "candidate": _candidate_to_wire(candidate),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > _MAX_RESULT_BYTES:
        return _encode_error(ast.SqlAstErrorCode.SHAPE_UNSUPPORTED)
    return raw


def _encode_error(code: ast.SqlAstErrorCode) -> bytes:
    return json.dumps(
        {"version": _WIRE_VERSION, "status": "error", "code": code.value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _publish_result(result_buffer: Any, payload: bytes) -> None:
    if not payload or len(payload) > _MAX_RESULT_BYTES:
        raise ValueError("worker result does not fit the bounded buffer")
    view = memoryview(result_buffer).cast("B")
    try:
        result_end = _RESULT_HEADER_BYTES + len(payload)
        view[_RESULT_HEADER_BYTES:result_end] = payload
        view[1:_RESULT_HEADER_BYTES] = len(payload).to_bytes(
            _RESULT_LENGTH_BYTES,
            byteorder="big",
        )
        # READY пишется последним, поэтому частичный результат не будет принят.
        view[0] = _RESULT_READY
    finally:
        view.release()


def _read_published_result(result_buffer: Any) -> bytes:
    view = memoryview(result_buffer).cast("B")
    try:
        if view[0] != _RESULT_READY:
            raise _MalformedPayload("worker did not publish a complete result")
        length = int.from_bytes(view[1:_RESULT_HEADER_BYTES], byteorder="big")
        if not 1 <= length <= _MAX_RESULT_BYTES:
            raise _MalformedPayload("worker result length is invalid")
        result_end = _RESULT_HEADER_BYTES + length
        return bytes(view[_RESULT_HEADER_BYTES:result_end])
    finally:
        view.release()


def _parse_worker(
    result_buffer: Any,
    sql: str,
    dialect: str,
    candidate_id: str,
    limits: ast.AstLimits,
) -> None:
    try:
        candidate = parse_and_build_candidate(sql, dialect, candidate_id, limits)
        payload = _encode_envelope(candidate)
    except ast.SqlAstError as exc:
        payload = _encode_error(exc.code)
    except Exception:
        payload = _encode_error(ast.SqlAstErrorCode.PARSE_FAILED)
    _publish_result(result_buffer, payload)


def _require_list(value: Any, length: int | None, context: str) -> list[Any]:
    if not isinstance(value, list) or (length is not None and len(value) != length):
        raise _MalformedPayload(f"{context} has an invalid list shape")
    return value


def _require_collection(value: Any, context: str) -> list[Any]:
    items = _require_list(value, None, context)
    if len(items) > _MAX_FACTS:
        raise _MalformedPayload(f"{context} contains too many facts")
    return items


def _require_text(value: Any, context: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise _MalformedPayload(f"{context} must be non-empty text")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise _MalformedPayload(f"{context} is too long")
    return value


def _require_bool(value: Any, context: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise _MalformedPayload(f"{context} must be boolean")
    return value


def _require_int(value: Any, context: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise _MalformedPayload(f"{context} must be an integer")
    return value


def _require_scalar(value: Any, context: str) -> ast.CanonicalScalar:
    if value is None or type(value) in {str, int, bool}:
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise _MalformedPayload(f"{context} is too long")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise _MalformedPayload(f"{context} is not a finite scalar")


def _decode_expression(
    value: Any,
    *,
    limits: ast.AstLimits,
    depth: int = 1,
    count: list[int] | None = None,
) -> ast.ExpressionFact:
    if depth > limits.max_depth:
        raise _MalformedPayload("expression exceeds AST depth")
    counter = count if count is not None else [0]
    counter[0] += 1
    if counter[0] > limits.max_nodes:
        raise _MalformedPayload("expression contains too many nodes")
    kind, raw_attributes, raw_children = _require_list(value, 3, "expression")
    kind_text = _require_text(kind, "expression kind")
    attributes: list[tuple[str, ast.CanonicalScalar]] = []
    for raw_attribute in _require_collection(raw_attributes, "expression attributes"):
        name, attribute_value = _require_list(raw_attribute, 2, "expression attribute")
        attributes.append(
            (
                str(_require_text(name, "expression attribute name")),
                _require_scalar(attribute_value, "expression attribute value"),
            )
        )
    children: list[tuple[str, int, ast.ExpressionFact]] = []
    for raw_child in _require_collection(raw_children, "expression children"):
        argument, ordinal, child = _require_list(raw_child, 3, "expression child")
        child_ordinal = _require_int(ordinal, "expression child ordinal")
        if child_ordinal is None or child_ordinal < 0:
            raise _MalformedPayload("expression child ordinal must be non-negative")
        children.append(
            (
                str(_require_text(argument, "expression child argument")),
                child_ordinal,
                _decode_expression(
                    child,
                    limits=limits,
                    depth=depth + 1,
                    count=counter,
                ),
            )
        )
    return ast.ExpressionFact(str(kind_text), tuple(attributes), tuple(children))


T = TypeVar("T")


def _decode_collection(
    value: Any,
    context: str,
    decoder: Callable[[Any], T],
) -> tuple[T, ...]:
    try:
        return tuple(decoder(item) for item in _require_collection(value, context))
    except _MalformedPayload:
        raise
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise _MalformedPayload(f"{context} contains an invalid fact") from exc


def _decode_candidate(
    value: Any,
    *,
    limits: ast.AstLimits,
) -> ast.ParsedSqlCandidate:
    expected_keys = {
        "candidate_id",
        "dialect",
        "candidate_digest",
        "source_sql_digest",
        "scopes",
        "ctes",
        "table_scans",
        "cte_references",
        "derived_relations",
        "expression_relations",
        "set_operations",
        "subquery_refs",
        "joins",
        "projections",
        "predicates",
        "aggregates",
        "groupings",
        "orderings",
        "limits",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise _MalformedPayload("candidate object has invalid keys")

    expression_count = [0]

    def decode_expression(value: Any) -> ast.ExpressionFact:
        return _decode_expression(value, limits=limits, count=expression_count)

    def decode_aliases(value: Any, context: str) -> tuple[str, ...]:
        aliases = tuple(
            str(_require_text(item, context))
            for item in _require_collection(value, context)
        )
        if len(aliases) != len(set(aliases)):
            raise _MalformedPayload(f"{context} must be unique")
        return aliases

    def scope(item: Any) -> ast.SelectScopeFact:
        node, scope_id, parent_scope_id, query_role, distinct, options = _require_list(
            item, 6, "scope fact"
        )
        return ast.SelectScopeFact(
            str(_require_text(node, "scope node ID")),
            str(_require_text(scope_id, "scope ID")),
            _require_text(parent_scope_id, "parent scope ID", nullable=True),
            ast.QueryRole(str(_require_text(query_role, "query role"))),
            decode_expression(distinct) if distinct is not None else None,
            decode_expression(options) if options is not None else None,
        )

    def cte(item: Any) -> ast.CteFact:
        node, scope_id, cte_id, query_scope, recursive, column_aliases = _require_list(
            item, 6, "CTE fact"
        )
        return ast.CteFact(
            str(_require_text(node, "CTE node ID")),
            str(_require_text(scope_id, "CTE declaring scope")),
            str(_require_text(cte_id, "CTE ID")),
            str(_require_text(query_scope, "CTE query scope")),
            bool(_require_bool(recursive, "CTE recursive flag")),
            decode_aliases(column_aliases, "CTE column aliases"),
        )

    def table_scan(item: Any) -> ast.TableScanFact:
        node, scope_id, relation_id, raw_table, source_alias, column_aliases, options = _require_list(
            item, 7, "table scan fact"
        )
        catalog, schema, name = _require_list(raw_table, 3, "qualified table")
        return ast.TableScanFact(
            str(_require_text(node, "table scan node ID")),
            str(_require_text(scope_id, "table scan scope")),
            str(_require_text(relation_id, "table scan relation ID")),
            str(_require_text(source_alias, "table scan source alias")),
            ast.QualifiedTableName(
                _require_text(catalog, "table catalog", nullable=True),
                _require_text(schema, "table schema", nullable=True),
                str(_require_text(name, "table name")),
            ),
            decode_aliases(column_aliases, "table scan column aliases"),
            decode_expression(options) if options is not None else None,
        )

    def cte_reference(item: Any) -> ast.CteReferenceFact:
        node, scope_id, relation_id, cte_id, source_alias, column_aliases = _require_list(
            item, 6, "CTE reference fact"
        )
        return ast.CteReferenceFact(
            str(_require_text(node, "CTE reference node ID")),
            str(_require_text(scope_id, "CTE reference scope")),
            str(_require_text(relation_id, "CTE reference relation ID")),
            str(_require_text(source_alias, "CTE reference source alias")),
            str(_require_text(cte_id, "referenced CTE ID")),
            decode_aliases(column_aliases, "CTE reference column aliases"),
        )

    def derived_relation(item: Any) -> ast.DerivedRelationFact:
        (
            node,
            scope_id,
            relation_id,
            query_scope_id,
            lateral,
            source_alias,
            column_aliases,
        ) = _require_list(item, 7, "derived relation fact")
        return ast.DerivedRelationFact(
            str(_require_text(node, "derived relation node ID")),
            str(_require_text(scope_id, "derived relation scope")),
            str(_require_text(relation_id, "derived relation ID")),
            str(_require_text(source_alias, "derived relation source alias")),
            str(_require_text(query_scope_id, "derived query scope")),
            bool(_require_bool(lateral, "derived relation lateral flag")),
            decode_aliases(column_aliases, "derived relation column aliases"),
        )

    def expression_relation(item: Any) -> ast.ExpressionRelationFact:
        node, scope_id, relation_id, source_alias, expression, input_relation_ids, column_aliases = _require_list(
            item, 7, "expression relation fact"
        )
        return ast.ExpressionRelationFact(
            str(_require_text(node, "expression relation node ID")),
            str(_require_text(scope_id, "expression relation scope")),
            str(_require_text(relation_id, "expression relation ID")),
            str(_require_text(source_alias, "expression relation source alias")),
            decode_expression(expression),
            tuple(
                str(_require_text(item, "expression relation input relation ID"))
                for item in _require_collection(
                    input_relation_ids, "expression relation input relation IDs"
                )
            ),
            decode_aliases(column_aliases, "expression relation column aliases"),
        )

    def set_operation(item: Any) -> ast.SetOperationFact:
        (
            node,
            scope_id,
            parent_scope_id,
            query_role,
            operation,
            left_scope_id,
            right_scope_id,
            distinct,
            options,
        ) = _require_list(item, 9, "set operation fact")
        return ast.SetOperationFact(
            str(_require_text(node, "set operation node ID")),
            str(_require_text(scope_id, "set operation scope")),
            _require_text(parent_scope_id, "parent scope ID", nullable=True),
            ast.QueryRole(str(_require_text(query_role, "query role"))),
            ast.SetOperationKind(str(_require_text(operation, "set operation kind"))),
            str(_require_text(left_scope_id, "set left scope")),
            str(_require_text(right_scope_id, "set right scope")),
            bool(_require_bool(distinct, "set operation distinct flag")),
            decode_expression(options) if options is not None else None,
        )

    def subquery_ref(item: Any) -> ast.SubqueryRefFact:
        node, scope_id, child_scope_id, query_role = _require_list(
            item, 4, "subquery reference fact"
        )
        return ast.SubqueryRefFact(
            str(_require_text(node, "subquery reference node ID")),
            str(_require_text(scope_id, "subquery reference scope")),
            str(_require_text(child_scope_id, "subquery child scope")),
            ast.QueryRole(str(_require_text(query_role, "query role"))),
        )

    def join(item: Any) -> ast.JoinFact:
        (
            node,
            scope_id,
            relation_id,
            raw_left,
            output_visible,
            condition,
            raw_using_columns,
            options,
        ) = _require_list(
            item, 8, "join fact"
        )
        left_ids = tuple(
            str(_require_text(left, "join left relation ID"))
            for left in _require_collection(raw_left, "join left relations")
        )
        return ast.JoinFact(
            str(_require_text(node, "join node ID")),
            str(_require_text(scope_id, "join scope")),
            str(_require_text(relation_id, "join relation ID")),
            left_ids,
            _require_bool(output_visible, "join output visibility"),
            decode_expression(condition) if condition is not None else None,
            tuple(
                str(_require_text(column, "JOIN USING column"))
                for column in _require_collection(raw_using_columns, "JOIN USING columns")
            ),
            decode_expression(options) if options is not None else None,
        )

    def projection(item: Any) -> ast.ProjectionFact:
        node, scope_id, output_id, expression = _require_list(
            item, 4, "projection fact"
        )
        return ast.ProjectionFact(
            str(_require_text(node, "projection node ID")),
            str(_require_text(scope_id, "projection scope")),
            str(_require_text(output_id, "projection output ID")),
            decode_expression(expression),
        )

    def predicate(item: Any) -> ast.PredicateFact:
        node, scope_id, location, owner, expression, raw_atoms = _require_list(
            item, 6, "predicate fact"
        )

        def atom(raw_atom: Any) -> ast.PredicateAtomFact:
            atom_node, raw_path, atom_expression = _require_list(
                raw_atom, 3, "predicate atom"
            )
            path = tuple(
                str(_require_text(part, "predicate atom path"))
                for part in _require_collection(raw_path, "predicate atom path")
            )
            return ast.PredicateAtomFact(
                str(_require_text(atom_node, "predicate atom node ID")),
                path,
                decode_expression(atom_expression),
            )

        return ast.PredicateFact(
            str(_require_text(node, "predicate node ID")),
            str(_require_text(scope_id, "predicate scope")),
            ast.PredicateLocation(str(_require_text(location, "predicate location"))),
            _require_text(owner, "predicate owner node ID", nullable=True),
            decode_expression(expression),
            _decode_collection(raw_atoms, "predicate atoms", atom),
        )

    def aggregate(item: Any) -> ast.AggregateFact:
        node, scope_id, function, distinct, expression = _require_list(
            item, 5, "aggregate fact"
        )
        return ast.AggregateFact(
            str(_require_text(node, "aggregate node ID")),
            str(_require_text(scope_id, "aggregate scope")),
            str(_require_text(function, "aggregate function")),
            bool(_require_bool(distinct, "aggregate distinct")),
            decode_expression(expression),
        )

    def grouping(item: Any) -> ast.GroupingFact:
        node, scope_id, raw_expression = _require_list(item, 3, "grouping fact")
        return ast.GroupingFact(
            str(_require_text(node, "grouping node ID")),
            str(_require_text(scope_id, "grouping scope")),
            decode_expression(raw_expression),
        )

    def ordering(item: Any) -> ast.OrderingFact:
        node, scope_id, expression, descending, nulls_first, options = _require_list(
            item, 6, "ordering fact"
        )
        return ast.OrderingFact(
            str(_require_text(node, "ordering node ID")),
            str(_require_text(scope_id, "ordering scope")),
            decode_expression(expression),
            bool(_require_bool(descending, "ordering direction")),
            _require_bool(nulls_first, "ordering nulls first", nullable=True),
            decode_expression(options) if options is not None else None,
        )

    def limit(item: Any) -> ast.LimitFact:
        node, scope_id, count, offset = _require_list(item, 4, "limit fact")
        decoded_count = _require_int(count, "limit count", nullable=True)
        decoded_offset = _require_int(offset, "limit offset")
        if decoded_count is not None and decoded_count < 0:
            raise _MalformedPayload("limit count must be non-negative")
        if decoded_offset is None or decoded_offset < 0:
            raise _MalformedPayload("limit offset must be non-negative")
        return ast.LimitFact(
            str(_require_text(node, "limit node ID")),
            str(_require_text(scope_id, "limit scope")),
            decoded_count,
            decoded_offset,
        )

    candidate_digest = str(_require_text(value["candidate_digest"], "candidate digest"))
    source_digest = str(_require_text(value["source_sql_digest"], "source SQL digest"))
    for digest_value, context in (
        (candidate_digest, "candidate digest"),
        (source_digest, "source SQL digest"),
    ):
        if (
            not digest_value.startswith("sha256:")
            or len(digest_value) != len("sha256:") + 64
            or any(
                character not in "0123456789abcdef" for character in digest_value[7:]
            )
        ):
            raise _MalformedPayload(f"{context} is invalid")
    candidate = ast.ParsedSqlCandidate(
        candidate_id=str(_require_text(value["candidate_id"], "candidate ID")),
        dialect=str(_require_text(value["dialect"], "dialect")),
        candidate_digest=candidate_digest,
        source_sql_digest=source_digest,
        scopes=_decode_collection(value["scopes"], "scopes", scope),
        ctes=_decode_collection(value["ctes"], "CTEs", cte),
        table_scans=_decode_collection(value["table_scans"], "table scans", table_scan),
        cte_references=_decode_collection(
            value["cte_references"], "CTE references", cte_reference
        ),
        derived_relations=_decode_collection(
            value["derived_relations"], "derived relations", derived_relation
        ),
        expression_relations=_decode_collection(
            value["expression_relations"], "expression relations", expression_relation
        ),
        set_operations=_decode_collection(
            value["set_operations"], "set operations", set_operation
        ),
        subquery_refs=_decode_collection(
            value["subquery_refs"], "subquery references", subquery_ref
        ),
        joins=_decode_collection(value["joins"], "joins", join),
        projections=_decode_collection(value["projections"], "projections", projection),
        predicates=_decode_collection(value["predicates"], "predicates", predicate),
        aggregates=_decode_collection(value["aggregates"], "aggregates", aggregate),
        groupings=_decode_collection(value["groupings"], "groupings", grouping),
        orderings=_decode_collection(value["orderings"], "orderings", ordering),
        limits=_decode_collection(value["limits"], "limits", limit),
    )
    _validate_candidate_structure(candidate)
    return candidate


def _validate_candidate_structure(candidate: ast.ParsedSqlCandidate) -> None:
    query_facts = (*candidate.scopes, *candidate.set_operations)
    scope_ids = {scope.scope_id for scope in query_facts}
    if len(scope_ids) != len(query_facts):
        raise _MalformedPayload("candidate contains duplicate scope IDs")

    roots = [
        fact
        for fact in query_facts
        if fact.parent_scope_id is None and fact.query_role is ast.QueryRole.ROOT
    ]
    if len(roots) != 1 or any(
        (fact.parent_scope_id is None) != (fact.query_role is ast.QueryRole.ROOT)
        or (fact.parent_scope_id is not None and fact.parent_scope_id not in scope_ids)
        for fact in query_facts
    ):
        raise _MalformedPayload("candidate contains an invalid query scope tree")

    scope_links: dict[str, tuple[str, ast.QueryRole]] = {}

    def add_scope_link(
        child_scope_id: str,
        parent_scope_id: str,
        query_role: ast.QueryRole,
    ) -> None:
        if child_scope_id in scope_links:
            raise _MalformedPayload("candidate query scope has multiple owners")
        scope_links[child_scope_id] = (parent_scope_id, query_role)

    for cte in candidate.ctes:
        add_scope_link(cte.query_scope_id, cte.declaring_scope_id, ast.QueryRole.CTE)
    for derived in candidate.derived_relations:
        add_scope_link(derived.query_scope_id, derived.scope_id, ast.QueryRole.DERIVED)
    subquery_roles = {
        ast.QueryRole.SCALAR_SUBQUERY,
        ast.QueryRole.EXISTS_SUBQUERY,
        ast.QueryRole.IN_SUBQUERY,
        ast.QueryRole.QUANTIFIED_SUBQUERY,
    }
    for reference in candidate.subquery_refs:
        if reference.query_role not in subquery_roles:
            raise _MalformedPayload("candidate subquery role is invalid")
        add_scope_link(
            reference.child_scope_id, reference.scope_id, reference.query_role
        )
    for operation in candidate.set_operations:
        add_scope_link(
            operation.left_scope_id, operation.scope_id, ast.QueryRole.SET_LEFT
        )
        add_scope_link(
            operation.right_scope_id, operation.scope_id, ast.QueryRole.SET_RIGHT
        )
    actual_links = {
        fact.scope_id: (fact.parent_scope_id, fact.query_role)
        for fact in query_facts
        if fact.parent_scope_id is not None
    }
    if scope_links != actual_links:
        raise _MalformedPayload("candidate query scope links are incomplete")
    scoped_facts = (
        *candidate.table_scans,
        *candidate.cte_references,
        *candidate.derived_relations,
        *candidate.expression_relations,
        *candidate.subquery_refs,
        *candidate.joins,
        *candidate.projections,
        *candidate.predicates,
        *candidate.aggregates,
        *candidate.groupings,
        *candidate.orderings,
        *candidate.limits,
    )
    if any(fact.scope_id not in scope_ids for fact in scoped_facts):
        raise _MalformedPayload("candidate contains an unknown scope reference")

    node_ids = [scope.node_id for scope in query_facts]
    for collection in (
        candidate.ctes,
        candidate.table_scans,
        candidate.cte_references,
        candidate.derived_relations,
        candidate.expression_relations,
        candidate.subquery_refs,
        candidate.joins,
        candidate.projections,
        candidate.predicates,
        candidate.aggregates,
        candidate.groupings,
        candidate.orderings,
        candidate.limits,
    ):
        node_ids.extend(fact.node_id for fact in collection)
    node_ids.extend(
        atom.node_id for predicate in candidate.predicates for atom in predicate.atoms
    )
    if len(node_ids) != len(set(node_ids)):
        raise _MalformedPayload("candidate contains duplicate fact IDs")

    relation_facts = (
        *candidate.table_scans,
        *candidate.cte_references,
        *candidate.derived_relations,
        *candidate.expression_relations,
    )
    relation_by_id = {fact.relation_id: fact for fact in relation_facts}
    input_relation_ids = tuple(
        relation_id
        for fact in candidate.expression_relations
        for relation_id in fact.input_relation_ids
    )
    if len(input_relation_ids) != len(set(input_relation_ids)) or any(
        relation_id not in relation_by_id
        or isinstance(relation_by_id[relation_id], ast.ExpressionRelationFact)
        for relation_id in input_relation_ids
    ):
        raise _MalformedPayload("candidate transform input relation is invalid")
    input_relation_id_set = set(input_relation_ids)
    output_relation_facts = tuple(
        fact for fact in relation_facts if fact.relation_id not in input_relation_id_set
    )
    relation_ids = [fact.relation_id for fact in output_relation_facts]
    if len(relation_ids) != len(set(relation_ids)):
        raise _MalformedPayload("candidate contains duplicate relation IDs")
    aliases_by_scope: dict[str, set[str]] = {}
    for relation in output_relation_facts:
        aliases = aliases_by_scope.setdefault(relation.scope_id, set())
        if relation.source_alias in aliases:
            raise _MalformedPayload("candidate contains duplicate source aliases")
        aliases.add(relation.source_alias)

    relations_by_scope: dict[str, list[tuple[int, str]]] = {}
    for relation in output_relation_facts:
        prefix = f"{relation.scope_id}:relation:"
        index_text = relation.relation_id.removeprefix(prefix)
        if not index_text.isdecimal():
            raise _MalformedPayload("candidate contains an invalid relation ID")
        index = int(index_text)
        if relation.relation_id != f"{prefix}{index}":
            raise _MalformedPayload("candidate contains an invalid relation ID")
        relations_by_scope.setdefault(relation.scope_id, []).append(
            (index, relation.relation_id)
        )
    ordered_relations: dict[str, tuple[str, ...]] = {}
    for scope_id, relations in relations_by_scope.items():
        sorted_relations = tuple(relation_id for _, relation_id in sorted(relations))
        if tuple(index for index, _ in sorted(relations)) != tuple(
            range(len(sorted_relations))
        ):
            raise _MalformedPayload("candidate relation IDs are not contiguous")
        ordered_relations[scope_id] = sorted_relations
    for join in candidate.joins:
        relations = ordered_relations.get(join.scope_id, ())
        try:
            join_index = relations.index(join.relation_id)
        except ValueError as exc:
            raise _MalformedPayload(
                "candidate contains an unknown relation reference"
            ) from exc
        if (
            not join.left_relation_ids
            or join.left_relation_ids != relations[:join_index]
        ):
            raise _MalformedPayload("candidate join references are not canonical")
        if join.using_columns and (
            join.condition is not None
            or len(join.using_columns) != len(set(join.using_columns))
            or any(not column for column in join.using_columns)
        ):
            raise _MalformedPayload("candidate JOIN USING has an invalid shape")
    for scope_id, relations in ordered_relations.items():
        if tuple(
            join.relation_id
            for join in candidate.joins
            if join.scope_id == scope_id
        ) != relations[1:]:
            raise _MalformedPayload("candidate JOIN facts are incomplete")

    cte_ids = [fact.cte_id for fact in candidate.ctes]
    cte_id_set = set(cte_ids)
    if len(cte_ids) != len(set(cte_ids)) or any(
        reference.cte_id not in cte_id_set for reference in candidate.cte_references
    ):
        raise _MalformedPayload("candidate contains an unknown CTE reference")
    output_ids = [fact.output_id for fact in candidate.projections]
    if len(output_ids) != len(set(output_ids)):
        raise _MalformedPayload("candidate contains duplicate output IDs")

    relation_scope_by_id = {
        relation.relation_id: relation.scope_id for relation in relation_facts
    }
    query_by_scope = {fact.scope_id: fact for fact in query_facts}
    derived_by_child = {
        fact.query_scope_id: fact for fact in candidate.derived_relations
    }
    subquery_links = {
        (reference.scope_id, reference.child_scope_id)
        for reference in candidate.subquery_refs
    }
    seen_subquery_links: set[tuple[str, str]] = set()
    subquery_visibility: dict[str, set[str]] = {}
    outer_references: list[tuple[str, str, str]] = []

    def validate_expression(
        expression: ast.ExpressionFact,
        scope_id: str,
        visible_relations: set[str] | None,
    ) -> None:
        stack = [expression]
        while stack:
            node = stack.pop()
            attributes = dict(node.attributes)
            if len(attributes) != len(node.attributes):
                raise _MalformedPayload("expression contains duplicate attributes")
            if node.kind == "subquery_ref":
                referenced_scope_id = attributes.get("scope_id")
                if not isinstance(referenced_scope_id, str):
                    raise _MalformedPayload("expression subquery reference is invalid")
                link = (scope_id, referenced_scope_id)
                if set(attributes) != {"scope_id"} or link not in subquery_links:
                    raise _MalformedPayload("expression subquery reference is invalid")
                seen_subquery_links.add(link)
                if visible_relations is not None:
                    existing = subquery_visibility.setdefault(
                        referenced_scope_id, set(visible_relations)
                    )
                    existing.intersection_update(visible_relations)
            elif node.kind == "outer_column":
                if set(attributes) != {"name", "outer_scope_id", "relation_id"}:
                    raise _MalformedPayload("outer column reference is invalid")
                if not isinstance(attributes["name"], str) or not attributes["name"]:
                    raise _MalformedPayload("outer column name is invalid")
                outer_scope_id = attributes["outer_scope_id"]
                relation_id = attributes["relation_id"]
                if (
                    not isinstance(outer_scope_id, str)
                    or not isinstance(relation_id, str)
                    or relation_scope_by_id.get(relation_id) != outer_scope_id
                ):
                    raise _MalformedPayload("outer column relation is invalid")
                outer_references.append((scope_id, outer_scope_id, relation_id))
            elif node.kind == "column":
                allowed_attributes = {"name", "relation_id", "catalog", "schema", "table"}
                if not set(attributes).issubset(allowed_attributes):
                    raise _MalformedPayload("column reference is invalid")
                name = attributes.get("name")
                if not isinstance(name, str) or not name:
                    raise _MalformedPayload("column name is invalid")
                relation_id = attributes.get("relation_id")
                if relation_id is not None:
                    if (
                        set(attributes) != {"name", "relation_id"}
                        or not isinstance(relation_id, str)
                        or relation_scope_by_id.get(relation_id) != scope_id
                        or (
                            visible_relations is not None
                            and relation_id not in visible_relations
                        )
                    ):
                        raise _MalformedPayload("column relation is invalid")
                elif any(
                    not isinstance(value, str) or not value
                    for key, value in attributes.items()
                    if key != "name"
                ):
                    raise _MalformedPayload("column qualifier is invalid")
            stack.extend(child for _name, _index, child in node.children)

    joins_by_node_id = {join.node_id: join for join in candidate.joins}
    joins_by_relation_id = {join.relation_id: join for join in candidate.joins}
    output_relations: dict[str, set[str]] = {}
    for scope_id, relations in ordered_relations.items():
        visible: set[str] = set()
        for relation_id in relations:
            join = joins_by_relation_id.get(relation_id)
            if join is None or join.output_visible:
                visible.add(relation_id)
        output_relations[scope_id] = visible
    expression_facts: list[tuple[str, ast.ExpressionFact, set[str] | None]] = []
    expression_facts.extend(
        (
            scope.scope_id,
            scope.distinct,
            output_relations.get(scope.scope_id, set()),
        )
        for scope in candidate.scopes
        if scope.distinct is not None
    )
    expression_facts.extend(
        (fact.scope_id, fact.options, output_relations.get(fact.scope_id, set()))
        for facts in (candidate.scopes, candidate.set_operations, candidate.orderings)
        for fact in facts
        if fact.options is not None
    )
    for fact in candidate.table_scans:
        if fact.options is None:
            continue
        relations = ordered_relations.get(fact.scope_id, ())
        visible = (
            {fact.relation_id}
            if fact.relation_id in input_relation_id_set
            else set(relations[: relations.index(fact.relation_id) + 1])
        )
        expression_facts.append(
            (
                fact.scope_id,
                fact.options,
                visible,
            )
        )
    expression_facts.extend(
        (
            fact.scope_id,
            expression,
            {*fact.left_relation_ids, fact.relation_id},
        )
        for fact in candidate.joins
        if (expression := fact.condition) is not None
    )
    expression_facts.extend(
        (
            fact.scope_id,
            fact.options,
            {*fact.left_relation_ids, fact.relation_id},
        )
        for fact in candidate.joins
        if fact.options is not None
    )
    for fact in candidate.expression_relations:
        expression_facts.append(
            (
                fact.scope_id,
                fact.expression,
                (
                    set(fact.input_relation_ids)
                    if fact.input_relation_ids
                    else set(
                        ordered_relations[fact.scope_id][
                            : ordered_relations[fact.scope_id].index(fact.relation_id)
                        ]
                    )
                ),
            )
        )
    expression_facts.extend(
        (
            fact.scope_id,
            fact.expression,
            output_relations.get(fact.scope_id, set()),
        )
        for fact in (*candidate.projections, *candidate.orderings)
    )
    for fact in candidate.predicates:
        if fact.location is ast.PredicateLocation.JOIN_ON:
            owner = joins_by_node_id.get(fact.owner_node_id)
            visible = (
                {*owner.left_relation_ids, owner.relation_id}
                if owner is not None
                else set()
            )
        else:
            visible = output_relations.get(fact.scope_id, set())
        expression_facts.append((fact.scope_id, fact.expression, visible))
    expression_facts.extend(
        (fact.scope_id, fact.expression, output_relations.get(fact.scope_id, set()))
        for fact in candidate.aggregates
    )
    expression_facts.extend(
        (
            fact.scope_id,
            expression,
            output_relations.get(fact.scope_id, set()),
        )
        for fact in candidate.groupings
        for expression in (fact.expression,)
    )
    for expression_scope_id, expression, visible_relations in expression_facts:
        validate_expression(expression, expression_scope_id, visible_relations)
    if seen_subquery_links != subquery_links:
        raise _MalformedPayload("candidate subquery facts are incomplete")
    if set(subquery_visibility) != {
        reference.child_scope_id for reference in candidate.subquery_refs
    }:
        raise _MalformedPayload("candidate subquery visibility is incomplete")

    correlated_roles = {
        ast.QueryRole.SCALAR_SUBQUERY,
        ast.QueryRole.EXISTS_SUBQUERY,
        ast.QueryRole.IN_SUBQUERY,
        ast.QueryRole.QUANTIFIED_SUBQUERY,
    }

    def outer_path_is_legal(
        scope_id: str,
        outer_scope_id: str,
        relation_id: str,
    ) -> bool:
        current_scope_id = scope_id
        while current_scope_id != outer_scope_id:
            query = query_by_scope[current_scope_id]
            parent_scope_id = query.parent_scope_id
            if parent_scope_id is None:
                return False
            if query.query_role is ast.QueryRole.CTE:
                return False
            if query.query_role is ast.QueryRole.DERIVED:
                derived = derived_by_child.get(current_scope_id)
                if derived is None or not derived.lateral:
                    return False
                if parent_scope_id == outer_scope_id:
                    relations = ordered_relations.get(parent_scope_id, ())
                    try:
                        return relations.index(relation_id) < relations.index(
                            derived.relation_id
                        )
                    except ValueError:
                        return False
            elif query.query_role in correlated_roles:
                if parent_scope_id == outer_scope_id:
                    return relation_id in subquery_visibility.get(
                        current_scope_id, set()
                    )
            elif query.query_role not in {
                ast.QueryRole.SET_LEFT,
                ast.QueryRole.SET_RIGHT,
            }:
                return False
            current_scope_id = parent_scope_id
        return False

    if any(
        not outer_path_is_legal(scope_id, outer_scope_id, relation_id)
        for scope_id, outer_scope_id, relation_id in outer_references
    ):
        raise _MalformedPayload("outer column crosses an illegal scope boundary")

    join_on_owners: set[str] = set()
    for predicate in candidate.predicates:
        if predicate.location is ast.PredicateLocation.JOIN_ON:
            owner = joins_by_node_id.get(predicate.owner_node_id)
            if (
                owner is None
                or owner.scope_id != predicate.scope_id
                or owner.condition is None
                or owner.node_id in join_on_owners
            ):
                raise _MalformedPayload("candidate contains an invalid JOIN ON owner")
            join_on_owners.add(owner.node_id)
        elif predicate.owner_node_id is not None:
            raise _MalformedPayload("candidate WHERE/HAVING owner must be empty")
    if join_on_owners != {
        join.node_id for join in candidate.joins if join.condition is not None
    }:
        raise _MalformedPayload("candidate JOIN ON facts are incomplete")


_ERROR_MESSAGES = {
    ast.SqlAstErrorCode.PARSE_FAILED: "SQL parser rejected the candidate",
    ast.SqlAstErrorCode.MULTI_STATEMENT: "SQL candidate must contain one statement",
    ast.SqlAstErrorCode.SHAPE_UNSUPPORTED: "SQL candidate shape is unsupported",
    ast.SqlAstErrorCode.PARSE_TIMEOUT: "SQL parsing exceeded its wall-time budget",
    ast.SqlAstErrorCode.DIALECT_UNSUPPORTED: "SQL dialect is unsupported",
}


def _decode_envelope(
    raw: bytes,
    *,
    limits: ast.AstLimits,
) -> ast.ParsedSqlCandidate:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MalformedPayload("worker payload is not valid JSON") from exc
    if (
        not isinstance(envelope, dict)
        or type(envelope.get("version")) is not int
        or envelope["version"] != _WIRE_VERSION
    ):
        raise _MalformedPayload("worker payload version is invalid")
    status = envelope.get("status")
    if status == "error" and set(envelope) == {"version", "status", "code"}:
        try:
            code = ast.SqlAstErrorCode(envelope["code"])
        except (TypeError, ValueError) as exc:
            raise _MalformedPayload("worker error code is invalid") from exc
        raise ast.SqlAstError(code, _ERROR_MESSAGES[code])
    if status != "ok" or set(envelope) != {"version", "status", "candidate"}:
        raise _MalformedPayload("worker envelope shape is invalid")
    return _decode_candidate(envelope["candidate"], limits=limits)


def _stop_and_reap(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()


def _wait_for_process_exit(
    process: multiprocessing.Process,
    *,
    deadline: float,
) -> bool:
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        process.join(min(remaining, _MAX_JOIN_SLICE_SECONDS))
    process.join()
    return True


def parse_candidate_isolated(
    sql: str,
    dialect: str,
    candidate_id: str,
    *,
    max_ast_nodes: Any,
    max_ast_depth: Any,
) -> ast.ParsedSqlCandidate:
    _validate_input_size(sql)
    timeout = _parse_timeout_seconds()
    limits = _validated_ast_limits(max_ast_nodes, max_ast_depth)
    deadline = time.monotonic() + timeout
    context = multiprocessing.get_context("fork")
    result_buffer = mmap.mmap(-1, _RESULT_BUFFER_BYTES, access=mmap.ACCESS_WRITE)
    process = context.Process(
        target=_parse_worker,
        args=(result_buffer, sql, dialect, candidate_id, limits),
        daemon=True,
        name="text-to-sql-ast-parser",
    )
    started = False
    try:
        process.start()
        started = True
        if (
            not _wait_for_process_exit(process, deadline=deadline)
            or time.monotonic() > deadline
        ):
            _stop_and_reap(process)
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_TIMEOUT,
                "SQL parsing exceeded its wall-time budget",
            )
        try:
            raw = _read_published_result(result_buffer)
            if time.monotonic() > deadline:
                raise ast.SqlAstError(
                    ast.SqlAstErrorCode.PARSE_TIMEOUT,
                    "SQL parsing exceeded its wall-time budget",
                )
            candidate = _decode_envelope(raw, limits=limits)
        except _MalformedPayload as exc:
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_FAILED,
                "SQL parser worker returned an invalid result",
            ) from exc
        if candidate.candidate_id != candidate_id or candidate.dialect != dialect:
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_FAILED,
                "SQL parser worker result identity does not match the request",
            )
        if candidate.source_sql_digest != source_sql_digest(
            sql
        ) or candidate.candidate_digest != semantic_candidate_digest(candidate):
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_FAILED,
                "SQL parser worker result does not match the input SQL",
            )
        if time.monotonic() > deadline:
            raise ast.SqlAstError(
                ast.SqlAstErrorCode.PARSE_TIMEOUT,
                "SQL parsing exceeded its wall-time budget",
            )
        return candidate
    finally:
        try:
            if started:
                if process.is_alive():
                    _stop_and_reap(process)
                elif process.exitcode is None:
                    process.join()
                process.close()
        finally:
            result_buffer.close()


__all__ = ["parse_candidate_isolated"]
