"""W1-1.1: registry «шаг пайплайна → класс модели» (``llm_models.yaml::step_models``).

Contract:
  * ``step_model_name(step)`` (``custom_tools/text_to_sql/llm_models_config.py``)
    resolves the alias for a pipeline step; the alias must be a valid key
    of ``agent_command.py::_MODEL_CONFIGS``.
  * ``schema_research``/``sql_solver`` are mirrors of the respective
    ``agent_profiles/*.yaml`` — the agent profile is the source of truth,
    the registry only mirrors it for discoverability/documentation. This
    module asserts the mirror stays in sync (contract test).
  * ``experiment`` defines every key ``default`` does, downgrading exactly
    three steps (research_stop_review/result_review/safety_llm_audit) — see
    the yaml header comment and ``test_experiment_step_models_downgrades_
    exactly_three_steps`` below.

Heavier integration checks for two specific routed steps live next to their
natural fixtures rather than being duplicated here:
  * ``result_review`` end-to-end registry override:
    ``tests/test_text_to_sql_result_validation_runtime.py::
    test_result_review_runtime_reads_model_from_step_models_registry``
    (regression companion:
    ``test_result_review_runtime_forwards_remaining_deadline_and_skips_expired_call``,
    which stays green asserting the *default* alias ``model_hard``).
  * ``research_stop_review`` end-to-end registry override on the targeted
    re-entry path:
    ``tests/test_text_to_sql_adaptive_solver_reentry_runtime.py::
    test_reentry_continuation_stop_review_model_comes_from_step_models_registry``
    (also asserts ``stable_schema_research_model_identity`` — a separate,
    ``profile.model``-based ledger-identity concern — stays unaffected by
    the stop-review alias override).
"""

from __future__ import annotations

import pytest

import agent_command
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.sql_solver_agent import (
    load_sql_solver_agent_profile,
)
from custom_tools.text_to_sql.llm_models_config import (
    get_active_profile,
    step_model_name,
)

_STEP_KEYS = (
    "schema_research",
    "sql_solver",
    "research_stop_review",
    "result_review",
    "safety_llm_audit",
    "nlu_query_understanding",
    "nlu_completeness",
    "nlu_intent",
    "schema_enricher",
    "schema_linking",
    "sql_generation",
)


@pytest.mark.parametrize("step", _STEP_KEYS)
def test_step_model_name_resolves_to_a_known_model_class(step: str) -> None:
    """Every registered step must resolve to a real ``_MODEL_CONFIGS`` alias.

    ``name in agent_command.model_mapping`` uses ``_ModelMapping.__contains__``,
    which only checks membership in ``_MODEL_CONFIGS`` — it must NOT
    construct a real model client (``model_mapping[name]`` would, via
    ``_get_model``/``_create_model``), so this stays a pure config-shape
    check with no network-client side effect.
    """
    name = step_model_name(step)
    assert isinstance(name, str) and name
    assert name in agent_command.model_mapping


def test_step_model_name_raises_key_error_for_unknown_step() -> None:
    with pytest.raises(KeyError) as excinfo:
        step_model_name("not_a_real_step")

    message = str(excinfo.value)
    assert "step_models" in message
    assert "not_a_real_step" in message
    # The error enumerates the available keys (see LLMModelsProfile.get).
    for step in _STEP_KEYS:
        assert step in message


def test_schema_research_step_mirrors_agent_profile() -> None:
    """``step_models.schema_research`` is a mirror, not a routing source.

    The runtime for the primary schema-research decision model reads
    ``profile.model`` directly (``workflow/text_to_sql_typed_research.py``,
    ``_research_model(profile.model, ...)`` — unchanged by W1-1.1). This
    test only guards that the registry mirror does not silently drift from
    the agent profile, which is the actual source of truth.
    """
    profile = load_schema_research_agent_profile()
    assert profile.model == step_model_name("schema_research")


def test_sql_solver_step_mirrors_agent_profile() -> None:
    profile = load_sql_solver_agent_profile()
    assert profile.model == step_model_name("sql_solver")


def test_experiment_step_models_resolves_every_key() -> None:
    """``experiment`` must define every key ``default`` does -- a step
    silently missing from ``step_models`` would raise ``KeyError`` at
    runtime (``step_model_name``) the first time that step is reached."""

    default_profile = get_active_profile("default")
    experiment_profile = get_active_profile("experiment")

    assert set(experiment_profile.sections["step_models"]) == set(
        default_profile.sections["step_models"]
    )


def test_experiment_step_models_downgrades_exactly_three_steps() -> None:
    """Only research_stop_review/result_review/safety_llm_audit move off the
    ``default`` alias in ``experiment``. Every other step -- including
    ``nlu_intent``, which is deliberately NOT downgraded because its
    heuristic fallback only fires with ``TEXT_TO_SQL_NLU_ALLOW_FALLBACKS=1``
    (default off, otherwise a plain ``RuntimeError`` -- see the yaml header
    comment and ``custom_tools/text_to_sql/nlu.py``) -- stays identical to
    ``default``."""

    default_profile = get_active_profile("default")
    experiment_profile = get_active_profile("experiment")

    default_steps = default_profile.sections["step_models"]
    experiment_steps = experiment_profile.sections["step_models"]

    changed = {
        step for step in default_steps if default_steps[step] != experiment_steps[step]
    }
    assert changed == {"research_stop_review", "result_review", "safety_llm_audit"}
