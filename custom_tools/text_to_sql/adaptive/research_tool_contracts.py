"""Pure logical argument contracts for adaptive research tools."""

from __future__ import annotations

import math
from typing import Annotated, TypeAlias

from pydantic import Field, field_validator, model_validator

from .models import Id, StrictModel


MAX_RESEARCH_PARAMETERS = 64
MAX_RESEARCH_PARAMETER_STRING_CHARS = 16 * 1024
MAX_SEARCH_VALUE_STRING_CHARS = 4 * 1024

_JsonScalar: TypeAlias = str | int | float | bool | None
_Parameters: TypeAlias = tuple[tuple[str, _JsonScalar], ...]


class _RequestModel(StrictModel):
    pass


_TopK = Annotated[int, Field(ge=1, le=50)]
_Depth = Annotated[int, Field(ge=1, le=4)]
_Limit = Annotated[int, Field(ge=1, le=50)]
_Sql = Annotated[str, Field(min_length=1, max_length=50_000)]
_ColumnName = Annotated[str, Field(min_length=1, max_length=65_536)]
_LogicalName = Annotated[str, Field(min_length=1, max_length=65_536)]


class _TableTopKRequest(_RequestModel):
    query: _LogicalName
    top_k: _TopK


class _TableRequest(_RequestModel):
    table: _LogicalName


class _ColumnRequest(_RequestModel):
    table: _LogicalName
    column: _ColumnName


class _RelationshipsRequest(_RequestModel):
    table: _LogicalName
    top_k: _TopK
    depth: _Depth


class _SampleRowsRequest(_RequestModel):
    table: _LogicalName
    columns: tuple[_ColumnName, ...]
    limit: _Limit

    @field_validator("columns", mode="before")
    @classmethod
    def decode_json_columns(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        return value

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= 20:
            raise ValueError("columns exceed the closed data probe bound")
        if len(value) != len(set(value)):
            raise ValueError("columns must be unique")
        return value


class _SearchValueRequest(_RequestModel):
    table: _LogicalName
    column: _ColumnName
    value: _JsonScalar
    top_k: _TopK

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: _JsonScalar) -> _JsonScalar:
        _require_json_scalar(
            value,
            "value",
            max_string_chars=MAX_SEARCH_VALUE_STRING_CHARS,
        )
        return value


class _ColumnTopKRequest(_RequestModel):
    table: _LogicalName
    column: _ColumnName
    top_k: _TopK


class _RawResearchRequest(_RequestModel):
    sql: _Sql
    parameters: Annotated[
        tuple[_JsonScalar, ...],
        Field(max_length=MAX_RESEARCH_PARAMETERS),
    ] = ()

    @field_validator("parameters", mode="before")
    @classmethod
    def decode_json_parameters(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_raw_request(self) -> _RawResearchRequest:
        if not self.sql.strip():
            raise ValueError("sql must contain a statement")
        for index, value in enumerate(self.parameters):
            _require_json_scalar(
                value,
                f"parameter_{index:03d}",
                max_string_chars=MAX_RESEARCH_PARAMETER_STRING_CHARS,
            )
        return self


class _DocumentRequest(_RequestModel):
    document_id: Id


SearchSchemaCatalogArguments = _TableTopKRequest
InspectTableArguments = _TableRequest
InspectColumnArguments = _ColumnRequest
InspectRelationshipsArguments = _RelationshipsRequest
ProfileColumnArguments = _ColumnRequest
SampleRowsArguments = _SampleRowsRequest
SearchValueArguments = _SearchValueRequest
GetDistinctValuesArguments = _ColumnTopKRequest
ExecuteResearchProbeArguments = _RawResearchRequest
ReadSchemaEvidenceArguments = _DocumentRequest


def _require_json_scalar(
    value: object,
    label: str,
    *,
    max_string_chars: int,
) -> None:
    if type(value) is str:
        if len(value) > max_string_chars:
            raise ValueError(f"{label} string exceeds its closed bound")
        return
    if value is None or type(value) in {int, bool}:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise ValueError(f"{label} must be an exact finite JSON scalar")


__all__ = [
    "ExecuteResearchProbeArguments",
    "GetDistinctValuesArguments",
    "InspectColumnArguments",
    "InspectRelationshipsArguments",
    "InspectTableArguments",
    "MAX_RESEARCH_PARAMETERS",
    "MAX_RESEARCH_PARAMETER_STRING_CHARS",
    "MAX_SEARCH_VALUE_STRING_CHARS",
    "ProfileColumnArguments",
    "ReadSchemaEvidenceArguments",
    "SampleRowsArguments",
    "SearchSchemaCatalogArguments",
    "SearchValueArguments",
]
