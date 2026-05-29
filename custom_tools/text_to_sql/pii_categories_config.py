"""Загрузчик yaml-конфига PII-категорий для промпта LLM-детектора PII.

Конфиг — единственный source of truth для compliance-критичной
классификации (что считается PII на уровнях low/medium/high и в какой
юрисдикции). Раньше эти списки были вшиты в
``custom_tools/text_to_sql/prompts.py`` (см. T4.6 и AGENTS.md: запрет
на хардкод в QA-слоях).

Контракт:
  * Путь по умолчанию: ``config/pii/categories.yaml`` в корне репо.
  * Путь переопределяется через env ``PII_CATEGORIES_PATH``.
  * Юрисдикция выбирается через env ``PII_JURISDICTION``; если не задана —
    используется ``default_jurisdiction`` из yaml.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов из Python.
  * Неизвестная юрисдикция → ``KeyError`` (fail-fast).
  * Неизвестный уровень чувствительности → ``ValueError``.
  * Содержимое кэшируется по абсолютному пути; чтобы перечитать файл
    (или сменить путь), нужен ``reset_cache()`` либо рестарт процесса.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._yaml_config_loader import YamlConfigLoader, build_mapping_error_message


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "pii" / "categories.yaml"

_ENV_PATH_VAR = "PII_CATEGORIES_PATH"
_ENV_JURISDICTION_VAR = "PII_JURISDICTION"

# Минимальная версия yaml-конфига. v1 — до EPIC 5.4 (хардкод
# sensitivity-уровней в Python). v2 — обязательная секция
# ``policy.sensitivity_levels`` в yaml.
_MIN_CONFIG_VERSION = 2


class PiiCategory:
    """Одна PII-категория в конкретной юрисдикции."""

    __slots__ = ("id", "label_ru", "label_ru_overrides", "sensitivities")

    def __init__(
        self,
        id: str,
        label_ru: str,
        label_ru_overrides: Dict[str, str],
        sensitivities: List[str],
    ) -> None:
        self.id = id
        self.label_ru = label_ru
        self.label_ru_overrides = label_ru_overrides
        self.sensitivities = sensitivities

    def label_for(self, sensitivity: str) -> str:
        """Лейбл этой категории на конкретном уровне чувствительности."""
        return self.label_ru_overrides.get(sensitivity, self.label_ru)


class PiiSyncMaskRule:
    """Regex-rule for LLM-free sync masking paths (audit/RAG sanitization)."""

    __slots__ = (
        "id",
        "pattern",
        "replacement",
        "enabled",
        "enable_env",
        "preserve_before_group",
        "use_fullname_exclusions",
    )

    def __init__(
        self,
        id: str,
        pattern: str,
        replacement: str,
        enabled: bool,
        enable_env: str | None,
        preserve_before_group: int | None,
        use_fullname_exclusions: bool,
    ) -> None:
        self.id = id
        self.pattern = pattern
        self.replacement = replacement
        self.enabled = enabled
        self.enable_env = enable_env
        self.preserve_before_group = preserve_before_group
        self.use_fullname_exclusions = use_fullname_exclusions


class PiiJurisdiction:
    """Все настройки PII для одной юрисдикции (например, ru или eu)."""

    __slots__ = (
        "name",
        "description",
        "prefixes",
        "negatives",
        "fullname_exclusions",
        "sync_masking_rules",
        "categories",
    )

    def __init__(
        self,
        name: str,
        description: str,
        prefixes: Dict[str, str],
        negatives: Dict[str, str],
        fullname_exclusions: List[str],
        sync_masking_rules: List[PiiSyncMaskRule],
        categories: List[PiiCategory],
    ) -> None:
        self.name = name
        self.description = description
        self.prefixes = prefixes
        self.negatives = negatives
        self.fullname_exclusions = fullname_exclusions
        self.sync_masking_rules = sync_masking_rules
        self.categories = categories

    def categories_for(self, sensitivity: str) -> List[PiiCategory]:
        """Категории, считающиеся PII на указанном уровне (с сохранением порядка)."""
        return [c for c in self.categories if sensitivity in c.sensitivities]


class PiiCategoriesConfig:
    """Прочитанный yaml-конфиг PII-категорий со всеми юрисдикциями."""

    __slots__ = (
        "version",
        "default_jurisdiction",
        "jurisdictions",
        "sensitivity_levels",
        "source_path",
    )

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        # EPIC 5.4: версия >= 2 обязательна и должна содержать
        # ``policy.sensitivity_levels``. v1 без policy → fail-fast
        # с указанием на необходимость миграции (без silent fallback).
        if not isinstance(self.version, int) or self.version < _MIN_CONFIG_VERSION:
            raise ValueError(
                f"pii/categories.yaml at {source_path}: yaml migration required — "
                f"version must be >= {_MIN_CONFIG_VERSION} and contain "
                "'policy.sensitivity_levels'. v1 layout (hardcoded "
                "sensitivities in Python) is no longer supported."
            )

        policy = raw.get("policy")
        if not isinstance(policy, dict):
            raise ValueError(
                f"pii/categories.yaml at {source_path}: yaml migration required — "
                "'policy' must be a mapping with 'sensitivity_levels' list."
            )
        raw_levels = policy.get("sensitivity_levels")
        if (
            not isinstance(raw_levels, list)
            or not raw_levels
            or not all(isinstance(item, str) and item for item in raw_levels)
        ):
            raise ValueError(
                "pii/categories.yaml: 'policy.sensitivity_levels' must be a "
                "non-empty list of non-empty strings"
            )
        if len(set(raw_levels)) != len(raw_levels):
            raise ValueError(
                "pii/categories.yaml: 'policy.sensitivity_levels' must not "
                f"contain duplicates (got {raw_levels!r})"
            )
        self.sensitivity_levels: tuple[str, ...] = tuple(raw_levels)

        default_jurisdiction = raw.get("default_jurisdiction")
        if not isinstance(default_jurisdiction, str) or not default_jurisdiction:
            raise ValueError(
                "pii/categories.yaml: 'default_jurisdiction' must be a non-empty string"
            )
        self.default_jurisdiction = default_jurisdiction

        raw_jurisdictions = raw.get("jurisdictions")
        if not isinstance(raw_jurisdictions, dict) or not raw_jurisdictions:
            raise ValueError(
                "pii/categories.yaml: 'jurisdictions' must be a non-empty mapping"
            )

        jurisdictions: Dict[str, PiiJurisdiction] = {}
        for name, body in raw_jurisdictions.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "pii/categories.yaml: jurisdiction names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"pii/categories.yaml: jurisdiction '{name}' must be a mapping"
                )
            jurisdictions[name] = _build_jurisdiction(
                name, body, self.sensitivity_levels
            )

        if default_jurisdiction not in jurisdictions:
            raise ValueError(
                f"pii/categories.yaml: default_jurisdiction "
                f"'{default_jurisdiction}' is missing from 'jurisdictions'"
            )

        self.jurisdictions: Dict[str, PiiJurisdiction] = jurisdictions

    def get_jurisdiction(self, name: str) -> PiiJurisdiction:
        """Вернуть юрисдикцию по имени или поднять KeyError (fail-fast)."""
        if name not in self.jurisdictions:
            raise KeyError(
                f"pii/categories.yaml: unknown jurisdiction '{name}'. "
                f"Available: {sorted(self.jurisdictions)}"
            )
        return self.jurisdictions[name]


def _build_jurisdiction(
    name: str, body: Dict[str, Any], allowed_levels: tuple[str, ...]
) -> PiiJurisdiction:
    description = body.get("description", "")
    if not isinstance(description, str):
        raise ValueError(
            f"pii/categories.yaml: jurisdictions.{name}.description must be a string"
        )

    prefixes = _coerce_str_dict(
        body.get("prefixes"), f"jurisdictions.{name}.prefixes"
    )
    for sens in allowed_levels:
        if sens not in prefixes or not prefixes[sens]:
            raise ValueError(
                f"pii/categories.yaml: jurisdictions.{name}.prefixes.{sens} "
                "must be a non-empty string"
            )

    negatives = _coerce_str_dict(
        body.get("negatives"), f"jurisdictions.{name}.negatives", allow_empty=True
    )
    fullname_exclusions = _coerce_str_list(
        body.get("fullname_exclusions"),
        f"jurisdictions.{name}.fullname_exclusions",
        allow_empty=True,
    )
    sync_masking_rules = _build_sync_masking_rules(
        name,
        body.get("sync_masking"),
    )

    raw_categories = body.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise ValueError(
            f"pii/categories.yaml: jurisdictions.{name}.categories must be a non-empty list"
        )

    seen_ids: set[str] = set()
    categories: List[PiiCategory] = []
    for idx, entry in enumerate(raw_categories):
        if not isinstance(entry, dict):
            raise ValueError(
                f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}] "
                "must be a mapping"
            )
        cat_id = entry.get("id")
        if not isinstance(cat_id, str) or not cat_id:
            raise ValueError(
                f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}].id "
                "must be a non-empty string"
            )
        if cat_id in seen_ids:
            raise ValueError(
                f"pii/categories.yaml: duplicate category id '{cat_id}' "
                f"in jurisdiction '{name}'"
            )
        seen_ids.add(cat_id)

        label_ru = entry.get("label_ru")
        if not isinstance(label_ru, str) or not label_ru:
            raise ValueError(
                f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}].label_ru "
                "must be a non-empty string"
            )

        overrides_raw = entry.get("label_ru_overrides") or {}
        label_overrides = _coerce_str_dict(
            overrides_raw,
            f"jurisdictions.{name}.categories[{idx}].label_ru_overrides",
            allow_empty=True,
        )
        for sens in label_overrides:
            if sens not in allowed_levels:
                raise ValueError(
                    f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}]."
                    f"label_ru_overrides has unknown sensitivity '{sens}'. "
                    f"Allowed: {list(allowed_levels)}"
                )

        sensitivities = entry.get("sensitivities")
        if not isinstance(sensitivities, list) or not all(
            isinstance(s, str) for s in sensitivities
        ):
            raise ValueError(
                f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}]."
                "sensitivities must be a list of strings"
            )
        for sens in sensitivities:
            if sens not in allowed_levels:
                raise ValueError(
                    f"pii/categories.yaml: jurisdictions.{name}.categories[{idx}] "
                    f"has unknown sensitivity '{sens}'. "
                    f"Allowed: {list(allowed_levels)}"
                )

        categories.append(
            PiiCategory(
                id=cat_id,
                label_ru=label_ru,
                label_ru_overrides=dict(label_overrides),
                sensitivities=list(sensitivities),
            )
        )

    return PiiJurisdiction(
        name=name,
        description=description,
        prefixes=dict(prefixes),
        negatives=dict(negatives),
        fullname_exclusions=fullname_exclusions,
        sync_masking_rules=sync_masking_rules,
        categories=categories,
    )


def _build_sync_masking_rules(
    jurisdiction_name: str,
    raw: Any,
) -> List[PiiSyncMaskRule]:
    if raw is None:
        raise ValueError(
            f"pii/categories.yaml: jurisdictions.{jurisdiction_name}.sync_masking "
            "is required; audit/RAG masking must not silently return raw text"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"pii/categories.yaml: jurisdictions.{jurisdiction_name}.sync_masking "
            "must be a mapping"
        )
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(
            f"pii/categories.yaml: jurisdictions.{jurisdiction_name}.sync_masking.rules "
            "must be a non-empty list"
        )

    rules: List[PiiSyncMaskRule] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(raw_rules):
        field = f"jurisdictions.{jurisdiction_name}.sync_masking.rules[{idx}]"
        if not isinstance(entry, dict):
            raise ValueError(f"pii/categories.yaml: {field} must be a mapping")
        rule_id = entry.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"pii/categories.yaml: {field}.id must be a non-empty string")
        if rule_id in seen_ids:
            raise ValueError(
                f"pii/categories.yaml: duplicate sync masking rule id '{rule_id}' "
                f"in jurisdiction '{jurisdiction_name}'"
            )
        seen_ids.add(rule_id)

        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"pii/categories.yaml: {field}.pattern must be a non-empty string")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"pii/categories.yaml: {field}.pattern is invalid regex: {exc}"
            ) from exc

        replacement = entry.get("replacement")
        if not isinstance(replacement, str) or not replacement:
            raise ValueError(
                f"pii/categories.yaml: {field}.replacement must be a non-empty string"
            )
        enabled = _coerce_bool(
            entry.get("enabled", True),
            field=f"{field}.enabled",
        )
        enable_env_raw = entry.get("enable_env")
        if enable_env_raw is not None and (
            not isinstance(enable_env_raw, str) or not enable_env_raw
        ):
            raise ValueError(
                f"pii/categories.yaml: {field}.enable_env must be a non-empty string"
            )

        preserve_before_group_raw = entry.get("preserve_before_group")
        preserve_before_group: int | None
        if preserve_before_group_raw is None:
            preserve_before_group = None
        elif (
            isinstance(preserve_before_group_raw, int)
            and preserve_before_group_raw > 0
            and preserve_before_group_raw <= compiled.groups
        ):
            preserve_before_group = preserve_before_group_raw
        else:
            raise ValueError(
                f"pii/categories.yaml: {field}.preserve_before_group must be "
                "a positive group index present in pattern"
            )

        use_fullname_exclusions = _coerce_bool(
            entry.get("use_fullname_exclusions", False),
            field=f"{field}.use_fullname_exclusions",
        )
        rules.append(
            PiiSyncMaskRule(
                id=rule_id,
                pattern=pattern,
                replacement=replacement,
                enabled=enabled,
                enable_env=enable_env_raw,
                preserve_before_group=preserve_before_group,
                use_fullname_exclusions=use_fullname_exclusions,
            )
        )
    return rules


def _coerce_str_list(value: Any, field: str, allow_empty: bool = False) -> List[str]:
    if value is None:
        if allow_empty:
            return []
        raise ValueError(f"pii/categories.yaml: {field} is required")
    if not isinstance(value, list):
        raise ValueError(f"pii/categories.yaml: {field} must be a list")
    result: List[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(
                f"pii/categories.yaml: {field}[{idx}] must be a non-empty string"
            )
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError(f"pii/categories.yaml: {field} must not contain duplicates")
    return result


def _coerce_str_dict(
    value: Any, field: str, allow_empty: bool = False
) -> Dict[str, str]:
    if value is None:
        if allow_empty:
            return {}
        raise ValueError(f"pii/categories.yaml: {field} is required")
    if not isinstance(value, dict):
        raise ValueError(f"pii/categories.yaml: {field} must be a mapping")
    result: Dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str):
            raise ValueError(
                f"pii/categories.yaml: {field} has non-string key {key!r}"
            )
        if val is None or val == "":
            # допускаем пустое значение для negatives (отсутствие хвоста)
            result[key] = ""
            continue
        if not isinstance(val, str):
            raise ValueError(
                f"pii/categories.yaml: {field}.{key} must be a string"
            )
        result[key] = val
    return result


def _coerce_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
    raise ValueError(f"pii/categories.yaml: {field} must be boolean")


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "PII categories config not found at "
        f"{path}. Set {env_var} or create "
        "config/pii/categories.yaml. "
        "PII detection prompt requires an explicit yaml source of "
        "truth for compliance-critical category mapping."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "pii/categories.yaml")


_loader: YamlConfigLoader["PiiCategoriesConfig"] = YamlConfigLoader["PiiCategoriesConfig"](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: PiiCategoriesConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_pii_categories_config() -> PiiCategoriesConfig:
    """Загрузить и закэшировать конфиг PII-категорий.

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    Кэшируется по абсолютному пути; повторный вызов с тем же путём не читает
    диск. Чтобы перечитать файл (или сменить путь), нужен ``reset_cache()``.
    """
    return _loader.load()


def resolve_active_jurisdiction_name(explicit: Optional[str] = None) -> str:
    """Какую юрисдикцию использовать.

    Приоритет:
      1. явный аргумент ``explicit`` (если непустой);
      2. env ``PII_JURISDICTION``;
      3. ``default_jurisdiction`` из yaml.
    """
    if explicit:
        return explicit
    from_env = os.getenv(_ENV_JURISDICTION_VAR)
    if from_env:
        return from_env
    return load_pii_categories_config().default_jurisdiction


def compose_pii_description(
    sensitivity: str, jurisdiction: Optional[str] = None
) -> str:
    """Сформировать описание PII-категорий для промпта.

    Возвращает строку вида
    ``"<prefix>: <label1>, <label2>, ..., <labelN>[, <negatives>]"``,
    собранную из yaml по выбранной юрисдикции и уровню чувствительности.

    Raises:
        ValueError: если ``sensitivity`` не в ``policy.sensitivity_levels``
            (yaml — source of truth).
        KeyError: если юрисдикция отсутствует в yaml.
        FileNotFoundError: если yaml не найден.
    """
    config = load_pii_categories_config()
    if sensitivity not in config.sensitivity_levels:
        raise ValueError(
            f"Unknown PII sensitivity '{sensitivity}'. "
            f"Allowed: {list(config.sensitivity_levels)}"
        )

    jur_name = resolve_active_jurisdiction_name(jurisdiction)
    jur = config.get_jurisdiction(jur_name)

    labels = [c.label_for(sensitivity) for c in jur.categories_for(sensitivity)]
    prefix = jur.prefixes[sensitivity]
    parts = ", ".join(labels)
    description = f"{prefix}: {parts}"

    negative = jur.negatives.get(sensitivity, "").strip()
    if negative:
        description = f"{description}, {negative}"
    return description


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()
