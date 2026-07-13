"""
Общий ``YamlConfigLoader[T]`` для yaml-конфигов text-to-sql (EPIC 8.7).

В пакете ``custom_tools/text_to_sql`` исторически 8+ загрузчиков yaml-
конфигов (``safety_config``, ``type_categories_config``,
``llm_models_config`` и т.д.) повторяют почти один и тот же сценарий:

  * вычислить путь (env-переменная → дефолт);
  * fail-fast при отсутствии файла (с упоминанием env-переменной и пути);
  * yaml.safe_load → проверка top-level mapping → инициализация ``T(raw,
    source_path=abs_key)``;
  * thread-safe кэширование immutable object/SHA snapshot по абсолютному пути
    (или по ``(абсолютный путь, профиль)`` для profile-aware конфигов);
  * ``reset_cache()`` начинает новое поколение snapshot.

Этот модуль вытаскивает общую обвязку. Каждый ``*_config.py`` остаётся
сам по себе (публичный API сохранён, докстринги тоже): он только
делегирует загрузку и сброс кэша экземпляру ``YamlConfigLoader``.

Никаких silent fallback (AGENTS.md): отсутствие файла → ``FileNotFoundError``
с упоминанием env-переменной и пути.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Generic, Hashable, Mapping, Optional, TypeVar

import yaml

T = TypeVar("T")

_ACTIVE_CONFIG_VERSIONS_LOCK = threading.Lock()
_ACTIVE_CONFIG_VERSIONS: Dict[
    str,
    Dict["ConfigSnapshotVersion", int],
] = {}


@dataclass(frozen=True, slots=True)
class ConfigSnapshotVersion:
    source_path: str
    content_sha256: str
    profile: Optional[str]

    def to_mapping(self) -> Dict[str, Optional[str]]:
        return {
            "source_path": self.source_path,
            "content_sha256": self.content_sha256,
            "profile": self.profile,
        }


@dataclass(frozen=True, slots=True)
class _ConfigSnapshot(Generic[T]):
    config: T
    version: ConfigSnapshotVersion


def _version_registry_key(version: ConfigSnapshotVersion) -> str:
    if version.profile is None:
        return version.source_path
    return f"{version.source_path}#profile={version.profile}"


def get_active_yaml_config_versions() -> Dict[str, Dict[str, Optional[str]]]:
    """Return detached, JSON-ready versions published in this process."""
    with _ACTIVE_CONFIG_VERSIONS_LOCK:
        return {
            key: next(reversed(versions)).to_mapping()
            for key, versions in sorted(_ACTIVE_CONFIG_VERSIONS.items())
        }


class YamlConfigLoader(Generic[T]):
    """Generic-загрузчик yaml-конфига с thread-safe кэшем.

    Параметры:
      ``env_path_var`` — имя env-переменной для override-пути; упоминается
          в тексте ошибки при отсутствии файла (AGENTS.md «no silent fallback»).
      ``default_path`` — путь к конфигу, если env-переменная не задана.
      ``parser`` — конструктор ``T``; вызывается как ``parser(raw, abs_key)``
          (для profile-aware loader'ов значение профиля прокидывается через
          замыкание; см. ``profile_extra``).
      ``not_found_message`` — функция, формирующая текст ``FileNotFoundError``
          из ``(абсолютный путь, env_path_var)``. Контракт сообщения каждого
          существующего loader'а сохраняется по месту вызова.
      ``mapping_error_message`` — функция, формирующая текст ``ValueError``
          при некорректном top-level (не mapping).
      ``profile_extra`` — опциональный callable, возвращающий доп. ключ
          (имя профиля). Если задан, кэш ключуется кортежем
          ``(абсолютный путь, profile_extra())``; иначе — только путём.
          Используется profile-aware loader'ами (safety_config), которые
          парсят разные профили из одного yaml-файла.
    """

    __slots__ = (
        "_env_path_var",
        "_default_path",
        "_parser",
        "_not_found_message",
        "_mapping_error_message",
        "_profile_extra",
        "_lock",
        "_cache",
    )

    def __init__(
        self,
        *,
        env_path_var: str,
        default_path: Path,
        parser: Callable[[Dict[str, Any], str], T],
        not_found_message: Callable[[Path, str], str],
        mapping_error_message: Callable[[Path], str],
        profile_extra: Optional[Callable[[], Hashable]] = None,
    ) -> None:
        self._env_path_var = env_path_var
        self._default_path = default_path
        self._parser = parser
        self._not_found_message = not_found_message
        self._mapping_error_message = mapping_error_message
        self._profile_extra = profile_extra
        self._lock = threading.Lock()
        self._cache: Dict[Any, _ConfigSnapshot[T]] = {}

    def _resolve_path(self) -> Path:
        env_path = os.getenv(self._env_path_var)
        if env_path:
            return Path(env_path).expanduser()
        return self._default_path

    def _make_cache_key(self, abs_key: str) -> Any:
        if self._profile_extra is None:
            return abs_key
        return (abs_key, self._profile_extra())

    def load(self) -> T:
        """Загрузить (или вернуть из кэша) разобранный конфиг.

        Контракт ошибок (verbatim из существующих loader'ов):
          * отсутствие файла → ``FileNotFoundError`` с упоминанием env-var
            и пути;
          * top-level не mapping → ``ValueError``;
          * любые валидационные ошибки внутри ``parser`` пробрасываются как
            есть.
        """
        path = self._resolve_path()
        abs_key = str(path.resolve(strict=False))
        cache_key = self._make_cache_key(abs_key)

        with self._lock:
            snapshot = self._cache.get(cache_key)
            if snapshot is not None:
                return snapshot.config

            if not path.is_file():
                raise FileNotFoundError(
                    self._not_found_message(path, self._env_path_var)
                )

            source_bytes = path.read_bytes()
            raw = yaml.safe_load(source_bytes.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(self._mapping_error_message(path))

            config = self._parser(raw, abs_key)
            profile = None
            if isinstance(cache_key, tuple):
                profile = str(cache_key[1])
            version = ConfigSnapshotVersion(
                source_path=abs_key,
                content_sha256=hashlib.sha256(source_bytes).hexdigest(),
                profile=profile,
            )
            snapshot = _ConfigSnapshot(config=config, version=version)
            with _ACTIVE_CONFIG_VERSIONS_LOCK:
                self._cache[cache_key] = snapshot
                registrations = _ACTIVE_CONFIG_VERSIONS.setdefault(
                    _version_registry_key(version),
                    {},
                )
                registrations[version] = registrations.get(version, 0) + 1
            return config

    def active_version(self) -> Optional[ConfigSnapshotVersion]:
        path = self._resolve_path()
        abs_key = str(path.resolve(strict=False))
        cache_key = self._make_cache_key(abs_key)
        with self._lock:
            snapshot = self._cache.get(cache_key)
            return None if snapshot is None else snapshot.version

    def reset_cache(self) -> None:
        """Сброс кэша (для тестов, меняющих env-переменные)."""
        with self._lock:
            with _ACTIVE_CONFIG_VERSIONS_LOCK:
                for snapshot in self._cache.values():
                    registry_key = _version_registry_key(snapshot.version)
                    registrations = _ACTIVE_CONFIG_VERSIONS[registry_key]
                    references = registrations[snapshot.version]
                    if references == 1:
                        registrations.pop(snapshot.version)
                    else:
                        registrations[snapshot.version] = references - 1
                    if not registrations:
                        _ACTIVE_CONFIG_VERSIONS.pop(registry_key, None)
                self._cache.clear()


# ---------------------------------------------------------------------------
# Общие helpers для *_config.py модулей (EPIC 8.7 follow-up).
#
# Каждый ``*_config.py`` повторял одно и то же:
#   * ``_mapping_error_message(path)`` — шаблон ``"<yaml> at <path> must
#     contain a mapping at the top level"``;
#   * ``_coerce_str_list(value, field)`` — yaml → list[str] | [] | ValueError
#     (с одним и тем же телом, отличался только yaml-filename в сообщении);
#   * ``resolve_active_profile_name`` — приоритет explicit → env → default.
#
# Извлекаем их сюда. Контракт текста ошибок сохранён (yaml-filename
# прокидывается параметром). Сообщения ``_not_found_message`` каждый
# loader формирует у себя — они кастомные (упоминают конкретный сценарий
# использования: "PII detection prompt requires...", "Column-aliases
# lookup requires...", и т.п.), поэтому общего шаблона нет.
# ---------------------------------------------------------------------------


def build_mapping_error_message(path: Path, yaml_filename: str) -> str:
    """Сообщение об ошибке при top-level не-mapping в yaml-конфиге.

    Возвращает строку ``"<yaml_filename> at <path> must contain a mapping at
    the top level"`` — общий шаблон для всех ``*_config.py``.
    """
    return f"{yaml_filename} at {path} must contain a mapping at the top level"


def coerce_str_list(
    value: Any,
    field: str,
    *,
    yaml_filename: str,
    reject_empty_strings: bool = False,
) -> list:
    """Привести yaml-значение к ``list[str]``.

    Контракт (идентичный в schema_linking_examples / significance / nlu):
      * ``None`` → ``[]``;
      * ``list`` из строк → ``list(value)``;
      * иначе → ``ValueError`` с упоминанием yaml-файла и поля.

    При ``reject_empty_strings=True`` пустые строки ``""`` отвергаются
    с ``ValueError`` — используй для полей significance/schema-linking,
    где ``"" in column_name.lower()`` всегда True и создаёт silent-баг.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{yaml_filename}: {field} must be a list of strings")
    result = list(value)
    if reject_empty_strings and "" in result:
        raise ValueError(
            f"{yaml_filename}: {field} must not contain empty strings"
        )
    return result


def resolve_active_profile_name(
    explicit: Optional[str],
    *,
    env_var: str,
    default: str,
    umbrella_map: Optional[Mapping[str, str]] = None,
) -> str:
    """Какой профиль использовать.

    Приоритет: явный аргумент (если непустой) → профильная env-переменная
    подсистемы → umbrella env ``TEXT2SQL_PROFILE`` → ``default``. Старые
    ``TEXT_TO_SQL_*_PROFILE`` остаются explicit compatibility overrides.
    """
    if explicit:
        return explicit
    from_env = os.getenv(env_var)
    if from_env:
        return from_env
    umbrella = os.getenv("TEXT2SQL_PROFILE")
    if umbrella:
        if umbrella_map and umbrella in umbrella_map:
            return umbrella_map[umbrella]
        return umbrella
    return default
