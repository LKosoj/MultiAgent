"""W9-A13: detection of duplicate line-blocks in agent profile YAMLs.

Дубликаты внутри scalar-полей (например, ``prompt_templates``) НЕ устраняются
YAML anchors — anchor работает на уровне YAML-нод, а не фрагментов scalar.
В sql_generator_agent.yaml исторический дубликат блока «Анализ контекста /
Генерация SQL / Форматирование / ОБЯЗАТЕЛЬНЫЙ ВЫВОД» был устранён удалением
секции ``# Methodology`` (W9-A13 follow-up, ручное согласование с user).

Тест ловит регрессии: новые windows-дубликаты (≥5 строк подряд)
в scalar-полях И структурные дубликаты на уровне mapping/sequence-нод.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_PROFILES_DIR = REPO_ROOT / "agent_profiles"


def _normalize(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _find_duplicate_windows(lines: list[str], window: int = 5) -> list[tuple[int, int, tuple[str, ...]]]:
    """Sliding-window поиск точных дубликатов нормализованных строк.

    Возвращает список (orig_line_no, dup_line_no, window_tuple) — 1-based номера.
    """
    seen: dict[tuple[str, ...], int] = {}
    duplicates: list[tuple[int, int, tuple[str, ...]]] = []
    for i in range(len(lines) - window + 1):
        win = tuple(_normalize(l) for l in lines[i : i + window])
        # Игнорируем windows, состоящие в основном из пустых строк.
        if sum(1 for w in win if w) < max(2, window // 2):
            continue
        if win in seen:
            duplicates.append((seen[win] + 1, i + 1, win))
        else:
            seen[win] = i
    return duplicates


def test_sql_generator_agent_yaml_is_valid():
    """Базовая проверка: YAML парсится."""
    path = AGENT_PROFILES_DIR / "sql_generator_agent.yaml"
    with path.open() as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "prompt_templates" in data
    assert isinstance(data["prompt_templates"], str)


def test_no_duplicate_top_level_keys_in_agent_profiles():
    """Регрессия: YAML-mapping не должен содержать дублирующих ключей.

    Это случай, который YAML anchors могли бы устранить (alias на mapping-узел).
    """
    for yaml_path in sorted(AGENT_PROFILES_DIR.glob("*.yaml")):
        with yaml_path.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            continue
        # PyYAML тихо схлопывает дубликаты ключей в последний; явная проверка
        # на пары "key:" в начале строки. Проверяем только top-level (без отступов).
        lines = yaml_path.read_text().splitlines()
        top_level_keys = []
        for line in lines:
            # Только строки без ведущих пробелов и не комментарии.
            if not line or line.startswith(" ") or line.startswith("\t"):
                continue
            if line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                top_level_keys.append(m.group(1))
        # Проверяем что top_level_keys уникальны.
        seen = set()
        for key in top_level_keys:
            assert key not in seen, (
                f"duplicate top-level key '{key}' in {yaml_path.name} — "
                "должно быть устранено через YAML merge keys (<<: *anchor)"
            )
            seen.add(key)


def test_sql_generator_no_window_duplicates():
    """W9-A13: дубликаты внутри scalar prompt_templates не должны появляться.

    Исторический дубликат «# Instructions» vs «# Methodology» в
    sql_generator_agent.yaml устранён удалением секции Methodology
    (она дублировала Instructions). Этот тест ловит регрессии: новые
    >=5-строчные точные повторы в prompt_templates.

    Дубликат на уровне scalar нельзя устранить YAML anchors — только
    редактированием содержимого. Если падение неизбежно (например,
    переиспользование разделов между профилями), вынесите общий блок
    в отдельный YAML-файл и читайте его loader'ом.
    """
    path = AGENT_PROFILES_DIR / "sql_generator_agent.yaml"
    lines = path.read_text().splitlines()
    duplicates = _find_duplicate_windows(lines, window=5)

    assert not duplicates, (
        "Найдены дубликаты блоков в sql_generator_agent.yaml:\n"
        + "\n".join(
            f"  L{orig} vs L{dup}: {win[0][:80]!r}" for orig, dup, win in duplicates
        )
    )


def test_agent_profiles_no_new_structural_duplicates():
    """Регрессия: новые YAML mapping-узлы должны использовать anchors при повторе.

    Сейчас тест-canary: проходит, пока никто не вписал дубликат на уровне
    YAML-структуры (mapping/sequence), который должен быть anchor'ом.

    Алгоритм: для каждого профиля собираем сериализованные представления
    sub-mappings (исключая scalar) и ищем повторы. Текущий ожидаемый результат
    — пусто (все профили проходят).
    """
    structural_duplicates: list[str] = []

    def _walk(node, path: str, seen_mappings: dict[str, str]) -> None:
        if isinstance(node, dict):
            # Только non-trivial mappings (≥ 3 ключей).
            if len(node) >= 3:
                # Сериализуем детерминированно.
                key = yaml.safe_dump(node, sort_keys=True, default_flow_style=False)
                if key in seen_mappings:
                    structural_duplicates.append(
                        f"  {path} duplicates {seen_mappings[key]} "
                        f"(use YAML anchor &name + <<: *name)"
                    )
                else:
                    seen_mappings[key] = path
            for k, v in node.items():
                _walk(v, f"{path}.{k}", seen_mappings)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]", seen_mappings)

    for yaml_path in sorted(AGENT_PROFILES_DIR.glob("*.yaml")):
        with yaml_path.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, (dict, list)):
            continue
        seen_mappings: dict[str, str] = {}
        _walk(data, yaml_path.name, seen_mappings)

    assert not structural_duplicates, (
        "Найдены структурные дубликаты mapping-узлов в agent_profiles/. "
        "Должны быть устранены через YAML anchors:\n"
        + "\n".join(structural_duplicates)
    )
