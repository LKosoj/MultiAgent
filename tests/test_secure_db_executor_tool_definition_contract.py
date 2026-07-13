from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import yaml


_DEFINITION_PATH = Path("tool_definitions/secure_db_executor.yaml")


def _load_definition() -> dict:
    return yaml.safe_load(_DEFINITION_PATH.read_text(encoding="utf-8"))


def test_secure_db_executor_yaml_matches_optional_keyword_only_contract():
    definition = _load_definition()
    parameters = {item["name"]: item for item in definition["parameters"]}

    dry_run = parameters["dry_run_only"]
    assert dry_run["type"] == "boolean"
    assert dry_run["required"] is False
    assert dry_run["nullable"] is True
    assert dry_run["default"] is None

    description = dry_run["description"]
    assert "keyword-only" in description
    assert "TEXT_TO_SQL_DRY_RUN_ONLY" in description
    assert "payload OR env" in description

    module_name, function_name = definition["implementation_source"].rsplit(".", 1)
    function = getattr(importlib.import_module(module_name), function_name)
    signature_parameter = inspect.signature(function).parameters["dry_run_only"]
    assert signature_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert signature_parameter.default is None


def test_agent_factory_registers_nullable_boolean_dry_run_input(monkeypatch, tmp_path):
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(mcp_config))

    from agent_factory import AgentFactory

    factory = AgentFactory()
    registered_tool = factory._create_tool("secure_db_executor")

    assert registered_tool is factory.tool_mapping["secure_db_executor"]
    assert registered_tool.name == "secure_db_executor"
    assert registered_tool.inputs["dry_run_only"]["type"] == "boolean"
    assert registered_tool.inputs["dry_run_only"]["nullable"] is True
    assert "TEXT_TO_SQL_DRY_RUN_ONLY" in registered_tool.inputs["dry_run_only"]["description"]


def test_secure_db_executor_output_schema_matches_terminal_evidence_contract():
    output = _load_definition()["output"]["properties"]

    assert output["applied_row_limit"] == {
        "type": "integer",
        "minimum": 1,
        "nullable": True,
    }
    assert {item["type"] for item in output["data"]["items"]["oneOf"]} == {
        "array",
        "object",
    }
    issue_schema = output["safety_issues"]["items"]
    assert issue_schema["required"] == ["issue_type", "description"]
    assert issue_schema["additionalProperties"] is False
    explain = output["explain_result"]
    assert explain["required"] == [
        "plan",
        "estimated_cost",
        "rows_to_scan",
        "issues",
    ]
    assert explain["additionalProperties"] is False
    assert output["pre_execution_error_code"]["enum"] == [
        "INVALID_ROW_LIMIT",
        "INVALID_ROW_LIMIT_CAP",
        "INVALID_STATEMENT_TIMEOUT",
    ]
    capability_error = output["capability_error"]
    assert capability_error["type"] == "object"
    assert capability_error["required"] == ["capability", "reason_code"]
    assert capability_error["additionalProperties"] is False
    assert capability_error["properties"]["capability"]["type"] == "string"
    assert capability_error["properties"]["reason_code"]["type"] == "string"
