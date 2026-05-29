"""
Загрузчик yaml-конфига доменных примеров для schema-linking промпта.

Конфиг — единственный source of truth для конкретных имён колонок
(territory_id, oktmo, municipal_district_name и т.п.), которые раньше
были захардкожены в ``custom_tools/text_to_sql/prompts.py``. В .py
файлах ни одной доменной строки быть не должно (см. AGENTS.md, T4.4).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/prompts/schema_linking_examples.yaml``
    в корне репо.
  * Путь переопределяется через env
    ``TEXT_TO_SQL_SCHEMA_LINKING_EXAMPLES_PATH``.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути (изменение env требует
    либо рестарта процесса, либо вызова ``reset_cache()``).
  * Профиль ``default`` обязательно должен присутствовать и быть пустым,
    чтобы промпт по умолчанию не содержал доменных терминов.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    coerce_str_list,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "text_to_sql" / "prompts" / "schema_linking_examples.yaml"
)

_ENV_PATH_VAR = "TEXT_TO_SQL_SCHEMA_LINKING_EXAMPLES_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SCHEMA_LINKING_PROFILE"
_DEFAULT_PROFILE = "default"


class SchemaLinkingProfile:
    """Доменные подсказки для одного профиля (например, muni_ru)."""

    __slots__ = (
        "name",
        "priority_id_columns",
        "low_priority_name_columns",
        "prefer_id_over_name_rules",
    )

    def __init__(
        self,
        name: str,
        priority_id_columns: List[str],
        low_priority_name_columns: List[str],
        prefer_id_over_name_rules: List[Dict[str, str]],
    ) -> None:
        self.name = name
        self.priority_id_columns = priority_id_columns
        self.low_priority_name_columns = low_priority_name_columns
        self.prefer_id_over_name_rules = prefer_id_over_name_rules

    def is_empty(self) -> bool:
        """Профиль пуст — добавлять блок в промпт не нужно."""
        return (
            not self.priority_id_columns
            and not self.low_priority_name_columns
            and not self.prefer_id_over_name_rules
        )


class SchemaLinkingExamplesConfig:
    """Все профили доменных подсказок, прочитанные из yaml."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                "schema_linking_examples.yaml: 'profiles' must be a non-empty mapping"
            )

        profiles: Dict[str, SchemaLinkingProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "schema_linking_examples.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"schema_linking_examples.yaml: profile '{name}' must be a mapping"
                )
            profiles[name] = SchemaLinkingProfile(
                name=name,
                priority_id_columns=_coerce_str_list(
                    body.get("priority_id_columns"),
                    f"profiles.{name}.priority_id_columns",
                ),
                low_priority_name_columns=_coerce_str_list(
                    body.get("low_priority_name_columns"),
                    f"profiles.{name}.low_priority_name_columns",
                ),
                prefer_id_over_name_rules=_coerce_id_rule_list(
                    body.get("prefer_id_over_name_rules"),
                    f"profiles.{name}.prefer_id_over_name_rules",
                ),
            )

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                "schema_linking_examples.yaml: profile 'default' is required"
            )

        self.profiles: Dict[str, SchemaLinkingProfile] = profiles

    def get_profile(self, name: str) -> SchemaLinkingProfile:
        """Вернуть профиль по имени или поднять KeyError (fail-fast)."""
        if name not in self.profiles:
            raise KeyError(
                f"schema_linking_examples.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _coerce_str_list(value: Any, field: str) -> List[str]:
    return coerce_str_list(value, field, yaml_filename="schema_linking_examples.yaml")


def _coerce_id_rule_list(value: Any, field: str) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"schema_linking_examples.yaml: {field} must be a list of mappings"
        )
    result: List[Dict[str, str]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(
                f"schema_linking_examples.yaml: {field}[{idx}] must be a mapping"
            )
        id_column = entry.get("id_column")
        ignore_column = entry.get("ignore_column")
        if not isinstance(id_column, str) or not id_column:
            raise ValueError(
                f"schema_linking_examples.yaml: {field}[{idx}].id_column must be a non-empty string"
            )
        if not isinstance(ignore_column, str) or not ignore_column:
            raise ValueError(
                f"schema_linking_examples.yaml: {field}[{idx}].ignore_column must be a non-empty string"
            )
        result.append({"id_column": id_column, "ignore_column": ignore_column})
    return result


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Schema-linking examples config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/prompts/schema_linking_examples.yaml. "
        "Schema-linking prompt requires an explicit yaml source of truth "
        "for domain-specific column hints."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "schema_linking_examples.yaml")


_loader: YamlConfigLoader["SchemaLinkingExamplesConfig"] = YamlConfigLoader[
    "SchemaLinkingExamplesConfig"
](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: SchemaLinkingExamplesConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_schema_linking_examples_config() -> SchemaLinkingExamplesConfig:
    """Загрузить и закэшировать конфиг доменных примеров для schema-linking.

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    Кэшируется по абсолютному пути; повторный вызов с тем же путём не читает
    диск. Чтобы перечитать файл (или сменить путь), нужен рестарт процесса
    либо ``reset_cache()``.
    """
    return _loader.load()


def resolve_active_profile_name(explicit: str | None = None) -> str:
    """Какой профиль использовать.

    Приоритет:
      1. явный аргумент ``explicit`` (если непустой);
      2. env ``TEXT_TO_SQL_SCHEMA_LINKING_PROFILE``;
      3. дефолтный ``"default"`` (пустой профиль = промпт без доменных строк).
    """
    return _shared_resolve_active_profile_name(
        explicit, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def compose_schema_linking_domain_examples(profile_name: str | None = None) -> str:
    """Сформировать блок текста с доменными подсказками для промпта.

    Возвращает пустую строку, если профиль пуст (дефолтный сценарий) —
    промпт остаётся универсальным.
    """
    name = resolve_active_profile_name(profile_name)
    config = load_schema_linking_examples_config()
    profile = config.get_profile(name)

    if profile.is_empty():
        return ""

    lines: List[str] = ["ДОМЕННЫЕ ПРИМЕРЫ ИЗ ТЕКУЩЕЙ СХЕМЫ:"]
    if profile.priority_id_columns:
        lines.append(
            "- Приоритетные ID-колонки (предпочитай их при джойнах): "
            + ", ".join(profile.priority_id_columns)
        )
    if profile.low_priority_name_columns:
        lines.append(
            "- Низкоприоритетные текстовые колонки (использовать только если нет ID): "
            + ", ".join(profile.low_priority_name_columns)
        )
    for rule in profile.prefer_id_over_name_rules:
        lines.append(
            f"- Если есть {rule['id_column']} — используй его, "
            f"игнорируй {rule['ignore_column']}."
        )
    return "\n".join(lines) + "\n\n"


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()
