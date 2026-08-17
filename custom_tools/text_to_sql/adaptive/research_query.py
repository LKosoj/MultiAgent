"""Pure fail-closed admission for model-authored bounded research SELECTs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from typing import Annotated, TypeAlias

from pydantic import Field, ValidationError, field_validator, model_validator
from sqlglot import Dialect, exp, parse
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

from ..utils import get_table_columns
from .models import QueryProbeRef, StrictModel
from .serialization import canonical_digest


JsonScalar: TypeAlias = str | int | float | bool | None
ResearchSql = Annotated[str, Field(min_length=1, max_length=50_000)]

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_OUTPUT_COLUMNS = 20
_SYSTEM_SCHEMAS = frozenset(
    {"information_schema", "mysql", "performance_schema", "pg_catalog", "sys"}
)
_SYSTEM_TABLES = frozenset(
    {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
)
_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
_MUTATIONS = (
    exp.Alter,
    exp.Command,
    exp.Commit,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Merge,
    exp.Rollback,
    exp.Transaction,
    exp.Update,
)


class RawResearchQuery(StrictModel):
    """The complete public input: SQL text and immutable bound parameters."""

    sql: ResearchSql
    parameters: tuple[JsonScalar, ...] = ()

    @field_validator("parameters", mode="before")
    @classmethod
    def require_immutable_parameters(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("parameters must be an immutable tuple")
        return value

    @model_validator(mode="after")
    def validate_public_input(self) -> RawResearchQuery:
        if not self.sql.strip():
            raise ValueError("sql must contain a statement")
        for value in self.parameters:
            if value is None or type(value) in {str, int, bool}:
                continue
            if type(value) is float and math.isfinite(value):
                continue
            raise ValueError("parameters must contain exact finite JSON scalars")
        return self


class ResearchQueryAdmissionError(ValueError):
    """A raw query cannot be proven safe, scoped, bounded, and deterministic."""

    def __init__(self, failure_code: str, summary: str) -> None:
        self.failure_code = failure_code
        self.summary = summary
        super().__init__(summary)


@dataclass(frozen=True, slots=True)
class ResearchQueryIdentity:
    """Schema-free canonical identity used to build the claimed action."""

    target: QueryProbeRef
    normalized_sql: str
    action_parameters: tuple[tuple[str, JsonScalar], ...]


@dataclass(frozen=True, slots=True)
class ResearchQueryAdmission:
    """Trusted values derived from one admitted AST and scoped schema."""

    target: QueryProbeRef
    normalized_sql: str
    output_columns: tuple[str, ...]
    row_limit: int
    action_parameters: tuple[tuple[str, JsonScalar], ...]


@dataclass(frozen=True, slots=True)
class _DerivedResearchQuery:
    checked: RawResearchQuery
    tree: exp.Expression
    identity: ResearchQueryIdentity


@dataclass(frozen=True, slots=True)
class _Source:
    columns: tuple[str, ...]


def admit_research_query(
    query: RawResearchQuery,
    *,
    schema: Mapping[str, object],
    dialect: str,
    namespace: str,
    schema_namespace_version: str,
    maximum_row_limit: int,
) -> ResearchQueryAdmission:
    """Validate and normalize one SELECT without I/O or mutable state."""

    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a trusted mapping")
    if type(maximum_row_limit) is not int or maximum_row_limit <= 0:
        raise ValueError("maximum_row_limit must be a positive integer")
    derived = _derive_research_query(
        query,
        dialect,
        namespace,
        schema_namespace_version,
    )
    tree = _require_select(derived.tree)
    row_limit = _literal_row_limit(tree, maximum_row_limit)
    _validate_placeholders(derived.checked, dialect.casefold(), tree)
    _validate_closed_ast(tree)
    output_columns = _resolve_scopes(tree, schema, dialect.casefold())
    return ResearchQueryAdmission(
        target=derived.identity.target,
        normalized_sql=_normalized_execution_sql(
            tree,
            dialect.casefold(),
        ),
        output_columns=output_columns,
        row_limit=row_limit,
        action_parameters=derived.identity.action_parameters,
    )


def derive_research_query_identity(
    query: RawResearchQuery,
    *,
    dialect: str,
    namespace: str,
    schema_namespace_version: str,
) -> ResearchQueryIdentity:
    """Derive a canonical preclaim target without loading database schema."""

    return _derive_research_query(
        query,
        dialect,
        namespace,
        schema_namespace_version,
    ).identity


def _derive_research_query(
    query: RawResearchQuery,
    dialect: str,
    namespace: str,
    schema_namespace_version: str,
) -> _DerivedResearchQuery:
    checked = _revalidate_query(query)
    _validate_trusted_inputs(
        dialect,
        namespace,
        schema_namespace_version,
    )
    normalized_dialect = dialect.casefold()
    tree = _parse_one_statement(checked.sql, normalized_dialect)
    normalized_sql = tree.sql(
        dialect=normalized_dialect,
        comments=False,
        normalize=True,
        pretty=False,
    )
    identity_digest = canonical_digest(
        {
            "dialect": normalized_dialect,
            "identity_version": 3,
            "normalized_ast": normalized_sql,
            "statement_kind": tree.key,
            "schema_namespace_version": schema_namespace_version,
        }
    )
    return _DerivedResearchQuery(
        checked=checked,
        tree=tree,
        identity=ResearchQueryIdentity(
            target=QueryProbeRef(
                probe_id=f"research:{identity_digest.split(':', 1)[1]}",
                namespace=namespace,
            ),
            normalized_sql=normalized_sql,
            action_parameters=_action_parameters(checked.parameters),
        ),
    )


def research_query_action_parameters(
    query: RawResearchQuery,
) -> tuple[tuple[str, JsonScalar], ...]:
    """Return the only action parameters allowed for a public raw query."""

    return _action_parameters(_revalidate_query(query).parameters)


def dialect_for_plugin(plugin: object) -> str:
    """Read sqlglot's dialect name from the already selected trusted plugin."""

    dialect = getattr(plugin, "sqlglot_dialect", None)
    if not isinstance(dialect, str) or not dialect:
        dialect = getattr(plugin, "dialect", None)
    if not isinstance(dialect, str) or not dialect:
        raise ResearchQueryAdmissionError(
            "research_query_dialect",
            "database plugin does not declare a SQL dialect",
        )
    normalized = {"impala": "hive", "sapiq": "tsql"}.get(
        dialect.casefold(),
        dialect.casefold(),
    )
    try:
        Dialect.get_or_raise(normalized)
    except ValueError:
        raise ResearchQueryAdmissionError(
            "research_query_dialect",
            "database plugin declares an unsupported SQL dialect",
        ) from None
    return normalized


def _revalidate_query(query: RawResearchQuery) -> RawResearchQuery:
    if not isinstance(query, RawResearchQuery):
        raise TypeError("query must be RawResearchQuery")
    try:
        return RawResearchQuery.model_validate(
            query.model_dump(mode="python", round_trip=True)
        )
    except ValidationError:
        raise TypeError("query violates the raw research query contract") from None


def _validate_trusted_inputs(
    dialect: str,
    namespace: str,
    schema_namespace_version: str,
) -> None:
    if type(dialect) is not str or not dialect:
        raise TypeError("dialect must be trusted non-empty text")
    try:
        Dialect.get_or_raise(dialect.casefold())
    except ValueError:
        raise ResearchQueryAdmissionError(
            "research_query_dialect",
            "SQL dialect is unsupported",
        ) from None
    if type(namespace) is not str or not namespace:
        raise TypeError("namespace must be trusted non-empty text")
    if type(schema_namespace_version) is not str or not _SHA256_RE.fullmatch(
        schema_namespace_version
    ):
        raise ValueError("schema_namespace_version must be an exact sha256 digest")


def _parse_one_statement(sql: str, dialect: str) -> exp.Expression:
    try:
        statements = parse(sql, read=dialect)
    except (ParseError, TokenError, ValueError):
        raise ResearchQueryAdmissionError(
            "research_query_parse",
            "research SQL cannot be parsed in the trusted dialect",
        ) from None
    if len(statements) != 1:
        raise ResearchQueryAdmissionError(
            "research_query_statement_count",
            "research SQL must contain exactly one statement",
        )
    statement = statements[0]
    if not isinstance(statement, exp.Expression):
        raise ResearchQueryAdmissionError(
            "research_query_parse",
            "research SQL did not produce one statement",
        )
    return statement


def _require_select(statement: exp.Expression) -> exp.Select:
    if not isinstance(statement, exp.Select):
        raise ResearchQueryAdmissionError(
            "research_query_not_select",
            "research SQL must be one SELECT statement",
        )
    return statement


def _literal_row_limit(tree: exp.Select, maximum: int) -> int:
    outer_limit = _literal_select_limit(tree, maximum, required=True)
    assert outer_limit is not None
    for select in tree.find_all(exp.Select):
        if select is not tree:
            _literal_select_limit(select, maximum, required=False)
    return outer_limit


def _literal_select_limit(
    select: exp.Select,
    maximum: int,
    *,
    required: bool,
) -> int | None:
    limit = select.args.get("limit")
    offset = select.args.get("offset")
    expression = limit.expression if isinstance(limit, exp.Limit) else None
    if offset is not None:
        raise ResearchQueryAdmissionError(
            "research_query_limit",
            "research SQL does not admit OFFSET",
        )
    if limit is None and not required:
        return None
    if not isinstance(expression, exp.Literal) or expression.is_string:
        raise ResearchQueryAdmissionError(
            "research_query_limit",
            "research SQL requires one positive literal LIMIT",
        )
    try:
        value = int(expression.this)
    except (TypeError, ValueError):
        value = 0
    if str(value) != expression.this or not 1 <= value <= maximum:
        raise ResearchQueryAdmissionError(
            "research_query_limit",
            "research SQL LIMIT exceeds its closed policy bound",
        )
    return value


def _validate_placeholders(
    query: RawResearchQuery,
    dialect: str,
    tree: exp.Select,
) -> None:
    try:
        tokens = Dialect.get_or_raise(dialect).tokenize(query.sql)
    except (ParseError, TokenError, ValueError):
        raise ResearchQueryAdmissionError(
            "research_query_parse",
            "research SQL tokenization failed",
        ) from None
    token_count = sum(token.token_type is TokenType.PLACEHOLDER for token in tokens)
    placeholders = tuple(tree.find_all(exp.Placeholder))
    if (
        any(placeholder.this is not None for placeholder in placeholders)
        or tree.find(exp.Parameter) is not None
        or token_count != len(placeholders)
        or token_count != len(query.parameters)
    ):
        raise ResearchQueryAdmissionError(
            "research_query_parameters",
            "research SQL requires exact anonymous placeholders",
        )


def _validate_closed_ast(tree: exp.Select) -> None:
    if any(
        not (
            isinstance(star.parent, exp.Count)
            and star.parent.this is star
            and all(value is None for value in star.args.values())
        )
        for star in tree.find_all(exp.Star)
    ):
        raise ResearchQueryAdmissionError(
            "research_query_star",
            "research SQL star is allowed only as bare COUNT(*)",
        )
    if tree.find(*_SET_OPERATIONS) is not None:
        raise ResearchQueryAdmissionError(
            "research_query_not_select",
            "set operations are outside the research SQL contract",
        )
    if tree.find(*_MUTATIONS) is not None or tree.find(exp.Into, exp.Lock) is not None:
        raise ResearchQueryAdmissionError(
            "research_query_not_select",
            "mutating and locking SELECT forms are not admitted",
        )
    if tree.find(exp.TableSample, exp.Unnest, exp.Lateral, exp.Pivot) is not None:
        raise ResearchQueryAdmissionError(
            "research_query_row_source",
            "sampled, generated, and lateral row sources are not admitted",
        )
    with_clause = tree.args.get("with")
    if isinstance(with_clause, exp.With) and with_clause.args.get("recursive"):
        raise ResearchQueryAdmissionError(
            "research_query_row_source",
            "recursive CTEs are outside the research SQL contract",
        )
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise ResearchQueryAdmissionError(
                "research_query_row_source",
                "table, file, and network functions are not admitted",
            )

def _resolve_scopes(
    tree: exp.Select,
    schema: Mapping[str, object],
    dialect: str,
) -> tuple[str, ...]:
    scope_outputs: dict[int, tuple[str, ...]] = {}
    try:
        scopes = tuple(traverse_scope(tree))
    except Exception:
        raise ResearchQueryAdmissionError(
            "research_query_row_source",
            "research SQL row sources are ambiguous",
        ) from None
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            raise ResearchQueryAdmissionError(
                "research_query_not_select",
                "every research SQL scope must be a SELECT",
            )
        if scope.outer_columns:
            raise ResearchQueryAdmissionError(
                "research_query_output",
                "derived column lists are outside the research SQL contract",
            )
        sources = _scope_sources(scope, schema, scope_outputs)
        outputs = _output_columns(
            scope.expression,
            nested_non_row_source=scope.is_subquery,
            dialect=dialect,
        )
        _resolve_columns(scope, sources, outputs)
        scope_outputs[id(scope)] = outputs
    root_outputs = scope_outputs.get(id(scopes[-1])) if scopes else None
    if root_outputs is None:
        raise ResearchQueryAdmissionError(
            "research_query_parse",
            "research SQL did not produce one root scope",
        )
    return root_outputs


def _scope_sources(
    scope: Scope,
    schema: Mapping[str, object],
    scope_outputs: Mapping[int, tuple[str, ...]],
) -> dict[str, _Source]:
    result: dict[str, _Source] = {}
    try:
        selected_sources = scope.selected_sources
    except Exception:
        raise ResearchQueryAdmissionError(
            "research_query_row_source",
            "research SQL row source aliases must be unique",
        ) from None
    for alias, (node, source) in selected_sources.items():
        key = _source_alias_key(alias, node)
        if key in result:
            raise ResearchQueryAdmissionError(
                "research_query_row_source",
                "research SQL row source aliases must be unique",
            )
        if isinstance(source, exp.Table):
            result[key] = _physical_source(source, schema)
        elif isinstance(source, Scope):
            columns = scope_outputs.get(id(source))
            if columns is None:
                raise ResearchQueryAdmissionError(
                    "research_query_row_source",
                    "derived research SQL source is unresolved",
                )
            result[key] = _Source(columns)
        else:
            raise ResearchQueryAdmissionError(
                "research_query_row_source",
                "research SQL contains an unsupported row source",
            )
    return result


def _source_alias_key(alias: str, node: exp.Expression) -> str:
    table_alias = node.args.get("alias")
    if isinstance(table_alias, exp.TableAlias) and isinstance(
        table_alias.this,
        exp.Identifier,
    ):
        return _canonical_identifier(table_alias.this)
    if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
        return _canonical_identifier(node.this)
    return alias.casefold()


def _physical_source(table: exp.Table, schema: Mapping[str, object]) -> _Source:
    name = table.name
    database = table.db
    catalog = table.catalog
    if (
        catalog
        or database.casefold() in _SYSTEM_SCHEMAS
        or name.casefold() in _SYSTEM_TABLES
    ):
        raise ResearchQueryAdmissionError(
            "research_query_scope",
            "research SQL references a system or out-of-scope catalog",
        )
    candidates = []
    for table_name, body in schema.items():
        if type(table_name) is not str or not isinstance(body, Mapping):
            continue
        parts = table_name.split(".")
        matches = (
            len(parts) == 2
            and _identifier_matches(table.args.get("db"), parts[0])
            and _identifier_matches(table.this, parts[1])
            if database
            else _identifier_matches(table.this, parts[-1])
        )
        if matches:
            candidates.append((table_name, body))
    if not candidates:
        code = "research_query_scope" if database else "research_query_table"
        raise ResearchQueryAdmissionError(
            code,
            "research SQL table is outside the trusted scoped schema",
        )
    if len(candidates) != 1:
        raise ResearchQueryAdmissionError(
            "research_query_table",
            "research SQL table reference is ambiguous",
        )
    canonical_name, body = candidates[0]
    canonical_parts = canonical_name.split(".")
    if (
        any(part.casefold() in _SYSTEM_SCHEMAS for part in canonical_parts[:-1])
        or canonical_parts[-1].casefold() in _SYSTEM_TABLES
    ):
        raise ResearchQueryAdmissionError(
            "research_query_scope",
            "research SQL references a system or out-of-scope catalog",
        )
    columns = get_table_columns(body)
    if not columns:
        raise ResearchQueryAdmissionError(
            "research_query_table",
            "research SQL table metadata is unavailable",
        )
    return _Source(tuple(columns))


def _output_columns(
    select: exp.Select,
    *,
    nested_non_row_source: bool,
    dialect: str,
) -> tuple[str, ...]:
    if not 1 <= len(select.expressions) <= _MAX_OUTPUT_COLUMNS:
        raise ResearchQueryAdmissionError(
            "research_query_output",
            "research SQL output exceeds its closed column bound",
        )
    explicit_outputs = {
        _canonical_identifier(identifier)
        for projection in select.expressions
        for identifier in (
            projection.args.get("alias")
            if isinstance(projection, exp.Alias)
            else projection.this
            if isinstance(projection, exp.Column)
            else None,
        )
        if isinstance(identifier, exp.Identifier) and identifier.this
    }
    outputs = []
    output_keys = set()
    for index, projection in enumerate(select.expressions, start=1):
        if isinstance(projection, exp.Alias):
            identifier = projection.args.get("alias")
        elif isinstance(projection, exp.Column):
            identifier = projection.this
        else:
            identifier = None
        if not isinstance(identifier, exp.Identifier) or not identifier.this:
            if not nested_non_row_source and not isinstance(projection, exp.Window):
                raise ResearchQueryAdmissionError(
                    "research_query_output",
                    "computed research SQL projections require explicit aliases",
                )
            output = _expression_sql(projection, dialect)
            if output in explicit_outputs or output.casefold() in output_keys:
                raise ResearchQueryAdmissionError(
                    "research_query_output",
                    "research SQL output aliases must be unique",
                )
            outputs.append(output)
            output_keys.add(output.casefold())
            continue
        output = identifier.this if isinstance(projection, exp.Column) else _canonical_identifier(identifier)
        outputs.append(output)
        output_keys.add(_canonical_identifier(identifier))
    if len(outputs) != len(output_keys):
        raise ResearchQueryAdmissionError(
            "research_query_output",
            "research SQL output aliases must be unique",
        )
    return tuple(outputs)


def _normalized_execution_sql(tree: exp.Select, dialect: str) -> str:
    return tree.sql(
        dialect=dialect,
        comments=False,
        normalize=True,
        pretty=False,
    )


def _resolve_columns(
    scope: Scope,
    sources: Mapping[str, _Source],
    outputs: tuple[str, ...],
) -> None:
    for column in scope.columns:
        if column.db or column.catalog:
            raise ResearchQueryAdmissionError(
                "research_query_column",
                "research SQL column uses an unsupported database or catalog qualifier",
            )
        if _is_order_alias(column, scope.expression, set(outputs)):
            continue
        identifier = column.this
        if not isinstance(identifier, exp.Identifier):
            raise ResearchQueryAdmissionError(
                "research_query_column",
                "research SQL column name is invalid",
            )
        qualifier = _canonical_optional_identifier(column.args.get("table"))
        if qualifier:
            source = sources.get(qualifier)
            if source is None:
                raise ResearchQueryAdmissionError(
                    "research_query_column",
                    "research SQL column qualifier is not a row source",
                )
            actual = _matching_column(identifier, source.columns)
            if actual is None:
                raise ResearchQueryAdmissionError(
                    "research_query_column",
                    "research SQL column is absent from its row source",
                )
            continue
        matches = [
            (alias, actual)
            for alias, source in sources.items()
            if (actual := _matching_column(identifier, source.columns)) is not None
        ]
        if not matches:
            raise ResearchQueryAdmissionError(
                "research_query_column",
                "research SQL column is absent from the scoped row sources",
            )
        if len(matches) != 1:
            raise ResearchQueryAdmissionError(
                "research_query_column_ambiguous",
                "unqualified research SQL column is ambiguous",
            )


def _matching_column(
    identifier: exp.Identifier | str,
    columns: tuple[str, ...],
) -> str | None:
    matches = [column for column in columns if _identifier_matches(identifier, column)]
    return matches[0] if len(matches) == 1 else None


def _identifier_matches(identifier: object, actual: str) -> bool:
    if isinstance(identifier, str):
        return identifier.casefold() == actual.casefold()
    if not isinstance(identifier, exp.Identifier):
        return False
    if identifier.args.get("quoted"):
        return identifier.this == actual
    return identifier.this.casefold() == actual.casefold()


def _canonical_identifier(identifier: exp.Identifier) -> str:
    return (
        identifier.this if identifier.args.get("quoted") else identifier.this.casefold()
    )


def _canonical_optional_identifier(identifier: object) -> str:
    return (
        _canonical_identifier(identifier)
        if isinstance(identifier, exp.Identifier)
        else ""
    )


def _is_order_alias(
    column: exp.Column,
    select: exp.Select,
    output_names: set[str],
) -> bool:
    order = select.args.get("order")
    return (
        isinstance(order, exp.Order)
        and column.find_ancestor(exp.Order) is order
        and isinstance(column.this, exp.Identifier)
        and _canonical_identifier(column.this) in output_names
    )


def _expression_sql(expression: exp.Expression, dialect: str) -> str:
    return expression.sql(
        dialect=dialect,
        comments=False,
        normalize=True,
        pretty=False,
    )

def _action_parameters(
    parameters: tuple[JsonScalar, ...],
) -> tuple[tuple[str, JsonScalar], ...]:
    return tuple(
        (f"parameter_{index:03d}", value) for index, value in enumerate(parameters)
    )


__all__ = [
    "RawResearchQuery",
    "ResearchQueryAdmission",
    "ResearchQueryAdmissionError",
    "ResearchQueryIdentity",
    "admit_research_query",
    "derive_research_query_identity",
    "dialect_for_plugin",
    "research_query_action_parameters",
]
