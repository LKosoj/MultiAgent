"""
Тесты для yaml-конфига SQL safety-валидатора (EPIC 2 блок A: 2.1, 2.7, 2.8).

Покрывают:
- 2.1: загрузка профилей default/strict, fail-fast при отсутствии файла/профиля
       или некорректной структуре yaml; validator использует значения из yaml.
- 2.7: thread-safety _SQLGLOT_METRICS под конкурентным validate(...).
- 2.8: накопление всех нарушений в одном проходе (без early break/return).
"""
from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("USE_SQLGLOT", "1")

from custom_tools.text_to_sql.validators import (  # noqa: E402
    SQLSafetyValidator,
    get_sqlglot_metrics,
    reset_sqlglot_metrics,
)
from custom_tools.text_to_sql.validators import safety_config  # noqa: E402
from custom_tools.text_to_sql.validators.safety_config import (  # noqa: E402
    load_safety_profile,
)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Гарантируем, что каждый тест видит свежий загрузчик."""
    safety_config.reset_cache()
    yield
    safety_config.reset_cache()


# ---------------------------------------------------------------------------
# 2.1 — yaml-конфиг и загрузчик
# ---------------------------------------------------------------------------
def test_default_profile_loads_from_yaml(monkeypatch):
    """`load_safety_profile()` без env → профиль default из репо-yaml."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    profile = load_safety_profile()

    assert profile.profile_name == "default"
    assert "INSERT" in profile.forbidden_keywords
    assert "DROP" in profile.forbidden_keywords
    assert "Insert" in profile.ast_forbidden_stmt_classes
    assert "LOAD" in profile.ast_forbidden_command_words
    assert profile.max_in_list_size == 1000
    assert profile.max_query_length == 10000


def test_env_profile_selects_strict(monkeypatch):
    """env TEXT_TO_SQL_SAFETY_PROFILE=strict → ужесточённые лимиты и список."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "strict")
    safety_config.reset_cache()

    profile = load_safety_profile()

    assert profile.profile_name == "strict"
    assert "SET" in profile.forbidden_keywords
    assert "SHOW" in profile.forbidden_keywords
    assert "KILL" in profile.ast_forbidden_command_words
    assert profile.max_query_length == 4000
    assert profile.max_in_list_size == 200


def test_fail_fast_missing_yaml(monkeypatch):
    """Несуществующий путь → FileNotFoundError, без silent default."""
    monkeypatch.setenv(
        "TEXT_TO_SQL_SAFETY_CONFIG_PATH", "/nonexistent/does_not_exist.yaml"
    )
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    with pytest.raises(FileNotFoundError):
        load_safety_profile()


def test_fail_fast_missing_profile(monkeypatch):
    """Несуществующий профиль → KeyError."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "nope_definitely_missing")
    safety_config.reset_cache()

    with pytest.raises(KeyError):
        load_safety_profile()


def test_fail_fast_malformed_yaml(tmp_path, monkeypatch):
    """Пустой forbidden_keywords / неверный тип → ValueError, без молчаливого fallback."""
    bad_yaml = tmp_path / "safety.yaml"
    bad_yaml.write_text(
        """
version: 1
profiles:
  default:
    forbidden_keywords: []
    ast_forbidden_stmt_classes: [Insert]
    ast_forbidden_command_words: [LOAD]
    max_query_length: 10000
    max_in_list_size: 1000
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", str(bad_yaml))
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    with pytest.raises(ValueError):
        load_safety_profile()

    # И с неверным типом значения (max_in_list_size = "many") — тоже ValueError.
    bad_yaml.write_text(
        """
version: 1
profiles:
  default:
    forbidden_keywords: [INSERT]
    ast_forbidden_stmt_classes: [Insert]
    ast_forbidden_command_words: [LOAD]
    max_query_length: 10000
    max_in_list_size: many
""".strip(),
        encoding="utf-8",
    )
    safety_config.reset_cache()
    with pytest.raises(ValueError):
        load_safety_profile()


def test_validator_uses_yaml_keywords(tmp_path, monkeypatch):
    """Тонкий yaml только с [INSERT] → DROP TABLE НЕ ловится через keyword path.

    Это доказывает, что списки больше не хардкожены в safety.py — они приходят
    исключительно из yaml. DROP всё равно может быть пойман через AST-маршрут
    (sqlglot распознает Drop-стейтмент), что разрешено: главное — нет
    хардкода DROP в forbidden_keywords.
    """
    minimal_yaml = tmp_path / "safety.yaml"
    minimal_yaml.write_text(
        """
version: 1
profiles:
  tiny:
    forbidden_keywords: [INSERT]
    ast_forbidden_stmt_classes: [Insert]
    ast_forbidden_command_words: [LOAD]
    max_query_length: 10000
    max_in_list_size: 1000
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", str(minimal_yaml))
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "tiny")
    safety_config.reset_cache()

    validator = SQLSafetyValidator()
    assert validator.forbidden_keywords == ["INSERT"]

    # Проверяем, что DROP отсутствует в forbidden_keywords у инстанса —
    # это и есть «отсутствие хардкода».
    assert "DROP" not in validator.forbidden_keywords

    # А вот INSERT должен честно ловиться regex-маршрутом.
    res = validator.validate("INSERT INTO t VALUES (1)")
    assert res["is_safe"] is False
    assert any(i["issue_type"] == "FORBIDDEN_STATEMENT" for i in res["issues"])


# ---------------------------------------------------------------------------
# 2.7 — thread-safety _SQLGLOT_METRICS
# ---------------------------------------------------------------------------
def test_metrics_thread_safety():
    """50 потоков validate(...) параллельно — все инкременты validation_count учтены."""
    reset_sqlglot_metrics()
    validator = SQLSafetyValidator()

    threads_count = 50
    barrier = threading.Barrier(threads_count)
    errors: list[Exception] = []

    def _worker():
        try:
            barrier.wait()
            validator.validate("SELECT 1 FROM t WHERE id = 1")
        except Exception as exc:  # pragma: no cover — диагностика гонок
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Worker errors: {errors}"
    metrics = get_sqlglot_metrics()
    assert metrics["validation_count"] == threads_count
    assert metrics["parse_attempts"] == threads_count


# ---------------------------------------------------------------------------
# 2.8 — накопление всех нарушений
# ---------------------------------------------------------------------------
def test_violations_accumulated():
    """`INSERT INTO x; UPDATE y -- comment` → как минимум 2 разных issue_type."""
    validator = SQLSafetyValidator()
    res = validator.validate("INSERT INTO x; UPDATE y -- comment")

    assert res["is_safe"] is False
    issue_types = {i["issue_type"] for i in res["issues"]}
    # COMMENTS_NOT_ALLOWED + FORBIDDEN_STATEMENT — оба должны присутствовать,
    # потому что после EPIC 2.8 валидатор не возвращается на первом совпадении.
    assert len(res["issues"]) >= 2
    assert len(issue_types) >= 2
    assert "FORBIDDEN_STATEMENT" in issue_types
    assert "COMMENTS_NOT_ALLOWED" in issue_types
