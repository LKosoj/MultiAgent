"""JSONL observability export for local Text-to-SQL eval runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import field_validator, model_validator

from custom_tools.text_to_sql.adaptive.models import (
    Digest,
    Id,
    NonEmptyText,
    NonNegativeInt,
    StrictModel,
)
from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
    PreExecutionGateReceipt,
)


PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION = 1
_PRE_EXECUTION_GATE_COVERAGE_RECORD_KIND = "pre_execution_gate_coverage"
_PRE_EXECUTION_GATE_COVERAGE_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "case_id",
        "fixture_id",
        "fixture_category",
        "receipt",
        "source_coverage",
    }
)
_MAX_PRE_EXECUTION_GATE_COVERAGE_RECORDS = 10_000
_MAX_PRE_EXECUTION_GATE_COVERAGE_BYTES = 64 * 1024 * 1024
_MAX_ADAPTIVE_REPLAY_OBSERVABILITY_RECORDS = 10_000
_MAX_ADAPTIVE_REPLAY_OBSERVABILITY_BYTES = 16 * 1024 * 1024


class HistoricalReplayObservationCategory(StrEnum):
    VERIFIED = "VERIFIED"
    LEGACY = "LEGACY"
    ARTIFACT_TAMPER = "ARTIFACT_TAMPER"
    DURABLE_INPUT_GAP = "DURABLE_INPUT_GAP"
    CONTRACT_CORRUPTION = "CONTRACT_CORRUPTION"


class HistoricalReplayReasonCode(StrEnum):
    VERIFIED = "HISTORICAL_REPLAY_VERIFIED"
    NO_TYPED_PROVENANCE = "NO_TYPED_PROVENANCE"
    MISSING_RESEARCH_ABORT_REDUCER_INPUT = "MISSING_RESEARCH_ABORT_REDUCER_INPUT"
    MISSING_RESEARCH_TRANSITION_INPUT = "MISSING_RESEARCH_TRANSITION_INPUT"
    MISSING_RESEARCH_TERMINAL_INPUT = "MISSING_RESEARCH_TERMINAL_INPUT"
    MISSING_SOLVER_TRANSITION_INPUT = "MISSING_SOLVER_TRANSITION_INPUT"
    TRUSTED_ARTIFACT_DIGEST_MISMATCH = "TRUSTED_ARTIFACT_DIGEST_MISMATCH"
    DURABLE_INPUT_GAP = "DURABLE_INPUT_GAP"
    CONTRACT_CORRUPTION = "CONTRACT_CORRUPTION"


class ReplayReuseReasonCode(StrEnum):
    CURRENT_EVIDENCE_REUSABLE = "CURRENT_EVIDENCE_REUSABLE"
    CURRENT_EVIDENCE_REVALIDATION_REQUIRED = "CURRENT_EVIDENCE_REVALIDATION_REQUIRED"
    HISTORICAL_REPLAY_NOT_VERIFIED = "HISTORICAL_REPLAY_NOT_VERIFIED"
    NOT_EVALUATED = "NOT_EVALUATED"


_LEGACY_REASONS = frozenset(
    {
        HistoricalReplayReasonCode.NO_TYPED_PROVENANCE,
        HistoricalReplayReasonCode.MISSING_RESEARCH_ABORT_REDUCER_INPUT,
        HistoricalReplayReasonCode.MISSING_RESEARCH_TRANSITION_INPUT,
        HistoricalReplayReasonCode.MISSING_RESEARCH_TERMINAL_INPUT,
        HistoricalReplayReasonCode.MISSING_SOLVER_TRANSITION_INPUT,
    }
)
_ERROR_REASON_BY_CATEGORY = {
    HistoricalReplayObservationCategory.ARTIFACT_TAMPER: (
        HistoricalReplayReasonCode.TRUSTED_ARTIFACT_DIGEST_MISMATCH
    ),
    HistoricalReplayObservationCategory.DURABLE_INPUT_GAP: (
        HistoricalReplayReasonCode.DURABLE_INPUT_GAP
    ),
    HistoricalReplayObservationCategory.CONTRACT_CORRUPTION: (
        HistoricalReplayReasonCode.CONTRACT_CORRUPTION
    ),
}


class AdaptiveReplayObservabilityRecord(StrictModel):
    schema_version: Literal[2] = 2
    record_kind: Literal["adaptive_replay_observability"] = (
        "adaptive_replay_observability"
    )
    case_id: NonEmptyText
    run_id: Id
    run_incarnation: Id
    trusted_artifact_digest: Digest
    historical_status: Literal["VERIFIED", "UNVERIFIABLE", "ERROR"]
    historical_category: HistoricalReplayObservationCategory
    historical_reason_code: HistoricalReplayReasonCode
    verified_research_transition_count: NonNegativeInt
    verified_solver_transition_count: NonNegativeInt
    reuse_status: Literal["REUSABLE", "REVALIDATION_REQUIRED", "NOT_EVALUATED"]
    reuse_reason_code: ReplayReuseReasonCode

    @field_validator("trusted_artifact_digest")
    @classmethod
    def validate_trusted_digest(cls, value: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("trusted artifact digest must be exact SHA-256")
        return value

    @model_validator(mode="after")
    def validate_outcomes(self) -> AdaptiveReplayObservabilityRecord:
        historical_is_valid = (
            (
                self.historical_status == "VERIFIED"
                and self.historical_category
                is HistoricalReplayObservationCategory.VERIFIED
                and self.historical_reason_code is HistoricalReplayReasonCode.VERIFIED
            )
            or (
                self.historical_status == "UNVERIFIABLE"
                and self.historical_category
                is HistoricalReplayObservationCategory.LEGACY
                and self.historical_reason_code in _LEGACY_REASONS
            )
            or (
                self.historical_status == "ERROR"
                and _ERROR_REASON_BY_CATEGORY.get(self.historical_category)
                is self.historical_reason_code
            )
        )
        if not historical_is_valid:
            raise ValueError("historical observation is inconsistent")
        if self.historical_status != "VERIFIED" and (
            self.verified_research_transition_count != 0
            or self.verified_solver_transition_count != 0
        ):
            raise ValueError(
                "unverified observation cannot report verified transitions"
            )

        reuse_is_valid = (
            (
                self.reuse_status == "REUSABLE"
                and self.reuse_reason_code
                is ReplayReuseReasonCode.CURRENT_EVIDENCE_REUSABLE
                and self.historical_status == "VERIFIED"
            )
            or (
                self.reuse_status == "REVALIDATION_REQUIRED"
                and (
                    (
                        self.reuse_reason_code
                        is ReplayReuseReasonCode.CURRENT_EVIDENCE_REVALIDATION_REQUIRED
                        and self.historical_status == "VERIFIED"
                    )
                    or (
                        self.reuse_reason_code
                        is ReplayReuseReasonCode.HISTORICAL_REPLAY_NOT_VERIFIED
                        and self.historical_status == "UNVERIFIABLE"
                    )
                )
            )
            or (
                self.reuse_status == "NOT_EVALUATED"
                and self.reuse_reason_code is ReplayReuseReasonCode.NOT_EVALUATED
                and self.historical_status == "ERROR"
            )
        )
        if not reuse_is_valid:
            raise ValueError("reuse observation is inconsistent")
        return self


def adaptive_replay_observability_record(
    *,
    case_id: str,
    envelope: object,
    historical: object,
    reuse: object,
) -> AdaptiveReplayObservabilityRecord:
    """Copy one already anchored replay outcome into its local eval record."""

    from custom_tools.text_to_sql.adaptive.replay_contract import (
        EvidenceReuseResult,
        EvidenceReuseStatus,
        HistoricalReplayResult,
        HistoricalReplayStatus,
        LegacyReplayReason,
        ReplayArtifactEnvelope,
    )

    if type(envelope) is not ReplayArtifactEnvelope:
        raise TypeError("envelope must be ReplayArtifactEnvelope")
    if type(historical) is not HistoricalReplayResult:
        raise TypeError("historical must be HistoricalReplayResult")
    if type(reuse) is not EvidenceReuseResult:
        raise TypeError("reuse must be EvidenceReuseResult")

    payload = envelope.payload
    expected_research_count = (
        len(payload.research_transitions)
        if historical.status is HistoricalReplayStatus.VERIFIED
        else 0
    )
    expected_solver_count = (
        len(payload.solver_steps)
        if historical.status is HistoricalReplayStatus.VERIFIED
        else 0
    )
    expected_research_digest = (
        payload.research_snapshots[-1].digest
        if payload.research_snapshots
        and historical.status is HistoricalReplayStatus.VERIFIED
        else None
    )
    expected_solver_digest = (
        payload.solver_snapshots[-1].digest
        if payload.solver_snapshots
        and historical.status is HistoricalReplayStatus.VERIFIED
        else None
    )
    if (
        payload.historical_status is not historical.status
        or reuse.historical_status is not historical.status
        or payload.legacy_reasons != historical.legacy_reasons
        or reuse.trusted_artifact_digest != historical.trusted_artifact_digest
        or historical.verified_research_transition_count != expected_research_count
        or historical.verified_solver_transition_count != expected_solver_count
        or historical.research_state_digest != expected_research_digest
        or historical.solver_state_digest != expected_solver_digest
    ):
        raise ValueError("replay outcome does not agree with the decoded envelope")

    reason_codes = {
        LegacyReplayReason.NO_TYPED_PROVENANCE: (
            HistoricalReplayReasonCode.NO_TYPED_PROVENANCE
        ),
        LegacyReplayReason.RESEARCH_ABORT_INPUT: (
            HistoricalReplayReasonCode.MISSING_RESEARCH_ABORT_REDUCER_INPUT
        ),
        LegacyReplayReason.RESEARCH_TRANSITION_INPUT: (
            HistoricalReplayReasonCode.MISSING_RESEARCH_TRANSITION_INPUT
        ),
        LegacyReplayReason.RESEARCH_TERMINAL_INPUT: (
            HistoricalReplayReasonCode.MISSING_RESEARCH_TERMINAL_INPUT
        ),
        LegacyReplayReason.SOLVER_TRANSITION_INPUT: (
            HistoricalReplayReasonCode.MISSING_SOLVER_TRANSITION_INPUT
        ),
    }
    if historical.status is HistoricalReplayStatus.VERIFIED:
        historical_category = HistoricalReplayObservationCategory.VERIFIED
        historical_reason = HistoricalReplayReasonCode.VERIFIED
    else:
        if not historical.legacy_reasons:
            raise ValueError("unverifiable replay has no legacy reason")
        historical_category = HistoricalReplayObservationCategory.LEGACY
        historical_reason = reason_codes[historical.legacy_reasons[0]]

    if reuse.status is EvidenceReuseStatus.REUSABLE:
        if historical.status is not HistoricalReplayStatus.VERIFIED:
            raise ValueError("reusable evidence requires verified replay")
        reuse_reason = ReplayReuseReasonCode.CURRENT_EVIDENCE_REUSABLE
    else:
        reuse_reason = (
            ReplayReuseReasonCode.CURRENT_EVIDENCE_REVALIDATION_REQUIRED
            if historical.status is HistoricalReplayStatus.VERIFIED
            else ReplayReuseReasonCode.HISTORICAL_REPLAY_NOT_VERIFIED
        )

    return AdaptiveReplayObservabilityRecord(
        case_id=case_id,
        run_id=payload.run_id,
        run_incarnation=payload.run_incarnation,
        trusted_artifact_digest=historical.trusted_artifact_digest,
        historical_status=historical.status.value,
        historical_category=historical_category,
        historical_reason_code=historical_reason,
        verified_research_transition_count=(
            historical.verified_research_transition_count
        ),
        verified_solver_transition_count=historical.verified_solver_transition_count,
        reuse_status=reuse.status.value,
        reuse_reason_code=reuse_reason,
    )


def canonical_files_digest(path: str | Path) -> str:
    """Hash JSONL evidence with stable file ordering and path boundaries."""
    root = Path(path)
    files = sorted(root.glob("*.jsonl")) if root.is_dir() else [root]
    if not files or any(not item.is_file() for item in files):
        raise ValueError(f"{root}: no digestible JSONL files found")
    digest = hashlib.sha256()
    for item in files:
        relative_name = item.name if root.is_dir() else root.name
        content = item.read_bytes()
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def write_json_evidence(path: str | Path, payload: dict[str, Any]) -> None:
    """Write a deterministic JSON evidence artifact."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def eval_result_observability_record(result: Any) -> dict[str, Any]:
    metrics = getattr(result, "schema_linking_metrics", None)
    return {
        "case_id": getattr(result, "case_id", None),
        "passed": bool(getattr(result, "passed", False)),
        "duration_ms": float(getattr(result, "duration_ms", 0.0) or 0.0),
        "error": getattr(result, "error", None),
        "schema_linking_metrics": asdict(metrics) if is_dataclass(metrics) else None,
    }


def write_eval_observability_jsonl(path: str | Path, results: Iterable[Any]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(
                json.dumps(
                    eval_result_observability_record(result),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            fh.write("\n")
            count += 1
    return count


def write_adaptive_replay_observability_jsonl(
    path: str | Path,
    records: Iterable[AdaptiveReplayObservabilityRecord],
) -> int:
    """Atomically write separate historical-replay and current-reuse outcomes."""

    checked = []
    for record in records:
        if len(checked) >= _MAX_ADAPTIVE_REPLAY_OBSERVABILITY_RECORDS:
            raise ValueError("too many adaptive replay observability records")
        if type(record) is not AdaptiveReplayObservabilityRecord:
            raise TypeError("record must be AdaptiveReplayObservabilityRecord")
        canonical = AdaptiveReplayObservabilityRecord.model_validate(
            record.model_dump(mode="python", round_trip=True, warnings="error")
        )
        if canonical != record:
            raise ValueError("adaptive replay observability record is not canonical")
        checked.append(canonical)
    checked.sort(key=lambda item: item.case_id)
    if len({item.case_id for item in checked}) != len(checked):
        raise ValueError("duplicate adaptive replay observability case_id")
    lines = [
        json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for item in checked
    ]
    if sum(len(line.encode("utf-8")) for line in lines) > (
        _MAX_ADAPTIVE_REPLAY_OBSERVABILITY_BYTES
    ):
        raise ValueError("adaptive replay observability artifact is too large")
    _atomic_write_lines(Path(path), lines)
    return len(checked)


def pre_execution_gate_coverage_record(
    receipt: PreExecutionGateReceipt,
    *,
    case_id: str,
    fixture_id: str,
    fixture_category: str,
) -> dict[str, Any]:
    """Project only evidence already retained by one W5-05 gate receipt."""

    if type(receipt) is not PreExecutionGateReceipt:
        raise TypeError("receipt must be PreExecutionGateReceipt")
    receipt = _canonical_pre_execution_gate_receipt(receipt)
    for name, value in (
        ("case_id", case_id),
        ("fixture_id", fixture_id),
        ("fixture_category", fixture_category),
    ):
        if type(value) is not str or not value.strip():
            raise ValueError(f"{name} must be non-empty text")

    return {
        "schema_version": PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION,
        "record_kind": _PRE_EXECUTION_GATE_COVERAGE_RECORD_KIND,
        "case_id": case_id.strip(),
        "fixture_id": fixture_id.strip(),
        "fixture_category": fixture_category.strip(),
        "receipt": receipt.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
        "source_coverage": _source_coverage_from_receipt(receipt),
    }


def write_pre_execution_gate_coverage_jsonl(
    path: str | Path,
    records: Iterable[dict[str, Any]],
) -> int:
    """Atomically write a bounded, deterministic W5-06 evidence matrix."""

    normalized: list[dict[str, Any]] = []
    for record in records:
        if len(normalized) >= _MAX_PRE_EXECUTION_GATE_COVERAGE_RECORDS:
            raise ValueError("too many pre-execution gate coverage records")
        normalized.append(_validate_pre_execution_gate_coverage_record(record))
    normalized.sort(key=lambda row: (row["fixture_id"], row["case_id"]))
    identities = [(row["fixture_id"], row["case_id"]) for row in normalized]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate pre-execution gate coverage record")
    case_ids = [row["case_id"] for row in normalized]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate case_id in pre-execution gate coverage records")

    try:
        lines = [
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
            for record in normalized
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "pre-execution gate coverage record must be canonical JSON"
        ) from exc
    if sum(len(line.encode("utf-8")) for line in lines) > (
        _MAX_PRE_EXECUTION_GATE_COVERAGE_BYTES
    ):
        raise ValueError("pre-execution gate coverage artifact is too large")

    output = Path(path)
    _atomic_write_lines(output, lines)
    return len(normalized)


def _validate_pre_execution_gate_coverage_record(
    record: dict[str, Any],
) -> dict[str, Any]:
    if type(record) is not dict or set(record) != _PRE_EXECUTION_GATE_COVERAGE_FIELDS:
        raise ValueError("pre-execution gate coverage record fields are invalid")
    try:
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "pre-execution gate coverage record must be canonical JSON"
        ) from exc
    if record["schema_version"] != PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION:
        raise ValueError("pre-execution gate coverage schema version is invalid")
    if record["record_kind"] != _PRE_EXECUTION_GATE_COVERAGE_RECORD_KIND:
        raise ValueError("pre-execution gate coverage record kind is invalid")
    for name in ("case_id", "fixture_id", "fixture_category"):
        value = record[name]
        if type(value) is not str or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be non-empty text")
    if type(record["receipt"]) is not dict:
        raise ValueError("pre-execution gate coverage receipt must be an object")
    try:
        receipt = PreExecutionGateReceipt.model_validate_json(
            json.dumps(
                record["receipt"],
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
        receipt = _canonical_pre_execution_gate_receipt(receipt)
    except Exception as exc:
        raise ValueError("pre-execution gate coverage receipt is invalid") from exc
    canonical_receipt = receipt.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )
    if record["receipt"] != canonical_receipt:
        raise ValueError("pre-execution gate coverage receipt is not canonical")
    source_coverage = _source_coverage_from_receipt(receipt)
    if record["source_coverage"] != source_coverage:
        raise ValueError("source_coverage does not match the receipt AST coverage")
    return {
        "schema_version": PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION,
        "record_kind": _PRE_EXECUTION_GATE_COVERAGE_RECORD_KIND,
        "case_id": record["case_id"],
        "fixture_id": record["fixture_id"],
        "fixture_category": record["fixture_category"],
        "receipt": canonical_receipt,
        "source_coverage": source_coverage,
    }


def _canonical_pre_execution_gate_receipt(
    receipt: PreExecutionGateReceipt,
) -> PreExecutionGateReceipt:
    try:
        canonical = PreExecutionGateReceipt.model_validate(
            receipt.model_dump(
                mode="python",
                round_trip=True,
                warnings="error",
            )
        )
    except Exception as exc:
        raise ValueError("pre-execution gate receipt is not canonical") from exc
    if canonical != receipt:
        raise ValueError("pre-execution gate receipt changed during validation")
    return canonical


def _source_coverage_from_receipt(
    receipt: PreExecutionGateReceipt,
) -> dict[str, Any] | None:
    if not receipt.source_coverage_available:
        return None
    coverage = receipt.semantic_coverage
    if coverage is None:
        raise ValueError("source coverage requires AST coverage")
    return {
        "required_source_ids": list(coverage.required_source_ids),
        "evidence_ids": list(coverage.evidence_ids),
        "nodes": [
            {
                "node_id": annotation.node_id,
                "expression_field": annotation.expression_field,
                "expression_index": annotation.expression_index,
                "expression_path": [
                    {
                        "argument": segment.argument,
                        "ordinal": segment.ordinal,
                    }
                    for segment in annotation.expression_path
                ],
                "source_ids": list(annotation.source_ids),
                "evidence_ids": list(annotation.evidence_ids),
            }
            for annotation in coverage.annotations
        ],
    }


def _atomic_write_lines(output: Path, lines: list[str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
