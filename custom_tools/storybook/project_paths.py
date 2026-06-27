import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


def storybook_projects_root() -> Path:
    """Return the storybook projects root used by pipeline tools."""
    env_root = _env_projects_root()
    if env_root is not None:
        return env_root
    return _cwd_projects_root()


def _env_projects_root() -> Path | None:
    configured = os.getenv("STORYBOOK_PROJECTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return None


def _cwd_projects_root() -> Path:
    return (Path("plots") / "storybooks").resolve()


def require_safe_storybook_project_id(value: Any, field_name: str = "project_id") -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} is required")
    if stripped in {".", ".."}:
        raise ValueError(f"{field_name} must be a safe path segment")
    if (
        "/" in stripped
        or "\\" in stripped
        or PurePosixPath(stripped).is_absolute()
        or PureWindowsPath(stripped).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(stripped).parts)
        or any(part in {"", ".", ".."} for part in PureWindowsPath(stripped).parts)
    ):
        raise ValueError(f"{field_name} must be a safe path segment")
    return stripped


def safe_storybook_project_dir(project_id: str, *, must_exist: bool = False) -> Path:
    value = require_safe_storybook_project_id(project_id)

    root = storybook_projects_root()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"project_id escapes storybook root: {project_id}") from exc
    if candidate == root:
        raise ValueError("project_id must identify a project directory")
    if must_exist and not candidate.is_dir():
        raise ValueError(f"project not found: {project_id}")
    return candidate
