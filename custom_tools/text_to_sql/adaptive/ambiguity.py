"""Strict structured report for an unresolved research ambiguity."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from ._model_primitives import Id, NonEmptyText, StrictModel, require_canonical_ids


class AmbiguityReport(StrictModel):
    """Evidence-backed alternatives that the pipeline must not guess between."""

    interpretations: Annotated[tuple[NonEmptyText, ...], Field(min_length=2)]
    citation_evidence_ids: Annotated[tuple[Id, ...], Field(min_length=1)] | None = None
    citation_evidence_handles: Annotated[
        tuple[Annotated[str, Field(pattern=r"^e[1-9][0-9]*$")], ...],
        Field(min_length=1),
    ] | None = Field(default=None, exclude=True)
    missing_distinguishing_fact: NonEmptyText

    @field_validator(
        "interpretations",
        "citation_evidence_ids",
        "citation_evidence_handles",
        mode="before",
    )
    @classmethod
    def require_tuple_input(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_canonical_content(self) -> "AmbiguityReport":
        if any(item != item.strip() for item in self.interpretations):
            raise ValueError("ambiguity interpretations must be canonical text")
        if len(self.interpretations) != len(set(self.interpretations)):
            raise ValueError("ambiguity interpretations must be distinct")
        if (self.citation_evidence_ids is None) == (
            self.citation_evidence_handles is None
        ):
            raise ValueError(
                "ambiguity citation evidence requires exactly one raw-ID or handle form"
            )
        if self.citation_evidence_ids is not None:
            require_canonical_ids(
                self.citation_evidence_ids,
                "ambiguity citation_evidence_ids",
            )
        if self.citation_evidence_handles is not None:
            require_canonical_ids(
                self.citation_evidence_handles,
                "ambiguity citation_evidence_handles",
            )
        if self.missing_distinguishing_fact != self.missing_distinguishing_fact.strip():
            raise ValueError("missing_distinguishing_fact must be canonical text")
        return self
