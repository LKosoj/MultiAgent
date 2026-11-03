"""Каталог стилей для экрана настроек прогона (без декораторов: возможность в run_settings.py)."""

from __future__ import annotations

import logging
from typing import Any

from custom_tools.storybook.style_library_config import (
    load_style_library_config,
    resolve_active_style_profile,
)

logger = logging.getLogger(__name__)


def _summary(preset: Any) -> str:
    """Короткое описание для подсказки: когда стиль уместен, иначе материал."""
    text = (preset.when_to_use or preset.material_signature or preset.art_style or "").strip()
    return text.splitlines()[0].strip() if text else ""


def build_style_catalog() -> dict:
    """`{profiles: [{id, presets: [{id, title, summary}]}], active_profile}`.

    Ошибка чтения yaml пробрасывается вызывающему (он превращает её в `unavailable`).
    """
    config = load_style_library_config()
    profiles = [
        {
            "id": name,
            "fallback_preset_id": profile.fallback_preset_id,
            "presets": [
                {"id": preset.id, "title": preset.title, "summary": _summary(preset)}
                for preset in profile.presets.values()
            ],
        }
        for name, profile in config.profiles.items()
    ]
    return {"profiles": profiles, "active_profile": resolve_active_style_profile(None)}


def profile_presets(profile_name: str) -> list[str] | None:
    """Id пресетов профиля; `None`, если профиля нет."""
    config = load_style_library_config()
    profile = config.profiles.get(profile_name)
    return profile.preset_ids() if profile is not None else None
