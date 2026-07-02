"""Deterministic confidence/abstention helpers for Text-to-SQL stages."""

from __future__ import annotations

import os
from typing import Any


def schema_linking_quality(result: dict[str, Any]) -> dict[str, Any]:
    linked = result.get("linked_entities") if isinstance(result, dict) else {}
    linked = linked if isinstance(linked, dict) else {}
    linked_count = _linked_count(linked)
    unlinked = result.get("unlinked_entities") if isinstance(result, dict) else []
    unlinked_count = len(unlinked) if isinstance(unlinked, list) else 0
    warnings = result.get("join_warnings") if isinstance(result, dict) else []
    warning_count = len(warnings) if isinstance(warnings, list) else 0

    confidence = 1.0
    reasons: list[str] = []
    if result.get("error"):
        confidence -= 0.4
        reasons.append("schema_linking_error")
    if result.get("join_success") is False:
        confidence -= 0.35
        reasons.append("join_path_failed")
    if linked_count == 0:
        confidence -= 0.25
        reasons.append("no_linked_entities")
    if unlinked_count:
        confidence -= min(0.3, 0.1 * unlinked_count)
        reasons.append("unlinked_entities")
    if warning_count:
        confidence -= min(0.2, 0.05 * warning_count)
        reasons.append("join_warnings")

    confidence = max(0.0, min(1.0, round(confidence, 3)))
    ambiguity = {
        "requires_clarification": bool(unlinked_count or linked_count == 0),
        "unlinked_count": unlinked_count,
        "reasons": reasons,
    }
    min_confidence = _min_confidence_to_generate()
    abstain = bool(result.get("join_success") is False or confidence < min_confidence)
    abstention_reason = None
    if abstain:
        abstention_reason = ", ".join(reasons) if reasons else "low_confidence"
    return {
        "confidence": confidence,
        "ambiguity": ambiguity,
        "abstain": abstain,
        "abstention_reason": abstention_reason,
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
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))
