"""Contracts for seeded adaptive Text-to-SQL SQLite fixtures."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from tests.fixtures.text_to_sql_adaptive import sqlite as adaptive_sqlite


EXPECTED_IDS = (
    "F01_CONVENTIONAL_STAR",
    "F02_VERTICAL_EAV",
    "F03_OPAQUE_NAMES",
    "F04_MISSING_DECLARED_FK",
    "F05_AMBIGUOUS_BINDING",
    "F06_POLYMORPHIC_DISCRIMINATOR",
    "F07_DERIVED_METRIC",
    "F08_DATE_STORED_AS_TEXT",
    "F09_UNSUPPORTED_QUESTION",
    "F10_SAFE_EMPTY_RESULT",
)


def _snapshot(path: Path) -> tuple[dict[str, str], dict[str, list[tuple[object, ...]]]]:
    with sqlite3.connect(path) as connection:
        tables = {
            str(name): str(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        rows = {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    return tables, rows


def _foreign_keys(path: Path, table: str) -> tuple[tuple[str, str], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            (str(row[3]), str(row[2]))
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        )


def _schema_signature(
    path: Path,
) -> tuple[tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]], ...]:
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return tuple(
            (
                table,
                tuple(
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ),
                tuple(
                    (str(row[3]), str(row[2]))
                    for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                ),
            )
            for table in tables
        )


def test_public_fixture_specs_are_exact_and_stable() -> None:
    assert adaptive_sqlite.FIXTURE_IDS == EXPECTED_IDS
    assert adaptive_sqlite.DDL_ORDERS == ("canonical", "reversed")
    expected_metadata = {
        "F01_CONVENTIONAL_STAR": {
            "category": "conventional_star",
            "diagnostic_capabilities": ("schema_linking", "value_grounding"),
            "expected_category": "supported",
            "expected_schema_signature": (
                ("branch_dim", ("branch_id", "branch_label"), ()),
                ("day_dim", ("day_id", "day_label"), ()),
                (
                    "sales_fact",
                    ("sale_id", "branch_id", "day_id", "sale_value"),
                    (("day_id", "day_dim"), ("branch_id", "branch_dim")),
                ),
            ),
        },
        "F02_VERTICAL_EAV": {
            "category": "vertical_eav",
            "diagnostic_capabilities": ("schema_linking", "value_grounding"),
            "expected_category": "supported",
            "expected_schema_signature": (
                (
                    "attribute_fact",
                    ("member_id", "attribute_id", "value_text"),
                    (("attribute_id", "attribute_kind"), ("member_id", "member")),
                ),
                ("attribute_kind", ("attribute_id", "attribute_key"), ()),
                ("member", ("member_id", "member_label"), ()),
            ),
        },
        "F03_OPAQUE_NAMES": {
            "category": "opaque_names",
            "diagnostic_capabilities": ("schema_linking", "value_grounding"),
            "expected_category": "supported",
            "expected_schema_signature": (
                ("a17", ("k0", "v1"), ()),
                ("b29", ("k2", "v3"), ()),
                (
                    "c31",
                    ("k4", "u5", "w6", "x7"),
                    (("w6", "b29"), ("u5", "a17")),
                ),
            ),
        },
        "F04_MISSING_DECLARED_FK": {
            "category": "missing_declared_fk",
            "diagnostic_capabilities": ("missing_declared_fk",),
            "expected_category": "supported",
            "expected_schema_signature": (
                ("depot", ("depot_id", "depot_label"), ()),
                ("ledger", ("ledger_id", "depot_ref", "entry_value"), ()),
            ),
        },
        "F05_AMBIGUOUS_BINDING": {
            "category": "ambiguous_binding",
            "diagnostic_capabilities": ("ambiguous_binding",),
            "expected_category": "ambiguous",
            "expected_schema_signature": (
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
        },
        "F06_POLYMORPHIC_DISCRIMINATOR": {
            "category": "polymorphic_discriminator",
            "diagnostic_capabilities": (
                "schema_linking",
                "value_grounding",
                "polymorphic_discriminator",
            ),
            "expected_category": "supported",
            "expected_schema_signature": (
                ("account_entity", ("account_id", "account_label"), ()),
                (
                    "activity_record",
                    ("activity_id", "owner_kind", "owner_ref", "activity_value"),
                    (),
                ),
                ("team_entity", ("team_id", "team_label"), ()),
            ),
        },
        "F07_DERIVED_METRIC": {
            "category": "derived_metric_document",
            "diagnostic_capabilities": ("schema_linking", "schema_document"),
            "expected_category": "supported",
            "expected_schema_signature": (
                (
                    "measure_record",
                    ("record_id", "gross_value", "expense_value"),
                    (),
                ),
            ),
        },
        "F08_DATE_STORED_AS_TEXT": {
            "category": "text_date",
            "diagnostic_capabilities": (
                "schema_linking",
                "value_grounding",
                "text_date",
            ),
            "expected_category": "supported",
            "expected_schema_signature": (
                (
                    "visit_record",
                    ("visit_id", "occurred_on", "subject_label"),
                    (),
                ),
            ),
        },
        "F09_UNSUPPORTED_QUESTION": {
            "category": "unsupported_semantic",
            "diagnostic_capabilities": (
                "schema_linking",
                "unsupported_semantic",
            ),
            "expected_category": "unsupported",
            "expected_schema_signature": (
                (
                    "shipment_record",
                    ("shipment_id", "route_label", "parcel_count"),
                    (),
                ),
            ),
        },
        "F10_SAFE_EMPTY_RESULT": {
            "category": "empty_result",
            "diagnostic_capabilities": ("schema_linking", "empty_result"),
            "expected_category": "supported",
            "expected_schema_signature": (
                (
                    "stock_record",
                    ("record_id", "item_label", "quantity"),
                    (),
                ),
            ),
        },
    }
    assert {
        fixture_id: {
            "category": spec.category,
            "diagnostic_capabilities": spec.diagnostic_capabilities,
            "expected_category": spec.expected_category,
            "expected_schema_signature": spec.expected_schema_signature,
        }
        for fixture_id in adaptive_sqlite.FIXTURE_IDS
        for spec in (adaptive_sqlite.get_fixture_spec(fixture_id),)
    } == expected_metadata
    assert {
        spec.expected_category
        for spec in map(adaptive_sqlite.get_fixture_spec, adaptive_sqlite.FIXTURE_IDS)
    } <= {"supported", "ambiguous", "unsupported"}


def test_wave4_fixture_semantics_and_evidence_requirements_are_explicit() -> None:
    expected = {
        "F06_POLYMORPHIC_DISCRIMINATOR": {
            "activity value": {
                "description": "Recorded value of one activity.",
                "expected_binding_kind": "physical_column",
                "evidence_requirements": ("schema_column",),
            },
            "account owners": {
                "description": (
                    "Account-type owners; overlapping owner identifiers require "
                    "type evidence."
                ),
                "expected_binding_kind": "discriminator_value",
                "evidence_requirements": (
                    "schema_columns",
                    "exact_discriminator_value",
                    "relationship_shape",
                ),
            },
        },
        "F07_DERIVED_METRIC": {
            "net contribution": {
                "description": (
                    "Net contribution equals gross value minus expense value."
                ),
                "expected_binding_kind": "derived_expression",
                "evidence_requirements": (
                    "schema_columns",
                    "full_schema_document",
                ),
            },
        },
        "F08_DATE_STORED_AS_TEXT": {
            "visits in January 2026": {
                "description": (
                    "Visit records whose stored calendar date falls in January 2026."
                ),
                "expected_binding_kind": "physical_column",
                "evidence_requirements": ("schema_column", "sample_text_dates"),
            },
        },
        "F09_UNSUPPORTED_QUESTION": {
            "shipment carbon emissions": {
                "description": (
                    "Carbon-emission semantics are absent from the available schema "
                    "and schema documents."
                ),
                "expected_binding_kind": None,
                "evidence_requirements": (
                    "complete_schema_catalog",
                    "candidate_column_inspection",
                    "no_schema_document",
                ),
            },
        },
        "F10_SAFE_EMPTY_RESULT": {
            "items with negative quantity": {
                "description": (
                    "Stock items whose quantity is below zero; a valid binding remains "
                    "supported even when no row matches."
                ),
                "expected_binding_kind": "physical_column",
                "evidence_requirements": ("schema_column", "empty_result"),
            },
        },
    }

    actual = {
        fixture_id: {
            item.source_semantic: {
                "description": item.description,
                "expected_binding_kind": item.expected_binding_kind,
                "evidence_requirements": item.evidence_requirements,
            }
            for item in adaptive_sqlite.get_fixture_spec(
                fixture_id
            ).semantic_expectations
        }
        for fixture_id in expected
    }

    assert actual == expected


def test_derived_metric_document_is_exact_full_content() -> None:
    spec = adaptive_sqlite.get_fixture_spec("F07_DERIVED_METRIC")

    assert len(spec.schema_documents) == 1
    document = spec.schema_documents[0]
    assert document.document_id == "net-contribution-rule"
    assert document.namespace == "main"
    assert document.source_version == "1"
    assert document.content == "gross_value - expense_value"
    assert document.content == spec.semantic_expectations[0].expected_expression


def test_unsupported_and_empty_result_are_distinct_contracts() -> None:
    unsupported = adaptive_sqlite.get_fixture_spec("F09_UNSUPPORTED_QUESTION")
    empty = adaptive_sqlite.get_fixture_spec("F10_SAFE_EMPTY_RESULT")

    assert unsupported.expected_category == "unsupported"
    assert unsupported.semantic_expectations[0].expected_binding_kind is None
    assert unsupported.schema_documents == ()
    assert empty.expected_category == "supported"
    assert empty.semantic_expectations[0].expected_binding_kind == "physical_column"
    assert "empty_result" in empty.semantic_expectations[0].evidence_requirements


@pytest.mark.parametrize("fixture_id", EXPECTED_IDS)
def test_fixture_data_snapshot_is_identical_for_canonical_and_reversed_ddl(
    tmp_path: Path,
    fixture_id: str,
) -> None:
    canonical = adaptive_sqlite.create_sqlite_adaptive_fixture(
        fixture_id,
        tmp_path / f"{fixture_id}-canonical.sqlite",
    )
    reversed_ddl = adaptive_sqlite.create_sqlite_adaptive_fixture(
        fixture_id,
        tmp_path / f"{fixture_id}-reversed.sqlite",
        ddl_order="reversed",
    )

    assert _snapshot(canonical) == _snapshot(reversed_ddl)
    assert (
        _schema_signature(canonical)
        == adaptive_sqlite.get_fixture_spec(fixture_id).expected_schema_signature
    )
    assert (
        _schema_signature(reversed_ddl)
        == adaptive_sqlite.get_fixture_spec(fixture_id).expected_schema_signature
    )


def test_fixture_seeded_content_covers_the_five_planned_invariants(
    tmp_path: Path,
) -> None:
    paths = {
        fixture_id: adaptive_sqlite.create_sqlite_adaptive_fixture(
            fixture_id,
            tmp_path / f"{fixture_id}.sqlite",
        )
        for fixture_id in EXPECTED_IDS[:5]
    }

    star_tables, star_rows = _snapshot(paths["F01_CONVENTIONAL_STAR"])
    assert set(star_tables) == {"branch_dim", "day_dim", "sales_fact"}
    assert len(star_rows["sales_fact"]) == 3
    assert _foreign_keys(paths["F01_CONVENTIONAL_STAR"], "sales_fact") == (
        ("day_id", "day_dim"),
        ("branch_id", "branch_dim"),
    )

    eav_tables, eav_rows = _snapshot(paths["F02_VERTICAL_EAV"])
    assert set(eav_tables) == {"attribute_fact", "attribute_kind", "member"}
    assert (1, "membership_level") in eav_rows["attribute_kind"]
    assert (1, 1, "gold") in eav_rows["attribute_fact"]

    opaque_tables, opaque_rows = _snapshot(paths["F03_OPAQUE_NAMES"])
    assert set(opaque_tables) == {"a17", "b29", "c31"}
    assert (1, "member") in opaque_rows["a17"]
    assert (1, "membership_level") in opaque_rows["b29"]
    assert (1, 1, 1, "gold") in opaque_rows["c31"]

    missing_declared_fk_tables, missing_declared_fk_rows = _snapshot(
        paths["F04_MISSING_DECLARED_FK"]
    )
    assert set(missing_declared_fk_tables) == {"depot", "ledger"}
    assert _foreign_keys(paths["F04_MISSING_DECLARED_FK"], "ledger") == ()
    assert {row[1] for row in missing_declared_fk_rows["ledger"]} <= {
        row[0] for row in missing_declared_fk_rows["depot"]
    }

    ambiguous_tables, ambiguous_rows = _snapshot(paths["F05_AMBIGUOUS_BINDING"])
    assert set(ambiguous_tables) == {"member", "member_state_a", "member_state_b"}
    gold_a = {row[0] for row in ambiguous_rows["member_state_a"] if row[1] == "gold"}
    gold_b = {row[0] for row in ambiguous_rows["member_state_b"] if row[1] == "gold"}
    assert gold_a == {1, 2}
    assert gold_b == {2, 3}
    assert gold_a != gold_b


def test_fixture_seeded_content_covers_the_wave4_expansion(
    tmp_path: Path,
) -> None:
    paths = {
        fixture_id: adaptive_sqlite.create_sqlite_adaptive_fixture(
            fixture_id,
            tmp_path / f"{fixture_id}.sqlite",
        )
        for fixture_id in EXPECTED_IDS[5:]
    }

    polymorphic_tables, polymorphic_rows = _snapshot(
        paths["F06_POLYMORPHIC_DISCRIMINATOR"]
    )
    assert set(polymorphic_tables) == {
        "account_entity",
        "activity_record",
        "team_entity",
    }
    assert {row[0] for row in polymorphic_rows["account_entity"]} == {1, 2}
    assert {row[0] for row in polymorphic_rows["team_entity"]} == {1, 2}
    assert {(row[1], row[2]) for row in polymorphic_rows["activity_record"]} == {
        ("account", 1),
        ("account", 2),
        ("team", 1),
        ("team", 2),
    }
    assert (
        _foreign_keys(paths["F06_POLYMORPHIC_DISCRIMINATOR"], "activity_record") == ()
    )

    derived_tables, derived_rows = _snapshot(paths["F07_DERIVED_METRIC"])
    assert set(derived_tables) == {"measure_record"}
    assert derived_rows["measure_record"] == [
        (1, 12.0, 5.0),
        (2, 9.5, 4.0),
        (3, 6.0, 8.0),
    ]

    text_date_tables, text_date_rows = _snapshot(paths["F08_DATE_STORED_AS_TEXT"])
    assert set(text_date_tables) == {"visit_record"}
    assert "occurred_on TEXT NOT NULL" in text_date_tables["visit_record"]
    assert [row[1] for row in text_date_rows["visit_record"]] == [
        "2025-12-31",
        "2026-1-2",
        "2026/01/15",
        "2026-01-31",
        "2026-02-01",
    ]
    assert all(isinstance(row[1], str) for row in text_date_rows["visit_record"])

    unsupported_tables, unsupported_rows = _snapshot(paths["F09_UNSUPPORTED_QUESTION"])
    assert set(unsupported_tables) == {"shipment_record"}
    assert unsupported_rows["shipment_record"] == [
        (1, "route-a", 4),
        (2, "route-b", 7),
    ]
    assert all(
        "emission" not in column.casefold() and "carbon" not in column.casefold()
        for _, columns, _ in _schema_signature(paths["F09_UNSUPPORTED_QUESTION"])
        for column in columns
    )

    empty_tables, empty_rows = _snapshot(paths["F10_SAFE_EMPTY_RESULT"])
    assert set(empty_tables) == {"stock_record"}
    assert empty_rows["stock_record"] == [
        (1, "item-a", 0),
        (2, "item-b", 3),
        (3, "item-c", 8),
    ]
    assert all(row[2] >= 0 for row in empty_rows["stock_record"])


def test_text_date_fixture_defeats_naive_lexical_range(tmp_path: Path) -> None:
    path = adaptive_sqlite.create_sqlite_adaptive_fixture(
        "F08_DATE_STORED_AS_TEXT",
        tmp_path / "text-date.sqlite",
    )

    with sqlite3.connect(path) as connection:
        stored_rows = connection.execute(
            "SELECT visit_id, occurred_on, typeof(occurred_on) "
            "FROM visit_record ORDER BY visit_id"
        ).fetchall()
        lexical_january_ids = {
            int(row[0])
            for row in connection.execute(
                "SELECT visit_id FROM visit_record "
                "WHERE occurred_on >= '2026-01-01' "
                "AND occurred_on < '2026-02-01'"
            )
        }
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    assert stored_rows == [
        (1, "2025-12-31", "text"),
        (2, "2026-1-2", "text"),
        (3, "2026/01/15", "text"),
        (4, "2026-01-31", "text"),
        (5, "2026-02-01", "text"),
    ]
    intended_january_ids = {2, 3, 4}
    assert lexical_january_ids == {4}
    assert lexical_january_ids != intended_january_ids

    text_date = adaptive_sqlite.get_fixture_spec("F08_DATE_STORED_AS_TEXT")
    unsupported = adaptive_sqlite.get_fixture_spec("F09_UNSUPPORTED_QUESTION")
    assert text_date.expected_category == "supported"
    assert text_date.semantic_expectations[0].expected_binding_kind == "physical_column"
    assert text_date.expected_category != unsupported.expected_category


def test_fixture_instances_are_isolated(tmp_path: Path) -> None:
    first = adaptive_sqlite.create_sqlite_adaptive_fixture(
        "F02_VERTICAL_EAV",
        tmp_path / "first.sqlite",
    )
    second = adaptive_sqlite.create_sqlite_adaptive_fixture(
        "F02_VERTICAL_EAV",
        tmp_path / "second.sqlite",
    )
    with sqlite3.connect(first) as connection:
        connection.execute("DELETE FROM attribute_fact")
        connection.commit()

    assert _snapshot(first)[1]["attribute_fact"] == []
    assert len(_snapshot(second)[1]["attribute_fact"]) == 3


@pytest.mark.parametrize("fixture_id", EXPECTED_IDS[5:])
def test_wave4_fixture_instances_are_isolated(
    tmp_path: Path,
    fixture_id: str,
) -> None:
    first = adaptive_sqlite.create_sqlite_adaptive_fixture(
        fixture_id,
        tmp_path / f"{fixture_id}-first.sqlite",
    )
    second = adaptive_sqlite.create_sqlite_adaptive_fixture(
        fixture_id,
        tmp_path / f"{fixture_id}-second.sqlite",
    )
    first_table = adaptive_sqlite.get_fixture_spec(
        fixture_id
    ).expected_schema_signature[0][0]
    with sqlite3.connect(first) as connection:
        connection.execute(f"DELETE FROM {first_table}")
        connection.commit()

    assert _snapshot(first) != _snapshot(second)


def test_fixture_builder_rejects_unknown_ids_orders_and_existing_targets(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fixture.sqlite"

    for fixture_id in ("unknown", ["F01_CONVENTIONAL_STAR"]):
        with pytest.raises(ValueError, match="unknown adaptive SQLite fixture"):
            adaptive_sqlite.create_sqlite_adaptive_fixture(fixture_id, target)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown DDL order"):
        adaptive_sqlite.create_sqlite_adaptive_fixture(
            "F01_CONVENTIONAL_STAR",
            target,
            ddl_order="random",
        )

    target.write_bytes(b"must remain unchanged")
    with pytest.raises(FileExistsError, match="already exists"):
        adaptive_sqlite.create_sqlite_adaptive_fixture("F01_CONVENTIONAL_STAR", target)
    assert target.read_bytes() == b"must remain unchanged"


def test_fixture_builder_preserves_concurrent_target_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "fixture.sqlite"
    concurrent_bytes = b"concurrent fixture bytes"
    original_link = adaptive_sqlite.os.link

    def publish_concurrently(source: Path, destination: Path) -> None:
        Path(destination).write_bytes(concurrent_bytes)
        original_link(source, destination)

    monkeypatch.setattr(adaptive_sqlite.os, "link", publish_concurrently)
    with pytest.raises(FileExistsError):
        adaptive_sqlite.create_sqlite_adaptive_fixture("F01_CONVENTIONAL_STAR", target)

    assert target.read_bytes() == concurrent_bytes
    assert list(tmp_path.glob(".fixture.sqlite.*.tmp")) == []


def test_interrupted_build_cleans_up_and_a_later_rebuild_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "interrupted.sqlite"
    original_create = adaptive_sqlite._create_database

    def fail_build(
        _path: Path,
        _statements: tuple[str, ...],
        _seed_data: tuple[tuple[str, tuple[object, ...]], ...],
    ) -> None:
        raise RuntimeError("simulated interrupted build")

    monkeypatch.setattr(adaptive_sqlite, "_create_database", fail_build)
    with pytest.raises(RuntimeError, match="simulated interrupted build"):
        adaptive_sqlite.create_sqlite_adaptive_fixture("F01_CONVENTIONAL_STAR", target)
    assert not target.exists()
    assert list(tmp_path.glob(".interrupted.sqlite.*.tmp")) == []

    monkeypatch.setattr(adaptive_sqlite, "_create_database", original_create)
    rebuilt = adaptive_sqlite.create_sqlite_adaptive_fixture(
        "F01_CONVENTIONAL_STAR",
        target,
    )
    assert len(_snapshot(rebuilt)[1]["sales_fact"]) == 3


def test_fixture_source_has_no_public_benchmark_markers() -> None:
    source = Path(adaptive_sqlite.__file__).read_text(encoding="utf-8").lower()
    assert "bi" + "rd" not in source
    assert "spi" + "der" not in source
    assert "mini" + "_dev" not in source
