"""
Natural Language Understanding для Text-to-SQL.

Fallback-эвристика (``_fallback_extract_intent``/``_fallback_tokenize``)
включается только при ``TEXT_TO_SQL_NLU_ALLOW_FALLBACKS=1`` и читает все
морфемы/regex из ``config/text_to_sql/nlu_morphemes.yaml``. В этом модуле
не должно появляться закрытых эвристик, морфем или regex'ов в коде —
смотри AGENTS.md (T4.1) и ``nlu_config.py``.
"""
from __future__ import annotations

import re
import logging
import os
import yaml
from typing import TYPE_CHECKING, Any, Dict, List

from .nlu_config import NLUMorphemesRegistry

logger = logging.getLogger(__name__)

try:
    from utils import call_openai_api  # type: ignore
except Exception:
    call_openai_api = None  # type: ignore

from .prompts import (  # noqa: E402
    build_adaptive_query_completeness_prompt,
    build_adaptive_query_understanding_prompt,
    build_nlu_prompt,
)

if TYPE_CHECKING:
    from .adaptive.models import QuerySpec


def _nlu_max_tokens(key: str) -> int:
    """``max_tokens`` для NLU LLM-вызовов из llm_models.yaml (W6-T3).

    ``key`` — один из ключей NLU-секции, включая отдельный лимит adaptive
    query understanding. Fail-fast при отсутствии секции/ключа — magic-числа
    в .py запрещены AGENTS.md.
    """
    from .llm_models_config import load_llm_models_config

    return int(load_llm_models_config().get("nlu", key))


def _nlu_model(step: str):
    """Модель для NLU LLM-вызова ``step`` из реестра ``llm_models.yaml::step_models`` (W1-1.1)."""
    from agent_command import model_mapping

    from .llm_models_config import step_model_name

    return model_mapping[step_model_name(step)]


_TOKEN_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}|\d+[.,]?\d*|[\w\-]+", re.IGNORECASE | re.UNICODE)
_DATE_TOKEN_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUM_TOKEN_PATTERN = re.compile(r"\d+[.,]?\d*")
class NLUProcessor:
    """Обработчик естественного языка для извлечения намерений и сущностей."""

    def __init__(self, *, morphemes_registry: NLUMorphemesRegistry | None = None) -> None:
        # DI: явный scoped registry для morphemes-кэша (см. 3.23). Если не
        # передан — используется активный (глобальный или scoped) registry.
        self._morphemes_registry = morphemes_registry

    def _allow_fallbacks(self) -> bool:
        return os.getenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "0").strip().lower() in {"1", "true", "yes", "on"}

    def _nlu_unavailable_error(self, operation: str) -> RuntimeError:
        return RuntimeError(
            f"LLM {operation} unavailable or returned invalid data. "
            "Set TEXT_TO_SQL_NLU_ALLOW_FALLBACKS=1 to use heuristic NLU fallback."
        )

    def _require_fallback_cfg(self, operation: str, *, dsn: str | None = None):
        """Загружает yaml и явно валидирует feature-flag `enabled`.

        Если конфиг помечен ``enabled: false`` — поднимаем RuntimeError,
        не запуская closed-world эвристику.

        ``dsn`` (W1-1.2b): если передан и для него есть непустой
        DSN-профиль в части ``nlu_hints``, он приоритетнее named
        nlu_morphemes-профиля — см.
        ``dsn_profile_overrides.resolve_nlu_morphemes``.
        """
        from .dsn_profile_overrides import resolve_nlu_morphemes

        cfg = resolve_nlu_morphemes(dsn=dsn, registry=self._morphemes_registry)
        if not cfg.enabled:
            raise RuntimeError(
                f"Heuristic NLU fallback for {operation} is disabled by "
                f"config ({cfg.source_path}: enabled=false). "
                "Set 'enabled: true' explicitly to opt in."
            )
        return cfg

    # NOTE (T10-nlu): tokens/pos_tags используются downstream декоративно.
    # Поэтому scored-путь не должен зависеть от отдельного LLM-вызова:
    # локальная токенизация сохраняет внешний контракт детерминированно.
    def process_text(self, text: str, session_id: str | None = None) -> Dict[str, List[str]]:
        """Токенизация и POS-тегирование текста.

        ``session_id`` пробрасывается в logger.extra для трассировки;
        per AGENTS.md (no hardcode/no silent fallback) — если не передан,
        просто пишется None, без выдумывания идентификатора.
        """
        logger.info("Processing text with NLU", extra={"session_id": session_id})

        # Ранний возврат на пустой/пробельный ввод — LLM-вызов не нужен.
        if not text or not text.strip():
            logger.warning("process_text: пустой или пробельный ввод, пропускаем NLP")
            return {"tokens": [], "pos_tags": []}

        tokens = _TOKEN_PATTERN.findall(text.lower())
        pos_tags: List[str] = []
        for token in tokens:
            if _DATE_TOKEN_PATTERN.fullmatch(token):
                pos_tags.append("DATE")
            elif _NUM_TOKEN_PATTERN.fullmatch(token):
                pos_tags.append("NUM")
            else:
                pos_tags.append("OTHER")
        return {"tokens": tokens, "pos_tags": pos_tags}

    def _understand_query(
        self,
        text: str,
        *,
        run_id: str,
        run_incarnation: str,
        context_documents: tuple[str, ...] = (),
        schema_context: str = "",
    ) -> QuerySpec:
        """Build strict adaptive ``QuerySpec`` without schema inference."""
        if call_openai_api is None:
            raise RuntimeError("LLM adaptive query understanding is unavailable")

        from .utils import parse_llm_json_response
        from .adaptive.query_understanding import understand_query

        prompt = build_adaptive_query_understanding_prompt(
            text,
            context_documents=context_documents,
        )
        system_prompt = (
            "Ты выделяешь только смысловые элементы запроса Text-to-SQL "
            "без привязки к схеме. Верни только JSON."
        )
        max_tokens = _nlu_max_tokens("query_understanding_max_tokens")
        response = call_openai_api(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            model=_nlu_model("nlu_query_understanding"),
            response_format={"type": "json_object"},
        )
        decoded = parse_llm_json_response(response)
        understand_query(
            text,
            run_id=run_id,
            run_incarnation=run_incarnation,
            response=decoded,
        )
        completeness_response = call_openai_api(
            prompt=build_adaptive_query_completeness_prompt(
                text,
                decoded,
                context_documents=context_documents,
                schema_context=schema_context,
            ),
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            model=_nlu_model("nlu_completeness"),
            response_format={"type": "json_object"},
        )
        corrected = parse_llm_json_response(completeness_response)
        return understand_query(
            text,
            run_id=run_id,
            run_incarnation=run_incarnation,
            response=corrected,
        )

    def extract_intent(
        self, text: str, session_id: str | None = None, *, dsn: str | None = None
    ) -> Dict[str, Any]:
        """Извлечение намерения и сущностей из текста.

        ``session_id`` пробрасывается в logger.extra; не используется для
        фабрикации значений — per AGENTS.md.

        ``dsn`` (W1-1.2b): если передан и для него есть непустой
        DSN-профиль в части ``nlu_hints``, он приоритетнее named
        nlu_morphemes-профиля в fallback-эвристике (см.
        ``dsn_profile_overrides.resolve_nlu_morphemes``). Используется
        только heuristic fallback-путём; LLM-путь ``dsn`` не использует.
        """
        logger.info("Extracting intent and entities", extra={"session_id": session_id})

        # Ранний возврат на пустой/пробельный ввод — LLM-вызов не нужен.
        # T10: intent НЕ хардкодим — читаем default_intent из конфига
        # (source of truth — nlu_morphemes.yaml), иначе при смене дефолта в
        # yaml этот путь молча разойдётся с остальным NLU. Пустой ввод — это
        # не heuristic-fallback, поэтому НЕ гейтим по cfg.enabled
        # (_require_fallback_cfg), а грузим конфиг напрямую (load кэширован
        # через registry).
        if not text or not text.strip():
            logger.warning("extract_intent: пустой или пробельный ввод, пропускаем NLU")
            try:
                from .dsn_profile_overrides import resolve_nlu_morphemes

                cfg = resolve_nlu_morphemes(dsn=dsn, registry=self._morphemes_registry)
                default_intent = cfg.default_intent
            except (FileNotFoundError, ValueError, OSError, yaml.YAMLError):
                logger.warning("extract_intent: конфиг NLU недоступен, используем нейтральный intent")
                default_intent = "query"
            return {
                "intent": default_intent,
                "entities": {"metrics": [], "dimensions": [], "filters": {}},
            }

        if call_openai_api:
            try:
                prompt = build_nlu_prompt(text)
                resp = call_openai_api(
                    prompt=prompt,
                    system_prompt="Ты находишь intent и сущности для Text-to-SQL. Верни только JSON.",
                    max_tokens=_nlu_max_tokens("intent_max_tokens"),
                    model=_nlu_model("nlu_intent"),
                    response_format={"type": "json_object"}
                )
                from .utils import parse_llm_json_response
                obj = parse_llm_json_response(resp)
                if isinstance(obj, dict) and isinstance(obj.get("intent"), str) and isinstance(obj.get("entities"), dict):
                    return obj
                raise ValueError("LLM intent response must contain intent and entities; intent must be str, entities must be dict")
            except Exception as e:
                logger.warning(f"LLM intent extraction failed: {e}")
                if not self._allow_fallbacks():
                    raise self._nlu_unavailable_error("intent extraction") from e
        elif not self._allow_fallbacks():
            raise self._nlu_unavailable_error("intent extraction")

        # Opt-in fallback эвристика
        return self._fallback_extract_intent(text, dsn=dsn)

    def _fallback_tokenize(self, text: str) -> Dict[str, List[str]]:
        """Fallback токенизация без LLM. Список adpositions грузится из yaml."""
        cfg = self._require_fallback_cfg("NLP processing")
        tokens = _TOKEN_PATTERN.findall(text.lower())
        adpositions = set(cfg.tokenizer_adpositions)
        pos_tags: List[str] = []

        for token in tokens:
            if _DATE_TOKEN_PATTERN.fullmatch(token):
                pos_tags.append("DATE")
            elif _NUM_TOKEN_PATTERN.fullmatch(token):
                pos_tags.append("NUM")
            elif token in adpositions:
                pos_tags.append("ADP")
            else:
                pos_tags.append("OTHER")

        return {"tokens": tokens, "pos_tags": pos_tags}

    def _fallback_extract_intent(
        self, text: str, *, dsn: str | None = None
    ) -> Dict[str, Any]:
        """Fallback извлечение интента без LLM.

        Все морфемы, регэксп-паттерны и интент-правила берутся из
        ``config/text_to_sql/nlu_morphemes.yaml`` (см. ``nlu_config.py``),
        либо (W1-1.2b) из непустого DSN-профиля для ``dsn``, если он
        приоритетнее — см. ``dsn_profile_overrides.resolve_nlu_morphemes``.
        """
        cfg = self._require_fallback_cfg("intent extraction", dsn=dsn)
        lower = text.lower()

        metrics: List[str] = [
            group["canonical"]
            for group in cfg.intents
            if any(morpheme in lower for morpheme in group["morphemes"])
        ]

        dimensions: List[str] = [
            group["canonical"]
            for group in cfg.dimensions
            if any(morpheme in lower for morpheme in group["morphemes"])
        ]

        filters: Dict[str, Any] = {}

        # Диапазоны дат (ISO)
        date_matches: List[str] = []
        for pattern in cfg.patterns_date_iso:
            date_matches.extend(pattern.findall(lower))
        if len(date_matches) >= 2:
            filters["date_range"] = {"start": date_matches[0], "end": date_matches[1]}
        elif len(date_matches) == 1:
            # Открытый интервал: только start без явного end=None (3.14).
            # null в JSON может быть интерпретирован downstream как
            # ``IS NULL``; правильный контракт — отсутствие границы.
            filters["date_range"] = {"start": date_matches[0]}

        # Относительные даты
        if any(trigger in lower for trigger in cfg.relative_date_triggers):
            # Сначала извлекаем числовой модификатор из обобщённого паттерна.
            # Если нет числа — count=1 (по умолчанию: «за последний период»).
            days_match = cfg.relative_date_days_pattern.search(lower)
            if days_match and days_match.lastindex is not None and days_match.lastindex >= 1:
                try:
                    count = int(days_match.group(1))
                except (ValueError, IndexError):
                    count = 1
            else:
                count = 1
            # Один проход по periods: canonical и count уже известны.
            for period in cfg.relative_date_periods:
                if any(morpheme in lower for morpheme in period["morphemes"]):
                    filters["relative_date"] = {"period": period["canonical"], "count": count}
                    break

        # Извлечение значения dimension (см. patterns.region в yaml).
        # Канонизация — через cfg.regions_normalize (см. 3.13). title() не
        # используется: он хардкодит правила и портит русские суффиксы.
        for pattern in cfg.patterns_region:
            region_match = pattern.search(lower)
            if region_match and region_match.lastindex is not None and region_match.lastindex >= 1:
                raw_region = region_match.group(1).strip()
                key = raw_region.lower()
                canonical = cfg.regions_normalize.get(key)
                if canonical is None:
                    # Поиск по префиксу (для словоформ "московской" → "москв")
                    for prefix, value in cfg.regions_normalize.items():
                        if key.startswith(prefix):
                            canonical = value
                            break
                filters["region"] = canonical if canonical is not None else raw_region
                break

        # Числовые сравнения
        for pattern in cfg.patterns_amount_greater:
            greater_match = pattern.search(lower)
            if greater_match and greater_match.lastindex is not None and greater_match.lastindex >= 1:
                try:
                    filters["amount_greater"] = float(greater_match.group(1))
                    break
                except (ValueError, IndexError):
                    continue

        for pattern in cfg.patterns_amount_less:
            less_match = pattern.search(lower)
            if less_match and less_match.lastindex is not None and less_match.lastindex >= 1:
                try:
                    filters["amount_less"] = float(less_match.group(1))
                    break
                except (ValueError, IndexError):
                    continue

        for pattern in cfg.patterns_amount_between:
            between_match = pattern.search(lower)
            if between_match and between_match.lastindex is not None and between_match.lastindex >= 2:
                try:
                    filters["amount_range"] = {
                        "min": float(between_match.group(1)),
                        "max": float(between_match.group(2)),
                    }
                    break
                except (ValueError, IndexError):
                    continue

        # TOP N / лимиты
        for pattern in cfg.patterns_top_n:
            top_match = pattern.search(lower)
            if top_match and top_match.lastindex is not None and top_match.lastindex >= 1:
                try:
                    filters["limit"] = int(top_match.group(1))
                    break
                except (ValueError, IndexError):
                    continue

        # Сортировка
        if any(trigger in lower for trigger in cfg.order_triggers):
            if any(trigger in lower for trigger in cfg.order_desc_triggers):
                filters["order"] = "desc"
            else:
                filters["order"] = "asc"

        # Интент: первое совпавшее правило выигрывает, иначе top_n или default.
        intent = cfg.default_intent
        for rule in cfg.intent_rules:
            if any(morpheme in lower for morpheme in rule["morphemes"]):
                intent = rule["canonical"]
                break
        else:
            if filters.get("limit"):
                intent = cfg.top_n_intent

        return {
            "intent": intent,
            "entities": {
                "metrics": metrics,
                "dimensions": dimensions,
                "filters": filters,
            },
        }
