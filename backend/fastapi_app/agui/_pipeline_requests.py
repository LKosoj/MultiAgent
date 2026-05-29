"""Pydantic-модели для валидации ``parameters`` workflow-пайплайнов (W9-A3).

Расширение реестра ``PIPELINE_VALIDATORS`` из ``_t2s_requests`` на все
pipelines в ``workflow_pipelines/*.yaml``. Каждый pipeline получает
минимальную модель, отражающую секцию ``inputs:`` YAML.

Правила (см. AGENTS.md / fail-fast):
* Поля с ``default = ""`` в YAML — *required* (``min_length=1``); пустой
  default означает «обязательный input, который пользователь должен дать».
* Поля с непустым default — optional, default из YAML переносится в модель.
* Все типы явные (``str``/``int``/``bool``). ``extra="ignore"`` — терпим
  поля, которые pipeline-движок умеет принимать сверх документации, но не
  допускаем silent rename'ов (это была бы регрессия контракта).

Source of truth — секции ``inputs:`` YAML; при добавлении нового pipeline
или поля YAML обязательно обновить модель здесь + добавить запись в
``PIPELINE_VALIDATORS``.

ПРЕДЕЛЫ применимости:
* ``workflows.start`` для pipeline БЕЗ записи в реестре остаётся без
  валидации parameters. Это известная брешь — см. TODO в
  ``service.py::workflows.start``. Закрывать её должен whitelist-режим,
  это отдельная задача (требует review всех call-sites streamlit/UI).
* Модели НЕ покрывают session_id/client_id — это infra-поля, передаются
  через top-level payload ``workflows.start``, не через ``parameters``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _BasePipelineRequest(BaseModel):
    """Базовая модель для pipeline-валидаторов.

    ``extra="ignore"``: pipeline-движок (WorkflowManager) исторически
    принимает дополнительные поля (например, ``session_id`` пробрасывается
    через ``parameters`` в некоторых вызовах). Игнорирование лишних
    полей — компромисс между fail-fast валидацией и backward-compat.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def _require_non_empty_string(value: Any, field_name: str) -> str:
    """Универсальный валидатор для обязательного string-поля.

    Используется в моделях ниже через ``field_validator(mode="before")``.
    Принимает None как "не передан" -> raise. Пустая строка / whitespace —
    тоже отказ (fail-fast, не silent fallback на default).
    """
    if value is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} is required")
    return stripped


def _pipeline_inputs(workflow_name: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "workflow_pipelines" / f"{workflow_name}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{workflow_name}.yaml not found for pipeline validator") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{workflow_name}.yaml must contain a mapping")
    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError(f"{workflow_name}.yaml inputs must contain a mapping")
    return inputs


def _pipeline_input_default(workflow_name: str, input_name: str) -> Any:
    inputs = _pipeline_inputs(workflow_name)
    if input_name not in inputs:
        raise RuntimeError(f"{workflow_name}.yaml inputs.{input_name} default is required")
    return inputs[input_name]


def _pipeline_default_str(workflow_name: str, input_name: str, *, allow_empty: bool = False) -> str:
    value = _pipeline_input_default(workflow_name, input_name)
    if not isinstance(value, str):
        raise RuntimeError(f"{workflow_name}.yaml inputs.{input_name} must be a string")
    if not allow_empty and not value.strip():
        raise RuntimeError(f"{workflow_name}.yaml inputs.{input_name} must be a non-empty string")
    return value


def _pipeline_default_int(workflow_name: str, input_name: str) -> int:
    value = _pipeline_input_default(workflow_name, input_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{workflow_name}.yaml inputs.{input_name} must be an integer")
    return value


def _pipeline_default_bool(workflow_name: str, input_name: str) -> bool:
    value = _pipeline_input_default(workflow_name, input_name)
    if not isinstance(value, bool):
        raise RuntimeError(f"{workflow_name}.yaml inputs.{input_name} must be a boolean")
    return value


def _storybook_default_str(input_name: str, *, allow_empty: bool = False) -> str:
    return _pipeline_default_str("storybook_pipeline", input_name, allow_empty=allow_empty)


def _storybook_default_int(input_name: str) -> int:
    return _pipeline_default_int("storybook_pipeline", input_name)


def _storybook_default_bool(input_name: str) -> bool:
    return _pipeline_default_bool("storybook_pipeline", input_name)


class ArchitectureReviewRequest(_BasePipelineRequest):
    """architecture_review.yaml: ``project_path`` обязателен."""

    project_path: str = Field(..., min_length=1)

    @field_validator("project_path", mode="before")
    @classmethod
    def _v_project_path(cls, v: Any) -> str:
        return _require_non_empty_string(v, "project_path")


class ContentCreationRequest(_BasePipelineRequest):
    """content_creation.yaml: ``topic`` обязателен."""

    topic: str = Field(..., min_length=1)

    @field_validator("topic", mode="before")
    @classmethod
    def _v_topic(cls, v: Any) -> str:
        return _require_non_empty_string(v, "topic")


class DataAnalysisRequest(_BasePipelineRequest):
    """data_analysis.yaml: ``analysis_request`` обязателен."""

    analysis_request: str = Field(..., min_length=1)

    @field_validator("analysis_request", mode="before")
    @classmethod
    def _v_analysis_request(cls, v: Any) -> str:
        return _require_non_empty_string(v, "analysis_request")


class ManagerTeamDemoRequest(_BasePipelineRequest):
    """manager_team_demo.yaml: ``topic`` обязателен."""

    topic: str = Field(..., min_length=1)

    @field_validator("topic", mode="before")
    @classmethod
    def _v_topic(cls, v: Any) -> str:
        return _require_non_empty_string(v, "topic")


class RubleAnalysisToolRequest(_BasePipelineRequest):
    """ruble_analysis_tool.yaml: ``end_date`` опционально (default ``"today"``)."""

    end_date: str = Field(default="today", min_length=1)

    @field_validator("end_date", mode="before")
    @classmethod
    def _v_end_date(cls, v: Any) -> str:
        if v is None:
            return "today"
        if not isinstance(v, str):
            raise ValueError("end_date must be a string")
        stripped = v.strip()
        return stripped or "today"


class SimpleResearchRequest(_BasePipelineRequest):
    """simple_research.yaml: ``topic`` обязателен."""

    topic: str = Field(..., min_length=1)

    @field_validator("topic", mode="before")
    @classmethod
    def _v_topic(cls, v: Any) -> str:
        return _require_non_empty_string(v, "topic")


class StepResultsDemoRequest(_BasePipelineRequest):
    """step_results_demo.yaml: ``topic`` optional с default из YAML."""

    topic: str = Field(default="Искусственный интеллект в 2024", min_length=1)
    session_id: str = Field(default="")

    @field_validator("topic", mode="before")
    @classmethod
    def _v_topic(cls, v: Any) -> str:
        if v is None:
            return "Искусственный интеллект в 2024"
        if not isinstance(v, str):
            raise ValueError("topic must be a string")
        return v.strip() or "Искусственный интеллект в 2024"

    @field_validator("session_id", mode="before")
    @classmethod
    def _v_session_id(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("session_id must be a string")
        return v


class StorybookPipelineRequest(_BasePipelineRequest):
    """storybook_pipeline.yaml: ``task`` обязателен, прочие defaults берутся из YAML."""

    task: str = Field(..., min_length=1)
    project_id: str = Field(
        default_factory=lambda: _storybook_default_str("project_id"),
        min_length=1,
        validate_default=True,
    )
    pages_min: int = Field(
        default_factory=lambda: _storybook_default_int("pages_min"),
        ge=1,
        validate_default=True,
    )
    pages_max: int = Field(
        default_factory=lambda: _storybook_default_int("pages_max"),
        ge=1,
        validate_default=True,
    )
    words_per_page_min: int = Field(
        default_factory=lambda: _storybook_default_int("words_per_page_min"),
        ge=1,
        validate_default=True,
    )
    words_per_page_max: int = Field(
        default_factory=lambda: _storybook_default_int("words_per_page_max"),
        ge=1,
        validate_default=True,
    )
    generate_screenplay: bool = Field(
        default_factory=lambda: _storybook_default_bool("generate_screenplay"),
        validate_default=True,
    )
    generate_end_shots: bool = Field(
        default_factory=lambda: _storybook_default_bool("generate_end_shots"),
        validate_default=True,
    )
    language: str = Field(
        default_factory=lambda: _storybook_default_str("language"),
        min_length=1,
        validate_default=True,
    )
    screenplay_time: int = Field(
        default_factory=lambda: _storybook_default_int("screenplay_time"),
        ge=1,
        validate_default=True,
    )
    force_update_prompts: bool = Field(
        default_factory=lambda: _storybook_default_bool("force_update_prompts"),
        validate_default=True,
    )
    skip_prompt_enhancement: bool = Field(
        default_factory=lambda: _storybook_default_bool("skip_prompt_enhancement"),
        validate_default=True,
    )

    @field_validator("task", mode="before")
    @classmethod
    def _v_task(cls, v: Any) -> str:
        return _require_non_empty_string(v, "task")

    @field_validator("pages_min", "pages_max", mode="before")
    @classmethod
    def _v_pages_int(cls, v: Any) -> int:
        if isinstance(v, bool):
            raise ValueError("pages_min/pages_max must be an integer")
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped.isdigit():
                raise ValueError("pages_min/pages_max must be an integer")
            return int(stripped)
        raise ValueError("pages_min/pages_max must be an integer")


class ToolDemoRequest(_BasePipelineRequest):
    """tool_demo.yaml: ``image_prompt`` и ``research_topic`` обязательны."""

    image_prompt: str = Field(..., min_length=1)
    research_topic: str = Field(..., min_length=1)
    session_id: str = Field(default="")

    @field_validator("image_prompt", mode="before")
    @classmethod
    def _v_image_prompt(cls, v: Any) -> str:
        return _require_non_empty_string(v, "image_prompt")

    @field_validator("research_topic", mode="before")
    @classmethod
    def _v_research_topic(cls, v: Any) -> str:
        return _require_non_empty_string(v, "research_topic")

    @field_validator("session_id", mode="before")
    @classmethod
    def _v_session_id(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("session_id must be a string")
        return v


__all__ = [
    "ArchitectureReviewRequest",
    "ContentCreationRequest",
    "DataAnalysisRequest",
    "ManagerTeamDemoRequest",
    "RubleAnalysisToolRequest",
    "SimpleResearchRequest",
    "StepResultsDemoRequest",
    "StorybookPipelineRequest",
    "ToolDemoRequest",
]
