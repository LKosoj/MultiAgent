"""DSN-профиль как приоритетный источник эвристических подсказок (W1-1.2b).

Пользовательское требование (см. ``dsn_profile.py``): «профиль должен
собираться под конкретный DSN в разрезе схемы БД! Не может быть
универсальных профилей». Часть A (``dsn_profile.py`` + промпт
schema-linking) уже читает DSN-профиль. Этот модуль подключает остальных
читателей именованных профилей (``column_aliases.yaml`` /
``significance.yaml`` / ``nlu_morphemes.yaml``) к тому же DSN-профилю.

Правило приоритета — простое и одинаковое для каждой независимой секции
(``aliases``, ``type_hints``, ``metric_hints.significant_columns``,
``nlu_hints``): если DSN-профиль непуст в этой секции, он ПОЛНОСТЬЮ
ЗАМЕЩАЕТ named-профиль (а не мержится с ним поключево). Секции
независимы друг от друга: DSN может задать только ``aliases``, оставив
``type_hints`` наследоваться от named-профиля, и наоборот.

Почему замещение, а не merge: ``DsnProfile`` не хранит метаданные о том,
какие конкретно ключи внутри секции автор профиля «имел в виду»
переопределить, а какие — оставить как в named-профиле. Мерж по ключам
для мапы (``aliases``/``type_hints``) можно было бы определить, но для
``significant_columns`` (набор из нескольких списков) и ``nlu_hints``
(произвольная секция nlu_morphemes.yaml) такое поключевое решение уже
неоднозначно и увеличивает поверхность непредсказуемого поведения.
Полное замещение секции — единственное правило, которое одинаково
работает для всех четырёх источников и не требует специального case
для каждого.

Обработка ошибок ``load_dsn_profile``:
  * ``ValueError`` (профиль от другого DSN / битый yaml / неизвестные
    ключи) — это не повод ронять schema-linking, т.к. DSN-профиль
    полностью опционален. Логируем ``logger.warning`` и работаем так,
    будто DSN-профиля нет (``DsnProfile.empty()``).
  * ``RuntimeError`` (``TEXT_TO_SQL_DSN_PROFILE_STRICT=1`` +
    несовпадение ``schema_namespace_version``) — явный сигнал «профиль
    устарел», проглатывать нельзя, поэтому пробрасывается наверх.
    Функции этого модуля не передают ``live_schema_fingerprint`` в
    ``load_dsn_profile`` (эта проверка вне зоны ответственности
    эвристического слоя), поэтому на практике здесь это ветвление не
    достигается — оставлено для форвард-совместимости с ``dsn_profile.py``.

Все функции принимают ``dsn: Optional[str]``. Если ``dsn`` пуст/``None``
(DSN не проброшен вызывающим кодом) — поведение байт-в-байт как до
W1-1.2b: используется только named-профиль (env-переменная профиля).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

import yaml

from . import column_aliases_config as _column_aliases_config
from . import nlu_config as _nlu_config
from . import significance_config as _significance_config
from .column_aliases_config import ColumnAliasesProfile
from .dsn_profile import DsnProfile, load_dsn_profile_or_empty
from .nlu_config import NLUMorphemes, NLUMorphemesRegistry
from .significance_config import SignificanceProfile
from .utils import dsn_to_sanitized_name

logger = logging.getLogger(__name__)

# NOTE: функции named-профилей (``get_active_profile`` / ``load_significance_
# config`` / ``load_nlu_morphemes`` и приватные хелперы nlu_config) намеренно
# вызываются через ссылку на модуль (``_column_aliases_config.get_active_
# profile()``), а не через `from module import func` на верхнем уровне.
# Существующие тесты (см. tests/test_text_to_sql_fail_fast_W1.py) делают
# ``monkeypatch.setattr("custom_tools.text_to_sql.nlu_config.load_nlu_
# morphemes", ...)`` — это патчит атрибут МОДУЛЯ. Если бы имя функции было
# импортировано в этот файл напрямую при импорте модуля, патч бы не был
# виден (label уже был бы привязан к старому объекту функции). Доступ через
# `_nlu_config.load_nlu_morphemes(...)` каждый раз читает актуальный
# атрибут модуля — так же, как раньше это делали локальные `from .nlu_config
# import load_nlu_morphemes` внутри тел функций-читателей.


def _safe_load_dsn_profile(dsn: Optional[str], *, purpose: str) -> DsnProfile:
    """DSN-профиль для ``dsn`` либо ``DsnProfile.empty()``.

    Тонкая обёртка над общим ``dsn_profile.load_dsn_profile_or_empty`` —
    раньше этот try/except (warning + fallback на пустой профиль при
    ``ValueError``, проброс ``RuntimeError`` STRICT как есть) был продублирован
    здесь и в ``prompts._resolve_schema_linking_domain_examples``; теперь
    оба читателя используют одну реализацию.
    """
    return load_dsn_profile_or_empty(dsn, purpose=purpose)


def resolve_column_aliases_profile(dsn: Optional[str] = None) -> ColumnAliasesProfile:
    """Профиль алиасов/type_hints колонок с приоритетом DSN-профиля.

    ``aliases`` и ``type_hints`` замещаются НЕЗАВИСИМО друг от друга:
    если DSN-профиль задаёт только ``aliases`` — ``type_hints`` всё равно
    берутся из named-профиля, и наоборот.
    """
    named = _column_aliases_config.get_active_profile()
    dsn_profile = _safe_load_dsn_profile(dsn, purpose="column_aliases")

    aliases: Mapping[str, Any] = dsn_profile.aliases if dsn_profile.aliases else named.aliases
    type_hints_raw: Mapping[str, Any] = (
        dsn_profile.type_hints if dsn_profile.type_hints else named.type_hints
    )
    # DsnProfile.type_hints хранит tuple[str, ...] на категорию;
    # ColumnAliasesProfile ожидает list[str] или dict (см.
    # column_aliases_config._normalize_category_hints).
    type_hints = {
        category: (list(value) if isinstance(value, tuple) else value)
        for category, value in type_hints_raw.items()
    }

    overridden = bool(dsn_profile.aliases) or bool(dsn_profile.type_hints)
    name = f"{named.name}+dsn_override" if overridden else named.name

    return ColumnAliasesProfile(
        name=name,
        aliases=dict(aliases),
        type_hints=type_hints,
        type_hint_categories=tuple(type_hints.keys()),
    )


def resolve_significance_profile(dsn: Optional[str] = None) -> SignificanceProfile:
    """Профиль значимости колонок с приоритетом DSN-профиля.

    ``significant_columns`` — одна секция, замещается целиком (все пять
    полей разом), если в DSN-профиле она непуста (``SignificantColumnHints.
    is_empty()`` проверяет все поля). ``medium_priority_patterns`` в
    DSN-профиле хранится как текст паттерна (``dsn_profile._parse_pattern_pairs``
    — «БЕЗ компиляции regex»), поэтому здесь паттерны компилируются через
    ``re.compile`` перед тем, как попасть в ``SignificanceProfile``
    (которая ожидает уже скомпилированные regex, см. ``significance_config.
    SignificanceProfile``). ``important_column_name_substrings`` в
    named-профиле хранится в lower-case (``significance_config._build_profile``)
    — здесь применяется та же нормализация для консистентности сравнения.
    """
    named = _significance_config.load_significance_config()
    dsn_profile = _safe_load_dsn_profile(dsn, purpose="significance")
    hints = dsn_profile.metric_hints.significant_columns
    if hints.is_empty():
        return named

    return SignificanceProfile(
        name=f"{named.name}+dsn_override",
        high_priority_exact=frozenset(hints.high_priority_exact),
        high_priority_compound=frozenset(hints.high_priority_compound),
        medium_priority_patterns=tuple(
            (re.compile(pattern), description)
            for pattern, description in hints.medium_priority_patterns
        ),
        critical_description_keywords=frozenset(hints.critical_description_keywords),
        important_column_name_substrings=frozenset(
            s.lower() for s in hints.important_column_name_substrings
        ),
    )


def resolve_nlu_morphemes(
    dsn: Optional[str] = None,
    *,
    registry: Optional[NLUMorphemesRegistry] = None,
) -> NLUMorphemes:
    """NLU-морфемы (nlu_morphemes.yaml) с приоритетом DSN-профиля.

    ``DsnProfile.nlu_hints`` — 1:1 секция ``profiles.<name>`` из
    ``nlu_morphemes.yaml``: те же ключи, что и в теле профиля
    (``enabled``, ``intents``, ``dimensions``, ``relative_date``,
    ``patterns``, ``order``, ``intent_rules``, ``default_intent``,
    ``top_n_intent``, ``tokenizer``, ``regions``, и т.д.) — эта функция
    их все мержит одинаково (без разбора по ключу).

    Замещение — целиком по ``nlu_hints`` (не по ключевое): если секция
    непуста, её ключи накладываются на уже резолвленный named-профиль
    (``nlu_config._apply_profile``), поверх структурных top-level полей
    yaml (``version``/``table_name_inflections``/...), которые всегда
    общие и не входят в ``nlu_hints``.

    ``tokenizer`` (W1-1.2-review, замечание 4): merge сюда его технически
    затягивает как любой другой ключ, но НЕ означает, что DSN-профиль
    реально переопределяет токенизатор в проде — единственный потребитель
    ``cfg.tokenizer_adpositions`` (``NLUProcessor._fallback_tokenize`` /
    ``process_text``) вызывает ``_require_fallback_cfg`` без ``dsn`` вовсе,
    т.е. эта функция для токенизации всегда получает ``dsn=None`` и
    DSN-профиль сюда никогда не доходит. Честно: реально работающее
    покрытие DSN-профиля через эту функцию — только для
    ``_fallback_extract_intent`` (там ``dsn`` пробрасывается).
    """
    # ``registry=None`` (не передан явно) вызывает load_nlu_morphemes()
    # без kwarg вовсе — так же, как это делали читатели-вызовы до
    # W1-1.2b. Некоторые существующие тесты подменяют
    # ``nlu_config.load_nlu_morphemes`` стабом без параметров
    # (``lambda: ...``), и явный ``registry=None`` ломал бы такой стаб.
    named = (
        _nlu_config.load_nlu_morphemes(registry=registry)
        if registry is not None
        else _nlu_config.load_nlu_morphemes()
    )
    dsn_profile = _safe_load_dsn_profile(dsn, purpose="nlu_morphemes")
    if not dsn_profile.nlu_hints:
        return named

    yaml_path = _nlu_config._resolve_path()
    # ``named`` уже был успешно загружен из этого пути — файл существует
    # и синтаксически валиден, повторный парсинг детерминирован.
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"nlu_morphemes.yaml at {yaml_path} must contain a mapping at the top level"
        )

    profile_name = _nlu_config.resolve_active_profile()
    resolved_named_raw = _nlu_config._apply_profile(raw, profile_name, source=str(yaml_path))
    merged = dict(resolved_named_raw)
    merged.update(dsn_profile.nlu_hints)

    # Блокер 3: source_path попадает в RuntimeError-сообщение (см.
    # nlu.py::_require_fallback_cfg — "config ({cfg.source_path}: enabled=false)").
    # Сырой ``dsn`` может содержать credentials (user:password@host) — вместо
    # него используем ``dsn_to_sanitized_name`` (host:port:db, без пароля).
    return NLUMorphemes(
        merged,
        source_path=f"{yaml_path} (dsn override, dsn={dsn_to_sanitized_name(dsn)})",
    )


__all__ = [
    "resolve_column_aliases_profile",
    "resolve_significance_profile",
    "resolve_nlu_morphemes",
]
