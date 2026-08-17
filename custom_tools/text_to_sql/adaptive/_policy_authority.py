"""Pure W4-01 generation authority derived from canonical coverage readiness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from ._semantic_coverage_footprint import model_payload
from .freshness import FreshnessContext
from .models import ResearchState
from .semantic_coverage import (
    CoverageInputError,
    CoverageInputErrorCode,
    CoverageRequirements,
    validate_coverage_inputs,
)


class ResearchGenerationAuthorityStatus(StrEnum):
    """Closed outcome of deciding whether research may hand off to generation."""

    ALLOWED = "ALLOWED"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True, slots=True)
class ResearchGenerationAuthority:
    """Typed, side-effect-free authority result for one exact research state."""

    allowed: bool
    status: ResearchGenerationAuthorityStatus
    reason: CoverageInputErrorCode | None
    affected_source_ids: tuple[str, ...]
    requirements: CoverageRequirements | None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise TypeError("authority allowed flag must be bool")
        if type(self.status) is not ResearchGenerationAuthorityStatus:
            raise TypeError(
                "authority status must be ResearchGenerationAuthorityStatus"
            )
        if self.reason is not None and type(self.reason) is not CoverageInputErrorCode:
            raise TypeError("authority reason must be CoverageInputErrorCode or None")
        if type(self.affected_source_ids) is not tuple or any(
            type(item) is not str for item in self.affected_source_ids
        ):
            raise TypeError("authority affected source IDs must be a tuple of strings")
        if (
            self.requirements is not None
            and type(self.requirements) is not CoverageRequirements
        ):
            raise TypeError(
                "authority requirements must be CoverageRequirements or None"
            )
        if self.requirements is not None:
            try:
                validated_requirements = CoverageRequirements.model_validate(
                    model_payload(self.requirements)
                )
            except (AttributeError, TypeError, ValidationError, ValueError) as error:
                raise ValueError(
                    "authority requirements violate their contract"
                ) from error
            if validated_requirements != self.requirements:
                raise ValueError("authority requirements must be canonical")
        if self.allowed != (self.status is ResearchGenerationAuthorityStatus.ALLOWED):
            raise ValueError("authority allowed flag must match status")
        if self.allowed != (self.reason is None):
            raise ValueError("authority reason must be absent exactly when allowed")
        if self.allowed != (self.requirements is not None):
            raise ValueError("authority requirements must exist exactly when allowed")
        if self.allowed and self.affected_source_ids:
            raise ValueError("allowed authority cannot affect source IDs")
        if self.affected_source_ids != tuple(sorted(set(self.affected_source_ids))):
            raise ValueError("authority affected source IDs must be sorted and unique")

    def is_canonical(self) -> bool:
        """Return whether a possibly forged instance still satisfies this contract."""

        try:
            self.__post_init__()
        except (AttributeError, TypeError, ValueError):
            return False
        return True


def evaluate_research_generation_authority(
    state: ResearchState,
    freshness_context: FreshnessContext,
    run_id: str,
    run_incarnation: str,
) -> ResearchGenerationAuthority:
    """Allow generation only when canonical coverage inputs are fully authorized."""

    try:
        requirements = validate_coverage_inputs(
            state,
            freshness_context,
            run_id,
            run_incarnation,
        )
    except CoverageInputError as error:
        return ResearchGenerationAuthority(
            allowed=False,
            status=ResearchGenerationAuthorityStatus.DEFERRED,
            reason=error.code,
            affected_source_ids=error.affected_source_ids,
            requirements=None,
        )
    except Exception:
        return ResearchGenerationAuthority(
            allowed=False,
            status=ResearchGenerationAuthorityStatus.DEFERRED,
            reason=CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
            affected_source_ids=(),
            requirements=None,
        )
    return ResearchGenerationAuthority(
        allowed=True,
        status=ResearchGenerationAuthorityStatus.ALLOWED,
        reason=None,
        affected_source_ids=(),
        requirements=requirements,
    )
