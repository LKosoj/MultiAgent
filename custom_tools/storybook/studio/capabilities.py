"""Реестр возможностей studio: одна запись на функцию, общая для бэкенда, React и Tk."""

from __future__ import annotations

import importlib
import inspect
import logging
import re
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal

from custom_tools.storybook.project_paths import require_safe_storybook_project_id

from .errors import StudioError, error_result, from_exception, is_error

logger = logging.getLogger(__name__)

Access = Literal["read", "write", "run"]
_ACCESS_VALUES = ("read", "write", "run")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Фиксированный список модулей: новые возможности добавляются в эти файлы,
# а не сюда. Отсутствующий модуль пропускается (ещё не написан).
STUDIO_MODULES = ("core", "projects", "status", "runs", "run_settings", "shots",
                  "review", "reports", "media", "artifacts", "blockout",
                  "app_settings", "image_edit")
_PACKAGE = "custom_tools.storybook.studio"


def _camel(snake: str) -> str:
    head, *tail = snake.split("_")
    return head + "".join(part.capitalize() for part in tail)


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    access: Access
    func: Callable[..., dict]
    module: str
    category: str = "studio"
    client_fn: str = ""
    tk_handler: str = ""
    supersedes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    project_scoped: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"bad capability id: {self.id!r}")
        if self.access not in _ACCESS_VALUES:
            raise ValueError(f"bad capability access: {self.access!r}")
        if not self.client_fn:
            object.__setattr__(self, "client_fn", _camel(self.id))
        if not self.tk_handler:
            object.__setattr__(self, "tk_handler", self.id)

    @property
    def action(self) -> str:
        return f"storybook.{self.id}"


_REGISTRY: dict[str, Capability] = {}
_LOADED = False
_LOCK = threading.RLock()


def capability(id: str, *, title: str, access: Access, category: str = "studio",
               client_fn: str = "", tk_handler: str = "",
               supersedes: tuple[str, ...] = (), aliases: tuple[str, ...] = (),
               project_scoped: bool = True, reason: str = ""):
    """Записать функцию в реестр; саму функцию возвращает без изменений."""

    def decorate(func: Callable[..., dict]) -> Callable[..., dict]:
        module = func.__module__.rsplit(".", 1)[-1]
        cap = Capability(
            id=id, title=title, access=access, func=func, module=module,
            category=category, client_fn=client_fn, tk_handler=tk_handler,
            supersedes=tuple(supersedes), aliases=tuple(aliases),
            project_scoped=project_scoped, reason=reason,
        )
        with _LOCK:
            taken = set(_REGISTRY) | {alias for other in _REGISTRY.values() for alias in other.aliases}
            for name in (cap.id, *cap.aliases):
                if name in taken:
                    raise ValueError(f"duplicate capability id or alias: {name}")
            _REGISTRY[cap.id] = cap
        return func

    return decorate


def load_all() -> None:
    """Импортировать модули studio один раз (идемпотентно)."""
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        for name in STUDIO_MODULES:
            full = f"{_PACKAGE}.{name}"
            try:
                importlib.import_module(full)
            except ModuleNotFoundError as exc:
                if exc.name != full:
                    raise
        _LOADED = True


def registry() -> dict[str, Capability]:
    load_all()
    with _LOCK:
        return dict(_REGISTRY)


def _reset_for_tests() -> None:
    """Очистить реестр и забыть импортированные модули studio (только для тестов)."""
    global _LOADED
    with _LOCK:
        _REGISTRY.clear()
        _LOADED = False
        for name in STUDIO_MODULES:
            sys.modules.pop(f"{_PACKAGE}.{name}", None)


def find(name: str) -> Capability | None:
    """Найти возможность по id, `storybook.<id>` или псевдониму."""
    reg = registry()
    short = name[len("storybook."):] if name.startswith("storybook.") else name
    if short in reg:
        return reg[short]
    for cap in reg.values():
        if name in cap.aliases or short in cap.aliases:
            return cap
    return None


def invoke(capability_id: str, payload: dict | None = None) -> dict:
    """Единая точка вызова: проверка аргументов, вызов, единый формат ответа."""
    cap = find(capability_id)
    if cap is None:
        return error_result("unknown_capability", f"Неизвестная возможность: {capability_id}")
    args: dict[str, Any] = {
        key: value for key, value in (payload or {}).items()
        if not (isinstance(key, str) and key.startswith("__"))
    }
    params = inspect.signature(cap.func).parameters
    extra = sorted(set(args) - set(params))
    if extra:
        return error_result("invalid_argument", f"Лишние параметры: {', '.join(extra)}")
    missing = [
        name for name, param in params.items()
        if param.default is inspect.Parameter.empty and name not in args
    ]
    if missing:
        return error_result("invalid_argument", f"Не хватает параметров: {', '.join(missing)}")
    try:
        if "project_id" in params:
            args["project_id"] = require_safe_storybook_project_id(args.get("project_id"))
        result = cap.func(**args)
    except PermissionError:
        raise
    except StudioError as exc:
        return from_exception(exc)
    except ValueError as exc:
        return from_exception(exc)
    except Exception:
        logger.exception("studio capability %s failed", cap.id)
        return error_result("internal", "Внутренняя ошибка")
    if not isinstance(result, dict):
        return error_result("invalid_result", "Возможность вернула не словарь")
    if "status" not in result and not is_error(result):
        result = {"status": "success", **result}
    return result
