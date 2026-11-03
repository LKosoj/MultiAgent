"""Форма «Генерации»: правка настроек прогона и полей брифа (без tkinter).

Повторяет lib/storybook/generationForm.ts. Все функции возвращают новые объекты.
"""

import copy
import re
from typing import Dict, List, Optional, Tuple

from StoryBookManager.gui.generation.texts import fmt


def clone(settings: dict) -> dict:
    return copy.deepcopy(settings)


def is_dirty(saved: Optional[dict], draft: Optional[dict]) -> bool:
    return bool(saved) and bool(draft) and saved != draft


def patched(settings: dict, section: str, key: str, value) -> dict:
    out = clone(settings)
    out.setdefault(section, {})[key] = value
    return out


def patch_budget(settings: dict, budget) -> dict:
    out = clone(settings)
    out["budget_rub"] = budget
    return out


def change_provider(settings: dict, provider: Optional[str]) -> dict:
    """Смена провайдера сбрасывает модель и разрешение: у другого они могут не подойти."""
    out = clone(settings)
    out.setdefault("video", {}).update(provider=provider, model=None, fallback_model=None,
                                       resolution=None, allowed_durations=None)
    return out


# ---- бюджет

def parse_budget(text: str) -> Tuple[Optional[float], Optional[str]]:
    """(значение, ошибка); пусто - без ограничения."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None, None
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None, fmt("budget_invalid")
    value = float(t)
    return (int(value) if value.is_integer() else value), None


def budget_text(value) -> str:
    if value is None:
        return ""
    return str(value).replace(".", ",")


# ---- списки выбора

def provider_options(catalog: Optional[dict]) -> List[dict]:
    out = []
    for p in (catalog or {}).get("providers") or []:
        title = f"{p.get('title')} (ограничено)" if p.get("pipeline_support") == "limited" else p.get("title")
        out.append({"value": p.get("id"), "label": title, "disabled": not p.get("selectable", True)})
    return out


def active_provider(catalog: Optional[dict], settings: Optional[dict]) -> Optional[dict]:
    """Провайдер, который реально будет использован: выбранный в настройках, иначе заданный сервером."""
    if not catalog:
        return None
    ident = ((settings or {}).get("video") or {}).get("provider") or catalog.get("selected_provider")
    return next((p for p in catalog.get("providers") or [] if p.get("id") == ident), None)


def active_model(provider: Optional[dict], settings: Optional[dict]) -> Optional[dict]:
    if not provider:
        return None
    models = provider.get("models") or []
    ident = ((settings or {}).get("video") or {}).get("model") or provider.get("configured_model")
    return next((m for m in models if m.get("id") == ident), None) or (models[0] if models else None)


def model_ids(provider: Optional[dict]) -> List[str]:
    return [m.get("id") for m in (provider or {}).get("models") or []]


def resolution_ids(model: Optional[dict]) -> List[str]:
    return list((model or {}).get("supported_resolutions") or [])


def duration_choices(model: Optional[dict]) -> List[int]:
    return sorted((model or {}).get("supported_durations") or [])


def toggle_duration(current: Optional[List[int]], every: List[int], value: int) -> Optional[List[int]]:
    """None в allowed_durations значит «любые»; снятие галочки делает список явным."""
    base = list(current) if current is not None else list(every)
    nxt = [d for d in base if d != value] if value in base else sorted(base + [value])
    return None if sorted(nxt) == sorted(every) else nxt


def is_duration_on(current: Optional[List[int]], value: int) -> bool:
    return current is None or value in current


def style_groups(catalog: Optional[dict]) -> List[dict]:
    return [{"profile": p.get("id"),
             "options": [{"value": f"{p.get('id')}|{x.get('id')}", "label": x.get("title"), "summary": x.get("summary")}
                         for x in p.get("presets") or []]}
            for p in (catalog or {}).get("profiles") or []]


def style_value(settings: dict) -> str:
    style = settings.get("style") or {}
    return f"{style['profile']}|{style['preset']}" if style.get("profile") and style.get("preset") else ""


def with_style(settings: dict, value: str) -> dict:
    out = clone(settings)
    if not value:
        out["style"] = {"profile": None, "preset": None}
    else:
        profile, _, preset = value.partition("|")
        out["style"] = {"profile": profile or None, "preset": preset or None}
    return out


def source_hint(effective: Optional[dict], key: str) -> Optional[str]:
    """Откуда взято значение: из настроек, окружения сервера или по умолчанию."""
    item = (effective or {}).get(key)
    if not item:
        return None
    return fmt({"setting": "source_setting", "env": "source_env"}.get(item.get("source"), "source_default"))


# ---- поля брифа

BRIEF_FIELDS = (
    ("pages_min", "brief_pages_min", "int"),
    ("pages_max", "brief_pages_max", "int"),
    ("words_per_page_min", "brief_words_min", "int"),
    ("words_per_page_max", "brief_words_max", "int"),
    ("screenplay_time", "brief_screenplay_time", "int"),
    ("language", "brief_language", "text"),
    ("generate_end_shots", "brief_end_shots", "bool"),
)


def brief_value(draft: Dict, plan_inputs: Optional[dict], key: str):
    if key in draft:
        return draft[key]
    value = (plan_inputs or {}).get(key)
    if isinstance(value, bool):
        return value
    return "" if value is None else str(value)


def collect_inputs(draft: Dict, plan_inputs: Optional[dict]) -> Tuple[dict, Optional[str]]:
    """В запрос уходят только затронутые поля; ошибка - первая найденная."""
    inputs: dict = {}
    for key, label, kind in BRIEF_FIELDS:
        if key not in draft:
            continue
        raw = draft[key]
        if kind == "bool":
            inputs[key] = raw is True
        elif kind == "int":
            text = str(raw if raw is not None else "").strip()
            if not re.fullmatch(r"[1-9]\d*", text):
                return {}, fmt("field_int_positive", name=fmt(label))
            inputs[key] = int(text)
        else:
            inputs[key] = str(raw if raw is not None else "").strip()

    def pair(lo, hi, label):
        if lo not in inputs and hi not in inputs:
            return None
        try:
            a = float(inputs.get(lo, (plan_inputs or {}).get(lo)))
            b = float(inputs.get(hi, (plan_inputs or {}).get(hi)))
        except (TypeError, ValueError):
            return None
        return fmt("field_min_max", name=fmt(label)) if a > b else None

    error = pair("pages_min", "pages_max", "brief_pages_min") or pair("words_per_page_min", "words_per_page_max", "brief_words_min")
    return ({}, error) if error else (inputs, None)


def blockout_on(plan_inputs: Optional[dict]) -> bool:
    return (plan_inputs or {}).get("generate_blockout") is True
