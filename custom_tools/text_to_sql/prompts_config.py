"""
Загрузчик yaml-конфига текстов LLM-промптов для text-to-sql pipeline (W6-T2).

Промпт-тексты (system_prompt, user_prompt_rules) — единственный source of truth
для текстовой части LLM-вызовов в text-to-sql. В .py-файлах ни одного
длинного промпт-текста быть не должно (см. AGENTS.md: «ничего не
хардкодить в … генерации промптов»).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/prompts.yaml``.
  * Путь переопределяется через env ``TEXT_TO_SQL_PROMPTS_PATH``.
  * Активный профиль: явный аргумент → env ``TEXT_TO_SQL_PROMPTS_PROFILE``
    → ``"default"``.
  * Файл обязателен: при отсутствии — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` для тестов.

Паттерн повторяет ``llm_models_config`` / ``schema_linking_examples_config`` —
единая семантика конфигов в проекте (EPIC 4 / 8.7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "prompts.yaml"

_ENV_PATH_VAR = "TEXT_TO_SQL_PROMPTS_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_PROMPTS_PROFILE"
_DEFAULT_PROFILE = "default"


class PromptsProfile:
    """Тексты промптов одного профиля (например, ``default``).

    Профиль — это мапа ``section -> mapping``. Используется как:
        profile.get_text("schema_linking", "system_prompt")
        profile.get_list("sql_generation", "user_prompt_rules")
    """

    __slots__ = ("name", "sections")

    def __init__(self, name: str, sections: Dict[str, Dict[str, Any]]) -> None:
        self.name = name
        self.sections: Dict[str, Dict[str, Any]] = {
            section_name: dict(section_body)
            for section_name, section_body in sections.items()
        }

    def get_text(self, section: str, key: str) -> str:
        """Вернуть строковое значение ``key`` из секции ``section``.

        Fail-fast: отсутствие секции/ключа → KeyError; не-строка → ValueError.
        Trailing whitespace (например, перенос строки от yaml ``|``)
        обрезается.
        """
        if section not in self.sections:
            raise KeyError(
                f"prompts.yaml: profile '{self.name}' has no section "
                f"'{section}'. Available: {sorted(self.sections)}"
            )
        body = self.sections[section]
        if key not in body:
            raise KeyError(
                f"prompts.yaml: profile '{self.name}' section '{section}' "
                f"has no key '{key}'. Available: {sorted(body)}"
            )
        value = body[key]
        if not isinstance(value, str):
            raise ValueError(
                f"prompts.yaml: profile '{self.name}'.{section}.{key} "
                f"must be a string, got {type(value).__name__}"
            )
        return value.strip()

    def get_list(self, section: str, key: str) -> List[str]:
        """Вернуть список строк из ``section.key``.

        Fail-fast: отсутствие — KeyError; не list[str] — ValueError.
        Пустой список — разрешён (вызывающая сторона решает, что делать).
        """
        if section not in self.sections:
            raise KeyError(
                f"prompts.yaml: profile '{self.name}' has no section "
                f"'{section}'. Available: {sorted(self.sections)}"
            )
        body = self.sections[section]
        if key not in body:
            raise KeyError(
                f"prompts.yaml: profile '{self.name}' section '{section}' "
                f"has no key '{key}'. Available: {sorted(body)}"
            )
        value = body[key]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(
                f"prompts.yaml: profile '{self.name}'.{section}.{key} "
                "must be a list of strings"
            )
        return list(value)


class PromptsConfig:
    """Все профили prompts.yaml."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError("prompts.yaml: 'profiles' must be a non-empty mapping")

        profiles: Dict[str, PromptsProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "prompts.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"prompts.yaml: profile '{name}' must be a mapping"
                )
            sections: Dict[str, Dict[str, Any]] = {}
            for section_name, section_body in body.items():
                if not isinstance(section_name, str) or not section_name:
                    raise ValueError(
                        f"prompts.yaml: profile '{name}' has a non-string section name"
                    )
                if not isinstance(section_body, dict):
                    raise ValueError(
                        f"prompts.yaml: profile '{name}'.{section_name} "
                        "must be a mapping"
                    )
                sections[section_name] = section_body
            profiles[name] = PromptsProfile(name=name, sections=sections)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError("prompts.yaml: profile 'default' is required")

        # Fail-fast: профиль 'default' обязан содержать все секции/ключи,
        # которые реально читаются runtime-кодом. При добавлении новой
        # точки чтения обновить REQUIRED_KEYS ниже.
        REQUIRED_KEYS = {
            "schema_linking": {"system_prompt"},
            "sql_generation": {"system_prompt", "user_prompt_rules"},
        }
        default_profile = profiles[_DEFAULT_PROFILE]
        for section, keys in REQUIRED_KEYS.items():
            if section not in default_profile.sections:
                raise ValueError(
                    f"prompts.yaml: profile 'default' missing required section '{section}'"
                )
            missing = keys - set(default_profile.sections[section].keys())
            if missing:
                raise ValueError(
                    f"prompts.yaml: profile 'default' section '{section}' "
                    f"missing required keys: {sorted(missing)}"
                )

        self.profiles: Dict[str, PromptsProfile] = profiles

    def get_profile(self, name: str) -> PromptsProfile:
        if name not in self.profiles:
            raise KeyError(
                f"prompts.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Text-to-SQL prompts config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/prompts.yaml. "
        "LLM prompt texts require an explicit yaml source of truth."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "prompts.yaml")


_loader: YamlConfigLoader["PromptsConfig"] = YamlConfigLoader["PromptsConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: PromptsConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_prompts_config() -> PromptsProfile:
    """Загрузить активный профиль промпт-текстов.

    Возвращает уже выбранный профиль (с учётом env / явного `default`).
    Кэшируется по абсолютному пути yaml-файла.
    """
    return get_active_profile()


def _load_config_object() -> PromptsConfig:
    return _loader.load()


def resolve_active_profile_name(explicit: str | None = None) -> str:
    """Какой профиль использовать: явный аргумент → env → ``"default"``."""
    return _shared_resolve_active_profile_name(
        explicit, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def get_active_profile(explicit: str | None = None) -> PromptsProfile:
    """Вернуть активный профиль (с учётом env / явного аргумента)."""
    name = resolve_active_profile_name(explicit)
    config = _load_config_object()
    return config.get_profile(name)


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env)."""
    _loader.reset_cache()
