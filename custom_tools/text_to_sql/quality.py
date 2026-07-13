"""Deterministic confidence/abstention helpers for Text-to-SQL stages."""

from __future__ import annotations

import math
import os
from enum import Enum
from typing import Any


class SchemaLinkingDecision(str, Enum):
    PROCEED = "PROCEED"
    CLARIFY = "CLARIFY"
    ABSTAIN = "ABSTAIN"


class SchemaLinkingReason(str, Enum):
    SCHEMA_CONTEXT_BUDGET_EXCEEDED = "SCHEMA_CONTEXT_BUDGET_EXCEEDED"
    SCHEMA_LINKING_ERROR = "SCHEMA_LINKING_ERROR"
    NO_SCHEMA = "NO_SCHEMA"
    NO_VALID_BINDING = "NO_VALID_BINDING"
    JOIN_PATH_FAILED = "JOIN_PATH_FAILED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNRESOLVED_ENTITIES = "UNRESOLVED_ENTITIES"
    AMBIGUOUS_BINDINGS = "AMBIGUOUS_BINDINGS"


def schema_linking_quality(result: dict[str, Any]) -> dict[str, Any]:
    linked = result.get("linked_entities") if isinstance(result, dict) else {}
    linked = linked if isinstance(linked, dict) else {}
    linked_count = _linked_count(linked)
    unresolved = result.get("unresolved_entities") if isinstance(result, dict) else []
    unresolved = unresolved if isinstance(unresolved, list) else []
    unresolved_count = len(unresolved)
    ambiguous = result.get("ambiguous_bindings") if isinstance(result, dict) else []
    ambiguous = ambiguous if isinstance(ambiguous, list) else []
    warnings = result.get("join_warnings") if isinstance(result, dict) else []
    warning_count = len(warnings) if isinstance(warnings, list) else 0

    confidence = 1.0
    if result.get("error"):
        confidence -= 0.4
    if result.get("join_success") is False:
        confidence -= 0.35
    if linked_count == 0:
        confidence -= 0.25
    if unresolved_count and linked_count:
        confidence -= min(0.3, 0.1 * unresolved_count)
    if warning_count:
        confidence -= min(0.2, 0.05 * warning_count)

    confidence = max(0.0, min(1.0, round(confidence, 3)))
    min_confidence = _min_confidence_to_generate()
    decision_reasons: list[str] = []
    if result.get("reason_code") == SchemaLinkingReason.SCHEMA_CONTEXT_BUDGET_EXCEEDED.value:
        decision_reasons.append(
            SchemaLinkingReason.SCHEMA_CONTEXT_BUDGET_EXCEEDED.value
        )
    if result.get("error"):
        decision_reasons.append(SchemaLinkingReason.SCHEMA_LINKING_ERROR.value)
    schema_info = result.get("schema_info")
    if not isinstance(schema_info, dict) or not schema_info:
        decision_reasons.append(SchemaLinkingReason.NO_SCHEMA.value)
    if linked_count == 0:
        decision_reasons.append(SchemaLinkingReason.NO_VALID_BINDING.value)
    if result.get("join_success") is not True:
        decision_reasons.append(SchemaLinkingReason.JOIN_PATH_FAILED.value)
    if confidence < min_confidence:
        decision_reasons.append(SchemaLinkingReason.LOW_CONFIDENCE.value)
    if unresolved_count:
        decision_reasons.append(SchemaLinkingReason.UNRESOLVED_ENTITIES.value)
    if ambiguous:
        decision_reasons.append(SchemaLinkingReason.AMBIGUOUS_BINDINGS.value)

    abstain_reasons = {
        SchemaLinkingReason.SCHEMA_CONTEXT_BUDGET_EXCEEDED.value,
        SchemaLinkingReason.SCHEMA_LINKING_ERROR.value,
        SchemaLinkingReason.NO_SCHEMA.value,
        SchemaLinkingReason.NO_VALID_BINDING.value,
        SchemaLinkingReason.JOIN_PATH_FAILED.value,
        SchemaLinkingReason.LOW_CONFIDENCE.value,
    }
    if any(reason in abstain_reasons for reason in decision_reasons):
        decision = SchemaLinkingDecision.ABSTAIN
    elif decision_reasons:
        decision = SchemaLinkingDecision.CLARIFY
    else:
        decision = SchemaLinkingDecision.PROCEED

    terminal_reason_code = ""
    if decision is SchemaLinkingDecision.CLARIFY:
        terminal_reason_code = "SCHEMA_CLARIFICATION_REQUIRED"
    elif decision is SchemaLinkingDecision.ABSTAIN:
        terminal_reason_code = (
            "SCHEMA_CONTEXT_BUDGET_EXCEEDED"
            if SchemaLinkingReason.SCHEMA_CONTEXT_BUDGET_EXCEEDED.value
            in decision_reasons
            else "SCHEMA_GROUNDING_FAILED"
        )

    ambiguity = {
        "requires_clarification": decision is SchemaLinkingDecision.CLARIFY,
        "unlinked_count": unresolved_count,
        "reasons": decision_reasons,
    }
    abstain = decision is SchemaLinkingDecision.ABSTAIN
    return {
        "confidence": confidence,
        "ambiguity": ambiguity,
        "decision": decision.value,
        "decision_reasons": decision_reasons,
        "abstain": abstain,
        "abstention_reason": ", ".join(decision_reasons) if abstain else None,
        "sql_generation_allowed": decision is SchemaLinkingDecision.PROCEED,
        "terminal_reason_code": terminal_reason_code,
    }


def _linked_count(linked: dict[str, Any]) -> int:
    count = 0
    for key in ("metrics", "dimensions"):
        items = linked.get(key)
        if isinstance(items, list):
            count += sum(
                1
                for item in items
                if isinstance(item, dict) and item.get("table") and item.get("column")
            )
    filters = linked.get("filters")
    if isinstance(filters, dict):
        count += sum(
            1
            for item in filters.values()
            if isinstance(item, dict) and item.get("table") and item.get("column")
        )
    return count


def _min_confidence_to_generate() -> float:
    raw = os.getenv("TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE", "0")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE must be a number between 0 and 1"
        ) from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(
            "TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE must be a number between 0 and 1"
        )
    return value
