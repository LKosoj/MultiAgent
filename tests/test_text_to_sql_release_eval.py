from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from custom_tools.text_to_sql.eval.cases import (
    REQUIRED_RELEASE_SLICES,
    load_gold_cases,
    validate_case_coverage,
)
from custom_tools.text_to_sql.eval.cli import build_parser, main
from custom_tools.text_to_sql.eval.metrics import (
    compute_release_metrics,
    compute_release_metrics_by_repetition,
    evaluate_release_thresholds,
)
from custom_tools.text_to_sql.eval.observability import canonical_files_digest
from custom_tools.text_to_sql.eval.release import (
    EXIT_APPROVAL,
    EXIT_CONTRACT,
    EXIT_ELIGIBLE,
    EXIT_INFRASTRUCTURE,
    EXIT_PROTECTED_LANE,
    ApprovedLatencyBaseline,
    CandidateIdentity,
    ReleasePolicy,
    canonical_baseline_payload_digest,
    canonical_policy_scope_digest,
    load_approved_latency_baseline,
    load_protected_lane_evidence,
    probe_required_driver_lanes,
)
from custom_tools.text_to_sql.eval.runner import (
    AuthenticatedT13EvalAdapter,
    EvalGenerationRequest,
    EvalObservation,
    schema_links_from_sql,
)
from streamlit_app.text_to_sql_client import TextToSqlApiClient, TextToSqlApiError


GOLD_DIR = Path("tests/eval/gold")
POLICY_PATH = Path("config/text_to_sql/eval_release.yaml")
TERMINAL_VECTORS = json.loads(
    Path("tests/fixtures/text_to_sql_terminal_contract_vectors.json").read_text(
        encoding="utf-8"
    )
)
COMMIT = "a" * 40
CANDIDATE_DIGEST = "b" * 64
LOCK_DIGEST = "c" * 64
MODEL_CONFIG_DIGEST = "d" * 64
PREVIOUS_COMMIT = "e" * 40
PREVIOUS_CANDIDATE_DIGEST = "f" * 64


def _policy_mapping(*, approval_status: str = "pending") -> dict:
    thresholds = {
        "execution_accuracy_overall_min": 0.90,
        "execution_accuracy_per_slice_min": 0.80,
        "schema_table_recall_min": 0.95,
        "schema_column_f1_min": 0.90,
        "expected_abstention_precision_min": 1.00,
        "expected_abstention_recall_min": 0.90,
        "unexpected_error_rate_max": 0.00,
        "contract_and_safety_case_pass_rate_min": 1.00,
        "latency_p95_regression_max_fraction": 0.20,
        "repetitions": 3,
    }
    required_slices = sorted(REQUIRED_RELEASE_SLICES)
    required_dialects = ["sqlite", "postgres", "mysql"]
    required_lanes = [
        "model:text2sql-primary",
        "driver:postgres",
        "driver:mysql",
    ]
    digest = canonical_policy_scope_digest(
        thresholds,
        required_slices,
        required_dialects,
        required_lanes,
    )
    approved = approval_status == "approved"
    return {
        "policy_version": 1,
        "thresholds": thresholds,
        "required_slices": required_slices,
        "required_dialects": required_dialects,
        "required_protected_lanes": required_lanes,
        "approval": {
            "status": approval_status,
            "approved_by": "release-owner" if approved else None,
            "approved_at": "2026-07-12T00:00:00Z" if approved else None,
            "approval_ref": "change-42" if approved else None,
            "thresholds_sha256": digest,
        },
    }


def _write_policy(path: Path, *, approval_status: str = "pending") -> Path:
    path.write_text(
        yaml.safe_dump(
            _policy_mapping(approval_status=approval_status),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _candidate(*, generated_at: str | None = None) -> dict[str, str]:
    return {
        "commit": COMMIT,
        "artifact_digest": CANDIDATE_DIGEST,
        "lock_digest": LOCK_DIGEST,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _ready_lane(dialect: str) -> dict:
    return {
        "lane_id": f"driver:{dialect}",
        "dialect": dialect,
        "production_ready": True,
        "reason_code": "READY",
        "probe_kind": "real_driver",
        "health": {"dialect": dialect, "production_ready": True},
    }


def _write_evidence(
    path: Path,
    *,
    lanes: list[dict] | None = None,
    candidate: dict | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": candidate or _candidate(),
                "lanes": lanes
                if lanes is not None
                else [_ready_lane("postgres"), _ready_lane("mysql")],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_connection_map(path: Path) -> Path:
    fixtures = {
        case.fixture
        for case in load_gold_cases(GOLD_DIR)
        if case.profile == "release"
    }
    path.write_text(
        json.dumps({fixture: f"connection-{index}" for index, fixture in enumerate(sorted(fixtures))}),
        encoding="utf-8",
    )
    return path


def _write_target_policy(path: Path) -> Path:
    path.write_text('{"schema_version":1}\n', encoding="utf-8")
    return path


def _write_latency_baseline(
    path: Path,
    *,
    baseline_overrides: dict | None = None,
    approval_overrides: dict | None = None,
) -> Path:
    now = datetime.now(timezone.utc)
    baseline = {
        "baseline_id": "baseline-text2sql-1",
        "provenance_kind": "prior_release",
        "baseline_commit": PREVIOUS_COMMIT,
        "baseline_digest": PREVIOUS_CANDIDATE_DIGEST,
        "for_candidate_commit": None,
        "for_candidate_digest": None,
        "measured_at": (now - timedelta(hours=2)).isoformat(),
        "model_id": "text2sql-primary",
        "provider_id": "provider-primary",
        "model_config_digest": MODEL_CONFIG_DIGEST,
        "gold_sha256": canonical_files_digest(GOLD_DIR),
        "latency_p95_ms": 1.0,
    }
    baseline.update(baseline_overrides or {})
    approval = {
        "status": "approved",
        "approved_by": "release-owner",
        "approved_at": (now - timedelta(hours=1)).isoformat(),
        "valid_until": (now + timedelta(hours=24)).isoformat(),
        "approval_ref": "change-42",
        "payload_sha256": canonical_baseline_payload_digest(baseline),
    }
    approval.update(approval_overrides or {})
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "baseline": baseline,
                "approval": approval,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _release_args(
    tmp_path: Path,
    *,
    policy: Path,
    evidence: Path,
    output: Path,
    latency_baseline_overrides: dict | None = None,
) -> list[str]:
    return [
        "--gold",
        str(GOLD_DIR),
        "--policy",
        str(policy),
        "--mode",
        "release",
        "--output",
        str(output),
        "--commit",
        COMMIT,
        "--candidate-digest",
        CANDIDATE_DIGEST,
        "--lock-digest",
        LOCK_DIGEST,
        "--model-id",
        "text2sql-primary",
        "--provider-id",
        "provider-primary",
        "--model-config-digest",
        MODEL_CONFIG_DIGEST,
        "--latency-baseline",
        str(
            _write_latency_baseline(
                tmp_path / "latency-baseline.json",
                baseline_overrides=latency_baseline_overrides,
            )
        ),
        "--target-policy",
        str(_write_target_policy(tmp_path / "target-policy.json")),
        "--protected-evidence",
        str(evidence),
        "--connection-map",
        str(_write_connection_map(tmp_path / "connections.json")),
        "--api-url",
        "http://api.test",
    ]


def _cancelled_terminal(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "status": "cancelled",
        "reason_code": "CANCELLED",
        "sql": "",
        "generated": False,
        "approved": False,
        "executed": False,
        "dry_run": False,
        "audited": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "error": None,
        "execution": {},
        "audit": {},
        "persistence": {"status": "not_attempted"},
    }


def _passing_observation(case, *, repetition: int = 1) -> EvalObservation:
    sql = case.expected_sql or ""
    succeeded = case.expected_outcome == "succeeded"
    dry_run = bool(case.request_options["dry_run_only"] and succeeded)
    executed = bool(succeeded and not dry_run)
    return EvalObservation(
        case_id=case.id,
        run_id=f"run-{repetition}-{case.id}",
        status=case.expected_outcome,
        reason_code=(case.expected_reason_codes[0] if case.expected_reason_codes else ""),
        generated_sql=sql,
        rows=list(case.expected_rows or []),
        schema_links=case.expected_schema_links or {},
        duration_ms=1.0,
        error=None,
        repetition=repetition,
        generated=bool(sql),
        approved=bool(sql),
        executed=executed,
        dry_run=dry_run,
        audited=bool(succeeded and sql),
    )


def _install_passing_adapter(monkeypatch) -> None:
    class PassingAdapter:
        def __init__(self, _client):
            pass

        def run_case(self, case, *, connection_ref):
            assert connection_ref.startswith("connection-")
            return _passing_observation(case)

    monkeypatch.setattr(
        "custom_tools.text_to_sql.eval.cli.AuthenticatedT13EvalAdapter",
        PassingAdapter,
    )
    monkeypatch.setenv("TEXT2SQL_EVAL_API_TOKEN", "eval-token")


def test_release_case_schema_and_gold_cover_required_local_slices() -> None:
    cases = load_gold_cases(GOLD_DIR)
    release_cases = [case for case in cases if case.profile == "release"]
    policy = ReleasePolicy.load(POLICY_PATH)
    coverage = validate_case_coverage(
        release_cases,
        required_slices=policy.required_slices,
        required_dialects=policy.required_dialects,
    )

    assert coverage.missing_slices == ()
    assert coverage.missing_dialects == ()
    assert len({case.id for case in cases}) == len(cases)
    assert all(case.schema_version == 1 and case.review.status == "reviewed" for case in cases)
    assert {case.dialect for case in release_cases} == {"sqlite", "postgres", "mysql"}
    assert any(
        {"aggregate", "filter"} <= set(case.slice_tags)
        for case in release_cases
        if case.dialect == "sqlite"
    )
    assert next(case for case in release_cases if "cancel" in case.slice_tags).cancel_after_start
    assert {case.expected_outcome for case in cases} >= {
        "succeeded",
        "abstained",
        "failed",
        "cancelled",
        "timed_out",
    }


def test_generation_request_contains_no_oracle_fields_or_values() -> None:
    case = next(case for case in load_gold_cases(GOLD_DIR) if case.expected_sql)
    request = EvalGenerationRequest.from_case(case)
    payload = request.to_service_payload(connection_ref="conn-authorized")

    assert set(payload) == {
        "query",
        "connection_ref",
        "max_rows",
        "safety_level",
        "include_explanation",
        "validate_schema",
        "dry_run_only",
        "use_schema_suggestions",
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert case.expected_sql not in serialized
    assert "expected_rows" not in serialized
    assert "expected_schema_links" not in serialized
    assert "expected_outcome" not in serialized


def test_schema_metrics_input_is_derived_from_generated_sql_without_oracle() -> None:
    links = schema_links_from_sql(
        "SELECT c.region, SUM(o.amount) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
        dialect="sqlite",
    )

    assert links == {
        "tables": ["customers", "orders"],
        "columns": [
            "customers.id",
            "customers.region",
            "orders.amount",
            "orders.customer_id",
        ],
    }


def test_authenticated_adapter_uses_t13_http_contract_only() -> None:
    case = next(
        case
        for case in load_gold_cases(GOLD_DIR)
        if case.expected_outcome == "succeeded"
    )
    calls = []
    terminal = {**TERMINAL_VECTORS["base"], "run_id": "run-eval-1"}

    def transport(method, path, *, json_body, headers):
        calls.append((method, path, json_body, headers))
        if (method, path) == ("POST", "/v1/runs"):
            return {
                "runId": "run-eval-1",
                "threadId": "thread-eval-1",
                "statusUrl": "/v1/runs/run-eval-1",
                "resultUrl": "/v1/runs/run-eval-1/result",
                "cancelUrl": "/v1/runs/run-eval-1/cancel",
            }
        if path == "/v1/runs/run-eval-1":
            return {"runId": "run-eval-1", "status": "finished"}
        if path == "/v1/runs/run-eval-1/result":
            return {"result": {"terminal_outcome": terminal}}
        raise AssertionError((method, path))

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer eval-token"},
        poll_interval_seconds=0,
    )
    observation = AuthenticatedT13EvalAdapter(client).run_case(
        case,
        connection_ref="conn-authorized",
    )

    assert observation.status == "succeeded"
    assert observation.run_id == "run-eval-1"
    assert observation.generated is True
    assert observation.approved is True
    assert observation.executed is True
    assert observation.dry_run is False
    assert observation.audited is True
    assert [call[:2] for call in calls] == [
        ("POST", "/v1/runs"),
        ("GET", "/v1/runs/run-eval-1"),
        ("GET", "/v1/runs/run-eval-1/result"),
    ]
    assert all(call[3] == {"Authorization": "Bearer eval-token"} for call in calls)
    start_payload = calls[0][2]["forwardedProps"]["service_payload"]
    assert start_payload["query"] == case.question
    assert start_payload["connection_ref"] == "conn-authorized"
    assert not ({"expected_sql", "expected_rows", "expected_schema_links"} & set(start_payload))


def test_explicit_cancel_case_uses_canonical_cancel_endpoint() -> None:
    case = replace(
        next(case for case in load_gold_cases(GOLD_DIR) if "cancel" in case.slice_tags),
        cancel_after_start=True,
    )
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/runs"):
            return {"runId": "run-cancel", "threadId": "thread-cancel"}
        if (method, path) == ("POST", "/v1/runs/run-cancel/cancel"):
            return {"runId": "run-cancel", "cancelled": True}
        if (method, path) == ("GET", "/v1/runs/run-cancel"):
            return {"runId": "run-cancel", "status": "cancelled"}
        if (method, path) == ("GET", "/v1/runs/run-cancel/result"):
            return {"result": {"terminal_outcome": _cancelled_terminal("run-cancel")}}
        raise AssertionError((method, path))

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer eval-token"},
        poll_interval_seconds=0,
    )

    observation = AuthenticatedT13EvalAdapter(client).run_case(
        case,
        connection_ref="conn-authorized",
    )

    assert observation.status == "cancelled"
    assert calls == [
        ("POST", "/v1/runs"),
        ("POST", "/v1/runs/run-cancel/cancel"),
        ("GET", "/v1/runs/run-cancel"),
        ("GET", "/v1/runs/run-cancel/result"),
    ]


def test_poll_timeout_cancels_and_confirms_bounded_terminal_cleanup() -> None:
    case = next(case for case in load_gold_cases(GOLD_DIR) if case.profile == "release")
    calls = []
    cancelled = False

    def transport(method, path, *, json_body, headers):
        nonlocal cancelled
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/runs"):
            return {"runId": "run-timeout", "threadId": "thread-timeout"}
        if (method, path) == ("POST", "/v1/runs/run-timeout/cancel"):
            cancelled = True
            return {"runId": "run-timeout", "cancelled": True}
        if (method, path) == ("GET", "/v1/runs/run-timeout"):
            return {
                "runId": "run-timeout",
                "status": "cancelled" if cancelled else "running",
            }
        if (method, path) == ("GET", "/v1/runs/run-timeout/result"):
            return {"result": {"terminal_outcome": _cancelled_terminal("run-timeout")}}
        raise AssertionError((method, path))

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer eval-token"},
        poll_interval_seconds=0,
        max_poll_attempts=2,
    )

    observation = AuthenticatedT13EvalAdapter(client).run_case(
        case,
        connection_ref="conn-authorized",
    )

    assert observation.status == "cancelled"
    assert calls.count(("GET", "/v1/runs/run-timeout")) == 3
    assert ("POST", "/v1/runs/run-timeout/cancel") in calls


def test_poll_timeout_raises_when_bounded_cleanup_never_reaches_terminal() -> None:
    case = next(case for case in load_gold_cases(GOLD_DIR) if case.profile == "release")
    calls = []

    def transport(method, path, *, json_body, headers):
        calls.append((method, path))
        if (method, path) == ("POST", "/v1/runs"):
            return {"runId": "run-stuck", "threadId": "thread-stuck"}
        if (method, path) == ("POST", "/v1/runs/run-stuck/cancel"):
            return {"runId": "run-stuck", "cancelled": True}
        if (method, path) == ("GET", "/v1/runs/run-stuck"):
            return {"runId": "run-stuck", "status": "running"}
        raise AssertionError((method, path))

    client = TextToSqlApiClient(
        base_url="http://api.test",
        transport=transport,
        auth_headers=lambda: {"Authorization": "Bearer eval-token"},
        poll_interval_seconds=0,
        max_poll_attempts=2,
    )

    with pytest.raises(TextToSqlApiError, match="cleanup"):
        AuthenticatedT13EvalAdapter(client).run_case(
            case,
            connection_ref="conn-authorized",
        )

    assert calls.count(("GET", "/v1/runs/run-stuck")) == 4


def test_policy_digest_and_pending_approval_are_fail_closed() -> None:
    policy = ReleasePolicy.load(POLICY_PATH)

    assert policy.version == 1
    assert policy.approval.status == "pending"
    assert policy.approval.approved_by is None
    assert policy.approval.thresholds_sha256 == canonical_policy_scope_digest(
        policy.thresholds,
        policy.required_slices,
        policy.required_dialects,
        policy.required_protected_lanes,
    )
    assert (EXIT_ELIGIBLE, EXIT_CONTRACT, EXIT_APPROVAL, EXIT_PROTECTED_LANE, EXIT_INFRASTRUCTURE) == (
        0,
        2,
        3,
        4,
        5,
    )
    option_names = {option for action in build_parser()._actions for option in action.option_strings}
    assert {
        "--repetitions",
        "--candidate-digest",
        "--lock-digest",
        "--latency-baseline",
        "--provider-id",
        "--target-policy",
    } <= option_names
    assert "--approve" not in option_names
    assert "--ignore-approval" not in option_names
    assert "--ignore-thresholds" not in option_names


def test_policy_scope_digest_covers_dialects_and_protected_lanes() -> None:
    policy = ReleasePolicy.load(POLICY_PATH)
    baseline = policy.scope_digest

    assert baseline != canonical_policy_scope_digest(
        policy.thresholds,
        policy.required_slices,
        (*policy.required_dialects, "duckdb"),
        policy.required_protected_lanes,
    )
    assert baseline != canonical_policy_scope_digest(
        policy.thresholds,
        policy.required_slices,
        policy.required_dialects,
        (*policy.required_protected_lanes, "driver:duckdb"),
    )


def test_strict_protected_evidence_accepts_only_fresh_matching_candidate(tmp_path) -> None:
    source = _write_evidence(tmp_path / "evidence.json")
    expected = CandidateIdentity(COMMIT, CANDIDATE_DIGEST, LOCK_DIGEST)

    loaded = load_protected_lane_evidence(
        [source],
        expected_candidate=expected,
        required_driver_lanes={"driver:postgres", "driver:mysql"},
    )

    assert set(loaded) == {"driver:postgres", "driver:mysql"}


@pytest.mark.parametrize(
    ("lanes", "match"),
    [
        ([_ready_lane("postgres"), _ready_lane("postgres")], "duplicate"),
        ([_ready_lane("duckdb")], "unknown"),
        (
            [
                {
                    **_ready_lane("postgres"),
                    "dialect": "mysql",
                }
            ],
            "dialect",
        ),
        (
            [
                {
                    **_ready_lane("postgres"),
                    "production_ready": False,
                }
            ],
            "READY",
        ),
    ],
)
def test_strict_protected_evidence_rejects_invalid_lanes(
    tmp_path,
    lanes,
    match,
) -> None:
    source = _write_evidence(tmp_path / "evidence.json", lanes=lanes)

    with pytest.raises(ValueError, match=match):
        load_protected_lane_evidence(
            [source],
            expected_candidate=CandidateIdentity(
                COMMIT,
                CANDIDATE_DIGEST,
                LOCK_DIGEST,
            ),
            required_driver_lanes={"driver:postgres", "driver:mysql"},
        )


def test_strict_protected_evidence_rejects_stale_or_mismatched_candidate(tmp_path) -> None:
    stale = _candidate(
        generated_at=(datetime.now(timezone.utc) - timedelta(hours=25))
        .isoformat()
        .replace("+00:00", "Z")
    )
    stale_source = _write_evidence(tmp_path / "stale.json", candidate=stale)
    mismatch = {**_candidate(), "artifact_digest": "e" * 64}
    mismatch_source = _write_evidence(tmp_path / "mismatch.json", candidate=mismatch)
    expected = CandidateIdentity(COMMIT, CANDIDATE_DIGEST, LOCK_DIGEST)

    with pytest.raises(ValueError, match="stale"):
        load_protected_lane_evidence(
            [stale_source],
            expected_candidate=expected,
            required_driver_lanes={"driver:postgres", "driver:mysql"},
        )
    with pytest.raises(ValueError, match="candidate"):
        load_protected_lane_evidence(
            [mismatch_source],
            expected_candidate=expected,
            required_driver_lanes={"driver:postgres", "driver:mysql"},
        )


@pytest.mark.parametrize(
    "candidate",
    [
        {**_candidate(), "commit": "A" * 40},
        {**_candidate(), "artifact_digest": "not-a-sha256"},
        {**_candidate(), "lock_digest": "D" * 64},
    ],
)
def test_strict_protected_evidence_rejects_noncanonical_identity(
    tmp_path,
    candidate,
) -> None:
    source = _write_evidence(tmp_path / "bad-identity.json", candidate=candidate)

    with pytest.raises(ValueError, match="lowercase|SHA-256"):
        load_protected_lane_evidence(
            [source],
            expected_candidate=CandidateIdentity(
                COMMIT,
                CANDIDATE_DIGEST,
                LOCK_DIGEST,
            ),
            required_driver_lanes={"driver:postgres", "driver:mysql"},
        )


def test_strict_protected_evidence_rejects_unknown_top_level_fields(tmp_path) -> None:
    source = _write_evidence(tmp_path / "evidence.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["extra"] = True
    source.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="fields"):
        load_protected_lane_evidence(
            [source],
            expected_candidate=CandidateIdentity(
                COMMIT,
                CANDIDATE_DIGEST,
                LOCK_DIGEST,
            ),
            required_driver_lanes={"driver:postgres", "driver:mysql"},
        )


def test_driver_probe_emits_strict_candidate_schema_and_checks_plugin_dialect(
    tmp_path,
    monkeypatch,
) -> None:
    class WrongPlugin:
        dialect = "mysql"

        def connect(self, _dsn):
            return object()

        def close(self, _connection):
            pass

    monkeypatch.setenv("TEXT2SQL_POSTGRES_DSN", "postgresql://protected")
    monkeypatch.setattr("db_plugins.manager.get_plugin", lambda _dsn: WrongPlugin())
    output = tmp_path / "probe.json"

    evidence = probe_required_driver_lanes(
        ["postgres"],
        evidence_path=output,
        commit=COMMIT,
        artifact_digest=CANDIDATE_DIGEST,
        lock_digest=LOCK_DIGEST,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    assert set(evidence) == {"schema_version", "candidate", "lanes"}
    assert evidence["candidate"]["commit"] == COMMIT
    assert evidence["lanes"][0]["production_ready"] is False
    assert evidence["lanes"][0]["reason_code"] == "DIALECT_MISMATCH"
    assert evidence["lanes"][0]["probe_kind"] == "real_driver"


def test_driver_probe_does_not_transport_latency_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    class Health:
        dialect = "postgres"
        production_ready = True
        reason_codes = ()

        def to_mapping(self):
            return {"dialect": self.dialect, "production_ready": True}

    class HealthyPlugin:
        dialect = "postgres"

        def connect(self, _dsn):
            return object()

        def probe_capabilities(self, _connection, _dsn):
            return Health()

        def close(self, _connection):
            pass

    monkeypatch.setenv("TEXT2SQL_POSTGRES_DSN", "postgresql://protected")
    monkeypatch.setenv("TEXT2SQL_EVAL_LATENCY_BASELINE_P95_MS", "7.25")
    monkeypatch.setattr("db_plugins.manager.get_plugin", lambda _dsn: HealthyPlugin())

    evidence = probe_required_driver_lanes(
        ["postgres"],
        evidence_path=tmp_path / "probe.json",
        commit=COMMIT,
        artifact_digest=CANDIDATE_DIGEST,
        lock_digest=LOCK_DIGEST,
    )

    lane = evidence["lanes"][0]
    assert lane["production_ready"] is True
    assert lane["reason_code"] == "READY"
    assert "latency_baseline_p95_ms" not in lane["health"]


def _load_latency_baseline(path: Path) -> ApprovedLatencyBaseline:
    return load_approved_latency_baseline(
        path,
        expected_candidate=CandidateIdentity(
            COMMIT,
            CANDIDATE_DIGEST,
            LOCK_DIGEST,
        ),
        expected_model_id="text2sql-primary",
        expected_provider_id="provider-primary",
        expected_model_config_digest=MODEL_CONFIG_DIGEST,
        expected_gold_sha256=canonical_files_digest(GOLD_DIR),
    )


def test_approved_latency_baseline_validates_exact_current_identity(tmp_path) -> None:
    path = _write_latency_baseline(tmp_path / "baseline.json")

    baseline = _load_latency_baseline(path)

    assert baseline.baseline_id == "baseline-text2sql-1"
    assert baseline.latency_p95_ms == 1.0
    assert baseline.provider_id == "provider-primary"


def test_approved_latency_baseline_rejects_tampered_payload(tmp_path) -> None:
    path = _write_latency_baseline(tmp_path / "baseline.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["baseline"]["latency_p95_ms"] = 99.0
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="payload_sha256"):
        _load_latency_baseline(path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("baseline_commit", COMMIT, "self-referential"),
        ("baseline_digest", CANDIDATE_DIGEST, "self-referential"),
        ("model_id", "other-model", "does not match"),
        ("provider_id", "other-provider", "does not match"),
        ("model_config_digest", "e" * 64, "does not match"),
        ("gold_sha256", "e" * 64, "does not match"),
    ],
)
def test_approved_latency_baseline_rejects_identity_mismatch(
    tmp_path,
    field,
    value,
    match,
) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        baseline_overrides={field: value},
    )

    with pytest.raises(ValueError, match=match):
        _load_latency_baseline(path)


def test_approved_latency_baseline_accepts_bootstrap_for_first_release(
    tmp_path,
) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        baseline_overrides={
            "provenance_kind": "bootstrap",
            "baseline_commit": COMMIT,
            "baseline_digest": CANDIDATE_DIGEST,
        },
    )

    baseline = _load_latency_baseline(path)

    assert baseline.provenance_kind == "bootstrap"
    assert baseline.baseline_commit == COMMIT
    assert baseline.baseline_digest == CANDIDATE_DIGEST


@pytest.mark.parametrize(
    "overrides",
    [
        {"provenance_kind": "bootstrap"},
        {"provenance_kind": "bootstrap", "baseline_digest": CANDIDATE_DIGEST},
    ],
)
def test_approved_latency_baseline_rejects_bootstrap_not_measured_on_candidate(
    tmp_path,
    overrides,
) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        baseline_overrides=overrides,
    )

    with pytest.raises(ValueError, match="bootstrap"):
        _load_latency_baseline(path)


def test_approved_latency_baseline_rejects_schema_version_one(tmp_path) -> None:
    path = _write_latency_baseline(tmp_path / "baseline.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="no longer accepted"):
        _load_latency_baseline(path)


def test_approved_latency_baseline_rejects_for_candidate_mismatch(tmp_path) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        baseline_overrides={
            "for_candidate_commit": "e" * 40,
            "for_candidate_digest": "f" * 64,
        },
    )

    with pytest.raises(ValueError, match="does not match"):
        _load_latency_baseline(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"for_candidate_commit": COMMIT},
        {"for_candidate_digest": CANDIDATE_DIGEST},
    ],
)
def test_approved_latency_baseline_rejects_partial_for_candidate(
    tmp_path,
    overrides,
) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        baseline_overrides=overrides,
    )

    with pytest.raises(ValueError, match="both"):
        _load_latency_baseline(path)


def test_approved_latency_baseline_rejects_expired_approval(tmp_path) -> None:
    path = _write_latency_baseline(
        tmp_path / "baseline.json",
        approval_overrides={
            "valid_until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        },
    )

    with pytest.raises(ValueError, match="expired|time order"):
        _load_latency_baseline(path)


def test_approved_latency_baseline_rejects_unknown_fields_and_pending_status(
    tmp_path,
) -> None:
    path = _write_latency_baseline(tmp_path / "baseline.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["extra"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        _load_latency_baseline(path)

    path = _write_latency_baseline(
        tmp_path / "pending.json",
        approval_overrides={"status": "pending"},
    )
    with pytest.raises(ValueError, match="approved"):
        _load_latency_baseline(path)


def test_deterministic_cli_emits_ineligible_evidence_with_zero_exit(tmp_path) -> None:
    output = tmp_path / "deterministic.json"

    exit_code = main(
        [
            "--gold",
            str(GOLD_DIR),
            "--policy",
            str(POLICY_PATH),
            "--mode",
            "deterministic-contract",
            "--output",
            str(output),
        ]
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_ELIGIBLE
    assert artifact["exit_code"] == EXIT_ELIGIBLE
    assert artifact["mode"] == "deterministic-contract"
    assert artifact["contract_passed"] is True
    assert artifact["release_eligible"] is False
    assert artifact["result_code"] == "DETERMINISTIC_CONTRACT_ONLY"
    assert artifact["gold_sha256"] == canonical_files_digest(GOLD_DIR)
    assert artifact["observations"] == []
    assert artifact["metrics"]["execution_accuracy_overall"] == 0.0
    assert artifact["threshold_failures"]
    assert any(
        "baseline_missing" in failure
        for failure in artifact["threshold_failures"]
    )


def test_release_cli_requires_complete_interface_even_when_approval_pending(tmp_path) -> None:
    output = tmp_path / "release-pending.json"

    exit_code = main(
        [
            "--gold",
            str(GOLD_DIR),
            "--policy",
            str(POLICY_PATH),
            "--mode",
            "release",
            "--output",
            str(output),
        ]
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_INFRASTRUCTURE
    assert artifact["result_code"] == "EVALUATOR_INFRASTRUCTURE_FAILED"
    assert artifact["release_eligible"] is False
    assert artifact["threshold_failures"]


def test_release_cli_returns_protected_lane_unavailable_after_approval(tmp_path) -> None:
    policy = _write_policy(tmp_path / "approved.yaml", approval_status="approved")
    output = tmp_path / "release-no-lanes.json"
    evidence = _write_evidence(tmp_path / "evidence.json", lanes=[])

    exit_code = main(
        _release_args(
            tmp_path,
            policy=policy,
            evidence=evidence,
            output=output,
        )
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_PROTECTED_LANE
    assert artifact["result_code"] == "PROTECTED_LANE_UNAVAILABLE"
    assert sorted(artifact["missing_protected_lanes"]) == [
        "driver:mysql",
        "driver:postgres",
    ]


def test_invalid_gold_contract_returns_contract_exit_and_evidence(tmp_path) -> None:
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "only-filter",
                "question": "one",
                "dialect": "sqlite",
                "fixture": "sqlite_text2sql_v2",
                "expected_outcome": "succeeded",
                "comparison_mode": "exact",
                "expected_rows": [{"value": 1}],
                "slice_tags": ["filter"],
                "request_options": {},
                "review": {
                    "status": "reviewed",
                    "reviewed_by": "reviewer",
                    "reviewed_at": "2026-07-12T00:00:00Z",
                    "reference": "test",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "invalid.json"

    exit_code = main(
        [
            "--gold",
            str(gold),
            "--policy",
            str(POLICY_PATH),
            "--mode",
            "deterministic-contract",
            "--output",
            str(output),
        ]
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_CONTRACT
    assert artifact["result_code"] == "CONTRACT_OR_COVERAGE_FAILED"
    assert artifact["release_eligible"] is False


def test_release_metrics_and_thresholds_fail_known_wrong_observation() -> None:
    cases = load_gold_cases(GOLD_DIR)
    observations = [
        EvalObservation(
            case_id=case.id,
            run_id=f"run-{case.id}",
            status=("failed" if case.expected_outcome == "succeeded" else case.expected_outcome),
            reason_code="EXECUTION_FAILED",
            generated_sql="SELECT 0",
            rows=[],
            schema_links={},
            duration_ms=1.0,
            error="wrong",
        )
        for case in cases
    ]
    metrics = compute_release_metrics(cases, observations)
    failures = evaluate_release_thresholds(
        metrics,
        ReleasePolicy.load(POLICY_PATH).thresholds,
    )

    assert metrics.execution_accuracy_overall < 0.90
    assert failures


def test_evaluate_release_thresholds_bootstrap_mode_skips_missing_baseline_gate() -> None:
    cases = [case for case in load_gold_cases(GOLD_DIR) if case.profile == "release"]
    observations = [_passing_observation(case) for case in cases]
    metrics = compute_release_metrics(cases, observations)
    thresholds = ReleasePolicy.load(POLICY_PATH).thresholds

    failures_default = evaluate_release_thresholds(metrics, thresholds)
    failures_bootstrap = evaluate_release_thresholds(
        metrics, thresholds, latency_gate_mode="bootstrap"
    )

    assert metrics.latency_p95_regression_fraction is None
    assert any("baseline_missing" in failure for failure in failures_default)
    assert not any("baseline_missing" in failure for failure in failures_bootstrap)


def test_release_metrics_count_every_policy_repetition() -> None:
    case = next(
        case
        for case in load_gold_cases(GOLD_DIR)
        if case.comparison_mode == "exact"
    )
    observations = [
        EvalObservation(
            case_id=case.id,
            run_id="run-pass",
            status="succeeded",
            reason_code="",
            generated_sql=case.expected_sql or "SELECT 1",
            rows=list(case.expected_rows or []),
            schema_links=case.expected_schema_links or {},
            duration_ms=1.0,
            error=None,
            repetition=1,
            generated=True,
            approved=True,
            executed=True,
            dry_run=False,
            audited=True,
        ),
        EvalObservation(
            case_id=case.id,
            run_id="run-fail",
            status="failed",
            reason_code="EXECUTION_FAILED",
            generated_sql="SELECT 0",
            rows=[],
            schema_links={},
            duration_ms=2.0,
            error="wrong",
            repetition=2,
            generated=True,
            approved=True,
            executed=False,
            dry_run=False,
            audited=False,
        ),
    ]

    metrics = compute_release_metrics([case], observations)

    assert metrics.total == 2
    assert metrics.passed == 1
    assert metrics.execution_accuracy_overall == 0.5


def test_execution_accuracy_excludes_contract_only_cases() -> None:
    cases = [case for case in load_gold_cases(GOLD_DIR) if case.profile == "release"]
    execution_case = next(case for case in cases if case.comparison_mode == "exact")
    abstain_case = next(case for case in cases if case.expected_outcome == "abstained")
    wrong_rows = replace(
        _passing_observation(execution_case),
        rows=[{"wrong": 1}],
    )

    metrics = compute_release_metrics(
        [execution_case, abstain_case],
        [wrong_rows, _passing_observation(abstain_case)],
    )

    assert metrics.execution_total == 1
    assert metrics.execution_accuracy_overall == 0.0
    assert metrics.contract_and_safety_case_pass_rate == 1.0


def test_dry_run_contract_requires_dry_run_without_execution() -> None:
    case = next(
        case
        for case in load_gold_cases(GOLD_DIR)
        if case.request_options["dry_run_only"]
    )
    invalid = replace(
        _passing_observation(case),
        dry_run=False,
        executed=True,
    )

    metrics = compute_release_metrics([case], [invalid])

    assert metrics.contract_and_safety_case_pass_rate == 0.0


def test_per_repetition_metrics_expose_worst_repetition() -> None:
    case = next(
        case
        for case in load_gold_cases(GOLD_DIR)
        if case.profile == "release" and case.comparison_mode == "exact"
    )
    passing = _passing_observation(case, repetition=1)
    failing = replace(
        _passing_observation(case, repetition=2),
        status="failed",
        reason_code="EXECUTION_FAILED",
        rows=[],
        executed=False,
    )

    metrics = compute_release_metrics_by_repetition(
        [case],
        [passing, failing],
        expected_repetitions=2,
        latency_baseline_p95_ms=1.0,
    )

    assert metrics[1].execution_accuracy_overall == 1.0
    assert metrics[2].execution_accuracy_overall == 0.0


def test_repetitions_cli_value_can_only_assert_policy_value(tmp_path) -> None:
    policy = _write_policy(tmp_path / "approved.yaml", approval_status="approved")
    evidence = _write_evidence(tmp_path / "evidence.json")
    output = tmp_path / "mismatch.json"

    exit_code = main(
        [
            *_release_args(
                tmp_path,
                policy=policy,
                evidence=evidence,
                output=output,
            ),
            "--repetitions",
            "2",
        ]
    )

    assert exit_code == EXIT_CONTRACT
    assert json.loads(output.read_text(encoding="utf-8"))["result_code"] == (
        "CONTRACT_OR_COVERAGE_FAILED"
    )


def test_pending_approval_runs_available_eval_and_reports_provisional_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    policy = _write_policy(tmp_path / "pending.yaml", approval_status="pending")
    evidence = _write_evidence(tmp_path / "evidence.json")
    output = tmp_path / "pending.json"
    _install_passing_adapter(monkeypatch)

    exit_code = main(
        _release_args(
            tmp_path,
            policy=policy,
            evidence=evidence,
            output=output,
        )
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))
    release_case_count = sum(
        case.profile == "release" for case in load_gold_cases(GOLD_DIR)
    )

    assert exit_code == EXIT_APPROVAL
    assert len(artifact["observations"]) == release_case_count * 3
    assert set(artifact["per_repetition_metrics"]) == {"1", "2", "3"}
    assert artifact["model_lane_satisfied"] is True


def test_approved_release_requires_real_eval_for_model_lane(
    tmp_path,
    monkeypatch,
) -> None:
    policy = _write_policy(tmp_path / "approved.yaml", approval_status="approved")
    evidence = _write_evidence(tmp_path / "evidence.json")
    evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert all(
        lane["lane_id"].startswith("driver:")
        for lane in evidence_payload["lanes"]
    )
    output = tmp_path / "approved.json"
    _install_passing_adapter(monkeypatch)

    exit_code = main(
        _release_args(
            tmp_path,
            policy=policy,
            evidence=evidence,
            output=output,
        )
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_ELIGIBLE
    assert artifact["release_eligible"] is True
    assert artifact["model_lane_satisfied"] is True
    assert artifact["threshold_failures"] == []
    baseline_path = tmp_path / "latency-baseline.json"
    baseline_file_sha256 = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    assert artifact["provenance"]["provider_id"] == "provider-primary"
    assert artifact["provenance"]["baseline_id"] == "baseline-text2sql-1"
    assert artifact["provenance"]["baseline_file_sha256"] == baseline_file_sha256
    assert artifact["provenance"]["baseline_provenance_kind"] == "prior_release"
    assert artifact["advisories"] == []
    assert {
        "kind": "latency_baseline",
        "path": str(baseline_path),
        "sha256": baseline_file_sha256,
    } in artifact["evidence_index"]
    assert all(
        {
            "case_id",
            "repetition",
            "run_id",
            "status",
            "reason_code",
            "sql_sha256",
            "schema_links",
            "rows",
            "columns",
            "generated",
            "approved",
            "executed",
            "dry_run",
            "audited",
            "duration_ms",
            "error",
        }
        == set(observation)
        for observation in artifact["observations"]
    )


def test_approved_release_with_bootstrap_baseline_emits_latency_gate_advisory(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    policy = _write_policy(tmp_path / "approved.yaml", approval_status="approved")
    evidence = _write_evidence(tmp_path / "evidence.json")
    output = tmp_path / "bootstrap.json"
    _install_passing_adapter(monkeypatch)

    with caplog.at_level("WARNING"):
        exit_code = main(
            _release_args(
                tmp_path,
                policy=policy,
                evidence=evidence,
                output=output,
                latency_baseline_overrides={
                    "provenance_kind": "bootstrap",
                    "baseline_commit": COMMIT,
                    "baseline_digest": CANDIDATE_DIGEST,
                },
            )
        )
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == EXIT_ELIGIBLE
    assert artifact["release_eligible"] is True
    assert artifact["threshold_failures"] == []
    assert artifact["provenance"]["baseline_provenance_kind"] == "bootstrap"
    assert artifact["advisories"] == [
        "latency gate skipped: bootstrap baseline (self-measured); "
        "valid only for the first release"
    ]
    assert any("bootstrap baseline" in record.message for record in caplog.records)


def test_canonical_files_digest_is_stable_and_content_sensitive(tmp_path) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "a.jsonl").write_text("one\n", encoding="utf-8")
    initial = canonical_files_digest(first)

    assert initial == canonical_files_digest(first)
    (first / "a.jsonl").write_text("two\n", encoding="utf-8")
    assert canonical_files_digest(first) != initial
