from __future__ import annotations

import concurrent.futures
from dataclasses import FrozenInstanceError

import pytest

from custom_tools.text_to_sql.validators import SQLSafetyValidator
from custom_tools.text_to_sql.validators import safety_config
from custom_tools.text_to_sql.validators.safety_config import (
    TextToSqlSafetyPolicy,
    resolve_safety_policy,
)


@pytest.fixture(autouse=True)
def _reset_safety_policy(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    monkeypatch.delenv("TEXT2SQL_PROFILE", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_LEVEL", raising=False)
    safety_config.reset_cache()
    terminal = __import__(
        "custom_tools.text_to_sql.core._terminal",
        fromlist=["_pre_execution_gate_allowed"],
    )
    monkeypatch.setattr(
        terminal,
        "_pre_execution_gate_allowed",
        lambda **_kwargs: True,
    )
    yield
    safety_config.reset_cache()


def test_request_strict_resolves_strict_profile_not_default():
    policy = resolve_safety_policy("strict")

    assert policy.level == "strict"
    assert policy.profile_name == "strict"
    assert policy.forbidden_functions
    assert "pg_sleep" in policy.forbidden_functions
    assert policy.max_query_length == 4000
    assert policy.max_in_list_size == 200
    assert len(policy.policy_version) == 64


def test_policy_mapping_round_trip_is_strict_and_immutable():
    policy = resolve_safety_policy("strict")
    mapping = policy.to_mapping()

    assert TextToSqlSafetyPolicy.from_mapping(mapping) == policy
    assert TextToSqlSafetyPolicy.from_mapping(mapping).to_mapping() == mapping
    with pytest.raises(FrozenInstanceError):
        policy.level = "default"  # type: ignore[misc]

    with pytest.raises(ValueError, match="keys"):
        TextToSqlSafetyPolicy.from_mapping({**mapping, "unexpected": True})
    malformed = dict(mapping)
    malformed["max_query_length"] = True
    with pytest.raises(ValueError, match="max_query_length"):
        TextToSqlSafetyPolicy.from_mapping(malformed)


def test_pg_sleep_is_rejected_with_only_request_strict():
    policy = resolve_safety_policy("strict")

    result = SQLSafetyValidator(policy=policy).validate("SELECT pg_sleep(1)")

    assert result["is_safe"] is False
    assert any(
        issue.get("issue_type") == "FORBIDDEN_FUNCTION"
        for issue in result["issues"]
    )
    assert result["profile_name"] == policy.profile_name
    assert result["policy_version"] == policy.policy_version


def test_root_duckdb_transform_is_readonly_but_dml_stays_rejected():
    validator = SQLSafetyValidator()

    root_transform = validator.validate(
        "PIVOT sales ON category USING SUM(amount)", dsn="duckdb://"
    )
    insert = validator.validate("INSERT INTO sales VALUES (1)", dsn="duckdb://")

    assert root_transform["is_safe"] is True, root_transform["issues"]
    assert insert["is_safe"] is False


def test_explicit_policy_does_not_read_request_safety_level_env(monkeypatch):
    policy = resolve_safety_policy("strict")
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_LEVEL", "unsupported")

    result = SQLSafetyValidator(policy=policy).validate("SELECT 1")

    assert not any(
        issue.get("issue_type") == "UNSUPPORTED_SAFETY_LEVEL"
        for issue in result["issues"]
    )
    assert result["policy_version"] == policy.policy_version


def test_policy_version_changes_when_rules_change(tmp_path, monkeypatch):
    policy = resolve_safety_policy("strict")
    config_path = tmp_path / "safety.yaml"
    config_path.write_text(
        """
version: 2
profiles:
  strict:
    forbidden_keywords: [INSERT]
    forbidden_functions: [pg_sleep, internal_test_function]
    ast_forbidden_stmt_classes: [Insert]
    ast_forbidden_command_words: [LOAD]
    max_query_length: 4000
    max_in_list_size: 200
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", str(config_path))
    safety_config.reset_cache()

    changed = resolve_safety_policy("strict")

    assert changed.policy_version != policy.policy_version


def test_concurrent_explicit_policies_do_not_cross_contaminate():
    strict = resolve_safety_policy("strict")
    default = safety_config._policy_from_profile(
        "internal-default",
        safety_config.load_safety_profile("default"),
    )

    def validate(policy):
        return SQLSafetyValidator(policy=policy).validate("SELECT pg_sleep(1)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        strict_result, default_result = list(pool.map(validate, (strict, default)))

    assert strict_result["is_safe"] is False
    assert default_result["is_safe"] is True
    assert strict_result["policy_version"] == strict.policy_version
    assert default_result["policy_version"] == default.policy_version


def test_cache_and_runtime_stages_keep_admitted_policy(monkeypatch):
    from custom_tools.text_to_sql import core
    from custom_tools.text_to_sql.core import _sql_generation_api

    policy = resolve_safety_policy("strict")
    monkeypatch.setattr(
        safety_config,
        "load_safety_profile",
        lambda *args, **kwargs: pytest.fail("admitted policy must not reload config"),
    )
    monkeypatch.setattr(
        _sql_generation_api,
        "_run_llm_safety_audit_with_timeout",
        lambda *args, **kwargs: {"issues": []},
    )

    safety = core.sql_safety_check("SELECT 1", safety_policy=policy)
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    explain = core.sql_explain("SELECT 1", dsn="", safety_policy=policy)
    execution = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="",
        dry_run_only=True,
        safety_policy=policy,
    )

    for result in (safety, explain, execution):
        assert result["profile_name"] == policy.profile_name
        assert result["policy_version"] == policy.policy_version


def test_generation_and_finalizer_forward_same_policy(monkeypatch):
    from custom_tools.text_to_sql import core
    from custom_tools.text_to_sql.core import _sql_generation_api, _terminal

    policy = resolve_safety_policy("strict")
    captured = []

    class Generator:
        def generate_sql(self, *args, safety_policy=None, **kwargs):
            captured.append(("generation", safety_policy))
            return {
                "sql_query": "SELECT 1",
                "profile_name": safety_policy.profile_name,
                "policy_version": safety_policy.policy_version,
            }

    generated = _sql_generation_api.sql_generation_plugin(
        "{}",
        "one",
        dsn="sqlite:///unused.db",
        safety_policy=policy,
        sql_generator=Generator(),
    )

    def executor(sql_query, row_limit=None, dsn=None, **kwargs):
        captured.append(("execution", kwargs.get("safety_policy")))
        return {
            "success": True,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": sql_query,
            "applied_row_limit": row_limit,
            "profile_name": policy.profile_name,
            "policy_version": policy.policy_version,
        }

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("dry-run must not persist SQL"),
    )
    terminal = _terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "sqlite:///unused.db",
        10,
        True,
        "session-1",
        "run-1",
        safety_policy=policy,
    )

    assert generated["policy_version"] == policy.policy_version
    assert terminal["execution"]["policy_version"] == policy.policy_version
    assert captured == [("generation", policy), ("execution", policy)]


class _PolicyBoundValidator:
    """Stub validator bound to a specific policy, no LLM/network side effects.

    Mirrors ``SQLSafetyValidator.validate()``'s real contract: the result
    reports ``profile_name``/``policy_version`` from ``self.policy``.
    """

    def __init__(self, policy: TextToSqlSafetyPolicy) -> None:
        self.policy = policy

    def validate(self, sql_query: str, dsn=None):
        return {
            "is_safe": True,
            "issues": [],
            "profile_name": self.policy.profile_name,
            "policy_version": self.policy.policy_version,
        }


def test_validator_and_executor_policy_resolution_cannot_diverge(monkeypatch):
    """T14: sql_safety_check и secure_db_executor резолвят policy через один

    и тот же core._request_safety_policy — если валидатор привязан к policy,
    отличной от startup-дефолта, обе точки входа обязаны увидеть ИМЕННО её,
    а не разойтись на дублированной логике резолюции (single source of truth).
    """
    from custom_tools.text_to_sql.core import _db_exec, _sql_generation_api

    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **kwargs: '{"issues": []}',
    )

    custom_policy = resolve_safety_policy("strict")
    validator = _PolicyBoundValidator(custom_policy)

    safety_result = _sql_generation_api.sql_safety_check(
        "SELECT 1", sql_validator=validator
    )
    executor_result = _db_exec.secure_db_executor(
        "SELECT 1",
        dry_run_only=True,
        sql_validator=validator,
    )

    assert safety_result["policy_version"] == custom_policy.policy_version
    assert executor_result["policy_version"] == custom_policy.policy_version
    assert safety_result["policy_version"] == executor_result["policy_version"]
    assert (
        safety_result["profile_name"]
        == executor_result["profile_name"]
        == custom_policy.profile_name
    )
