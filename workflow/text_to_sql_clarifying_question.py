"""Deterministic clarifying-question surfacing for adaptive Text-to-SQL early stops.

Training-free: the question text is a fixed Russian template filled in with
fields already present on the verified Typed state (``AmbiguityReport`` /
``MissingEvidenceRequest``). No new LLM call, no case-specific text.
"""

from __future__ import annotations

import os
from typing import Any

from custom_tools.text_to_sql.adaptive.ambiguity import AmbiguityReport
from custom_tools.text_to_sql.adaptive.models import (
    ColumnRef,
    DocumentRef,
    MissingEvidenceRequest,
    QueryProbeRef,
    ResearchStopReason,
    SolverState,
    SolverStopReason,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome

_CLARIFYING_QUESTIONS_ENV = "TEXT_TO_SQL_CLARIFYING_QUESTIONS"

_AMBIGUOUS_QUESTION_TEMPLATE = (
    "Запрос можно понять по-разному, и данных для однозначного выбора "
    "не хватает: {fact}. Выберите один из вариантов ниже."
)

# ``options`` и ``question`` идут прямо в UI-виджет (не пользовательский ввод,
# поэтому инъекция не риск), но модель верифицированного Typed-состояния может
# вернуть десятки длинных интерпретаций/вопрос на тысячи символов — обрезаем,
# чтобы виджет оставался читаемым и ответ не раздувался.
MAX_CLARIFICATION_OPTIONS = 8
MAX_CLARIFICATION_QUESTION_CHARS = 600
_QUESTION_TRUNCATION_SUFFIX = "…"


def clarifying_questions_enabled() -> bool:
    """Return whether early-stop clarifying questions should be surfaced."""
    return os.getenv(_CLARIFYING_QUESTIONS_ENV, "1") != "0"


def _cap_options(options: list[str]) -> list[str]:
    # Длина каждого варианта ограничена тем же порогом, что и вопрос.
    return [_cap_question(option) for option in options[:MAX_CLARIFICATION_OPTIONS]]


def _cap_question(question: str) -> str:
    if len(question) <= MAX_CLARIFICATION_QUESTION_CHARS:
        return question
    keep = MAX_CLARIFICATION_QUESTION_CHARS - len(_QUESTION_TRUNCATION_SUFFIX)
    return question[:keep] + _QUESTION_TRUNCATION_SUFFIX


def _target_ref_label(target: object) -> str | None:
    if type(target) is ColumnRef:
        return f"{target.table.table}.{target.column}"
    if type(target) is TableRef:
        return target.table
    if type(target) is DocumentRef:
        return target.document_id
    if type(target) is QueryProbeRef:
        return target.probe_id
    return None


def build_text_to_sql_clarifying_question(
    *,
    research_outcome: object,
    solver_state: object,
    terminal_status: object,
    terminal_reason_code: object,
) -> dict[str, Any] | None:
    """Derive a non-case-specific clarifying question from verified Typed state."""

    if terminal_status != "abstained" or type(terminal_reason_code) is not str:
        return None

    if terminal_reason_code == "RESEARCH_AMBIGUOUS":
        if type(research_outcome) is not ResearchLoopOutcome:
            return None
        if research_outcome.stop_reason is not ResearchStopReason.AMBIGUOUS:
            return None
        ambiguity = research_outcome.ambiguity
        if type(ambiguity) is not AmbiguityReport:
            return None
        return {
            "kind": "research_ambiguous",
            "question": _cap_question(
                _AMBIGUOUS_QUESTION_TEMPLATE.format(
                    fact=ambiguity.missing_distinguishing_fact
                )
            ),
            "options": _cap_options(list(ambiguity.interpretations)),
            "evidence_ids": list(ambiguity.citation_evidence_ids),
        }

    if terminal_reason_code != "SCHEMA_CLARIFICATION_REQUIRED":
        return None
    if (
        type(solver_state) is not SolverState
        or solver_state.stop_reason is not SolverStopReason.MISSING_EVIDENCE
        or not solver_state.missing_evidence_requests
    ):
        return None
    request = solver_state.missing_evidence_requests[-1]
    if type(request) is not MissingEvidenceRequest:
        return None
    options = [
        label
        for label in (
            _target_ref_label(target) for target in request.candidate_targets
        )
        if label is not None
    ]
    return {
        "kind": "missing_evidence",
        "question": _cap_question(request.question),
        "options": _cap_options(options),
    }


__all__ = [
    "MAX_CLARIFICATION_OPTIONS",
    "MAX_CLARIFICATION_QUESTION_CHARS",
    "build_text_to_sql_clarifying_question",
    "clarifying_questions_enabled",
]
