"""Closed durable inputs for exact adaptive reducer replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, ValidationError, model_validator

from . import _sql_ast_models
from ._sql_ast_models import ParsedSqlCandidate
from .freshness import FreshnessContext
from .models import BudgetState, Digest, Id, NonNegativeInt, StrictModel
from .probes import ProbeResult
from .research_decision import ResearchDecisionV1, SemanticCommitRequest
from .semantic_coverage import CoverageRequirements
from .semantic_reducer import TrustedSemanticBatch, TrustedToolClaim
from .serialization import DEFAULT_LIMITS, SerializationLimits, canonical_json_bytes
from .solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
)


class ReplayInputError(ValueError):
    """A durable reducer input is malformed or unsupported."""


_WireJson = Annotated[str, Field(min_length=1, max_length=8 * 1024 * 1024)]
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ParsedSqlCandidateReplayValue(StrictModel):
    """Canonical verified wire form of one already parsed SQL candidate."""

    wire_version: Literal[1] = 1
    wire_json: _WireJson
    wire_digest: Digest

    @classmethod
    def from_candidate(
        cls,
        candidate: ParsedSqlCandidate,
    ) -> ParsedSqlCandidateReplayValue:
        from ._sql_ast_process import _encode_envelope

        if type(candidate) is not ParsedSqlCandidate:
            raise TypeError("candidate must be ParsedSqlCandidate")
        raw = _encode_envelope(candidate)
        value = cls(
            wire_json=raw.decode("utf-8"),
            wire_digest=_digest(raw),
        )
        if value.to_candidate() != candidate:
            raise ReplayInputError("parsed candidate wire value changed identity")
        return value

    @model_validator(mode="after")
    def validate_wire(self) -> ParsedSqlCandidateReplayValue:
        self.to_candidate()
        return self

    def to_candidate(self) -> ParsedSqlCandidate:
        from ._sql_ast_process import _decode_envelope, _encode_envelope

        try:
            raw = self.wire_json.encode("utf-8")
        except UnicodeEncodeError:
            raise ReplayInputError("parsed candidate wire value is not UTF-8") from None
        if not _SHA256_RE.fullmatch(self.wire_digest) or not hmac.compare_digest(
            self.wire_digest,
            _digest(raw),
        ):
            raise ReplayInputError("parsed candidate wire_digest does not match")
        try:
            candidate = _decode_envelope(
                raw,
                limits=_sql_ast_models.AstLimits(
                    max_nodes=_sql_ast_models.MAX_AST_NODES,
                    max_depth=_sql_ast_models.MAX_AST_DEPTH,
                ),
            )
        except Exception:
            raise ReplayInputError("parsed candidate wire value is invalid") from None
        if _encode_envelope(candidate) != raw:
            raise ReplayInputError("parsed candidate wire value is not canonical")
        return candidate


class _ReplayInputBase(StrictModel):
    input_version: Literal[1] = 1


class ResearchSemanticReplayInput(_ReplayInputBase):
    input_kind: Literal["research_semantic"] = "research_semantic"
    decision: ResearchDecisionV1
    semantic_batch: TrustedSemanticBatch
    freshness_context: FreshnessContext
    tool_claim: TrustedToolClaim | None
    budget_state: BudgetState
    planned_action_digest: Digest
    observed_action_digest: Digest
    probe_result: ProbeResult | None

    @model_validator(mode="after")
    def validate_digests(self) -> ResearchSemanticReplayInput:
        _require_sha256(self.planned_action_digest, "planned_action_digest")
        _require_sha256(self.observed_action_digest, "observed_action_digest")
        semantic_only = isinstance(self.decision.next, SemanticCommitRequest)
        if semantic_only != (
            self.tool_claim is None and self.probe_result is None
        ):
            raise ValueError(
                "semantic replay requires null claim and probe only for semantic_commit"
            )
        return self


class ResearchTerminalReplayInput(_ReplayInputBase):
    input_kind: Literal["research_terminal"] = "research_terminal"
    freshness_context: FreshnessContext


class SolverSqlProposalReplayInput(_ReplayInputBase):
    input_kind: Literal["solver_sql_proposal"] = "solver_sql_proposal"
    proposal: SolverProposalV1
    parsed_candidate: ParsedSqlCandidateReplayValue
    requirements: CoverageRequirements
    generated_ids: tuple[Id, Id]

    @model_validator(mode="after")
    def validate_proposal(self) -> SolverSqlProposalReplayInput:
        if type(self.proposal.proposal) is not SqlCandidateProposal:
            raise ValueError("solver_sql_proposal requires SqlCandidateProposal")
        return self


class SolverMissingEvidenceReplayInput(_ReplayInputBase):
    input_kind: Literal["solver_missing_evidence"] = "solver_missing_evidence"
    proposal: SolverProposalV1
    requirements: CoverageRequirements
    generated_ids: tuple[Id, Id]

    @model_validator(mode="after")
    def validate_proposal(self) -> SolverMissingEvidenceReplayInput:
        if type(self.proposal.proposal) is not MissingEvidenceProposal:
            raise ValueError("solver_missing_evidence requires MissingEvidenceProposal")
        return self


class SolverReentryAdmissionReplayInput(_ReplayInputBase):
    input_kind: Literal["solver_reentry_admission"] = "solver_reentry_admission"
    research_state_revision: NonNegativeInt
    research_state_digest: Digest
    missing_evidence_request_id: Id
    generated_reentry_id: Id

    @model_validator(mode="after")
    def validate_digest(self) -> SolverReentryAdmissionReplayInput:
        _require_sha256(self.research_state_digest, "research_state_digest")
        return self


class SolverReentryCompletedReplayInput(_ReplayInputBase):
    input_kind: Literal["solver_reentry_completed"] = "solver_reentry_completed"
    research_reentry_id: Id
    research_state_revision: NonNegativeInt
    research_state_digest: Digest
    freshness_context: FreshnessContext
    requirements: CoverageRequirements

    @model_validator(mode="after")
    def validate_digest(self) -> SolverReentryCompletedReplayInput:
        _require_sha256(self.research_state_digest, "research_state_digest")
        return self


ReplayReducerInput: TypeAlias = (
    ResearchSemanticReplayInput
    | ResearchTerminalReplayInput
    | SolverSqlProposalReplayInput
    | SolverMissingEvidenceReplayInput
    | SolverReentryAdmissionReplayInput
    | SolverReentryCompletedReplayInput
)

_INPUT_TYPES = {
    model.model_fields["input_kind"].default: model
    for model in (
        ResearchSemanticReplayInput,
        ResearchTerminalReplayInput,
        SolverSqlProposalReplayInput,
        SolverMissingEvidenceReplayInput,
        SolverReentryAdmissionReplayInput,
        SolverReentryCompletedReplayInput,
    )
}


def serialize_replay_input(
    value: ReplayReducerInput,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> bytes:
    if type(value) not in _INPUT_TYPES.values():
        raise TypeError("value must be an exact ReplayReducerInput")
    checked = type(value).model_validate(
        value.model_dump(
            mode="python",
            round_trip=True,
            warnings="error",
        )
    )
    if checked != value:
        raise ReplayInputError("replay input changed during exact revalidation")
    return canonical_json_bytes(checked, limits=limits)


def deserialize_replay_input(
    raw: bytes | str,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> ReplayReducerInput:
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(raw_bytes) > limits.max_state_bytes:
        raise ReplayInputError("replay input exceeds max_state_bytes")
    try:
        mapping = json.loads(raw_bytes, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ReplayInputError("replay input is not valid JSON") from None
    if not isinstance(mapping, dict):
        raise ReplayInputError("replay input root must be an object")
    canonical = canonical_json_bytes(mapping, limits=limits)
    if canonical != raw_bytes:
        raise ReplayInputError("replay input is not canonical JSON")
    if mapping.get("input_version") != 1:
        raise ReplayInputError("replay input version is unsupported")
    model_type = _INPUT_TYPES.get(mapping.get("input_kind"))
    if model_type is None:
        raise ReplayInputError("replay input kind is unsupported")
    try:
        return model_type.model_validate_json(canonical)
    except ValidationError:
        raise ReplayInputError("replay input violates its closed contract") from None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be exact sha256 lowercase hex")


__all__ = [
    "ParsedSqlCandidateReplayValue",
    "ReplayInputError",
    "ReplayReducerInput",
    "ResearchSemanticReplayInput",
    "ResearchTerminalReplayInput",
    "SolverMissingEvidenceReplayInput",
    "SolverReentryAdmissionReplayInput",
    "SolverReentryCompletedReplayInput",
    "SolverSqlProposalReplayInput",
    "deserialize_replay_input",
    "serialize_replay_input",
]
