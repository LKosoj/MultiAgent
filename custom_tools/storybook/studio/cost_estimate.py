"""Оценка стоимости прогона: чтение данных проекта, без сети и без расходов.

Правило: неизвестная цена остаётся `None`, а не 0. В сумму идут только известные
части, рядом всегда `complete` и список `unknown_components`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook import video_budget, video_pricing
from custom_tools.storybook.project_paths import safe_storybook_project_dir

from . import provider_catalog

logger = logging.getLogger(__name__)

VIDEO_STEP = "video_generator"
IMAGE_STEPS = ("artist_batch", "character_reference_states", "background_references", "artist_batch_shots")
MUSIC_STEP = "storybook_music_generator"
SFX_STEP = "storybook_sfx_generator"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DONE = {"downloaded", "completed"}
_DEFAULT_SCREENPLAY_S = 120


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _wanted(step_ids: Optional[list], *steps: str) -> bool:
    return step_ids is None or any(step in step_ids for step in steps)


def _file_exists(raw: Any) -> bool:
    if not raw:
        return False
    path = Path(str(raw))
    return path.is_file() or (not path.is_absolute() and (_REPO_ROOT / path).is_file())


def _seconds(item: dict) -> Optional[float]:
    try:
        if item.get("duration_s"):
            return float(item["duration_s"])
        from custom_tools.storybook.video_generator_common import parse_duration_seconds_from_timing
        return float(parse_duration_seconds_from_timing(item.get("timing") or "5s"))
    except Exception:
        return None


def _start_items(project_id: str) -> tuple[Optional[list], Optional[str]]:
    """Шоты с учётом video_plan; `None`, если раскадровки ещё нет."""
    base = safe_storybook_project_dir(project_id)
    if not (base / "97_shots" / "shots.json").is_file():
        return None, None
    try:
        from custom_tools.storybook.video_contract import _load_shots
        _, items, error = _load_shots(project_id)
    except Exception as exc:  # битый план не должен ронять оценку
        return None, f"video_plan не применён: {exc}"
    if error:
        return None, error
    return [i for i in items if isinstance(i, dict) and i.get("shot_type", "start") == "start"], None


def _fit(seconds: Optional[float], supported: list, allowed: list) -> tuple[Optional[float], bool]:
    """Длительность, подогнанная под модель: (секунды, точно ли)."""
    if seconds is None:
        return None, False
    pool = [int(v) for v in supported if isinstance(v, (int, float))]
    if pool and allowed:
        narrowed = [v for v in pool if v in allowed]
        pool = narrowed or pool
    if not pool:
        return seconds, False
    pool.sort()
    best = min(pool, key=lambda v: (abs(v - seconds), v > seconds, v))
    return float(best), True


def _video_component(project_id: str, values: dict, shot_keys: Optional[list], warnings: list) -> dict:
    provider, model = values["provider"], values.get("model") or ""
    resolution = values.get("resolution") or ""
    component = {"id": "video", "title": "Видео", "units": None, "unit": "ролик", "seconds": None,
                 "unit_price_rub": None, "price_unit": None, "amount_rub": None,
                 "price_source": "none", "basis": "unknown", "already_done": 0}
    info = provider_catalog.provider_info(provider, project_id)
    model = model or info.get("configured_model") or ""
    entry = next((m for m in info["models"] if m["id"] == model), {})
    supported = entry.get("supported_durations") or []
    allowed = list(values.get("allowed_durations") or [])
    jobs_payload = _read_json(safe_storybook_project_dir(project_id) / "97_shots" / "provider_jobs.json")
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else jobs_payload
    jobs = [j for j in (jobs or []) if isinstance(j, dict)]
    price = video_pricing.unit_price(provider, model, resolution, jobs)
    component["price_unit"], component["price_source"] = price["unit"], price["source"]
    component["unit_price_rub"] = price["rub"]

    items, error = _start_items(project_id)
    if error:
        warnings.append({"code": "shots_unreadable", "message": f"Раскадровка не прочитана: {error}"})
    if items is None:
        screenplay = (_read_json(safe_storybook_project_dir(project_id) / "00_brief.json") or {}).get("screenplay_time")
        try:
            total = float(screenplay) if screenplay else float(_DEFAULT_SCREENPLAY_S)
        except (TypeError, ValueError):
            total = float(_DEFAULT_SCREENPLAY_S)
        typical = sorted(supported)[len(supported) // 2] if supported else 5
        count = max(1, round(total / typical))
        component.update(units=count, seconds=count * float(typical), basis="rough")
        warnings.append({"code": "shots_not_built",
                         "message": "Раскадровка ещё не построена: число роликов прикинуто по длительности фильма."})
        amount_items = [float(typical)] * count
        done = 0
    else:
        only = set(shot_keys) if shot_keys else None
        amount_items, done, exact, unknown_seconds = [], 0, True, 0
        for item in items:
            key = f"{item.get('scene_number')}-{item.get('shot_number')}"
            if only is not None and key not in only:
                continue
            if only is None and any(
                j.get("shot_key") == key and j.get("status") in _DONE
                and (not model or j.get("model") == model) and _file_exists(j.get("output_path"))
                for j in jobs
            ):
                done += 1
                continue
            seconds, fitted = _fit(_seconds(item), supported, allowed)
            exact = exact and fitted
            if seconds is None:
                # длительность шота неизвестна: в сумму не берём (не считаем как 0 секунд)
                unknown_seconds += 1
                continue
            amount_items.append(seconds)
        component.update(units=len(amount_items) + unknown_seconds, seconds=sum(amount_items),
                         basis="exact" if exact and not done else "rough", already_done=done)
        if unknown_seconds:
            component["incomplete"] = True
            component["unknown_duration_shots"] = unknown_seconds
            warnings.append({"code": "duration_unknown",
                             "message": f"У {unknown_seconds} шотов не указана длительность: в сумму они не вошли."})
        if done:
            component["note"] = f"{done} роликов уже готовы и повторно не оплачиваются"
            warnings.append({"code": "done_not_recounted",
                             "message": "Если вы меняли промпты или кадры, часть готовых роликов будет создана заново."})
        if not exact:
            warnings.append({"code": "duration_unfitted",
                             "message": "Возможности модели неизвестны: длительности взяты как в раскадровке."})
    if price["rub"] is not None and amount_items:
        total = 0.0
        for seconds in amount_items:
            clip = video_pricing.clip_price(price, seconds)
            total += clip or 0.0
        component["amount_rub"] = total
    elif component["units"] == 0:
        component["amount_rub"] = 0.0
    if price["source"] in ("env", "observed"):
        # цена не из таблицы: это допущение, а не точная цена (решение Ф5-1)
        component["basis"] = "rough"
        assumption = "допущение: рублей за ролик"
        component["note"] = f"{component['note']}; {assumption}" if component.get("note") else assumption
    return component


def _flat_price(section: str, field: str) -> Optional[float]:
    data, _ = video_pricing.load_pricing()
    block = data.get(section)
    value = block.get(field) if isinstance(block, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _flat_component(cid: str, title: str, unit: str, units: Optional[int], section: str, field: str,
                    basis: str, note: str = "") -> dict:
    price = _flat_price(section, field)
    amount = price * units if price is not None and units is not None else None
    result = {"id": cid, "title": title, "units": units, "unit": unit, "unit_price_rub": price,
              "price_unit": "per_item", "amount_rub": amount,
              "price_source": "pricing_file" if price is not None else "none",
              "basis": basis if units is not None else "unknown"}
    if note:
        result["note"] = note
    return result


def _count_music(project_id: str) -> int:
    plan = _read_json(safe_storybook_project_dir(project_id) / "98_audio" / "music_plan.json")
    if isinstance(plan, dict) and isinstance(plan.get("leitmotifs"), dict):
        return len(plan["leitmotifs"]) + (1 if plan.get("neutral") else 0)
    return 1


def _count_sfx(project_id: str) -> Optional[int]:
    data = _read_json(safe_storybook_project_dir(project_id) / "91_screenplay" / "screenplay.json")
    scenes = data.get("screenplay") if isinstance(data, dict) else data
    if not isinstance(scenes, list):
        return None
    return sum(1 for s in scenes if isinstance(s, dict) and s.get("sfx"))


def build_estimate(project_id: str, values: dict, step_ids: Optional[list] = None,
                   shot_keys: Optional[list] = None) -> dict:
    """Оценка по уже вычисленным действующим значениям настроек (`run_settings.effective_values`)."""
    warnings: list[dict] = []
    components: list[dict] = []
    if _wanted(step_ids, VIDEO_STEP):
        components.append(_video_component(project_id, values, shot_keys, warnings))
    if _wanted(step_ids, *IMAGE_STEPS):
        data = _read_json(safe_storybook_project_dir(project_id) / "97_shots" / "shots.json")
        raw = data.get("items") if isinstance(data, dict) else data
        count = sum(1 for i in raw if isinstance(i, dict)) if isinstance(raw, list) else None
        components.append(_flat_component("images", "Картинки", "кадр", count, "images", "per_image_rub", "exact"))
    if values.get("music", True) and _wanted(step_ids, MUSIC_STEP):
        components.append(_flat_component("music", "Музыка", "дорожка", _count_music(project_id),
                                          "music", "per_track_rub", "rough"))
    if values.get("sfx", True) and _wanted(step_ids, SFX_STEP):
        components.append(_flat_component("sfx", "Эффекты", "эффект", _count_sfx(project_id),
                                          "sfx", "per_effect_rub", "rough"))
    components.append({"id": "text_llm", "title": "Текстовые модели", "units": None, "amount_rub": None,
                       "price_source": "none", "basis": "unknown",
                       "note": "Затраты на текстовые модели не оцениваются"})
    known = [c["amount_rub"] for c in components if c.get("amount_rub") is not None]
    unknown = [c["id"] for c in components if c.get("amount_rub") is None or c.get("incomplete")]
    total = float(sum(known))
    complete = not unknown
    if unknown:
        warnings.append({"code": "price_unknown",
                         "message": "Цена неизвестна для: " + ", ".join(unknown) + ". Сумма включает только известное."})
    budget = float(values.get("budget_rub") or 0)
    # как охранник бюджета: уже потраченное плюс то, что осталось оплатить
    spent = video_budget.spent_rub(video_budget._read_jobs(safe_storybook_project_dir(project_id)))["total"]
    if budget <= 0:
        verdict, short_by = "no_budget", None
    elif spent + total > budget + 1e-9:
        verdict, short_by = "short", spent + total - budget
    elif not [u for u in unknown if u != "text_llm"]:
        # бюджет охраняет только платные видео-задачи; текстовые модели на вердикт не влияют (решение Ф3 п.4)
        verdict, short_by = "enough", None
    else:
        verdict, short_by = "cannot_check", None
    result = {
        "currency": "RUB",
        "provider": values["provider"],
        "model": values.get("model") or None,
        "components": components,
        "total_known_rub": total,
        "complete": complete,
        "unknown_components": unknown,
        "budget_rub": budget,
        "spent_rub": spent,
        "budget_verdict": verdict,
        "warnings": warnings,
    }
    if short_by is not None:
        result["short_by_rub"] = short_by
    return result
