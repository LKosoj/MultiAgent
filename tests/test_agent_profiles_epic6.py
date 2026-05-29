"""Tests for EPIC 6, block B — agent profile refactor (tasks 6.4..6.15).

Каждая задача проверяется отдельно. Тесты читают yaml боевых профилей
напрямую, без запуска LLM/инструментов.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = PROJECT_ROOT / "agent_profiles"


def _load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.yaml"
    assert path.exists(), f"profile {name} not found at {path}"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"profile {name} must be a YAML mapping"
    return data


def _raw_text(name: str) -> str:
    path = PROFILES_DIR / f"{name}.yaml"
    return path.read_text(encoding="utf-8")


# ---------------- 6.4 db_audit_agent ----------------

def test_db_audit_agent_passes_row_limit():
    """6.4: db_audit_agent должен явно требовать row_limit={max_rows} при вызове secure_db_executor."""
    profile = _load_profile("db_audit_agent")
    prompt = profile.get("prompt_templates", "")
    assert "secure_db_executor" in prompt
    assert "row_limit" in prompt, "prompt должен явно требовать передачу row_limit"
    assert "{max_rows}" in prompt or "max_rows" in prompt, "prompt должен ссылаться на max_rows"


def test_db_audit_agent_dry_run_only_skips():
    """6.4: при dry_run_only=true агент НЕ выполняет SQL и возвращает executed=false / skipped."""
    profile = _load_profile("db_audit_agent")
    prompt = profile.get("prompt_templates", "")
    assert "dry_run_only" in prompt
    # Описание output contract должно фиксировать executed и skipped/dry_run_only.
    lowered = prompt.lower()
    assert "executed" in lowered, "prompt должен описывать поле executed"
    assert "skipped" in lowered, "prompt должен явно описывать режим skipped"
    # Имена полей контракта не должны быть переименованы.
    for required_field in ("data", "columns", "rows_affected", "dry_run_only", "executed"):
        assert required_field in prompt, f"output contract потерял поле {required_field}"


# ---------------- 6.5 sql_verifier_agent ----------------

def test_sql_verifier_has_custom_report_template():
    """6.5: должен появиться custom_report_template '{{final_answer}}'."""
    profile = _load_profile("sql_verifier_agent")
    assert profile.get("custom_report_template") == "{{final_answer}}"


def test_sql_verifier_task_template_requires_json():
    """6.5: custom_task_template требует валидный JSON с фиксированными полями."""
    profile = _load_profile("sql_verifier_agent")
    task_template = profile.get("custom_task_template", "")
    assert task_template, "custom_task_template должен быть определён"
    # Требуем явное указание JSON-only ответа.
    assert "JSON" in task_template or "json" in task_template
    # Перечислены обязательные поля JSON-ответа.
    for required_field in (
        "verification_status",
        "safety_check",
        "performance_check",
        "recommendations",
    ):
        assert required_field in task_template, (
            f"custom_task_template не упоминает поле {required_field}"
        )
    # Approved/Rejected — downstream pipeline парсит точные значения.
    assert "Approved" in task_template
    assert "Rejected" in task_template


# ---------------- 6.6 schema_rag_agent (prompt brevity) ----------------

def test_schema_rag_prompt_under_limit():
    """6.6: prompt_templates schema_rag_agent должен умещаться в <100 строк после дедупликации."""
    profile = _load_profile("schema_rag_agent")
    prompt = profile.get("prompt_templates", "")
    prompt_lines = prompt.splitlines()
    assert len(prompt_lines) < 100, (
        f"prompt_templates длиной {len(prompt_lines)} строк, ожидалось < 100"
    )


def test_schema_rag_output_contract_preserved():
    """6.6: контракт выходного JSON не должен потерять обязательные поля.

    Дополнительно фиксируем «жёсткие» правила формата (Block B.3): JSON-only,
    двойные кавычки, экранирование. Цель — защитить контракт после сокращения
    prompt, чтобы рефакторинг случайно не убрал явные требования.
    """
    profile = _load_profile("schema_rag_agent")
    prompt = profile.get("prompt_templates", "")
    for required_field in (
        "linked_entities",
        "metrics",
        "dimensions",
        "filters",
        "joins",
        "schema_info",
        "distinct_values",
        "table_schema",
        "error",
    ):
        assert required_field in prompt, (
            f"prompt schema_rag_agent потерял упоминание поля {required_field}"
        )
    # custom-шаблоны не должны быть случайно удалены.
    assert profile.get("custom_report_template") == "{{final_answer}}"
    assert "custom_task_template" in profile
    assert profile.get("custom_task_template")

    # Block B.3: жёсткие правила формата ответа.
    lowered = prompt.lower()
    # "JSON only, no extra text" — должно быть явное требование выдавать только JSON.
    assert "json" in lowered, "prompt должен явно требовать JSON-формат ответа"
    json_only_markers = (
        "только итоговый валидный json",
        "никакого текста",
        "без текста до/после",
        "без обёрток",
    )
    assert any(marker in lowered for marker in json_only_markers), (
        "prompt должен содержать явное требование 'только JSON, без лишнего текста' "
        f"(ищем один из {json_only_markers})"
    )
    # Требование двойных кавычек в JSON.
    assert "двойные кавычки" in lowered, (
        "prompt должен явно требовать двойные кавычки в JSON"
    )
    # Требование экранирования.
    assert "экранир" in lowered, (
        "prompt должен описывать правила экранирования спецсимволов в строках JSON"
    )

    # custom_task_template тоже не должен терять JSON-only требование.
    task_template = profile.get("custom_task_template", "").lower()
    assert "json" in task_template, (
        "custom_task_template должен описывать JSON-only контракт final_answer"
    )


# ---------------- 6.7 sql_generator_agent ----------------

def test_sql_generator_safety_feedback_in_prompt():
    """6.7: prompt должен описывать feedback loop через sql_safety_check_feedback и роль sql_generation_plugin."""
    profile = _load_profile("sql_generator_agent")
    prompt = profile.get("prompt_templates", "")
    assert "sql_safety_check_feedback" in prompt, (
        "prompt должен описывать структурный маркер sql_safety_check_feedback"
    )
    assert "recommendations" in prompt, (
        "feedback loop должен учитывать recommendations"
    )
    assert "sql_generation_plugin" in prompt
    # Выходной контракт {sql, description} не меняем.
    assert '"sql"' in prompt
    assert '"description"' in prompt


# ---------------- 6.8 nlu_agent ----------------

def test_nlu_agent_retained_with_comment():
    """6.8: nlu_agent.yaml сохранён и содержит описательный комментарий о месте использования."""
    raw = _raw_text("nlu_agent")
    # Файл начинается с комментария-объяснения (decision: RETAIN).
    first_lines = raw.splitlines()[:5]
    joined = "\n".join(first_lines)
    assert joined.lstrip().startswith("#"), (
        "файл должен начинаться с поясняющего комментария"
    )
    assert "data_analysis.yaml" in joined
    assert "agent_system.py" in joined
    assert "text_to_sql_pipeline.yaml" in joined


# ---------------- 6.13 schema_rag_agent type ----------------

def test_schema_rag_agent_type_tool_calling():
    """6.13: schema_rag_agent должен использовать type: tool_calling вместо code."""
    profile = _load_profile("schema_rag_agent")
    assert profile.get("type") == "tool_calling"
    # Список инструментов и max_tool_threads должны быть совместимы с ToolCallingAgent.
    assert profile.get("tools") == ["schema_linking", "get_distinct_values", "schema_info"]
    assert profile.get("max_tool_threads") == 1
    # custom-шаблоны должны быть совместимы с tool_calling (просто сохранены).
    assert profile.get("custom_report_template") == "{{final_answer}}"
    assert profile.get("custom_task_template")


# ---------------- 6.15 optimization_metadata sidecar ----------------

SIDECAR_PATH = PROFILES_DIR / "optimization_metadata.yaml"


def _battle_profile_files() -> list[Path]:
    """Возвращает все боевые профильные yaml (без sidecar)."""
    result = []
    for path in PROFILES_DIR.glob("*.yaml"):
        if path.name == "optimization_metadata.yaml":
            continue
        result.append(path)
    return result


def test_optimization_metadata_sidecar_exists():
    """6.15: sidecar присутствует, валидный YAML-словарь с известными агентами."""
    assert SIDECAR_PATH.exists(), f"sidecar {SIDECAR_PATH} должен существовать"
    with open(SIDECAR_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "sidecar должен быть словарём agent_name -> metadata"
    # Ключи существующих профилей должны быть представлены.
    for required_agent in (
        "db_audit_agent",
        "sql_verifier_agent",
        "sql_generator_agent",
        "schema_rag_agent",
    ):
        assert required_agent in data, f"sidecar не содержит {required_agent}"
        meta = data[required_agent]
        assert isinstance(meta, dict)
        assert "optimized_at" in meta
        assert "optimizer_model" in meta


def test_no_optimization_metadata_in_battle_profiles():
    """6.15: ни один боевой профиль не должен содержать optimization_metadata в самом yaml."""
    offenders = []
    for path in _battle_profile_files():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            continue
        if "optimization_metadata" in data:
            offenders.append(path.name)
    assert not offenders, (
        f"следующие боевые профили всё ещё содержат optimization_metadata: {offenders}"
    )


def test_prompt_optimizer_reads_sidecar(tmp_path, monkeypatch):
    """6.15: PromptOptimizer.get_optimization_metadata сначала ищет в sidecar."""
    from prompt_optimizer.prompt_optimizer import PromptOptimizer

    optimizer = PromptOptimizer()
    # Считаем sidecar напрямую и через метод — данные должны совпасть.
    metadata_db = optimizer.get_optimization_metadata("db_audit_agent")
    assert isinstance(metadata_db, dict)
    assert metadata_db.get("optimizer_model"), (
        "metadata из sidecar для db_audit_agent должна содержать optimizer_model"
    )

    # Fallback: если в sidecar агента нет, используется legacy-поле из профиля.
    fake_profile = {
        "optimization_metadata": {
            "optimized_at": "2024-01-01T00:00:00",
            "optimizer_model": "legacy-model",
        }
    }
    legacy = optimizer.get_optimization_metadata(
        "__nonexistent_agent_for_test__", fake_profile
    )
    assert legacy.get("optimizer_model") == "legacy-model"

    # Если нет ни в sidecar, ни в профиле — пустой dict.
    empty = optimizer.get_optimization_metadata("__nonexistent_agent_for_test__")
    assert empty == {}
