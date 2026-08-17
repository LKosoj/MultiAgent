"""Versioned adaptive research policy configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ValidationError, model_validator

from custom_tools.text_to_sql._yaml_config_loader import YamlConfigLoader

from .models import (
    BudgetState,
    StrictModel,
)
from .model_budget import (
    ModelBudgetLimits,
    ModelBudgetState,
)

from ._policy_common import (
    MAX_ACTIONS,
    MAX_DB_PROBES,
    MAX_DB_PROBE_MS,
    MAX_INLINE_BYTES,
    MAX_MODEL_CALLS_V2,
    MAX_MODEL_DECISIONS,
    MAX_MODEL_INPUT_TOKENS_PER_CALL,
    MAX_MODEL_OUTPUT_TOKENS_PER_CALL,
    MAX_MODEL_TOKENS,
    MAX_MODEL_TOTAL_TOKENS,
    MAX_RETURNED_ROWS,
    MAX_SAMPLE_ROWS,
    MAX_WALL_CLOCK_SECONDS,
    MODEL_BUDGET_POLICY_VERSION,
    BudgetExhaustedError,
    POLICY_VERSION,
    PositiveInt,
    NonNegativeInt,
    ResearchPolicyConfigError,
    _CONFIG_ENV_VAR,
    _DEFAULT_CONFIG_PATH,
    _revalidate,
)


class WallClockBudget(StrictModel):
    wall_clock_seconds: PositiveInt


class ResourceBudget(StrictModel):
    model_tokens: NonNegativeInt
    db_probe_ms: PositiveInt


class OperationCountBudget(StrictModel):
    actions: PositiveInt
    model_decisions: PositiveInt
    db_probes: PositiveInt


class ResultVolumeBudget(StrictModel):
    returned_rows: PositiveInt
    inline_bytes: PositiveInt


class PerActionBudget(StrictModel):
    sample_rows: PositiveInt


class AdaptivePolicyConfig(StrictModel):
    policy_version: Literal[1, 2]
    wall_clock: WallClockBudget
    resource_limits: ResourceBudget
    operation_counts: OperationCountBudget
    result_volume: ResultVolumeBudget
    per_action: PerActionBudget
    model_budget: ModelBudgetLimits | None = None

    @model_validator(mode="after")
    def prevent_envelope_widening(self) -> AdaptivePolicyConfig:
        configured = {
            "wall_clock.wall_clock_seconds": (
                self.wall_clock.wall_clock_seconds,
                MAX_WALL_CLOCK_SECONDS,
            ),
            "resource_limits.model_tokens": (
                self.resource_limits.model_tokens,
                MAX_MODEL_TOKENS
                if self.policy_version == POLICY_VERSION
                else MAX_MODEL_TOTAL_TOKENS,
            ),
            "resource_limits.db_probe_ms": (
                self.resource_limits.db_probe_ms,
                MAX_DB_PROBE_MS,
            ),
            "operation_counts.actions": (self.operation_counts.actions, MAX_ACTIONS),
            "operation_counts.model_decisions": (
                self.operation_counts.model_decisions,
                MAX_MODEL_DECISIONS,
            ),
            "operation_counts.db_probes": (
                self.operation_counts.db_probes,
                MAX_DB_PROBES,
            ),
            "result_volume.returned_rows": (
                self.result_volume.returned_rows,
                MAX_RETURNED_ROWS,
            ),
            "result_volume.inline_bytes": (
                self.result_volume.inline_bytes,
                MAX_INLINE_BYTES,
            ),
            "per_action.sample_rows": (self.per_action.sample_rows, MAX_SAMPLE_ROWS),
        }
        for field_name, (value, maximum) in configured.items():
            if value > maximum:
                raise ValueError(
                    f"{field_name} cannot widen the policy safety envelope"
                )
        if self.policy_version == POLICY_VERSION:
            if self.model_budget is not None:
                raise ValueError("model_budget is only supported by policy v2")
            if self.resource_limits.model_tokens != MAX_MODEL_TOKENS:
                raise ValueError("policy v1 must keep model_tokens disabled")
            return self
        if self.model_budget is None:
            raise ValueError("policy v2 requires model_budget")
        if self.model_budget.model_calls > MAX_MODEL_CALLS_V2:
            raise ValueError(
                "model_budget.model_calls cannot widen the policy safety envelope"
            )
        if self.model_budget.input_tokens_per_call > MAX_MODEL_INPUT_TOKENS_PER_CALL:
            raise ValueError(
                "model_budget.input_tokens_per_call cannot widen the policy safety envelope"
            )
        if self.model_budget.output_tokens_per_call > MAX_MODEL_OUTPUT_TOKENS_PER_CALL:
            raise ValueError(
                "model_budget.output_tokens_per_call cannot widen the policy safety envelope"
            )
        if self.model_budget.total_tokens > MAX_MODEL_TOTAL_TOKENS:
            raise ValueError(
                "model_budget.total_tokens cannot widen the policy safety envelope"
            )
        if self.resource_limits.model_tokens != self.model_budget.total_tokens:
            raise ValueError(
                "resource_limits.model_tokens must equal model_budget.total_tokens"
            )
        if self.operation_counts.model_decisions != self.model_budget.model_calls:
            raise ValueError(
                "operation_counts.model_decisions must equal model_budget.model_calls"
            )
        return self

    @property
    def wall_clock_ms(self) -> int:
        return self.wall_clock.wall_clock_seconds * 1_000


def load_adaptive_policy_config() -> AdaptivePolicyConfig:
    """Load the immutable adaptive policy selected by config path or environment."""

    return _CONFIG_LOADER.load()


def reset_adaptive_policy_config_cache() -> None:
    """Forget cached policy snapshots after a deliberate config-path change."""

    _CONFIG_LOADER.reset_cache()


def initial_budget_state(config: AdaptivePolicyConfig) -> BudgetState:
    """Create the only v1 initial budget, entirely from versioned config."""

    checked = _revalidate(config, AdaptivePolicyConfig, "policy config")
    return BudgetState(
        initial_wall_clock_ms=checked.wall_clock_ms,
        used_wall_clock_ms=0,
        remaining_wall_clock_ms=checked.wall_clock_ms,
        initial_model_calls=checked.operation_counts.model_decisions,
        used_model_calls=0,
        remaining_model_calls=checked.operation_counts.model_decisions,
        initial_model_tokens=checked.resource_limits.model_tokens,
        used_model_tokens=0,
        remaining_model_tokens=checked.resource_limits.model_tokens,
        initial_db_probe_ms=checked.resource_limits.db_probe_ms,
        used_db_probe_ms=0,
        remaining_db_probe_ms=checked.resource_limits.db_probe_ms,
        initial_rows=checked.result_volume.returned_rows,
        used_rows=0,
        remaining_rows=checked.result_volume.returned_rows,
        initial_bytes=checked.result_volume.inline_bytes,
        used_bytes=0,
        remaining_bytes=checked.result_volume.inline_bytes,
    )


def initial_model_budget_state(config: AdaptivePolicyConfig) -> ModelBudgetState:
    """Create the separate v2 model-budget chain from immutable policy limits."""

    checked = _revalidate(config, AdaptivePolicyConfig, "policy config")
    limits = _require_model_budget_v2(checked)
    return ModelBudgetState(
        initial_model_calls=limits.model_calls,
        used_model_calls=0,
        remaining_model_calls=limits.model_calls,
        initial_input_tokens=limits.model_calls * limits.input_tokens_per_call,
        used_input_tokens=0,
        remaining_input_tokens=limits.model_calls * limits.input_tokens_per_call,
        initial_output_tokens=limits.model_calls * limits.output_tokens_per_call,
        used_output_tokens=0,
        remaining_output_tokens=limits.model_calls * limits.output_tokens_per_call,
        initial_total_tokens=limits.total_tokens,
        used_total_tokens=0,
        remaining_total_tokens=limits.total_tokens,
    )


def _parse_config(raw: dict[str, object], source_path: str) -> AdaptivePolicyConfig:
    try:
        return AdaptivePolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ResearchPolicyConfigError(
            f"adaptive policy at {source_path} does not satisfy closed v1 schema"
        ) from exc


def _not_found_message(path: Path, env_var: str) -> str:
    return f"adaptive policy config not found at {path}; override with {env_var}"


def _mapping_error_message(path: Path) -> str:
    return f"adaptive policy at {path} must contain a mapping"


def _require_model_budget_v2(config: AdaptivePolicyConfig) -> ModelBudgetLimits:
    if (
        config.policy_version != MODEL_BUDGET_POLICY_VERSION
        or config.model_budget is None
    ):
        raise BudgetExhaustedError("model budget is disabled outside policy v2")
    return config.model_budget


_CONFIG_LOADER = YamlConfigLoader[AdaptivePolicyConfig](
    env_path_var=_CONFIG_ENV_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=_parse_config,
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)
