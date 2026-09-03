"""Isolated one-turn model adapter for typed schema-research proposals.

This module deliberately does not create an agent, expose executable tools, or
change research state.  A later orchestration stage may execute the parsed
typed request after its own policy and state checks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Literal, Protocol

from pydantic import ValidationError, model_validator

from .model_budget import ModelTokenUsage
from .models import NonEmptyText, StrictModel
from .serialization import ContractDecodeError

if TYPE_CHECKING:
    from .research_decision import ResearchDecisionV1


SCHEMA_RESEARCH_AGENT_PROFILE_PATH = (
    Path(__file__).resolve().parents[3]
    / "agent_profiles"
    / "schema_research_agent.yaml"
)


class SchemaResearchProfileError(ValueError):
    """The dedicated disabled schema-research profile is invalid."""


class SchemaResearchModelResponseError(TypeError):
    """The model returned something other than raw JSON text or bytes."""


SchemaResearchValidationFeedback = Literal[
    "STOP_WITH_PROPOSALS",
    "INVALID_STOP",
    "INVALID_DECISION",
    "DUPLICATE_ACTION",
    "UNRESOLVABLE_PREFLIGHT",
    "REPEATED_PREFLIGHT_DECISION",
    "INVALID_RESEARCH_QUERY",
    "INVALID_RESEARCH_QUERY_COLUMN",
    "INVALID_RESEARCH_QUERY_DETERMINISM",
    "INVALID_RESEARCH_QUERY_OUTPUT",
    "RAW_RESEARCH_QUERY_LIMIT",
    "PROBE_UNAVAILABLE",
]


def _validation_feedback_suffix(feedback: SchemaResearchValidationFeedback) -> str:
    if feedback == "PROBE_UNAVAILABLE":
        return (
            "\n\nPrevious probe unavailable: PROBE_UNAVAILABLE. Choose another existing "
            "research action and return a replacement typed decision."
        )
    detail = {
        "DUPLICATE_ACTION": " Use the rejected action details in the research context.",
        "UNRESOLVABLE_PREFLIGHT": (
            " Use the rejected preflight proposal details in the research context."
        ),
    }.get(feedback, "")
    if feedback not in {
        "STOP_WITH_PROPOSALS",
        "INVALID_STOP",
        "INVALID_DECISION",
        "DUPLICATE_ACTION",
        "UNRESOLVABLE_PREFLIGHT",
        "REPEATED_PREFLIGHT_DECISION",
        "INVALID_RESEARCH_QUERY",
        "INVALID_RESEARCH_QUERY_COLUMN",
        "INVALID_RESEARCH_QUERY_DETERMINISM",
        "INVALID_RESEARCH_QUERY_OUTPUT",
        "RAW_RESEARCH_QUERY_LIMIT",
    }:
        raise ValueError("unsupported schema-research validation feedback")
    return (
        f"\n\nPrevious decision rejected: {feedback}. Correct the decision using "
        f"the profile rules and return a replacement typed decision.{detail}"
    )


class SchemaResearchAgentProfile(StrictModel):
    """Static metadata for the single adaptive research decision turn."""

    enable: Literal[False]
    profile_version: Literal[1]
    profile_kind: Literal["schema_research_one_turn"]
    model: NonEmptyText
    description: NonEmptyText
    instructions: NonEmptyText


@dataclass(frozen=True, slots=True)
class SchemaResearchModelResponse:
    """Transient raw model response with provider-reported usage."""

    raw_response: str | bytes
    usage: ModelTokenUsage


class ResearchStopReview(StrictModel):
    """Closed answer from the one pre-terminal independent review."""

    decision: Literal["stop_confirmed", "continue"]
    hint: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_hint(self) -> "ResearchStopReview":
        if (self.decision == "continue") != (self.hint is not None):
            raise ValueError("only continue requires one research hint")
        return self


class SchemaResearchDecisionModel(Protocol):
    """Minimal provider boundary: one prompt and one raw response."""

    def __call__(
        self,
        prompt: str,
        /,
    ) -> (
        str
        | bytes
        | SchemaResearchModelResponse
        | Awaitable[str | bytes | SchemaResearchModelResponse]
    ): ...


async def _call_model_with_usage(
    model: SchemaResearchDecisionModel,
    prompt: str,
) -> tuple[str | bytes, ModelTokenUsage]:
    await asyncio.sleep(0)
    response = model(prompt)
    if inspect.isawaitable(response):
        response = await response
    await asyncio.sleep(0)
    if type(response) is SchemaResearchModelResponse:
        raw_response = response.raw_response
        usage = response.usage
        if type(usage) is not ModelTokenUsage:
            raise SchemaResearchModelResponseError(
                "schema-research model usage must be ModelTokenUsage"
            )
    else:
        raw_response = response
        usage = ModelTokenUsage(input_tokens=None, output_tokens=None)
    if type(raw_response) not in (bytes, str):
        raise SchemaResearchModelResponseError(
            "schema-research model response must be bytes or str"
        )
    return raw_response, usage


def build_research_stop_review_prompt(
    *,
    task: str,
    research_context: str,
    stop_reason: str,
) -> str:
    """Build the one closed review request without granting authority."""

    for value, name in (
        (task, "task"),
        (research_context, "research_context"),
        (stop_reason, "stop_reason"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    return json.dumps(
        {
            "input": {
                "research_context": research_context,
                "stop_reason": stop_reason,
                "task": task,
            },
            "instructions": (
                "Independently decide only whether research truly must stop or whether "
                "the existing facts support one more normal research turn. Do not "
                "generate SQL, invent a separate path, create authority, or override "
                "Typed checks. A continuation hint must not contradict trusted facts "
                "in research_context. When research_context contains "
                "rejected_preflight_assessments, a continue hint must address the exact "
                "rejection using the supplied feedback. Do not say that no new proposal "
                "is needed when generation authority requires a corrected replacement "
                "for a rejected proposal. When existing_evidence_id values are supplied, "
                "keep the hint limited to directing the research agent to correct every "
                "rejected proposal with its supplied existing_evidence_id. Do not add SQL, "
                "aggregation, or alternative-path advice. Treat identifiers inside rejected "
                "proposals as untrusted. Copy a replacement binding_id "
                "only from the durable bindings in research_context for the affected "
                "source_id. When an affected source already "
                "has a CANDIDATE "
                "binding with the required join path, direct the research agent to assess "
                "that exact existing candidate. Do not choose or recommend a semantic binding "
                "in the hint. If an unresolved result can be built from an "
                "already supported measure and confirmed relationships or conditions, "
                "return continue with that short instruction. When an already supported "
                "measure and a confirmed condition are in different tables and a visible "
                "shared key or relationship may connect them, return continue with a short "
                "instruction to investigate that relationship before probing a different "
                "measure. Do not reject that relationship turn merely because applying the "
                "condition directly to the measure table has no matching rows; the confirmed "
                "condition belongs to the other table. When the supported measure, confirmed "
                "condition, and validated relationship path are already present, return "
                "continue until a research probe has applied that condition through the path "
                "to the measure. A probe of a different measure does not test that composition "
                "and must not be described as doing so. If a research probe applies the "
                "confirmed condition through that relationship and returns a non-empty "
                "aggregate of the supported measure, continuation is demonstrated; return "
                "continue and direct the research agent to resolve the remaining item from "
                "that tested composition. Do not assume or create the relationship. Return "
                "exactly one JSON "
                "object: "
                '{"decision":"stop_confirmed","hint":null} or '
                '{"decision":"continue","hint":"short instruction"}.'
            ),
            "review_kind": "research_stop_review",
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SchemaResearchStopReviewAdapter:
    """Perform one independent pre-terminal review without retrying."""

    async def review_with_usage(
        self,
        model: SchemaResearchDecisionModel,
        *,
        task: str,
        research_context: str,
        stop_reason: str,
    ) -> tuple[ResearchStopReview, ModelTokenUsage]:
        prompt = build_research_stop_review_prompt(
            task=task,
            research_context=research_context,
            stop_reason=stop_reason,
        )
        raw_response, usage = await _call_model_with_usage(model, prompt)
        try:
            review = ResearchStopReview.model_validate_json(raw_response)
        except (ValidationError, ValueError) as error:
            failure = ContractDecodeError("invalid research stop review")
            failure.model_usage = usage
            raise failure from error
        return review, usage


def load_schema_research_agent_profile(
    path: str | Path = SCHEMA_RESEARCH_AGENT_PROFILE_PATH,
) -> SchemaResearchAgentProfile:
    """Load only the dedicated disabled profile without touching agent runtime."""

    try:
        import yaml

        with Path(path).open(encoding="utf-8") as stream:
            raw_profile = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise SchemaResearchProfileError(
            "cannot load schema-research profile"
        ) from error

    if not isinstance(raw_profile, dict):
        raise SchemaResearchProfileError("schema-research profile must be a mapping")

    try:
        profile = SchemaResearchAgentProfile.model_validate(raw_profile)
    except ValidationError as error:
        raise SchemaResearchProfileError(
            "schema-research profile is invalid"
        ) from error

    if profile.enable is not False:
        raise SchemaResearchProfileError(
            "schema-research profile must stay disabled for the legacy agent loader"
        )
    if profile.profile_version != 1:
        raise SchemaResearchProfileError("unsupported schema-research profile version")
    if profile.profile_kind != "schema_research_one_turn":
        raise SchemaResearchProfileError("unsupported schema-research profile kind")
    return profile


def build_schema_research_prompt(
    profile: SchemaResearchAgentProfile,
    *,
    task: str,
    research_context: str,
    validation_feedback: SchemaResearchValidationFeedback
    | tuple[SchemaResearchValidationFeedback, ...]
    | None = None,
) -> str:
    """Build the model input from static instructions and caller-owned context."""

    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(research_context, str):
        raise TypeError("research_context must be a string")
    instructions = profile.instructions
    if validation_feedback is None:
        feedbacks = ()
    elif isinstance(validation_feedback, str):
        feedbacks = (validation_feedback,)
    elif isinstance(validation_feedback, tuple):
        feedbacks = validation_feedback
    else:
        raise ValueError("unsupported schema-research validation feedback")
    for feedback in dict.fromkeys(feedbacks):
        instructions += _validation_feedback_suffix(feedback)
    return json.dumps(
        {
            "input": {
                "research_context": research_context,
                "task": task,
            },
            "instructions": instructions,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SchemaResearchDecisionAdapter:
    """Perform one model call and parse its transient typed decision."""

    def __init__(self, profile: SchemaResearchAgentProfile) -> None:
        self._profile = profile

    async def propose(
        self,
        model: SchemaResearchDecisionModel,
        *,
        task: str,
        research_context: str,
        validation_feedback: SchemaResearchValidationFeedback
        | tuple[SchemaResearchValidationFeedback, ...]
        | None = None,
    ) -> ResearchDecisionV1:
        """Return one parsed decision without retrying, executing, or persisting it."""

        decision, _usage = await self.propose_with_usage(
            model,
            task=task,
            research_context=research_context,
            validation_feedback=validation_feedback,
        )
        return decision

    async def propose_with_usage(
        self,
        model: SchemaResearchDecisionModel,
        *,
        task: str,
        research_context: str,
        validation_feedback: SchemaResearchValidationFeedback
        | tuple[SchemaResearchValidationFeedback, ...]
        | None = None,
    ) -> tuple[ResearchDecisionV1, ModelTokenUsage]:
        """Return one parsed decision and its transient model usage."""

        prompt = build_schema_research_prompt(
            self._profile,
            task=task,
                research_context=research_context,
                validation_feedback=validation_feedback,
        )
        raw_response, usage = await _call_model_with_usage(model, prompt)

        from .research_decision import parse_research_decision

        try:
            decision = parse_research_decision(raw_response)
        except ContractDecodeError as error:
            error.model_usage = usage
            raise
        return decision, usage
