"""Dependency purity and shared identity for W3 research contracts."""

from __future__ import annotations

import json
import subprocess
import sys


def test_research_decision_and_reentry_import_without_runtime_chain() -> None:
    script = """
import json
import sys

import custom_tools.text_to_sql.adaptive.research_decision as decision
import custom_tools.text_to_sql.adaptive.research_reentry as reentry

forbidden = (
    "custom_tools.text_to_sql.adaptive.tool_registry",
    "custom_tools.text_to_sql.adaptive.controller",
    "custom_tools.text_to_sql.adaptive.decision_resolver",
    "custom_tools.text_to_sql.adaptive.semantic_reducer",
    "custom_tools.text_to_sql.schema_loader",
    "workflow.adaptive_state_store",
    "sqlite3",
)
print(json.dumps({
    "forbidden": [name for name in forbidden if name in sys.modules],
    "contracts_loaded": "custom_tools.text_to_sql.adaptive.research_tool_contracts" in sys.modules,
    "decision_module": decision.ResearchDecisionV1.__module__,
    "intent_module": decision.ToolIntent.__module__,
    "captured_decision": reentry._CANONICAL_RESEARCH_DECISION_TYPE is decision.ResearchDecisionV1,
    "captured_intent": reentry._CANONICAL_TOOL_INTENT_TYPE is decision.ToolIntent,
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    observed = json.loads(completed.stdout)
    assert observed == {
        "forbidden": [],
        "contracts_loaded": True,
        "decision_module": "custom_tools.text_to_sql.adaptive.research_decision",
        "intent_module": "custom_tools.text_to_sql.adaptive.research_decision",
        "captured_decision": True,
        "captured_intent": True,
    }


def test_registry_reexports_the_exact_pure_argument_contracts() -> None:
    from custom_tools.text_to_sql.adaptive import (
        research_tool_contracts,
        tool_registry,
    )

    names = (
        "SearchSchemaCatalogArguments",
        "InspectTableArguments",
        "InspectColumnArguments",
        "InspectRelationshipsArguments",
        "ProfileColumnArguments",
        "SampleRowsArguments",
        "SearchValueArguments",
        "GetDistinctValuesArguments",
        "ExecuteResearchProbeArguments",
        "ReadSchemaEvidenceArguments",
    )

    assert all(
        getattr(tool_registry, name) is getattr(research_tool_contracts, name)
        for name in names
    )
    assert tool_registry.ProfileColumnArguments is tool_registry.InspectColumnArguments


def test_decision_identity_json_and_schema_are_unchanged() -> None:
    from custom_tools.text_to_sql.adaptive.research_decision import (
        InspectColumnIntent,
        ResearchDecisionV1,
        ToolIntent,
    )
    from custom_tools.text_to_sql.adaptive.research_tool_contracts import (
        InspectColumnArguments,
    )
    from custom_tools.text_to_sql.adaptive.serialization import (
        canonical_digest,
        canonical_json_bytes,
    )

    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": ToolIntent(
                next_kind="tool",
                hypothesis_ref=None,
                intent=InspectColumnIntent(
                    tool_name="inspect_column",
                    arguments=InspectColumnArguments(
                        table="orders",
                        column="status",
                    ),
                ),
            ),
        }
    )

    assert ResearchDecisionV1.__module__ == (
        "custom_tools.text_to_sql.adaptive.research_decision"
    )
    assert ResearchDecisionV1.__qualname__ == "ResearchDecisionV1"
    assert ToolIntent.__module__ == (
        "custom_tools.text_to_sql.adaptive.research_decision"
    )
    assert ToolIntent.__qualname__ == "ToolIntent"
    assert canonical_json_bytes(decision) == (
        b'{"decision_version":1,"next":{"hypothesis_ref":null,'
        b'"intent":{"arguments":{"column":"status","table":"orders"},'
        b'"tool_name":"inspect_column"},"next_kind":"tool"},"proposals":[]}'
    )
    assert canonical_digest(decision) == (
        "sha256:35c448c0dbe3d7e6861094b4fd747cc50bcb2c9d9f7065637ff5fb47e07e6251"
    )
    assert canonical_digest(ResearchDecisionV1.model_json_schema()) == (
        "sha256:7dd5e4dc0d831ec9183651818522c104d2afdf0aa19497e8c63e945e0b207e95"
    )
    assert canonical_digest(ToolIntent.model_json_schema()) == (
        "sha256:f92074173045d807a909d21ca66a4db0500bb1750a52cbb1692781084eb31374"
    )


def test_pre_registration_forgery_cannot_become_coordinator_authority() -> None:
    script = """
import json
import sys
from types import ModuleType

from custom_tools.text_to_sql.adaptive.models import StrictModel
import custom_tools.text_to_sql.adaptive.serialization as serialization

module_name = "custom_tools.text_to_sql.adaptive.research_decision"
forged_module = ModuleType(module_name)
forged_type = type(
    "ResearchDecisionV1",
    (StrictModel,),
    {
        "__module__": module_name,
        "__annotations__": {"proposals": tuple, "next": object},
    },
)
forged_module.ResearchDecisionV1 = forged_type
sys.modules[module_name] = forged_module
serialization._register_internal_decode_models(forged_type)
del sys.modules[module_name]

imported = False
captured_forgery = False
try:
    import custom_tools.text_to_sql.adaptive.research_reentry as reentry
except TypeError:
    pass
else:
    imported = True
    captured_forgery = reentry._CANONICAL_RESEARCH_DECISION_TYPE is forged_type

print(json.dumps({
    "mutable_trust_helper_exposed": hasattr(
        serialization, "_is_registered_internal_decode_model"
    ),
    "imported": imported,
    "captured_forgery": captured_forgery,
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    observed = json.loads(completed.stdout)
    assert observed["mutable_trust_helper_exposed"] is False
    assert observed["imported"] is False
    assert observed["captured_forgery"] is False
