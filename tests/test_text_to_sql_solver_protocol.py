"""Tests for the closed transient SQL-solver proposal contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from custom_tools.text_to_sql.adaptive.models import EvidenceSourceKind
from custom_tools.text_to_sql.adaptive.serialization import (
    ContractDecodeError,
    ContractValidationError,
    StateSizeLimitError,
    deserialize_as,
    serialize_contract,
)
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MAX_SOLVER_PROPOSAL_BYTES,
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
    parse_solver_proposal,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sql_payload() -> dict[str, object]:
    return {
        "proposal_version": 1,
        "proposal": {
            "proposal_kind": "sql_candidate",
            "sql": "SELECT id FROM orders",
        },
    }


def _missing_payload() -> dict[str, object]:
    return {
        "proposal_version": 1,
        "proposal": {
            "proposal_kind": "missing_evidence",
            "source_id": "source-1",
            "question": "Which column stores the customer tier?",
            "required_evidence_kind": "schema",
            "reason": "The candidate query needs the tier column.",
        },
    }


def test_sql_candidate_is_the_only_sql_authority() -> None:
    proposal = parse_solver_proposal(json.dumps(_sql_payload()))

    assert type(proposal.proposal) is SqlCandidateProposal
    assert set(type(proposal).model_fields) == {"proposal_version", "proposal"}
    assert set(type(proposal.proposal).model_fields) == {"proposal_kind", "sql"}
    assert proposal.proposal.sql == "SELECT id FROM orders"


def test_missing_evidence_has_exact_transient_fields() -> None:
    proposal = parse_solver_proposal(json.dumps(_missing_payload()))

    assert type(proposal.proposal) is MissingEvidenceProposal
    assert set(type(proposal.proposal).model_fields) == {
        "proposal_kind",
        "source_id",
        "question",
        "required_evidence_kind",
        "reason",
    }
    assert proposal.proposal.required_evidence_kind is EvidenceSourceKind.SCHEMA


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "run_id",
        "query_plan",
        "check_result",
        "execution",
        "terminal",
        "selection",
        "proposal_id",
        "status",
    ),
)
def test_runtime_authority_fields_are_rejected(forbidden_field: str) -> None:
    payload = _sql_payload()
    payload[forbidden_field] = "not model authority"

    with pytest.raises(ContractValidationError):
        parse_solver_proposal(json.dumps(payload))


@pytest.mark.parametrize(
    "payload",
    (
        b"{",
        b'{"proposal_version":1,"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
        (b"[" * 65) + b"0" + (b"]" * 65),
        b'{"proposal_version":2,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
        b'{"proposal_version":1,"proposal":{"proposal_kind":"unknown","sql":"SELECT 1"}}',
        b'{"proposal_version":1,"proposal":{"proposal_kind":"missing_evidence","source_id":"source-1","question":"Which?","required_evidence_kind":"unknown","reason":"Needed."}}',
    ),
)
def test_parser_rejects_noncanonical_or_unknown_payloads(payload: bytes) -> None:
    with pytest.raises(ContractDecodeError):
        parse_solver_proposal(payload)


def test_parser_rejects_oversized_payload() -> None:
    payload = b"{" + b" " * MAX_SOLVER_PROPOSAL_BYTES + b"}"

    with pytest.raises(StateSizeLimitError):
        parse_solver_proposal(payload)


@pytest.mark.parametrize(
    "payload",
    (
        b"\xef\xbb\xbf"
        b'{"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
        "\ufeff"
        '{"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
    ),
)
def test_parser_rejects_utf8_bom(payload: bytes | str) -> None:
    with pytest.raises(ContractDecodeError):
        parse_solver_proposal(payload)


def test_transient_proposal_is_deserialized_only_and_not_persisted() -> None:
    payload = json.dumps(_sql_payload())

    assert deserialize_as(payload, SolverProposalV1) == parse_solver_proposal(payload)
    with pytest.raises(ContractValidationError, match="registered top-level"):
        serialize_contract(parse_solver_proposal(payload))


def test_solver_decode_allowlist_uses_lazy_exact_identity() -> None:
    script = """
import sys
from pydantic import BaseModel
from custom_tools.text_to_sql.adaptive.serialization import deserialize_as

module_name = "custom_tools.text_to_sql.adaptive.solver_protocol"
assert module_name not in sys.modules

class Spoof(BaseModel):
    pass

Spoof.__module__ = module_name
Spoof.__name__ = "SolverProposalV1"
try:
    deserialize_as(b"{}", Spoof)
except TypeError:
    pass
else:
    raise AssertionError("spoof model was accepted")
assert module_name not in sys.modules

from custom_tools.text_to_sql.adaptive.solver_protocol import SolverProposalV1
assert deserialize_as(
    b'{"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
    SolverProposalV1,
).proposal.sql == "SELECT 1"
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_internal_registration_rejects_same_name_impostor_atomically() -> None:
    from pydantic import BaseModel

    from custom_tools.text_to_sql.adaptive import serialization

    class SolverProposalImpostor(BaseModel):
        pass

    SolverProposalImpostor.__module__ = SolverProposalV1.__module__
    SolverProposalImpostor.__qualname__ = SolverProposalV1.__qualname__

    serialization._SUPPORTED_DECODE_MODELS.discard(SolverProposalV1)
    try:
        with pytest.raises(TypeError, match="identity"):
            serialization._register_internal_decode_models(
                SolverProposalV1,
                SolverProposalImpostor,
            )
        assert SolverProposalV1 not in serialization._SUPPORTED_DECODE_MODELS
    finally:
        serialization._register_internal_decode_models(SolverProposalV1)


def test_registered_identity_survives_declared_module_replacement() -> None:
    script = """
import sys
from types import ModuleType
from pydantic import BaseModel

from custom_tools.text_to_sql.adaptive.solver_protocol import SolverProposalV1
from custom_tools.text_to_sql.adaptive.serialization import (
    _INTERNAL_DECODE_MODEL_OBJECTS,
    _SUPPORTED_DECODE_MODELS,
    _register_internal_decode_models,
    deserialize_as,
)

module_name = SolverProposalV1.__module__
canonical_module = sys.modules[module_name]

class ForgedSolverProposalV1(BaseModel):
    pass

ForgedSolverProposalV1.__module__ = module_name
ForgedSolverProposalV1.__qualname__ = SolverProposalV1.__qualname__
replacement = ModuleType(module_name)
replacement.SolverProposalV1 = ForgedSolverProposalV1
sys.modules[module_name] = replacement
try:
    try:
        _register_internal_decode_models(ForgedSolverProposalV1)
    except TypeError:
        pass
    else:
        raise AssertionError("replacement-module impostor was accepted")

    identity = (SolverProposalV1.__module__, SolverProposalV1.__qualname__)
    assert _INTERNAL_DECODE_MODEL_OBJECTS[identity] is SolverProposalV1
    assert ForgedSolverProposalV1 not in _SUPPORTED_DECODE_MODELS
    decoded = deserialize_as(
        b'{"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}',
        SolverProposalV1,
    )
    assert type(decoded) is SolverProposalV1
finally:
    sys.modules[module_name] = canonical_module
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_decode_allowlist_rejects_equality_and_hash_impostor() -> None:
    script = """
from custom_tools.text_to_sql.adaptive.solver_protocol import SolverProposalV1
from custom_tools.text_to_sql.adaptive.serialization import deserialize_as

class EqualityImpostorMeta(type(SolverProposalV1)):
    def __hash__(cls):
        return hash(SolverProposalV1)

    def __eq__(cls, other):
        if other is SolverProposalV1:
            return True
        return super().__eq__(other)

class EqualityImpostor(
    SolverProposalV1,
    metaclass=EqualityImpostorMeta,
):
    pass

payload = b'{"proposal_version":1,"proposal":{"proposal_kind":"sql_candidate","sql":"SELECT 1"}}'
try:
    deserialize_as(payload, EqualityImpostor)
except TypeError:
    pass
else:
    raise AssertionError("equality/hash impostor was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_protocol_import_does_not_load_execution_or_tool_runtime() -> None:
    script = """
import sys

import custom_tools.text_to_sql.adaptive.solver_protocol

for module_name in (
    "agent_command",
    "agent_factory",
    "smolagents",
    "custom_tools.text_to_sql.adaptive.tool_registry",
    "custom_tools.text_to_sql.adaptive.research_loop",
    "custom_tools.text_to_sql.adaptive.pre_execution_gate",
):
    assert module_name not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
