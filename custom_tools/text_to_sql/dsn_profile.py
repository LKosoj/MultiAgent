"""DSN-scoped Text-to-SQL profile (W1-1.2, часть A).

Пользовательское требование: «профиль должен собираться под конкретный DSN
в разрезе схемы БД! Не может быть универсальных профилей». До этого модуля
доменные подсказки жили в именованных профилях (``default``/др.) четырёх
yaml (column_aliases / nlu_morphemes / schema_linking_examples /
significance), выбираемых через env-переменные — общих для всех БД сразу.

Этот модуль вводит один yaml-файл на конкретную БД:
``sqlrag/<dsn_to_sanitized_name(dsn)>.profile.yaml`` (тот же каталог и та же
sanitized-схема имени, что и у ``sqlrag/<name>.json``/``sqlrag/<name>.md``).

Контракт:
  * Нет файла профиля → ``DsnProfile.empty()`` (нейтрально, без fallback на
    именованные профили).
  * Невалидный yaml / неизвестные ключи / неверные типы → ``ValueError``
    (fail-fast, без silent fallback).
  * ``dsn_fingerprint`` в файле не совпадает с фактическим DSN → ``ValueError``
    (профиль явно от другой базы).
  * ``schema_namespace_version`` в файле не совпадает с переданным
    ``live_schema_fingerprint`` → ``logger.warning``; при
    ``TEXT_TO_SQL_DSN_PROFILE_STRICT=1`` → ``RuntimeError``.

``GlossaryEntry``, ``DsnProfile.glossary``, ``DsnProfile.empty()`` и
``load_dsn_profile(dsn, *, live_schema_fingerprint=None)`` — зафиксированный
публичный интерфейс: его использует W2-2.2 (глоссарий → SemanticFact).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

import yaml

from .utils import coerce_strict_bool, dsn_to_sanitized_name, get_repo_root

logger = logging.getLogger(__name__)

_ENV_STRICT_VAR = "TEXT_TO_SQL_DSN_PROFILE_STRICT"

_GLOSSARY_KIND_VALUES = ("dimension", "measure", "filter_value", "entity")

_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "dsn_fingerprint",
        "schema_namespace_version",
        "captured_at",
        "glossary",
        "aliases",
        "type_hints",
        "metric_hints",
        "nlu_hints",
        "few_shots_ref",
    }
)
_GLOSSARY_ENTRY_KEYS = frozenset({"term", "synonyms", "table", "column", "kind", "note"})
_METRIC_HINTS_KEYS = frozenset(
    {
        "priority_id_columns",
        "low_priority_name_columns",
        "prefer_id_over_name_rules",
        "significant_columns",
    }
)
_SIGNIFICANT_COLUMNS_KEYS = frozenset(
    {
        "high_priority_exact",
        "high_priority_compound",
        "medium_priority_patterns",
        "critical_description_keywords",
        "important_column_name_substrings",
    }
)
_PREFER_RULE_KEYS = frozenset({"id_column", "ignore_column"})
_PATTERN_PAIR_KEYS = frozenset({"pattern", "description"})


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    synonyms: tuple[str, ...]
    table: str
    column: Optional[str]
    kind: Optional[Literal["dimension", "measure", "filter_value", "entity"]]
    note: Optional[str]


@dataclass(frozen=True)
class PreferIdOverNameRule:
    id_column: str
    ignore_column: str


@dataclass(frozen=True)
class SignificantColumnHints:
    high_priority_exact: tuple[str, ...] = ()
    high_priority_compound: tuple[str, ...] = ()
    medium_priority_patterns: tuple[tuple[str, str], ...] = ()
    critical_description_keywords: tuple[str, ...] = ()
    important_column_name_substrings: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.high_priority_exact
            or self.high_priority_compound
            or self.medium_priority_patterns
            or self.critical_description_keywords
            or self.important_column_name_substrings
        )


@dataclass(frozen=True)
class MetricHints:
    priority_id_columns: tuple[str, ...] = ()
    low_priority_name_columns: tuple[str, ...] = ()
    prefer_id_over_name_rules: tuple[PreferIdOverNameRule, ...] = ()
    significant_columns: SignificantColumnHints = field(default_factory=SignificantColumnHints)

    def is_empty(self) -> bool:
        return (
            not self.priority_id_columns
            and not self.low_priority_name_columns
            and not self.prefer_id_over_name_rules
            and self.significant_columns.is_empty()
        )


@dataclass(frozen=True)
class DsnProfile:
    version: int
    dsn_fingerprint: Optional[str]
    schema_namespace_version: Optional[str]
    captured_at: Optional[str]
    glossary: tuple[GlossaryEntry, ...]
    aliases: Mapping[str, tuple[str, ...]]
    type_hints: Mapping[str, tuple[str, ...]]
    metric_hints: MetricHints
    nlu_hints: Mapping[str, Any]
    few_shots_ref: Optional[str]

    @classmethod
    def empty(cls) -> "DsnProfile":
        return cls(
            version=1,
            dsn_fingerprint=None,
            schema_namespace_version=None,
            captured_at=None,
            glossary=(),
            aliases={},
            type_hints={},
            metric_hints=MetricHints(),
            nlu_hints={},
            few_shots_ref=None,
        )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def dsn_profile_path(dsn: str) -> Path:
    """Путь к per-DSN профилю: ``sqlrag/<sanitized>.profile.yaml``.

    Тот же ``sqlrag``-каталог и та же sanitized-схема имени, что и у
    ``sqlrag/<name>.json`` (schema_loader) / ``sqlrag/<name>.md``.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("dsn_profile_path: dsn must be a non-empty string")
    name = dsn_to_sanitized_name(dsn)
    return get_repo_root() / "sqlrag" / f"{name}.profile.yaml"


def _dsn_identity(dsn: str) -> str:
    # Lazy import: schema_cache.py может (в будущем) импортировать этот
    # модуль для инвалидации кэша, поэтому top-level import создал бы цикл.
    from .schema_cache import _dsn_host_port_db

    return _dsn_host_port_db(dsn)


# ---------------------------------------------------------------------------
# Cache: (path, mtime) -> DsnProfile (без учёта dsn/live_schema_fingerprint —
# они проверяются на каждый вызов load_dsn_profile, т.к. зависят от аргументов).
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int, int], DsnProfile] = {}


def load_dsn_profile(
    dsn: str, *, live_schema_fingerprint: Optional[str] = None
) -> DsnProfile:
    """Загрузить DSN-профиль для конкретной базы.

    Нет файла → ``DsnProfile.empty()``. Файл есть, но принадлежит другому
    DSN (``dsn_fingerprint`` не совпадает) → ``ValueError``. Файл устарел
    относительно текущей схемы (``schema_namespace_version`` не совпадает с
    ``live_schema_fingerprint``) → ``logger.warning``, либо ``RuntimeError``
    при ``TEXT_TO_SQL_DSN_PROFILE_STRICT=1``.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        return DsnProfile.empty()

    path = dsn_profile_path(dsn)
    if not path.is_file():
        return DsnProfile.empty()

    # T3.5/schema_cache.py:268-279: (mtime, size) — не только mtime, т.к.
    # некоторые файловые системы дают грубую (секундную) точность mtime, и
    # быстрая правка файла в пределах той же секунды могла бы дать cache hit
    # на устаревшее содержимое.
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is None:
        cached = _parse_profile_file(path)
        with _CACHE_LOCK:
            _CACHE[cache_key] = cached

    _verify_dsn_fingerprint(cached, dsn=dsn, source_path=path)
    _verify_schema_namespace_version(
        cached, live_schema_fingerprint=live_schema_fingerprint, source_path=path
    )
    return cached


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены/перезаписи файла профиля)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def load_dsn_profile_or_empty(
    dsn: Optional[str],
    *,
    live_schema_fingerprint: Optional[str] = None,
    purpose: str,
) -> DsnProfile:
    """DSN-профиль либо ``DsnProfile.empty()`` при отсутствии/битом профиле.

    Общий хелпер для читателей, которым не нужно различать "профиля нет" и
    "профиль битый/от другого DSN" — оба случая деградируют на
    ``DsnProfile.empty()`` (сам вызывающий код решает, чем заменить пустой
    профиль: именованным профилем или явным дефолтом). Раньше этот try/except
    был продублирован в ``dsn_profile_overrides._safe_load_dsn_profile`` и
    ``prompts._resolve_schema_linking_domain_examples`` — вынесено сюда.

    ``dsn`` пуст/``None`` → ``DsnProfile.empty()`` без похода в файловую
    систему (тот же контракт, что и у ``load_dsn_profile``).

    ``ValueError`` из ``load_dsn_profile`` (профиль от другого DSN, битый
    yaml, неизвестные ключи) — не повод ронять caller: логируется
    ``logger.warning`` с САНИТИЗИРОВАННЫМ именем DSN (``dsn_to_sanitized_name``
    — без user/password) и меткой ``purpose`` (какой читатель вызвал, для
    диагностики в логах).

    ``RuntimeError`` (``TEXT_TO_SQL_DSN_PROFILE_STRICT=1`` + устаревший
    ``schema_namespace_version``) пробрасывается наверх без изменений — это
    осознанный fail-fast режим оператора, глушить нельзя.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        return DsnProfile.empty()
    try:
        return load_dsn_profile(dsn, live_schema_fingerprint=live_schema_fingerprint)
    except ValueError as exc:
        logger.warning(
            "dsn_profile: failed to load profile for dsn=%s (purpose=%s): %s; "
            "falling back to named profile",
            dsn_to_sanitized_name(dsn),
            purpose,
            exc,
        )
        return DsnProfile.empty()


def _verify_dsn_fingerprint(profile: DsnProfile, *, dsn: str, source_path: Path) -> None:
    if not profile.dsn_fingerprint:
        return
    actual = _dsn_identity(dsn)
    if profile.dsn_fingerprint != actual:
        raise ValueError(
            f"dsn_profile: {source_path} belongs to a different DSN "
            f"(stored dsn_fingerprint={profile.dsn_fingerprint!r}, "
            f"current={actual!r})"
        )


def _verify_schema_namespace_version(
    profile: DsnProfile,
    *,
    live_schema_fingerprint: Optional[str],
    source_path: Path,
) -> None:
    if not profile.schema_namespace_version or not live_schema_fingerprint:
        return
    if profile.schema_namespace_version == live_schema_fingerprint:
        return
    message = (
        f"dsn_profile: {source_path} was captured for schema_namespace_version="
        f"{profile.schema_namespace_version!r}, current live schema fingerprint is "
        f"{live_schema_fingerprint!r}; profile hints may be stale"
    )
    strict = coerce_strict_bool(
        os.getenv(_ENV_STRICT_VAR), default=False, field_name=_ENV_STRICT_VAR
    )
    if strict:
        raise RuntimeError(message)
    logger.warning(message)


# ---------------------------------------------------------------------------
# Parsing (fail-fast: невалидный yaml / неизвестные ключи / неверные типы →
# ValueError).
# ---------------------------------------------------------------------------


def _parse_profile_file(path: Path) -> DsnProfile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"dsn_profile: invalid yaml at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"dsn_profile: {path} must contain a mapping at the top level")

    unexpected = set(raw) - _TOP_LEVEL_KEYS
    if unexpected:
        raise ValueError(
            f"dsn_profile: {path} has unexpected top-level keys: {sorted(unexpected)}"
        )

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"dsn_profile: {path}: 'version' must be a positive integer")

    return DsnProfile(
        version=version,
        dsn_fingerprint=_optional_str(raw.get("dsn_fingerprint"), f"{path}: dsn_fingerprint"),
        schema_namespace_version=_optional_str(
            raw.get("schema_namespace_version"), f"{path}: schema_namespace_version"
        ),
        captured_at=_optional_str(raw.get("captured_at"), f"{path}: captured_at"),
        glossary=_parse_glossary(raw.get("glossary"), path),
        aliases=_parse_str_list_map(raw.get("aliases"), f"{path}: aliases"),
        type_hints=_parse_str_list_map(raw.get("type_hints"), f"{path}: type_hints"),
        metric_hints=_parse_metric_hints(raw.get("metric_hints"), path),
        nlu_hints=_parse_nlu_hints(raw.get("nlu_hints"), path),
        few_shots_ref=_optional_str(raw.get("few_shots_ref"), f"{path}: few_shots_ref"),
    )


def _optional_str(value: Any, field_label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"dsn_profile: {field_label} must be a string or null")
    return value


def _coerce_tokens(value: Any, field_label: str) -> tuple[str, ...]:
    """list[str] или {'tokens': list[str], ...} (расширенный формат type_hints).

    Взвешенный расширенный формат ``{tokens, weight_solo, weight_with_signal}``
    (``column_aliases_config.py``, T4.26) в DSN-профиле не хранится — тут
    только плоские списки токенов. Присутствие весов в ``value``
    — fail-fast: оператор, вручную редактирующий ``sqlrag/<dsn>.profile.yaml``,
    иначе не заметит, что веса тихо потеряются.
    """
    if value is None:
        return ()
    if isinstance(value, dict):
        unsupported = {"weight_solo", "weight_with_signal"} & set(value)
        if unsupported:
            raise ValueError(
                f"dsn_profile: {field_label}: веса в DSN-профиле не поддерживаются "
                f"(unsupported keys: {sorted(unsupported)}); задайте плоский список "
                "токенов без weight_solo/weight_with_signal"
            )
        value = value.get("tokens", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"dsn_profile: {field_label} must be a list of non-empty strings")
    return tuple(value)


def _parse_pattern_pairs(value: Any, field_label: str) -> tuple[tuple[str, str], ...]:
    """list[{'pattern': str, 'description': str}] → tuple[(pattern, description), ...].

    Тот же raw-формат, что ``significance.yaml`` (``significance_config.py``,
    ``_coerce_pattern_pairs``), но БЕЗ компиляции regex: DSN-профиль хранит
    паттерн как текст, компиляция — забота потребителя.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"dsn_profile: {field_label} must be a list of mappings")
    result: list[tuple[str, str]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"dsn_profile: {field_label}[{idx}] must be a mapping")
        unexpected = set(entry) - _PATTERN_PAIR_KEYS
        if unexpected:
            raise ValueError(
                f"dsn_profile: {field_label}[{idx}] has unexpected keys: {sorted(unexpected)}"
            )
        pattern = entry.get("pattern")
        description = entry.get("description", "")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(
                f"dsn_profile: {field_label}[{idx}].pattern must be a non-empty string"
            )
        if not isinstance(description, str):
            raise ValueError(
                f"dsn_profile: {field_label}[{idx}].description must be a string"
            )
        result.append((pattern, description))
    return tuple(result)


def _parse_str_list_map(value: Any, field_label: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"dsn_profile: {field_label} must be a mapping")
    result: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"dsn_profile: {field_label} keys must be non-empty strings")
        result[key] = _coerce_tokens(items, f"{field_label}.{key}")
    return result


def _parse_glossary(value: Any, path: Path) -> tuple[GlossaryEntry, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"dsn_profile: {path}: glossary must be a list")
    entries: list[GlossaryEntry] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"dsn_profile: {path}: glossary[{idx}] must be a mapping")
        unexpected = set(item) - _GLOSSARY_ENTRY_KEYS
        if unexpected:
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}] has unexpected keys: {sorted(unexpected)}"
            )
        term = item.get("term")
        table = item.get("table")
        if not isinstance(term, str) or not term:
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}].term must be a non-empty string"
            )
        if not isinstance(table, str) or not table:
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}].table must be a non-empty string"
            )
        column = item.get("column")
        if column is not None and not isinstance(column, str):
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}].column must be a string or null"
            )
        kind = item.get("kind")
        if kind is not None and kind not in _GLOSSARY_KIND_VALUES:
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}].kind must be one of "
                f"{_GLOSSARY_KIND_VALUES} or null"
            )
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(
                f"dsn_profile: {path}: glossary[{idx}].note must be a string or null"
            )
        entries.append(
            GlossaryEntry(
                term=term,
                synonyms=_coerce_tokens(item.get("synonyms"), f"glossary[{idx}].synonyms"),
                table=table,
                column=column,
                kind=kind,
                note=note,
            )
        )
    return tuple(entries)


def _parse_prefer_rules(value: Any, path: Path) -> tuple[PreferIdOverNameRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"dsn_profile: {path}: prefer_id_over_name_rules must be a list")
    rules: list[PreferIdOverNameRule] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f"dsn_profile: {path}: prefer_id_over_name_rules[{idx}] must be a mapping"
            )
        unexpected = set(item) - _PREFER_RULE_KEYS
        if unexpected:
            raise ValueError(
                f"dsn_profile: {path}: prefer_id_over_name_rules[{idx}] has unexpected "
                f"keys: {sorted(unexpected)}"
            )
        id_column = item.get("id_column")
        ignore_column = item.get("ignore_column")
        if not isinstance(id_column, str) or not id_column:
            raise ValueError(
                f"dsn_profile: {path}: prefer_id_over_name_rules[{idx}].id_column "
                "must be a non-empty string"
            )
        if not isinstance(ignore_column, str) or not ignore_column:
            raise ValueError(
                f"dsn_profile: {path}: prefer_id_over_name_rules[{idx}].ignore_column "
                "must be a non-empty string"
            )
        rules.append(PreferIdOverNameRule(id_column=id_column, ignore_column=ignore_column))
    return tuple(rules)


def _parse_significant_columns(value: Any, path: Path) -> SignificantColumnHints:
    if value is None:
        return SignificantColumnHints()
    if not isinstance(value, dict):
        raise ValueError(f"dsn_profile: {path}: significant_columns must be a mapping")
    unexpected = set(value) - _SIGNIFICANT_COLUMNS_KEYS
    if unexpected:
        raise ValueError(
            f"dsn_profile: {path}: significant_columns has unexpected keys: {sorted(unexpected)}"
        )
    return SignificantColumnHints(
        high_priority_exact=_coerce_tokens(
            value.get("high_priority_exact"), "significant_columns.high_priority_exact"
        ),
        high_priority_compound=_coerce_tokens(
            value.get("high_priority_compound"), "significant_columns.high_priority_compound"
        ),
        medium_priority_patterns=_parse_pattern_pairs(
            value.get("medium_priority_patterns"),
            "significant_columns.medium_priority_patterns",
        ),
        critical_description_keywords=_coerce_tokens(
            value.get("critical_description_keywords"),
            "significant_columns.critical_description_keywords",
        ),
        important_column_name_substrings=_coerce_tokens(
            value.get("important_column_name_substrings"),
            "significant_columns.important_column_name_substrings",
        ),
    )


def _parse_metric_hints(value: Any, path: Path) -> MetricHints:
    if value is None:
        return MetricHints()
    if not isinstance(value, dict):
        raise ValueError(f"dsn_profile: {path}: metric_hints must be a mapping")
    unexpected = set(value) - _METRIC_HINTS_KEYS
    if unexpected:
        raise ValueError(
            f"dsn_profile: {path}: metric_hints has unexpected keys: {sorted(unexpected)}"
        )
    return MetricHints(
        priority_id_columns=_coerce_tokens(
            value.get("priority_id_columns"), "metric_hints.priority_id_columns"
        ),
        low_priority_name_columns=_coerce_tokens(
            value.get("low_priority_name_columns"), "metric_hints.low_priority_name_columns"
        ),
        prefer_id_over_name_rules=_parse_prefer_rules(
            value.get("prefer_id_over_name_rules"), path
        ),
        significant_columns=_parse_significant_columns(value.get("significant_columns"), path),
    )


def _parse_nlu_hints(value: Any, path: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"dsn_profile: {path}: nlu_hints must be a mapping")
    return dict(value)


# ---------------------------------------------------------------------------
# Serialization (used by scaffold/migration scripts).
# ---------------------------------------------------------------------------


def profile_to_mapping(profile: DsnProfile) -> dict[str, Any]:
    """Сериализовать ``DsnProfile`` в plain dict для записи в yaml."""
    return {
        "version": profile.version,
        "dsn_fingerprint": profile.dsn_fingerprint,
        "schema_namespace_version": profile.schema_namespace_version,
        "captured_at": profile.captured_at,
        "glossary": [
            {
                "term": entry.term,
                "synonyms": list(entry.synonyms),
                "table": entry.table,
                "column": entry.column,
                "kind": entry.kind,
                "note": entry.note,
            }
            for entry in profile.glossary
        ],
        "aliases": {key: list(values) for key, values in profile.aliases.items()},
        "type_hints": {key: list(values) for key, values in profile.type_hints.items()},
        "metric_hints": {
            "priority_id_columns": list(profile.metric_hints.priority_id_columns),
            "low_priority_name_columns": list(profile.metric_hints.low_priority_name_columns),
            "prefer_id_over_name_rules": [
                {"id_column": rule.id_column, "ignore_column": rule.ignore_column}
                for rule in profile.metric_hints.prefer_id_over_name_rules
            ],
            "significant_columns": {
                "high_priority_exact": list(
                    profile.metric_hints.significant_columns.high_priority_exact
                ),
                "high_priority_compound": list(
                    profile.metric_hints.significant_columns.high_priority_compound
                ),
                "medium_priority_patterns": [
                    {"pattern": pattern, "description": description}
                    for pattern, description in (
                        profile.metric_hints.significant_columns.medium_priority_patterns
                    )
                ],
                "critical_description_keywords": list(
                    profile.metric_hints.significant_columns.critical_description_keywords
                ),
                "important_column_name_substrings": list(
                    profile.metric_hints.significant_columns.important_column_name_substrings
                ),
            },
        },
        "nlu_hints": dict(profile.nlu_hints),
        "few_shots_ref": profile.few_shots_ref,
    }


def dump_profile_yaml(profile: DsnProfile) -> str:
    """Сериализовать ``DsnProfile`` в yaml-текст (используется скриптами)."""
    return yaml.safe_dump(
        profile_to_mapping(profile),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


__all__ = [
    "GlossaryEntry",
    "PreferIdOverNameRule",
    "SignificantColumnHints",
    "MetricHints",
    "DsnProfile",
    "dsn_profile_path",
    "load_dsn_profile",
    "load_dsn_profile_or_empty",
    "reset_cache",
    "profile_to_mapping",
    "dump_profile_yaml",
]
