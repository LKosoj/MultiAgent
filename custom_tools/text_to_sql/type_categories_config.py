"""
Загрузчик yaml-конфига SQL type-categories.

Конфиг — единственный source of truth для классификации SQL-типов по
группам (integer/numeric/string/temporal) и для совместимости групп при
type-aware join validation. Раньше эти списки были захардкожены в
``schema_metadata.py::ColumnMetadataHelper.check_type_compatibility`` (см.
AGENTS.md, EPIC 5.2).

Контракт:
  * Путь по умолчанию: ``config/text_to_sql/type_categories.yaml``.
  * Путь переопределяется через env ``TEXT_TO_SQL_TYPE_CATEGORIES_PATH``.
  * Файл обязателен: если его нет — ``FileNotFoundError`` без молчаливых
    дефолтов.
  * Содержимое кэшируется по абсолютному пути; ``reset_cache()`` для тестов.

Контракт классификации:
  * ``get_category(sql_type)`` — substring-match по lowercase-форме
    ``sql_type``. Сравнение жадное: первая подошедшая категория выигрывает,
    порядок категорий — как в yaml.
  * Если ни одна категория не подошла → ``"other"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Tuple

from ._yaml_config_loader import YamlConfigLoader, build_mapping_error_message

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = (
    _REPO_ROOT / "config" / "text_to_sql" / "type_categories.yaml"
)

_ENV_VAR = "TEXT_TO_SQL_TYPE_CATEGORIES_PATH"
_OTHER_CATEGORY = "other"


class TypeCategoriesConfig:
    """Прочитанный yaml-конфиг категорий SQL-типов."""

    __slots__ = (
        "version",
        "categories",
        "_category_order",
        "_compat_pairs",
        "source_path",
    )

    def __init__(self, raw: Dict[str, Any], source_path: str) -> None:
        self.source_path = source_path
        self.version = raw.get("version")

        raw_categories = raw.get("categories")
        if not isinstance(raw_categories, dict) or not raw_categories:
            raise ValueError(
                "type_categories.yaml: 'categories' must be a non-empty mapping"
            )

        categories: Dict[str, Tuple[str, ...]] = {}
        order: List[str] = []
        for name, tokens in raw_categories.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "type_categories.yaml: category names must be non-empty strings"
                )
            if name == _OTHER_CATEGORY:
                raise ValueError(
                    f"type_categories.yaml: '{_OTHER_CATEGORY}' is reserved "
                    "(used as fallback for unknown types)"
                )
            if (
                not isinstance(tokens, list)
                or not tokens
                or not all(isinstance(t, str) and t for t in tokens)
            ):
                raise ValueError(
                    f"type_categories.yaml: categories.{name} must be a "
                    "non-empty list of non-empty strings"
                )
            categories[name] = tuple(t.lower() for t in tokens)
            order.append(name)

        self.categories: Dict[str, Tuple[str, ...]] = categories
        self._category_order: Tuple[str, ...] = tuple(order)

        raw_compat = raw.get("compatibility") or []
        if not isinstance(raw_compat, list):
            raise ValueError(
                "type_categories.yaml: 'compatibility' must be a list of "
                "[category_a, category_b] pairs"
            )
        compat_pairs: set[FrozenSet[str]] = set()
        for idx, pair in enumerate(raw_compat):
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not all(isinstance(item, str) and item for item in pair)
            ):
                raise ValueError(
                    f"type_categories.yaml: compatibility[{idx}] must be a "
                    "[category_a, category_b] pair of non-empty strings"
                )
            a, b = pair
            if a not in categories or b not in categories:
                raise ValueError(
                    f"type_categories.yaml: compatibility[{idx}] references "
                    f"unknown category (got {pair!r}; known: {sorted(categories)})"
                )
            compat_pairs.add(frozenset({a, b}))

        self._compat_pairs: FrozenSet[FrozenSet[str]] = frozenset(compat_pairs)

    def get_category(self, sql_type: str) -> str:
        """Категория для SQL-типа (substring match, lowercase).

        Если ни одна категория не подошла — возвращает ``"other"``.
        Пустая/``None``-форма приведёт к ``"other"`` без exception
        (тип-классификация — не место для fail-fast по пустому имени).
        """
        if not sql_type:
            return _OTHER_CATEGORY
        lowered = sql_type.lower().strip()
        for name in self._category_order:
            for token in self.categories[name]:
                if token in lowered:
                    return name
        return _OTHER_CATEGORY

    def is_compatible(self, type_a: str, type_b: str) -> bool:
        """Совместимы ли два SQL-типа для join validation.

        Совместимы, если:
          * категория совпадает (включая ``"other"`` ↔ ``"other"``);
          * либо пара категорий присутствует в ``compatibility`` yaml.
        """
        cat_a = self.get_category(type_a)
        cat_b = self.get_category(type_b)
        return self.is_compatible_categories(cat_a, cat_b)

    def is_compatible_categories(self, category_a: str, category_b: str) -> bool:
        """Совместимы ли две заранее резолвленные категории.

        Используется, когда категории получены через кастомный
        ``type_resolver`` (плагинная категоризация). Контракт совместимости
        тот же, что и в ``is_compatible``: равенство категорий или наличие
        пары в ``compatibility`` yaml.
        """
        if category_a == category_b:
            return True
        return frozenset({category_a, category_b}) in self._compat_pairs


def _not_found_message(path: Path, env_var: str) -> str:
    return (
        "Type categories config not found at "
        f"{path}. Set {env_var} or create "
        "config/text_to_sql/type_categories.yaml. "
        "SQL type classification requires an explicit yaml source of "
        "truth (no hardcoded type groups in Python)."
    )


def _mapping_error_message(path: Path) -> str:
    return build_mapping_error_message(path, "type_categories.yaml")


_loader: YamlConfigLoader[TypeCategoriesConfig] = YamlConfigLoader[TypeCategoriesConfig](
    env_path_var=_ENV_VAR,
    default_path=_DEFAULT_CONFIG_PATH,
    parser=lambda raw, src: TypeCategoriesConfig(raw, source_path=src),
    not_found_message=_not_found_message,
    mapping_error_message=_mapping_error_message,
)


def load_type_categories_config() -> TypeCategoriesConfig:
    """Загрузить и закэшировать конфиг категорий SQL-типов.

    Конфиг обязателен: при отсутствии файла поднимается ``FileNotFoundError``.
    """
    return _loader.load()


def reset_cache() -> None:
    """Сброс кэша (нужен в тестах после подмены env)."""
    _loader.reset_cache()
