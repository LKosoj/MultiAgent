"""Deterministic seeded SQLite schemas for adaptive Text-to-SQL tests.

The fixtures model schema-linking and value-grounding shapes only.  They do
not contain benchmark questions, expected SQL, expected result sets, dataset
identifiers, or production-runtime imports.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Literal


@dataclass(frozen=True, slots=True)
class SemanticExpectation:
    """Expected research meaning without prescribing a controller strategy."""

    source_semantic: str
    description: str
    expected_binding_kind: (
        Literal[
            "physical_column",
            "discriminator_value",
            "derived_expression",
        ]
        | None
    )
    evidence_requirements: tuple[str, ...]
    expected_expression: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaDocumentFixture:
    """One exact allowlisted schema document for document evidence tests."""

    document_id: str
    namespace: str
    source_version: str
    content: str


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """One named adaptive fixture and its schema/data construction inputs."""

    fixture_id: str
    category: str
    diagnostic_capabilities: tuple[str, ...]
    expected_category: Literal["supported", "ambiguous", "unsupported"]
    expected_schema_signature: tuple[
        tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]], ...
    ]
    ddl: tuple[str, ...]
    seed_data: tuple[tuple[str, tuple[object, ...]], ...]
    semantic_expectations: tuple[SemanticExpectation, ...] = ()
    schema_documents: tuple[SchemaDocumentFixture, ...] = ()


_FIXTURES = (
    FixtureSpec(
        fixture_id="F01_CONVENTIONAL_STAR",
        category="conventional_star",
        diagnostic_capabilities=("schema_linking", "value_grounding"),
        expected_category="supported",
        expected_schema_signature=(
            ("branch_dim", ("branch_id", "branch_label"), ()),
            ("day_dim", ("day_id", "day_label"), ()),
            (
                "sales_fact",
                ("sale_id", "branch_id", "day_id", "sale_value"),
                (("day_id", "day_dim"), ("branch_id", "branch_dim")),
            ),
        ),
        ddl=(
            """
            CREATE TABLE branch_dim (
                branch_id INTEGER PRIMARY KEY,
                branch_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE day_dim (
                day_id INTEGER PRIMARY KEY,
                day_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE sales_fact (
                sale_id INTEGER PRIMARY KEY,
                branch_id INTEGER NOT NULL,
                day_id INTEGER NOT NULL,
                sale_value REAL NOT NULL,
                FOREIGN KEY (branch_id) REFERENCES branch_dim(branch_id),
                FOREIGN KEY (day_id) REFERENCES day_dim(day_id)
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO branch_dim VALUES (?, ?)", (1, "branch-a")),
            ("INSERT INTO branch_dim VALUES (?, ?)", (2, "branch-b")),
            ("INSERT INTO day_dim VALUES (?, ?)", (1, "day-01")),
            ("INSERT INTO day_dim VALUES (?, ?)", (2, "day-02")),
            ("INSERT INTO sales_fact VALUES (?, ?, ?, ?)", (1, 1, 1, 12.0)),
            ("INSERT INTO sales_fact VALUES (?, ?, ?, ?)", (2, 1, 2, 7.0)),
            ("INSERT INTO sales_fact VALUES (?, ?, ?, ?)", (3, 2, 1, 5.0)),
        ),
    ),
    FixtureSpec(
        fixture_id="F02_VERTICAL_EAV",
        category="vertical_eav",
        diagnostic_capabilities=("schema_linking", "value_grounding"),
        expected_category="supported",
        expected_schema_signature=(
            (
                "attribute_fact",
                ("member_id", "attribute_id", "value_text"),
                (("attribute_id", "attribute_kind"), ("member_id", "member")),
            ),
            ("attribute_kind", ("attribute_id", "attribute_key"), ()),
            ("member", ("member_id", "member_label"), ()),
        ),
        ddl=(
            """
            CREATE TABLE member (
                member_id INTEGER PRIMARY KEY,
                member_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE attribute_kind (
                attribute_id INTEGER PRIMARY KEY,
                attribute_key TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE attribute_fact (
                member_id INTEGER NOT NULL,
                attribute_id INTEGER NOT NULL,
                value_text TEXT NOT NULL,
                PRIMARY KEY (member_id, attribute_id),
                FOREIGN KEY (member_id) REFERENCES member(member_id),
                FOREIGN KEY (attribute_id) REFERENCES attribute_kind(attribute_id)
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO member VALUES (?, ?)", (1, "member-a")),
            ("INSERT INTO member VALUES (?, ?)", (2, "member-b")),
            (
                "INSERT INTO attribute_kind VALUES (?, ?)",
                (1, "membership_level"),
            ),
            ("INSERT INTO attribute_kind VALUES (?, ?)", (2, "region")),
            ("INSERT INTO attribute_fact VALUES (?, ?, ?)", (1, 1, "gold")),
            ("INSERT INTO attribute_fact VALUES (?, ?, ?)", (2, 1, "silver")),
            ("INSERT INTO attribute_fact VALUES (?, ?, ?)", (1, 2, "north")),
        ),
    ),
    FixtureSpec(
        fixture_id="F03_OPAQUE_NAMES",
        category="opaque_names",
        diagnostic_capabilities=("schema_linking", "value_grounding"),
        expected_category="supported",
        expected_schema_signature=(
            ("a17", ("k0", "v1"), ()),
            ("b29", ("k2", "v3"), ()),
            (
                "c31",
                ("k4", "u5", "w6", "x7"),
                (("w6", "b29"), ("u5", "a17")),
            ),
        ),
        ddl=(
            """
            CREATE TABLE a17 (
                k0 INTEGER PRIMARY KEY,
                v1 TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE b29 (
                k2 INTEGER PRIMARY KEY,
                v3 TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE c31 (
                k4 INTEGER PRIMARY KEY,
                u5 INTEGER NOT NULL,
                w6 INTEGER NOT NULL,
                x7 TEXT NOT NULL,
                FOREIGN KEY (u5) REFERENCES a17(k0),
                FOREIGN KEY (w6) REFERENCES b29(k2)
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO a17 VALUES (?, ?)", (1, "member")),
            ("INSERT INTO a17 VALUES (?, ?)", (2, "visitor")),
            ("INSERT INTO b29 VALUES (?, ?)", (1, "membership_level")),
            ("INSERT INTO b29 VALUES (?, ?)", (2, "region")),
            ("INSERT INTO c31 VALUES (?, ?, ?, ?)", (1, 1, 1, "gold")),
            ("INSERT INTO c31 VALUES (?, ?, ?, ?)", (2, 2, 1, "silver")),
            ("INSERT INTO c31 VALUES (?, ?, ?, ?)", (3, 1, 2, "north")),
        ),
    ),
    FixtureSpec(
        fixture_id="F04_MISSING_DECLARED_FK",
        category="missing_declared_fk",
        diagnostic_capabilities=("missing_declared_fk",),
        expected_category="supported",
        expected_schema_signature=(
            ("depot", ("depot_id", "depot_label"), ()),
            ("ledger", ("ledger_id", "depot_ref", "entry_value"), ()),
        ),
        ddl=(
            """
            CREATE TABLE depot (
                depot_id INTEGER PRIMARY KEY,
                depot_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE ledger (
                ledger_id INTEGER PRIMARY KEY,
                depot_ref INTEGER NOT NULL,
                entry_value REAL NOT NULL
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO depot VALUES (?, ?)", (10, "depot-a")),
            ("INSERT INTO depot VALUES (?, ?)", (20, "depot-b")),
            ("INSERT INTO ledger VALUES (?, ?, ?)", (1, 10, 4.0)),
            ("INSERT INTO ledger VALUES (?, ?, ?)", (2, 10, 8.0)),
            ("INSERT INTO ledger VALUES (?, ?, ?)", (3, 20, 3.0)),
        ),
    ),
    FixtureSpec(
        fixture_id="F05_AMBIGUOUS_BINDING",
        category="ambiguous_binding",
        diagnostic_capabilities=("ambiguous_binding",),
        expected_category="ambiguous",
        expected_schema_signature=(
            ("member", ("member_id", "member_label"), ()),
            (
                "member_state_a",
                ("member_id", "state_value"),
                (("member_id", "member"),),
            ),
            (
                "member_state_b",
                ("member_id", "state_value"),
                (("member_id", "member"),),
            ),
        ),
        ddl=(
            """
            CREATE TABLE member (
                member_id INTEGER PRIMARY KEY,
                member_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE member_state_a (
                member_id INTEGER PRIMARY KEY,
                state_value TEXT NOT NULL,
                FOREIGN KEY (member_id) REFERENCES member(member_id)
            )
            """,
            """
            CREATE TABLE member_state_b (
                member_id INTEGER PRIMARY KEY,
                state_value TEXT NOT NULL,
                FOREIGN KEY (member_id) REFERENCES member(member_id)
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO member VALUES (?, ?)", (1, "member-a")),
            ("INSERT INTO member VALUES (?, ?)", (2, "member-b")),
            ("INSERT INTO member VALUES (?, ?)", (3, "member-c")),
            ("INSERT INTO member_state_a VALUES (?, ?)", (1, "gold")),
            ("INSERT INTO member_state_a VALUES (?, ?)", (2, "gold")),
            ("INSERT INTO member_state_b VALUES (?, ?)", (2, "gold")),
            ("INSERT INTO member_state_b VALUES (?, ?)", (3, "gold")),
        ),
    ),
    FixtureSpec(
        fixture_id="F06_POLYMORPHIC_DISCRIMINATOR",
        category="polymorphic_discriminator",
        diagnostic_capabilities=(
            "schema_linking",
            "value_grounding",
            "polymorphic_discriminator",
        ),
        expected_category="supported",
        expected_schema_signature=(
            ("account_entity", ("account_id", "account_label"), ()),
            (
                "activity_record",
                ("activity_id", "owner_kind", "owner_ref", "activity_value"),
                (),
            ),
            ("team_entity", ("team_id", "team_label"), ()),
        ),
        ddl=(
            """
            CREATE TABLE account_entity (
                account_id INTEGER PRIMARY KEY,
                account_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE team_entity (
                team_id INTEGER PRIMARY KEY,
                team_label TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE activity_record (
                activity_id INTEGER PRIMARY KEY,
                owner_kind TEXT NOT NULL,
                owner_ref INTEGER NOT NULL,
                activity_value REAL NOT NULL
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO account_entity VALUES (?, ?)", (1, "account-a")),
            ("INSERT INTO account_entity VALUES (?, ?)", (2, "account-b")),
            ("INSERT INTO team_entity VALUES (?, ?)", (1, "team-a")),
            ("INSERT INTO team_entity VALUES (?, ?)", (2, "team-b")),
            (
                "INSERT INTO activity_record VALUES (?, ?, ?, ?)",
                (1, "account", 1, 12.0),
            ),
            (
                "INSERT INTO activity_record VALUES (?, ?, ?, ?)",
                (2, "account", 2, 8.0),
            ),
            (
                "INSERT INTO activity_record VALUES (?, ?, ?, ?)",
                (3, "team", 1, 5.0),
            ),
            (
                "INSERT INTO activity_record VALUES (?, ?, ?, ?)",
                (4, "team", 2, 3.0),
            ),
        ),
        semantic_expectations=(
            SemanticExpectation(
                source_semantic="activity value",
                description="Recorded value of one activity.",
                expected_binding_kind="physical_column",
                evidence_requirements=("schema_column",),
            ),
            SemanticExpectation(
                source_semantic="account owners",
                description=(
                    "Account-type owners; overlapping owner identifiers require "
                    "type evidence."
                ),
                expected_binding_kind="discriminator_value",
                evidence_requirements=(
                    "schema_columns",
                    "exact_discriminator_value",
                    "relationship_shape",
                ),
            ),
        ),
    ),
    FixtureSpec(
        fixture_id="F07_DERIVED_METRIC",
        category="derived_metric_document",
        diagnostic_capabilities=("schema_linking", "schema_document"),
        expected_category="supported",
        expected_schema_signature=(
            (
                "measure_record",
                ("record_id", "gross_value", "expense_value"),
                (),
            ),
        ),
        ddl=(
            """
            CREATE TABLE measure_record (
                record_id INTEGER PRIMARY KEY,
                gross_value REAL NOT NULL,
                expense_value REAL NOT NULL
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO measure_record VALUES (?, ?, ?)", (1, 12.0, 5.0)),
            ("INSERT INTO measure_record VALUES (?, ?, ?)", (2, 9.5, 4.0)),
            ("INSERT INTO measure_record VALUES (?, ?, ?)", (3, 6.0, 8.0)),
        ),
        semantic_expectations=(
            SemanticExpectation(
                source_semantic="net contribution",
                description="Net contribution equals gross value minus expense value.",
                expected_binding_kind="derived_expression",
                evidence_requirements=("schema_columns", "full_schema_document"),
                expected_expression="gross_value - expense_value",
            ),
        ),
        schema_documents=(
            SchemaDocumentFixture(
                document_id="net-contribution-rule",
                namespace="main",
                source_version="1",
                content="gross_value - expense_value",
            ),
        ),
    ),
    FixtureSpec(
        fixture_id="F08_DATE_STORED_AS_TEXT",
        category="text_date",
        diagnostic_capabilities=(
            "schema_linking",
            "value_grounding",
            "text_date",
        ),
        expected_category="supported",
        expected_schema_signature=(
            (
                "visit_record",
                ("visit_id", "occurred_on", "subject_label"),
                (),
            ),
        ),
        ddl=(
            """
            CREATE TABLE visit_record (
                visit_id INTEGER PRIMARY KEY,
                occurred_on TEXT NOT NULL,
                subject_label TEXT NOT NULL
            )
            """,
        ),
        seed_data=(
            (
                "INSERT INTO visit_record VALUES (?, ?, ?)",
                (1, "2025-12-31", "subject-a"),
            ),
            (
                "INSERT INTO visit_record VALUES (?, ?, ?)",
                (2, "2026-1-2", "subject-b"),
            ),
            (
                "INSERT INTO visit_record VALUES (?, ?, ?)",
                (3, "2026/01/15", "subject-c"),
            ),
            (
                "INSERT INTO visit_record VALUES (?, ?, ?)",
                (4, "2026-01-31", "subject-d"),
            ),
            (
                "INSERT INTO visit_record VALUES (?, ?, ?)",
                (5, "2026-02-01", "subject-e"),
            ),
        ),
        semantic_expectations=(
            SemanticExpectation(
                source_semantic="visits in January 2026",
                description=(
                    "Visit records whose stored calendar date falls in January 2026."
                ),
                expected_binding_kind="physical_column",
                evidence_requirements=("schema_column", "sample_text_dates"),
            ),
        ),
    ),
    FixtureSpec(
        fixture_id="F09_UNSUPPORTED_QUESTION",
        category="unsupported_semantic",
        diagnostic_capabilities=("schema_linking", "unsupported_semantic"),
        expected_category="unsupported",
        expected_schema_signature=(
            (
                "shipment_record",
                ("shipment_id", "route_label", "parcel_count"),
                (),
            ),
        ),
        ddl=(
            """
            CREATE TABLE shipment_record (
                shipment_id INTEGER PRIMARY KEY,
                route_label TEXT NOT NULL,
                parcel_count INTEGER NOT NULL
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO shipment_record VALUES (?, ?, ?)", (1, "route-a", 4)),
            ("INSERT INTO shipment_record VALUES (?, ?, ?)", (2, "route-b", 7)),
        ),
        semantic_expectations=(
            SemanticExpectation(
                source_semantic="shipment carbon emissions",
                description=(
                    "Carbon-emission semantics are absent from the available schema "
                    "and schema documents."
                ),
                expected_binding_kind=None,
                evidence_requirements=(
                    "complete_schema_catalog",
                    "candidate_column_inspection",
                    "no_schema_document",
                ),
            ),
        ),
    ),
    FixtureSpec(
        fixture_id="F10_SAFE_EMPTY_RESULT",
        category="empty_result",
        diagnostic_capabilities=("schema_linking", "empty_result"),
        expected_category="supported",
        expected_schema_signature=(
            (
                "stock_record",
                ("record_id", "item_label", "quantity"),
                (),
            ),
        ),
        ddl=(
            """
            CREATE TABLE stock_record (
                record_id INTEGER PRIMARY KEY,
                item_label TEXT NOT NULL,
                quantity INTEGER NOT NULL
            )
            """,
        ),
        seed_data=(
            ("INSERT INTO stock_record VALUES (?, ?, ?)", (1, "item-a", 0)),
            ("INSERT INTO stock_record VALUES (?, ?, ?)", (2, "item-b", 3)),
            ("INSERT INTO stock_record VALUES (?, ?, ?)", (3, "item-c", 8)),
        ),
        semantic_expectations=(
            SemanticExpectation(
                source_semantic="item-b with a zero quantity",
                description=(
                    "Two individually observed values whose conjunction has no matching "
                    "row; a valid typed filter can still return an empty result."
                ),
                expected_binding_kind="discriminator_value",
                evidence_requirements=("schema_column", "exact_values", "empty_result"),
            ),
        ),
    ),
)

_FIXTURES_BY_ID = {spec.fixture_id: spec for spec in _FIXTURES}
FIXTURE_IDS = tuple(spec.fixture_id for spec in _FIXTURES)
DDL_ORDERS = ("canonical", "reversed")


def get_fixture_spec(fixture_id: str) -> FixtureSpec:
    """Return the immutable specification for one public fixture ID."""
    if not isinstance(fixture_id, str):
        raise ValueError(f"unknown adaptive SQLite fixture: {fixture_id!r}")
    try:
        return _FIXTURES_BY_ID[fixture_id]
    except KeyError as exc:
        raise ValueError(f"unknown adaptive SQLite fixture: {fixture_id!r}") from exc


def create_sqlite_adaptive_fixture(
    fixture_id: str,
    target: str | Path,
    *,
    ddl_order: str = "canonical",
) -> Path:
    """Atomically create one seeded fixture at a previously absent path."""
    spec = get_fixture_spec(fixture_id)
    if not isinstance(ddl_order, str) or ddl_order not in DDL_ORDERS:
        raise ValueError(f"unknown DDL order: {ddl_order!r}")

    target_path = Path(target)
    if target_path.exists():
        raise FileExistsError(f"fixture target already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    statements = spec.ddl if ddl_order == "canonical" else tuple(reversed(spec.ddl))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=target_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _create_database(temporary_path, statements, spec.seed_data)
        os.link(temporary_path, target_path)
        temporary_path.unlink()
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return target_path


def _create_database(
    path: Path,
    statements: tuple[str, ...],
    seed_data: tuple[tuple[str, tuple[object, ...]], ...],
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in statements:
            connection.execute(statement)
        for statement, parameters in seed_data:
            connection.execute(statement, parameters)
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {integrity!r}")
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_violations:
            raise RuntimeError(
                f"SQLite foreign key check failed: {foreign_key_violations!r}"
            )
    finally:
        connection.close()
