"""Цены на платную генерацию: таблица `config/storybook/video_pricing.yaml`, окружение, факт.

Цены в репозиторий не придумываем: таблица поставляется с пустыми значениями.
Цена неизвестна -> `None`, а не 0: «неизвестно» нельзя выдавать за «бесплатно».
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "storybook" / "video_pricing.yaml"
_ENV_PATH_VAR = "STORYBOOK_VIDEO_PRICING_PATH"

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _pricing_path() -> Path:
    configured = (os.getenv(_ENV_PATH_VAR) or "").strip()
    return Path(configured) if configured else PRICING_PATH


def load_pricing() -> tuple[dict, Optional[str]]:
    """Таблица цен и ошибка чтения (`None`, если всё в порядке). Кэш по mtime."""
    path = _pricing_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return {}, f"файл цен недоступен: {exc}"
    key = str(path)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("video_pricing.yaml не разобран: %s", exc)
        return {}, f"файл цен не разобран: {exc}"
    if not isinstance(data, dict):
        return {}, "файл цен не является словарём"
    with _cache_lock:
        _cache[key] = (mtime, data)
    return data, None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _from_record(record: Any) -> Optional[dict]:
    if not isinstance(record, dict):
        return None
    per_second = _number(record.get("per_second_rub"))
    if per_second is not None:
        return {"unit": "per_second", "rub": per_second}
    per_clip = _number(record.get("per_clip_rub"))
    if per_clip is not None:
        return {"unit": "per_clip", "rub": per_clip}
    return None


def table_price(provider: str, model: str, resolution: str = "") -> Optional[dict]:
    data, _ = load_pricing()
    video = data.get("video")
    models = ((video or {}).get(provider) or {}).get("models") if isinstance(video, dict) else None
    record = (models or {}).get(model) if isinstance(models, dict) else None
    if not isinstance(record, dict):
        return None
    by_resolution = record.get("by_resolution")
    if resolution and isinstance(by_resolution, dict):
        found = _from_record(by_resolution.get(resolution))
        if found:
            return found
    return _from_record(record)


def env_price(provider: str) -> Optional[dict]:
    """`<ПРОВАЙДЕР>_VIDEO_COST_RUB` / `STORYBOOK_VIDEO_COST_RUB`: допущение «рублей за ролик»."""
    value = (os.getenv(f"{provider.upper()}_VIDEO_COST_RUB") or os.getenv("STORYBOOK_VIDEO_COST_RUB") or "").strip()
    try:
        number = float(value)
    except ValueError:
        return None
    return {"unit": "per_clip", "rub": number} if number >= 0 else None


def observed_price(jobs: list) -> Optional[dict]:
    """Средняя фактическая цена ролика по оплаченным задачам проекта (валюта RUB)."""
    costs = [
        float(job["cost"]) for job in jobs
        if isinstance(job, dict) and not isinstance(job.get("cost"), bool)
        and isinstance(job.get("cost"), (int, float)) and job["cost"] > 0
        and str(job.get("currency") or "").upper() == "RUB"
        and job.get("status") in {"completed", "downloading", "downloaded", "download_failed"}
    ]
    if not costs:
        return None
    return {"unit": "per_clip", "rub": sum(costs) / len(costs)}


def unit_price(provider: str, model: str, resolution: str = "", jobs: Optional[list] = None,
               allow_observed: bool = True) -> dict:
    """Цена видео: таблица -> окружение -> фактическая -> неизвестна.

    Возвращает `{"unit": "per_second"|"per_clip"|None, "rub": float|None, "source": ...}`.
    """
    for source, found in (("pricing_file", table_price(provider, model, resolution)),
                          ("env", env_price(provider)),
                          ("observed", observed_price(jobs or []) if allow_observed else None)):
        if found:
            return {**found, "source": source}
    return {"unit": None, "rub": None, "source": "none"}


def clip_price(price: dict, duration_s: Any) -> Optional[float]:
    """Цена одного ролика заданной длительности; `None`, если цена или длительность неизвестны."""
    if price.get("rub") is None:
        return None
    if price.get("unit") == "per_clip":
        return float(price["rub"])
    try:
        seconds = float(duration_s)
    except (TypeError, ValueError):
        return None
    return float(price["rub"]) * seconds if seconds > 0 else None
