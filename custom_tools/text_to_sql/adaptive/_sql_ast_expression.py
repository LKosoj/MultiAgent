"""Канонические immutable expression facts для W5-02."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from sqlglot import expressions as exp

from . import _sql_ast_models as ast


def unsupported(message: str) -> ast.SqlAstError:
    return ast.SqlAstError(ast.SqlAstErrorCode.SHAPE_UNSUPPORTED, message)


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__}")


def digest(value: Any) -> str:
    payload = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def node_id(kind: str, occurrence: str, payload: Any) -> str:
    return f"ast:{kind}:{digest((occurrence, payload))[:24]}"


def _canonical_scalar(value: Any) -> ast.CanonicalScalar:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        enum_value = value.value
        if isinstance(enum_value, (str, int, float, bool)) or enum_value is None:
            return enum_value
        return str(enum_value)
    raise unsupported(f"unsupported scalar AST attribute type {type(value).__name__}")


def canonical_expression(
    node: exp.Expression,
    *,
    relation_aliases: dict[str, str],
    output_aliases: dict[str, str],
    outer_relation_aliases: dict[str, tuple[str, str]] | None = None,
    forbidden_outer_aliases: frozenset[str] = frozenset(),
    subquery_scope_ids: dict[int, str] | None = None,
    allow_output_ref: bool = False,
    allow_ordinal_ref: bool = False,
) -> ast.ExpressionFact:
    outer_aliases = outer_relation_aliases or {}
    query_scopes = subquery_scope_ids or {}

    def canonical(child: Any, *, ordinal: bool = False) -> ast.ExpressionFact:
        if not isinstance(child, exp.Expression):
            raise unsupported("expression child is missing")
        return canonical_expression(
            child,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
            outer_relation_aliases=outer_aliases,
            forbidden_outer_aliases=forbidden_outer_aliases,
            subquery_scope_ids=query_scopes,
            allow_output_ref=allow_output_ref,
            allow_ordinal_ref=ordinal,
        )

    referenced_scope_id = query_scopes.get(id(node))
    if referenced_scope_id is not None:
        return ast.ExpressionFact(
            kind="subquery_ref",
            attributes=(("scope_id", referenced_scope_id),),
        )

    if isinstance(node, (exp.Paren, exp.Alias)):
        return canonical(node.this, ordinal=allow_ordinal_ref)

    if allow_ordinal_ref and isinstance(node, exp.Literal) and not node.is_string:
        try:
            ordinal = int(node.this)
        except (TypeError, ValueError):
            ordinal = 0
        if ordinal > 0 and str(ordinal) == str(node.this):
            return ast.ExpressionFact(
                kind="ordinal_ref",
                attributes=(("ordinal", ordinal),),
            )

    if isinstance(node, exp.Column):
        name = node.name
        if not name:
            raise unsupported("column reference has no name")
        raw_table = node.table or None
        raw_schema = node.db or None
        raw_catalog = node.catalog or None
        if (
            allow_output_ref
            and raw_table is None
            and raw_schema is None
            and raw_catalog is None
            and name in output_aliases
        ):
            return ast.ExpressionFact(
                kind="output_ref",
                attributes=(("output_id", output_aliases[name]),),
            )

        attributes: list[tuple[str, ast.CanonicalScalar]] = [("name", name)]
        relation_id = _column_relation_id(
            node,
            relation_aliases=relation_aliases,
            table=raw_table,
            schema=raw_schema,
            catalog=raw_catalog,
        )
        if (
            relation_id is None
            and raw_table is not None
            and raw_schema is None
            and raw_catalog is None
        ):
            outer_relation = outer_aliases.get(raw_table)
            if outer_relation is not None:
                outer_scope_id, outer_relation_id = outer_relation
                return ast.ExpressionFact(
                    kind="outer_column",
                    attributes=(
                        ("name", name),
                        ("outer_scope_id", outer_scope_id),
                        ("relation_id", outer_relation_id),
                    ),
                )
            if raw_table in forbidden_outer_aliases:
                raise unsupported("relation is not visible from this query scope")
        if (
            relation_id is None
            and raw_table is None
            and not relation_aliases
            and outer_aliases
        ):
            raise unsupported("outer column references must be qualified")
        if relation_id is not None:
            attributes.append(("relation_id", relation_id))
        else:
            attributes.extend(
                (key, value)
                for key, value in (
                    ("catalog", raw_catalog),
                    ("schema", raw_schema),
                    ("table", raw_table),
                )
                if value
            )
        return ast.ExpressionFact(kind="column", attributes=tuple(attributes))

    if isinstance(node, exp.Identifier):
        return ast.ExpressionFact(
            kind="identifier",
            attributes=(("name", node.name),),
        )
    if isinstance(node, exp.Literal):
        return ast.ExpressionFact(
            kind="literal",
            attributes=(
                ("is_string", bool(node.is_string)),
                ("value", str(node.this)),
            ),
        )
    if isinstance(node, exp.Null):
        return ast.ExpressionFact(kind="null")
    if isinstance(node, exp.Boolean):
        return ast.ExpressionFact(
            kind="boolean",
            attributes=(("value", bool(node.this)),),
        )
    if isinstance(node, exp.Star):
        return ast.ExpressionFact(kind="star")

    attributes: list[tuple[str, ast.CanonicalScalar]] = []
    children: list[tuple[str, int, ast.ExpressionFact]] = []
    for argument_name in sorted(node.args):
        value = node.args[argument_name]
        if value is None or argument_name == "alias":
            continue
        ordinal = allow_ordinal_ref and isinstance(node, exp.Group) and argument_name == "expressions"
        if isinstance(value, exp.Expression):
            children.append((argument_name, 0, canonical(value, ordinal=ordinal)))
            continue
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if not isinstance(item, exp.Expression):
                    raise unsupported(
                        f"unsupported {node.key}.{argument_name} list item"
                    )
                children.append((argument_name, index, canonical(item, ordinal=ordinal)))
            continue
        attributes.append((argument_name, _canonical_scalar(value)))

    if isinstance(node, exp.Anonymous):
        attributes = [
            (
                name,
                value.lower() if name == "this" and isinstance(value, str) else value,
            )
            for name, value in attributes
        ]
    return ast.ExpressionFact(
        kind=node.key or type(node).__name__.lower(),
        attributes=tuple(attributes),
        children=tuple(children),
    )


def _column_relation_id(
    node: exp.Column,
    *,
    relation_aliases: dict[str, str],
    table: str | None,
    schema: str | None,
    catalog: str | None,
) -> str | None:
    relation_ids = set(relation_aliases.values())
    if table is None:
        return next(iter(relation_ids)) if len(relation_ids) == 1 else None
    if schema is None and catalog is None:
        return relation_aliases.get(table)

    select = node.find_ancestor(exp.Select)
    if select is None:
        return None
    from_clause = select.args.get("from")
    relations = [from_clause.this] if isinstance(from_clause, exp.From) else []
    relations.extend(
        join.this
        for join in select.args.get("joins") or ()
        if isinstance(join, exp.Join)
    )
    for relation in relations:
        if (
            not isinstance(relation, exp.Table)
            or relation.alias
            or relation.name != table
            or (schema is not None and relation.db != schema)
            or (catalog is not None and relation.catalog != catalog)
        ):
            continue
        return relation_aliases.get(relation.name)
    return None


def predicate_atoms(
    expression: ast.ExpressionFact,
    *,
    predicate_occurrence: str,
) -> tuple[ast.PredicateAtomFact, ...]:
    atoms: list[ast.PredicateAtomFact] = []

    def visit(node: ast.ExpressionFact, path: tuple[str, ...]) -> None:
        if node.kind in {"and", "or"}:
            branches = [
                (argument, ordinal, child)
                for argument, ordinal, child in node.children
                if argument in {"this", "expression"}
            ]
            if len(branches) != 2:
                raise unsupported(f"{node.kind} predicate must have two branches")
            for argument, ordinal, child in branches:
                visit(child, (*path, f"{node.kind}:{argument}:{ordinal}"))
            return
        atom_id = node_id(
            "predicate_atom",
            f"{predicate_occurrence}:{'/'.join(path) or 'root'}",
            node,
        )
        atoms.append(ast.PredicateAtomFact(atom_id, path, node))

    visit(expression, ())
    return tuple(atoms)


def aggregate_is_distinct(node: exp.AggFunc) -> bool:
    return any(
        isinstance(value, exp.Distinct)
        or (
            isinstance(value, (list, tuple))
            and any(isinstance(item, exp.Distinct) for item in value)
        )
        for value in node.args.values()
    )


def non_negative_integer(node: Any, *, context: str) -> int:
    if not isinstance(node, exp.Literal) or node.is_string:
        raise unsupported(f"{context} must be a non-negative integer literal")
    try:
        value = int(node.this)
    except (TypeError, ValueError) as exc:
        raise unsupported(f"{context} must be a non-negative integer literal") from exc
    if value < 0 or str(value) != str(node.this):
        raise unsupported(f"{context} must be a non-negative integer literal")
    return value


def check_ast_bounds(root: exp.Expression, *, limits: ast.AstLimits) -> None:
    node_count = 0
    stack: list[tuple[exp.Expression, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > limits.max_nodes:
            raise unsupported(f"SQL AST exceeds {limits.max_nodes} nodes")
        if depth > limits.max_depth:
            raise unsupported(f"SQL AST exceeds depth {limits.max_depth}")
        stack.extend((child, depth + 1) for child in node.iter_expressions())


__all__ = [
    "aggregate_is_distinct",
    "canonical_expression",
    "check_ast_bounds",
    "digest",
    "node_id",
    "non_negative_integer",
    "predicate_atoms",
    "unsupported",
]
