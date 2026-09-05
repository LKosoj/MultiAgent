"""Tests for the W3-3.2 code<->label cascade hint (shadow-mode feature)."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any

import pytest

from custom_tools.text_to_sql.adaptive.code_label_cascade import (
    CascadeCandidate,
    _fk_lookup_candidates,
    _sibling_label_columns,
    cascade_hint_mode,
    code_label_cascade_hints,
)
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    ColumnRef,
    EvidenceCost,
    ExpectedResultShape,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.serialization import (
    ArtifactReference,
    SerializationLimits,
    canonical_json_bytes,
)

RUN = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SCHEMA = "sha256:" + "a" * 64


def _table(name: str) -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def _column(table: str, column: str) -> ColumnRef:
    return ColumnRef(table=_table(table), column=column)


def _budget() -> BudgetState:
    return BudgetState(
        **{
            f"{stage}_{name}": value
            for name in (
                "wall_clock_ms",
                "model_calls",
                "model_tokens",
                "db_probe_ms",
                "rows",
                "bytes",
            )
            for stage, value in (("initial", 10), ("used", 0), ("remaining", 10))
        }
    )


def _state(evidence: tuple[Any, ...] = ()) -> ResearchState:
    query = QuerySpec(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_id="query-1",
        original_text="find Moscow",
        semantic_items=(),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=evidence,
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=(),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


class _ArtifactStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def write(self, payload: bytes) -> ArtifactReference:
        reference = ArtifactReference(
            artifact_id=f"artifact-{len(self.data) + 1}",
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            byte_count=len(payload),
        )
        self.data[reference.artifact_id] = payload
        return reference

    def read(self, reference: ArtifactReference) -> bytes:
        return self.data[reference.artifact_id]


def _search_value_evidence(
    column: ColumnRef,
    payload: dict[str, object],
    *,
    row_count: int,
    evidence_id: str = "search-evidence",
    kind: ResearchActionKind = ResearchActionKind.SEARCH_VALUE,
    write_artifact=None,
    read_artifact=None,
    limits: SerializationLimits = SerializationLimits(),
):
    """One real evidence record for a value-search-shaped probe.

    Built through the production probe-result -> evidence conversion so the
    canonical provenance envelope the cascade trigger reads is trustworthy,
    never a hand-rolled observation string.
    """
    digest = canonical_action_digest(
        kind=kind,
        hypothesis_id=None,
        target=column,
        parameters=(),
        expected_revision=0,
    )
    action = ResearchAction(
        action_id=f"action-{evidence_id}",
        kind=kind,
        hypothesis_id=None,
        target=column,
        parameters=(),
        action_digest=digest,
        expected_revision=0,
    )
    raw = canonical_json_bytes(payload)
    result = build_probe_result(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        invocation_id=evidence_id,
        action_digest=digest,
        probe_kind=kind,
        status=ProbeStatus.SUCCESS,
        target=column,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, tzinfo=UTC),
        summary="value search",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=row_count,
            bytes=len(raw),
        ),
        row_count=row_count,
        payload=payload,
        limits=limits,
        write_artifact=write_artifact,
        read_artifact=read_artifact,
    )
    evidence = probe_result_to_evidence(result, action, read_artifact=read_artifact)
    assert evidence is not None
    return evidence


_FK_SCHEMA: dict[str, Any] = {
    "orders": {
        "columns": {
            "region_code": {
                "type": "VARCHAR",
                "constraint_type": "FK",
                "references": "regions.id",
            },
        },
    },
    "regions": {
        "columns": {
            "id": {"type": "INT", "is_primary_key": True},
            "name": {"type": "VARCHAR"},
            "population": {"type": "INTEGER"},
        },
    },
}


def test_trigger_fires_for_empty_label_search_and_proposes_fk_lookup() -> None:
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    hints = code_label_cascade_hints(state, _FK_SCHEMA)

    assert hints == (
        CascadeCandidate(table="regions", column="name", reason="fk_lookup"),
    )


def test_fk_lookup_only_includes_string_columns() -> None:
    """`population` (integer) must never be proposed as a label lookup."""
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    hints = code_label_cascade_hints(state, _FK_SCHEMA)

    assert all(candidate.column != "population" for candidate in hints)


def test_fk_lookup_excludes_the_fk_target_column_itself() -> None:
    """A string-typed FK target column (e.g. an OKTMO-style code PK) must
    never be proposed as its own label lookup -- only genuinely different
    string columns on the target table qualify."""
    schema: dict[str, Any] = {
        "orders": {
            "columns": {
                "region_code": {
                    "type": "VARCHAR",
                    "constraint_type": "FK",
                    "references": "regions.code",
                },
            },
        },
        "regions": {
            "columns": {
                "code": {"type": "VARCHAR", "is_primary_key": True},
                "name": {"type": "VARCHAR"},
            },
        },
    }
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    hints = code_label_cascade_hints(state, schema)

    assert hints == (
        CascadeCandidate(table="regions", column="name", reason="fk_lookup"),
    )


def test_fk_lookup_candidates_excludes_all_composite_fk_target_columns() -> None:
    """W3: a composite FK (``column_pairs`` with 2 pairs) must exclude
    *both* ``to_column`` targets from the label-lookup candidates -- not
    just the first pair's, which would wrongly propose a code column
    (the other half of the composite key) as a human-readable label."""
    schema: dict[str, Any] = {
        "orders": {
            "columns": {
                "region_code": {"type": "VARCHAR"},
                "district_code": {"type": "VARCHAR"},
            },
        },
        "regions": {
            "columns": {
                "region_code": {"type": "VARCHAR", "is_primary_key": True},
                "district_code": {"type": "VARCHAR", "is_primary_key": True},
                "name": {"type": "VARCHAR"},
            },
        },
    }
    edge = {
        "column_pairs": [
            {"from_column": "region_code", "to_column": "region_code"},
            {"from_column": "district_code", "to_column": "district_code"},
        ],
        "constraint_id": "fk1",
        "from_table": "orders",
        "metadata_format": "constraint",
        "relationship_kind": "declared",
        "to_table": "regions",
    }

    candidates = _fk_lookup_candidates([edge], schema, "orders", "region_code")

    assert candidates == (
        CascadeCandidate(table="regions", column="name", reason="fk_lookup"),
    )
    assert all(
        candidate.column not in ("region_code", "district_code")
        for candidate in candidates
    )


def test_no_trigger_when_rows_are_present() -> None:
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [["RU-MOW"]], "requested_value": "Moscow"},
        row_count=1,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_no_trigger_for_distinct_values_probe_kind() -> None:
    """Only SEARCH_VALUE triggers; DISTINCT_VALUES never does, even with a
    (synthetic, non-production) requested_value key and zero rows."""
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
        kind=ResearchActionKind.DISTINCT_VALUES,
        evidence_id="distinct-evidence",
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_no_trigger_for_non_string_requested_value() -> None:
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": 42},
        row_count=0,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_no_trigger_for_empty_or_blank_requested_value() -> None:
    column = _column("orders", "region_code")
    for blank in ("", "   "):
        evidence = _search_value_evidence(
            column,
            {"columns": ["region_code"], "rows": [], "requested_value": blank},
            row_count=0,
        )
        state = _state((evidence,))

        assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_no_trigger_for_missing_requested_value_key() -> None:
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": []},
        row_count=0,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_no_trigger_for_artifact_storage() -> None:
    """Externalized (non-inline) observations never trigger the cascade."""
    store = _ArtifactStore()
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {
            "columns": ["region_code"],
            "rows": [],
            "requested_value": "Moscow",
            "padding": "x" * 70_000,
        },
        row_count=0,
        limits=SerializationLimits(max_state_bytes=500_000, max_inline_rows=10),
        write_artifact=store.write,
        read_artifact=store.read,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_unrelated_evidence_kinds_are_ignored() -> None:
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"status": "matched", "column": column.model_dump(mode="json", by_alias=True)},
        row_count=1,
        kind=ResearchActionKind.INSPECT_COLUMN,
        evidence_id="inspect-evidence",
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_sibling_label_candidates_both_directions() -> None:
    column = _column("regions", "region_name")
    schema = {
        "regions": {
            "columns": {
                "region_name": {"type": "VARCHAR"},
                "region_code": {"type": "VARCHAR"},
            },
        },
    }
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_name"], "rows": [], "requested_value": "42"},
        row_count=0,
    )
    state = _state((evidence,))

    hints = code_label_cascade_hints(state, schema)

    assert hints == (
        CascadeCandidate(table="regions", column="region_code", reason="sibling_label"),
    )


def test_sibling_label_columns_suffix_swap() -> None:
    assert _sibling_label_columns(
        "region_code", ["region_code", "region_name", "other"]
    ) == ("region_name",)
    assert _sibling_label_columns(
        "status_name", ["status_name", "status_code"]
    ) == ("status_code",)


def test_sibling_label_columns_bare_name_title_fallback() -> None:
    assert _sibling_label_columns("region_code", ["region_code", "name"]) == ("name",)
    assert _sibling_label_columns("region_id", ["region_id", "title"]) == ("title",)


def test_sibling_label_columns_no_match_returns_empty() -> None:
    assert _sibling_label_columns("unrelated", ["region_code", "region_name"]) == ()
    assert _sibling_label_columns("region_code", ["region_code"]) == ()


def test_sibling_label_columns_never_matches_itself() -> None:
    """A column literally named e.g. ``name`` must not propose itself."""
    assert _sibling_label_columns("name", ["name"]) == ()


def test_dedup_and_sort_and_top_n_cap() -> None:
    schema = {
        "orders": {
            "columns": {
                "region_code": {
                    "type": "VARCHAR",
                    "constraint_type": "FK",
                    "references": "regions.id",
                },
            },
        },
        "regions": {
            "columns": {
                "id": {"type": "INT", "is_primary_key": True},
                "alpha_name": {"type": "VARCHAR"},
                "beta_label": {"type": "TEXT"},
                "gamma_title": {"type": "CHAR"},
                "delta_desc": {"type": "VARCHAR"},
            },
        },
    }
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    hints = code_label_cascade_hints(state, schema)

    assert len(hints) == 3
    assert hints == tuple(sorted(hints, key=lambda c: (c.table, c.column, c.reason)))
    assert len(set(hints)) == len(hints)


def test_incoming_fk_edge_is_not_treated_as_lookup() -> None:
    """An FK pointing *into* the trigger table/column must not produce a
    candidate; only FKs declared *on* the trigger column count."""
    schema = {
        "regions": {
            "columns": {
                "region_code": {"type": "VARCHAR"},
            },
        },
        "orders": {
            "columns": {
                "region_code": {
                    "type": "VARCHAR",
                    "constraint_type": "FK",
                    "references": "regions.region_code",
                },
            },
        },
    }
    column = _column("regions", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, schema) == ()


def test_unresolvable_table_or_column_yields_no_candidates() -> None:
    column = _column("ghost_table", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    assert code_label_cascade_hints(state, _FK_SCHEMA) == ()


def test_code_label_cascade_hints_rejects_wrong_types() -> None:
    with pytest.raises(TypeError, match="ResearchState"):
        code_label_cascade_hints(object(), _FK_SCHEMA)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Mapping"):
        code_label_cascade_hints(_state(), object())  # type: ignore[arg-type]


def test_cascade_hint_mode_defaults_to_shadow_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT", raising=False)
    assert cascade_hint_mode() == "shadow"

    monkeypatch.setenv("TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT", "on")
    assert cascade_hint_mode() == "on"

    monkeypatch.setenv("TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT", "off")
    assert cascade_hint_mode() == "off"

    monkeypatch.setenv("TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT", "ON")
    assert cascade_hint_mode() == "on"

    for garbage in ("", "maybe", "1", "true"):
        monkeypatch.setenv("TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT", garbage)
        assert cascade_hint_mode() == "shadow"


def test_code_label_cascade_hints_reuses_relationship_edges_cache_by_version_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls sharing one ``version_key`` must hit schema_probes's bounded
    relationship-edges cache instead of recomputing edges each time."""
    from custom_tools.text_to_sql.adaptive import schema_probes as schema_probes_module

    calls = {"count": 0}
    original_relationship_edges = schema_probes_module._relationship_edges

    def counting_relationship_edges(schema: Any) -> Any:
        calls["count"] += 1
        return original_relationship_edges(schema)

    monkeypatch.setattr(
        schema_probes_module, "_relationship_edges", counting_relationship_edges
    )

    version_key = "cascade-cache-test-" + hashlib.sha256(b"cascade-cache").hexdigest()
    column = _column("orders", "region_code")
    evidence = _search_value_evidence(
        column,
        {"columns": ["region_code"], "rows": [], "requested_value": "Moscow"},
        row_count=0,
    )
    state = _state((evidence,))

    first = code_label_cascade_hints(state, _FK_SCHEMA, version_key=version_key)
    second = code_label_cascade_hints(state, _FK_SCHEMA, version_key=version_key)

    assert first == second == (
        CascadeCandidate(table="regions", column="name", reason="fk_lookup"),
    )
    assert calls["count"] == 1
