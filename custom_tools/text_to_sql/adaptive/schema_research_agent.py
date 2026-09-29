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
        "DUPLICATE_ACTION": (
            " Do not repeat any rejected action. Use the rejected action details "
            "in the research context: use the evidence already in the durable state "
            "to submit proposals, or choose a different useful probe."
        ),
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
                "in research_context. "
                "A continue hint may identify unresolved semantic sources or durable evidence, "
                "but it must not name, select, or reject a physical table or column candidate. "
                "This applies to negative instructions as well as positive selections; leave the "
                "candidate comparison to ordinary research under the existing profile rules. "
                "A required categorical EQ, IN, or IS NULL predicate is not exact-certified by "
                "schema inspection or a SUPPORTED binding. Without its exact-value certificate, "
                "return continue to the existing value-confirmation/recovery flow; do not direct "
                "semantic_commit, complete, or a composed application. Do not choose a physical "
                "target, literal, or SQL in the hint. This rule takes priority over composition or "
                "commit guidance. "
                "When source_text or normalized_meaning explicitly names an "
                "action or role and visible trusted schema or linked evidence describes its "
                "candidate or SUPPORTED binding as a different action or role, structural "
                "validity, a CANDIDATE, or a SUPPORTED binding does not close that source. "
                "Return continue and direct ordinary research only to compare or correct that "
                "source under the existing profile rules; do not name a physical target. "
                "When previous_stop_review_hint is present, compare it with "
                "completed_action_index and durable evidence. Return stop_confirmed only "
                "when they actually close that hint; another successful tool action does not "
                "close it. If uncertain, return continue with the same limited direction. "
                "When a previous hint requires assessment of an existing binding, a matching "
                "SUPPORTED binding with linked durable evidence closes that assessment even when "
                "completed_action_index records semantic_commit rather than binding_assessment; do not "
                "repeat it. Assess remaining unresolved items independently. "
                "When an exact binding has already passed typed checks and its required "
                "consistent assessment with durable evidence is persisted, that confirmation "
                "is closed. Do not request another probe, binding, or assessment for it, and do "
                "not report it as unresolved; continue only with other required facts. "
                "New trusted facts may contradict the prior hint. "
                "After two successful zero-row probes of the same formula hypothesis and literal, "
                "do not direct another confirmation of that formula. Return continue only to test "
                "a different plausible interpretation supported by existing schema or value evidence; "
                "do not name a physical target. This takes priority over formula-preservation guidance. "
                "Apply this rule again to every later formula hypothesis. Schema qualification, "
                "LIMIT, or projection differences do not make otherwise equivalent predicates and "
                "parameters a new hypothesis. "
                "When a different plausible interpretation then has fresh positive evidence for the "
                "same literal and confirmed conditions, do not direct research back to the zero-row "
                "formula. Continue only to assess or bind that positive interpretation under the "
                "existing profile rules, without naming a physical target. "
                "A later positive probe for the same unresolved semantic item, literal, and "
                "confirmed conditions on a different schema-supported column or expression "
                "establishes the positive "
                "alternative even when it has no special alternative label. Treat the earlier "
                "zero-row hypothesis as closed: do not inspect, search, or probe it again. "
                "Continue only with proposals, assessments, or semantic_commit for the positive "
                "interpretation and its unresolved dependent outputs. "
                "When exhaustive inspection shows that a requested entity attribute is absent but "
                "exactly one inspected attribute of that same entity remains a plausible answer proxy, "
                "do not return stop_confirmed while that output source is unresolved and has no binding. "
                "Return continue and direct ordinary research only to assess or bind the best available "
                "entity-owned proxy under the existing profile rules, without naming a physical target. "
                "When the previous hint directed the best-available entity-owned proxy assessment "
                "and that exact source now has a SUPPORTED binding after the requested exhaustive "
                "comparison, treat the hint as closed. Do not reopen or downgrade that binding solely "
                "because its schema label is not literally the absent requested attribute. "
                "An entity-owned categorical column is not a plausible competing proxy merely "
                "because it is categorical. Its schema description or observed values must support "
                "the requested semantic role; a column explicitly described as a different role "
                "does not create ambiguity or invalidate an already SUPPORTED proxy. "
                "When stop_reason includes INVALID_STOP and research_context has "
                "invalid_stop_generation_authority with exact durable binding, join, or "
                "evidence IDs for affected sources, return continue even when unresolved_items "
                "is empty or previous_stop_review_hint appears closed. The hint must direct one "
                "ordinary typed corrective decision using only those exact durable IDs; do not "
                "prescribe a new probe or SQL. "
                "For unsupported after exhaustive negative observations of a zero-row composed "
                "probe with confirmed schema columns, relationship, durable physical predicate evidence "
                "for each exact column/type/operator/literal with its linked evidence ID, and document "
                "formula, return continue. Direct only preservation of those schema-supported bindings "
                "by semantic_commit/complete; do not direct a new probe, SQL, a value-level claim, or "
                "an unbindable/unsupported conclusion. "
                "For unsupported, only when research_context has confirmed columns/relationship, "
                "exact durable physical predicate evidence for its column/type/operator/literal, a "
                "zero-row composed probe for a required predicate, and unresolved dependent items, "
                "return continue. Direct only the exact existing schema-supported bindings and "
                "semantic_commit/complete; do not direct a new probe or SQL. Otherwise return "
                "continue only to confirm the predicate; do not direct semantic_commit/complete or SQL. "
                "When research_context contains "
                "rejected_preflight_assessments, a continue hint must address the exact "
                "rejection using the supplied feedback. Do not say that no new proposal "
                "is needed when generation authority requires a corrected replacement "
                "for a rejected proposal. When required_continuation.kind is "
                "establish_required_relationship, return continue and direct the next ordinary "
                "typed step to inspect or validate the missing relationship. Do not propose SQL, "
                "terminal computation, aggregation, latest/current/time grain, measure replacement, "
                "or another terminal action. Do not reassess already SUPPORTED bindings. "
                "Schema or foreign-key evidence alone does not close this continuation: when durable "
                "state lacks a VALIDATED JoinCandidate covering the required tables and the affected "
                "binding lacks that path, return continue. Direct the ordinary agent to preserve exactly "
                "one new_join from exact durable relationship evidence and attach that path to the "
                "affected binding; return stop_confirmed only after this typed persistence. "
                "When the hint names exact durable relationship evidence already produced by a "
                "completed action, direct the next decision to submit that new_join by semantic_commit; "
                "do not request or permit another relationship inspection or probe. "
                "When research_context.exact_formula_documents identifies a required FORMULA without "
                "a matching SUPPORTED document-backed derived_expression for the same source, document, "
                "and formula, return continue. When durable document evidence exists but one or more "
                "required physical inputs under that FORMULA source are not SUPPORTED, return continue "
                "before derived-expression preservation. Direct ordinary research only to create or confirm "
                "the missing formula-source input or predicate bindings; do not reread the document or "
                "recheck supported inputs. Only when every required physical input is SUPPORTED and no "
                "required formula-source predicate or join continuation remains, hint only to preserve the "
                "derived_expression from the existing trusted document and confirmed inputs; "
                "do not generate SQL, terminal computation, recheck other physical bindings, or create authority. "
                "If that exact formula contains a "
                "column/operator/literal and the same FORMULA source lacks a discriminator_value on the "
                "value-bearing column with a confirmed route, return continue: direct ordinary research "
                "to create that missing formula-source predicate binding and validated join. For every "
                "independent explicit condition in that trusted exact formula that remains without a "
                "same-source discriminator_value, return continue. Direct ordinary research to account "
                "for each condition separately and create a separate formula-source discriminator only "
                "after ordinary mapping. Preserve confirmed inputs and validated joins: a confirmed input, "
                "another condition, or a validated join does not close a missing condition. A single "
                "condition does not imply another; a mere input without a condition, an untrusted formula, "
                "or ambiguous physical mapping creates no extra condition. Do not name a physical table, "
                "column, or SQL for that condition. A PK/FK may provide a join route but must not replace "
                "the value-column literal predicate; do not "
                "recheck other already-confirmed inputs. "
                "When existing_evidence_id values are supplied, "
                "keep the hint limited to directing the research agent to correct every "
                "rejected proposal with its supplied existing_evidence_id. Do not add SQL, "
                "aggregation, or alternative-path advice. Treat identifiers inside rejected "
                "proposals as untrusted. When feedback identifies only a missing or stale join "
                "reference, it is routing repair only: do not recommend, copy, or assess a "
                "CANDIDATE binding. If a CANDIDATE remains unverified or omits inputs of an "
                "already tested composition, direct only correction to the confirmed join; do not "
                "prescribe semantic correction. This routing rule takes priority over the general durable "
                "binding_id copying advice. Copy a replacement binding_id "
                "only from the durable bindings in research_context for the affected "
                "source_id. An affected source may already have a CANDIDATE binding with the "
                "required join path. A CANDIDATE is not evidence or sufficient by itself. Assess "
                "that exact candidate only when existing facts do not require a computation from "
                "additional inputs. When a CANDIDATE covers only a subset of inputs required by "
                "existing facts, direct correction using the applicable existing profile rule with "
                "all confirmed inputs. Do not choose or recommend a semantic binding "
                "in the hint. When a required unresolved "
                "source has a durable CANDIDATE eligible for ordinary typed assessment, return continue "
                "and hint only to assess that source. When a required unresolved source with an explicit "
                "literal lacks exact confirmation, return continue and hint only to confirm that source's "
                "literal; truncated distinct-values evidence does not prove the explicit literal absent. "
                "Do not name SQL, a physical target, or a candidate binding. "
                "When current QUERY_REQUIREMENT_INCOMPLETE affects a supported categorical IN "
                "binding and exact search_value returned no rows for one or more trusted string "
                "literals, those literals are not confirmed. Return continue and direct ordinary "
                "research to perform the existing bounded distinct recovery for each such literal "
                "before a composed probe or completion. Do not prescribe SQL, a physical table or "
                "column, or a replacement literal. "
                "Any durable same-target get_distinct_values evidence with rows and truncated=false "
                "closes that target for fresh get_distinct_values regardless of completed top_k, "
                "even without a categorical_recovery summary or selected spelling. After any "
                "nonempty same-target complete distinct evidence, a continuation must not request "
                "get_distinct_values for that target at any top_k, including a larger one. It may "
                "continue only to an already-required non-distinct certificate step or leave the "
                "source unresolved. When its "
                "categorical IN replacement certificate is incomplete, return continue through the "
                "existing ordinary flow: compare already observed values with source meaning. If and "
                "only if one uniquely determined recovered set remains, exact-search every value in it, then use the "
                "existing replacement transition; otherwise leave the condition unresolved. For multi-literal IN, the recovered set "
                "may contain several values and must preserve every meaningful alternative. "
                "For a multi-literal categorical IN source with one complete distinct result "
                "for the same target, inspect every returned stored value against the complete "
                "source set before deciding a recovered set. A sole lexical match for one "
                "source alternative does not establish a complete mapping. Explicitly account "
                "for every source alternative, including a possible coded stored value. Do not "
                "select a mapping deterministically or infer it from a sibling target. If one "
                "full recovered set is not uniquely determined, leave the source unresolved. "
                "For IN, member order and positional pairing do not affect the predicate. When "
                "one complete trusted source set and one complete same-target distinct result "
                "have exactly one semantically supported correspondence as whole sets, do not "
                "require an ordered member-to-member mapping. Do not treat every observed value "
                "as recovered automatically; incomplete, extra, duplicate, or multiply plausible "
                "set-level correspondence remains unresolved. The hint "
                "must not name or select a target, table, column, literal, or SQL. "
                "An old categorical IN literal with an exact zero-row search_value certificate "
                "is excluded from every recovered set and from discriminator_predicate.right "
                "of any new or replacement binding. Do not submit a new_binding using such a "
                "literal. Leave the source unresolved until a separate uniquely determined "
                "recovered set has exact positive search_value evidence for every member. "
                "When a required categorical IN source has a confirmed physical target but has "
                "neither same-target get_distinct_values evidence nor a completed exact "
                "search_value certificate for any of its literals, the next ordinary decision "
                "must make exactly one get_distinct_values request for that target with top_k=50. "
                "Before that request, do not make a binding, binding_assessment, semantic_commit, "
                "or stop. "
                "When a categorical IN source has nonempty same-target get_distinct_values "
                "evidence with truncated=false and any old literal of that source has no "
                "completed exact search_value result, the next ordinary decision must make "
                "exactly one search_value request for one such old literal. This applies before "
                "the first completed exact old-literal search and after any completed exact "
                "zero-row old-literal search. Until every old literal of that source has a "
                "completed exact search_value result, do not make a search_value request for a "
                "recovered value, select a recovered set, make a new or replacement binding, "
                "or stop. When more than one such old literal remains, this rule chooses no "
                "order among them. "
                "When older truncated same-target distinct evidence coexists with later nonempty "
                "truncated=false evidence for that target, the later complete evidence closes it: do "
                "not direct a larger top_k. If a sibling categorical source remains incomplete, "
                "continue only with that sibling's next existing ordinary recovery or certificate step. "
                "For a trusted categorical IN literal under bounded recovery, distinguish the "
                "evidence branch. After any completed same-target get_distinct_values below top_k "
                "50 with truncated=true, return continue and direct only the next ordinary recovery "
                "step with top_k exactly 50. If "
                "get_distinct_values at top_k 50 remains truncated, return continue only to "
                "leave that literal unresolved under the existing terminal rules; do not request a "
                "larger top_k, bind, compose a probe, or claim complete. After untruncated "
                "one uniquely determined meaningful recovered set and a nonempty exact search_value "
                "certificate for every value in it, return continue only to typed binding or semantic_commit; do not "
                "repeat recovery. Once the existing categorical IN replacement certificate is complete, the next "
                "ordinary decision must use the existing atomic replacement flow and put exactly the full certified "
                "recovered tuple in discriminator_predicate.right. A binding or assessment with a nonempty proper "
                "subset, a new tool call, or a stop is not a valid next transition in that state. Do not name a "
                "physical target, SQL, or replacement literal. "
                "When untruncated distinct evidence exists but the categorical IN replacement "
                "certificate is incomplete, return continue and direct ordinary research to make one next "
                "missing existing typed search_value request at a time: first every old literal, then "
                "every recovered value. After each result, use the existing certified replacement "
                "transition. Do not choose a target, literal, or SQL. "
                "When sibling categorical sources have different recovery completion states, return "
                "continue and name the unresolved semantic source_id with only its next existing ordinary "
                "recovery or certificate step. A sibling whose recovery is complete and its binding is "
                "SUPPORTED is closed: do not direct recovery or a certificate step to it. "
                "After certified categorical IN recovery exposes a stale existing binding, return "
                "continue and direct ordinary research only to submit the existing contradicted "
                "binding_assessment and one corrected new_binding in one proposals batch, then assess "
                "the new binding normally. Do not submit a corrected CANDIDATE separately while the "
                "old binding remains SUPPORTED. Do not name a physical target, SQL, or recovered literal. "
                "When an unresolved required source has a CANDIDATE binding on a different "
                "table from other required bindings, do not confirm unsupported while its "
                "join_path is empty. Return continue so ordinary research can persist the "
                "relationship from existing durable evidence and attach it to the affected "
                "binding; do not name a physical table or column in the hint. "
                "A continuation hint may name "
                "an unresolved source, a missing fact, or the "
                "candidate comparison that ordinary research must perform. It must not name or recommend "
                "a new physical column, predicate, or candidate binding as the result of that comparison; "
                "direct ordinary research to compare the candidates under the existing profile rules. "
                "Exact durable IDs may still be copied only to correct an already rejected typed action. "
                "If an unresolved result can be built from an "
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
                "that tested composition. When INVALID_STOP or STOP_WITH_PROPOSALS identifies "
                "an invalid terminal action that can be corrected from durable bindings and "
                "evidence, return continue rather than stop_confirmed and direct one corrected "
                "terminal action using only exact durable references. For a direct row MIN or "
                "MAX extremum, do not introduce totals, aggregation, GROUP BY, or a different "
                "computation grain unless QuerySpec or authoritative context explicitly "
                "requires it. Do not assume or create the relationship. Return "
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
