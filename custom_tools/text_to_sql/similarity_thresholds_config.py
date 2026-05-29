"""
Loader для ``config/text_to_sql/similarity_thresholds.yaml`` (W9-A7).

Единый source of truth для similarity-порогов text-to-sql / memory.
До W9-A7 пороги жили env-defaults в разных модулях (RAG_VECTOR_THRESHOLD,
RAG_RERANK_THRESHOLD, SCHEMA_TABLE_MIN_SCORE и т.д.). YAML с профилями
позволяет унифицировать их и переключать поведение под датасет
(muni_ru) без редактирования кода.

Контракт (verbatim из AGENTS.md):
  * Конфиг обязателен — отсутствие файла → ``FileNotFoundError``
    (no silent fallback).
  * Профиль ``default`` обязателен.
  * Активный профиль: env ``TEXT_TO_SQL_SIMILARITY_PROFILE`` → ``default``.
  * Значения env-переменных, существовавших до W9-A7 (RAG_VECTOR_THRESHOLD
    и т.п.), сохраняют приоритет над yaml — это сделано осознанно,
    чтобы существующие тесты и пользовательские конфигурации не
    сломались. См. ``resolve_threshold()``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "text_to_sql" / "similarity_thresholds.yaml"
)

_ENV_PATH_VAR = "TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SIMILARITY_PROFILE"
_DEFAULT_PROFILE = "default"

_REQUIRED_FIELDS = (
    "rag_examples_min_score",
    "strategic_memory_min_score",
    "tactical_memory_min_score",
    "schema_linking_min_score",
)


class SimilarityThresholdsProfile:
    """Один профиль similarity-порогов.

    Все поля — float в диапазоне ``[0.0, 1.0]``. Любое значение вне этого
    диапазона → ``ValueError`` при разборе yaml.
    """

    __slots__ = (
        "name",
        "rag_examples_min_score",
        "strategic_memory_min_score",
        "tactical_memory_min_score",
        "schema_linking_min_score",
    )

    def __init__(
        self,
        name: str,
        rag_examples_min_score: float,
        strategic_memory_min_score: float,
        tactical_memory_min_score: float,
        schema_linking_min_score: float,
    ) -> None:
        self.name = name
        self.rag_examples_min_score = rag_examples_min_score
        self.strategic_memory_min_score = strategic_memory_min_score
        self.tactical_memory_min_score = tactical_memory_min_score
        self.schema_linking_min_score = schema_linking_min_score


def _coerce_float_threshold(value: Any, field: str) -> float:
    """Привести yaml-значение к float в [0.0, 1.0]; иначе ValueError."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"similarity_thresholds.yaml: {field} must be a number, got {value!r}"
        )
    coerced = float(value)
    if not (0.0 <= coerced <= 1.0):
        raise ValueError(
            f"similarity_thresholds.yaml: {field} must be in [0.0, 1.0], "
            f"got {coerced}"
        )
    return coerced


def _build_profile(name: str, body: Mapping[str, Any]) -> SimilarityThresholdsProfile:
    field_ns = f"profiles.{name}"
    missing = [f for f in _REQUIRED_FIELDS if f not in body]
    if missing:
        raise ValueError(
            f"similarity_thresholds.yaml: profile '{name}' is missing required "
            f"fields: {sorted(missing)}"
        )
    return SimilarityThresholdsProfile(
        name=name,
        rag_examples_min_score=_coerce_float_threshold(
            body["rag_examples_min_score"], f"{field_ns}.rag_examples_min_score"
        ),
        strategic_memory_min_score=_coerce_float_threshold(
            body["strategic_memory_min_score"],
            f"{field_ns}.strategic_memory_min_score",
        ),
        tactical_memory_min_score=_coerce_float_threshold(
            body["tactical_memory_min_score"],
            f"{field_ns}.tactical_memory_min_score",
        ),
        schema_linking_min_score=_coerce_float_threshold(
            body["schema_linking_min_score"],
            f"{field_ns}.schema_linking_min_score",
        ),
    )


class SimilarityThresholdsConfig:
    """Все профили similarity-порогов из yaml.

    В отличие от SignificanceConfig тут НЕТ слияния с default'ом: пороги —
    скаляры, и «union» для них не имеет смысла. Каждый профиль самодостаточен.
    """

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ValueError(
                f"similarity_thresholds.yaml at {source_path}: 'profiles' must "
                "be a non-empty mapping"
            )

        profiles: Dict[str, SimilarityThresholdsProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "similarity_thresholds.yaml: profile names must be "
                    "non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"similarity_thresholds.yaml: profile '{name}' must be a mapping"
                )
            profiles[name] = _build_profile(name, body)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                f"similarity_thresholds.yaml at {source_path}: profile "
                f"'{_DEFAULT_PROFILE}' is required"
            )

        self.profiles: Dict[str, SimilarityThresholdsProfile] = profiles

    def get_profile(self, name: str) -> SimilarityThresholdsProfile:
        if name not in self.profiles:
            raise KeyError(
                f"similarity_thresholds.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Similarity thresholds config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/similarity_thresholds.yaml. "
        "Similarity thresholds require an explicit yaml source of truth (W9-A7)."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "similarity_thresholds.yaml")


_loader: YamlConfigLoader["SimilarityThresholdsConfig"] = YamlConfigLoader[
    "SimilarityThresholdsConfig"
](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: SimilarityThresholdsConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
    profile_extra=lambda: os.getenv(_ENV_PROFILE_VAR, _DEFAULT_PROFILE),
)


def resolve_active_profile() -> str:
    """Имя активного профиля: env ``TEXT_TO_SQL_SIMILARITY_PROFILE`` → ``default``."""
    return _shared_resolve_active_profile_name(
        None, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def load_similarity_thresholds() -> SimilarityThresholdsProfile:
    """Активный профиль similarity-порогов.

    Конфиг обязателен — отсутствие файла → ``FileNotFoundError``.
    Невалидный профиль → ``KeyError`` (fail-fast).
    """
    config = _loader.load()
    return config.get_profile(resolve_active_profile())


def resolve_threshold(field: str, *, env_override: Optional[str] = None) -> float:
    """Разрешить порог: env (если задан) → yaml-профиль.

    Параметры:
      ``field`` — имя поля профиля (например ``"strategic_memory_min_score"``).
      ``env_override`` — имя env-переменной (RAG_VECTOR_THRESHOLD и т.п.),
        которая исторически имела приоритет. Если задана и значение env
        непустое — её значение возвращается (после ``float()``). Если
        ``env_override`` не передан, используется только yaml.

    Контракт env: невалидный float в env → ``ValueError`` (fail-fast,
    не silent fallback на yaml).
    """
    if env_override:
        raw = os.getenv(env_override)
        if raw is not None and raw != "":
            try:
                return float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{env_override} must be a float, got {raw!r}"
                ) from exc

    profile = load_similarity_thresholds()
    if not hasattr(profile, field):
        raise KeyError(
            f"similarity_thresholds.yaml: unknown field '{field}'. "
            f"Available: {list(_REQUIRED_FIELDS)}"
        )
    return float(getattr(profile, field))


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()
