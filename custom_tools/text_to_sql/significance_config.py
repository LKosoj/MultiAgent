"""
Загрузчик yaml-конфига значимости колонок схемы.

Конфиг — единственный source of truth для списков, на которые опирается
``ColumnMetadataHelper.is_semantic_significant_column``. В .py файлах ни
одной доменной строки (oktmo, «значение показател», «год отчет» и т.п.)
быть не должно (см. AGENTS.md, T4.3).

Контракт (v2):
  * Путь по умолчанию: ``config/text_to_sql/significance.yaml`` в корне репо.
  * Путь переопределяется через env ``TEXT_TO_SQL_SIGNIFICANCE_PATH``.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое организовано по профилям (``profiles.<name>``). Профиль
    ``default`` обязателен и содержит общеотраслевые признаки. Доменные
    добавки живут в отдельных профилях (``muni_ru`` и т.п.).
  * Активный профиль выбирается через env
    ``TEXT_TO_SQL_SIGNIFICANCE_PROFILE`` (по умолчанию — ``"default"``).
  * Слияние: для активного профиля результат = union с ``default`` по
    каждому полю. Регресс-сейф для существующих пользовательских
    сценариев.
  * Несуществующий профиль → ``KeyError`` (fail-fast).

Миграция v1 → v2 (EPIC 5.8: legacy fallback УДАЛЁН):
  * Если yaml не содержит ключа ``profiles`` — ``ValueError`` (fail-fast).
  * v1-fallback с deprecation warning больше не поддерживается.
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` —
    для тестов.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Pattern, Tuple

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    coerce_str_list,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "significance.yaml"

_ENV_PATH_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PROFILE"
_DEFAULT_PROFILE = "default"

logger = logging.getLogger(__name__)


class SignificanceProfile:
    """Один профиль значимости с прекомпилированными regex'ами.

    Все строковые поля — frozenset для быстрых set-операций при слиянии.
    ``medium_priority_patterns`` — tuple пар ``(compiled_regex, description)``.
    """

    __slots__ = (
        "name",
        "high_priority_exact",
        "high_priority_compound",
        "medium_priority_patterns",
        "critical_description_keywords",
        "important_column_name_substrings",
    )

    def __init__(
        self,
        name: str,
        high_priority_exact: FrozenSet[str],
        high_priority_compound: FrozenSet[str],
        medium_priority_patterns: Tuple[Tuple[Pattern[str], str], ...],
        critical_description_keywords: FrozenSet[str],
        important_column_name_substrings: FrozenSet[str] = frozenset(),
    ) -> None:
        self.name = name
        self.high_priority_exact = high_priority_exact
        self.high_priority_compound = high_priority_compound
        self.medium_priority_patterns = medium_priority_patterns
        self.critical_description_keywords = critical_description_keywords
        self.important_column_name_substrings = important_column_name_substrings


def _build_profile(name: str, body: Mapping[str, Any]) -> SignificanceProfile:
    """Сконструировать ``SignificanceProfile`` из mapping одного профиля."""
    field_ns = f"profiles.{name}"
    return SignificanceProfile(
        name=name,
        high_priority_exact=frozenset(
            _coerce_str_list(body.get("high_priority_exact"), f"{field_ns}.high_priority_exact")
        ),
        high_priority_compound=frozenset(
            _coerce_str_list(body.get("high_priority_compound"), f"{field_ns}.high_priority_compound")
        ),
        medium_priority_patterns=tuple(
            _coerce_pattern_pairs(
                body.get("medium_priority_patterns"),
                f"{field_ns}.medium_priority_patterns",
            )
        ),
        critical_description_keywords=frozenset(
            _coerce_str_list(
                body.get("critical_description_keywords"),
                f"{field_ns}.critical_description_keywords",
            )
        ),
        important_column_name_substrings=frozenset(
            # Все substring-ключи нормализуем к lower для case-insensitive
            # сравнения в schema_memory.create_table_description.
            s.lower() for s in _coerce_str_list(
                body.get("important_column_name_substrings"),
                f"{field_ns}.important_column_name_substrings",
            )
        ),
    )


def _merge_patterns(
    base: Tuple[Tuple[Pattern[str], str], ...],
    overlay: Tuple[Tuple[Pattern[str], str], ...],
) -> Tuple[Tuple[Pattern[str], str], ...]:
    """Union для medium_priority_patterns по тексту pattern.

    Сохраняет порядок: сначала default, потом уникальные паттерны из overlay.
    """
    seen: set[str] = set()
    merged: list[Tuple[Pattern[str], str]] = []
    for compiled, desc in base + overlay:
        if compiled.pattern in seen:
            continue
        seen.add(compiled.pattern)
        merged.append((compiled, desc))
    return tuple(merged)


class SignificanceConfig:
    """Все профили значимости из yaml + методы для разрешения активного."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            # EPIC 5.8: v1-legacy fallback удалён. yaml без секции
            # 'profiles' → fail-fast (никаких silent миграций).
            raise ValueError(
                f"significance.yaml at {source_path}: 'profiles' must be a "
                "non-empty mapping. Legacy v1 root-level layout is no longer "
                "supported (EPIC 5.8) — migrate to v2: profiles: default: ..."
            )

        profiles: Dict[str, SignificanceProfile] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "significance.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"significance.yaml: profile '{name}' must be a mapping"
                )
            profiles[name] = _build_profile(name, body)

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                f"significance.yaml at {source_path}: profile 'default' is required"
            )

        self.profiles: Dict[str, SignificanceProfile] = profiles

    def get_profile(self, name: str) -> SignificanceProfile:
        """Вернуть профиль по имени без слияния (fail-fast при отсутствии)."""
        if name not in self.profiles:
            raise KeyError(
                f"significance.yaml: unknown profile '{name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return self.profiles[name]

    def get_merged_profile(self, name: str) -> SignificanceProfile:
        """Вернуть профиль, слитый по union с ``default``.

        Если name == "default" — возвращается сам default без удвоения.
        Иначе результат = union полей default и запрошенного профиля.
        """
        default = self.get_profile(_DEFAULT_PROFILE)
        if name == _DEFAULT_PROFILE:
            return default
        overlay = self.get_profile(name)
        return SignificanceProfile(
            name=name,
            high_priority_exact=default.high_priority_exact | overlay.high_priority_exact,
            high_priority_compound=default.high_priority_compound | overlay.high_priority_compound,
            medium_priority_patterns=_merge_patterns(
                default.medium_priority_patterns, overlay.medium_priority_patterns
            ),
            critical_description_keywords=(
                default.critical_description_keywords | overlay.critical_description_keywords
            ),
            important_column_name_substrings=(
                default.important_column_name_substrings
                | overlay.important_column_name_substrings
            ),
        )


def _coerce_str_list(value: Any, field: str) -> list[str]:
    return coerce_str_list(value, field, yaml_filename="significance.yaml", reject_empty_strings=True)


def _coerce_pattern_pairs(value: Any, field: str) -> list[Tuple[Pattern[str], str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"significance.yaml: {field} must be a list of mappings")
    result: list[Tuple[Pattern[str], str]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"significance.yaml: {field}[{idx}] must be a mapping")
        pattern = entry.get("pattern")
        description = entry.get("description", "")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(
                f"significance.yaml: {field}[{idx}].pattern must be a non-empty string"
            )
        if not isinstance(description, str):
            raise ValueError(
                f"significance.yaml: {field}[{idx}].description must be a string"
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"significance.yaml: {field}[{idx}].pattern is not a valid regex: {exc}"
            ) from exc
        result.append((compiled, description))
    return result


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Significance config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/significance.yaml. "
        "Column significance heuristic requires an explicit yaml source of truth."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "significance.yaml")


_loader: YamlConfigLoader["SignificanceConfig"] = YamlConfigLoader["SignificanceConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: SignificanceConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def resolve_active_profile() -> str:
    """Вернуть имя активного профиля.

    Приоритет: env ``TEXT_TO_SQL_SIGNIFICANCE_PROFILE`` → ``"default"``.
    """
    return _shared_resolve_active_profile_name(
        None, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def load_significance_config() -> SignificanceProfile:
    """Загрузить активный профиль значимости (с union к default).

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    Профиль обязателен: при отсутствии запрошенного — ``KeyError``.
    Кэшируется по абсолютному пути; чтобы перечитать файл (или сменить
    путь), нужен рестарт процесса либо ``reset_cache()``.

    Возвращает ``SignificanceProfile`` — merged-профиль с теми же полями,
    что использует ``ColumnMetadataHelper`` (high_priority_exact,
    high_priority_compound, medium_priority_patterns,
    critical_description_keywords).
    """
    config = _load_config()
    profile_name = resolve_active_profile()
    return config.get_merged_profile(profile_name)


def _load_config() -> SignificanceConfig:
    """Загрузить и закэшировать конфиг (внутренний helper)."""
    return _loader.load()


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()
