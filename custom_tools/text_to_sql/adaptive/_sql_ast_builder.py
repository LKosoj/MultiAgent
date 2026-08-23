"""Проверка формы SELECT и построение W5-02 AST facts."""

from __future__ import annotations

from dataclasses import replace

import sqlglot
from sqlglot import Dialect
from sqlglot import expressions as exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..dialects import SQLGLOT_DIALECT_MAPPING
from . import _sql_ast_models as ast
from ._sql_ast_expression import (
    aggregate_is_distinct,
    canonical_expression,
    check_ast_bounds,
    node_id,
    non_negative_integer,
    predicate_atoms,
    unsupported,
)
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest


# Загружаем разрешённые реализации диалектов до fork. Иначе дочерний процесс
# может впервые импортировать их уже после того, как вызывающий код временно
# запретил динамические Python-импорты для проверки отсутствия SQL-исполнения.
for _dialect_name in frozenset(SQLGLOT_DIALECT_MAPPING.values()):
    if _dialect_name not in {"", "ansi", "sql"}:
        Dialect.get_or_raise(_dialect_name)


def _validate_query_shape(root: exp.Query) -> None:
    if isinstance(root, exp.Pivot):
        if not isinstance(root.this, exp.Table) or not isinstance(
            root.this.this, exp.Identifier
        ):
            raise unsupported("root transform input must be one physical table")
        return
    for node in root.walk():
        if isinstance(node, exp.Select):
            select = node
            if select.args.get("into") is not None:
                raise unsupported("SELECT INTO is unsupported")
            with_clause = select.args.get("with")
            if with_clause is not None:
                if not isinstance(with_clause, exp.With):
                    raise unsupported("WITH clause has an unsupported shape")
                for cte in with_clause.expressions:
                    if not isinstance(cte, exp.CTE) or not isinstance(
                        cte.this, exp.Query
                    ):
                        raise unsupported("CTE body must be one query")
                    alias = cte.args.get("alias")
                    if not isinstance(alias, exp.TableAlias) or not isinstance(
                        alias.this, exp.Identifier
                    ):
                        raise unsupported("CTE must have a plain name")
        elif isinstance(node, exp.SetOperation):
            if (
                not isinstance(node.this, exp.Query)
                or not isinstance(node.expression, exp.Query)
            ):
                raise unsupported("set operation shape is unsupported")
        elif isinstance(node, exp.Pivot) and (
            not isinstance(node.parent, (exp.Table, exp.Subquery))
            or node not in (node.parent.args.get("pivots") or ())
        ):
            raise unsupported(f"{node.key} is unsupported")


class _FactBuilder:
    def __init__(self) -> None:
        self._scope_ids: dict[int, str] = {}
        self._scope_parents: dict[int, Scope | None] = {}
        self._scope_roles: dict[int, ast.QueryRole] = {}
        self._query_scopes: list[Scope] = []
        self._relation_aliases: dict[int, dict[str, str]] = {}
        self._all_relation_aliases: dict[int, dict[str, str]] = {}
        self._visible_aliases_by_scope_id: dict[str, dict[str, str]] = {}
        self._hidden_aliases_by_scope_id: dict[str, frozenset[str]] = {}
        self._outer_aliases: dict[str, dict[str, tuple[str, str]]] = {}
        self._forbidden_outer_aliases: dict[str, frozenset[str]] = {}
        self._subquery_scope_ids: dict[str, dict[int, str]] = {}
        self._cte_ids: dict[int, str] = {}
        self._cte_ids_by_name: dict[str, str] = {}
        self._derived_by_child: dict[int, ast.DerivedRelationFact] = {}
        self.scopes: list[ast.SelectScopeFact] = []
        self.ctes: list[ast.CteFact] = []
        self.table_scans: list[ast.TableScanFact] = []
        self.cte_references: list[ast.CteReferenceFact] = []
        self.derived_relations: list[ast.DerivedRelationFact] = []
        self.expression_relations: list[ast.ExpressionRelationFact] = []
        self.set_operations: list[ast.SetOperationFact] = []
        self.subquery_refs: list[ast.SubqueryRefFact] = []
        self.joins: list[ast.JoinFact] = []
        self.projections: list[ast.ProjectionFact] = []
        self.predicates: list[ast.PredicateFact] = []
        self.aggregates: list[ast.AggregateFact] = []
        self.groupings: list[ast.GroupingFact] = []
        self.orderings: list[ast.OrderingFact] = []
        self.limits: list[ast.LimitFact] = []

    def build(self, root: exp.Query) -> None:
        if isinstance(root, exp.Pivot):
            self._build_root_transform(root)
            return
        try:
            traversed = tuple(traverse_scope(root))
        except Exception as exc:
            raise unsupported("SQL query scopes are ambiguous") from exc
        query_scopes = [
            scope for scope in traversed if isinstance(scope.expression, exp.Query)
        ]
        roots = [scope for scope in query_scopes if scope.is_root]
        if len(roots) != 1 or roots[0].expression is not root:
            raise unsupported("SQL query did not produce one root scope")

        def query_depth(scope: Scope) -> int:
            depth = 0
            parent = scope.parent
            while parent is not None:
                depth += isinstance(parent.expression, exp.Query)
                parent = parent.parent
            return depth

        self._query_scopes = sorted(query_scopes, key=query_depth)
        self._scope_ids = {
            id(scope): f"scope:{index}"
            for index, scope in enumerate(self._query_scopes)
        }
        query_scope_ids = set(self._scope_ids)
        for scope in self._query_scopes:
            parent = scope.parent
            while parent is not None and id(parent) not in query_scope_ids:
                parent = parent.parent
            self._scope_parents[id(scope)] = parent
            self._scope_roles[id(scope)] = self._role_from_scope(scope, parent=parent)
        try:
            for scope in self._query_scopes:
                scope.selected_sources
        except Exception as exc:
            raise unsupported("row source aliases must be unique") from exc
        for scope in self._query_scopes:
            self._register_ctes(scope)
        for scope in self._query_scopes:
            self._register_sources(scope)
        for scope in self._query_scopes:
            self._configure_expression_context(scope)
        for scope in self._query_scopes:
            if isinstance(scope.expression, exp.Select):
                self._build_select_scope(scope)
            elif isinstance(scope.expression, exp.SetOperation):
                self._build_set_scope(scope)
            else:
                raise unsupported("query scope shape is unsupported")

    def _build_root_transform(self, root: exp.Pivot) -> None:
        source = root.this
        if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
            raise unsupported("root transform input must be one physical table")
        scope_id = "scope:0"
        relation_id = f"{scope_id}:relation:0"
        input_relation_id = f"{relation_id}:input:0"
        source_alias = source.alias_or_name
        if not source_alias:
            raise unsupported("root transform input alias is missing")
        table = ast.QualifiedTableName(
            source.catalog or None,
            source.db or None,
            source.name,
        )
        self.scopes.append(
            ast.SelectScopeFact(
                node_id("scope", scope_id, (scope_id, None, ast.QueryRole.ROOT, None, None)),
                scope_id,
                None,
                ast.QueryRole.ROOT,
                None,
                None,
            )
        )
        self.table_scans.append(
            ast.TableScanFact(
                node_id(
                    "table_scan",
                    input_relation_id,
                    (scope_id, input_relation_id, source_alias, table, (), None),
                ),
                scope_id,
                input_relation_id,
                source_alias,
                table,
                (),
                None,
            )
        )
        column_aliases = self._column_aliases(root)
        expression = self._canonical(
            root,
            scope_id=scope_id,
            relation_aliases={source_alias: input_relation_id},
            output_aliases={},
        )
        self.expression_relations.append(
            ast.ExpressionRelationFact(
                node_id(
                    "expression_relation",
                    relation_id,
                    (
                        scope_id,
                        relation_id,
                        source_alias,
                        expression,
                        (input_relation_id,),
                        column_aliases,
                    ),
                ),
                scope_id,
                relation_id,
                source_alias,
                expression,
                (input_relation_id,),
                column_aliases,
            )
        )
        star = self._canonical(
            exp.Star(),
            scope_id=scope_id,
            relation_aliases={source_alias: relation_id},
            output_aliases={},
        )
        self.projections.append(
            ast.ProjectionFact(
                node_id("projection", f"{scope_id}:projection:0", (scope_id, f"{scope_id}:output:0", star)),
                scope_id,
                f"{scope_id}:output:0",
                star,
            )
        )

    def _role_from_scope(
        self,
        scope: Scope,
        *,
        parent: Scope | None,
    ) -> ast.QueryRole:
        if parent is None:
            return ast.QueryRole.ROOT
        if scope.is_cte:
            return ast.QueryRole.CTE
        if scope.is_union:
            try:
                index = next(
                    index
                    for index, child in enumerate(parent.union_scopes)
                    if child is scope
                )
            except StopIteration as exc:
                raise unsupported("set operand scope is detached") from exc
            return (ast.QueryRole.SET_LEFT, ast.QueryRole.SET_RIGHT)[index]
        raw_parent = scope.parent
        lateral = False
        while raw_parent is not None and raw_parent is not parent:
            lateral |= isinstance(raw_parent.expression, exp.Lateral)
            raw_parent = raw_parent.parent
        if scope.is_derived_table or (scope.is_subquery and lateral):
            return ast.QueryRole.DERIVED
        if scope.is_subquery:
            return self._subquery_role(parent, scope)
        raise unsupported("query scope role is unsupported")

    @staticmethod
    def _derived_query_scope(source: Scope) -> Scope:
        if isinstance(source.expression, exp.Query):
            return source
        if isinstance(source.expression, exp.Lateral):
            children = [
                child
                for child in source.subquery_scopes
                if isinstance(child.expression, exp.Query)
            ]
            if len(children) == 1:
                return children[0]
        raise unsupported("derived relation must contain one query")

    @staticmethod
    def _subquery_role(parent: Scope, child: Scope) -> ast.QueryRole:
        node = child.expression.parent
        saw_subquery = False
        while node is not None and node is not parent.expression:
            if isinstance(node, (exp.Any, exp.All)):
                return ast.QueryRole.QUANTIFIED_SUBQUERY
            if isinstance(node, exp.Exists):
                return ast.QueryRole.EXISTS_SUBQUERY
            if isinstance(node, exp.In):
                return ast.QueryRole.IN_SUBQUERY
            if isinstance(node, exp.Subquery):
                saw_subquery = True
            node = node.parent
        if saw_subquery:
            return ast.QueryRole.SCALAR_SUBQUERY
        raise unsupported("subquery context is unsupported")

    def _register_ctes(self, scope: Scope) -> None:
        declaring_scope_id = self._scope_ids[id(scope)]
        for index, child in enumerate(scope.cte_scopes):
            cte = child.expression.find_ancestor(exp.CTE)
            if not isinstance(cte, exp.CTE):
                raise unsupported("CTE scope has no declaration")
            alias = cte.args.get("alias")
            if not isinstance(alias, exp.TableAlias) or not isinstance(
                alias.this, exp.Identifier
            ):
                raise unsupported("CTE must have a plain name")
            column_aliases = self._column_aliases(cte)
            recursive = bool(getattr(cte.parent, "args", {}).get("recursive"))
            cte_id = f"{declaring_scope_id}:cte:{index}"
            query_scope_id = self._scope_ids[id(child)]
            self._cte_ids[id(child)] = cte_id
            self._cte_ids_by_name[alias.this.name] = cte_id
            self.ctes.append(
                ast.CteFact(
                    node_id(
                        "cte",
                        cte_id,
                        (
                            declaring_scope_id,
                            cte_id,
                            query_scope_id,
                            recursive,
                            column_aliases,
                        ),
                    ),
                    declaring_scope_id,
                    cte_id,
                    query_scope_id,
                    recursive,
                    column_aliases,
                )
            )

    def _register_sources(self, scope: Scope) -> None:
        scope_key = id(scope)
        scope_id = self._scope_ids[scope_key]
        aliases: dict[str, str] = {}
        all_aliases: dict[str, str] = {}
        self._relation_aliases[scope_key] = aliases
        self._all_relation_aliases[scope_key] = all_aliases
        self._visible_aliases_by_scope_id[scope_id] = aliases
        if not isinstance(scope.expression, exp.Select):
            if scope.selected_sources:
                raise unsupported("set operation row sources are unsupported")
            return

        forbidden_ctes: set[str] = set()
        if self._scope_roles[scope_key] is ast.QueryRole.CTE:
            parent = self._scope_parents[scope_key]
            siblings = parent.cte_scopes if parent is not None else []
            try:
                current_index = siblings.index(scope)
            except ValueError as exc:
                raise unsupported("CTE scope is detached") from exc
            for sibling in siblings[current_index:]:
                cte = sibling.expression.find_ancestor(exp.CTE)
                if not isinstance(cte, exp.CTE) or not cte.alias_or_name:
                    raise unsupported("CTE must have a plain name")
                forbidden_ctes.add(cte.alias_or_name)

        from_clause = scope.expression.args.get("from")
        row_sources = (
            [from_clause.this]
            if isinstance(from_clause, exp.From)
            and isinstance(from_clause.this, exp.Expression)
            else []
        )
        row_sources.extend(
            join.this
            for join in scope.expression.args.get("joins") or ()
            if isinstance(join, exp.Join) and isinstance(join.this, exp.Expression)
        )
        ordered_sources = []
        for row_source in row_sources:
            pivots = tuple(row_source.args.get("pivots") or ())
            if len(pivots) > 1 or any(not isinstance(pivot, exp.Pivot) for pivot in pivots):
                raise unsupported("row source transform ownership is unsupported")
            pivot = pivots[0] if pivots else None
            source_alias = row_source.alias_or_name
            alias = (
                pivot.alias_or_name or source_alias
                if pivot is not None
                else source_alias
            )
            source = scope.sources.get(source_alias)
            if source is None and isinstance(row_source, exp.Table):
                source = scope.sources.get(row_source.name)
            if source is None:
                raise unsupported("row source is absent from its Scope")
            ordered_sources.append(
                (
                    alias,
                    source_alias,
                    row_source,
                    source,
                    source_alias in scope.selected_sources
                    or (
                        isinstance(row_source, exp.Table)
                        and row_source.name in scope.selected_sources
                    )
                    # The FROM source is visible even when sqlglot omits it
                    # from selected_sources after applying a row transform.
                    or row_source is row_sources[0],
                    pivot,
                )
            )

        for index, (alias, source_alias, node, source, output_visible, pivot) in enumerate(ordered_sources):
            relation_id = f"{scope_id}:relation:{index}"
            if not alias:
                if source_alias or not isinstance(node, exp.Subquery):
                    raise unsupported("empty relation alias is unsupported")
                alias = relation_id
            if alias in all_aliases:
                raise unsupported("duplicate or empty relation alias is unsupported")
            all_aliases[alias] = relation_id
            if output_visible:
                aliases[alias] = relation_id
            if pivot is not None:
                input_relation_id = f"{relation_id}:input:0"
                self._register_pivot_input(
                    source=source,
                    node=node,
                    scope_id=scope_id,
                    relation_id=input_relation_id,
                    source_alias=source_alias,
                    relation_aliases=all_aliases,
                    forbidden_ctes=forbidden_ctes,
                )
                expression = self._canonical(
                    pivot,
                    scope_id=scope_id,
                    relation_aliases={**all_aliases, source_alias: input_relation_id},
                    output_aliases={},
                )
                column_aliases = self._column_aliases(pivot)
                self.expression_relations.append(
                    ast.ExpressionRelationFact(
                        node_id(
                            "expression_relation",
                            relation_id,
                            (
                                scope_id,
                                relation_id,
                                alias,
                                expression,
                                (input_relation_id,),
                                column_aliases,
                            ),
                        ),
                        scope_id,
                        relation_id,
                        alias,
                        expression,
                        (input_relation_id,),
                        column_aliases,
                    )
                )
                continue
            if isinstance(source, exp.Table):
                column_aliases = self._column_aliases(source)
                if not isinstance(source.this, exp.Identifier):
                    expression = self._canonical(
                        source.this,
                        scope_id=scope_id,
                        relation_aliases=all_aliases,
                        output_aliases={},
                    )
                    self.expression_relations.append(
                        ast.ExpressionRelationFact(
                            node_id(
                                "expression_relation",
                                relation_id,
                                (scope_id, relation_id, alias, expression, column_aliases),
                            ),
                            scope_id,
                            relation_id,
                            alias,
                            expression,
                            (),
                            column_aliases,
                        )
                    )
                    continue
                table_alias = source.args.get("alias")
                if table_alias is not None and (
                    not isinstance(table_alias, exp.TableAlias)
                    or not isinstance(table_alias.this, exp.Identifier)
                ):
                    raise unsupported("relation alias must be one plain identifier")
                if (
                    not source.db
                    and not source.catalog
                    and source.name in forbidden_ctes
                ):
                    raise unsupported("forward or self CTE reference is unsupported")
                table = ast.QualifiedTableName(
                    source.catalog or None,
                    source.db or None,
                    source.name,
                )
                options = self._canonical_options(
                    source,
                    excluded=frozenset({"this", "db", "catalog", "alias"}),
                    scope_id=scope_id,
                    relation_aliases=all_aliases,
                    output_aliases={},
                )
                self.table_scans.append(
                    ast.TableScanFact(
                        node_id(
                            "table_scan",
                            relation_id,
                            (scope_id, relation_id, alias, table, column_aliases, options),
                        ),
                        scope_id,
                        relation_id,
                        alias,
                        table,
                        column_aliases,
                        options,
                    )
                )
                continue
            if not isinstance(source, Scope):
                raise unsupported("row source type is unsupported")
            if isinstance(node, exp.Table):
                cte_id = self._cte_ids.get(id(source)) or self._cte_ids_by_name.get(
                    node.name
                )
                if cte_id is None:
                    raise unsupported("CTE source is unresolved")
                self.cte_references.append(
                    ast.CteReferenceFact(
                        node_id(
                            "cte_reference",
                            relation_id,
                            (
                                scope_id,
                                relation_id,
                                alias,
                                cte_id,
                                self._column_aliases(node),
                            ),
                        ),
                        scope_id,
                        relation_id,
                        alias,
                        cte_id,
                        self._column_aliases(node),
                    )
                )
                continue

            if not isinstance(source.expression, (exp.Query, exp.Lateral)):
                expression = self._canonical(
                    node,
                    scope_id=scope_id,
                    relation_aliases=all_aliases,
                    output_aliases={},
                )
                column_aliases = self._column_aliases(node)
                self.expression_relations.append(
                    ast.ExpressionRelationFact(
                        node_id(
                            "expression_relation",
                            relation_id,
                            (scope_id, relation_id, alias, expression, column_aliases),
                        ),
                        scope_id,
                        relation_id,
                        alias,
                        expression,
                        (),
                        column_aliases,
                    )
                )
                continue

            child = self._derived_query_scope(source)
            lateral = isinstance(source.expression, exp.Lateral)
            owner = source.expression if lateral else child.expression.parent
            if not isinstance(owner, (exp.Lateral, exp.Subquery)):
                raise unsupported("derived relation has an unsupported shape")
            relation_alias = owner.args.get("alias")
            if (
                relation_alias is not None
                and (
                    not isinstance(relation_alias, exp.TableAlias)
                    or not isinstance(relation_alias.this, exp.Identifier)
                )
                or any(
                    value
                    for name, value in owner.args.items()
                    if name not in {"this", "alias"} and value not in (None, False, [])
                )
            ):
                raise unsupported("derived relation must have one plain alias")
            column_aliases = self._column_aliases(owner)
            query_scope_id = self._scope_ids[id(child)]
            fact = ast.DerivedRelationFact(
                node_id(
                    "derived_relation",
                    relation_id,
                    (
                        scope_id,
                        relation_id,
                        alias,
                        query_scope_id,
                        lateral,
                        column_aliases,
                    ),
                ),
                scope_id,
                relation_id,
                alias,
                query_scope_id,
                lateral,
                column_aliases,
            )
            self.derived_relations.append(fact)
            self._derived_by_child[id(child)] = fact
        self._hidden_aliases_by_scope_id[scope_id] = frozenset(all_aliases).difference(
            aliases
        )

    def _register_pivot_input(
        self,
        *,
        source: exp.Table | Scope,
        node: exp.Expression,
        scope_id: str,
        relation_id: str,
        source_alias: str,
        relation_aliases: dict[str, str],
        forbidden_ctes: set[str],
    ) -> None:
        if isinstance(source, exp.Table):
            if not isinstance(source.this, exp.Identifier):
                raise unsupported("pivot input relation is unsupported")
            if (
                not source.db
                and not source.catalog
                and source.name in forbidden_ctes
            ):
                raise unsupported("forward or self CTE reference is unsupported")
            table = ast.QualifiedTableName(
                source.catalog or None,
                source.db or None,
                source.name,
            )
            options = self._canonical_options(
                source,
                excluded=frozenset({"this", "db", "catalog", "alias", "pivots"}),
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases={},
            )
            self.table_scans.append(
                ast.TableScanFact(
                    node_id(
                        "table_scan",
                        relation_id,
                        (scope_id, relation_id, source_alias, table, (), options),
                    ),
                    scope_id,
                    relation_id,
                    source_alias,
                    table,
                    (),
                    options,
                )
            )
            return
        if isinstance(node, exp.Table):
            cte_id = self._cte_ids.get(id(source)) or self._cte_ids_by_name.get(
                node.name
            )
            if cte_id is None:
                raise unsupported("CTE source is unresolved")
            self.cte_references.append(
                ast.CteReferenceFact(
                    node_id(
                        "cte_reference",
                        relation_id,
                        (scope_id, relation_id, source_alias, cte_id, ()),
                    ),
                    scope_id,
                    relation_id,
                    source_alias,
                    cte_id,
                    (),
                )
            )
            return
        child = self._derived_query_scope(source)
        owner = child.expression.parent
        if not isinstance(owner, exp.Subquery):
            raise unsupported("derived pivot input is unsupported")
        fact = ast.DerivedRelationFact(
            node_id(
                "derived_relation",
                relation_id,
                (scope_id, relation_id, source_alias, self._scope_ids[id(child)], False, ()),
            ),
            scope_id,
            relation_id,
            source_alias,
            self._scope_ids[id(child)],
            False,
            (),
        )
        self.derived_relations.append(fact)
        self._derived_by_child[id(child)] = fact

    @staticmethod
    def _column_aliases(owner: exp.Expression) -> tuple[str, ...]:
        if isinstance(owner, exp.Pivot):
            columns = tuple(
                column.name
                for column in owner.args.get("columns") or ()
                if isinstance(column, exp.Identifier) and column.name
            )
            if columns:
                return columns
            into = owner.args.get("into")
            if isinstance(into, exp.Expression):
                output_columns = tuple(
                    column.name
                    for column in (
                        into.this,
                        *(into.expressions or ()),
                    )
                    if isinstance(column, exp.Column) and not column.table and column.name
                )
                if output_columns:
                    if len(output_columns) != len(set(output_columns)):
                        raise unsupported("relation column aliases must be unique")
                    return output_columns
        alias = owner.args.get("alias")
        if alias is None:
            return ()
        if not isinstance(alias, exp.TableAlias) or not isinstance(
            alias.this, exp.Identifier
        ):
            raise unsupported("relation alias must be one plain identifier")
        columns = alias.args.get("columns") or []
        if not all(
            isinstance(column, exp.Identifier) and column.name for column in columns
        ):
            raise unsupported("relation column aliases must be identifiers")
        names = tuple(column.name for column in columns)
        if len(names) != len(set(names)):
            raise unsupported("relation column aliases must be unique")
        return names

    def _configure_expression_context(self, scope: Scope) -> None:
        scope_key = id(scope)
        scope_id = self._scope_ids[scope_key]
        parent = self._scope_parents[scope_key]
        role = self._scope_roles[scope_key]
        self._subquery_scope_ids.setdefault(scope_id, {})
        allowed: dict[str, tuple[str, str]] = {}

        if parent is not None:
            parent_scope_id = self._scope_ids[id(parent)]
            parent_relations = self._relation_aliases[id(parent)]
            if role in {ast.QueryRole.SET_LEFT, ast.QueryRole.SET_RIGHT}:
                allowed = dict(self._outer_aliases[parent_scope_id])
            elif role is ast.QueryRole.DERIVED:
                fact = self._derived_by_child.get(scope_key)
                if fact is None:
                    raise unsupported("derived query scope has no relation fact")
                if fact.lateral:
                    allowed = dict(self._outer_aliases[parent_scope_id])
                    for alias, relation_id in parent_relations.items():
                        if relation_id == fact.relation_id:
                            break
                        allowed[alias] = (parent_scope_id, relation_id)
                    else:
                        raise unsupported("LATERAL source is absent from its parent")
            elif role is not ast.QueryRole.CTE:
                node = scope.expression.parent
                enclosing_join: exp.Join | None = None
                while node is not None and node is not parent.expression:
                    if isinstance(node, exp.Join):
                        enclosing_join = node
                    node = node.parent
                visible_count = len(parent_relations)
                if enclosing_join is not None:
                    joins = parent.expression.args.get("joins") or []
                    try:
                        visible_count = joins.index(enclosing_join) + 2
                    except ValueError as exc:
                        raise unsupported("subquery JOIN owner is unresolved") from exc
                    parent_relations = self._all_relation_aliases[id(parent)]
                allowed = dict(self._outer_aliases[parent_scope_id])
                allowed.update(
                    (
                        alias,
                        (parent_scope_id, relation_id),
                    )
                    for alias, relation_id in tuple(parent_relations.items())[
                        :visible_count
                    ]
                )

                child_scope_id = scope_id
                mappings = self._subquery_scope_ids[parent_scope_id]
                mappings[id(scope.expression)] = child_scope_id
                node = scope.expression.parent
                while node is not None and node is not parent.expression:
                    if isinstance(node, exp.Subquery):
                        mappings[id(node)] = child_scope_id
                    node = node.parent
                occurrence = sum(
                    reference.scope_id == parent_scope_id
                    for reference in self.subquery_refs
                )
                self.subquery_refs.append(
                    ast.SubqueryRefFact(
                        node_id(
                            "subquery_ref",
                            f"{parent_scope_id}:subquery:{occurrence}",
                            (parent_scope_id, child_scope_id, role),
                        ),
                        parent_scope_id,
                        child_scope_id,
                        role,
                    )
                )

        potential: set[str] = set()
        ancestor = parent
        while ancestor is not None:
            potential.update(self._all_relation_aliases[id(ancestor)])
            ancestor = self._scope_parents[id(ancestor)]
        self._outer_aliases[scope_id] = allowed
        self._forbidden_outer_aliases[scope_id] = frozenset(
            potential.difference(allowed)
        )

    def _canonical(
        self,
        expression: exp.Expression,
        *,
        scope_id: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
        allow_output_ref: bool = False,
        allow_ordinal_ref: bool = False,
    ) -> ast.ExpressionFact:
        return canonical_expression(
            expression,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
            outer_relation_aliases=self._outer_aliases.get(scope_id),
            forbidden_outer_aliases=(
                self._forbidden_outer_aliases.get(scope_id, frozenset())
                | (
                    self._hidden_aliases_by_scope_id.get(scope_id, frozenset())
                    if relation_aliases
                    is self._visible_aliases_by_scope_id.get(scope_id)
                    else frozenset()
                )
            ),
            subquery_scope_ids=self._subquery_scope_ids.get(scope_id),
            allow_output_ref=allow_output_ref,
            allow_ordinal_ref=allow_ordinal_ref,
        )

    def _canonical_options(
        self,
        node: exp.Expression,
        *,
        excluded: frozenset[str],
        scope_id: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
        allow_output_ref: bool = False,
    ) -> ast.ExpressionFact | None:
        residual = {
            name: value
            for name, value in node.args.items()
            if name not in excluded and value not in (None, False, [])
        }
        if not residual:
            return None
        return self._canonical(
            exp.Expression(**residual),
            scope_id=scope_id,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
            allow_output_ref=allow_output_ref,
        )

    def _build_select_scope(self, scope: Scope) -> None:
        select = scope.expression
        if not isinstance(select, exp.Select):
            raise unsupported("select scope is invalid")
        scope_id = self._scope_ids[id(scope)]
        parent = self._scope_parents[id(scope)]
        role = self._scope_roles[id(scope)]
        relation_aliases = self._relation_aliases[id(scope)]
        join_relation_aliases = self._all_relation_aliases[id(scope)]
        output_aliases = self._output_aliases(select, scope_id=scope_id)
        distinct_node = select.args.get("distinct")
        distinct = (
            self._canonical(
                distinct_node,
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )
            if isinstance(distinct_node, exp.Expression)
            else None
        )
        options = self._canonical_options(
            select,
            excluded=frozenset(
                {
                    "distinct", "expressions", "from", "joins", "where", "group",
                    "having", "order", "limit", "offset", "with", "into",
                }
            ),
            scope_id=scope_id,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
            allow_output_ref=True,
        )
        parent_scope_id = self._scope_ids[id(parent)] if parent is not None else None
        self.scopes.append(
            ast.SelectScopeFact(
                node_id(
                    "scope",
                    scope_id,
                    (scope_id, parent_scope_id, role, distinct, options),
                ),
                scope_id,
                parent_scope_id,
                role,
                distinct,
                options,
            )
        )
        joins = select.args.get("joins") or []
        relation_ids = tuple(join_relation_aliases.values())
        if joins and len(relation_ids) != len(joins) + 1:
            raise unsupported("JOIN sources are not canonical")
        self._build_joins(
            [
                (join, relation_ids[index + 1], relation_ids[: index + 1])
                for index, join in enumerate(joins)
                if isinstance(join, exp.Join)
            ],
            scope_id=scope_id,
            relation_aliases=join_relation_aliases,
            output_aliases=output_aliases,
            output_relation_ids=frozenset(relation_aliases.values()),
        )
        self._build_projections(
            select,
            scope_id=scope_id,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
        )
        self._build_clauses(
            select,
            scope_id=scope_id,
            relation_aliases=relation_aliases,
            output_aliases=output_aliases,
        )

    def _build_set_scope(self, scope: Scope) -> None:
        query = scope.expression
        if not isinstance(query, exp.SetOperation) or len(scope.union_scopes) != 2:
            raise unsupported("set operation scope is invalid")
        if isinstance(query, exp.Union):
            operation = ast.SetOperationKind.UNION
        elif isinstance(query, exp.Intersect):
            operation = ast.SetOperationKind.INTERSECT
        elif isinstance(query, exp.Except):
            operation = ast.SetOperationKind.EXCEPT
        else:
            raise unsupported("set operation type is unsupported")
        scope_id = self._scope_ids[id(scope)]
        parent = self._scope_parents[id(scope)]
        parent_scope_id = self._scope_ids[id(parent)] if parent is not None else None
        role = self._scope_roles[id(scope)]
        left, right = scope.union_scopes
        left_scope_id = self._scope_ids[id(left)]
        right_scope_id = self._scope_ids[id(right)]
        distinct = query.args.get("distinct") is not False
        options = self._canonical_options(
            query,
            excluded=frozenset({"this", "expression", "distinct", "with"}),
            scope_id=scope_id,
            relation_aliases={},
            output_aliases={},
        )
        self.set_operations.append(
            ast.SetOperationFact(
                node_id(
                    "set_operation",
                    scope_id,
                    (
                        scope_id,
                        parent_scope_id,
                        role,
                        operation,
                        left_scope_id,
                        right_scope_id,
                        distinct,
                        options,
                    ),
                ),
                scope_id,
                parent_scope_id,
                role,
                operation,
                left_scope_id,
                right_scope_id,
                distinct,
                options,
            )
        )

    @staticmethod
    def _output_aliases(select: exp.Select, *, scope_id: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for index, projection in enumerate(select.expressions):
            alias = projection.alias
            if not alias:
                continue
            if alias in aliases:
                raise unsupported("duplicate projection alias is unsupported")
            aliases[alias] = f"{scope_id}:output:{index}"
        return aliases

    def _build_joins(
        self,
        pending_joins: list[tuple[exp.Join, str, tuple[str, ...]]],
        *,
        scope_id: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
        output_relation_ids: frozenset[str],
    ) -> None:
        for index, (join, relation_id, left_relation_ids) in enumerate(pending_joins):
            if join.args.get("pivots"):
                raise unsupported("pivoted joins are unsupported")
            raw_using = join.args.get("using")
            on_node = join.args.get("on")
            if on_node is not None and not isinstance(on_node, exp.Expression):
                raise unsupported("JOIN ON has an unsupported shape")

            using_columns: tuple[str, ...] = ()
            if raw_using is not None:
                if not isinstance(raw_using, list) or not raw_using or on_node is not None:
                    raise unsupported("JOIN USING has an unsupported shape")
                if not all(
                    isinstance(column, exp.Identifier) and column.name
                    for column in raw_using
                ):
                    raise unsupported("JOIN USING columns must be identifiers")
                using_columns = tuple(column.name for column in raw_using)

            condition = (
                self._canonical(
                    on_node,
                    scope_id=scope_id,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                )
                if isinstance(on_node, exp.Expression)
                else None
            )
            options = self._canonical_options(
                join,
                excluded=frozenset({"this", "on", "using", "pivots"}),
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )
            output_visible = relation_id in output_relation_ids
            occurrence = f"{scope_id}:join:{index}"
            join_node_id = node_id(
                "join",
                occurrence,
                (
                    scope_id,
                    relation_id,
                    left_relation_ids,
                    output_visible,
                    condition,
                    using_columns,
                    options,
                ),
            )
            self.joins.append(
                ast.JoinFact(
                    join_node_id,
                    scope_id,
                    relation_id,
                    left_relation_ids,
                    output_visible,
                    condition,
                    using_columns,
                    options,
                )
            )
            if condition is not None:
                self._add_predicate(
                    scope_id=scope_id,
                    location=ast.PredicateLocation.JOIN_ON,
                    owner_node_id=join_node_id,
                    occurrence=f"{occurrence}:on",
                    expression=condition,
                )

    def _build_projections(
        self,
        select: exp.Select,
        *,
        scope_id: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
    ) -> None:
        for index, projection in enumerate(select.expressions):
            raw_expression = (
                projection.this if isinstance(projection, exp.Alias) else projection
            )
            if not isinstance(raw_expression, exp.Expression):
                raise unsupported("projection has an unsupported shape")
            expression = self._canonical(
                raw_expression,
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )
            output_id = f"{scope_id}:output:{index}"
            occurrence = f"{scope_id}:projection:{index}"
            self.projections.append(
                ast.ProjectionFact(
                    node_id(
                        "projection",
                        occurrence,
                        (scope_id, output_id, expression),
                    ),
                    scope_id,
                    output_id,
                    expression,
                )
            )
            self._add_aggregates(
                raw_expression,
                scope_id=scope_id,
                occurrence=occurrence,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )

    def _build_clauses(
        self,
        select: exp.Select,
        *,
        scope_id: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
    ) -> None:
        where = select.args.get("where")
        if where is not None:
            if not isinstance(where, exp.Where) or not isinstance(
                where.this, exp.Expression
            ):
                raise unsupported("WHERE clause has an unsupported shape")
            self._add_predicate(
                scope_id=scope_id,
                location=ast.PredicateLocation.WHERE,
                owner_node_id=None,
                occurrence=f"{scope_id}:where",
                expression=self._canonical(
                    where.this,
                    scope_id=scope_id,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                ),
            )

        group = select.args.get("group")
        if group is not None:
            if not isinstance(group, exp.Group):
                raise unsupported("GROUP BY clause has an unsupported shape")
            expression = self._canonical(
                group,
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
                allow_output_ref=True,
                allow_ordinal_ref=True,
            )
            occurrence = f"{scope_id}:group"
            self.groupings.append(
                ast.GroupingFact(
                    node_id("group", occurrence, (scope_id, expression)),
                    scope_id,
                    expression,
                )
            )

        having = select.args.get("having")
        if having is not None:
            if not isinstance(having, exp.Having) or not isinstance(
                having.this, exp.Expression
            ):
                raise unsupported("HAVING clause has an unsupported shape")
            occurrence = f"{scope_id}:having"
            self._add_predicate(
                scope_id=scope_id,
                location=ast.PredicateLocation.HAVING,
                owner_node_id=None,
                occurrence=occurrence,
                expression=self._canonical(
                    having.this,
                    scope_id=scope_id,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                ),
            )
            self._add_aggregates(
                having.this,
                scope_id=scope_id,
                occurrence=occurrence,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )

        order = select.args.get("order")
        if order is not None:
            if not isinstance(order, exp.Order):
                raise unsupported("ORDER BY clause has an unsupported shape")
            for index, ordered in enumerate(order.expressions):
                if not isinstance(ordered, exp.Ordered) or not isinstance(
                    ordered.this, exp.Expression
                ):
                    raise unsupported("ORDER BY item has an unsupported shape")
                expression = self._canonical(
                    ordered.this,
                    scope_id=scope_id,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                    allow_output_ref=True,
                    allow_ordinal_ref=True,
                )
                descending = bool(ordered.args.get("desc"))
                raw_nulls_first = ordered.args.get("nulls_first")
                nulls_first = (
                    bool(raw_nulls_first) if raw_nulls_first is not None else None
                )
                options = self._canonical_options(
                    ordered,
                    excluded=frozenset({"this", "desc", "nulls_first"}),
                    scope_id=scope_id,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                    allow_output_ref=True,
                )
                occurrence = f"{scope_id}:order:{index}"
                self.orderings.append(
                    ast.OrderingFact(
                        node_id(
                            "ordering",
                            occurrence,
                            (scope_id, expression, descending, nulls_first, options),
                        ),
                        scope_id,
                        expression,
                        descending,
                        nulls_first,
                        options,
                    )
                )
                self._add_aggregates(
                    ordered.this,
                    scope_id=scope_id,
                    occurrence=occurrence,
                    relation_aliases=relation_aliases,
                    output_aliases=output_aliases,
                )

        self._build_limit(select, scope_id=scope_id)

    def _build_limit(self, select: exp.Select, *, scope_id: str) -> None:
        limit = select.args.get("limit")
        offset = select.args.get("offset")
        if limit is None and offset is None:
            return
        count: int | None = None
        offset_value = 0
        if limit is not None:
            if (
                not isinstance(limit, exp.Limit)
                or limit.args.get("limit_options")
                or limit.args.get("expressions")
            ):
                raise unsupported("LIMIT clause has an unsupported shape")
            if not isinstance(limit.expression, exp.Expression):
                raise unsupported("LIMIT count is missing")
            count = non_negative_integer(limit.expression, context="LIMIT")
        if offset is not None:
            if not isinstance(offset, exp.Offset) or not isinstance(
                offset.expression, exp.Expression
            ):
                raise unsupported("OFFSET clause has an unsupported shape")
            offset_value = non_negative_integer(offset.expression, context="OFFSET")
        occurrence = f"{scope_id}:limit"
        self.limits.append(
            ast.LimitFact(
                node_id("limit", occurrence, (scope_id, count, offset_value)),
                scope_id,
                count,
                offset_value,
            )
        )

    def _add_predicate(
        self,
        *,
        scope_id: str,
        location: ast.PredicateLocation,
        owner_node_id: str | None,
        occurrence: str,
        expression: ast.ExpressionFact,
    ) -> None:
        self.predicates.append(
            ast.PredicateFact(
                node_id(
                    "predicate",
                    occurrence,
                    (scope_id, location, owner_node_id, expression),
                ),
                scope_id,
                location,
                owner_node_id,
                expression,
                predicate_atoms(expression, predicate_occurrence=occurrence),
            )
        )

    def _add_aggregates(
        self,
        expression: exp.Expression,
        *,
        scope_id: str,
        occurrence: str,
        relation_aliases: dict[str, str],
        output_aliases: dict[str, str],
    ) -> None:
        aggregate_index = 0
        pending = [expression]
        while pending:
            expression_node = pending.pop()
            if expression_node is not expression and isinstance(
                expression_node, (exp.Query, exp.Subquery)
            ):
                continue
            if not isinstance(expression_node, exp.AggFunc):
                pending.extend(expression_node.iter_expressions())
                continue
            canonical = self._canonical(
                expression_node,
                scope_id=scope_id,
                relation_aliases=relation_aliases,
                output_aliases=output_aliases,
            )
            function = expression_node.sql_name().lower()
            distinct = aggregate_is_distinct(expression_node)
            aggregate_occurrence = f"{occurrence}:aggregate:{aggregate_index}"
            self.aggregates.append(
                ast.AggregateFact(
                    node_id(
                        "aggregate",
                        aggregate_occurrence,
                        (scope_id, function, distinct, canonical),
                    ),
                    scope_id,
                    function,
                    distinct,
                    canonical,
                )
            )
            aggregate_index += 1
            pending.extend(expression_node.iter_expressions())


def _candidate_from_builder(
    builder: _FactBuilder,
    *,
    sql: str,
    candidate_id: str,
    dialect: str,
) -> ast.ParsedSqlCandidate:
    candidate = ast.ParsedSqlCandidate(
        candidate_id=candidate_id,
        dialect=dialect,
        candidate_digest="sha256:" + ("0" * 64),
        source_sql_digest=source_sql_digest(sql),
        scopes=tuple(builder.scopes),
        ctes=tuple(builder.ctes),
        table_scans=tuple(builder.table_scans),
        cte_references=tuple(builder.cte_references),
        derived_relations=tuple(builder.derived_relations),
        expression_relations=tuple(builder.expression_relations),
        set_operations=tuple(builder.set_operations),
        subquery_refs=tuple(builder.subquery_refs),
        joins=tuple(builder.joins),
        projections=tuple(builder.projections),
        predicates=tuple(builder.predicates),
        aggregates=tuple(builder.aggregates),
        groupings=tuple(builder.groupings),
        orderings=tuple(builder.orderings),
        limits=tuple(builder.limits),
    )
    return replace(candidate, candidate_digest=semantic_candidate_digest(candidate))


def parse_and_build_candidate(
    sql: str,
    dialect: str,
    candidate_id: str,
    limits: ast.AstLimits,
) -> ast.ParsedSqlCandidate:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        raise ast.SqlAstError(
            ast.SqlAstErrorCode.PARSE_FAILED,
            "SQL parser rejected the candidate",
        ) from exc
    if not statements or (len(statements) == 1 and statements[0] is None):
        raise ast.SqlAstError(
            ast.SqlAstErrorCode.PARSE_FAILED,
            "SQL candidate contains no statement",
        )
    if len(statements) != 1:
        raise ast.SqlAstError(
            ast.SqlAstErrorCode.MULTI_STATEMENT,
            "SQL candidate must contain exactly one statement",
        )
    root = statements[0]
    if not isinstance(root, (exp.Query, exp.Pivot)):
        raise unsupported("SQL candidate must be one query")
    check_ast_bounds(root, limits=limits)
    try:
        normalized = normalize_identifiers(root.copy(), dialect=dialect)
    except Exception as exc:
        raise ast.SqlAstError(
            ast.SqlAstErrorCode.PARSE_FAILED,
            "SQL identifiers could not be normalized",
        ) from exc
    if not isinstance(normalized, (exp.Query, exp.Pivot)):
        raise unsupported("normalized SQL candidate is not a query")
    check_ast_bounds(normalized, limits=limits)
    _validate_query_shape(normalized)
    builder = _FactBuilder()
    builder.build(normalized)
    return _candidate_from_builder(
        builder,
        sql=sql,
        candidate_id=candidate_id,
        dialect=dialect,
    )


__all__ = ["parse_and_build_candidate"]
