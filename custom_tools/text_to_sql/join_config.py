"""
Loader для ``config/text_to_sql/joins.yaml`` (join-параметры text-to-sql).

Единый source of truth для параметров построения join-пути (граф-джойн)
и containment-валидатора. См. дизайн в
``docs/plans/2026-06-29-text2sql-graph-join-containment-design.md`` (раздел 6).

Контракт (verbatim из AGENTS.md, no silent fallback):
  * Конфиг обязателен — отсутствие файла → ``FileNotFoundError``.
  * Профиль ``default`` обязателен.
  * Активный профиль: env ``TEXT_TO_SQL_JOINS_PROFILE`` → ``default``.
  * Все 5 полей обязательны в каждом профиле (fail-fast при пропуске).
  * Невалидное значение (enum не из списка, float вне [0..1], int<=0,
    bool вместо числа) → ``ValueError`` — НЕ silent fallback.
  * Env переопределяет yaml (env пусто/None → профиль). Значение из env
    прогоняется через те же коэрсеры (невалидное env → ``ValueError``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "joins.yaml"

_ENV_PATH_VAR = "TEXT_TO_SQL_JOINS_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_JOINS_PROFILE"
_DEFAULT_PROFILE = "default"

_PATH_ALGO_CHOICES = ("greedy", "graph")
_CONTAINMENT_CHOICES = ("off", "validate", "weight")

_REQUIRED_FIELDS = (
    "path_algo",
    "containment",
    "min_containment",
    "max_terminals",
    "containment_sample",
)

# Per-key env-переменные для оверрайда (раздел 6 дизайна).
_ENV_PATH_ALGO = "TEXT_TO_SQL_JOIN_PATH_ALGO"
_ENV_CONTAINMENT = "TEXT_TO_SQL_JOIN_CONTAINMENT"
_ENV_MIN_CONTAINMENT = "TEXT_TO_SQL_JOIN_MIN_CONTAINMENT"
_ENV_MAX_TERMINALS = "TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS"
_ENV_CONTAINMENT_SAMPLE = "TEXT_TO_SQL_CONTAINMENT_SAMPLE"


class JoinProfile:
    """Один профиль join-параметров.

    Поля:
      * ``path_algo`` — enum ``greedy|graph``;
      * ``containment`` — enum ``off|validate|weight``;
      * ``min_containment`` — float в ``[0.0, 1.0]``;
      * ``max_terminals`` — int ``> 0``;
      * ``containment_sample`` — int ``> 0``.
    """

    __slots__ = (
        "name",
        "path_algo",
        "containment",
        "min_containment",
        "max_terminals",
        "containment_sample",
    )

    def __init__(
        self,
        name: str,
        path_algo: str,
        containment: str,
        min_containment: float,
        max_terminals: int,
        containment_sample: int,
    ) -> None:
        self.name = name
        self.path_algo = path_algo
        self.containment = containment
        self.min_containment = min_containment
        self.max_terminals = max_terminals
        self.containment_sample = containment_sample


def _coerce_enum(value: Any, field: str, choices: tuple) -> str:
    """Привести yaml/env-значение к одному из ``choices``; иначе ValueError.

    Не-строка или строка не из списка → ``ValueError`` (fail-fast).
    """
    if not isinstance(value, str):
        raise ValueError(
            f"joins.yaml: {field} must be a string, got {value!r}"
        )
    if value not in choices:
        raise ValueError(
            f"joins.yaml: {field} must be one of {list(choices)}, got {value!r}"
        )
    return value


def _coerce_min_containment(value: Any, field: str) -> float:
    """Привести значение к float в ``[0.0, 1.0]``; bool отвергнуть; иначе ValueError."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"joins.yaml: {field} must be a number, got {value!r}"
        )
    coerced = float(value)
    if not (0.0 <= coerced <= 1.0):
        raise ValueError(
            f"joins.yaml: {field} must be in [0.0, 1.0], got {coerced}"
        )
    return coerced


def _coerce_positive_int(value: Any, field: str) -> int:
    """Привести значение к int ``> 0``; bool отвергнуть; иначе ValueError."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"joins.yaml: {field} must be an integer, got {value!r}"
        )
    if value <= 0:
        raise ValueError(
            f"joins.yaml: {field} must be > 0, got {value}"
        )
    return value


def _build_profile(name: str, body: Mapping[str, Any]) -> JoinProfile:
    field_ns = f"profiles.{name}"
    missing = [f for f in _REQUIRED_FIELDS if f not in body]
    if missing:
        raise ValueError(
            f"joins.yaml: profile '{name}' is missing required "
            f"fields: {sorted(missing)}"
        )
    return JoinProfile(
        name=name,
        path_algo=_coerce_enum(
            body["path_algo"], f"{field_ns}.path_algo", _PATH_ALGO_CHOICES
        ),
        containment=_coerce_enum(
            body["containment"], f"{field_ns}.containment", _CONTAINMENT_CHOICES
        ),
        min_containment=_coerce_min_containment(
            body["min_containment"], f"{field_ns}.min_containment"
        ),
        max_terminals=_coerce_positive_int(
            body["max_terminals"], f"{field_ns}.max_terminals"
        ),
        containment_sample=_coerce_positive_int(
            body["containment_sample"], f"{field_ns}.containment_sample"
        ),
    )


class JoinConfig:
    """Все профили join-параметров из yaml.

    Каждый профиль самодостаточен (нет слияния с default'ом). Профиль
    ``default`` обязателен.
    """

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                f"joins.yaml at {source_path}: 'profiles' must "
                "be a non-empty mapping"
            )

        profiles: Dict[str, JoinProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "joins.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"joins.yaml: profile '{name}' must be a mapping"
                )
            profiles[name] = _build_profile(name, body)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                f"joins.yaml at {source_path}: profile "
                f"'{_DEFAULT_PROFILE}' is required"
            )

        self.profiles: Dict[str, JoinProfile] = profiles

    def get_profile(self, name: str) -> JoinProfile:
        if name not in self.profiles:
            raise KeyError(
                f"joins.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Joins config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/joins.yaml. "
        "Join parameters require an explicit yaml source of truth."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "joins.yaml")


_loader: YamlConfigLoader["JoinConfig"] = YamlConfigLoader["JoinConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: JoinConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
    # Документ (все профили) кэшируется по пути; выбор профиля делается в
    # load_join_config() через config.get_profile(resolve_active_profile()).
)


def resolve_active_profile() -> str:
    """Имя активного профиля: env ``TEXT_TO_SQL_JOINS_PROFILE`` → ``default``."""
    return _shared_resolve_active_profile_name(
        None, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def load_join_config() -> JoinProfile:
    """Активный профиль join-параметров.

    Конфиг обязателен — отсутствие файла → ``FileNotFoundError``.
    Невалидный профиль → ``KeyError`` (fail-fast).
    """
    config = _loader.load()
    active_name = resolve_active_profile()
    return config.get_profile(active_name)


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()


# ---------------------------------------------------------------------------
# Per-key резолверы env > profile > default.
#
# Приоритет: непустая env-переменная (прогнанная через тот же коэрсер, что и
# yaml — невалидное env → ValueError, не silent fallback) → значение из
# активного профиля. Пустая/None env → профиль.
# ---------------------------------------------------------------------------


def _env_raw(env_var: str) -> Optional[str]:
    raw = os.getenv(env_var)
    if raw is None or raw == "":
        return None
    return raw


def resolve_path_algo() -> str:
    """``path_algo``: env ``TEXT_TO_SQL_JOIN_PATH_ALGO`` → профиль."""
    raw = _env_raw(_ENV_PATH_ALGO)
    if raw is not None:
        return _coerce_enum(raw, _ENV_PATH_ALGO, _PATH_ALGO_CHOICES)
    return load_join_config().path_algo


def resolve_containment() -> str:
    """``containment``: env ``TEXT_TO_SQL_JOIN_CONTAINMENT`` → профиль."""
    raw = _env_raw(_ENV_CONTAINMENT)
    if raw is not None:
        return _coerce_enum(raw, _ENV_CONTAINMENT, _CONTAINMENT_CHOICES)
    return load_join_config().containment


def resolve_min_containment() -> float:
    """``min_containment``: env ``TEXT_TO_SQL_JOIN_MIN_CONTAINMENT`` → профиль."""
    raw = _env_raw(_ENV_MIN_CONTAINMENT)
    if raw is not None:
        try:
            parsed: Any = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{_ENV_MIN_CONTAINMENT} must be a float, got {raw!r}"
            ) from exc
        return _coerce_min_containment(parsed, _ENV_MIN_CONTAINMENT)
    return load_join_config().min_containment


def resolve_max_terminals() -> int:
    """``max_terminals``: env ``TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS`` → профиль."""
    raw = _env_raw(_ENV_MAX_TERMINALS)
    if raw is not None:
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{_ENV_MAX_TERMINALS} must be an integer, got {raw!r}"
            ) from exc
        return _coerce_positive_int(parsed, _ENV_MAX_TERMINALS)
    return load_join_config().max_terminals


def resolve_containment_sample() -> int:
    """``containment_sample``: env ``TEXT_TO_SQL_CONTAINMENT_SAMPLE`` → профиль."""
    raw = _env_raw(_ENV_CONTAINMENT_SAMPLE)
    if raw is not None:
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{_ENV_CONTAINMENT_SAMPLE} must be an integer, got {raw!r}"
            ) from exc
        return _coerce_positive_int(parsed, _ENV_CONTAINMENT_SAMPLE)
    return load_join_config().containment_sample
