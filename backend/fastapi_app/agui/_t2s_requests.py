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

import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Импортируем общий strict-bool coercer (см. EPIC 7.22) — единая семантика
# с workflow.enhanced_engine._should_fallback_to_legacy.
from custom_tools.text_to_sql.utils import coerce_strict_bool

TEXT_TO_SQL_MAX_ROWS_MIN = 1
TEXT_TO_SQL_MAX_ROWS_MAX = 10000
TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS = frozenset({"strict"})
_RAW_DSN_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;\s])(?:database|dbname|driver|dsn|host|password|port|pwd|server|uid|user)\s*=",
    flags=re.IGNORECASE,
)
_RAW_DSN_SCHEMES = frozenset(
    {"duckdb", "file", "impala", "mysql", "postgres", "postgresql", "sapiq", "sqlite"}
)
_RAW_DSN_FILE_SUFFIXES = (".db", ".duckdb", ".sqlite", ".sqlite3")


def _looks_like_raw_dsn(value: str) -> bool:
    candidate = value.strip()
    normalized = candidate.casefold()
    scheme, separator, _ = normalized.partition(":")
    if "://" in normalized or (
        separator and scheme.split("+", 1)[0] in _RAW_DSN_SCHEMES
    ):
        return True
    if _RAW_DSN_ASSIGNMENT_RE.search(candidate):
        return True
    if normalized == ":memory:" or candidate.startswith(("/", "./", "../", "~/", "\\")):
        return True
    if (
        len(candidate) > 2
        and candidate[0].isalpha()
        and candidate[1] == ":"
        and candidate[2] in "/\\"
    ):
        return True
    path = normalized.split("?", 1)[0].split("#", 1)[0]
    return path.endswith(_RAW_DSN_FILE_SUFFIXES)


def _get_runtime_limits() -> tuple[int, int, frozenset[str]]:
    """Return the immutable request limits without importing service runtime."""
    return (
        TEXT_TO_SQL_MAX_ROWS_MIN,
        TEXT_TO_SQL_MAX_ROWS_MAX,
        TEXT_TO_SQL_SUPPORTED_SAFETY_LEVELS,
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

    Модель валидирует только форму выбора подключения. Резолвинг reference и
    principal-aware авторизация остаются в service-слое.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(..., min_length=1, description="NL-запрос пользователя")
    connection_ref: Optional[str] = Field(default=None, description="Opaque connection reference")
    dsn: Optional[str] = Field(default=None, min_length=1, description="Admin compatibility DSN")
    admin_raw_dsn_compat: bool = Field(default=False)
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
    idempotency_key: Optional[str] = Field(default=None)

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

    @field_validator("connection_ref", mode="before")
    @classmethod
    def _validate_connection_ref(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("connection_ref must be a non-empty string")
        if _looks_like_raw_dsn(v):
            raise ValueError("connection_ref must be an opaque reference, not a raw DSN")
        return v

    @field_validator("dsn", mode="before")
    @classmethod
    def _validate_dsn(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dsn is required")
        return v

    @field_validator("admin_raw_dsn_compat", mode="before")
    @classmethod
    def _coerce_admin_raw_dsn_compat(cls, v: Any) -> bool:
        return coerce_strict_bool(v, default=False, field_name="admin_raw_dsn_compat")

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

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("idempotency_key must be a string")
        if (
            not v
            or v != v.strip()
            or len(v) > 128
            or not all(character.isprintable() for character in v)
        ):
            raise ValueError(
                "idempotency_key must be canonical printable text up to 128 characters"
            )
        return v

    @model_validator(mode="after")
    def _validate_connection_input(self) -> "TextToSqlGenerateRequest":
        if (self.connection_ref is None) == (self.dsn is None):
            raise ValueError("exactly one of connection_ref or dsn is required")
        return self

    @model_validator(mode="after")
    def _validate_schema_mode(self) -> "TextToSqlGenerateRequest":
        if not self.use_schema_suggestions:
            raise ValueError("schema grounding is required for Text-to-SQL")
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
            location = first.get("loc") or ()
            if first.get("type") == "missing" and location:
                msg = f"{'.'.join(str(part) for part in location)} is required"
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


def parse_text_to_sql_start(payload: dict[str, Any]) -> TextToSqlGenerateRequest:
    """Normalize the public query alias and validate one start request."""
    if not isinstance(payload, dict):
        raise ValueError("service_payload must be an object")
    query = ""
    for key in ("natural_query", "query"):
        candidate = payload.get(key)
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            raise ValueError(f"{key} must be a string")
        if candidate.strip():
            query = candidate.strip()
            break
    normalized = dict(payload)
    normalized["query"] = query
    return parse_text_to_sql_generate(normalized)


def canonical_text_to_sql_start_fingerprint(payload: dict[str, Any]) -> str:
    """Digest canonical validated semantics, excluding transport-only identity."""
    request = parse_text_to_sql_start(payload)
    excluded_fields = {"admin_raw_dsn_compat", "client_id", "idempotency_key"}
    excluded_fields.add("dsn" if request.connection_ref is not None else "connection_ref")
    canonical = request.model_dump(
        exclude=excluded_fields,
        mode="json",
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
