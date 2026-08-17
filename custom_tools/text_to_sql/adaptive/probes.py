"""Immutable results for bounded adaptive research probes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
import json
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .models import (
    ContractModel,
    Digest,
    EvidenceCost,
    Id,
    NonNegativeInt,
    ResearchActionKind,
    TargetRef,
)
from .provenance import (
    ProbeObservationV1,
    ProbeProvenance,
    read_document_source_metadata,
)
from .serialization import (
    DEFAULT_LIMITS,
    ArtifactReference,
    ArtifactReferenceError,
    CanonicalJsonError,
    InlineRowsLimitError,
    SerializationLimits,
    StateSizeLimitError,
    canonical_digest,
    canonical_json_bytes,
    verify_artifact_reference,
)

if TYPE_CHECKING:
    from .data_probes import DataProbeBudgetRuntime, DataProbeRuntime
    from .models import ColumnRef, DocumentRef, QueryProbeRef, TableRef
    from .schema_probes import SchemaProbeBudgetRuntime, SchemaProbeRuntime


class ProbeResultError(ValueError):
    """Base error for a rejected probe result."""


class ProbeResultDecodeError(ProbeResultError):
    """Serialized probe result is malformed or violates its contract."""


class ProbeArtifactError(ProbeResultError):
    """External probe payload is missing, corrupt, or not the written payload."""


class ProbeStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


ProbeKind = ResearchActionKind

ArtifactWriter = Callable[[bytes], ArtifactReference]
ArtifactReader = Callable[[ArtifactReference], bytes]

_NO_PAYLOAD = object()
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_EVIDENCE_OBSERVATION_CHARS = 65_536
ProbeSummary = Annotated[str, Field(min_length=1, max_length=10_000)]


class ProbeResult(ContractModel):
    """Closed, versioned description of one observed probe invocation."""

    contract_name: Literal["probe_result"] = "probe_result"
    schema_namespace_version: Digest
    invocation_id: Id
    action_digest: Digest
    probe_kind: ResearchActionKind
    status: ProbeStatus
    target: TargetRef
    started_at: datetime
    completed_at: datetime
    summary: ProbeSummary
    cost: EvidenceCost
    row_count: NonNegativeInt
    byte_count: NonNegativeInt
    truncated: bool
    failure_code: Id | None
    payload_digest: Digest | None
    inline_payload_json: str | None
    artifact_reference: ArtifactReference | None

    @field_validator("action_digest", "payload_digest")
    @classmethod
    def validate_sha256_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> ProbeResult:
        for field_name in ("started_at", "completed_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
                raise ValueError(f"{field_name} must be a UTC timestamp")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.cost.rows != self.row_count or self.cost.bytes != self.byte_count:
            raise ValueError("cost rows/bytes must match the explicit result counts")
        if self.cost.model_calls != 0 or self.cost.model_tokens != 0:
            raise ValueError("probe costs cannot charge model resources")

        has_inline = self.inline_payload_json is not None
        has_artifact = self.artifact_reference is not None
        if self.status is not ProbeStatus.SUCCESS:
            if self.failure_code is None:
                raise ValueError(
                    "failed, timed-out, and cancelled probes require failure_code"
                )
            if has_inline or has_artifact or self.payload_digest is not None:
                raise ValueError(
                    "unsuccessful probes cannot carry an observation payload"
                )
            if self.truncated:
                raise ValueError(
                    "unsuccessful probes cannot claim a truncated observation"
                )
            return self

        if self.failure_code is not None:
            raise ValueError("successful probes cannot carry failure_code")
        if has_inline == has_artifact:
            raise ValueError("successful probes require exactly one payload location")
        if self.payload_digest is None:
            raise ValueError("successful probes require payload_digest")

        if has_inline:
            payload = _canonical_inline_payload(self.inline_payload_json)
            if canonical_digest(payload) != self.payload_digest:
                raise ValueError("inline payload digest does not match")
            if len(self.inline_payload_json.encode("utf-8")) != self.byte_count:
                raise ValueError("inline payload byte_count does not match")
        else:
            reference = self.artifact_reference
            if reference is None:  # pragma: no cover - narrowed by has_artifact
                raise ValueError("successful probe artifact is missing")
            if reference.digest != self.payload_digest:
                raise ValueError("artifact and payload digests must match")
            if reference.byte_count != self.byte_count:
                raise ValueError("artifact and payload byte counts must match")
        return self


def build_probe_result(
    *,
    run_id: str,
    run_incarnation: str,
    revision: int,
    schema_namespace_version: str,
    invocation_id: str,
    action_digest: str,
    probe_kind: ResearchActionKind,
    status: ProbeStatus,
    target: TargetRef,
    started_at: datetime,
    completed_at: datetime,
    summary: str,
    cost: EvidenceCost,
    row_count: int,
    truncated: bool = False,
    failure_code: str | None = None,
    payload: Any = _NO_PAYLOAD,
    limits: SerializationLimits = DEFAULT_LIMITS,
    write_artifact: ArtifactWriter | None = None,
    read_artifact: ArtifactReader | None = None,
) -> ProbeResult:
    """Build a result, externalizing an oversized success payload without changing it."""

    _require_limits(limits)
    common = {
        "run_id": run_id,
        "run_incarnation": run_incarnation,
        "revision": revision,
        "schema_namespace_version": schema_namespace_version,
        "invocation_id": invocation_id,
        "action_digest": action_digest,
        "probe_kind": probe_kind,
        "status": status,
        "target": target,
        "started_at": started_at,
        "completed_at": completed_at,
        "summary": summary,
        "cost": cost,
        "row_count": row_count,
        "truncated": truncated,
        "failure_code": failure_code,
    }
    if status is not ProbeStatus.SUCCESS:
        if payload is not _NO_PAYLOAD:
            raise ProbeResultError("unsuccessful probes cannot receive payload")
        return ProbeResult(
            **common,
            byte_count=cost.bytes,
            payload_digest=None,
            inline_payload_json=None,
            artifact_reference=None,
        )
    if payload is _NO_PAYLOAD:
        raise ProbeResultError("successful probes require payload")

    try:
        payload_bytes = canonical_json_bytes(payload)
        normalized_payload = _decode_json_value(payload_bytes)
    except (CanonicalJsonError, ProbeResultDecodeError) as exc:
        raise ProbeResultError("probe payload must be finite canonical JSON") from exc
    payload_text = payload_bytes.decode("utf-8")
    payload_digest = canonical_digest(normalized_payload)
    inline = ProbeResult(
        **common,
        byte_count=len(payload_bytes),
        payload_digest=payload_digest,
        inline_payload_json=payload_text,
        artifact_reference=None,
    )
    try:
        canonical_json_bytes(normalized_payload, limits=limits)
        canonical_json_bytes(inline, limits=limits)
        if len(probe_observation_json(inline)) <= _MAX_EVIDENCE_OBSERVATION_CHARS:
            return inline
    except (InlineRowsLimitError, StateSizeLimitError):
        pass

    if write_artifact is None or read_artifact is None:
        raise ProbeArtifactError(
            "oversized probe payload requires artifact writer and reader"
        )
    reference = _write_and_verify_artifact(payload_bytes, write_artifact, read_artifact)
    external = ProbeResult(
        **common,
        byte_count=len(payload_bytes),
        payload_digest=payload_digest,
        inline_payload_json=None,
        artifact_reference=reference,
    )
    canonical_json_bytes(external, limits=limits)
    return external


def serialize_probe_result(
    result: ProbeResult,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
    read_artifact: ArtifactReader | None = None,
) -> bytes:
    """Serialize a revalidated result to byte-stable canonical JSON."""

    _require_limits(limits)
    checked = _revalidate_result(result)
    read_probe_payload(checked, read_artifact=read_artifact)
    return canonical_json_bytes(checked, limits=limits)


def deserialize_probe_result(
    payload: bytes | str,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
    read_artifact: ArtifactReader | None = None,
) -> ProbeResult:
    """Decode and fully verify one closed ProbeResult contract."""

    _require_limits(limits)
    raw = _payload_to_bytes(payload)
    if len(raw) > limits.max_state_bytes:
        raise ProbeResultDecodeError("probe result exceeds max_state_bytes")
    mapping = _decode_json_mapping(raw)
    for field_name in ("started_at", "completed_at"):
        timestamp = mapping.get(field_name)
        if (
            not isinstance(timestamp, str)
            or not timestamp.endswith("Z")
            or "T" not in timestamp
        ):
            raise ProbeResultDecodeError(f"{field_name} must use UTC Z representation")
    try:
        canonical = canonical_json_bytes(mapping, limits=limits)
        result = ProbeResult.model_validate_json(canonical)
        result = _revalidate_result(result)
    except (
        CanonicalJsonError,
        InlineRowsLimitError,
        StateSizeLimitError,
        ValidationError,
    ):
        raise ProbeResultDecodeError("payload does not satisfy ProbeResult") from None
    read_probe_payload(result, read_artifact=read_artifact)
    return result


def read_probe_payload(
    result: ProbeResult,
    *,
    read_artifact: ArtifactReader | None = None,
) -> Any | None:
    """Return a verified JSON observation, or None for an unsuccessful result."""

    checked = _revalidate_result(result)
    if checked.status is not ProbeStatus.SUCCESS:
        return None
    if checked.inline_payload_json is not None:
        return _canonical_inline_payload(checked.inline_payload_json)
    if read_artifact is None:
        raise ProbeArtifactError(
            "artifact-backed probe result requires artifact reader"
        )
    reference = checked.artifact_reference
    if reference is None:  # pragma: no cover - guaranteed by ProbeResult validation
        raise ProbeArtifactError("artifact reference is missing")
    try:
        content = verify_artifact_reference(reference, read_artifact)
    except (ArtifactReferenceError, TypeError) as exc:
        raise ProbeArtifactError("probe artifact verification failed") from exc
    try:
        decoded = _decode_json_value(content)
        if canonical_json_bytes(decoded) != content:
            raise ProbeResultDecodeError("artifact payload is not canonical JSON")
    except (CanonicalJsonError, ProbeResultDecodeError) as exc:
        raise ProbeArtifactError("probe artifact is not canonical JSON") from exc
    if canonical_digest(decoded) != checked.payload_digest:
        raise ProbeArtifactError("probe artifact payload digest does not match result")
    return decoded


def probe_observation_json(
    result: ProbeResult,
    *,
    read_artifact: ArtifactReader | None = None,
) -> str:
    """Return the canonical observation stored in EvidenceRecord."""

    checked = _revalidate_result(result)
    if checked.status is not ProbeStatus.SUCCESS:
        raise ProbeResultError("only successful probes have an evidence observation")
    payload = read_probe_payload(checked, read_artifact=read_artifact)
    provenance = _probe_provenance(checked, payload)
    try:
        observation = ProbeObservationV1(
            artifact_reference=checked.artifact_reference,
            byte_count=checked.byte_count,
            invocation_id=checked.invocation_id,
            observation_version=1,
            payload=payload if checked.inline_payload_json is not None else None,
            payload_digest=checked.payload_digest,
            probe_kind=checked.probe_kind,
            provenance=provenance,
            row_count=checked.row_count,
            storage=(
                "artifact" if checked.artifact_reference is not None else "inline"
            ),
            summary=checked.summary,
            truncated=checked.truncated,
        )
    except ValidationError:
        raise ProbeResultError(
            "probe result cannot form a valid evidence observation"
        ) from None
    return canonical_json_bytes(observation).decode("utf-8")


def _probe_provenance(result: ProbeResult, payload: Any) -> ProbeProvenance:
    source_version = None
    valid_until = None
    if result.probe_kind is ResearchActionKind.READ_DOCUMENT:
        try:
            metadata = read_document_source_metadata(payload)
        except ValueError:
            raise ProbeResultError(
                "document probe payload has invalid source metadata"
            ) from None
        source_version = metadata.source_version
        valid_until = metadata.valid_until
    try:
        return ProbeProvenance(
            provenance_version=1,
            run_id=result.run_id,
            run_incarnation=result.run_incarnation,
            invocation_id=result.invocation_id,
            action_digest=result.action_digest,
            probe_kind=result.probe_kind,
            target=result.target,
            schema_namespace_version=result.schema_namespace_version,
            payload_digest=result.payload_digest,
            started_at=result.started_at,
            completed_at=result.completed_at,
            source_version=source_version,
            valid_until=valid_until,
        )
    except ValidationError:
        raise ProbeResultError("probe result cannot form valid provenance") from None


def _write_and_verify_artifact(
    payload: bytes,
    write_artifact: ArtifactWriter,
    read_artifact: ArtifactReader,
) -> ArtifactReference:
    if not callable(write_artifact) or not callable(read_artifact):
        raise TypeError("artifact writer and reader must be callable")
    try:
        reference = write_artifact(payload)
    except Exception:
        raise ProbeArtifactError("artifact writer failed") from None
    if not isinstance(reference, ArtifactReference):
        raise ProbeArtifactError("artifact writer must return ArtifactReference")
    try:
        checked = ArtifactReference.model_validate(
            reference.model_dump(mode="python", round_trip=True)
        )
        stored = verify_artifact_reference(checked, read_artifact)
    except (ArtifactReferenceError, ValidationError, ValueError, TypeError) as exc:
        raise ProbeArtifactError("written artifact failed verification") from exc
    if stored != payload:
        raise ProbeArtifactError("written artifact differs from probe payload")
    return checked


def _canonical_inline_payload(payload_json: str | None) -> Any:
    if payload_json is None:
        raise ValueError("inline payload is missing")
    try:
        raw = payload_json.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("inline payload is not UTF-8 encodable") from None
    decoded = _decode_json_value(raw)
    if canonical_json_bytes(decoded) != raw:
        raise ValueError("inline payload must be canonical JSON")
    return decoded


def _decode_json_mapping(payload: bytes) -> dict[str, Any]:
    decoded = _decode_json_value(payload)
    if not isinstance(decoded, dict):
        raise ProbeResultDecodeError("ProbeResult root must be a JSON object")
    return decoded


def _decode_json_value(payload: bytes) -> Any:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProbeResultDecodeError("payload is not strict JSON") from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _revalidate_result(result: ProbeResult) -> ProbeResult:
    if not isinstance(result, ProbeResult):
        raise TypeError("result must be ProbeResult")
    try:
        return ProbeResult.model_validate(
            result.model_dump(mode="python", round_trip=True)
        )
    except ValidationError:
        raise ProbeResultError("ProbeResult violates its strict contract") from None


def _payload_to_bytes(payload: bytes | str) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        try:
            return payload.encode("utf-8")
        except UnicodeEncodeError:
            raise ProbeResultDecodeError("payload is not UTF-8 encodable") from None
    raise TypeError("payload must be bytes or str")


def _require_limits(limits: SerializationLimits) -> None:
    if not isinstance(limits, SerializationLimits):
        raise TypeError("limits must be SerializationLimits")


def search_schema_catalog(
    target: TableRef,
    top_k: int,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    from .schema_probes import search_schema_catalog as execute

    return execute(target, top_k, runtime=runtime, budget=budget)


def inspect_table(
    target: TableRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    from .schema_probes import inspect_table as execute

    return execute(target, runtime=runtime, budget=budget)


def inspect_column(
    target: ColumnRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    from .schema_probes import inspect_column as execute

    return execute(target, runtime=runtime, budget=budget)


def inspect_relationships(
    target: TableRef,
    top_k: int,
    depth: int,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    from .schema_probes import inspect_relationships as execute

    return execute(target, top_k, depth, runtime=runtime, budget=budget)


def read_schema_evidence(
    target: DocumentRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    from .schema_probes import read_schema_evidence as execute

    return execute(target, runtime=runtime, budget=budget)


def profile_column(
    target: ColumnRef,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    from .data_probes import profile_column as execute

    return execute(target, runtime=runtime, budget=budget)


def sample_rows(
    target: TableRef,
    columns: tuple[str, ...],
    limit: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    from .data_probes import sample_rows as execute

    return execute(target, columns, limit, runtime=runtime, budget=budget)


def search_value(
    target: ColumnRef,
    value: str | int | float | bool | None,
    top_k: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    from .data_probes import search_value as execute

    return execute(target, value, top_k, runtime=runtime, budget=budget)


def get_distinct_values_probe(
    target: ColumnRef,
    top_k: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    from .data_probes import get_distinct_values_probe as execute

    return execute(target, top_k, runtime=runtime, budget=budget)


def execute_research_probe(
    target: QueryProbeRef,
    parameters: tuple[str | int | float | bool | None, ...],
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    from .data_probes import execute_research_probe as execute

    return execute(target, parameters, runtime=runtime, budget=budget)
