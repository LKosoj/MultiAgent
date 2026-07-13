"""
Загрузчик yaml-конфига SQLSafetyValidator.

Конфиг — единственный source of truth для запрещённых ключевых слов,
AST-классов sqlglot, командных слов и числовых лимитов SQL safety-валидатора.
Никаких хардкодов в safety.py быть не должно (см. AGENTS.md, EPIC 2.1).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/safety.yaml`` в корне репо.
  * Путь переопределяется через env ``TEXT_TO_SQL_SAFETY_CONFIG_PATH``.
  * Активный профиль выбирается через env ``TEXT_TO_SQL_SAFETY_PROFILE``
    (default — ``"default"``).
  * Файл и профиль обязательны: при отсутствии — ``SafetyConfigMissing``
    (наследник ``FileNotFoundError``) / ``KeyError`` / ``ValueError``,
    БЕЗ молчаливых дефолтов и embedded-копий (W3-T5).
  * Содержимое кэшируется по паре ``(абсолютный путь, имя профиля)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Tuple

from .._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "safety.yaml"

_ENV_CONFIG_PATH = "TEXT_TO_SQL_SAFETY_CONFIG_PATH"
_ENV_PROFILE = "TEXT_TO_SQL_SAFETY_PROFILE"
_DEFAULT_PROFILE = "default"

logger = logging.getLogger(__name__)


class SafetyConfigMissing(FileNotFoundError):
    """safety.yaml не найден — QA-слой обязан отказать в обработке запроса.

    Наследник ``FileNotFoundError`` для обратной совместимости с
    вызывающим кодом и тестами, которые ловят `FileNotFoundError`.
    Отдельный класс нужен, чтобы (а) caller мог явно отличить
    отсутствие safety.yaml от любых других IO-ошибок, (б) текст ошибки
    подсказывал точку восстановления (см. AGENTS.md — fail-fast,
    no silent fallback).
    """

# Активный профиль для текущего вызова ``load_safety_profile``.
# Profile-aware loader использует thread-local, чтобы ``YamlConfigLoader``-
# обвязка (общая для всех конфигов) могла прочитать имя профиля через
# ``profile_extra`` callable. Каждый вызов ``load_safety_profile`` сначала
# устанавливает thread-local, потом дёргает loader.
_current_profile = threading.local()


class SafetyProfile:
    """Структурированное представление одного профиля safety.yaml."""

    __slots__ = (
        "forbidden_keywords",
        "forbidden_functions",
        "ast_forbidden_stmt_classes",
        "ast_forbidden_command_words",
        "max_query_length",
        "max_in_list_size",
        "source_path",
        "profile_name",
        "config_version",
    )

    def __init__(
        self,
        raw: Dict[str, Any],
        source_path: str,
        profile_name: str,
        config_version: int,
    ) -> None:
        self.source_path = source_path
        self.profile_name = profile_name
        self.config_version = config_version

        self.forbidden_keywords: List[str] = _coerce_nonempty_str_list(
            raw.get("forbidden_keywords"),
            f"profiles.{profile_name}.forbidden_keywords",
        )
        # forbidden_functions — опциональная секция (может отсутствовать или
        # быть пустой). При наличии — список непустых строк.
        self.forbidden_functions: List[str] = _coerce_optional_str_list(
            raw.get("forbidden_functions"),
            f"profiles.{profile_name}.forbidden_functions",
        )
        self.ast_forbidden_stmt_classes: Tuple[str, ...] = tuple(
            _coerce_nonempty_str_list(
                raw.get("ast_forbidden_stmt_classes"),
                f"profiles.{profile_name}.ast_forbidden_stmt_classes",
            )
        )
        self.ast_forbidden_command_words: FrozenSet[str] = frozenset(
            _coerce_nonempty_str_list(
                raw.get("ast_forbidden_command_words"),
                f"profiles.{profile_name}.ast_forbidden_command_words",
            )
        )
        self.max_query_length = _coerce_positive_int(
            raw.get("max_query_length"),
            f"profiles.{profile_name}.max_query_length",
        )
        self.max_in_list_size = _coerce_positive_int(
            raw.get("max_in_list_size"),
            f"profiles.{profile_name}.max_in_list_size",
        )


@dataclass(frozen=True)
class TextToSqlSafetyPolicy:
    level: str
    profile_name: str
    source_path: str
    config_version: int
    policy_version: str
    forbidden_keywords: Tuple[str, ...]
    forbidden_functions: Tuple[str, ...]
    ast_forbidden_stmt_classes: Tuple[str, ...]
    ast_forbidden_command_words: FrozenSet[str]
    max_query_length: int
    max_in_list_size: int

    def __post_init__(self) -> None:
        _validate_policy_fields(self)
        expected = _compute_policy_version(self.to_mapping())
        if self.policy_version != expected:
            raise ValueError(
                "safety policy policy_version does not match its rule snapshot"
            )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "profile_name": self.profile_name,
            "source_path": self.source_path,
            "config_version": self.config_version,
            "policy_version": self.policy_version,
            "forbidden_keywords": list(self.forbidden_keywords),
            "forbidden_functions": list(self.forbidden_functions),
            "ast_forbidden_stmt_classes": list(self.ast_forbidden_stmt_classes),
            "ast_forbidden_command_words": sorted(self.ast_forbidden_command_words),
            "max_query_length": self.max_query_length,
            "max_in_list_size": self.max_in_list_size,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TextToSqlSafetyPolicy":
        if not isinstance(value, Mapping):
            raise ValueError("safety policy must be a mapping")
        expected_keys = {
            "level",
            "profile_name",
            "source_path",
            "config_version",
            "policy_version",
            "forbidden_keywords",
            "forbidden_functions",
            "ast_forbidden_stmt_classes",
            "ast_forbidden_command_words",
            "max_query_length",
            "max_in_list_size",
        }
        if set(value) != expected_keys:
            missing = sorted(expected_keys - set(value))
            unknown = sorted(set(value) - expected_keys)
            raise ValueError(
                f"safety policy keys mismatch: missing={missing}, unknown={unknown}"
            )
        return cls(
            level=_mapping_nonempty_string(value, "level"),
            profile_name=_mapping_nonempty_string(value, "profile_name"),
            source_path=_mapping_nonempty_string(value, "source_path"),
            config_version=_mapping_positive_int(value, "config_version"),
            policy_version=_mapping_nonempty_string(value, "policy_version"),
            forbidden_keywords=_mapping_string_tuple(
                value, "forbidden_keywords", allow_empty=False
            ),
            forbidden_functions=_mapping_string_tuple(
                value, "forbidden_functions", allow_empty=True
            ),
            ast_forbidden_stmt_classes=_mapping_string_tuple(
                value, "ast_forbidden_stmt_classes", allow_empty=False
            ),
            ast_forbidden_command_words=frozenset(
                _mapping_string_tuple(
                    value, "ast_forbidden_command_words", allow_empty=False
                )
            ),
            max_query_length=_mapping_positive_int(value, "max_query_length"),
            max_in_list_size=_mapping_positive_int(value, "max_in_list_size"),
        )


def _mapping_nonempty_string(value: Mapping[str, Any], field: str) -> str:
    item = value[field]
    if not isinstance(item, str) or not item:
        raise ValueError(f"safety policy {field} must be a non-empty string")
    return item


def _mapping_positive_int(value: Mapping[str, Any], field: str) -> int:
    item = value[field]
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ValueError(f"safety policy {field} must be a positive integer")
    return item


def _mapping_string_tuple(
    value: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool,
) -> Tuple[str, ...]:
    items = value[field]
    if not isinstance(items, list) or (not allow_empty and not items):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"safety policy {field} must be {qualifier} of strings")
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(
            f"safety policy {field} must contain only non-empty strings"
        )
    return tuple(items)


def _validate_policy_fields(policy: TextToSqlSafetyPolicy) -> None:
    for field in ("level", "profile_name", "source_path", "policy_version"):
        value = getattr(policy, field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"safety policy {field} must be a non-empty string")
    for field in ("config_version", "max_query_length", "max_in_list_size"):
        value = getattr(policy, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"safety policy {field} must be a positive integer")
    tuple_fields = (
        ("forbidden_keywords", False),
        ("forbidden_functions", True),
        ("ast_forbidden_stmt_classes", False),
    )
    for field, allow_empty in tuple_fields:
        value = getattr(policy, field)
        if not isinstance(value, tuple) or (not allow_empty and not value):
            raise ValueError(f"safety policy {field} must be an immutable tuple")
        if not all(isinstance(item, str) and item for item in value):
            raise ValueError(
                f"safety policy {field} must contain only non-empty strings"
            )
    commands = policy.ast_forbidden_command_words
    if not isinstance(commands, frozenset) or not commands:
        raise ValueError(
            "safety policy ast_forbidden_command_words must be a non-empty frozenset"
        )
    if not all(isinstance(item, str) and item for item in commands):
        raise ValueError(
            "safety policy ast_forbidden_command_words must contain only non-empty strings"
        )


def _compute_policy_version(policy: Mapping[str, Any]) -> str:
    payload = {
        "config_version": policy["config_version"],
        "profile_name": policy["profile_name"],
        "forbidden_keywords": sorted(set(policy["forbidden_keywords"])),
        "forbidden_functions": sorted(set(policy["forbidden_functions"])),
        "ast_forbidden_stmt_classes": sorted(
            set(policy["ast_forbidden_stmt_classes"])
        ),
        "ast_forbidden_command_words": sorted(
            policy["ast_forbidden_command_words"]
        ),
        "max_query_length": policy["max_query_length"],
        "max_in_list_size": policy["max_in_list_size"],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _policy_from_profile(level: str, profile: SafetyProfile) -> TextToSqlSafetyPolicy:
    values = {
        "level": level,
        "profile_name": profile.profile_name,
        "source_path": profile.source_path,
        "config_version": profile.config_version,
        "forbidden_keywords": tuple(profile.forbidden_keywords),
        "forbidden_functions": tuple(profile.forbidden_functions),
        "ast_forbidden_stmt_classes": tuple(profile.ast_forbidden_stmt_classes),
        "ast_forbidden_command_words": frozenset(
            profile.ast_forbidden_command_words
        ),
        "max_query_length": profile.max_query_length,
        "max_in_list_size": profile.max_in_list_size,
    }
    return TextToSqlSafetyPolicy(
        **values,
        policy_version=_compute_policy_version(values),
    )


def _coerce_nonempty_str_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"safety.yaml: {field} must be a non-empty list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"safety.yaml: {field} must contain only non-empty strings")
    return list(value)


def _coerce_optional_str_list(value: Any, field: str) -> List[str]:
    """Опциональная секция: None/[] допустимы; иначе список непустых строк."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"safety.yaml: {field} must be a list of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"safety.yaml: {field} must contain only non-empty strings")
    return list(value)


def _coerce_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"safety.yaml: {field} must be a positive integer")
    return value


def _resolve_profile_name(profile: str | None) -> str:
    if profile is not None and not isinstance(profile, str):
        raise ValueError("safety profile name must be a string")
    if profile is not None and not profile:
        raise ValueError("safety profile name must be a non-empty string")
    return _shared_resolve_active_profile_name(
        profile,
        env_var=_ENV_PROFILE,
        default=_DEFAULT_PROFILE,
        umbrella_map={"muni_ru": _DEFAULT_PROFILE},
    )


def _not_found_message(path: Path, env_var: str) -> str:
    # Контракт сообщения зафиксирован в W3-T5: первая фраза должна
    # называть путь, вторая — объяснять, что без файла QA-слой не работает.
    # Расширенный текст с env-var оставлен для удобства диагностики.
    return (
        f"safety.yaml not found at {path}. Required for QA layer. "
        f"Set {env_var} or create config/text_to_sql/safety.yaml."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "safety.yaml")


def _parse_profile(raw: Dict[str, Any], abs_key: str) -> "SafetyProfile":
    """Извлекает и валидирует профиль, имя которого хранится в thread-local.

    Profile-aware часть: profile_extra callable использует ту же
    ``_current_profile.name`` для cache-ключа, поэтому имя профиля известно
    и парсеру, и кэшу.
    """
    profile_name = getattr(_current_profile, "name", None)
    if not profile_name:
        # Защитная программистская ошибка: parser вызывается только из
        # load_safety_profile, который выставляет thread-local. Если попали
        # сюда без него — это баг рефакторинга, fail-fast.
        raise RuntimeError(
            "safety_config: profile name not set in thread-local; "
            "load_safety_profile must wrap _loader.load()"
        )

    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(
            f"safety.yaml at {abs_key}: top-level 'profiles' must be a non-empty mapping"
        )

    if profile_name not in profiles:
        raise KeyError(
            f"safety.yaml at {abs_key}: profile '{profile_name}' not found. "
            f"Available profiles: {sorted(profiles.keys())}"
        )

    profile_raw = profiles[profile_name]
    if not isinstance(profile_raw, dict):
        raise ValueError(
            f"safety.yaml at {abs_key}: profiles.{profile_name} must be a mapping"
        )

    config_version = _coerce_positive_int(raw.get("version"), "version")
    return SafetyProfile(
        profile_raw,
        source_path=abs_key,
        profile_name=profile_name,
        config_version=config_version,
    )


_loader: YamlConfigLoader["SafetyProfile"] = YamlConfigLoader["SafetyProfile"](
    env_path_var=_ENV_CONFIG_PATH,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=_parse_profile,
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
    profile_extra=lambda: getattr(_current_profile, "name", _DEFAULT_PROFILE),
)


def load_safety_profile(profile: str | None = None) -> SafetyProfile:
    """Загрузить и закэшировать профиль safety.yaml.

    Поведение строго **fail-fast** (см. AGENTS.md, W3-T5):
      * отсутствие файла → ``SafetyConfigMissing`` (наследник
        ``FileNotFoundError``) с подсказкой про
        ``config/text_to_sql/safety.yaml`` и env ``TEXT_TO_SQL_SAFETY_CONFIG_PATH``;
      * отсутствие профиля → ``KeyError``;
      * структурные ошибки → ``ValueError``.

    Никаких молчаливых дефолтов и embedded-копий конфига: yaml —
    единственный source of truth. Если QA-слою действительно нужен
    запасной набор правил, его место — отдельный профиль в yaml, который
    активируется через ``TEXT_TO_SQL_SAFETY_PROFILE`` явно.

    Кэшируется по паре ``(абсолютный путь, имя профиля)``.
    """
    profile_name = _resolve_profile_name(profile)
    prev = getattr(_current_profile, "name", None)
    _current_profile.name = profile_name
    try:
        try:
            profile_obj = _loader.load()
        except FileNotFoundError as exc:
            # Пере-кидываем как SafetyConfigMissing, чтобы caller мог явно
            # отличить отсутствие safety.yaml от прочих IO-ошибок и
            # отказать в обработке запроса с понятной диагностикой.
            # SafetyConfigMissing наследуется от FileNotFoundError, так что
            # существующие `except FileNotFoundError` продолжают работать.
            raise SafetyConfigMissing(str(exc)) from exc
        return profile_obj
    finally:
        if prev is None:
            try:
                del _current_profile.name
            except AttributeError:
                pass
        else:
            _current_profile.name = prev


def resolve_safety_policy(level: str) -> TextToSqlSafetyPolicy:
    if not isinstance(level, str) or not level.strip():
        raise ValueError("safety level must be a non-empty string")
    normalized = level.strip().lower()
    if normalized != "strict":
        raise ValueError("unsupported safety level; only 'strict' is supported")
    return _policy_from_profile(normalized, load_safety_profile("strict"))


@lru_cache(maxsize=1)
def load_startup_safety_policy() -> TextToSqlSafetyPolicy:
    profile_name = _resolve_profile_name(None)
    policy = _policy_from_profile(
        profile_name,
        load_safety_profile(profile_name),
    )
    logger.info(
        "Loaded Text-to-SQL startup safety policy profile=%s version=%s",
        policy.profile_name,
        policy.policy_version,
    )
    return policy

def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env-переменной)."""
    _loader.reset_cache()
    load_startup_safety_policy.cache_clear()


def reload_safety_config() -> None:
    """Явно сбросить кеш safety.yaml.

    W8-T5: предназначен для admin endpoint или тестов, когда нужно
    подхватить изменения ``TEXT_TO_SQL_SAFETY_PROFILE`` или содержимого
    ``config/text_to_sql/safety.yaml`` без рестарта процесса.

    Контракт:
      * Только explicit API. Никакого автоматического watcher mtime —
        в production такой watcher провоцирует гонки между
        ``reload`` и параллельными ``validate(...)``.
      * Не пере-создаёт ``SQLSafetyValidator``; для этого вызывайте
        ``SQLSafetyValidator.reload()`` (см. validators/safety.py).

    Рекомендации по эксплуатации:
      * В production runtime безопаснее **перезапуск процесса** — он
        исключает риск, что часть конкурентных запросов проверится
        старым профилем, а часть — новым.
      * Этот API не предотвращает гонки между ``validate(...)`` уже
        созданного валидатора и сменой конфига. Caller обязан гарантировать,
        что валидаторы пере-создаются после reload (например, через
        ``SQLSafetyValidator.reload()`` в admin handler-е).
    """
    reset_cache()
    from ..core._sql_generation_api import _clear_llm_safety_cache

    _clear_llm_safety_cache()
