"""Shared immutable model primitives and dependency-graph helpers."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


CONTRACT_VERSION = 1

Id: TypeAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Digest: TypeAlias = Annotated[
    str,
    Field(
        min_length=3,
        max_length=1024,
        pattern=r"^[a-z0-9][a-z0-9_-]*:[0-9a-f]+$",
    ),
]
NonEmptyText: TypeAlias = Annotated[str, Field(min_length=1, max_length=65536)]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
Probability: TypeAlias = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    """Закрытая immutable Pydantic-модель."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ContractModel(StrictModel):
    """Общие поля versioned top-level contracts."""

    contract_version: Literal[1] = CONTRACT_VERSION
    run_id: Id
    run_incarnation: Id
    revision: NonNegativeInt
    schema_namespace_version: Digest | None


def unique_ids(values: tuple[object, ...], field_name: str, label: str) -> set[str]:
    identifiers = [getattr(value, field_name) for value in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{label} {field_name} must be unique")
    return set(identifiers)


def require_canonical_ids(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be canonical unique sorted")


__all__ = [
    "CONTRACT_VERSION",
    "ContractModel",
    "Digest",
    "Id",
    "NonEmptyText",
    "NonNegativeInt",
    "Probability",
    "StrictModel",
]
