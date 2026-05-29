"""Pydantic-модели запросов для AG-UI service action ``presets.text_to_sql.generate``.

EPIC 7.23: единый source of truth для валидации payload — Pydantic-модель,
которая закрывает все ранее ручные проверки (``_validate_text_to_sql_max_rows``,
``_validate_text_to_sql_safety_level``, ``_coerce_bool``, ``_coerce_strict_bool``).

Контракт совпадает с задокументированным в ``doc/AG_UI_SERVICE_ACTIONS.md``.
Поля и defaults *должны* совпадать с ``backend/fastapi_app/agui/service.py``
секцией ``presets.text_to_sql.generate``: модель — снимок этой секции.

Изолирован в отдельном файле, чтобы не раздувать 3700+ строк service.py и
дать другим service actions точку отсчёта для миграции.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Импортируем общий strict-bool coercer (см. EPIC 7.22) — единая семантика
# с workflow.enhanced_engine._should_fallback_to_legacy.
from custom_tools.text_to_sql.utils import coerce_strict_bool


def _get_runtime_limits() -> tuple[int, int, frozenset[str]]:
    """Читает runtime-лимиты из service.py — единый source of truth.

    Lazy-import: ``service.py`` импортирует этот модуль, поэтому нельзя
    делать top-level импорт (circular import). Значения статические,
    но runtime-resolve упрощает их monkeypatch в тестах.
    """
    from . import service as _svc
    return (
        _svc._TEXT_TO_SQL_MAX_ROWS_MIN,
        _svc._TEXT_TO_SQL_MAX_ROWS_MAX,
        frozenset(_svc._TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS),
    )


# Soft-bool: для backwards-compat (ранее всё через _coerce_bool). Принимает любые
# truthy/falsy через bool(...), плюс canonical-строки.
def _coerce_soft_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


class TextToSqlGenerateRequest(BaseModel):
    """Валидированный payload для ``presets.text_to_sql.generate``.

    ВНИМАНИЕ: модель ожидает уже зарезолвленный ``dsn`` — резолвинг
    ``db_config:<name>`` остаётся в ``service.py`` (там есть side-effects:
    чтение секретов, fallback на legacy config). Это снижает coupling
    модели с infra-слоем и упрощает unit-тесты.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(..., min_length=1, description="NL-запрос пользователя")
    dsn: str = Field(..., min_length=1, description="Резолвленный DSN")
    max_rows: int = Field(default=100)
    safety_level: str = Field(default="strict")
    include_explanation: bool = Field(default=True)
    validate_schema: bool = Field(default=True)
    dry_run_only: bool = Field(default=False)
    use_schema_suggestions: bool = Field(default=True)
    allow_enhanced_fallback: bool = Field(default=False)
    workflow_name: str = Field(default="text_to_sql_pipeline", min_length=1)
    session_id: Optional[str] = Field(default=None)
    client_id: Optional[str] = Field(default=None)
    use_enhanced: bool = Field(default=True)
    enable_telemetry: bool = Field(default=False)

    # === Валидаторы для строгих/мягких полей =================================
    @field_validator("query", mode="before")
    @classmethod
    def _validate_query(cls, v: Any) -> str:
        if v is None:
            raise ValueError("query is required")
        if not isinstance(v, str):
            raise ValueError("query must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("query is required")
        return stripped

    @field_validator("dsn", mode="before")
    @classmethod
    def _validate_dsn(cls, v: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dsn is required")
        return v

    @field_validator("max_rows", mode="before")
    @classmethod
    def _validate_max_rows(cls, v: Any) -> int:
        min_v, max_v, _ = _get_runtime_limits()
        if isinstance(v, bool):
            raise ValueError("max_rows must be an integer")
        if isinstance(v, int):
            value = v
        elif isinstance(v, float):
            if not v.is_integer():
                raise ValueError("max_rows must be an integer")
            value = int(v)
        elif isinstance(v, str):
            normalized = v.strip()
            if not normalized.isdigit():
                raise ValueError("max_rows must be an integer")
            value = int(normalized)
        else:
            raise ValueError("max_rows must be an integer")
        if value < min_v or value > max_v:
            raise ValueError(f"max_rows must be between {min_v} and {max_v}")
        return value

    @field_validator("safety_level", mode="before")
    @classmethod
    def _validate_safety_level(cls, v: Any) -> str:
        _, _, supported_set = _get_runtime_limits()
        normalized = str(v or "strict").strip().lower()
        if normalized not in supported_set:
            supported = ", ".join(sorted(supported_set))
            raise ValueError(f"safety_level must be one of: {supported}")
        return normalized

    @field_validator(
        "include_explanation",
        "validate_schema",
        "dry_run_only",
        "use_schema_suggestions",
        "use_enhanced",
        "enable_telemetry",
        mode="before",
    )
    @classmethod
    def _coerce_soft_bool_fields(cls, v: Any, info) -> bool:
        default = cls.model_fields[info.field_name].default
        return _coerce_soft_bool(v, default=bool(default))

    @field_validator("allow_enhanced_fallback", mode="before")
    @classmethod
    def _coerce_strict_bool_field(cls, v: Any) -> bool:
        return coerce_strict_bool(v, default=False, field_name="allow_enhanced_fallback")

    @field_validator("workflow_name", mode="before")
    @classmethod
    def _validate_workflow_name(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            v = "text_to_sql_pipeline"
        if not isinstance(v, str):
            raise ValueError("workflow_name must be a string")
        name = v.strip()
        # Fail-fast: пайплайн должен существовать на диске. Иначе ошибка
        # вылезла бы глубоко внутри WorkflowManager (после стартовых
        # сайд-эффектов: создания run_id, регистрации сессии и т.п.).
        from pathlib import Path as _P
        pipeline_dir = _P(__file__).resolve().parents[3] / "workflow_pipelines"
        if not (pipeline_dir / f"{name}.yaml").exists():
            raise ValueError(f"Pipeline '{name}' not found in workflow_pipelines/")
        return name

    @field_validator("session_id", "client_id", mode="before")
    @classmethod
    def _validate_optional_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("session_id/client_id must be a string")
        return v or None

    @model_validator(mode="after")
    def _validate_schema_mode(self) -> "TextToSqlGenerateRequest":
        if not self.use_schema_suggestions and self.validate_schema:
            raise ValueError(
                "use_schema_suggestions=false requires validate_schema=false "
                "or an explicit schema-producing path"
            )
        return self


def parse_text_to_sql_generate(payload: dict) -> TextToSqlGenerateRequest:
    """Валидирует payload и возвращает модель.

    Pydantic ``ValidationError`` транслируется в ``ValueError`` с понятным
    сообщением, чтобы AG-UI-dispatcher продолжал получать тот же тип
    исключения, что и до миграции на Pydantic (обратная совместимость
    контракта обработки ошибок).
    """
    try:
        return TextToSqlGenerateRequest.model_validate(payload)
    except ValidationError as exc:
        # Берём первое сообщение — оно содержит field name + ctx.
        errors = exc.errors()
        if errors:
            first = errors[0]
            # Pydantic выкладывает наше raise ValueError(msg) в ctx['error'].
            ctx_err = first.get("ctx", {}).get("error") if isinstance(first.get("ctx"), dict) else None
            msg = str(ctx_err) if ctx_err else first.get("msg") or "invalid payload"
        else:
            msg = "invalid payload"
        # Дополнительно прокидываем агрегированный список всех ошибок —
        # упрощает диагностику payload'а с несколькими невалидными полями.
        # При len(errors) == 1 суффикс был бы дубликатом основного сообщения.
        if len(errors) > 1:
            all_errs = "; ".join(err.get("msg", "unknown") for err in errors)
            if all_errs:
                msg = f"{msg} (all: {all_errs})"
        raise ValueError(msg) from exc


def parse_text_to_sql_pipeline_inputs(inputs: dict) -> TextToSqlGenerateRequest:
    """W1-T2: Валидирует ``inputs`` для ``workflows.start(text_to_sql_pipeline)``.

    Pipeline принимает уже зарезолвленный DSN — резолвинг ``db_config:<name>``
    выполняется в ``service.py`` до вызова валидатора (там же, где для
    ``presets.text_to_sql.generate``). Эта функция — тонкий wrapper, который
    закрывает контракт-обход workflows.start через единую Pydantic-модель.
    """
    return parse_text_to_sql_generate(inputs)


# === W1-T2 / W9-A3: реестр валидаторов pipeline_name -> Pydantic-модель ====
#
# Когда ``workflows.start`` приходит с ``workflow_name``, попадающим в реестр,
# inputs пропускаются через модель (single source of truth с preset action).
#
# W9-A3: реестр расширен на все pipelines в ``workflow_pipelines/*.yaml``.
# Каждая модель отражает секцию ``inputs:`` соответствующего YAML
# (см. ``_pipeline_requests.py``). Source of truth — YAML; при изменении
# inputs необходимо обновить и модель.
#
# SECURITY GAP (TODO): pipelines без записи в реестре остаются без
# валидации parameters. ``workflows.start`` для них принимает любой dict
# и пробрасывает в generic engine. Это известная брешь — закрывать её
# должен whitelist-режим (workflow_validator_required: true в settings),
# который потребует review всех call-sites (streamlit UI, AG-UI clients,
# tests, fixtures). По AG-UI контракту
# (см. ``doc/AG_UI_SERVICE_ACTIONS.md`` секция ``workflows.start``) код
# ``workflow_not_found`` уже отказывает в неизвестных pipelines, но не
# валидирует parameters; whitelist-валидацию вводить отдельной задачей.
from ._pipeline_requests import (
    ArchitectureReviewRequest,
    ContentCreationRequest,
    DataAnalysisRequest,
    ManagerTeamDemoRequest,
    RubleAnalysisToolRequest,
    SimpleResearchRequest,
    StepResultsDemoRequest,
    StorybookPipelineRequest,
    ToolDemoRequest,
)

PIPELINE_VALIDATORS: dict[str, type[BaseModel]] = {
    "text_to_sql_pipeline": TextToSqlGenerateRequest,
    "architecture_review": ArchitectureReviewRequest,
    "content_creation": ContentCreationRequest,
    "data_analysis": DataAnalysisRequest,
    "manager_team_demo": ManagerTeamDemoRequest,
    "ruble_analysis_tool": RubleAnalysisToolRequest,
    "simple_research": SimpleResearchRequest,
    "step_results_demo": StepResultsDemoRequest,
    "storybook_pipeline": StorybookPipelineRequest,
    "tool_demo": ToolDemoRequest,
}


__all__ = [
    "TextToSqlGenerateRequest",
    "parse_text_to_sql_generate",
    "parse_text_to_sql_pipeline_inputs",
    "PIPELINE_VALIDATORS",
]
