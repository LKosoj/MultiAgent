"""
Загрузчик yaml-конфига LLM-параметров для text-to-sql pipeline.

Конфиг — единственный source of truth для параметров LLM-вызовов
(``max_tokens``, и в перспективе ``model``, ``temperature``, ...). В .py
файлах ни одного магического числа быть не должно (см. AGENTS.md и
EPIC 4 / 4.17).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/llm_models.yaml``.
  * Путь переопределяется через env ``TEXT_TO_SQL_LLM_MODELS_PATH``.
  * Активный профиль: явный аргумент → env
    ``TEXT_TO_SQL_LLM_MODELS_PROFILE`` → ``"default"``.
  * Файл обязателен: при отсутствии — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` для тестов.

Паттерн полностью повторяет ``column_aliases_config`` /
``main_table_scoring_config`` — единая семантика конфигов в проекте.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "llm_models.yaml"

_ENV_PATH_VAR = "TEXT_TO_SQL_LLM_MODELS_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_LLM_MODELS_PROFILE"
_DEFAULT_PROFILE = "default"


class LLMModelsProfile:
    """LLM-параметры одного профиля (например, ``default``).

    Профиль — это мапа ``section -> mapping``. Используется как:
        profile.get("schema_linking", "max_tokens")
    """

    __slots__ = ("name", "sections")

    def __init__(self, name: str, sections: Dict[str, Dict[str, Any]]) -> None:
        self.name = name
        # Все ключи секций оставляем как есть; значения копируем
        # без модификации (типы валидируются на стороне yaml).
        self.sections: Dict[str, Dict[str, Any]] = {
            section_name: dict(section_body)
            for section_name, section_body in sections.items()
        }

    def get(self, section: str, key: str) -> Any:
        """Вернуть значение ``key`` из секции ``section`` или поднять KeyError."""
        if section not in self.sections:
            raise KeyError(
                f"llm_models.yaml: profile '{self.name}' has no section "
                f"'{section}'. Available: {sorted(self.sections)}"
            )
        body = self.sections[section]
        if key not in body:
            raise KeyError(
                f"llm_models.yaml: profile '{self.name}' section '{section}' "
                f"has no key '{key}'. Available: {sorted(body)}"
            )
        return body[key]

    def has(self, section: str, key: str) -> bool:
        return section in self.sections and key in self.sections[section]


class LLMModelsConfig:
    """Все профили llm_models.yaml."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                "llm_models.yaml: 'profiles' must be a non-empty mapping"
            )

        profiles: Dict[str, LLMModelsProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "llm_models.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"llm_models.yaml: profile '{name}' must be a mapping"
                )
            sections: Dict[str, Dict[str, Any]] = {}
            for section_name, section_body in body.items():
                if not isinstance(section_name, str) or not section_name:
                    raise ValueError(
                        f"llm_models.yaml: profile '{name}' has a non-string section name"
                    )
                if not isinstance(section_body, dict):
                    raise ValueError(
                        f"llm_models.yaml: profile '{name}'.{section_name} "
                        "must be a mapping"
                    )
                sections[section_name] = section_body
            profiles[name] = LLMModelsProfile(name=name, sections=sections)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                "llm_models.yaml: profile 'default' is required"
            )

        # Fail-fast: профиль 'default' обязан содержать все секции, которые
        # реально читаются runtime-кодом text-to-sql пайплайна. Список снят
        # с config/text_to_sql/llm_models.yaml — единого source of truth.
        # При добавлении новой секции в yaml необходимо обновить этот набор.
        REQUIRED_SECTIONS = {"schema_linking", "sql_generation", "nlu"}
        default_profile = profiles[_DEFAULT_PROFILE]
        missing = REQUIRED_SECTIONS - set(default_profile.sections.keys())
        if missing:
            raise ValueError(
                f"Default profile in llm_models.yaml missing sections: {sorted(missing)}"
            )

        self.profiles: Dict[str, LLMModelsProfile] = profiles

    def get_profile(self, name: str) -> LLMModelsProfile:
        if name not in self.profiles:
            raise KeyError(
                f"llm_models.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "LLM models config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/llm_models.yaml. "
        "LLM-models lookup requires an explicit yaml source of truth."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "llm_models.yaml")


_loader: YamlConfigLoader["LLMModelsConfig"] = YamlConfigLoader["LLMModelsConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: LLMModelsConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_llm_models_config() -> LLMModelsProfile:
    """Загрузить активный профиль LLM-параметров.

    Возвращает уже выбранный профиль (с учётом env / явного `default`).
    Кэшируется по абсолютному пути yaml-файла.
    """
    return get_active_profile()


def _load_config_object() -> LLMModelsConfig:
    return _loader.load()


def resolve_active_profile_name(explicit: str | None = None) -> str:
    """Какой профиль использовать: явный аргумент → env → ``"default"``."""
    return _shared_resolve_active_profile_name(
        explicit, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def get_active_profile(explicit: str | None = None) -> LLMModelsProfile:
    """Вернуть активный профиль (с учётом env / явного аргумента)."""
    name = resolve_active_profile_name(explicit)
    config = _load_config_object()
    return config.get_profile(name)


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env)."""
    _loader.reset_cache()
