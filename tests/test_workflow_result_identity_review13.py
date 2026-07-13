from __future__ import annotations

import pytest

import workflow.result_identity as identity_module


def _v1_payload() -> dict[str, str]:
    return {
        "run_id": "legacy:run",
        "run_incarnation": "legacy:inc",
        "event_key": "workflow-result:legacy:run:legacy:inc",
        "status": "failed",
    }


def test_v2_event_keys_are_injective_for_colon_containing_components():
    first = identity_module.workflow_result_event_key("a:b", "c")
    second = identity_module.workflow_result_event_key("a", "b:c")

    assert first == "workflow-result:v2:3:a:b:1:c"
    assert second == "workflow-result:v2:1:a:3:b:c"
    assert first != second


def test_v2_event_key_parser_round_trips_length_delimited_components():
    event_key = identity_module.workflow_result_event_key("run:α", "inc:β")

    identity = identity_module.parse_workflow_result_event_key(event_key)

    assert identity.run_id == "run:α"
    assert identity.run_incarnation == "inc:β"
    assert identity.event_key == event_key


def test_default_identity_validator_rejects_v1_key():
    payload = _v1_payload()

    with pytest.raises(ValueError, match="v2|event_key"):
        identity_module.validate_workflow_result_identity(
            event_key=payload["event_key"],
            run_id=payload["run_id"],
            run_incarnation=payload["run_incarnation"],
        )


def test_explicit_legacy_read_helper_validates_and_upgrades_v1_payload():
    payload = _v1_payload()

    legacy = identity_module.workflow_result_identity_from_legacy_payload(payload)
    upgraded = identity_module.upgrade_legacy_workflow_result_payload(payload)

    assert legacy.event_key == payload["event_key"]
    assert legacy.run_id == payload["run_id"]
    assert legacy.run_incarnation == payload["run_incarnation"]
    assert upgraded == {
        **payload,
        "event_key": "workflow-result:v2:10:legacy:run:10:legacy:inc",
    }
