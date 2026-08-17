from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import multiprocessing
import os
import threading
import time

import pytest

from custom_tools.text_to_sql.adaptive import sql_ast
from custom_tools.text_to_sql.adaptive.sql_ast import (
    ExpressionFact,
    SqlAstError,
    SqlAstErrorCode,
    parse_sql_candidate,
)


POSTGRES_DSN = "postgresql://user:password@localhost/example"


def _parse(sql: str, *, candidate_id: str = "candidate-1"):
    return parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)


def _assert_error(sql: str, code: SqlAstErrorCode) -> None:
    with pytest.raises(SqlAstError) as exc_info:
        _parse(sql)
    assert exc_info.value.code is code
    assert str(exc_info.value)


def test_candidate_wire_roundtrip_requires_both_digests() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse("SELECT o.id FROM orders o")
    wire = _sql_ast_process._candidate_to_wire(parsed)
    decoded = _sql_ast_process._decode_candidate(
        wire,
        limits=AstLimits(max_nodes=10_000, max_depth=128),
    )

    assert decoded == parsed
    missing_source_digest = dict(wire)
    missing_source_digest.pop("source_sql_digest")
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            missing_source_digest,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_v8_wire_requires_exactly_one_join_fact_for_each_joined_relation() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT a.id FROM alpha AS a CROSS JOIN beta AS b")
    )
    missing = json.loads(json.dumps(wire))
    missing["joins"] = []
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            missing, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )

    duplicate = json.loads(json.dumps(wire))
    duplicate_join = list(duplicate["joins"][0])
    duplicate_join[0] = "join:duplicate"
    duplicate["joins"].append(duplicate_join)
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            duplicate, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_natural_inner_join_preserves_shape_digest_and_v9_wire_roundtrip() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_identity import semantic_candidate_digest
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse("SELECT * FROM a NATURAL INNER JOIN b")
    envelope = json.loads(_sql_ast_process._encode_envelope(parsed).decode())
    decoded = _sql_ast_process._decode_envelope(
        json.dumps(envelope).encode(),
        limits=AstLimits(max_nodes=10_000, max_depth=128),
    )

    assert parsed.joins[0].options is not None
    assert parsed.joins[0].condition is None
    assert parsed.candidate_digest == semantic_candidate_digest(parsed)
    assert envelope["version"] == 9
    assert decoded == parsed


def test_v8_wire_rejects_tampered_join_output_visibility() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT b.id FROM alpha a JOIN beta b ON a.id = b.alpha_id")
    )
    wire["joins"][0][4] = False

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_v7_wire_roundtrip_preserves_generic_grouping_and_rejects_legacy_shape() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse(
        "SELECT a, b, SUM(c) FROM totals GROUP BY a, ROLLUP(b)"
    )
    wire = _sql_ast_process._candidate_to_wire(parsed)
    decoded = _sql_ast_process._decode_candidate(
        wire,
        limits=AstLimits(max_nodes=10_000, max_depth=128),
    )
    malformed = json.loads(json.dumps(wire))
    malformed["groupings"][0][2] = [malformed["groupings"][0][2]]

    assert decoded == parsed
    assert wire["groupings"][0][2][0] == "group"
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            malformed,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_candidate_wire_roundtrip_includes_nested_query_facts() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse(
        "SELECT o.id FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM (SELECT i.order_id FROM items i) d "
        "WHERE d.order_id = o.id UNION SELECT 1)"
    )
    wire = _sql_ast_process._candidate_to_wire(parsed)
    decoded = _sql_ast_process._decode_candidate(
        wire,
        limits=AstLimits(max_nodes=10_000, max_depth=128),
    )

    assert decoded == parsed
    assert wire["derived_relations"]
    assert wire["set_operations"]
    assert wire["subquery_refs"]


def test_v9_wire_roundtrip_preserves_relation_aliases_and_rejects_v8() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse(
        "WITH cte_source AS (SELECT base_source.id FROM base_table AS base_source) "
        "SELECT cte_role.id FROM cte_source AS cte_role "
        "JOIN (SELECT derived_source.id FROM derived_table AS derived_source) "
        "AS derived_role ON cte_role.id = derived_role.id"
    )
    envelope = json.loads(_sql_ast_process._encode_envelope(parsed).decode())

    assert envelope["version"] == 9
    decoded = _sql_ast_process._decode_envelope(
        json.dumps(envelope).encode(),
        limits=AstLimits(max_nodes=10_000, max_depth=128),
    )
    assert {fact.table.name: fact.source_alias for fact in decoded.table_scans} == {
        "base_table": "base_source",
        "derived_table": "derived_source",
    }
    assert decoded.cte_references[0].source_alias == "cte_role"
    assert decoded.derived_relations[0].source_alias == "derived_role"

    old_version = json.loads(json.dumps(envelope))
    old_version["version"] = 8
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_envelope(
            json.dumps(old_version).encode(),
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_v9_wire_roundtrip_preserves_generic_expression_relation_and_rejects_malformed() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse("SELECT g.value FROM generate_series(1, 3) AS g(value)")
    envelope = json.loads(
        _sql_ast_process._encode_envelope(
            _parse(
                "WITH cte_source AS (SELECT base_source.id FROM base_table AS base_source) "
                "SELECT cte_role.id FROM cte_source AS cte_role "
                "JOIN (SELECT derived_source.id FROM derived_table AS derived_source) "
                "AS derived_role ON cte_role.id = derived_role.id"
            )
        ).decode()
    )
    wire = _sql_ast_process._candidate_to_wire(parsed)
    malformed = json.loads(json.dumps(wire))
    malformed["expression_relations"][0][4] = [""]

    assert _sql_ast_process._decode_candidate(
        wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
    ) == parsed
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            malformed, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


    using = json.loads(
        _sql_ast_process._encode_envelope(
            _parse("SELECT * FROM left_table JOIN right_table USING (id)")
        ).decode()
    )
    on = _sql_ast_process._candidate_to_wire(
        _parse("SELECT * FROM left_table JOIN right_table ON left_table.id = right_table.id")
    )["joins"][0][5]
    using["candidate"]["joins"][0][5] = on
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_envelope(
            json.dumps(using).encode(),
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    for collection in ("table_scans", "cte_references", "derived_relations"):
        malformed = json.loads(json.dumps(envelope))
        malformed["candidate"][collection][0].pop()
        with pytest.raises(_sql_ast_process._MalformedPayload):
            _sql_ast_process._decode_envelope(
                json.dumps(malformed).encode(),
                limits=AstLimits(max_nodes=10_000, max_depth=128),
            )

    duplicate = json.loads(
        _sql_ast_process._encode_envelope(
            _parse(
                "SELECT a.id FROM employee AS a "
                "JOIN employee AS b ON a.id = b.parent_id"
            )
        ).decode()
    )
    duplicate_scans = duplicate["candidate"]["table_scans"]
    duplicate_scans[1][4] = duplicate_scans[0][4]
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_envelope(
            json.dumps(duplicate).encode(),
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    mixed_duplicate = json.loads(json.dumps(envelope))
    mixed_candidate = mixed_duplicate["candidate"]
    mixed_candidate["derived_relations"][0][5] = mixed_candidate["cte_references"][
        0
    ][4]
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_envelope(
            json.dumps(mixed_duplicate).encode(),
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_v9_wire_rejects_tampered_pivot_input_relation() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse(
            "SELECT p.a FROM sales PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a)) AS p"
        )
    )
    wire["expression_relations"][0][5] = ["scope:0:relation:missing"]

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_v9_root_transform_roundtrip_and_input_integrity() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = parse_sql_candidate(
        "PIVOT sales ON category USING SUM(amount)", "duckdb://", "root-transform"
    )
    wire = _sql_ast_process._candidate_to_wire(parsed)

    assert _sql_ast_process._decode_candidate(
        wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
    ) == parsed
    wire["expression_relations"][0][5] = ["scope:0:relation:missing"]
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_v7_generic_options_reject_malformed_and_unknown_links() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT o.id FROM orders o TABLESAMPLE BERNOULLI (10)")
    )
    malformed = json.loads(json.dumps(wire))
    malformed["table_scans"][0][6].pop()
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            malformed, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )

    unknown_link = json.loads(json.dumps(wire))
    unknown_link["table_scans"][0][6] = [
        "subquery_ref", [["scope_id", "unknown-scope"]], []
    ]
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            unknown_link, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_v7_table_options_cannot_reference_later_relation() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT a.id FROM alpha a JOIN beta b ON a.id = b.id")
    )
    wire["table_scans"][0][6] = [
        "column",
        [["name", "id"], ["relation_id", wire["table_scans"][1][2]]],
        [],
    ]

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


def test_wire_decoder_rejects_column_with_unknown_relation_id() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(_parse("SELECT a.id FROM alpha a"))
    wire["projections"][0][3] = [
        "column",
        [["name", "id"], ["relation_id", "scope:0:relation:99"]],
        [],
    ]

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire, limits=AstLimits(max_nodes=10_000, max_depth=128)
        )


@pytest.mark.parametrize(
    ("collection", "field_index", "replacement"),
    [
        ("scopes", 2, "unknown-parent"),
        ("derived_relations", 3, "unknown-child"),
        ("set_operations", 5, "unknown-left"),
        ("subquery_refs", 2, "unknown-child"),
    ],
)
def test_wire_decoder_rejects_invalid_nested_scope_links(
    collection: str,
    field_index: int,
    replacement: str,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    parsed = _parse(
        "SELECT o.id FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM (SELECT i.order_id FROM items i) d "
        "WHERE d.order_id = o.id UNION SELECT 1)"
    )
    wire = _sql_ast_process._candidate_to_wire(parsed)
    wire[collection][0][field_index] = replacement

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_wire_decoder_rejects_subquery_fact_without_expression_reference() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT (SELECT a.id FROM alpha a) FROM beta b")
    )
    wire["projections"][0][3] = ["literal", [["value", "1"]], []]

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def _candidate_with_forged_outer_projection(
    parsed,
    *,
    child_scope_id: str,
    outer_scope_id: str,
    relation_id: str,
):
    from custom_tools.text_to_sql.adaptive._sql_ast_identity import (
        semantic_candidate_digest,
    )

    forged_expression = ExpressionFact(
        "outer_column",
        (
            ("name", "id"),
            ("outer_scope_id", outer_scope_id),
            ("relation_id", relation_id),
        ),
    )
    forged = replace(
        parsed,
        projections=tuple(
            replace(projection, expression=forged_expression)
            if projection.scope_id == child_scope_id
            else projection
            for projection in parsed.projections
        ),
    )
    return replace(forged, candidate_digest=semantic_candidate_digest(forged))


@pytest.mark.parametrize(
    "case",
    [
        "cte",
        "non_lateral",
        "lateral_later_source",
        "set_through_non_lateral",
        "join_on_later_source",
    ],
)
def test_wire_decoder_rejects_illegal_outer_scope_paths_with_recomputed_digest(
    case: str,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    if case == "cte":
        parsed = _parse("WITH c AS (SELECT 1) SELECT o.id FROM orders o")
        child_scope_id = parsed.ctes[0].query_scope_id
        relation_id = "scope:0:relation:0"
    elif case == "non_lateral":
        parsed = _parse("SELECT d.id FROM (SELECT 1 AS id) d JOIN orders o ON TRUE")
        child_scope_id = parsed.derived_relations[0].query_scope_id
        relation_id = "scope:0:relation:1"
    elif case == "lateral_later_source":
        parsed = _parse(
            "SELECT o.id FROM orders o CROSS JOIN LATERAL "
            "(SELECT o.id AS id) d CROSS JOIN later_table l"
        )
        child_scope_id = parsed.derived_relations[0].query_scope_id
        relation_id = "scope:0:relation:2"
    elif case == "set_through_non_lateral":
        parsed = _parse(
            "SELECT o.id FROM orders o JOIN (SELECT 1 AS id UNION SELECT 2) d ON TRUE"
        )
        set_scope_id = parsed.derived_relations[0].query_scope_id
        child_scope_id = next(
            operation.left_scope_id
            for operation in parsed.set_operations
            if operation.scope_id == set_scope_id
        )
        relation_id = "scope:0:relation:0"
    else:
        parsed = _parse(
            "SELECT a.id FROM alpha a "
            "JOIN beta b ON EXISTS (SELECT 1) "
            "JOIN gamma c ON TRUE"
        )
        child_scope_id = parsed.subquery_refs[0].child_scope_id
        relation_id = "scope:0:relation:2"

    forged = _candidate_with_forged_outer_projection(
        parsed,
        child_scope_id=child_scope_id,
        outer_scope_id="scope:0",
        relation_id=relation_id,
    )
    wire = _sql_ast_process._candidate_to_wire(forged)

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )
    malformed_source_digest = {**wire, "source_sql_digest": "sha256:ABC"}
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            malformed_source_digest,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_wire_decoder_rejects_boolean_version_and_duplicate_fact_ids() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_envelope(
            json.dumps(
                {
                    "version": True,
                    "status": "error",
                    "code": "PARSE_FAILED",
                }
            ).encode(),
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT o.id, o.status FROM orders o")
    )
    wire["projections"][1][0] = wire["projections"][0][0]
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    unknown_scope_wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT o.id FROM orders o")
    )
    unknown_scope_wire["table_scans"][0][1] = "unknown-scope"
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            unknown_scope_wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    join_wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT a.id FROM alpha a JOIN beta b ON a.id = b.alpha_id")
    )
    join_wire["joins"][0][3] = []
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            join_wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )

    predicate_wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT a.id FROM alpha a JOIN beta b ON a.id = b.alpha_id")
    )
    predicate_wire["predicates"][0][3] = None
    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            predicate_wire,
            limits=AstLimits(max_nodes=10_000, max_depth=128),
        )


def test_wire_decoder_counts_expression_nodes_across_the_whole_candidate() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    wire = _sql_ast_process._candidate_to_wire(
        _parse("SELECT o.id, o.status FROM orders o")
    )

    with pytest.raises(_sql_ast_process._MalformedPayload):
        _sql_ast_process._decode_candidate(
            wire,
            limits=AstLimits(max_nodes=1, max_depth=128),
        )


@pytest.mark.parametrize("field", ("source_sql_digest", "candidate_digest"))
def test_isolated_parser_rejects_worker_digests_that_do_not_match_input(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    original_decode = _sql_ast_process._decode_envelope

    def forged_decode(*args, **kwargs):
        candidate = original_decode(*args, **kwargs)
        return replace(candidate, **{field: "sha256:" + ("0" * 64)})

    monkeypatch.setattr(_sql_ast_process, "_decode_envelope", forged_decode)

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT o.id FROM orders o")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED


def test_isolated_parser_rejects_recomputed_but_invalid_worker_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    def publish_invalid_candidate(result_buffer, sql, dialect, candidate_id, limits):
        candidate = _sql_ast_process.parse_and_build_candidate(
            sql,
            dialect,
            candidate_id,
            limits,
        )
        forged = replace(
            candidate,
            joins=(replace(candidate.joins[0], left_relation_ids=()),),
        )
        forged = replace(
            forged,
            candidate_digest=_sql_ast_process.semantic_candidate_digest(forged),
        )
        _sql_ast_process._publish_result(
            result_buffer,
            _sql_ast_process._encode_envelope(forged),
        )

    monkeypatch.setattr(_sql_ast_process, "_parse_worker", publish_invalid_candidate)

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT a.id FROM alpha a JOIN beta b ON a.id = b.alpha_id")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED


def _active_child_pids() -> set[int]:
    return {
        process.pid
        for process in multiprocessing.active_children()
        if process.pid is not None
    }


def _live_thread_ids() -> set[int]:
    return {
        thread.ident for thread in threading.enumerate() if thread.ident is not None
    }


def _open_fd_ids() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_partial_result_writer_times_out_without_resource_leaks_before_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    release = threading.Event()

    def write_partial_result_then_block(result_buffer, *_args):
        view = memoryview(result_buffer).cast("B")
        try:
            view[1:4] = b"\x00\x00\x01"
            start = _sql_ast_process._RESULT_HEADER_BYTES
            view[start : start + 12] = b'{"version":1'
        finally:
            view.release()
        release.wait()

    monkeypatch.setattr(
        _sql_ast_process,
        "_parse_worker",
        write_partial_result_then_block,
    )
    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "0.01")
    child_pids_before = _active_child_pids()
    thread_ids_before = _live_thread_ids()
    fd_ids_before = _open_fd_ids()
    elapsed_samples: list[float] = []

    try:
        for index in range(24):
            started_at = time.monotonic()
            with pytest.raises(SqlAstError) as exc_info:
                _parse("SELECT 1", candidate_id=f"partial-{index}")
            elapsed_samples.append(time.monotonic() - started_at)
            assert exc_info.value.code is SqlAstErrorCode.PARSE_TIMEOUT

        assert min(elapsed_samples) >= 0.005
        assert max(elapsed_samples) < 0.5
        assert _active_child_pids() == child_pids_before
        assert _live_thread_ids() == thread_ids_before
        assert _open_fd_ids() == fd_ids_before
    finally:
        release.set()


@pytest.mark.parametrize(
    "configured_timeout",
    ["nan", "inf", "-inf", "1e309", "0", "-0.1", "not-a-number"],
)
def test_invalid_timeout_is_typed_before_process_start(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: str,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("process context must not be created")

    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", configured_timeout)
    monkeypatch.setattr(
        _sql_ast_process.multiprocessing,
        "get_context",
        unexpected_context,
    )

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED


def test_public_limits_do_not_mutate_private_model_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_models

    private_limits_before = (
        _sql_ast_models.MAX_AST_NODES,
        _sql_ast_models.MAX_AST_DEPTH,
    )
    monkeypatch.setattr(sql_ast, "MAX_AST_NODES", 8)

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT a, b, c, d FROM alpha WHERE a = 1")

    assert exc_info.value.code is SqlAstErrorCode.SHAPE_UNSUPPORTED
    assert (
        _sql_ast_models.MAX_AST_NODES,
        _sql_ast_models.MAX_AST_DEPTH,
    ) == private_limits_before


def test_concurrent_isolated_calls_keep_their_own_ast_limits() -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    sql = "SELECT a, b, c, d FROM alpha WHERE a = 1"

    def parse_with_limit(candidate_id: str, max_nodes: int):
        try:
            return _sql_ast_process.parse_candidate_isolated(
                sql,
                "postgres",
                candidate_id,
                max_ast_nodes=max_nodes,
                max_ast_depth=128,
            )
        except SqlAstError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        low = executor.submit(parse_with_limit, "low-limit", 8)
        normal = executor.submit(parse_with_limit, "normal-limit", 10_000)

    assert low.result() is SqlAstErrorCode.SHAPE_UNSUPPORTED
    assert normal.result().candidate_id == "normal-limit"


@pytest.mark.parametrize(
    ("max_nodes", "max_depth"),
    [
        (True, 128),
        (0, 128),
        (-1, 128),
        (10_001, 128),
        (10_000, False),
        (10_000, 0),
        (10_000, 129),
    ],
)
def test_invalid_ast_limits_fail_before_process_start(
    monkeypatch: pytest.MonkeyPatch,
    max_nodes,
    max_depth,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("process context must not be created")

    monkeypatch.setattr(
        _sql_ast_process.multiprocessing,
        "get_context",
        unexpected_context,
    )

    with pytest.raises(SqlAstError) as exc_info:
        _sql_ast_process.parse_candidate_isolated(
            "SELECT 1",
            "postgres",
            "invalid-limits",
            max_ast_nodes=max_nodes,
            max_ast_depth=max_depth,
        )

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED


def test_timeout_terminates_worker_before_return_without_thread_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_release = threading.Event()

    def slow_parse(*_args, **_kwargs):
        never_release.wait()
        return []

    monkeypatch.setattr("sqlglot.parse", slow_parse)
    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "0.01")
    child_pids_before = _active_child_pids()
    thread_ids_before = _live_thread_ids()

    started_at = time.monotonic()
    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_TIMEOUT
    assert time.monotonic() - started_at < 1.0
    assert _active_child_pids() == child_pids_before
    assert _live_thread_ids() == thread_ids_before


def test_repeated_timeouts_do_not_accumulate_workers_or_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_release = threading.Event()

    def slow_parse(*_args, **_kwargs):
        never_release.wait()
        return []

    monkeypatch.setattr("sqlglot.parse", slow_parse)
    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "0.005")
    child_pids_before = _active_child_pids()
    thread_ids_before = _live_thread_ids()

    for index in range(24):
        with pytest.raises(SqlAstError) as exc_info:
            _parse("SELECT 1", candidate_id=f"timeout-{index}")
        assert exc_info.value.code is SqlAstErrorCode.PARSE_TIMEOUT

    assert _active_child_pids() == child_pids_before
    assert _live_thread_ids() == thread_ids_before


def test_normal_and_parse_error_paths_leave_no_child_processes() -> None:
    start_method_before = multiprocessing.get_start_method(allow_none=True)
    child_pids_before = _active_child_pids()

    assert _parse("SELECT 1").projections
    _assert_error("SELECT (", SqlAstErrorCode.PARSE_FAILED)

    assert _active_child_pids() == child_pids_before
    assert multiprocessing.get_start_method(allow_none=True) == start_method_before


def test_child_exit_without_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    child_pids_before = _active_child_pids()

    def exit_without_payload(_result_buffer, *_args):
        return None

    monkeypatch.setattr(_sql_ast_process, "_parse_worker", exit_without_payload)

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1")
    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED
    assert _active_child_pids() == child_pids_before


def test_malformed_child_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    child_pids_before = _active_child_pids()

    def send_malformed_payload(result_buffer, *_args):
        _sql_ast_process._publish_result(
            result_buffer,
            b'{"status":"ok","candidate":',
        )

    monkeypatch.setattr(
        _sql_ast_process,
        "_parse_worker",
        send_malformed_payload,
    )

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1")
    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED
    assert _active_child_pids() == child_pids_before


def test_oversized_child_payload_length_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    child_pids_before = _active_child_pids()

    def publish_oversized_length(result_buffer, *_args):
        view = memoryview(result_buffer).cast("B")
        try:
            oversized = _sql_ast_process._MAX_RESULT_BYTES + 1
            view[1 : _sql_ast_process._RESULT_HEADER_BYTES] = oversized.to_bytes(
                _sql_ast_process._RESULT_LENGTH_BYTES,
                byteorder="big",
            )
            view[0] = _sql_ast_process._RESULT_READY
        finally:
            view.release()

    monkeypatch.setattr(
        _sql_ast_process,
        "_parse_worker",
        publish_oversized_length,
    )

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED
    assert _active_child_pids() == child_pids_before


def test_decoder_finishing_after_original_deadline_returns_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    original_decode = _sql_ast_process._decode_envelope

    def slow_decode(*args, **kwargs):
        candidate = original_decode(*args, **kwargs)
        time.sleep(0.55)
        return candidate

    monkeypatch.setattr(_sql_ast_process, "_decode_envelope", slow_decode)
    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "0.5")

    started_at = time.monotonic()
    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT 1", candidate_id="slow-decoder")
    elapsed = time.monotonic() - started_at

    assert exc_info.value.code is SqlAstErrorCode.PARSE_TIMEOUT
    assert 0.5 <= elapsed < 1.5


def test_decoder_finishing_within_original_deadline_returns_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import _sql_ast_process

    original_decode = _sql_ast_process._decode_envelope

    def bounded_decode(*args, **kwargs):
        candidate = original_decode(*args, **kwargs)
        time.sleep(0.01)
        return candidate

    monkeypatch.setattr(_sql_ast_process, "_decode_envelope", bounded_decode)
    monkeypatch.setenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "0.5")

    started_at = time.monotonic()
    parsed = _parse("SELECT 1", candidate_id="bounded-decoder")

    assert parsed.candidate_id == "bounded-decoder"
    assert time.monotonic() - started_at < 0.5
