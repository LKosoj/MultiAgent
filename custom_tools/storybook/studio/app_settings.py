"""Настройки приложения (Ф12): папки, бэкапы, логи, уровень логов.

Общие ключи доступны из React и Tk через возможности `app_settings_*`. Ключи, которые
нужны только Tk (тема, геометрия окна, масштаб, автосохранение), читаются и пишутся
обычными функциями `tk_settings_*` без регистрации.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from custom_tools.storybook.project_paths import storybook_projects_root
from StoryBookManager.config.settings import app_settings

from . import project_store
from .capabilities import capability
from .errors import StudioError, success

logger = logging.getLogger(__name__)

SHARED_KEYS = ("projects_directory", "backup_directory", "logs_directory", "max_backup_files", "log_level")
PATH_KEYS = ("projects_directory", "backup_directory", "logs_directory")
PATH_KINDS = {"projects": "projects_directory", "backups": "backup_directory", "logs": "logs_directory"}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
UI_SCALES = (100, 125, 150, 200)
ENV_NAME = "STORYBOOK_PROJECTS_DIR"
_GEOMETRY_RE = re.compile(r"^\d+x\d+([+-]-?\d+[+-]-?\d+)?$")

PATH_MESSAGES = {
    "ok": "Папка доступна",
    "missing": "Папки не существует",
    "not_dir": "Это не папка",
    "not_writable": "Нет прав на запись",
}
MSG_LOCKED_RUN = "Недоступно, пока идёт прогон"
MSG_LOCKED_ENV = "Папка задана переменной окружения сервера"


# ---------------------------------------------------------------- окружение

# Значение переменной, которое выставили мы сами (не сервер): пока она равна ему, это не "env"
_env_set_by_us: str | None = None


def _set_env_ourselves(value: str) -> None:
    global _env_set_by_us
    os.environ[ENV_NAME] = value
    _env_set_by_us = value


def apply_runtime_environment() -> None:
    """Если корень проектов не задан окружением, взять его из настроек (как делает Tk)."""
    if not os.environ.get(ENV_NAME):
        value = app_settings.get("projects_directory")
        if value:
            _set_env_ourselves(str(value))


# ---------------------------------------------------------------- чтение

def _disk_settings() -> dict:
    try:
        data = json.loads(Path(app_settings.config_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize(value: Any, field: str = "Путь") -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise StudioError("invalid_argument", f"{field}: укажите непустой путь без служебных символов")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = Path(app_settings.project_root) / path
    return path.resolve()


def _env_root() -> Path | None:
    configured = os.environ.get(ENV_NAME)
    return Path(configured).expanduser().resolve() if configured else None


def _projects_from_env() -> bool:
    """Корень задан окружением сервера, а не файлом настроек."""
    env = _env_root()
    if env is None or os.environ.get(ENV_NAME) == _env_set_by_us:
        return False
    try:
        return env != _normalize(_disk_settings().get("projects_directory") or app_settings.defaults["projects_directory"])
    except StudioError:
        return True


def _source(key: str, disk: dict) -> str:
    if key == "projects_directory" and _projects_from_env():
        return "env"
    replaced = getattr(app_settings, "_raw_replaced", {})
    if key in replaced and replaced[key][1] == app_settings.defaults.get(key):
        return "default"
    return "file" if key in disk else "default"


def _values() -> dict:
    disk = _disk_settings()
    result = {}
    for key in SHARED_KEYS:
        value = app_settings.get(key, app_settings.defaults.get(key))
        if key == "projects_directory" and _projects_from_env():
            value = str(_env_root())
        result[key] = {"value": value, "default": app_settings.defaults.get(key), "source": _source(key, disk)}
    return result


def _digest(values: dict) -> str:
    plain = {key: values[key]["value"] for key in SHARED_KEYS}
    return hashlib.sha256(json.dumps(plain, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def path_state(path: Path) -> str:
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not_dir"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return "not_writable"
    return "ok"


def _warnings() -> list[str]:
    result = []
    for key, (raw, substituted) in getattr(app_settings, "_raw_replaced", {}).items():
        if substituted == app_settings.defaults.get(key) and raw != substituted:
            result.append(f"Путь {raw} не найден, использована папка по умолчанию")
    return result


def _any_busy(root: Path) -> bool:
    return any(project_store.is_busy(path) for path in project_store.list_project_dirs(root))


def _locked_reason() -> str | None:
    if _projects_from_env():
        return MSG_LOCKED_ENV
    if _any_busy(storybook_projects_root()):
        return MSG_LOCKED_RUN
    return None


def _snapshot(**extra) -> dict:
    values = _values()
    checks = {}
    for key in PATH_KEYS:
        state = path_state(Path(str(values[key]["value"])))
        checks[key] = {"state": state, "message": PATH_MESSAGES[state]}
    return success(
        settings=values, checks=checks, warnings=extra.pop("warnings", []) + _warnings(),
        digest=_digest(values), projects_directory_locked_reason=_locked_reason(), **extra,
    )


# ---------------------------------------------------------------- запись

def _write_settings(updates: dict) -> None:
    """Слияние со свежим файлом под блокировкой, атомарная запись, затем память процесса."""
    config_file = Path(app_settings.config_file)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    lock = open(config_file.with_name(config_file.name + ".lock"), "a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        data = _disk_settings() or dict(app_settings.defaults)
        data.update(updates)
        tmp = config_file.with_name(f".{config_file.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, config_file)
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        lock.close()
    for key, value in updates.items():
        app_settings.settings[key] = value
        getattr(app_settings, "_raw_replaced", {}).pop(key, None)
        if hasattr(app_settings, "_baseline"):
            app_settings._baseline[key] = value  # Tk не должен считать это своей правкой


def _related(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _dir_for_new(path: Path, field: str) -> Path:
    """Папка бэкапов/логов: существует или существует родитель (создадим одну папку)."""
    if path.exists():
        base = path
        if not path.is_dir():
            raise StudioError("invalid_argument", f"{field}: это не папка")
    else:
        base = path.parent
        if not base.is_dir():
            raise StudioError("invalid_argument", f"{field}: родительской папки не существует")
    if not os.access(base, os.R_OK | os.W_OK | os.X_OK):
        raise StudioError("invalid_argument", f"{field}: нет прав на запись")
    return path


def _plan_update(new: dict) -> dict:
    """Проверить и нормализовать новые значения; вернуть то, что реально меняется."""
    current = {key: info["value"] for key, info in _values().items()}
    resolved: dict[str, Any] = {}
    if new.get("projects_directory") is not None:
        path = _normalize(new["projects_directory"], "Папка проектов")
        if str(path) != str(Path(str(current["projects_directory"])).expanduser().resolve()):
            if _projects_from_env():
                raise StudioError("unsupported", MSG_LOCKED_ENV)
            if path_state(path) != "ok":
                raise StudioError("invalid_argument", f"Папка проектов: {PATH_MESSAGES[path_state(path)].lower()}")
            if _any_busy(storybook_projects_root()):
                raise StudioError("locked", MSG_LOCKED_RUN)
            resolved["projects_directory"] = str(path)
    for key, field in (("backup_directory", "Папка бэкапов"), ("logs_directory", "Папка логов")):
        if new.get(key) is not None:
            path = _dir_for_new(_normalize(new[key], field), field)
            if str(path) != str(Path(str(current[key])).expanduser().resolve()):
                resolved[key] = str(path)
    if new.get("max_backup_files") is not None:
        value = new["max_backup_files"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise StudioError("invalid_argument", "Поле «Число бэкапов» должно быть целым от 1 до 100")
        if value != current["max_backup_files"]:
            resolved["max_backup_files"] = value
    if new.get("log_level") is not None:
        level = new["log_level"]
        level = level.strip().upper() if isinstance(level, str) else level
        if level not in LOG_LEVELS:
            raise StudioError("invalid_argument", f"Поле «Уровень логов» должно быть одним из: {', '.join(LOG_LEVELS)}")
        if level != current["log_level"]:
            resolved["log_level"] = level
    _check_overlap({**{k: current[k] for k in PATH_KEYS}, **resolved})
    return resolved


def _check_overlap(paths: dict) -> None:
    items = [(key, Path(str(paths[key])).expanduser().resolve()) for key in PATH_KEYS]
    for index, (key_a, a) in enumerate(items):
        for key_b, b in items[index + 1:]:
            if _related(a, b):
                raise StudioError("invalid_argument", "Папки проектов, бэкапов и логов не должны лежать друг в друге")


def _apply(resolved: dict) -> list[str]:
    """Записать файл и применить значения в работающем процессе; вернуть предупреждения."""
    warnings: list[str] = []
    old_backup = app_settings.get("backup_directory")
    for key in ("backup_directory", "logs_directory"):
        if key in resolved:
            Path(resolved[key]).mkdir(parents=True, exist_ok=True)
    _write_settings(resolved)
    if "projects_directory" in resolved:
        _set_env_ourselves(resolved["projects_directory"])
        project_store.invalidate_size_cache()
    if "log_level" in resolved:
        logging.getLogger().setLevel(getattr(logging, resolved["log_level"]))
    if "backup_directory" in resolved and old_backup:
        warnings.append(f"Прежние бэкапы остались в {old_backup}")
    return warnings


# ---------------------------------------------------------------- возможности

@capability("app_settings_get", title="Настройки приложения", access="read", project_scoped=False)
def app_settings_get() -> dict:
    return _snapshot()


@capability("app_settings_update", title="Изменить настройки приложения", access="write", project_scoped=False)
def app_settings_update(*, projects_directory: str | None = None, backup_directory: str | None = None,
                        logs_directory: str | None = None, max_backup_files: int | None = None,
                        log_level: str | None = None, expected_digest: str = "") -> dict:
    if expected_digest and expected_digest != _digest(_values()):
        raise StudioError("conflict", "Данные изменили после того, как вы их открыли. Обновите страницу и повторите.")
    resolved = _plan_update({
        "projects_directory": projects_directory, "backup_directory": backup_directory,
        "logs_directory": logs_directory, "max_backup_files": max_backup_files, "log_level": log_level,
    })
    warnings = _apply(resolved) if resolved else []
    return _snapshot(changed=sorted(resolved), warnings=warnings)


@capability("app_settings_reset", title="Сбросить настройки приложения", access="write", project_scoped=False)
def app_settings_reset(*, confirm: bool = False) -> dict:
    if confirm is not True:
        raise StudioError("invalid_argument", "Подтвердите сброс настроек")
    defaults = {key: app_settings.defaults[key] for key in SHARED_KEYS}
    default_projects = Path(defaults["projects_directory"])
    if not default_projects.exists() and default_projects.parent.is_dir() and not _projects_from_env():
        default_projects.mkdir(exist_ok=True)  # своя папка по умолчанию, не ввод пользователя
    resolved = _plan_update(defaults)
    warnings = _apply(resolved) if resolved else []
    return _snapshot(changed=sorted(resolved), warnings=warnings)


@capability("app_settings_check_path", title="Проверить папку", access="write", project_scoped=False)
def app_settings_check_path(*, kind: str, path: str) -> dict:
    if kind not in PATH_KINDS:
        raise StudioError("invalid_argument", "Вид папки: projects, backups или logs")
    resolved = _normalize(path)
    state = path_state(resolved)
    return success(state=state, message=PATH_MESSAGES[state], resolved=str(resolved))


# ---------------------------------------------------------------- только Tk (не возможности)

TK_DEFAULTS = {"theme": "default", "window_geometry": "1200x800", "ui_scale": 100, "auto_save_interval": 30}


def _tk_validate(key: str, value: Any) -> Any:
    if key == "window_geometry":
        if not isinstance(value, str) or not _GEOMETRY_RE.match(value):
            raise StudioError("invalid_argument", "Геометрия окна: ожидается вид 1200x800 или 1200x800+10+10")
    elif key == "ui_scale":
        if isinstance(value, bool) or value not in UI_SCALES:
            raise StudioError("invalid_argument", "Масштаб интерфейса: 100, 125, 150 или 200 процентов")
    elif key == "theme":
        if not isinstance(value, str) or not value.strip() or len(value) > 32:
            raise StudioError("invalid_argument", "Тема: непустая строка до 32 символов")
    elif key == "auto_save_interval":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
            raise StudioError("invalid_argument", "Автосохранение: целое число секунд от 0 до 3600")
    return value


def tk_settings_get() -> dict:
    return {key: app_settings.get(key, default) for key, default in TK_DEFAULTS.items()}


def tk_settings_update(**changes: Any) -> dict:
    unknown = sorted(set(changes) - set(TK_DEFAULTS))
    if unknown:
        raise StudioError("invalid_argument", f"Неизвестные настройки: {', '.join(unknown)}")
    _write_settings({key: _tk_validate(key, value) for key, value in changes.items()})
    return tk_settings_get()


def tk_settings_reset() -> dict:
    _write_settings(dict(TK_DEFAULTS))
    return tk_settings_get()
