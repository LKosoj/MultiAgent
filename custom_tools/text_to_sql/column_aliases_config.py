"""
Загрузчик yaml-конфига доменных алиасов колонок для best_column_for.

Конфиг — единственный source of truth для доменных синонимов
("revenue ≈ amount", "date ≈ order_date" и т.п.). В .py файлах ни одной
доменной строки быть не должно (см. AGENTS.md, T4.2).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/column_aliases.yaml``.
  * Путь переопределяется через env ``TEXT_TO_SQL_COLUMN_ALIASES_PATH``.
  * Активный профиль: явный аргумент → env
    ``TEXT_TO_SQL_COLUMN_ALIASES_PROFILE`` → ``"default"`` (пустой).
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` для
    тестов.
  * Профиль ``default`` обязателен и должен быть пустым (никаких
    синонимов в дефолтном сценарии).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "column_aliases.yaml"

_ENV_PATH_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_COLUMN_ALIASES_PROFILE"
_DEFAULT_PROFILE = "default"

# Минимальная версия yaml-конфига. v1 — до EPIC 5.5/5.6 (категории и
# инварианты профилей в Python). v2 — обязательная секция ``policy``.
_MIN_CONFIG_VERSION = 2


class ColumnAliasesProfile:
    """Алиасы одного доменного профиля (например, muni_ru)."""

    __slots__ = ("name", "aliases", "type_hints")

    def __init__(
        self,
        name: str,
        aliases: Dict[str, List[str]],
        type_hints: Dict[str, Any] | None = None,
        *,
        type_hint_categories: tuple[str, ...],
    ) -> None:
        self.name = name
        # Нормализуем все ключи и значения в lowercase — сравнение всегда
        # case-insensitive.
        self.aliases: Dict[str, List[str]] = {
            key.lower(): [value.lower() for value in values]
            for key, values in aliases.items()
        }
        # type_hints: набор категорий из policy.type_hint_categories
        # (yaml — source of truth). Значение категории может быть:
        #   * list[str] (legacy) — плоский список query-токенов
        #     (старое поведение: type-bonus только при primary_signal);
        #   * dict {tokens: list[str], weight_solo: int, weight_with_signal:
        #     int} (4.26) — type-bonus работает всегда, но с разным весом.
        # Если в yaml категория не указана — пустой список (это валидно).
        raw_hints = type_hints or {}
        self.type_hints: Dict[str, Any] = {
            category: _normalize_category_hints(raw_hints.get(category))
            for category in type_hint_categories
        }

    def expand(self, term: str) -> List[str]:
        """Расширить term списком алиасов из профиля.

        Возвращает уникальный набор lowercase-строк, включающий сам term.
        Поведение:
          * прямой ключ: aliases[term] → возвращаем список;
          * обратный ключ: если term встречается в значениях какой-то
            группы — возвращаем всю группу (revenue → [amount, total],
            и amount → [revenue, total]);
          * иначе — пустой профиль для этого терма.
        """
        if not term:
            return []
        lowered = term.lower()
        results: List[str] = [lowered]
        seen = {lowered}

        direct = self.aliases.get(lowered)
        if direct:
            for value in direct:
                if value not in seen:
                    seen.add(value)
                    results.append(value)

        for key, values in self.aliases.items():
            if lowered == key or lowered in values:
                if key not in seen:
                    seen.add(key)
                    results.append(key)
                for value in values:
                    if value not in seen:
                        seen.add(value)
                        results.append(value)
        return results

    def is_empty(self) -> bool:
        return not self.aliases


class ColumnAliasesConfig:
    """Все доменные профили алиасов из yaml."""

    __slots__ = ("version", "profiles", "type_hint_categories", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        # EPIC 5.5/5.6: версия >= 2 обязательна и должна содержать
        # ``policy`` (категории type_hints + инварианты профилей).
        if not isinstance(self.version, int) or self.version < _MIN_CONFIG_VERSION:
            raise ValueError(
                f"column_aliases.yaml at {source_path}: yaml migration required "
                f"— version must be >= {_MIN_CONFIG_VERSION} and contain a "
                "'policy' section. v1 layout (hardcoded categories in Python) "
                "is no longer supported."
            )

        policy = raw.get("policy")
        if not isinstance(policy, dict):
            raise ValueError(
                f"column_aliases.yaml at {source_path}: yaml migration required "
                "— 'policy' must be a mapping with 'type_hint_categories', "
                "'required_profiles', 'default_profile_must_be_empty'."
            )

        raw_categories = policy.get("type_hint_categories")
        if (
            not isinstance(raw_categories, list)
            or not raw_categories
            or not all(isinstance(item, str) and item for item in raw_categories)
        ):
            raise ValueError(
                "column_aliases.yaml: 'policy.type_hint_categories' must be a "
                "non-empty list of non-empty strings"
            )
        if len(set(raw_categories)) != len(raw_categories):
            raise ValueError(
                "column_aliases.yaml: 'policy.type_hint_categories' must not "
                f"contain duplicates (got {raw_categories!r})"
            )
        self.type_hint_categories: tuple[str, ...] = tuple(raw_categories)

        raw_required = policy.get("required_profiles", [_DEFAULT_PROFILE])
        if (
            not isinstance(raw_required, list)
            or not raw_required
            or not all(isinstance(item, str) and item for item in raw_required)
        ):
            raise ValueError(
                "column_aliases.yaml: 'policy.required_profiles' must be a "
                "non-empty list of non-empty strings"
            )
        required_profiles: tuple[str, ...] = tuple(raw_required)

        default_must_be_empty = policy.get("default_profile_must_be_empty", True)
        if not isinstance(default_must_be_empty, bool):
            raise ValueError(
                "column_aliases.yaml: 'policy.default_profile_must_be_empty' "
                "must be a boolean"
            )

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                "column_aliases.yaml: 'profiles' must be a non-empty mapping"
            )

        profiles: Dict[str, ColumnAliasesProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "column_aliases.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"column_aliases.yaml: profile '{name}' must be a mapping"
                )
            aliases = _coerce_alias_map(
                body.get("aliases"), f"profiles.{name}.aliases"
            )
            type_hints = _coerce_type_hints_map(
                body.get("type_hints"),
                f"profiles.{name}.type_hints",
                allowed_categories=self.type_hint_categories,
            )
            profiles[name] = ColumnAliasesProfile(
                name=name,
                aliases=aliases,
                type_hints=type_hints,
                type_hint_categories=self.type_hint_categories,
            )

        # Strict-режим включается через env COLUMN_ALIASES_STRICT=1.
        # По умолчанию пустой required non-default профиль — это warning,
        # а не ошибка: в реальных деплоях бывают временные конфигурации,
        # где доменный профиль намеренно "выключен" (пустые алиасы),
        # но required-список менять не хочется.
        strict_required = os.getenv("COLUMN_ALIASES_STRICT", "0") == "1"
        for required in required_profiles:
            if required not in profiles:
                raise ValueError(
                    f"column_aliases.yaml: profile '{required}' is required "
                    f"(see policy.required_profiles)"
                )
            # Профиль default по контракту должен быть пустым (см. invariant
            # ниже), поэтому проверку на непустоту делаем только для прочих
            # required-профилей.
            if required != _DEFAULT_PROFILE and profiles[required].is_empty():
                message = (
                    f"column_aliases.yaml: required profile '{required}' is empty; "
                    "required profiles normally define at least one alias group "
                    "(see policy.required_profiles)"
                )
                if strict_required:
                    raise ValueError(message)
                logger.warning(
                    "%s — using empty profile (set COLUMN_ALIASES_STRICT=1 to fail-fast).",
                    message,
                )

        if (
            default_must_be_empty
            and _DEFAULT_PROFILE in profiles
            and not profiles[_DEFAULT_PROFILE].is_empty()
        ):
            raise ValueError(
                "column_aliases.yaml: profile 'default' must be empty "
                "(no aliases). Доменные синонимы держим в отдельных профилях. "
                "Если это намеренно — выставите policy.default_profile_must_be_empty: false."
            )

        self.profiles: Dict[str, ColumnAliasesProfile] = profiles

    def get_profile(self, name: str) -> ColumnAliasesProfile:
        """Вернуть профиль по имени или поднять KeyError (fail-fast)."""
        if name not in self.profiles:
            raise KeyError(
                f"column_aliases.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]

    def get_type_hints(self, profile_name: str) -> Dict[str, Any]:
        """Вернуть type_hints профиля.

        Возвращает словарь вида ``{"numeric": <value>, "temporal":
        <value>, "identifier": <value>}``. Гарантирует, что все категории
        присутствуют. ``<value>`` — либо ``list[str]`` (legacy), либо
        ``dict`` с ключами ``tokens``/``weight_solo``/``weight_with_signal``
        (расширенный формат 4.26).
        """
        return dict(self.get_profile(profile_name).type_hints)


def _coerce_alias_map(value: Any, field: str) -> Dict[str, List[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"column_aliases.yaml: {field} must be a mapping")
    result: Dict[str, List[str]] = {}
    for key, values in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"column_aliases.yaml: {field} keys must be non-empty strings"
            )
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise ValueError(
                f"column_aliases.yaml: {field}.{key} must be a list of non-empty strings"
            )
        result[key] = list(values)
    return result


def _coerce_type_hints_map(
    value: Any,
    field: str,
    *,
    allowed_categories: tuple[str, ...],
) -> Dict[str, Any]:
    """Прочитать секцию ``type_hints`` профиля.

    Контракт:
      * Ключ ``type_hints`` опциональный — отсутствие = пустая карта.
      * Ключи внутри обязательно из ``allowed_categories``
        (= ``policy.type_hint_categories`` из yaml);
        опечатка ``numerc`` → ``ValueError`` (fail-fast).
      * Значение категории может быть:
          - list[str] (legacy) — список query-токенов (могут быть
            пустыми, это допустимо);
          - dict с ключами ``tokens`` (list[str]), ``weight_solo`` (int),
            ``weight_with_signal`` (int) — расширенный формат для 4.26.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"column_aliases.yaml: {field} must be a mapping")
    result: Dict[str, Any] = {}
    for key, values in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"column_aliases.yaml: {field} keys must be non-empty strings"
            )
        if key not in allowed_categories:
            raise ValueError(
                f"column_aliases.yaml: {field}.{key} is not a recognized "
                f"type-hint category. Allowed: {list(allowed_categories)}"
            )
        if isinstance(values, list):
            if not all(isinstance(item, str) and item for item in values):
                raise ValueError(
                    f"column_aliases.yaml: {field}.{key} list items must be non-empty strings"
                )
            result[key] = list(values)
            continue
        if isinstance(values, dict):
            tokens = values.get("tokens", [])
            if not isinstance(tokens, list) or not all(
                isinstance(item, str) and item for item in tokens
            ):
                raise ValueError(
                    f"column_aliases.yaml: {field}.{key}.tokens must be a list "
                    "of non-empty strings"
                )
            weight_solo = values.get("weight_solo", 1)
            weight_with_signal = values.get("weight_with_signal", 3)
            if not isinstance(weight_solo, int) or weight_solo < 0:
                raise ValueError(
                    f"column_aliases.yaml: {field}.{key}.weight_solo must be a non-negative int"
                )
            if not isinstance(weight_with_signal, int) or weight_with_signal < 0:
                raise ValueError(
                    f"column_aliases.yaml: {field}.{key}.weight_with_signal must be a non-negative int"
                )
            result[key] = {
                "tokens": list(tokens),
                "weight_solo": weight_solo,
                "weight_with_signal": weight_with_signal,
            }
            continue
        raise ValueError(
            f"column_aliases.yaml: {field}.{key} must be a list of non-empty strings "
            "or a mapping with keys 'tokens', 'weight_solo', 'weight_with_signal'"
        )
    return result


def _normalize_category_hints(value: Any) -> Any:
    """Нормализует значение категории type_hints для активного профиля.

    Преобразует legacy-list в lowercase, расширенный dict — оставляет dict
    (но lowercase для tokens). Если значение отсутствует — возвращает
    пустой list (= legacy-empty).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).lower() for item in value]
    if isinstance(value, dict):
        tokens = [str(t).lower() for t in value.get("tokens", [])]
        return {
            "tokens": tokens,
            "weight_solo": int(value.get("weight_solo", 1)),
            "weight_with_signal": int(value.get("weight_with_signal", 3)),
        }
    # yaml-loader валидирует входные значения; сюда попадаем только при
    # программной ошибке — поднимаем fail-fast.
    raise TypeError(
        f"_normalize_category_hints: unexpected value type "
        f"{type(value).__name__} (expected list, dict, or None)"
    )


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Column aliases config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/column_aliases.yaml. "
        "Column-aliases lookup requires an explicit yaml source of truth."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "column_aliases.yaml")


_loader: YamlConfigLoader["ColumnAliasesConfig"] = YamlConfigLoader["ColumnAliasesConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: ColumnAliasesConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_column_aliases_config() -> ColumnAliasesConfig:
    """Загрузить и закэшировать конфиг доменных алиасов.

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    Кэшируется по абсолютному пути; чтобы перечитать файл (или сменить путь),
    нужен рестарт процесса либо ``reset_cache()``.
    """
    return _loader.load()


def resolve_active_profile_name(explicit: str | None = None) -> str:
    """Какой профиль использовать.

    Приоритет: явный аргумент → env → ``"default"``.
    """
    return _shared_resolve_active_profile_name(
        explicit, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def get_active_profile(explicit: str | None = None) -> ColumnAliasesProfile:
    """Вернуть активный профиль (с учётом env / явного аргумента)."""
    name = resolve_active_profile_name(explicit)
    config = load_column_aliases_config()
    return config.get_profile(name)


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env)."""
    _loader.reset_cache()
