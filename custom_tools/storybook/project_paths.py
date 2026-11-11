import os
from contextlib import contextmanager
from contextvars import ContextVar
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


_series_scope = ContextVar("storybook_series_scope", default=None)


def current_series_scope():
    return _series_scope.get()


@contextmanager
def series_scope(project_id: str, series_id: str):
    project_id = require_safe_storybook_project_id(project_id)
    series_id = require_safe_storybook_project_id(series_id, "series_id")
    from custom_tools.storybook.studio.series import series_dir
    series_dir(project_id, series_id)
    token = _series_scope.set((project_id, series_id))
    try:
        yield
    finally:
        _series_scope.reset(token)


def safe_storybook_project_dir(project_id: str, *, must_exist: bool = False) -> Path:
    value = require_safe_storybook_project_id(project_id)

    root = storybook_projects_root()
    scope = _series_scope.get()
    candidate = (root / value / "series" / scope[1]).resolve() if scope and scope[0] == value else (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"project_id escapes storybook root: {project_id}") from exc
    if candidate == root:
        raise ValueError("project_id must identify a project directory")
    if must_exist and not candidate.is_dir():
        raise ValueError(f"project not found: {project_id}")
    return candidate


def _require_current_lock(handle) -> None:
    from custom_tools.storybook.studio.errors import StudioError
    held = os.fstat(handle.fileno())
    current = os.stat(handle.name)
    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise StudioError("not_found", "Проект или серия заменены. Выберите серию заново")


def acquire_storybook_project_guard(project_id: str):
    """Shared project lock: series work can coexist, project replacement cannot."""
    import fcntl
    from custom_tools.storybook.studio.errors import StudioError
    guard = None
    try:
        guard = (storybook_projects_root() / require_safe_storybook_project_id(project_id) / ".pipeline.lock").open("a+", encoding="utf-8")
        fcntl.flock(guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        _require_current_lock(guard)
        return guard
    except BlockingIOError:
        release_storybook_project_lock(guard)
        return None
    except BaseException as exc:
        release_storybook_project_lock(guard)
        if isinstance(exc, FileNotFoundError):
            raise StudioError("not_found", "Проект удалён. Выберите проект заново") from exc
        raise


def acquire_storybook_project_lock(project_id: str):
    """Acquire the existing non-blocking advisory lock for one project."""
    project_id = require_safe_storybook_project_id(project_id)
    project_dir = safe_storybook_project_dir(project_id)
    scope = current_series_scope()
    scoped = bool(scope and scope[0] == project_id)
    guard = None
    handle = None
    try:
        import fcntl

        if scoped:
            from custom_tools.storybook.studio.series import series_dir
            guard = acquire_storybook_project_guard(project_id)
            if guard is None:
                return None
            project_dir = series_dir(project_id, scope[1])
        else:
            project_dir.mkdir(parents=True, exist_ok=True)
        handle = (project_dir / ".pipeline.lock").open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if scoped:
            _require_current_lock(handle)
            series_dir(project_id, scope[1])
    except ImportError:
        project_dir.mkdir(parents=True, exist_ok=True)
        handle = (project_dir / ".pipeline.lock").open("a+", encoding="utf-8")
    except BlockingIOError:
        release_storybook_project_lock(handle)
        release_storybook_project_lock(guard)
        return None
    except BaseException as exc:
        release_storybook_project_lock(handle)
        release_storybook_project_lock(guard)
        if scoped and isinstance(exc, FileNotFoundError):
            from custom_tools.storybook.studio.errors import StudioError
            raise StudioError("not_found", "Проект или серия удалены. Выберите серию заново") from exc
        raise
    return (handle, guard) if guard is not None else handle


def release_storybook_project_lock(handle) -> None:
    if handle is None:
        return
    if isinstance(handle, tuple):
        for item in handle:
            release_storybook_project_lock(item)
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            handle.close()
        except Exception:
            pass
