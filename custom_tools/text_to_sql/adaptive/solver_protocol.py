"""Strict transient proposals produced by the one-turn SQL solver."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from .models import EvidenceSourceKind, Id, NonEmptyText, PredicateRef, StrictModel
from .serialization import _register_internal_decode_models


SOLVER_PROPOSAL_VERSION = 1
MAX_SOLVER_PROPOSAL_BYTES = 256 * 1024


class SqlCandidateProposal(StrictModel):
    """Untrusted SQL candidate; validation and execution happen later."""

    proposal_kind: Literal["sql_candidate"]
    sql: NonEmptyText


class MissingEvidenceProposal(StrictModel):
    """One precise request for evidence not available to the solver."""

    proposal_kind: Literal["missing_evidence"]
    source_id: Id
    question: NonEmptyText
    required_evidence_kind: EvidenceSourceKind
    reason: NonEmptyText
    repair_kind: Literal["semantic_binding_mismatch"] | None = None
    repair_binding_id: Id | None = None
    predicate_authority: PredicateRef | None = None

    @model_validator(mode="after")
    def validate_repair_target(self) -> "MissingEvidenceProposal":
        if (self.repair_kind is None) != (self.repair_binding_id is None):
            raise ValueError("semantic repair kind and binding ID must be provided together")
        if self.predicate_authority is not None and self.repair_kind is not None:
            raise ValueError("predicate authority cannot be combined with semantic repair")
        return self


SolverProposal: TypeAlias = Annotated[
    SqlCandidateProposal | MissingEvidenceProposal,
    Field(discriminator="proposal_kind"),
]


class SolverProposalV1(StrictModel):
    """Closed versioned envelope for one non-persistent solver proposal."""

    proposal_version: Literal[SOLVER_PROPOSAL_VERSION]
    proposal: SolverProposal


_register_internal_decode_models(SolverProposalV1)


def parse_solver_proposal(payload: bytes | str) -> SolverProposalV1:
    """Parse a bounded canonical JSON solver proposal."""

    from .serialization import (
        ContractDecodeError,
        SerializationLimits,
        deserialize_as,
    )

    if (
        isinstance(payload, bytes)
        and payload.startswith(b"\xef\xbb\xbf")
        or isinstance(payload, str)
        and payload.startswith("\ufeff")
    ):
        raise ContractDecodeError("solver proposal must not start with a UTF-8 BOM")

    return deserialize_as(
        payload,
        SolverProposalV1,
        limits=SerializationLimits(
            max_state_bytes=MAX_SOLVER_PROPOSAL_BYTES,
            max_inline_rows=0,
        ),
    )
