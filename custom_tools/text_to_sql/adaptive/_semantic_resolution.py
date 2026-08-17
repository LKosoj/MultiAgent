"""Pure semantic-item resolution derived from one source's bindings."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class _BindingResolutionInput(Protocol):
    binding_id: str
    status: object


def derive_semantic_resolution(
    prior_status: str,
    bindings: Iterable[_BindingResolutionInput],
) -> tuple[str, tuple[str, ...]]:
    """Return the canonical status and binding IDs for one semantic source."""

    source_bindings = tuple(sorted(bindings, key=lambda binding: binding.binding_id))
    supported_ids = tuple(
        binding.binding_id
        for binding in source_bindings
        if _status_value(binding.status) == "supported"
    )
    candidate_ids = tuple(
        binding.binding_id
        for binding in source_bindings
        if _status_value(binding.status) == "candidate"
    )
    if supported_ids:
        return "resolved", supported_ids
    if candidate_ids:
        return "partially_resolved", candidate_ids
    if prior_status == "unsupported":
        return "unsupported", ()
    return "unresolved", ()


def _status_value(value: object) -> str:
    raw = getattr(value, "value", value)
    if type(raw) is not str:
        raise TypeError("binding status must be a string enum")
    return raw
