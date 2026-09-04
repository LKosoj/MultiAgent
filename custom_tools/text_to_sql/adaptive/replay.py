"""Anchored codec and pure deterministic adaptive replay API."""

from __future__ import annotations

import hmac
import json
import re

from pydantic import ValidationError

from .freshness import (
    DataSnapshotStatus,
    FreshnessContext,
    FreshnessStatus,
)
from .models import EvidenceValidityScope
from .replay_contract import (
    AdaptiveReplayPayload,
    CanonicalReplayBlob,
    EvidenceReuseResult,
    EvidenceReuseStatus,
    FinalizerExecutionRequest,
    HistoricalReplayResult,
    HistoricalReplayStatus,
    LegacyReplayReason,
    ReplayArtifactAttachment,
    ReplayArtifactEnvelope,
    ReplayContractError,
    ResearchAbortedReplayAction,
    ResearchObservedReplayAction,
    ResearchPlannedReplayAction,
    ResearchReplayAbortJournal,
    ResearchReplaySnapshot,
    ResearchReplayTerminal,
    ResearchReplayTransition,
    ResearchTerminalReplayAction,
    SolverCheckReplayAction,
    SolverExecutionReconciliation,
    SolverExecutionReplayAction,
    SolverExecutionReplayStep,
    SolverReentryAdmittedReplayAction,
    SolverReentryFinalizedReplayAction,
    SolverSemanticRepairFallbackReplayAction,
    SolverReplaySnapshot,
    SolverReplayTerminal,
    SolverStopReplayAction,
    SolverTransitionReplayStep,
    sha256_digest,
)
from .replay_engine import replay_envelope as _replay_envelope
from .serialization import (
    DEFAULT_LIMITS,
    SerializationLimits,
    canonical_json_bytes,
)


def encode_replay_artifact(
    payload: AdaptiveReplayPayload,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> bytes:
    if type(payload) is not AdaptiveReplayPayload:
        raise TypeError("payload must be AdaptiveReplayPayload")
    try:
        checked = AdaptiveReplayPayload.model_validate(
            payload.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ReplayContractError(
            "payload does not satisfy the closed contract"
        ) from exc
    if checked != payload:
        raise ReplayContractError("payload changed during exact validation")
    payload_bytes = canonical_json_bytes(checked, limits=limits)
    _reject_forbidden_fields(json.loads(payload_bytes))
    envelope = ReplayArtifactEnvelope(
        payload=checked,
        payload_digest=sha256_digest(payload_bytes),
        byte_count=len(payload_bytes),
    )
    return canonical_json_bytes(envelope, limits=limits)


def decode_replay_artifact(
    raw: bytes,
    *,
    trusted_artifact_digest: str,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> ReplayArtifactEnvelope:
    """Decode only bytes authenticated by a separately retained SHA-256 anchor."""

    raw_bytes = _anchored_bytes(raw, trusted_artifact_digest)
    return _decode_anchored(raw_bytes, limits=limits)


def replay_adaptive_artifact(
    raw: bytes,
    *,
    trusted_artifact_digest: str,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> HistoricalReplayResult:
    """Replay the exact artifact authenticated by ``trusted_artifact_digest``."""

    raw_bytes = _anchored_bytes(raw, trusted_artifact_digest)
    artifact = _decode_anchored(raw_bytes, limits=limits)
    return _replay_envelope(artifact, trusted_artifact_digest)


def evaluate_replay_evidence_reuse(
    raw: bytes,
    freshness_context: FreshnessContext,
    *,
    trusted_artifact_digest: str,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> EvidenceReuseResult:
    """Replay anchored history first, then evaluate evidence against current facts."""

    from .freshness import project_evidence_freshness

    if type(freshness_context) is not FreshnessContext:
        raise TypeError("freshness_context must be FreshnessContext")
    raw_bytes = _anchored_bytes(raw, trusted_artifact_digest)
    artifact = _decode_anchored(raw_bytes, limits=limits)
    historical = _replay_envelope(artifact, trusted_artifact_digest)
    states = artifact.payload.research_snapshots
    evidence = () if not states else states[-1].state.evidence
    projection = project_evidence_freshness(evidence, freshness_context)
    decisions = {item.evidence_id: item for item in projection.decisions}
    snapshot_statuses = {
        item.token: item.status for item in freshness_context.data_snapshots
    }
    reusable = historical.status is HistoricalReplayStatus.VERIFIED and all(
        (snapshot_statuses.get(item.data_snapshot_token) is DataSnapshotStatus.VALID)
        if item.validity_scope is EvidenceValidityScope.DATA_SNAPSHOT
        else decisions[item.evidence_id].status is FreshnessStatus.FRESH
        for item in evidence
    )
    return EvidenceReuseResult(
        status=(
            EvidenceReuseStatus.REUSABLE
            if reusable
            else EvidenceReuseStatus.REVALIDATION_REQUIRED
        ),
        projection=projection,
        historical_status=historical.status,
        trusted_artifact_digest=trusted_artifact_digest,
    )


def _decode_anchored(
    raw_bytes: bytes,
    *,
    limits: SerializationLimits,
) -> ReplayArtifactEnvelope:
    if len(raw_bytes) > limits.max_state_bytes:
        raise ReplayContractError("replay artifact exceeds max_state_bytes")
    try:
        document = json.loads(raw_bytes, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ReplayContractError("replay artifact is not valid JSON") from None
    if not isinstance(document, dict):
        raise ReplayContractError("replay artifact root must be an object")
    _reject_forbidden_fields(document)
    try:
        canonical = canonical_json_bytes(document, limits=limits)
    except ValueError:
        raise ReplayContractError("replay artifact is not canonical JSON") from None
    if not hmac.compare_digest(raw_bytes, canonical):
        raise ReplayContractError("replay artifact is not canonical JSON")
    payload_value = document.get("payload")
    if not isinstance(payload_value, dict):
        raise ReplayContractError("payload must be an object")
    payload_bytes = canonical_json_bytes(payload_value, limits=limits)
    if document.get("byte_count") != len(payload_bytes):
        raise ReplayContractError("payload byte_count does not match")
    expected_digest = document.get("payload_digest")
    if not isinstance(expected_digest, str) or not hmac.compare_digest(
        expected_digest,
        sha256_digest(payload_bytes),
    ):
        raise ReplayContractError("payload_digest does not match")
    try:
        envelope = ReplayArtifactEnvelope.model_validate_json(canonical)
        checked = ReplayArtifactEnvelope.model_validate(
            envelope.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError):
        raise ReplayContractError(
            "payload does not satisfy the closed contract"
        ) from None
    if checked != envelope:
        raise ReplayContractError("payload changed during exact validation")
    return checked


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _anchored_bytes(raw: bytes, trusted_artifact_digest: str) -> bytes:
    if type(raw) is not bytes:
        raise TypeError("raw must be exact bytes")
    if type(trusted_artifact_digest) is not str or not _SHA256_RE.fullmatch(
        trusted_artifact_digest
    ):
        raise ValueError("trusted_artifact_digest must be exact sha256 lowercase hex")
    if not hmac.compare_digest(sha256_digest(raw), trusted_artifact_digest):
        raise ReplayContractError("trusted artifact digest does not match raw bytes")
    return raw


_FORBIDDEN_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "benchmark",
        "chain_of_thought",
        "cot",
        "credentials",
        "dsn",
        "gold",
        "gold_sql",
        "password",
        "prompt",
        "secret",
        "transcript",
    }
)


def _reject_forbidden_fields(value: object) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
                if normalized in _FORBIDDEN_FIELDS:
                    raise ReplayContractError(
                        f"replay payload contains forbidden field {key!r}"
                    )
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


__all__ = [
    "AdaptiveReplayPayload",
    "CanonicalReplayBlob",
    "EvidenceReuseResult",
    "EvidenceReuseStatus",
    "FinalizerExecutionRequest",
    "HistoricalReplayResult",
    "HistoricalReplayStatus",
    "LegacyReplayReason",
    "ReplayArtifactAttachment",
    "ReplayArtifactEnvelope",
    "ReplayContractError",
    "ResearchAbortedReplayAction",
    "ResearchObservedReplayAction",
    "ResearchPlannedReplayAction",
    "ResearchReplayAbortJournal",
    "ResearchReplaySnapshot",
    "ResearchReplayTerminal",
    "ResearchReplayTransition",
    "ResearchTerminalReplayAction",
    "SolverCheckReplayAction",
    "SolverExecutionReconciliation",
    "SolverExecutionReplayAction",
    "SolverExecutionReplayStep",
    "SolverReentryAdmittedReplayAction",
    "SolverReentryFinalizedReplayAction",
    "SolverSemanticRepairFallbackReplayAction",
    "SolverReplaySnapshot",
    "SolverReplayTerminal",
    "SolverStopReplayAction",
    "SolverTransitionReplayStep",
    "decode_replay_artifact",
    "encode_replay_artifact",
    "evaluate_replay_evidence_reuse",
    "replay_adaptive_artifact",
]
