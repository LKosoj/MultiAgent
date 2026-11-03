"""Настройки видео-прогона проекта: одно место, откуда их читают все видеоинструменты.

Файл `<проект>/.studio/run_settings.json` пишет студия (`studio/run_settings.py`),
а читают его сами инструменты: прогон идёт в отдельном процессе, переменные
окружения до него не доходят, а `contextvars` не переходит в потоки
ThreadPoolExecutor. Поэтому проект запоминается в переменной уровня модуля.

Ограничение: «привязанный» проект один на процесс. Воркер backend ведёт один
прогон, Tk-раннер тоже выполняет один прогон за раз. Если один процесс начнёт
вести два прогона разных проектов одновременно, настройки перепутаются.
Читалки интерфейса проект не привязывают, а передают `project_id` явно.

Модуль низкоуровневый: только стандартная библиотека и `project_paths`
(без импорта studio, video_contract и инструментов, чтобы не было круговых импортов).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook.project_paths import safe_storybook_project_dir

logger = logging.getLogger(__name__)

SETTINGS_RELPATH = ".studio/run_settings.json"
DEFAULT_PROVIDER = "routerai"
PROVIDER_TOOLS = {
    "routerai": "video_generator_routerai_tool",
    "aitunnel": "video_generator_aitunnel_tool",
    "minimax": "video_generator_mm_tool",
    "veo": "video_generator_veo_tool",
}
DISPATCH_TOOL_NAME = "video_generator_dispatch_tool"

# имя настройки окружения (суффикс после <ПРОВАЙДЕР>_) -> поле раздела video файла
_SETTING_FIELDS = {
    "VIDEO_MODEL": "model",
    "RESOLUTION": "resolution",
    "VIDEO_FALLBACK_MODEL": "fallback_model",
    "VIDEO_RETRY_CYCLES": "retry_cycles",
}

_bound_project: Optional[str] = None
_budget_fallback: dict[str, float] = {}
_lock = threading.Lock()


def bind_run_project(project_id: Any) -> None:
    """Запомнить проект текущего прогона (вызывают инструменты внутри прогона).

    Привязка одна на процесс и «залипает» до следующего вызова: инструмент без
    проекта обязан сбросить её через `bind_run_project(None)`.
    """
    global _bound_project
    value = str(project_id).strip() if project_id else ""
    _bound_project = value or None


def bound_project() -> Optional[str]:
    return _bound_project


def read_stored_at(project_dir: Any) -> dict:
    """Сырой файл настроек из папки проекта; `{}`, если файла нет или он битый."""
    try:
        path = Path(project_dir) / SETTINGS_RELPATH
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("run_settings.json в %s не прочитан: %s", project_dir, exc)
        return {}
    return data if isinstance(data, dict) else {}


def read_stored(project_id: Any) -> dict:
    """Сырой файл настроек проекта; `{}`, если файла нет или он битый."""
    if not project_id:
        return {}
    try:
        return read_stored_at(safe_storybook_project_dir(str(project_id)))
    except ValueError:
        return {}


def _section(stored: dict, name: str) -> dict:
    value = stored.get(name)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _project(project_id: Any) -> Optional[str]:
    return str(project_id) if project_id else _bound_project


def effective_provider(project_id: Any = None, explicit: Any = "") -> str:
    """Провайдер видео: вход -> файл проекта -> STORYBOOK_VIDEO_PROVIDER -> routerai."""
    name = _text(explicit).lower()
    if name:
        return name
    name = _text(_section(read_stored(_project(project_id)), "video").get("provider")).lower()
    if name:
        return name
    name = (os.getenv("STORYBOOK_VIDEO_PROVIDER") or "").strip().lower()
    return name or DEFAULT_PROVIDER


def video_setting(name: str, provider: str, project_id: Any = None) -> str:
    """Настройка `<ПРОВАЙДЕР>_<name>`: файл проекта (только для выбранного провайдера) -> окружение."""
    field = _SETTING_FIELDS.get(name)
    if field:
        stored = read_stored(_project(project_id))
        if stored and _text(provider).lower() == effective_provider(project_id):
            value = _section(stored, "video").get(field)
            if value is not None and not isinstance(value, bool) and str(value).strip():
                return str(value).strip()
    return (os.getenv(f"{provider.upper()}_{name}") or "").strip()


def tool_name_for(provider: Any) -> Optional[str]:
    return PROVIDER_TOOLS.get(_text(provider).lower())


def stored_budget_rub_at(project_dir: Any) -> Optional[float]:
    """Бюджет из файла проекта; `None`, если не задан (читается заново при каждой отправке)."""
    value = read_stored_at(project_dir).get("budget_rub")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def set_run_budget_fallback(project_id: Any, budget_rub: Any) -> None:
    """Запасной бюджет из параметра диспетчера (запуск не из студии, файла нет)."""
    key = str(project_id or "")
    try:
        value = float(budget_rub)
    except (TypeError, ValueError):
        value = 0.0
    with _lock:
        if value > 0:
            _budget_fallback[key] = value
        else:
            _budget_fallback.pop(key, None)


def budget_fallback(project_id: Any) -> float:
    with _lock:
        return _budget_fallback.get(str(project_id or ""), 0.0)

