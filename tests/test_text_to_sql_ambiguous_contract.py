from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.replay_contract import (
    ResearchTerminalReplayAction,
)
from custom_tools.text_to_sql.adaptive.research_decision import StopRequest
from custom_tools.text_to_sql.adaptive.terminal import research_stop_terminal_result
from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
from workflow.text_to_sql_contract import TextToSqlTerminalResult


RUN_ID = "ambiguous-contract-run"


def _ambiguity_report() -> dict[str, object]:
    return {
        "interpretations": (
            "Revenue means invoiced amount.",
            "Revenue means collected payment amount.",
        ),
        "citation_evidence_ids": ("evidence-a",),
        "missing_distinguishing_fact": "The metric definition is absent.",
    }


def _ambiguous_stop_payload() -> dict[str, object]:
    return {
        "next_kind": "stop",
        "reason": "ambiguous",
        "source_ids": ("source-a",),
        "citation_evidence_ids": ("evidence-a",),
        "ambiguity": _ambiguity_report(),
    }


def test_ambiguous_stop_requires_and_preserves_the_strict_report() -> None:
    stop = StopRequest.model_validate(_ambiguous_stop_payload())

    assert stop.ambiguity.interpretations == (
        "Revenue means invoiced amount.",
        "Revenue means collected payment amount.",
    )
    assert stop.ambiguity.citation_evidence_ids == stop.citation_evidence_ids
    assert stop.ambiguity.missing_distinguishing_fact == (
        "The metric definition is absent."
    )
    invalid_payloads = []
    for mutate in (
        lambda payload: payload.pop("ambiguity"),
        lambda payload: payload["ambiguity"].update(
            interpretations=("Only one reading.",)
        ),
        lambda payload: payload["ambiguity"].update(
            interpretations=("", "A second reading.")
        ),
        lambda payload: payload["ambiguity"].update(
            interpretations=("Same reading.", "Same reading.")
        ),
        lambda payload: payload["ambiguity"].update(
            missing_distinguishing_fact=""
        ),
        lambda payload: payload["ambiguity"].update(
            citation_evidence_ids=("evidence-a", "evidence-b")
        ),
        lambda payload: payload["ambiguity"].update(unexpected="extra"),
    ):
        payload = deepcopy(_ambiguous_stop_payload())
        mutate(payload)
        invalid_payloads.append(payload)
    for reason in ("complete", "unsupported"):
        payload = _ambiguous_stop_payload()
        payload["reason"] = reason
        if reason == "complete":
            payload["source_ids"] = ()
        invalid_payloads.append(payload)
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            StopRequest.model_validate(payload)


def test_ambiguous_terminal_replay_carries_the_exact_report() -> None:
    action = ResearchTerminalReplayAction.model_validate(
        {
            "contract_version": 2,
            "kind": "research_terminal",
            "reason": ResearchStopReason.AMBIGUOUS,
            "affected_source_ids": ("source-a",),
            "citation_evidence_ids": ("evidence-a",),
            "ambiguity": _ambiguity_report(),
            "rejection_signatures": (),
        }
    )

    assert action.ambiguity.model_dump(mode="python") == _ambiguity_report()
    assert action.ambiguity.citation_evidence_ids == action.citation_evidence_ids
    for mutate in (
        lambda payload: payload.pop("ambiguity"),
        lambda payload: payload["ambiguity"].update(
            citation_evidence_ids=("evidence-b",)
        ),
        lambda payload: payload["ambiguity"].update(
            missing_distinguishing_fact=""
        ),
    ):
        payload = {
            "contract_version": 2,
            "kind": "research_terminal",
            "reason": ResearchStopReason.AMBIGUOUS,
            "affected_source_ids": ("source-a",),
            "citation_evidence_ids": ("evidence-a",),
            "ambiguity": _ambiguity_report(),
            "rejection_signatures": (),
        }
        mutate(payload)
        with pytest.raises(ValidationError):
            ResearchTerminalReplayAction.model_validate(payload)


def test_public_ambiguous_terminal_rejects_legacy_mapping() -> None:
    ambiguity = StopRequest.model_validate(_ambiguous_stop_payload()).ambiguity
    terminal = research_stop_terminal_result(
        RUN_ID,
        ResearchStopReason.AMBIGUOUS,
        ambiguity,
    )

    assert terminal is not None
    assert terminal.executed is False
    assert terminal.ambiguity == ambiguity
    legacy_mapping = terminal.to_mapping()
    legacy_mapping.pop("ambiguity")
    with pytest.raises(ValueError, match="ambiguity"):
        TextToSqlTerminalResult.from_mapping(legacy_mapping)
