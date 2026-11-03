"""Каталог видео-провайдеров: что выбрать, чего не хватает и какие у провайдера ограничения.

Значения ключей наружу не попадают никогда: только булево и имена переменных.
Сеть вызывается только при `refresh_remote=True`; сбой сети — предупреждение, не ошибка.
Ограничения провайдера считаются сравнением параметров функции инструмента с
эталоном routerai, поэтому список не устаревает.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Optional

from custom_tools.storybook import video_pricing, video_run_config

logger = logging.getLogger(__name__)

REMOTE_TIMEOUT_S = 15

FULL_PARAMS = ("shot_keys", "sample_before_batch", "sample_shot_key", "generate_blockout",
               "use_blockout_reference", "retry_cycles", "fallback_model")
LIMITATION_LABELS = {
    "shot_keys": "нет перегенерации отдельных шотов",
    "sample_before_batch": "нет пробного шота",
    "sample_shot_key": "нет выбора пробного шота",
    "generate_blockout": "нет слоя болванок",
    "use_blockout_reference": "нет опорного видео болванки",
    "retry_cycles": "нет повторов с резервной моделью",
    "fallback_model": "нет резервной модели",
}
# повторы и резервная модель — одна возможность для человека
_LABEL_GROUPS = {"нет повторов с резервной моделью", "нет резервной модели"}

_ENV_NAMES = {  # имена переменных, которые нужны провайдеру (значения не читаем в ответ)
    "routerai": ["ROUTERAI_API_KEY", "ROUTERAI_VIDEO_MODEL"],
    "aitunnel": ["AITUNNEL_API_KEY", "AITUNNEL_VIDEO_MODEL"],
    "minimax": ["MINIMAX_API_KEY"],
    "kling": ["KLING_API_KEY", "KLING_API_SECRET_KEY"],
    "veo": ["GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY"],
}

# id, название, имя инструмента, выбирается ли в окне
PROVIDERS = (
    {"id": "routerai", "title": "RouterAI", "tool_name": "video_generator_routerai_tool", "selectable": True},
    {"id": "aitunnel", "title": "AITunnel", "tool_name": "video_generator_aitunnel_tool", "selectable": True},
    {"id": "minimax", "title": "MiniMax", "tool_name": "video_generator_mm_tool", "selectable": True},
    {"id": "veo", "title": "Google Veo", "tool_name": "video_generator_veo_tool", "selectable": True},
    {"id": "kling", "title": "Kling", "tool_name": "video_generator_tool", "selectable": False},
)


def _has_env(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def _configured(provider: str, project_id: Any) -> bool:
    if provider in ("routerai", "aitunnel"):
        key = f"{provider.upper()}_API_KEY"
        return _has_env(key) and bool(video_run_config.video_setting("VIDEO_MODEL", provider, project_id))
    if provider == "minimax":
        return _has_env("MINIMAX_API_KEY")
    if provider == "kling":
        return _has_env("KLING_API_KEY") and _has_env("KLING_API_SECRET_KEY")
    if provider == "veo":
        return _has_env("GOOGLE_CLOUD_PROJECT") or _has_env("GEMINI_API_KEY")
    return False


def _missing_env(provider: str, project_id: Any) -> list[str]:
    if provider in ("routerai", "aitunnel"):
        missing = [] if _has_env(f"{provider.upper()}_API_KEY") else [f"{provider.upper()}_API_KEY"]
        if not video_run_config.video_setting("VIDEO_MODEL", provider, project_id):
            missing.append(f"{provider.upper()}_VIDEO_MODEL")
        return missing
    if _configured(provider, project_id):
        return []
    return [name for name in _ENV_NAMES[provider] if not _has_env(name)] if provider in ("minimax", "kling") \
        else list(_ENV_NAMES[provider])


def _params_from_source(tool_name: str) -> Optional[set]:
    """Параметры функции инструмента по исходнику, если модуль не импортируется."""
    path = Path(__file__).resolve().parents[1] / f"{tool_name}.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == tool_name:
            args = node.args
            return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    return None


def limitations_for(tool_name: str) -> tuple[str, list[str], Optional[str]]:
    """(`full`/`limited`, список ограничений по-русски, ошибка импорта)."""
    import_error = None
    try:
        module = importlib.import_module(f"custom_tools.storybook.{tool_name}")
        params = set(inspect.signature(getattr(module, tool_name)).parameters)
    except Exception as exc:  # тяжёлые зависимости не должны ронять каталог
        logger.info("Каталог провайдеров: %s не импортирован (читаем сигнатуру из исходника): %s", tool_name, exc)
        import_error = type(exc).__name__
        params = _params_from_source(tool_name)
        if params is None:
            return "limited", [], import_error
    missing = [name for name in FULL_PARAMS if name not in params]
    labels: list[str] = []
    for name in missing:
        label = LIMITATION_LABELS[name]
        if label in _LABEL_GROUPS:
            label = "нет повторов с резервной моделью"
        if label not in labels:
            labels.append(label)
    return ("full" if not missing else "limited"), labels, import_error


def _common():
    from custom_tools.storybook import video_generator_common as common
    return common


def _cached_remote(provider: str) -> Optional[dict]:
    try:
        common = _common()
    except Exception:
        return None
    if provider == "routerai":
        return getattr(common, "_ROUTERAI_MODELS_CACHE", None)
    if provider == "aitunnel":
        return getattr(common, "_AITUNNEL_MODELS_CACHE", None)
    return None


def _static_models(provider: str) -> dict:
    try:
        common = _common()
    except Exception:
        return {}
    if provider == "routerai":
        return {name: dict(caps) for name, caps in common._ROUTERAI_MODEL_CAPABILITIES.items()}
    if provider == "aitunnel":
        return {name: dict(caps) for name, caps in common._AITUNNEL_MODEL_CAPABILITIES.items()}
    if provider in ("minimax", "veo", "kling"):
        entry = common._VIDEO_MODEL_CAPABILITIES_REGISTRY.get(
            next(p["tool_name"] for p in PROVIDERS if p["id"] == provider))
        return {entry["model"]: dict(entry)} if entry else {}
    return {}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _model_entry(provider: str, model_id: str, record: dict, source: str) -> dict:
    refs = record.get("max_image_references")
    price = video_pricing.unit_price(provider, model_id, allow_observed=False)
    return {
        "id": model_id,
        "supported_durations": _list(record.get("supported_durations")),
        "supported_resolutions": _list(record.get("supported_resolutions")),
        "supported_aspect_ratios": _list(record.get("supported_aspect_ratios")),
        "supported_frame_images": _list(record.get("supported_frame_images")),
        "max_image_references": refs if isinstance(refs, int) and not isinstance(refs, bool) else None,
        "source": source,
        "price": {"unit": price["unit"], "rub": price["rub"], "source": price["source"]},
    }


def _fetch_remote(providers: list[str]) -> tuple[dict, list[dict]]:
    """Скачать каталоги в потоках с общим ограничением по времени; ошибки -> предупреждения."""
    fetchers: dict[str, Callable[[], dict]] = {}
    common = _common()
    if "routerai" in providers:
        fetchers["routerai"] = lambda: common._get_routerai_video_models(force_refresh=True)
    if "aitunnel" in providers:
        fetchers["aitunnel"] = lambda: common._get_aitunnel_video_models(force_refresh=True)
    result: dict = {}
    warnings: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=max(len(fetchers), 1))
    try:
        futures = {name: pool.submit(fn) for name, fn in fetchers.items()}
        for name, future in futures.items():
            try:
                result[name] = future.result(timeout=REMOTE_TIMEOUT_S)
            except FutureTimeout:
                warnings.append({"code": "remote_catalog_unavailable",
                                 "message": f"Каталог {name} не ответил за {REMOTE_TIMEOUT_S} с: показан список без сети."})
            except Exception as exc:
                logger.warning("Каталог %s недоступен: %s", name, exc)
                warnings.append({"code": "remote_catalog_unavailable",
                                 "message": f"Каталог {name} недоступен: показан список без сети."})
    finally:
        pool.shutdown(wait=False)
    return result, warnings


def provider_info(provider: str, project_id: Any = "", remote: Optional[dict] = None) -> dict:
    meta = next(p for p in PROVIDERS if p["id"] == provider)
    support, limitations, import_error = limitations_for(meta["tool_name"])
    models: dict[str, dict] = {}
    for name, record in _static_models(provider).items():
        models[name] = _model_entry(provider, name, record, "static" if provider in ("routerai", "aitunnel") else "registry")
    network = remote if remote is not None else _cached_remote(provider)
    if isinstance(network, dict):
        for name, record in network.items():
            if isinstance(record, dict):
                merged = {**record, **(_static_models(provider).get(name) or {})}
                models[name] = _model_entry(provider, name, merged, "network")
    configured_model = video_run_config.video_setting("VIDEO_MODEL", provider, project_id) \
        if provider in ("routerai", "aitunnel") else ""
    if not configured_model and provider in ("minimax", "veo", "kling"):
        configured_model = next(iter(models), "")
    if configured_model and configured_model not in models:
        models[configured_model] = _model_entry(provider, configured_model, {}, "env")
    info = {
        "id": provider,
        "title": meta["title"],
        "tool_name": meta["tool_name"],
        "selectable": meta["selectable"],
        "pipeline_support": support,
        "limitations": limitations,
        "credentials_found": _configured(provider, project_id),
        "missing_env": _missing_env(provider, project_id),
        "required_env": list(_ENV_NAMES[provider]),
        "budget_supported": provider in ("routerai", "aitunnel"),
        "settings_honored": {
            "resolution": provider == "routerai",
            "fallback_model": "нет резервной модели" not in limitations,
            "retry_cycles": "нет повторов с резервной моделью" not in limitations,
        },
        "models": list(models.values()),
        "configured_model": configured_model or None,
        "catalog_status": "network" if isinstance(network, dict) else "static_fallback",
    }
    if import_error:
        info["import_error"] = import_error
    return info


def build_catalog(refresh_remote: bool = False, project_id: Any = "") -> dict:
    ids = [p["id"] for p in PROVIDERS]
    remote: dict = {}
    warnings: list[dict] = []
    if refresh_remote:
        try:
            remote, warnings = _fetch_remote(["routerai", "aitunnel"])
        except Exception as exc:
            logger.warning("Обновление каталогов не выполнено: %s", exc)
            warnings.append({"code": "remote_catalog_unavailable",
                             "message": "Каталоги провайдеров недоступны: показан список без сети."})
    providers = [provider_info(pid, project_id, remote.get(pid)) for pid in ids]
    selected = video_run_config.effective_provider(project_id or None) if project_id else None
    return {"providers": providers, "selected_provider": selected, "warnings": warnings}
