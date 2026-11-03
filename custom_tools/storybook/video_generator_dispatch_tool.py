"""Диспетчер видеопровайдера: шаг video_generator выбирает провайдера входом, а не правкой YAML.

Диспетчер не оборачивает ошибки и не повторяет вызов: результат целевого
инструмента возвращается как есть, добавляются только ключи `provider` и
`dispatch`. Его единственная защита: не молча выбрасывать параметры, которых нет
у ограниченного провайдера (veo, minimax). Иначе просьба «один пробный шот»
превратилась бы в платную генерацию всех шотов.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Dict, Optional

from custom_tools.storybook import video_run_config

logger = logging.getLogger(__name__)

# провайдеры, у которых есть журнал задач provider_jobs.json и проверка бюджета
BUDGET_PROVIDERS = frozenset({"routerai", "aitunnel"})

# параметр -> что именно пользователь потерял бы, если бы его выбросили молча
_OPTION_LABELS = {
    "shot_keys": "перегенерация отдельных шотов (shot_keys)",
    "sample_before_batch": "пробный шот (sample_before_batch)",
    "sample_shot_key": "выбор пробного шота (sample_shot_key)",
    "generate_blockout": "слой болванок (generate_blockout)",
    "use_blockout_reference": "опорное видео болванки (use_blockout_reference)",
    "retry_cycles": "повторы с резервной моделью (retry_cycles)",
    "fallback_model": "резервная модель (fallback_model)",
}


def _is_default(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, str) and not value.strip())


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _budget(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _refuse(provider: str, code: str, message: str) -> Dict[str, Any]:
    return {"status": "error", "message": message, "error": message, "code": code,
            "results": [], "provider": provider, "dispatch": True}


def video_generator_dispatch_tool(
    session_id: str,
    project_id: Optional[str] = None,
    video_provider: str = "",
    items: Any = None,
    max_concurrency: Optional[int] = None,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = "en",
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False,
    sample_before_batch: bool = False,
    sample_shot_key: Optional[str] = None,
    generate_blockout: bool = False,
    use_blockout_reference: bool = False,
    shot_keys: Optional[list] = None,
    retry_cycles: Optional[int] = None,
    fallback_model: Optional[str] = None,
    budget_rub: Any = 0,
) -> Dict[str, Any]:
    """Сгенерировать видео через выбранного провайдера (routerai, aitunnel, minimax, veo).

    Args:
        session_id: Идентификатор сессии выполнения.
        project_id: Идентификатор проекта storybook.
        video_provider: Провайдер; пусто = настройка проекта, STORYBOOK_VIDEO_PROVIDER, routerai.
        items: Кадры, если нет сохранённого shots.json.
        max_concurrency: Максимум параллельных запросов (пусто = значение по умолчанию провайдера).
        enable: Генерировать ли видео.
        seed: Сид генерации.
        language: Язык проекта.
        force_update_prompts: Принудительно обновить video_prompt.
        skip_prompt_enhancement: Не улучшать промпт LLM.
        sample_before_batch: Обработать только один пробный шот (нет у veo/minimax).
        sample_shot_key: Ключ пробного шота scene-shot.
        generate_blockout: Включён ли слой болванок проекта.
        use_blockout_reference: Подавать ли ролик болванки видео-референсом (нет у veo/minimax).
        shot_keys: Ограничить обработку указанными шотами (нет у veo/minimax).
        retry_cycles: Циклы основной и резервной модели.
        fallback_model: Резервная модель.
        budget_rub: Запасной денежный бюджет, если нет файла настроек проекта; 0 = без ограничения.
    """
    video_run_config.bind_run_project(project_id)
    provider = video_run_config.effective_provider(project_id, explicit=video_provider)
    tool_name = video_run_config.tool_name_for(provider)
    if tool_name is None:
        return _refuse(provider, "unknown_provider",
                       f"Неизвестный провайдер видео: {provider or '(пусто)'}. "
                       f"Доступны: {', '.join(video_run_config.PROVIDER_TOOLS)}.")

    if provider not in BUDGET_PROVIDERS:
        stored = video_run_config.stored_budget_rub_at(_project_dir(project_id)) if project_id else None
        if (stored if stored is not None else _budget(budget_rub)) > 0:
            return _refuse(provider, "budget_unsupported_provider",
                           f"Провайдер {provider} не поддерживает денежный бюджет: "
                           "у него нет журнала задач. Поставьте бюджет 0 или выберите routerai/aitunnel.")

    module = importlib.import_module(f"custom_tools.storybook.{tool_name}")
    target = getattr(module, tool_name)
    accepted = inspect.signature(target).parameters
    kwargs: Dict[str, Any] = dict(
        session_id=session_id, project_id=project_id, items=items, enable=enable, seed=seed,
        language=language, force_update_prompts=force_update_prompts,
        skip_prompt_enhancement=skip_prompt_enhancement, sample_before_batch=sample_before_batch,
        sample_shot_key=sample_shot_key, generate_blockout=generate_blockout,
        use_blockout_reference=use_blockout_reference, shot_keys=shot_keys,
        retry_cycles=retry_cycles, fallback_model=fallback_model,
    )
    if max_concurrency is not None:
        kwargs["max_concurrency"] = max_concurrency

    unsupported = []
    for name in list(kwargs):
        if name in accepted:
            continue
        value = kwargs.pop(name)
        if _is_default(value):
            continue
        # слой болванок сам по себе ничего не меняет в запросе: важно только опорное видео
        if name == "generate_blockout" and not _as_bool(use_blockout_reference):
            continue
        unsupported.append(_OPTION_LABELS.get(name, name))
    if unsupported:
        return _refuse(provider, "provider_option_unsupported",
                       f"Провайдер {provider} не поддерживает: {', '.join(unsupported)}.")

    if project_id and provider in BUDGET_PROVIDERS:
        # запасной бюджет на случай запуска не из студии (файла настроек нет)
        video_run_config.set_run_budget_fallback(project_id, _budget(budget_rub))

    result = target(**kwargs)
    if isinstance(result, dict):
        result = dict(result, provider=provider, dispatch=True)
    return result


def _project_dir(project_id: str):
    from custom_tools.storybook.project_paths import safe_storybook_project_dir
    return safe_storybook_project_dir(project_id)
