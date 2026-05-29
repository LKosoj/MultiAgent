"""
Загрузчик yaml-конфига NLU fallback-эвристики.

Конфиг — единственный source of truth для морфем/regex, на которые опирается
``_fallback_extract_intent`` и ``_fallback_tokenize``. В .py файлах ни одной
русской морфемы или regex-паттерна быть не должно (см. AGENTS.md, T4.1).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/nlu_morphemes.yaml`` в корне репо.
  * Путь переопределяется через env ``TEXT_TO_SQL_NLU_MORPHEMES_PATH``.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути (изменение env требует
    рестарта процесса).
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Pattern, Tuple

import yaml

from ._yaml_config_loader import (
    coerce_str_list,
    resolve_active_profile_name as _shared_resolve_active_profile_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "text_to_sql" / "nlu_morphemes.yaml"

_ENV_VAR = "TEXT_TO_SQL_NLU_MORPHEMES_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_NLU_PROFILE"
_DEFAULT_PROFILE = "default"

# Поля, которые могут жить либо на верхнем уровне yaml (flat layout,
# backward-compat с pre-W3 конфигом), либо внутри ``profiles.<name>``
# (W3-T1: профиль-ориентированный layout, default = пустой/нейтральный,
# доменные RU морфемы — в ``profiles.muni_ru``).
_PROFILE_OVERRIDABLE_KEYS = (
    "enabled",
    "intents",
    "dimensions",
    "relative_date",
    "patterns",
    "order",
    "intent_rules",
    "default_intent",
    "top_n_intent",
    "tokenizer",
    "regions",
)


class NLUMorphemesRegistry:
    """Scoped registry для кэша конфигов NLU.

    Хранит prepared ``NLUMorphemes`` по абсолютному пути yaml-файла.
    Используется как DI-объект: в production один глобальный экземпляр
    (см. ``_DEFAULT_REGISTRY``), в тестах — свой через ``nlu_morphemes_scope``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, "NLUMorphemes"] = {}

    def get_or_load(self, path: Path) -> "NLUMorphemes":
        abs_key = str(path.resolve(strict=False))
        # W3-T1: cache key учитывает активный профиль — переключение
        # ``TEXT_TO_SQL_NLU_PROFILE`` между muni_ru и default в рамках
        # одного процесса (например, в тестах) должно давать разные
        # объекты, без явного ``reset_cache``.
        profile = resolve_active_profile()
        cache_key = (abs_key, profile)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            if not path.is_file():
                raise FileNotFoundError(
                    "NLU morphemes config not found at "
                    f"{path}. Set {_ENV_VAR} or create "
                    f"config/text_to_sql/nlu_morphemes.yaml. "
                    "Heuristic NLU fallback requires an explicit yaml source of truth."
                )

            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"nlu_morphemes.yaml at {path} must contain a mapping at the top level"
                )

            resolved = _apply_profile(raw, profile, source=str(path))
            morphemes = NLUMorphemes(resolved, source_path=abs_key)
            self._cache[cache_key] = morphemes
            return morphemes

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_DEFAULT_REGISTRY = NLUMorphemesRegistry()

# Активный registry для текущего scope. Не подменяется напрямую — только
# через ``nlu_morphemes_scope`` (DI / context-scope).
_active_registry: NLUMorphemesRegistry = _DEFAULT_REGISTRY


class NLUMorphemes:
    """Структурированное представление nlu_morphemes.yaml с прекомпилированными regex'ами."""

    __slots__ = (
        "version",
        "language",
        "enabled",
        "intents",
        "dimensions",
        "relative_date_triggers",
        "relative_date_periods",
        "relative_date_days_pattern",
        "patterns_date_iso",
        "patterns_region",
        "patterns_amount_greater",
        "patterns_amount_less",
        "patterns_amount_between",
        "patterns_top_n",
        "order_triggers",
        "order_desc_triggers",
        "intent_rules",
        "default_intent",
        "top_n_intent",
        "tokenizer_adpositions",
        "regions_normalize",
        "table_name_inflections_enabled",
        "table_name_pluralizers",
        "source_path",
    )

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")
        self.language = raw.get("language")

        # Явный feature-flag (см. 3.15). Если ключ отсутствует — закрыто
        # по умолчанию, чтобы heuristic fallback не активировался неявно
        # из чужого yaml.
        enabled_raw = raw.get("enabled", False)
        if not isinstance(enabled_raw, bool):
            raise ValueError(
                "nlu_morphemes.yaml: 'enabled' must be a boolean (true|false)"
            )
        self.enabled: bool = enabled_raw

        self.intents: List[Dict[str, Any]] = _coerce_canonical_groups(raw.get("intents"), "intents")
        self.dimensions: List[Dict[str, Any]] = _coerce_canonical_groups(raw.get("dimensions"), "dimensions")

        rel = raw.get("relative_date") or {}
        self.relative_date_triggers: List[str] = _coerce_str_list(rel.get("triggers"), "relative_date.triggers")
        self.relative_date_periods: List[Dict[str, Any]] = _coerce_canonical_groups(
            rel.get("periods"), "relative_date.periods"
        )
        days_pattern = rel.get("days_pattern")
        self.relative_date_days_pattern: Pattern[str] = _compile_pattern(
            days_pattern, "relative_date.days_pattern"
        )

        patterns = raw.get("patterns") or {}
        self.patterns_date_iso: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("date_iso"), "patterns.date_iso"
        )
        self.patterns_region: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("region"), "patterns.region"
        )
        self.patterns_amount_greater: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("amount_greater"), "patterns.amount_greater"
        )
        self.patterns_amount_less: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("amount_less"), "patterns.amount_less"
        )
        self.patterns_amount_between: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("amount_between"), "patterns.amount_between"
        )
        self.patterns_top_n: List[Pattern[str]] = _compile_pattern_list(
            patterns.get("top_n"), "patterns.top_n"
        )

        order = raw.get("order") or {}
        self.order_triggers: List[str] = _coerce_str_list(order.get("triggers"), "order.triggers")
        self.order_desc_triggers: List[str] = _coerce_str_list(order.get("desc_triggers"), "order.desc_triggers")

        self.intent_rules: List[Dict[str, Any]] = _coerce_canonical_groups(
            raw.get("intent_rules"), "intent_rules"
        )
        default_intent = raw.get("default_intent")
        top_n_intent = raw.get("top_n_intent")
        if not isinstance(default_intent, str) or not default_intent:
            raise ValueError("nlu_morphemes.yaml: default_intent must be a non-empty string")
        if not isinstance(top_n_intent, str) or not top_n_intent:
            raise ValueError("nlu_morphemes.yaml: top_n_intent must be a non-empty string")
        self.default_intent = default_intent
        self.top_n_intent = top_n_intent

        tokenizer = raw.get("tokenizer") or {}
        self.tokenizer_adpositions: List[str] = _coerce_str_list(
            tokenizer.get("adpositions"), "tokenizer.adpositions"
        )

        regions_block = raw.get("regions") or {}
        if not isinstance(regions_block, dict):
            raise ValueError("nlu_morphemes.yaml: 'regions' must be a mapping")
        normalize = regions_block.get("normalize") or {}
        if not isinstance(normalize, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in normalize.items()
        ):
            raise ValueError(
                "nlu_morphemes.yaml: regions.normalize must be a mapping of str->str"
            )
        # Ключи приводим к lower для матчинга, значения — канонические формы.
        self.regions_normalize: Dict[str, str] = {
            k.lower(): v for k, v in normalize.items()
        }

        # EPIC 5.1: английская плюрализация имён таблиц.
        # Раньше JoinBuilder.infer_joins_by_convention хардкодил
        # ``name[:-1]`` и ``f"{base}s"``. Теперь правила живут здесь.
        inflections_block = raw.get("table_name_inflections")
        if inflections_block is None:
            # Секция опциональна — отсутствие = inflections отключены.
            self.table_name_inflections_enabled = False
            self.table_name_pluralizers: List[Tuple[str, str]] = []
        else:
            if not isinstance(inflections_block, dict):
                raise ValueError(
                    "nlu_morphemes.yaml: 'table_name_inflections' must be a mapping"
                )
            enabled = inflections_block.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError(
                    "nlu_morphemes.yaml: table_name_inflections.enabled must be bool"
                )
            self.table_name_inflections_enabled = enabled
            raw_pluralizers = inflections_block.get("pluralizers") or []
            if not isinstance(raw_pluralizers, list):
                raise ValueError(
                    "nlu_morphemes.yaml: table_name_inflections.pluralizers "
                    "must be a list of [suffix, plural_suffix] pairs"
                )
            pluralizers: List[Tuple[str, str]] = []
            for idx, pair in enumerate(raw_pluralizers):
                if (
                    not isinstance(pair, (list, tuple))
                    or len(pair) != 2
                    or not all(isinstance(item, str) for item in pair)
                ):
                    raise ValueError(
                        "nlu_morphemes.yaml: table_name_inflections."
                        f"pluralizers[{idx}] must be a [suffix, plural_suffix] "
                        "pair of strings"
                    )
                pluralizers.append((pair[0], pair[1]))
            self.table_name_pluralizers = pluralizers


def _coerce_str_list(value: Any, field: str) -> List[str]:
    return coerce_str_list(value, field, yaml_filename="nlu_morphemes.yaml")


def _coerce_canonical_groups(value: Any, field: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"nlu_morphemes.yaml: {field} must be a list")
    groups: List[Dict[str, Any]] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"nlu_morphemes.yaml: {field}[{idx}] must be a mapping")
        canonical = entry.get("canonical")
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"nlu_morphemes.yaml: {field}[{idx}].canonical must be a non-empty string")
        morphemes = _coerce_str_list(entry.get("morphemes"), f"{field}[{idx}].morphemes")
        groups.append({"canonical": canonical, "morphemes": morphemes})
    return groups


def _compile_pattern(pattern: Any, field: str) -> Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"nlu_morphemes.yaml: {field} must be a non-empty regex string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"nlu_morphemes.yaml: {field} is not a valid regex: {exc}") from exc


def _compile_pattern_list(value: Any, field: str) -> List[Pattern[str]]:
    raw_list = _coerce_str_list(value, field)
    compiled: List[Pattern[str]] = []
    for idx, expr in enumerate(raw_list):
        try:
            compiled.append(re.compile(expr))
        except re.error as exc:
            raise ValueError(f"nlu_morphemes.yaml: {field}[{idx}] is not a valid regex: {exc}") from exc
    return compiled


def _resolve_path() -> Path:
    env_path = os.getenv(_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    return _DEFAULT_CONFIG_PATH


def resolve_active_profile() -> str:
    """Вернуть имя активного NLU-профиля.

    Приоритет: env ``TEXT_TO_SQL_NLU_PROFILE`` → ``"default"``.

    Контракт (W3-T1):
      * При активном профиле ``default`` (или если env не задан) yaml
        в profile-aware layout отдаёт пустой/нейтральный набор морфем
        (``enabled: false``, intents/dimensions/intent_rules: []) —
        non-RU инсталляции не получают RU интентов в fallback'е.
      * ``TEXT_TO_SQL_NLU_PROFILE=muni_ru`` восстанавливает legacy
        набор морфем под муниципальный РФ-датасет.
    """
    return _shared_resolve_active_profile_name(
        None, env_var=_ENV_PROFILE_VAR, default=_DEFAULT_PROFILE
    )


def _apply_profile(
    raw: Dict[str, Any], profile_name: str, *, source: str
) -> Dict[str, Any]:
    """Развернуть profile-aware yaml в flat-mapping для ``NLUMorphemes``.

    Поведение:
      * Если в ``raw`` нет ключа ``profiles`` — yaml в legacy flat layout
        (pre-W3); возвращаем как есть (backward-compat с существующими
        тестами и tmp-yaml в fixtures).
      * Иначе: берём профиль ``profiles[profile_name]`` (fail-fast при
        отсутствии), overlay его поля поверх top-level raw для всех
        ``_PROFILE_OVERRIDABLE_KEYS``. Поля, отсутствующие в профиле,
        наследуются от top-level (это даёт shared structural константы —
        ``version``/``language``/``table_name_inflections``).
    """
    profiles_block = raw.get("profiles")
    if profiles_block is None:
        return raw
    if not isinstance(profiles_block, dict) or not profiles_block:
        raise ValueError(
            f"nlu_morphemes.yaml at {source}: 'profiles' must be a non-empty mapping"
        )
    if profile_name not in profiles_block:
        raise KeyError(
            f"nlu_morphemes.yaml at {source}: unknown profile '{profile_name}'. "
            f"Available: {sorted(profiles_block)}"
        )
    profile_body = profiles_block[profile_name]
    if not isinstance(profile_body, dict):
        raise ValueError(
            f"nlu_morphemes.yaml at {source}: profile '{profile_name}' must be a mapping"
        )

    merged: Dict[str, Any] = {k: v for k, v in raw.items() if k != "profiles"}
    for key in _PROFILE_OVERRIDABLE_KEYS:
        if key in profile_body:
            merged[key] = profile_body[key]
    return merged


def load_nlu_morphemes(
    *, registry: Optional[NLUMorphemesRegistry] = None
) -> NLUMorphemes:
    """Загрузить и закэшировать конфиг морфем NLU.

    По умолчанию использует активный (scoped) registry. Тесты и DI-сценарии
    могут передать свой ``NLUMorphemesRegistry`` явно, либо использовать
    ``nlu_morphemes_scope`` как context manager.
    """

    reg = registry if registry is not None else _active_registry
    return reg.get_or_load(_resolve_path())


@contextlib.contextmanager
def nlu_morphemes_scope(
    registry: Optional[NLUMorphemesRegistry] = None,
) -> Iterator[NLUMorphemesRegistry]:
    """Контекстный scope для NLU-кэша.

    Внутри блока ``load_nlu_morphemes()`` будет использовать переданный
    (или свежесозданный) registry. По выходу из блока активный registry
    восстанавливается, а scoped-кэш отбрасывается — без глобального
    ``reset_cache``.
    """

    global _active_registry
    new_registry = registry if registry is not None else NLUMorphemesRegistry()
    previous = _active_registry
    _active_registry = new_registry
    try:
        yield new_registry
    finally:
        _active_registry = previous


def reset_cache() -> None:
    """Backward-compatible: сброс активного scoped-кэша.

    Предпочтительный способ изоляции — ``nlu_morphemes_scope`` или
    собственный ``NLUMorphemesRegistry``. Эта функция оставлена для
    совместимости со старыми тестами.
    """

    _active_registry.clear()


def match_table_base(
    fk_base: str,
    table_base: str,
    pluralizers: List[Tuple[str, str]],
) -> bool:
    """Совпадает ли имя FK-базы (``user`` из ``user_id``) с базой таблицы.

    ``pluralizers`` — список пар ``[suffix, plural_suffix]`` из yaml.
    Помимо exact-match, перебираем все правила:
      * подставляем plural_suffix вместо suffix у ``fk_base``;
      * подставляем suffix вместо plural_suffix у ``table_base``.

    Если ``pluralizers`` пуст — работает только exact match.

    Пример (yaml: [["", "s"], ["y", "ies"]]):
      * user vs users → s-плюрализация совпадает;
      * category vs categories → y→ies совпадает;
      * order vs orders → s-плюрализация совпадает.
    """
    if fk_base == table_base:
        return True
    for suffix, plural in pluralizers:
        # FK-сторона → plural
        if not suffix:
            if fk_base + plural == table_base:
                return True
        elif fk_base.endswith(suffix):
            if fk_base[: -len(suffix)] + plural == table_base:
                return True
        # Таблица-сторона → singular
        if not suffix:
            if table_base.endswith(plural) and table_base[: -len(plural)] == fk_base:
                return True
        else:
            if table_base.endswith(plural):
                singularized = table_base[: -len(plural)] + suffix
                if singularized == fk_base:
                    return True
    return False


def canonicalize_token_via_morphemes(
    token: str,
    cfg: Optional[NLUMorphemes] = None,
) -> str:
    """Лемматизация query-токена через nlu_morphemes.yaml.

    Используется schema_linking-скорингом (см. 4.14) для того, чтобы
    «выручка» и «доход» (или «region» и «регион») попадали в один
    канонический ключ и матчили одни и те же колонки.

    Поведение:
      * Если ``cfg.enabled`` is ``False`` или ``cfg`` is ``None`` —
        возвращаем ``token.lower()`` без изменений (identity).
      * Иначе: ищем токен среди ``intents`` и ``dimensions``. Если хотя
        бы одна морфема входит в lower-форму токена (или токен входит
        в морфему — поддержка точных значений вроде ``revenue``),
        возвращаем ``canonical``.
      * Если совпадений нет — возвращаем ``token.lower()`` (identity).

    Никаких хардкодов морфем здесь нет: всё берётся из yaml. Если
    yaml ``enabled: false`` — функция стабильно ведёт себя как identity.
    """
    if not token:
        return ""
    lowered = token.lower()
    if cfg is None or not getattr(cfg, "enabled", False):
        return lowered

    # Для коротких морфем («id», «am») двунаправленный substring даёт
    # ложные канонизации (внутри «valid», «name» и т.п.). Минимальная
    # длина для substring-режима — 3 символа; для более коротких морфем
    # требуем точного совпадения.
    min_substring_len = 3
    for group in list(cfg.intents) + list(cfg.dimensions):
        morphemes = group.get("morphemes") or []
        canonical = group.get("canonical")
        if not canonical:
            continue
        for morpheme in morphemes:
            morpheme_lower = str(morpheme).lower()
            if not morpheme_lower:
                continue
            if len(morpheme_lower) < min_substring_len:
                if morpheme_lower == lowered:
                    return canonical.lower()
                continue
            if morpheme_lower in lowered or lowered in morpheme_lower:
                return canonical.lower()
    return lowered
