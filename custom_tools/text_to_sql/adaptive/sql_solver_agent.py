"""Isolated one-turn model adapter for transient SQL-solver proposals."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import ValidationError

from workflow.deadline import DeadlineBudget, execute_step_attempt

from .models import NonEmptyText, StrictModel

if TYPE_CHECKING:
    from .solver_protocol import SolverProposalV1


SQL_SOLVER_AGENT_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "agent_profiles" / "sql_solver_agent.yaml"
)


class SqlSolverProfileError(ValueError):
    """The dedicated disabled SQL-solver profile is invalid."""


class SqlSolverModelResponseError(TypeError):
    """The SQL-solver model returned something other than JSON text or bytes."""


class SqlSolverAgentProfile(StrictModel):
    """Static metadata for one SQL-solver model turn."""

    enable: Literal[False]
    profile_version: Literal[1]
    profile_kind: Literal["sql_solver_one_turn"]
    model: NonEmptyText
    description: NonEmptyText
    instructions: NonEmptyText


class SqlSolverProposalModel(Protocol):
    """Minimal async provider boundary: one prompt and one raw response."""

    async def __call__(self, prompt: str, /) -> str | bytes: ...


def load_sql_solver_agent_profile(
    path: str | Path = SQL_SOLVER_AGENT_PROFILE_PATH,
) -> SqlSolverAgentProfile:
    """Load only the disabled profile without touching agent runtime."""

    try:
        import yaml

        with Path(path).open(encoding="utf-8") as stream:
            raw_profile = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise SqlSolverProfileError("cannot load SQL-solver profile") from error

    if not isinstance(raw_profile, dict):
        raise SqlSolverProfileError("SQL-solver profile must be a mapping")

    try:
        profile = SqlSolverAgentProfile.model_validate(raw_profile)
    except ValidationError as error:
        raise SqlSolverProfileError("SQL-solver profile is invalid") from error

    if profile.enable is not False:
        raise SqlSolverProfileError(
            "SQL-solver profile must stay disabled for the legacy agent loader"
        )
    if profile.profile_version != 1:
        raise SqlSolverProfileError("unsupported SQL-solver profile version")
    if profile.profile_kind != "sql_solver_one_turn":
        raise SqlSolverProfileError("unsupported SQL-solver profile kind")
    return profile


def build_sql_solver_prompt(
    profile: SqlSolverAgentProfile,
    *,
    task: str,
    solver_context: str,
) -> str:
    """Build a canonical JSON envelope for untrusted caller input."""

    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(solver_context, str):
        raise TypeError("solver_context must be a string")
    return json.dumps(
        {
            "input": {"solver_context": solver_context, "task": task},
            "instructions": profile.instructions,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SqlSolverProposalAdapter:
    """Perform one deadline-bounded async model call and parse its proposal."""

    def __init__(
        self,
        profile: SqlSolverAgentProfile,
        model: SqlSolverProposalModel,
    ) -> None:
        self._profile = profile
        self._model = model

    async def propose(
        self,
        *,
        task: str,
        solver_context: str,
        deadline: DeadlineBudget,
    ) -> SolverProposalV1:
        """Return one parsed proposal without retrying, executing, or persisting it."""

        await asyncio.sleep(0)
        if not isinstance(deadline, DeadlineBudget):
            raise TypeError("deadline must be a DeadlineBudget")
        if not _is_async_callable(self._model):
            raise TypeError("SQL-solver model must be an async callable")
        prompt = build_sql_solver_prompt(
            self._profile,
            task=task,
            solver_context=solver_context,
        )

        async def call_model(_: object) -> str | bytes:
            return await self._model(prompt)

        response = await execute_step_attempt(
            "sql_solver_agent",
            call_model,
            None,
            attempt_timeout=None,
            deadline=deadline,
        )
        await asyncio.sleep(0)
        deadline.require_remaining("SQL-solver response parsing")
        if type(response) not in (bytes, str):
            raise SqlSolverModelResponseError(
                "SQL-solver model response must be bytes or str"
            )

        from .solver_protocol import MAX_SOLVER_PROPOSAL_BYTES, parse_solver_proposal

        response_size = (
            len(response) if type(response) is bytes else len(response.encode("utf-8"))
        )
        if response_size <= MAX_SOLVER_PROPOSAL_BYTES and not (
            type(response) is bytes and response.startswith(b"\xef\xbb\xbf")
            or type(response) is str and response.startswith("\ufeff")
        ):
            try:
                top_level = json.loads(
                    response,
                    object_pairs_hook=lambda pairs: pairs,
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                if (
                    type(top_level) is list
                    and len(top_level) == 1
                    and type(top_level[0]) is tuple
                    and top_level[0][0] == "answer"
                    and type(top_level[0][1]) is str
                ):
                    response = top_level[0][1]
        return parse_solver_proposal(response)


def _is_async_callable(model: object) -> bool:
    return inspect.iscoroutinefunction(model) or inspect.iscoroutinefunction(
        getattr(model, "__call__", None)
    )
