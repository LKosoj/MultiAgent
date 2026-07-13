"""Fail-closed release policy and protected-lane evidence helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .observability import write_json_evidence


EXIT_ELIGIBLE = 0
EXIT_CONTRACT = 2
EXIT_APPROVAL = 3
EXIT_PROTECTED_LANE = 4
EXIT_INFRASTRUCTURE = 5

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_EVIDENCE_FIELDS = frozenset({"schema_version", "candidate", "lanes"})
_CANDIDATE_FIELDS = frozenset(
    {"commit", "artifact_digest", "lock_digest", "generated_at"}
)
_LANE_FIELDS = frozenset(
    {"lane_id", "dialect", "production_ready", "reason_code", "probe_kind"}
)
_BASELINE_DOCUMENT_FIELDS = frozenset({"schema_version", "baseline", "approval"})
_BASELINE_FIELDS = frozenset(
    {
        "baseline_id",
        "provenance_kind",
        "baseline_commit",
        "baseline_digest",
        "for_candidate_commit",
        "for_candidate_digest",
        "measured_at",
        "model_id",
        "provider_id",
        "model_config_digest",
        "gold_sha256",
        "latency_p95_ms",
    }
)
_BASELINE_PROVENANCE_KINDS = frozenset({"prior_release", "bootstrap"})
_BASELINE_APPROVAL_FIELDS = frozenset(
    {
        "status",
        "approved_by",
        "approved_at",
        "valid_until",
        "approval_ref",
        "payload_sha256",
    }
)
_THRESHOLD_FIELDS = frozenset(
    {
        "execution_accuracy_overall_min",
        "execution_accuracy_per_slice_min",
        "schema_table_recall_min",
        "schema_column_f1_min",
        "expected_abstention_precision_min",
        "expected_abstention_recall_min",
        "unexpected_error_rate_max",
        "contract_and_safety_case_pass_rate_min",
        "latency_p95_regression_max_fraction",
        "repetitions",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "policy_version",
        "thresholds",
        "required_slices",
        "required_dialects",
        "required_protected_lanes",
        "approval",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "status",
        "approved_by",
        "approved_at",
        "approval_ref",
        "thresholds_sha256",
    }
)


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_policy_scope_digest(
    thresholds: Mapping[str, Any],
    required_slices: Iterable[str],
    required_dialects: Iterable[str],
    required_protected_lanes: Iterable[str],
) -> str:
    """Hash every release-policy field covered by human approval."""
    return _canonical_sha256(
        {
            "required_dialects": sorted(str(item) for item in required_dialects),
            "required_protected_lanes": sorted(
                str(item) for item in required_protected_lanes
            ),
            "required_slices": sorted(str(item) for item in required_slices),
            "thresholds": dict(thresholds),
        }
    )


def canonical_baseline_payload_digest(baseline: Mapping[str, Any]) -> str:
    """Hash the exact latency-baseline payload covered by approval."""
    return _canonical_sha256(dict(baseline))


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field_name} entries must be non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} entries must be unique")
    return normalized


def _validate_thresholds(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or set(value) != _THRESHOLD_FIELDS:
        raise ValueError("threshold fields do not match policy v1")
    thresholds = dict(value)
    repetitions = thresholds["repetitions"]
    if type(repetitions) is not int or repetitions <= 0:
        raise ValueError("thresholds.repetitions must be a positive integer")
    for name, raw in thresholds.items():
        if name == "repetitions":
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"thresholds.{name} must be numeric")
        numeric = float(raw)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"thresholds.{name} must be finite and non-negative")
        if name != "latency_p95_regression_max_fraction" and numeric > 1:
            raise ValueError(f"thresholds.{name} must be between 0 and 1")
    return thresholds


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"approval.{field_name} must be non-empty text or null")
    return value.strip()


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    commit: str
    artifact_digest: str
    lock_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.commit, str) or not _COMMIT_RE.fullmatch(self.commit):
            raise ValueError("candidate commit must be lowercase hexadecimal")
        for name in ("artifact_digest", "lock_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError(f"candidate {name} must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ApprovedLatencyBaseline:
    baseline_id: str
    provenance_kind: str
    baseline_commit: str
    baseline_digest: str
    for_candidate_commit: str | None
    for_candidate_digest: str | None
    measured_at: str
    model_id: str
    provider_id: str
    model_config_digest: str
    gold_sha256: str
    latency_p95_ms: float
    approved_by: str
    approved_at: str
    valid_until: str
    approval_ref: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class PolicyApproval:
    status: str
    approved_by: str | None
    approved_at: str | None
    approval_ref: str | None
    thresholds_sha256: str

    @classmethod
    def from_mapping(cls, value: Any) -> "PolicyApproval":
        if not isinstance(value, Mapping) or set(value) != _APPROVAL_FIELDS:
            raise ValueError("approval fields do not match policy v1")
        status = value.get("status")
        if status not in {"pending", "approved"}:
            raise ValueError("approval.status must be pending or approved")
        digest = value.get("thresholds_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError("approval.thresholds_sha256 must be a lowercase SHA-256")
        record = cls(
            status=status,
            approved_by=_optional_text(value.get("approved_by"), field_name="approved_by"),
            approved_at=_optional_text(value.get("approved_at"), field_name="approved_at"),
            approval_ref=_optional_text(value.get("approval_ref"), field_name="approval_ref"),
            thresholds_sha256=digest,
        )
        metadata = (record.approved_by, record.approved_at, record.approval_ref)
        if status == "approved" and not all(metadata):
            raise ValueError("approved policy requires complete approval metadata")
        if status == "pending" and any(item is not None for item in metadata):
            raise ValueError("pending policy approval metadata must be null")
        return record


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    version: int
    thresholds: dict[str, int | float]
    required_slices: tuple[str, ...]
    required_dialects: tuple[str, ...]
    required_protected_lanes: tuple[str, ...]
    approval: PolicyApproval

    @classmethod
    def load(cls, path: str | Path) -> "ReleasePolicy":
        source = Path(path)
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or set(raw) != _POLICY_FIELDS:
            raise ValueError(f"{source}: fields do not match release policy v1")
        if raw.get("policy_version") != 1:
            raise ValueError(f"{source}: policy_version must be 1")
        return cls(
            version=1,
            thresholds=_validate_thresholds(raw.get("thresholds")),
            required_slices=_string_tuple(
                raw.get("required_slices"), field_name="required_slices"
            ),
            required_dialects=_string_tuple(
                raw.get("required_dialects"), field_name="required_dialects"
            ),
            required_protected_lanes=_string_tuple(
                raw.get("required_protected_lanes"),
                field_name="required_protected_lanes",
            ),
            approval=PolicyApproval.from_mapping(raw.get("approval")),
        )

    @property
    def scope_digest(self) -> str:
        return canonical_policy_scope_digest(
            self.thresholds,
            self.required_slices,
            self.required_dialects,
            self.required_protected_lanes,
        )

    @property
    def approval_is_current(self) -> bool:
        return (
            self.approval.status == "approved"
            and self.approval.thresholds_sha256 == self.scope_digest
        )


def _required_text(value: Any, *, field_name: str, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {field_name} must be non-empty text")
    return value.strip()


def _utc_datetime(value: Any, *, field_name: str, source: Path) -> datetime:
    text = _required_text(value, field_name=field_name, source=source)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{source}: {field_name} must be a valid UTC time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{source}: {field_name} must be a valid UTC time")
    return parsed.astimezone(timezone.utc)


def _sha256_text(value: Any, *, field_name: str, source: Path) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{source}: {field_name} must be a lowercase SHA-256")
    return value


def load_approved_latency_baseline(
    path: str | Path,
    *,
    expected_candidate: CandidateIdentity,
    expected_model_id: str,
    expected_provider_id: str,
    expected_model_config_digest: str,
    expected_gold_sha256: str,
) -> ApprovedLatencyBaseline:
    """Load one approved, current and identity-bound latency baseline."""
    source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or set(raw) != _BASELINE_DOCUMENT_FIELDS:
        raise ValueError(f"{source}: fields do not match latency baseline v1")
    schema_version = raw.get("schema_version")
    if schema_version == 1:
        raise ValueError(
            f"{source}: schema_version 1 (candidate-bound) is no longer accepted; "
            "re-approve using schema_version 2"
        )
    if schema_version != 2:
        raise ValueError(f"{source}: schema_version must be 2")
    baseline = raw.get("baseline")
    approval = raw.get("approval")
    if not isinstance(baseline, Mapping) or set(baseline) != _BASELINE_FIELDS:
        raise ValueError(f"{source}: baseline fields do not match schema v1")
    if not isinstance(approval, Mapping) or set(approval) != _BASELINE_APPROVAL_FIELDS:
        raise ValueError(f"{source}: approval fields do not match baseline schema v1")

    baseline_id = _required_text(
        baseline.get("baseline_id"), field_name="baseline.baseline_id", source=source
    )
    provenance_kind = _required_text(
        baseline.get("provenance_kind"),
        field_name="baseline.provenance_kind",
        source=source,
    )
    if provenance_kind not in _BASELINE_PROVENANCE_KINDS:
        raise ValueError(
            f"{source}: baseline.provenance_kind must be prior_release or bootstrap"
        )
    baseline_commit = _required_text(
        baseline.get("baseline_commit"),
        field_name="baseline.baseline_commit",
        source=source,
    )
    if not _COMMIT_RE.fullmatch(baseline_commit):
        raise ValueError(f"{source}: baseline.baseline_commit must be lowercase hex")
    baseline_digest = _sha256_text(
        baseline.get("baseline_digest"),
        field_name="baseline.baseline_digest",
        source=source,
    )
    for_candidate_commit = baseline.get("for_candidate_commit")
    if for_candidate_commit is not None and (
        not isinstance(for_candidate_commit, str)
        or not _COMMIT_RE.fullmatch(for_candidate_commit)
    ):
        raise ValueError(
            f"{source}: baseline.for_candidate_commit must be lowercase hex or null"
        )
    for_candidate_digest = baseline.get("for_candidate_digest")
    if for_candidate_digest is not None and (
        not isinstance(for_candidate_digest, str)
        or not _SHA256_RE.fullmatch(for_candidate_digest)
    ):
        raise ValueError(
            f"{source}: baseline.for_candidate_digest must be a lowercase SHA-256 or null"
        )
    if (for_candidate_commit is None) != (for_candidate_digest is None):
        raise ValueError(
            f"{source}: baseline.for_candidate_commit and for_candidate_digest "
            "must be both set or both null"
        )
    model_id = _required_text(
        baseline.get("model_id"), field_name="baseline.model_id", source=source
    )
    provider_id = _required_text(
        baseline.get("provider_id"), field_name="baseline.provider_id", source=source
    )
    model_config_digest = _sha256_text(
        baseline.get("model_config_digest"),
        field_name="baseline.model_config_digest",
        source=source,
    )
    gold_sha256 = _sha256_text(
        baseline.get("gold_sha256"),
        field_name="baseline.gold_sha256",
        source=source,
    )
    latency = baseline.get("latency_p95_ms")
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(float(latency))
        or float(latency) <= 0
    ):
        raise ValueError(f"{source}: baseline.latency_p95_ms must be positive and finite")

    if approval.get("status") != "approved":
        raise ValueError(f"{source}: latency baseline approval must be approved")
    approved_by = _required_text(
        approval.get("approved_by"), field_name="approval.approved_by", source=source
    )
    approval_ref = _required_text(
        approval.get("approval_ref"), field_name="approval.approval_ref", source=source
    )
    payload_sha256 = _sha256_text(
        approval.get("payload_sha256"),
        field_name="approval.payload_sha256",
        source=source,
    )
    if payload_sha256 != canonical_baseline_payload_digest(baseline):
        raise ValueError(f"{source}: approval.payload_sha256 does not match baseline")

    measured_at = _utc_datetime(
        baseline.get("measured_at"), field_name="baseline.measured_at", source=source
    )
    approved_at = _utc_datetime(
        approval.get("approved_at"), field_name="approval.approved_at", source=source
    )
    valid_until = _utc_datetime(
        approval.get("valid_until"), field_name="approval.valid_until", source=source
    )
    now = datetime.now(timezone.utc)
    if not measured_at <= approved_at <= now:
        raise ValueError(f"{source}: baseline approval time order is invalid")
    if now > valid_until:
        raise ValueError(f"{source}: latency baseline approval is expired")

    expected = {
        "model_id": expected_model_id,
        "provider_id": expected_provider_id,
        "model_config_digest": expected_model_config_digest,
        "gold_sha256": expected_gold_sha256,
    }
    actual = {
        "model_id": model_id,
        "provider_id": provider_id,
        "model_config_digest": model_config_digest,
        "gold_sha256": gold_sha256,
    }
    for field_name, expected_value in expected.items():
        if actual[field_name] != expected_value:
            raise ValueError(f"{source}: baseline.{field_name} does not match current release")
    if (
        for_candidate_commit is not None
        and for_candidate_commit != expected_candidate.commit
    ):
        raise ValueError(
            f"{source}: baseline.for_candidate_commit does not match current release"
        )
    if (
        for_candidate_digest is not None
        and for_candidate_digest != expected_candidate.artifact_digest
    ):
        raise ValueError(
            f"{source}: baseline.for_candidate_digest does not match current release"
        )
    if provenance_kind == "prior_release" and (
        baseline_commit == expected_candidate.commit
        or baseline_digest == expected_candidate.artifact_digest
    ):
        raise ValueError(
            f"{source}: prior_release baseline must not be self-referential to "
            "the current candidate"
        )
    if provenance_kind == "bootstrap" and (
        baseline_commit != expected_candidate.commit
        or baseline_digest != expected_candidate.artifact_digest
    ):
        raise ValueError(
            f"{source}: bootstrap baseline must be measured on the current candidate"
        )
    return ApprovedLatencyBaseline(
        baseline_id=baseline_id,
        provenance_kind=provenance_kind,
        baseline_commit=baseline_commit,
        baseline_digest=baseline_digest,
        for_candidate_commit=for_candidate_commit,
        for_candidate_digest=for_candidate_digest,
        measured_at=str(baseline["measured_at"]),
        model_id=model_id,
        provider_id=provider_id,
        model_config_digest=model_config_digest,
        gold_sha256=gold_sha256,
        latency_p95_ms=float(latency),
        approved_by=approved_by,
        approved_at=str(approval["approved_at"]),
        valid_until=str(approval["valid_until"]),
        approval_ref=approval_ref,
        payload_sha256=payload_sha256,
    )


def _fresh_generated_at(value: Any, *, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: candidate.generated_at is required")
    try:
        generated_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{source}: candidate.generated_at is invalid") from exc
    if generated_at.tzinfo is None:
        raise ValueError(f"{source}: candidate.generated_at must include timezone")
    age = datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)
    if age < timedelta(0) or age > timedelta(hours=24):
        raise ValueError(f"{source}: protected evidence is stale")
    return value


def _candidate_from_mapping(value: Any, *, source: Path) -> CandidateIdentity:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise ValueError(f"{source}: candidate fields do not match evidence v1")
    identity = CandidateIdentity(
        commit=value.get("commit"),
        artifact_digest=value.get("artifact_digest"),
        lock_digest=value.get("lock_digest"),
    )
    _fresh_generated_at(value.get("generated_at"), source=source)
    return identity


def load_protected_lane_evidence(
    paths: Iterable[str | Path],
    *,
    expected_candidate: CandidateIdentity,
    required_driver_lanes: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Validate and index fresh real-driver evidence for one exact candidate."""
    allowed_lanes = set(required_driver_lanes)
    if not all(lane.startswith("driver:") for lane in allowed_lanes):
        raise ValueError("required_driver_lanes may contain only driver lanes")
    indexed: dict[str, dict[str, Any]] = {}
    for source_value in paths:
        source = Path(source_value)
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or set(raw) != _EVIDENCE_FIELDS:
            raise ValueError(f"{source}: fields do not match protected evidence v1")
        if raw.get("schema_version") != 1:
            raise ValueError(f"{source}: schema_version must be 1")
        if _candidate_from_mapping(raw.get("candidate"), source=source) != expected_candidate:
            raise ValueError(f"{source}: protected evidence candidate does not match")
        lanes = raw.get("lanes")
        if not isinstance(lanes, list):
            raise ValueError(f"{source}: lanes must be a list")
        for raw_lane in lanes:
            if not isinstance(raw_lane, Mapping):
                raise ValueError(f"{source}: lane evidence must be an object")
            fields = set(raw_lane)
            if fields not in {_LANE_FIELDS, _LANE_FIELDS | {"health"}}:
                raise ValueError(f"{source}: lane fields do not match evidence v1")
            lane = dict(raw_lane)
            lane_id = lane.get("lane_id")
            if lane_id not in allowed_lanes:
                raise ValueError(f"{source}: unknown protected lane {lane_id!r}")
            if lane_id in indexed:
                raise ValueError(f"{source}: duplicate protected lane {lane_id!r}")
            dialect = lane.get("dialect")
            if dialect != lane_id.split(":", 1)[1]:
                raise ValueError(f"{source}: lane dialect does not match lane_id")
            if type(lane.get("production_ready")) is not bool:
                raise ValueError(f"{source}: production_ready must be boolean")
            reason_code = lane.get("reason_code")
            if not isinstance(reason_code, str) or not reason_code:
                raise ValueError(f"{source}: reason_code must be non-empty text")
            if (reason_code == "READY") is not lane["production_ready"]:
                raise ValueError(f"{source}: READY consistency violation")
            if lane.get("probe_kind") != "real_driver":
                raise ValueError(f"{source}: driver probe_kind must be real_driver")
            if "health" in lane and not isinstance(lane["health"], Mapping):
                raise ValueError(f"{source}: lane health must be an object")
            indexed[lane_id] = lane
    return indexed


def _probe_candidate(
    *,
    commit: str | None,
    artifact_digest: str | None,
    lock_digest: str | None,
    generated_at: str | None,
) -> tuple[CandidateIdentity, str]:
    identity = CandidateIdentity(
        commit=commit or os.getenv("TEXT2SQL_EVAL_COMMIT", ""),
        artifact_digest=artifact_digest
        or os.getenv("TEXT2SQL_EVAL_CANDIDATE_DIGEST", ""),
        lock_digest=lock_digest or os.getenv("TEXT2SQL_EVAL_LOCK_DIGEST", ""),
    )
    timestamp = generated_at or datetime.now(timezone.utc).isoformat()
    _fresh_generated_at(timestamp, source=Path("<probe>"))
    return identity, timestamp


def probe_required_driver_lanes(
    dialects: Iterable[str],
    *,
    evidence_path: str | Path,
    commit: str | None = None,
    artifact_digest: str | None = None,
    lock_digest: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Probe configured real-driver DSNs and bind results to one candidate."""
    from db_plugins.manager import get_plugin

    candidate, timestamp = _probe_candidate(
        commit=commit,
        artifact_digest=artifact_digest,
        lock_digest=lock_digest,
        generated_at=generated_at,
    )
    normalized_dialects = tuple(str(item).strip().lower() for item in dialects)
    if not normalized_dialects or any(not item for item in normalized_dialects):
        raise ValueError("driver dialects must be non-empty")
    if len(set(normalized_dialects)) != len(normalized_dialects):
        raise ValueError("driver dialects must be unique")
    lanes: list[dict[str, Any]] = []
    for dialect in normalized_dialects:
        base = {
            "lane_id": f"driver:{dialect}",
            "dialect": dialect,
            "probe_kind": "real_driver",
        }
        dsn = os.getenv(f"TEXT2SQL_{dialect.upper()}_DSN")
        if not dsn:
            lanes.append(
                {
                    **base,
                    "production_ready": False,
                    "reason_code": "DSN_UNAVAILABLE",
                }
            )
            continue
        connection = None
        plugin = None
        lane: dict[str, Any]
        try:
            plugin = get_plugin(dsn)
            if getattr(plugin, "dialect", None) != dialect:
                lane = {
                    **base,
                    "production_ready": False,
                    "reason_code": "DIALECT_MISMATCH",
                }
            else:
                connection = plugin.connect(dsn)
                health = plugin.probe_capabilities(connection, dsn)
                if health.dialect != dialect:
                    lane = {
                        **base,
                        "production_ready": False,
                        "reason_code": "DIALECT_MISMATCH",
                    }
                else:
                    lane = {
                        **base,
                        "production_ready": health.production_ready,
                        "reason_code": (
                            "READY"
                            if health.production_ready
                            else ",".join(health.reason_codes)
                            or "CAPABILITY_PROBE_FAILED"
                        ),
                        "health": health.to_mapping(),
                    }
        except Exception as exc:
            lane = {
                **base,
                "production_ready": False,
                "reason_code": f"PROBE_ERROR:{type(exc).__name__}",
            }
        if connection is not None and plugin is not None:
            try:
                plugin.close(connection)
            except Exception as exc:
                lane = {
                    **base,
                    "production_ready": False,
                    "reason_code": f"CLOSE_ERROR:{type(exc).__name__}",
                }
        lanes.append(lane)
    evidence = {
        "schema_version": 1,
        "candidate": {
            "commit": candidate.commit,
            "artifact_digest": candidate.artifact_digest,
            "lock_digest": candidate.lock_digest,
            "generated_at": timestamp,
        },
        "lanes": lanes,
    }
    write_json_evidence(evidence_path, evidence)
    return evidence


__all__ = [
    "EXIT_APPROVAL",
    "EXIT_CONTRACT",
    "EXIT_ELIGIBLE",
    "EXIT_INFRASTRUCTURE",
    "EXIT_PROTECTED_LANE",
    "ApprovedLatencyBaseline",
    "CandidateIdentity",
    "PolicyApproval",
    "ReleasePolicy",
    "canonical_baseline_payload_digest",
    "canonical_policy_scope_digest",
    "load_approved_latency_baseline",
    "load_protected_lane_evidence",
    "probe_required_driver_lanes",
]
