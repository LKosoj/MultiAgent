"""PII masking подмодуль core (Phase 7 декомпозиция).

EPIC 7.15-7.17:
- DI ``call_openai_api`` через keyword-only kwarg (фасад прокидывает singleton).
- Расширенный return-контракт: ``pii_detected``, ``masked_columns``, ``reason``.
- Корректная обработка duplicate column names (все индексы маскируются).
- Соль ``PII_MASK_SALT`` (fail-fast при unset для AUTO-detected masking),
  SHA-256 вместо MD5 для устойчивости к rainbow-table атакам.

W1-T4 (audit/RAG sanitization):
- ``pii_mask_sync(text)`` — LLM-free regex-маскировка строки. Назначение:
  audit-pipeline и sqlrag-артефакты, где LLM-вызов недопустим (deadlock
  audit-path + утечка в обучающий датасет).
- ``mask_pii_in_obj(value)`` — рекурсивный walker по dict/list/tuple/str
  для применения ``pii_mask_sync`` к строкам внутри произвольных структур.
  Cycle-guard через id->redacted-object memo.
"""
import hashlib
import json
import logging
import os
import re
from typing import Any, Callable, List, Optional

from ..prompts import build_pii_detection_prompt

logger = logging.getLogger(__name__)


# === W1-T4: LLM-free regex sanitization для audit/RAG путей ===============
#
# ВАЖНО: эти helper'ы НЕ заменяют ``pii_masking`` (table-mode + LLM AUTO).
# Они используются в audit/RAG-путях, где LLM-вызов запрещён:
#   * audit_logger пишется синхронно в каждом запросе — LLM-roundtrip
#     создаёт deadlock-источник (audit вызывает LLM, LLM может писать audit);
#   * sqlrag/*.md впоследствии включается в LLM-промпты, поэтому утечка
#     PII удваивается (диск + контекст модели).
#
# Regex-политика, порядок применения, replacement labels и opt-in для
# рискованных категорий берутся из ``config/pii/categories.yaml``:
# ``jurisdictions.<name>.sync_masking.rules``. Python-код ниже — только
# generic compile/apply engine.

_SYNC_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_SYNC_FALSE_TOKENS = frozenset({"", "0", "false", "no", "off"})

# Кэш скомпилированных regex по (jurisdiction_name, tuple(pattern, ...)).
# Инвалидируется автоматически при смене юрисдикции: ключ включает имя.
# Риск переполнения минимален: на практике 2-3 юрисдикции в процессе.
_compiled_rules_cache: dict = {}


def _load_active_pii_jurisdiction(jur_name: Optional[str] = None):
    from ..pii_categories_config import (
        load_pii_categories_config,
        resolve_active_jurisdiction_name,
    )

    config = load_pii_categories_config()
    # Если jur_name передан (уже резолвлен caller'ом) — используем его напрямую,
    # чтобы избежать повторного вызова resolve_active_jurisdiction_name().
    resolved = jur_name if jur_name is not None else resolve_active_jurisdiction_name()
    return config.get_jurisdiction(resolved)


def _env_flag_enabled(env_name: str) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in _SYNC_TRUE_TOKENS:
        return True
    if normalized in _SYNC_FALSE_TOKENS:
        return False
    raise ValueError(f"{env_name} must be boolean")


def _sync_rule_enabled(rule: Any) -> bool:
    if getattr(rule, "enabled", False):
        return True
    enable_env = getattr(rule, "enable_env", None)
    return bool(enable_env and _env_flag_enabled(enable_env))

def _is_likely_fullname(match: str) -> bool:
    """Проверяет, что matched-фрагмент похож на ФИО, а не на топоним.

    Исключения берутся из ``config/pii/categories.yaml`` активной юрисдикции,
    чтобы QA/runtime политика не жила hardcoded-списком в Python.
    """
    if not match:
        return False
    exclusions = _ru_fullname_exclusions()
    tokens = match.split()
    for token in tokens:
        if token.casefold() in exclusions:
            return False
    return True


def _ru_fullname_exclusions() -> frozenset[str]:
    jur = _load_active_pii_jurisdiction()
    return frozenset(item.casefold() for item in jur.fullname_exclusions)


def _ru_fullname_enabled(jur: Any = None) -> bool:
    """Compatibility helper: FULLNAME enablement is defined by yaml rule policy.

    ``jur`` — уже загруженная юрисдикция (опционально): передаётся из
    pii_mask_sync, чтобы не загружать конфиг повторно. При None — загружаем.
    """
    if jur is None:
        jur = _load_active_pii_jurisdiction()
    for rule in jur.sync_masking_rules:
        if rule.id == "full_name":
            return _sync_rule_enabled(rule)
    return False


def _apply_sync_mask_rule(masked: str, rule: Any, compiled: Optional[re.Pattern] = None) -> str:
    pattern = compiled if compiled is not None else re.compile(rule.pattern)
    replacement = rule.replacement
    preserve_group = getattr(rule, "preserve_before_group", None)
    use_fullname_exclusions = getattr(rule, "use_fullname_exclusions", False)

    if use_fullname_exclusions:
        return pattern.sub(
            lambda match: (
                replacement
                if _is_likely_fullname(match.group(0))
                else match.group(0)
            ),
            masked,
        )
    if preserve_group is not None:
        return pattern.sub(
            lambda match: (
                match.group(0)[: match.start(preserve_group) - match.start()]
                + replacement
            ),
            masked,
        )
    return pattern.sub(replacement, masked)


def _get_compiled_rules(jur: Any, jur_name: str) -> list:
    """Возвращает список (rule, compiled_pattern) с кэшированием по (jur_name, patterns).

    Ключ включает имя юрисдикции — при смене env PII_JURISDICTION кэш
    инвалидируется автоматически. Рост кэша ограничен числом юрисдикций (2-3).
    """
    cache_key = (jur_name, tuple(r.pattern for r in jur.sync_masking_rules))
    if cache_key not in _compiled_rules_cache:
        _compiled_rules_cache[cache_key] = [
            (rule, re.compile(rule.pattern)) for rule in jur.sync_masking_rules
        ]
    return _compiled_rules_cache[cache_key]


def pii_mask_sync(text: str) -> str:
    """Маскирует строку regex-only правилами активной PII-юрисдикции.

    Контракт:
        * Вход — ``str``. На не-строку возвращает as is (контракт walker'а).
        * Используется в audit-pipeline и при сохранении sqlrag-артефактов;
          LLM-вызов в этих местах НЕДОПУСТИМ (deadlock + утечка в RAG-датасет).
        * Regex, порядок шагов, replacement labels, prefix-preservation и
          opt-in правила берутся из ``config/pii/categories.yaml``.
        * Если yaml недоступен или невалиден, ошибка пробрасывается: audit/RAG
          sanitization не должен молча возвращать сырые PII.
    """
    if not isinstance(text, str) or not text:
        return text
    from ..pii_categories_config import resolve_active_jurisdiction_name
    jur_name = resolve_active_jurisdiction_name()
    # Передаём уже резолвленный jur_name, чтобы _load_active_pii_jurisdiction
    # не вызывал resolve_active_jurisdiction_name() повторно.
    jur = _load_active_pii_jurisdiction(jur_name)
    compiled_rules = _get_compiled_rules(jur, jur_name)
    masked = text
    for rule, compiled in compiled_rules:
        if rule.id == "full_name":
            if not _ru_fullname_enabled(jur):
                continue
        elif not _sync_rule_enabled(rule):
            continue
        masked = _apply_sync_mask_rule(masked, rule, compiled)
    return masked


def mask_pii_in_obj(value: Any, _memo: Optional[dict[int, Any]] = None) -> Any:
    """Рекурсивно применяет ``pii_mask_sync`` ко всем строкам внутри value.

    Поддержка: ``dict``, ``list``, ``tuple``, ``str``. Остальные типы
    (int, bool, float, Decimal, datetime, Path, None, ...) возвращаются
    БЕЗ изменений — это критично для дальнейшей сериализации через
    ``json.dumps(..., default=str)``.

    Cycle-guard: id->redacted-object memo отслеживает уже посещённые
    контейнеры. На рецидиве возвращается уже очищенная копия, а не исходный
    контейнер с сырыми PII.
    """
    if isinstance(value, str):
        return pii_mask_sync(value)
    # Не-контейнерные значения — as is. bool наследует int, но мы их и не трогаем.
    if not isinstance(value, (dict, list, tuple)):
        return value

    if _memo is None:
        _memo = {}
    obj_id = id(value)
    if obj_id in _memo:
        return _memo[obj_id]

    if isinstance(value, dict):
        # Ключи оставляем как есть (это metadata-field-names, не PII).
        # Маскируем только значения.
        redacted: dict[Any, Any] = {}
        _memo[obj_id] = redacted
        for key, item in value.items():
            redacted[key] = mask_pii_in_obj(item, _memo)
        return redacted
    if isinstance(value, list):
        redacted_list: list[Any] = []
        _memo[obj_id] = redacted_list
        redacted_list.extend(mask_pii_in_obj(item, _memo) for item in value)
        return redacted_list
    # tuple
    _memo[obj_id] = ()
    redacted_tuple = tuple(mask_pii_in_obj(item, _memo) for item in value)
    _memo[obj_id] = redacted_tuple
    return redacted_tuple


_CACHED_CALL_OPENAI_API: Optional[Callable] = None


def _reset_call_openai_api_cache() -> None:
    """Сбрасывает кэш фасадного call_openai_api (для тестов monkeypatch'ящих facade)."""
    global _CACHED_CALL_OPENAI_API
    _CACHED_CALL_OPENAI_API = None


def _resolve_facade_call_openai_api() -> Optional[Callable]:
    """Берёт ``call_openai_api`` через фасад для совместимости с monkeypatch.

    Возвращает None, если атрибут отсутствует — это позволяет вызывающей
    стороне явно решать (fail-fast vs. inject), а не подменять silent'но.

    Опциональный кэш активируется env ``TEXT_TO_SQL_CACHE_PII_LLM_CALLER=1``.
    По умолчанию выключен: ~45 тестов делают ``monkeypatch.setattr(core,
    "call_openai_api", ...)`` между вызовами — кэш ломает их без conftest-reset.
    """
    if os.getenv("TEXT_TO_SQL_CACHE_PII_LLM_CALLER", "0") == "1":
        global _CACHED_CALL_OPENAI_API
        if _CACHED_CALL_OPENAI_API is not None:
            return _CACHED_CALL_OPENAI_API
        from custom_tools.text_to_sql import core as _facade
        resolved = getattr(_facade, "call_openai_api", None)
        if resolved is not None:
            _CACHED_CALL_OPENAI_API = resolved
        return resolved

    from custom_tools.text_to_sql import core as _facade
    return getattr(_facade, "call_openai_api", None)


def _mask_value(value: object, salt: str) -> str:
    """Возвращает маскированную форму ``***<hash8>`` для значения.

    Использует SHA-256 + соль ``PII_MASK_SALT``. Truncation до 8 hex chars
    оставлен прежним: цель не криптографическая стойкость хэша целиком,
    а уникальность маркера; защита от rainbow-table обеспечивается солью.
    """
    payload = f"{salt}:{value}".encode("utf-8")
    return f"***{hashlib.sha256(payload).hexdigest()[:8]}"


def pii_masking(
    data: List[List[object]],
    columns_to_mask: List[str],
    column_names: Optional[List[str]] = None,
    *,
    call_openai_api: Optional[Callable] = None,
) -> dict:
    """Маскирование PII с опциональным определением колонок через LLM.

    Args:
        data: Двумерный список с данными для маскирования.
        columns_to_mask: Список имен колонок для маскирования
            или ``["AUTO"]`` для LLM-детекции.
        column_names: Список имен всех колонок (требуется, если
            ``columns_to_mask`` непуст).
        call_openai_api: Инъектируемая LLM-функция (DI). Если не передана,
            берётся через фасад ``custom_tools.text_to_sql.core``. Прямой
            вызов ``_pii.pii_masking`` без LLM в AUTO-режиме → fail-fast.

    Returns:
        Словарь с ключами:
            * ``masked_data`` — данные (возможно, замаскированные).
            * ``pii_detected`` — bool, было ли реально что-то замаскировано.
            * ``masked_columns`` — список имён колонок, которые маскировались.
            * ``reason`` — причина "no-op" режима, если ``pii_detected=False``;
              иначе ``None``. Значения:
                - ``"masking_disabled"`` — env kill-switch ``PII_MASKING_ENABLED=0``;
                - ``"empty_data"`` — пустой ``data``;
                - ``"no_columns_to_mask"`` — пустой ``columns_to_mask``;
                - ``"auto_detected_none"`` — AUTO вернул пустой список.

    Raises:
        RuntimeError: при AUTO-сбое LLM или unset ``PII_MASK_SALT``
            (когда маскирование реально выполняется).
            Также при попытке маскировать с непустым ``columns_to_mask``,
            но ``column_names is None`` — индексы нельзя разрешить.

    Environment Variables:
        PII_MASKING_ENABLED: "0" для полного отключения PII маскирования.
        PII_DETECTION_SENSITIVITY: "low"/"medium"/"high" для AUTO режима.
        PII_MASK_SALT: обязательная соль для хэширования значений
            (fail-fast если unset на момент реального маскирования).
    """
    # Проверяем отключение PII маскирования
    if os.getenv("PII_MASKING_ENABLED", "1") == "0":
        logger.info("PII masking disabled by environment variable")
        return {
            "masked_data": data,
            "pii_detected": False,
            "masked_columns": [],
            "reason": "masking_disabled",
        }

    logger.info("Performing PII masking")

    if not data:
        return {
            "masked_data": data,
            "pii_detected": False,
            "masked_columns": [],
            "reason": "empty_data",
        }

    cols_to_use = list(columns_to_mask or [])

    # Автоматическое определение PII колонок через LLM
    if cols_to_use == ["AUTO"] and column_names:
        # DI: keyword-only `call_openai_api`. Если не передан — резолвим
        # через фасад (для обратной совместимости с тестами, которые
        # делают monkeypatch.setattr core.call_openai_api).
        llm = call_openai_api if call_openai_api is not None else _resolve_facade_call_openai_api()
        if llm is None:
            raise RuntimeError(
                "PII AUTO-detection requested but call_openai_api is unavailable "
                "(no DI kwarg passed and facade core.call_openai_api is None). "
                "Inject `call_openai_api=` or restore module-level binding."
            )
        try:
            # Получаем уровень чувствительности из переменной окружения
            sensitivity = os.getenv("PII_DETECTION_SENSITIVITY", "medium")  # low, medium, high

            prompt = build_pii_detection_prompt(column_names, sensitivity)
            resp = llm(
                prompt=prompt,
                system_prompt="Ты эксперт по безопасности данных. Верни только JSON.",
                max_tokens=2000,
                response_format={"type": "json_object"}
            )
            from ..utils import parse_llm_json_response
            obj = parse_llm_json_response(resp)
            if not (isinstance(obj, dict) and isinstance(obj.get("columns"), list)):
                # Невалидный ответ LLM при AUTO-режиме маскирования — fail-safe:
                # отказ в выдаче немаскированных данных, чтобы не допустить утечки PII.
                raise RuntimeError(
                    "PII auto-detection returned malformed LLM response; "
                    "refusing to return unmasked data"
                )
            cols_to_use = [c for c in obj["columns"] if c in column_names]
        except RuntimeError:
            # Внутренний raise RuntimeError(...) для malformed LLM response —
            # пробрасываем без переоборачивания (сохраняет исходный message/cause).
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # Сбой AUTO-определения PII => строгий fail (AGENTS.md: молчаливый
            # возврат немаскированных данных запрещён). Узкий список покрывает
            # JSON-парсинг и доступ к obj["columns"].
            logger.error("Auto PII detection failed; refusing to return unmasked data: %s", e)
            raise RuntimeError(f"PII auto-detection failed: {e}") from e

        if not cols_to_use:
            # LLM явно сказал «PII не обнаружено». Это валидный результат,
            # но он отличается от "masking_disabled": помечаем явно reason.
            return {
                "masked_data": data,
                "pii_detected": False,
                "masked_columns": [],
                "reason": "auto_detected_none",
            }

    if not cols_to_use:
        # Пустой columns_to_mask (и не AUTO) — нечего маскировать.
        return {
            "masked_data": data,
            "pii_detected": False,
            "masked_columns": [],
            "reason": "no_columns_to_mask",
        }

    # На этом этапе cols_to_use непуст. Нужны column_names для индексации.
    # Раньше silent-возврат немаскированных данных — нарушение AGENTS.md.
    if not column_names:
        raise RuntimeError(
            "pii_masking received non-empty columns_to_mask but column_names is "
            "missing — cannot resolve indices. Pass column_names explicitly."
        )

    # Определяем ВСЕ индексы колонок для маскирования (duplicate-safe).
    # Раньше column_names.index(col) возвращал только первое вхождение,
    # дубли (например, два столбца "email" в результате JOIN) ускользали.
    cols_to_use_set = set(cols_to_use)
    idxs: List[int] = sorted({
        i for i, name in enumerate(column_names) if name in cols_to_use_set
    })
    masked_columns: List[str] = sorted({
        name for name in column_names if name in cols_to_use_set
    })

    # Соль обязательна на момент реального маскирования.
    # Если env не выставлен или пустой — fail-fast по AGENTS.md, без silent default.
    salt_raw = os.getenv("PII_MASK_SALT")
    if salt_raw is None or not salt_raw.strip():
        raise RuntimeError(
            "PII_MASK_SALT environment variable is not set (or empty). "
            "Set it to a non-empty secret to enable deterministic but "
            "non-rainbow-tableable PII masking, or disable masking via "
            "PII_MASKING_ENABLED=0."
        )
    salt = salt_raw

    # Маскируем данные
    masked: List[List[object]] = []
    for row in data:
        new_row = list(row)
        for i in idxs:
            if i < len(new_row) and new_row[i] is not None:
                new_row[i] = _mask_value(new_row[i], salt)
        masked.append(new_row)

    return {
        "masked_data": masked,
        "pii_detected": bool(masked_columns),
        "masked_columns": masked_columns,
        "reason": None,
    }
