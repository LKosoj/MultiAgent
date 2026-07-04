"""
Загрузчик yaml-конфига доменных подсказок для storyboard QA-промптов (WS-E, M-25).

Конфиг — единственный source of truth для доменно-специфичных строк
(сценический/подиум/медицинский набор: podium, anesthesiologist,
трибуна->podium/lectern, gloves-continuity и т.п.), которые раньше были
захардкожены в «универсальных» промптах ``shots_prompt_qa.py`` и
``screenplay_shots_generator_utils/shared_utils.py``. В .py доменных строк
быть не должно — они переезжают сюда (ср.
``custom_tools/text_to_sql/schema_linking_examples_config.py``, T4.4).

Контракт:
  * Путь по умолчанию: ``config/storybook/prompts/domain_glossary.yaml``.
  * Override пути: env ``STORYBOOK_DOMAIN_GLOSSARY_PATH``.
  * Активный профиль: явный аргумент → env ``STORYBOOK_DOMAIN_PROFILE`` →
    ``default`` (пустой профиль = универсальный промпт без доменных строк).
  * Файл обязателен: нет файла → ``FileNotFoundError`` (no silent fallback).
  * Профиль ``default`` обязан присутствовать и быть пустым, чтобы промпт
    по умолчанию не тянул closed-world-предположения в кадры.
  * Кэш по абсолютному пути; ``reset_cache()`` для тестов, меняющих env.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "storybook" / "prompts" / "domain_glossary.yaml"
)

_ENV_PATH_VAR = "STORYBOOK_DOMAIN_GLOSSARY_PATH"
_ENV_PROFILE_VAR = "STORYBOOK_DOMAIN_PROFILE"
_DEFAULT_PROFILE = "default"

_YAML_NAME = "domain_glossary.yaml"


class DomainProfile:
    """Доменные подсказки одного профиля (например, ``stage_medical``)."""

    __slots__ = (
        "name",
        "anchor_glossary",
        "anchor_patterns",
        "spatial_locks",
        "pov_anchor_examples",
        "continuity_token_rules",
    )

    def __init__(
        self,
        name: str,
        anchor_glossary: List[Dict[str, str]],
        anchor_patterns: List[str],
        spatial_locks: List[Dict[str, str]],
        pov_anchor_examples: List[str],
        continuity_token_rules: List[Dict[str, Any]],
    ) -> None:
        self.name = name
        self.anchor_glossary = anchor_glossary
        self.anchor_patterns = anchor_patterns
        self.spatial_locks = spatial_locks
        self.pov_anchor_examples = pov_anchor_examples
        self.continuity_token_rules = continuity_token_rules

    def is_empty(self) -> bool:
        """Профиль пуст — доменных блоков в промпт добавлять не нужно."""
        return not (
            self.anchor_glossary
            or self.anchor_patterns
            or self.spatial_locks
            or self.pov_anchor_examples
            or self.continuity_token_rules
        )


class DomainGlossaryConfig:
    """Все доменные профили, прочитанные из yaml."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                f"{_YAML_NAME}: 'profiles' must be a non-empty mapping"
            )

        profiles: Dict[str, DomainProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"{_YAML_NAME}: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"{_YAML_NAME}: profile '{name}' must be a mapping"
                )
            profiles[name] = DomainProfile(
                name=name,
                anchor_glossary=_coerce_pair_list(
                    body.get("anchor_glossary"),
                    f"profiles.{name}.anchor_glossary",
                    ("source", "target"),
                ),
                anchor_patterns=_coerce_str_list(
                    body.get("anchor_patterns"),
                    f"profiles.{name}.anchor_patterns",
                ),
                spatial_locks=_coerce_pair_list(
                    body.get("spatial_locks"),
                    f"profiles.{name}.spatial_locks",
                    ("lock", "rule"),
                ),
                pov_anchor_examples=_coerce_str_list(
                    body.get("pov_anchor_examples"),
                    f"profiles.{name}.pov_anchor_examples",
                ),
                continuity_token_rules=_coerce_token_rules(
                    body.get("continuity_token_rules"),
                    f"profiles.{name}.continuity_token_rules",
                ),
            )

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(f"{_YAML_NAME}: profile 'default' is required")

        self.profiles: Dict[str, DomainProfile] = profiles

    def get_profile(self, name: str) -> DomainProfile:
        if name not in self.profiles:
            raise KeyError(
                f"{_YAML_NAME}: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _coerce_str_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{_YAML_NAME}: {field} must be a list of non-empty strings")
    return list(value)


def _coerce_pair_list(value: Any, field: str, keys: tuple) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{_YAML_NAME}: {field} must be a list of mappings")
    result: List[Dict[str, str]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"{_YAML_NAME}: {field}[{idx}] must be a mapping")
        row: Dict[str, str] = {}
        for key in keys:
            val = entry.get(key)
            if not isinstance(val, str) or not val:
                raise ValueError(
                    f"{_YAML_NAME}: {field}[{idx}].{key} must be a non-empty string"
                )
            row[key] = val
        result.append(row)
    return result


def _coerce_token_rules(value: Any, field: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{_YAML_NAME}: {field} must be a list of mappings")
    result: List[Dict[str, Any]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"{_YAML_NAME}: {field}[{idx}] must be a mapping")
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError(
                f"{_YAML_NAME}: {field}[{idx}].token must be a non-empty string"
            )
        add_negative = entry.get("add_negative", "")
        if not isinstance(add_negative, str):
            raise ValueError(
                f"{_YAML_NAME}: {field}[{idx}].add_negative must be a string"
            )
        strip_negatives = _coerce_str_list(
            entry.get("strip_negatives"), f"{field}[{idx}].strip_negatives"
        )
        result.append(
            {
                "token": token,
                "add_negative": add_negative,
                "strip_negatives": strip_negatives,
            }
        )
    return result


_lock = threading.Lock()
_cache: Dict[str, DomainGlossaryConfig] = {}


def _resolve_path() -> Path:
    env_path = os.getenv(_ENV_PATH_VAR)
    if env_path:
        return Path(env_path).expanduser()
    return _DEFAULT_CONFIG_PATH


def load_domain_glossary_config() -> DomainGlossaryConfig:
    """Загрузить (или вернуть из кэша) доменный конфиг. Fail-fast без файла."""
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
                f"Storybook domain glossary config not found at {path}. "
                f"Set {_ENV_PATH_VAR} or create "
                f"config/storybook/prompts/{_YAML_NAME}."
            )
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(
                f"{_YAML_NAME} at {path} must contain a mapping at the top level"
            )
        config = DomainGlossaryConfig(raw, source_path=abs_key)
        _cache[abs_key] = config
        return config


def resolve_active_domain_profile(explicit: str | None = None) -> str:
    """Какой профиль использовать: explicit → env → ``default``."""
    if explicit:
        return explicit
    from_env = os.getenv(_ENV_PROFILE_VAR)
    if from_env:
        return from_env
    return _DEFAULT_PROFILE


def get_active_profile(explicit: str | None = None) -> DomainProfile:
    """Активный доменный профиль (fail-fast KeyError для неизвестного имени)."""
    name = resolve_active_domain_profile(explicit)
    return load_domain_glossary_config().get_profile(name)


def compose_anchor_glossary_note(profile_name: str | None = None) -> str:
    """RU->EN glossary + pattern-подсказки для video_prompt.

    Возвращает пустую строку для пустого профиля — промпт остаётся
    универсальным.
    """
    profile = get_active_profile(profile_name)
    lines: List[str] = []
    if profile.anchor_glossary:
        pairs = ", ".join(
            f"{p['source']}->{p['target']}" for p in profile.anchor_glossary
        )
        lines.append(f"- Anchor glossary: {pairs}.")
    if profile.anchor_patterns:
        pats = "/".join(f'"{p}"' for p in profile.anchor_patterns)
        lines.append(
            f"- Pattern {pats} -> translate <ANCHOR> to English and include it."
        )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def compose_qa_domain_prompt(profile_name: str | None = None) -> str:
    """Доменный блок для основного QA system-промпта.

    Пустой профиль → пустая строка (промпт без доменных строк).
    """
    profile = get_active_profile(profile_name)
    if profile.is_empty():
        return ""

    parts: List[str] = [f"\n## Domain-specific guidance (profile: {profile.name})\n"]
    if profile.spatial_locks:
        parts.append("### Spatial & physics locks\n| Lock | Rule |\n|------|------|\n")
        for lk in profile.spatial_locks:
            parts.append(f"| {lk['lock']} | {lk['rule']} |\n")
    glossary_note = compose_anchor_glossary_note(profile.name)
    if glossary_note:
        parts.append("### Translation glossary\n")
        parts.append(glossary_note)
    if profile.pov_anchor_examples:
        examples = "; ".join(profile.pov_anchor_examples)
        parts.append(
            f"### POV/anchor substitution\n- No anchor substitution "
            f"(e.g., {examples}).\n"
        )
    return "".join(parts)


def continuity_token_rules(profile_name: str | None = None) -> List[Dict[str, Any]]:
    """Правила continuity для negative_prompt активного профиля (пусто → no-op)."""
    return list(get_active_profile(profile_name).continuity_token_rules)


def reset_cache() -> None:
    """Сброс кэша (для тестов, меняющих env-переменные)."""
    with _lock:
        _cache.clear()
