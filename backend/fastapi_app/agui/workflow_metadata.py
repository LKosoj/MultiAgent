"""Workflow YAML metadata helpers shared by AG-UI service and runner."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = False, *, field_name: str = "metadata flag") -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be boolean")


def workflow_agui_entrypoint(
    workflow_name: Any,
    pipelines_dir: Path,
) -> Optional[str]:
    """Return metadata-declared AG-UI entrypoint for a workflow, if any."""
    requested_name = str(workflow_name or "").strip()
    safe_name = "".join(c for c in requested_name if c.isalnum() or c in "._-")
    if not requested_name or safe_name != requested_name or safe_name in {".", ".."}:
        raise ValueError("invalid workflow_name")

    base_dir = pipelines_dir.resolve()
    direct_path = (base_dir / f"{safe_name}.yaml").resolve()
    if base_dir != direct_path.parent:
        raise ValueError("invalid workflow_name")

    candidates: list[tuple[Path, bool]] = []
    if direct_path.exists():
        candidates.append((direct_path, True))
    for path in sorted(base_dir.glob("*.yaml")):
        resolved = path.resolve()
        if resolved == direct_path:
            continue
        candidates.append((resolved, False))

    for candidate, is_direct in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            if is_direct:
                raise ValueError(
                    f"invalid workflow metadata file: {candidate.name}"
                ) from exc
            logger.warning(
                "Skipping invalid workflow YAML %s while resolving AG-UI metadata for %s",
                candidate,
                requested_name,
            )
            continue
        if not isinstance(data, dict):
            continue
        if not is_direct and data.get("name") != requested_name:
            continue
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return None
        entrypoint = metadata.get("agui_entrypoint")
        forbid_start = _coerce_bool(
            metadata.get("forbid_workflows_start"),
            False,
            field_name=f"{candidate.name}: metadata.forbid_workflows_start",
        )
        if isinstance(entrypoint, str) and entrypoint.strip():
            return entrypoint.strip()
        if forbid_start:
            raise ValueError(
                f"{candidate.name}: metadata.agui_entrypoint is required "
                "when metadata.forbid_workflows_start is true"
            )
        return None
    return None
