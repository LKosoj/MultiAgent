"""
Heuristic schema linking — name/description/type/FK-based scoring.

Расщеплено из ``strategies.py`` (EPIC 8.2). Этот модуль содержит весь
heuristic-based pipeline: ``heuristic_linking``, ``find_main_table``,
``best_column_for``, ``link_filters``, плюс приватные helpers для
type-hint bonus и canonicalisation через nlu_morphemes.yaml.

Доменных синонимов в коде нет — они живут в
``config/text_to_sql/column_aliases.yaml`` (см. column_aliases_config),
type-hint наборы — там же. Лемматизация — через nlu_morphemes.yaml.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

import yaml

from memory.manager import EmbeddingUnavailableError, EmbeddingFailedError

from ..schema_metadata import is_pk, is_fk, get_type
from ..schema_namespace import SchemaNamespace
from .join_validation import _parse_fk_reference_table

logger = logging.getLogger(__name__)


def _is_identifier_like_dimension_candidate(entity_name: Any, column_name: str) -> bool:
    entity = str(entity_name or "").strip().casefold()
    column = str(column_name or "").strip().casefold()
    if entity in {"id", "identifier", "key", "code"} or entity.endswith(("_id", "_code", "_key")):
        return False
    return column in {"id", "uuid", "guid", "code"} or column.endswith(("_id", "_code", "_key"))


class HeuristicLinker:
    """Эвристическое связывание сущностей со схемой.

    Не имеет внешних зависимостей кроме ``memory_manager`` (для
    ``find_semantic_relevant_tables``). LLM не используется.
    """

    def __init__(self, memory_manager):
        self.memory_manager = memory_manager
        # Кэш для лемматизации query-токенов (4.14). None == ещё не
        # резолвили yaml; объект ``NLUMorphemes`` или False == cfg недоступен.
        self._morphemes_cfg: Any = None

    # ------------------------------------------------------------------
    # Morphemes (NLU) helpers — 4.14
    # ------------------------------------------------------------------
    def _get_morphemes_cfg(self) -> Any:
        """Ленивая загрузка nlu_morphemes.yaml с кэшированием на инстансе.

        Возвращает ``NLUMorphemes`` или ``None`` — если yaml-файл
        недоступен (тогда лемматизация работает как identity).
        """
        if self._morphemes_cfg is False:
            # ``False`` означает «уже пытались, не получилось» — больше
            # не пытаемся, чтобы не платить за повторные FileNotFoundError.
            return None
        if self._morphemes_cfg is not None:
            return self._morphemes_cfg
        from ..nlu_config import load_nlu_morphemes

        try:
            self._morphemes_cfg = load_nlu_morphemes()
        except FileNotFoundError as exc:
            # Явный «cfg не настроен» — это легальный режим (identity),
            # а не silent fallback другой бизнес-логики (см. AGENTS.md).
            logger.debug(
                "schema_linking: nlu_morphemes.yaml отсутствует (%s); "
                "лемматизация работает как identity",
                exc,
            )
            self._morphemes_cfg = False
            return None
        except (yaml.YAMLError, ValueError):
            # Битый yaml / нарушение схемы — это ошибка конфигурации,
            # не молчим, пробрасываем наверх (fail-fast).
            raise
        return self._morphemes_cfg

    def _compute_type_hint_bonus(
        self,
        query_token: str,
        col_type: str,
        numeric_hints: Any,
        temporal_hints: Any,
        identifier_hints: Any,
        primary_signal_present: bool,
        _type_cfg: Any = None,
    ) -> int:
        """Считает бонус за type-hint совпадение (4.26).

        Поддерживает оба формата (см. column_aliases.yaml):

          * legacy: ``type_hints: { numeric: [amount, ...] }``  → плоский
            list, бонус +3 ТОЛЬКО при ``primary_signal_present`` (старое
            поведение).
          * новый: ``type_hints: { numeric: { tokens: [...],
            weight_solo: 1, weight_with_signal: 3 } }`` — бонус работает
            всегда, но с разным весом.

        ``_type_cfg`` передаётся caller'ом (``_score_columns_for_name``)
        — один вызов ``load_type_categories_config()`` на таблицу, а не
        на каждую колонку.
        """

        # T5-linking / #14 LOW: категории SQL-типов берём из yaml.
        # Если _type_cfg не передан (backward-compat) — загружаем здесь.
        if _type_cfg is None:
            from ..type_categories_config import load_type_categories_config
            _type_cfg = load_type_categories_config()
        col_category = _type_cfg.get_category(col_type)

        def _matches_category(yaml_categories: Tuple[str, ...]) -> bool:
            return col_category in yaml_categories

        def _category(hints: Any, yaml_categories: Tuple[str, ...]) -> Tuple[bool, int]:
            if not hints:
                return False, 0
            if isinstance(hints, dict):
                tokens = hints.get("tokens") or []
                if query_token not in tokens:
                    return False, 0
                if not _matches_category(yaml_categories):
                    return True, 0
                if primary_signal_present:
                    return True, int(hints.get("weight_with_signal", 3))
                return True, int(hints.get("weight_solo", 1))
            if isinstance(hints, list):
                if query_token not in hints:
                    return False, 0
                if not _matches_category(yaml_categories):
                    return True, 0
                if primary_signal_present:
                    return True, 3
                # Симметрия с dict-форматом (weight_solo по умолчанию 1):
                # если есть name_hit и совпадение типа — даём минимальный
                # bonus, иначе пользователи legacy-list не получают
                # type-сигнала вовсе, а dict-формат — получают.
                return True, 1
            return False, 0

        # W1-T5 (баг 2): раньше цикл выходил по первому ``name_hit``, даже
        # если у этой категории ``bonus == 0`` (например, token есть в
        # numeric_hints, но col_type — TEXT). В результате валидная
        # категория с реальным bonus’ом ниже по списку игнорировалась.
        # Теперь собираем все категории, где token нашёлся, и выбираем
        # максимальный bonus.
        # T5-linking / #14 LOW: yaml-категории вместо хардкода.
        # identifier: покрываем char/text/varchar/int плюс ‘uuid’
        # (uuid/uniqueidentifier) — восстанавливает прежнюю поддержку
        # UUID-ключей (PG/MSSQL), которая была в хардкоде до миграции.
        any_name_hit = False
        best_bonus = 0
        for hints, yaml_categories in (
            (numeric_hints, ("integer", "numeric")),
            (temporal_hints, ("temporal",)),
            (identifier_hints, ("string", "integer", "uuid")),
        ):
            name_hit, bonus = _category(hints, yaml_categories)
            if name_hit:
                any_name_hit = True
                if bonus > best_bonus:
                    best_bonus = bonus
        if not any_name_hit:
            return 0
        return best_bonus

    # ------------------------------------------------------------------
    # Entity term collection (shared with LLM linker)
    # ------------------------------------------------------------------
    def collect_entity_terms(self, entities: Dict[str, Any]) -> List[str]:
        """Извлекает имена metrics/dimensions/filters для поиска схемы."""
        if not isinstance(entities, dict):
            return []

        terms: List[str] = []

        def add_term(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                value = value.strip()
                if value:
                    terms.append(value)
            elif isinstance(value, dict):
                add_term(value.get("name") or value.get("column"))
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    add_term(item)
            else:
                terms.append(str(value))

        for key in ("metrics", "dimensions"):
            add_term(entities.get(key, []))

        filters = entities.get("filters", {})
        if isinstance(filters, dict):
            for key in filters:
                add_term(key)
        else:
            add_term(filters)

        seen = set()
        unique_terms = []
        for term in terms:
            lowered = term.lower()
            if lowered not in seen:
                seen.add(lowered)
                unique_terms.append(term)
        return unique_terms

    # ------------------------------------------------------------------
    # Heuristic linking entry-points
    # ------------------------------------------------------------------
    def heuristic_linking(
        self,
        entities: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
        namespace: Optional[SchemaNamespace] = None,
    ) -> Tuple[List, List, Dict, List]:
        """Эвристическое связывание сущностей со схемой."""
        return self._heuristic_linking_with_diagnostics(
            entities,
            db_schema,
            dsn=dsn,
            diagnostics=None,
            namespace=namespace,
        )

    def _heuristic_linking_with_diagnostics(
        self,
        entities: Dict[str, Any],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str],
        diagnostics: Optional[Dict[str, Any]],
        namespace: Optional[SchemaNamespace] = None,
    ) -> Tuple[List, List, Dict, List]:
        linked_metrics: List[Dict[str, Any]] = []
        linked_dimensions: List[Dict[str, Any]] = []
        linked_filters: Dict[str, Any] = {}
        unlinked: List[str] = []

        metrics_in = entities.get("metrics", []) if isinstance(entities, dict) else []
        dims_in = entities.get("dimensions", []) if isinstance(entities, dict) else []
        filters_in = entities.get("filters", {}) if isinstance(entities, dict) else {}

        # T3: find_semantic_relevant_tables теперь ПРОБРАСЫВАЕТ
        # EmbeddingUnavailableError/EmbeddingFailedError (вместо молчаливого []),
        # чтобы caller видел реальную причину. Но heuristic-связывание —
        # это и есть путь деградации: его ядро работает на строковом/
        # морфемном матчинге, а semantic_tables — лишь ОПЦИОНАЛЬНАЯ подсказка
        # для выбора main_table. Поэтому здесь недоступность эмбеддингов не
        # должна рушить heuristic-fallback: логируем (НЕ молча) и продолжаем
        # без семантической подсказки.
        try:
            if namespace is None:
                semantic_tables = self.memory_manager.find_semantic_relevant_tables(
                    self.collect_entity_terms(entities),
                    dsn=dsn,
                )
            else:
                semantic_tables = self.memory_manager.find_semantic_relevant_tables(
                    self.collect_entity_terms(entities),
                    namespace=namespace,
                )
        except (EmbeddingUnavailableError, EmbeddingFailedError) as e:
            logger.warning(
                "Семантическая подсказка main_table недоступна (эмбеддинги "
                "недоступны/не настроены — проверьте OPENAI_API_KEY_DB / "
                "embedding config): %s. Продолжаю heuristic-связывание по "
                "строковому матчингу без семантической подсказки.",
                e,
            )
            semantic_tables = []

        main_table = self.find_main_table(db_schema, semantic_tables)

        if main_table:
            for m in metrics_in:
                col = self._best_column_for_with_diagnostics(
                    m,
                    main_table,
                    db_schema.get(main_table, {}),
                    entity_type="metric",
                    diagnostics=diagnostics,
                )
                if col:
                    linked_metrics.append({"name": m, "table": main_table, "column": col})
                else:
                    unlinked.append(m)

            from ..utils import get_table_columns

            for d in dims_in:
                # T5-linking / #11 MEDIUM: собираем всех кандидатов из всех
                # не-main таблиц со score, фильтруем FK-колонки (они не могут
                # быть dimension — это ссылки, а не данные). Затем проверяем
                # main_table отдельно. Выбираем победителя по score DESC;
                # tie-break: main_table предпочтительнее, иначе алфавитно.
                # Это устраняет order-dependent поведение (баг #11).
                candidates: List[Tuple[str, str, int]] = []
                for t, table_schema in db_schema.items():
                    ranked = self._score_columns_for_name(d, table_schema)
                    if not ranked:
                        continue
                    table_cols = get_table_columns(table_schema)
                    for candidate, cand_score in ranked:
                        col_meta = table_cols.get(candidate)
                        if isinstance(col_meta, dict) and is_fk(col_meta):
                            continue
                        if _is_identifier_like_dimension_candidate(d, candidate):
                            continue
                        candidates.append((t, candidate, cand_score))

                top_candidates = self._top_candidates(candidates)
                if len(top_candidates) > 1:
                    self._record_ambiguity(
                        diagnostics,
                        entity_type="dimension",
                        name=d,
                        candidates=top_candidates,
                    )
                    unlinked.append(d)
                elif top_candidates:
                    best_table, best_col, _ = top_candidates[0]
                    if best_table and best_col:
                        linked_dimensions.append(
                            {"name": d, "table": best_table, "column": best_col}
                        )
                else:
                    unlinked.append(d)
        else:
            # W1-T5 (баг 1): раньше при main_table=None весь блок
            # metrics/dimensions молча пропускался — сущности «терялись»,
            # не попадая даже в ``unlinked``. Это маскировало fail-fast
            # find_main_table и затрудняло аудит. Теперь каждая
            # ненайденная сущность явно фиксируется в ``unlinked`` с
            # diagnostic reason; формат — opaque строка (см.
            # linking_orchestrator HIGH #6).
            reason = "no_main_table_resolved"
            logger.warning(
                "heuristic_linking: main_table=None — %d metric(s) и %d dimension(s) "
                "помечены unlinked (reason=%s).",
                len(metrics_in),
                len(dims_in),
                reason,
            )
            for m in metrics_in:
                unlinked.append(f"{m} (reason={reason})")
            for d in dims_in:
                unlinked.append(f"{d} (reason={reason})")

        linked_filters = self._link_filters_with_diagnostics(
            filters_in,
            linked_dimensions,
            main_table,
            db_schema,
            diagnostics,
        )

        return linked_metrics, linked_dimensions, linked_filters, unlinked

    def find_main_table(
        self,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        semantic_tables: List[str] = None,
    ) -> Optional[str]:
        """Находит основную таблицу по скорингу признаков.

        Алгоритм (см. AGENTS.md, T4.5):
          1. Скоринг таблиц по сумме сигналов (semantic + структурный).
          2. Все веса берутся из ``main_table_scoring.yaml``.
          3. Если у победителя score < ``min_score_for_pick`` —
             возвращаем ``None`` (fail-fast на стороне вызова).
        """
        from ..utils import get_table_columns
        from ..main_table_scoring_config import load_main_table_scoring_config
        # T5-linking / #14 MEDIUM: numeric-типы берём из yaml, а не из хардкода
        from ..type_categories_config import load_type_categories_config

        if not db_schema:
            return None

        scoring = load_main_table_scoring_config()
        # Загружаем один раз вне внутреннего цикла (кэшируется)
        _type_cfg = load_type_categories_config()

        semantic_tables_list = list(semantic_tables) if semantic_tables else []
        semantic_tables_set = set(semantic_tables_list)

        if semantic_tables_list and not (semantic_tables_set & db_schema.keys()):
            logger.warning(
                "find_main_table: semantic memory suggested tables %s, but none "
                "of them are present in the current db_schema (keys sample: %s). "
                "This may indicate stale schema_table memory — re-index schema.",
                semantic_tables_list,
                list(db_schema.keys())[:10],
            )

        scored_tables: List[Tuple[str, int]] = []
        for table_name, table_schema in db_schema.items():
            score = 0
            table_columns = get_table_columns(table_schema)

            pk_count = 0
            fk_count = 0
            numeric_count = 0

            for col_name, col_info in table_columns.items():
                if isinstance(col_info, dict):
                    if is_pk(col_info):
                        pk_count += 1
                    if is_fk(col_info):
                        fk_count += 1
                    col_type = get_type(col_info)
                    # T5-linking / #14 MEDIUM: категории из yaml (integer + numeric),
                    # покрывают bigint/smallint/tinyint/serial/double/real и т.д.
                    if _type_cfg.get_category(col_type) in ("integer", "numeric"):
                        numeric_count += 1

            score += len(table_columns) * scoring.columns_count_weight
            score += pk_count * scoring.pk_weight
            score += fk_count * scoring.fk_weight
            score += numeric_count * scoring.numeric_weight

            if table_name in semantic_tables_set:
                score += scoring.semantic_match_weight

            scored_tables.append((table_name, score))

        if not scored_tables:
            return None

        scored_tables.sort(key=lambda x: x[1], reverse=True)
        best_table, best_score = scored_tables[0]

        if best_score < scoring.min_score_for_pick:
            logger.warning(
                "find_main_table fail-fast: no table has total score >= %d "
                "(semantic + structural). Provide entity terms or activate "
                "fallback NLU.",
                scoring.min_score_for_pick,
            )
            return None

        if best_table in semantic_tables_set:
            logger.info(
                "Selected main table via semantic+structural scoring: %s (score: %d)",
                best_table,
                best_score,
            )
        else:
            logger.info(
                "Selected main table (structural only): %s (score: %d)",
                best_table,
                best_score,
            )
        return best_table

    def _best_column_with_score(
        self,
        name: str,
        table: str,
        table_schema: Dict[str, Dict[str, Any]],
    ) -> Optional[Tuple[str, int]]:
        """Как ``best_column_for``, но возвращает (имя, score).

        Введено в W1-T5 (баг 4), чтобы ``link_filters`` мог сравнивать
        качество кандидатов из разных таблиц, а не брать первый попавшийся.
        """
        candidates = self._score_columns_for_name(name, table_schema)
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[1], x[0]))
        best_column, best_score = candidates[0]
        return best_column, best_score

    def best_column_for(
        self,
        name: str,
        table: str,
        table_schema: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """Находит наилучшую колонку для сущности.

        Алгоритм (см. AGENTS.md, T4.2) — никакого вшитого словаря синонимов:
          1. Exact-match по имени колонки (+10).
          2. Совпадение query_term с description колонки (+7).
          3. Совпадение query_term с именем FK references таблицы (+5).
          4. Type-match (+3 при primary signal / weight_solo иначе).
          5. Опциональный yaml-override через
             ``config/text_to_sql/column_aliases.yaml``.
        """
        from ..column_aliases_config import get_active_profile

        result = self._best_column_with_score(name, table, table_schema)
        if result is None:
            return None
        best_column, best_score = result
        profile = get_active_profile()
        logger.debug(
            "Best column for '%s' in %s: %s (score: %d, profile: %s)",
            name,
            table,
            best_column,
            best_score,
            profile.name,
        )
        return best_column

    def _best_column_for_with_diagnostics(
        self,
        name: str,
        table: str,
        table_schema: Dict[str, Dict[str, Any]],
        *,
        entity_type: str,
        diagnostics: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        candidates = self._score_columns_for_name(name, table_schema)
        top_candidates = self._top_candidates(
            [(table, column, score) for column, score in candidates]
        )
        if len(top_candidates) > 1:
            self._record_ambiguity(
                diagnostics,
                entity_type=entity_type,
                name=name,
                candidates=top_candidates,
            )
            return None
        return top_candidates[0][1] if top_candidates else None

    @staticmethod
    def _top_candidates(
        candidates: List[Tuple[str, str, int]],
    ) -> List[Tuple[str, str, int]]:
        if not candidates:
            return []
        best_score = max(score for _, _, score in candidates)
        return sorted(
            (candidate for candidate in candidates if candidate[2] == best_score),
            key=lambda candidate: (candidate[0], candidate[1]),
        )

    @staticmethod
    def _record_ambiguity(
        diagnostics: Optional[Dict[str, Any]],
        *,
        entity_type: str,
        name: str,
        candidates: List[Tuple[str, str, int]],
    ) -> None:
        if diagnostics is None:
            return
        diagnostics.setdefault("ambiguous_bindings", []).append(
            {
                "entity_type": entity_type,
                "name": name,
                "candidates": [
                    {"table": table, "column": column, "score": score}
                    for table, column, score in candidates
                ],
            }
        )

    def _score_columns_for_name(
        self,
        name: str,
        table_schema: Dict[str, Dict[str, Any]],
    ) -> List[Tuple[str, int]]:
        """Полный scoring всех колонок таблицы для заданного query-имени.

        Возвращает список ``(col_name, score)`` для всех колонок со
        ``score > 0`` (без сортировки). Используется ``best_column_for``
        и ``_best_column_with_score``.
        """
        import re
        from ..utils import get_table_columns
        from ..column_aliases_config import get_active_profile
        from ..nlu_config import canonicalize_token_via_morphemes
        from ..type_categories_config import load_type_categories_config

        if not name:
            return []

        name_lower = name.lower()
        profile = get_active_profile()
        alias_terms = profile.expand(name_lower)

        type_hints = profile.type_hints
        numeric_hints = type_hints.get("numeric", [])
        temporal_hints = type_hints.get("temporal", [])
        identifier_hints = type_hints.get("identifier", [])

        # Загружаем один раз на таблицу и передаём в _compute_type_hint_bonus,
        # чтобы не вызывать load_type_categories_config() на каждую колонку (L48).
        _type_cfg = load_type_categories_config()

        morphemes_cfg = self._get_morphemes_cfg()
        name_canonical = canonicalize_token_via_morphemes(name_lower, morphemes_cfg)

        def _canonicalize(token: str) -> str:
            return canonicalize_token_via_morphemes(token, morphemes_cfg)

        table_columns = get_table_columns(table_schema)
        candidates: List[Tuple[str, int]] = []

        def _name_matches_token(query: str, token: str) -> bool:
            q = _canonicalize(query)
            t = _canonicalize(token)
            return q == t or t.startswith(q) or q.startswith(t)

        for col_name, col_info in table_columns.items():
            score = 0
            primary_signal_present = False
            col_name_lower = col_name.lower()

            if col_name_lower == name_lower:
                score += 10
                primary_signal_present = True
            elif col_name_lower in alias_terms:
                score += 10
                primary_signal_present = True

            if isinstance(col_info, dict):
                description = str(col_info.get("description", "")).lower()
                if description:
                    desc_tokens = set(re.findall(r"\w+", description))
                    desc_canonicals = {_canonicalize(t) for t in desc_tokens}
                    if name_lower in desc_tokens or name_canonical in desc_canonicals:
                        score += 7
                        primary_signal_present = True
                    elif alias_terms:
                        if any(
                            term in desc_tokens
                            or _canonicalize(term) in desc_canonicals
                            for term in alias_terms
                            if term != name_lower
                        ):
                            score += 7
                            primary_signal_present = True

                if is_fk(col_info):
                    references = str(col_info.get("references", ""))
                    ref_table_name = _parse_fk_reference_table(references)
                    if ref_table_name:
                        ref_lower = ref_table_name.lower().rsplit(".", 1)[-1]
                        if _name_matches_token(name_lower, ref_lower):
                            score += 5
                            primary_signal_present = True
                        elif alias_terms and any(
                            _name_matches_token(term, ref_lower)
                            for term in alias_terms
                            if term != name_lower
                        ):
                            score += 5
                            primary_signal_present = True

                col_type = get_type(col_info).lower()
                if col_type:
                    type_bonus = self._compute_type_hint_bonus(
                        name_lower,
                        col_type,
                        numeric_hints,
                        temporal_hints,
                        identifier_hints,
                        primary_signal_present,
                        _type_cfg=_type_cfg,
                    )
                    if type_bonus > 0:
                        score += type_bonus
                        if not primary_signal_present:
                            primary_signal_present = True

            if score > 0:
                candidates.append((col_name, score))

        # W1-T5 (баг 3): tie-break применяется в callers
        # (``best_column_for`` / ``_best_column_with_score``) через
        # sort key=(-score, name), а не в порядке вставки.
        return candidates

    def link_filters(
        self,
        filters_in: Dict[str, Any],
        linked_dimensions: List[Dict[str, Any]],
        main_table: Optional[str],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Связывает фильтры с колонками схемы.

        4.15: сравнение ``filter_name`` с ``dim["name"]`` идёт через
        column_aliases.yaml (активный профиль).
        """
        return self._link_filters_with_diagnostics(
            filters_in,
            linked_dimensions,
            main_table,
            db_schema,
            diagnostics=None,
        )

    def _link_filters_with_diagnostics(
        self,
        filters_in: Dict[str, Any],
        linked_dimensions: List[Dict[str, Any]],
        main_table: Optional[str],
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        diagnostics: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from ..column_aliases_config import get_active_profile

        linked_filters: Dict[str, Any] = {}
        profile = get_active_profile()

        for filter_name, filter_value in filters_in.items():
            found_in_dimensions = False
            filter_name_lower = filter_name.lower() if filter_name else ""
            filter_aliases = set(profile.expand(filter_name_lower)) if filter_name_lower else set()
            for dim in linked_dimensions:
                dim_name_lower = dim.get("name", "").lower()
                if not dim_name_lower:
                    continue
                if dim_name_lower in filter_aliases or dim_name_lower == filter_name_lower:
                    linked_filters[filter_name] = {
                        "table": dim["table"],
                        "column": dim["column"],
                        "value": filter_value,
                        "source": "dimension_match",
                    }
                    found_in_dimensions = True
                    break

            if found_in_dimensions:
                continue

            candidates: List[Tuple[str, str, int]] = []
            for table_name, table_schema in db_schema.items():
                candidates.extend(
                    (table_name, column, score)
                    for column, score in self._score_columns_for_name(
                        filter_name, table_schema
                    )
                )

            top_candidates = self._top_candidates(candidates)
            if len(top_candidates) > 1:
                self._record_ambiguity(
                    diagnostics,
                    entity_type="filter",
                    name=filter_name,
                    candidates=top_candidates,
                )
            elif top_candidates:
                best_table, best_col, _ = top_candidates[0]
                linked_filters[filter_name] = {
                    "table": best_table,
                    "column": best_col,
                    "value": filter_value,
                    "source": (
                        "main_table" if best_table == main_table else "other_table"
                    ),
                }

        return linked_filters
