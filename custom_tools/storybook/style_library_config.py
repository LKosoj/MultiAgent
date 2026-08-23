"""
Загрузчик каталога визуальных стилей для storybook-пайплайна.

Раньше ``style_keeper.py`` просил LLM выдумать art_style/palette/lighting/
texture с нуля: результат не воспроизводился между запусками и не содержал
признаков носителя (мазок, зерно, поры кожи), из-за чего картинка
сваливалась в усреднённый дефолт генератора.

Теперь source of truth — ``config/storybook/prompts/style_library.yaml``:
LLM выбирает ``style_preset_id`` из каталога, а проектная специфика
накладывается поверх пресета узким списком overrides.

Контракт:
  * Путь по умолчанию: ``config/storybook/prompts/style_library.yaml``.
  * Override пути: env ``STORYBOOK_STYLE_LIBRARY_PATH``.
  * Активный профиль: явный аргумент → env ``STORYBOOK_STYLE_PROFILE`` →
    ``default``.
  * Файл обязателен: нет файла → ``FileNotFoundError`` (no silent fallback).
  * Неизвестный профиль → ``KeyError``; неизвестный ``style_preset_id`` от
    LLM → ``fallback_preset_id`` профиля с предупреждением в лог.
  * Кэш по абсолютному пути; ``reset_cache()`` для тестов, меняющих env.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "storybook" / "prompts" / "style_library.yaml"
)

_ENV_PATH_VAR = "STORYBOOK_STYLE_LIBRARY_PATH"
_ENV_PROFILE_VAR = "STORYBOOK_STYLE_PROFILE"
_DEFAULT_PROFILE = "default"

_YAML_NAME = "style_library.yaml"

# Поля, которые LLM разрешено уточнять под конкретный проект. Всё остальное
# (art_style, texture, material_signature, camera_defaults, pitfalls) держит
# пресет — иначе каталог теряет смысл и стиль снова «плывёт» между запусками.
_OVERRIDABLE_FIELDS = (
    "color_palette",
    "composition_rules",
    "lighting",
    "detail_density",
    "do_not_include",
    "model",
    "project_note",
)


class StylePreset:
    """Один визуальный стиль из каталога."""

    __slots__ = (
        "id",
        "title",
        "when_to_use",
        "art_style",
        "color_palette",
        "composition_rules",
        "lighting",
        "texture",
        "detail_density",
        "material_signature",
        "camera_defaults",
        "negative_seed",
        "pitfalls",
    )

    def __init__(self, body: Dict[str, Any], field: str) -> None:
        self.id = _coerce_str(body.get("id"), f"{field}.id")
        self.title = _coerce_str(body.get("title"), f"{field}.title")
        self.when_to_use = _coerce_str(body.get("when_to_use"), f"{field}.when_to_use")
        self.art_style = _coerce_str(body.get("art_style"), f"{field}.art_style")
        self.color_palette = _coerce_str_list(
            body.get("color_palette"), f"{field}.color_palette"
        )
        self.composition_rules = _coerce_str_list(
            body.get("composition_rules"), f"{field}.composition_rules"
        )
        self.lighting = _coerce_str(body.get("lighting"), f"{field}.lighting")
        self.texture = _coerce_str(body.get("texture"), f"{field}.texture")
        self.detail_density = _coerce_str(
            body.get("detail_density"), f"{field}.detail_density"
        )
        self.material_signature = _coerce_str(
            body.get("material_signature"), f"{field}.material_signature"
        )
        self.camera_defaults = _coerce_str_map(
            body.get("camera_defaults"), f"{field}.camera_defaults"
        )
        self.negative_seed = _coerce_str_list(
            body.get("negative_seed"), f"{field}.negative_seed"
        )
        self.pitfalls = _coerce_str_list(body.get("pitfalls"), f"{field}.pitfalls")

        if not self.color_palette:
            raise ValueError(f"{_YAML_NAME}: {field}.color_palette must be non-empty")
        if not self.negative_seed:
            raise ValueError(f"{_YAML_NAME}: {field}.negative_seed must be non-empty")


class StyleProfile:
    """Библиотека стилей одного профиля."""

    __slots__ = ("name", "fallback_preset_id", "universal_negative_seed",
                 "universal_pitfalls", "presets")

    def __init__(self, name: str, body: Dict[str, Any]) -> None:
        self.name = name
        field = f"profiles.{name}"

        raw_presets = body.get("presets")
        if not isinstance(raw_presets, list) or not raw_presets:
            raise ValueError(f"{_YAML_NAME}: {field}.presets must be a non-empty list")

        presets: Dict[str, StylePreset] = {}
        for idx, entry in enumerate(raw_presets):
            if not isinstance(entry, dict):
                raise ValueError(f"{_YAML_NAME}: {field}.presets[{idx}] must be a mapping")
            preset = StylePreset(entry, f"{field}.presets[{idx}]")
            if preset.id in presets:
                raise ValueError(
                    f"{_YAML_NAME}: {field}.presets duplicate id '{preset.id}'"
                )
            presets[preset.id] = preset
        self.presets = presets

        self.fallback_preset_id = _coerce_str(
            body.get("fallback_preset_id"), f"{field}.fallback_preset_id"
        )
        if self.fallback_preset_id not in presets:
            raise ValueError(
                f"{_YAML_NAME}: {field}.fallback_preset_id "
                f"'{self.fallback_preset_id}' is not among presets"
            )

        self.universal_negative_seed = _coerce_str_list(
            body.get("universal_negative_seed"), f"{field}.universal_negative_seed"
        )
        self.universal_pitfalls = _coerce_str_list(
            body.get("universal_pitfalls"), f"{field}.universal_pitfalls"
        )

    def preset_ids(self) -> List[str]:
        return list(self.presets)


class StyleLibraryConfig:
    """Все профили каталога стилей, прочитанные из yaml."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(f"{_YAML_NAME}: 'profiles' must be a non-empty mapping")

        profiles: Dict[str, StyleProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"{_YAML_NAME}: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(f"{_YAML_NAME}: profile '{name}' must be a mapping")
            profiles[name] = StyleProfile(name, body)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(f"{_YAML_NAME}: profile 'default' is required")

        self.profiles: Dict[str, StyleProfile] = profiles

    def get_profile(self, name: str) -> StyleProfile:
        if name not in self.profiles:
            raise KeyError(
                f"{_YAML_NAME}: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _coerce_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{_YAML_NAME}: {field} must be a non-empty string")
    return value.strip()


def _coerce_str_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(x, str) and x.strip() for x in value
    ):
        raise ValueError(f"{_YAML_NAME}: {field} must be a list of non-empty strings")
    return [x.strip() for x in value]


def _coerce_str_map(value: Any, field: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{_YAML_NAME}: {field} must be a mapping")
    result: Dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{_YAML_NAME}: {field} keys must be non-empty strings")
        result[key.strip()] = _coerce_str(val, f"{field}.{key}")
    return result


_lock = threading.Lock()
_cache: Dict[str, StyleLibraryConfig] = {}


def _resolve_path() -> Path:
    env_path = os.getenv(_ENV_PATH_VAR)
    if env_path:
        return Path(env_path).expanduser()
    return _DEFAULT_CONFIG_PATH


def load_style_library_config() -> StyleLibraryConfig:
    """Загрузить (или вернуть из кэша) каталог стилей. Fail-fast без файла."""
    path = _resolve_path()
    abs_key = str(path.resolve(strict=False))

    cached = _cache.get(abs_key)
    if cached is not None:
        return cached

    with _lock:
        cached = _cache.get(abs_key)
        if cached is not None:
            return cached
        if not path.is_file():
            raise FileNotFoundError(
                f"Storybook style library config not found at {path}. "
                f"Set {_ENV_PATH_VAR} or create "
                f"config/storybook/prompts/{_YAML_NAME}."
            )
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(
                f"{_YAML_NAME} at {path} must contain a mapping at the top level"
            )
        config = StyleLibraryConfig(raw, source_path=abs_key)
        _cache[abs_key] = config
        return config


def resolve_active_style_profile(explicit: Optional[str] = None) -> str:
    """Какой профиль использовать: explicit → env → ``default``."""
    if explicit:
        return explicit
    from_env = os.getenv(_ENV_PROFILE_VAR)
    if from_env:
        return from_env
    return _DEFAULT_PROFILE


def get_active_style_profile(explicit: Optional[str] = None) -> StyleProfile:
    """Активный профиль каталога (fail-fast KeyError для неизвестного имени)."""
    return load_style_library_config().get_profile(
        resolve_active_style_profile(explicit)
    )


def resolve_preset(
    preset_id: Any, profile_name: Optional[str] = None
) -> StylePreset:
    """Пресет по id; неизвестный или пустой id → ``fallback_preset_id``."""
    profile = get_active_style_profile(profile_name)
    key = preset_id.strip() if isinstance(preset_id, str) else ""
    preset = profile.presets.get(key)
    if preset is not None:
        return preset
    logger.warning(
        "style_library: неизвестный style_preset_id %r, берём fallback '%s'",
        preset_id,
        profile.fallback_preset_id,
    )
    return profile.presets[profile.fallback_preset_id]


def compose_style_catalog_note(profile_name: Optional[str] = None) -> str:
    """Каталог пресетов для system-промпта арт-директора."""
    profile = get_active_style_profile(profile_name)
    lines = ["Доступные style_preset_id (выбери ровно один):"]
    for preset in profile.presets.values():
        lines.append(
            f"- {preset.id} — {preset.title}. Когда уместен: {preset.when_to_use} "
            f"Визуальный язык: {preset.art_style}."
        )
    return "\n".join(lines)


def compose_negative_seed(
    preset: StylePreset, profile_name: Optional[str] = None
) -> List[str]:
    """Стилевые запреты пресета поверх универсальных, без дублей."""
    profile = get_active_style_profile(profile_name)
    seed: List[str] = []
    seen: set = set()
    for term in list(profile.universal_negative_seed) + list(preset.negative_seed):
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        seed.append(term)
    return seed


def merge_preset_into_style_images(
    preset: StylePreset,
    llm_overrides: Any,
    profile_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Пресет как база, проектные уточнения LLM — поверх разрешённых полей."""
    profile = get_active_style_profile(profile_name)
    overrides: Dict[str, Any] = llm_overrides if isinstance(llm_overrides, dict) else {}

    merged: Dict[str, Any] = {
        "style_preset_id": preset.id,
        "art_style": preset.art_style,
        "color_palette": list(preset.color_palette),
        "composition_rules": list(preset.composition_rules),
        "lighting": preset.lighting,
        "texture": preset.texture,
        "detail_density": preset.detail_density,
        "material_signature": preset.material_signature,
        "camera_defaults": dict(preset.camera_defaults),
        "style_pitfalls": list(profile.universal_pitfalls) + list(preset.pitfalls),
        "do_not_include": [],
        "model": "illustration",
        "project_note": "",
    }

    for field in _OVERRIDABLE_FIELDS:
        value = overrides.get(field)
        if isinstance(value, str) and value.strip():
            merged[field] = value.strip()
        elif isinstance(value, list):
            cleaned = [str(x).strip() for x in value if str(x).strip()]
            if cleaned:
                merged[field] = cleaned
    return merged


def compose_style_directives(style_images: Any) -> str:
    """Блок материальности стиля для промпт-инженера (пустой вход → "")."""
    if not isinstance(style_images, dict):
        return ""

    art_style = str(style_images.get("art_style") or "").strip()
    material = str(style_images.get("material_signature") or "").strip()
    texture = str(style_images.get("texture") or "").strip()
    lighting = str(style_images.get("lighting") or "").strip()
    camera = style_images.get("camera_defaults")
    pitfalls = style_images.get("style_pitfalls")

    if not (art_style or material):
        return ""

    lines = ["=== STYLE MATERIALITY (source: style_images.json) ==="]
    if art_style:
        lines.append(f"- Визуальный язык: {art_style}.")
    if material:
        lines.append(
            f"- Материальность носителя — включай в каждый english_prompt: {material}."
        )
    if texture:
        lines.append(f"- Фактура: {texture}.")
    if lighting:
        lines.append(f"- Свет по умолчанию: {lighting}.")
    if isinstance(camera, dict) and camera:
        pairs = "; ".join(f"{key}: {value}" for key, value in camera.items())
        lines.append(
            f"- Оптика и охват по крупности (используй конкретный параметр, "
            f"а не прилагательное): {pairs}."
        )
    if isinstance(pitfalls, list) and pitfalls:
        lines.append("- Типовые провалы этого стиля, которых нельзя допускать:")
        for item in pitfalls:
            text = str(item).strip()
            if text:
                lines.append(f"  * {text}")
    return "\n".join(lines)


def reset_cache() -> None:
    """Сброс кэша (для тестов, меняющих env-переменные)."""
    with _lock:
        _cache.clear()
