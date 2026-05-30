"""
Загрузчик yaml-конфига весов для find_main_table.

Конфиг — единственный source of truth для структурных весов скоринга
основной таблицы (PK/FK/numeric/количество колонок). Магических чисел
``* 10`` / ``* 5`` / ``* 3`` в .py файлах больше нет (см. AGENTS.md, T4.5).

Контракт (v2, W3-T4):
  * Путь по умолчанию: ``config/text_to_sql/main_table_scoring.yaml``.
  * Путь переопределяется через env
    ``TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH``.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое организовано по профилям (``profiles.<name>``). Профиль
    ``default`` обязателен. Активный профиль выбирается через env
    ``TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE`` (по умолчанию ``"default"``).
  * Несуществующий профиль → ``KeyError`` (fail-fast).
  * yaml без секции ``profiles`` → ``ValueError`` (fail-fast). Legacy
    flat-формат больше не поддерживается (W3-T4).
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` для
    тестов.

``load_main_table_scoring_config()`` возвращает объект ``MainTableScoringConfig``
для активного профиля. Поля (``semantic_match_weight``, ``pk_weight``, ...)
доступны напрямую, как и раньше — публичный API caller'ов не меняется.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from ._yaml_config_loader import (
    YamlConfigLoader,
    build_mapping_error_message,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "text_to_sql" / "main_table_scoring.yaml"
)

_ENV_PATH_VAR = "TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE"
_DEFAULT_PROFILE = "default"


class MainTableScoringConfig:
    """Структурные веса скоринга для find_main_table (один профиль)."""

    __slots__ = (
        "version",
        "profile_name",
        "semantic_match_weight",
        "pk_weight",
        "fk_weight",
        "numeric_weight",
        "columns_count_weight",
        "min_score_for_pick",
        "source_path",
    )

    def __init__(
        self,
        body: Mapping[str, Any],
        *,
        version: Any,
        profile_name: str,
        source_path: str,
    ) -> None:
        self.source_path = source_path
        self.version = version
        self.profile_name = profile_name

        field_ns = f"profiles.{profile_name}"
        # EPIC 5.7: единый helper _coerce_weight с явным per-field
        # min_value заменяет россыпь _coerce_int + ad-hoc проверок.
        # Структурные веса >= 0 (отрицательные ломают скоринг).
        self.semantic_match_weight = _coerce_weight(
            body.get("semantic_match_weight"),
            f"{field_ns}.semantic_match_weight",
            min_value=0,
        )
        self.pk_weight = _coerce_weight(
            body.get("pk_weight"), f"{field_ns}.pk_weight", min_value=0
        )
        self.fk_weight = _coerce_weight(
            body.get("fk_weight"), f"{field_ns}.fk_weight", min_value=0
        )
        self.numeric_weight = _coerce_weight(
            body.get("numeric_weight"), f"{field_ns}.numeric_weight", min_value=0
        )
        self.columns_count_weight = _coerce_weight(
            body.get("columns_count_weight"),
            f"{field_ns}.columns_count_weight",
            min_value=0,
        )
        # min_score_for_pick == 0 делает fail-fast no-op — таблица всегда
        # «выигрывает», даже если у неё ни одного сигнала. Требуем >= 1.
        self.min_score_for_pick = _coerce_weight(
            body.get("min_score_for_pick"),
            f"{field_ns}.min_score_for_pick",
            min_value=1,
            allow_zero=False,
        )


class _MainTableScoringDocument:
    """Все профили из yaml. Внутреннее представление, не наружу."""

    __slots__ = ("version", "profiles", "source_path")

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            # W3-T4: legacy flat-формат больше не поддерживается. yaml
            # обязан содержать секцию ``profiles`` (см. остальные конфиги
            # text-to-sql — significance/column_aliases/safety/nlu_morphemes).
            raise ValueError(
                f"main_table_scoring.yaml at {source_path}: 'profiles' must "
                "be a non-empty mapping. Legacy flat layout is no longer "
                "supported (W3-T4) — migrate to: profiles: default: ..."
            )

        profiles: Dict[str, Mapping[str, Any]] = {}
        for name, body in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "main_table_scoring.yaml: profile names must be non-empty strings"
                )
            if not isinstance(body, dict):
                raise ValueError(
                    f"main_table_scoring.yaml: profile '{name}' must be a mapping"
                )
            profiles[name] = body

        if _DEFAULT_PROFILE not in profiles:
            raise ValueError(
                f"main_table_scoring.yaml at {source_path}: profile "
                f"'{_DEFAULT_PROFILE}' is required"
            )

        self.profiles: Dict[str, Mapping[str, Any]] = profiles

    def build(self, profile_name: str) -> MainTableScoringConfig:
        if profile_name not in self.profiles:
            raise KeyError(
                f"main_table_scoring.yaml: unknown profile '{profile_name}'. "
                f"Available: {sorted(self.profiles)}"
            )
        return MainTableScoringConfig(
            self.profiles[profile_name],
            version=self.version,
            profile_name=profile_name,
            source_path=self.source_path,
        )


def _coerce_weight(
    value: Any,
    field: str,
    *,
    min_value: int = 0,
    allow_zero: bool = True,
) -> int:
    """Унифицированная валидация целочисленных весов из yaml.

    Контракт:
      * ``None`` / ``bool`` / не-int → ``ValueError`` (fail-fast).
      * Значение должно быть >= ``min_value``. ``allow_zero=False``
        дополнительно отвергает 0 (для min_score_for_pick).
    """
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"main_table_scoring.yaml: {field} must be an integer (got {value!r})"
        )
    if not allow_zero and value == 0:
        raise ValueError(
            f"main_table_scoring.yaml: {field} must be > 0 (got 0); value 0 "
            "would disable the fail-fast threshold and let any table win "
            "without any signal."
        )
    if value < min_value:
        raise ValueError(
            f"main_table_scoring.yaml: {field} must be >= {min_value} "
            f"(got {value!r})"
        )
    return value



def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Main-table scoring config not found at "
        f"{path}. Set {env_var} or create "
        f"config/text_to_sql/main_table_scoring.yaml. "
        "find_main_table requires an explicit yaml source of truth "
        "for structural weights."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "main_table_scoring.yaml")


def resolve_active_profile() -> str:
    """Вернуть имя активного профиля.

    Приоритет: env ``TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE`` → ``"default"``.
    """
    return _shared_resolve_active_profile_name(
        None, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


_loader: YamlConfigLoader[_MainTableScoringDocument] = YamlConfigLoader[
    _MainTableScoringDocument
](
    env_path_var=_ENV_PATH_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: _MainTableScoringDocument(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
    # Документ (все профили) кэшируется только по пути. Выбор активного
    # профиля — лёгкий dict-lookup, делается на каждом ``load()``
    # (см. ``load_main_table_scoring_config``).
)


def load_main_table_scoring_config() -> MainTableScoringConfig:
    """Загрузить и закэшировать веса скоринга основной таблицы.

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    Возвращается ``MainTableScoringConfig`` для активного профиля
    (см. ``resolve_active_profile``).
    """
    document = _loader.load()
    return document.build(resolve_active_profile())


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env)."""
    _loader.reset_cache()
