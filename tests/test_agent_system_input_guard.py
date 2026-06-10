"""
Тесты для critical #2 аудита 29.05.2026: input-guard в agent_system.

Этап «Проверка безопасности на входе» в _coordinate_unlocked был полностью
закомментирован. Теперь он обязан существовать как живой код:
- гейтится наличием профиля input_guard_agent в AGENT_PROFILES
  (enable: false в yaml исключает профиль из словаря → этап пропускается);
- decision == BLOCK → немедленный return отказа;
- любой сбой guard → fail-closed (return, а не пропуск дальше);
- finally убирает guard из factory.agents (иначе он попадёт
  в managed_agents менеджера на этапе 2).

Проверка AST-уровневая: import agent_system тянет тяжёлый стек (smolagents,
matplotlib, html_utils), паттерн как в test_thread_safety_finish_generation.py.
"""

import ast
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "agent_system.py"


def _coordinate_unlocked_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_coordinate_unlocked":
            return node
    raise ValueError("Метод _coordinate_unlocked не найден")


def _guard_if_node(func: ast.AsyncFunctionDef) -> ast.If:
    """Находит if 'input_guard_agent' in AGENT_PROFILES: ..."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Constant)
            and node.test.left.value == "input_guard_agent"
            and any(isinstance(op, ast.In) for op in node.test.ops)
        ):
            return node
    raise AssertionError(
        "Этап input-guard не найден в _coordinate_unlocked как живой код "
        "(critical #2 аудита: этап был закомментирован)"
    )


def _guard_try_node(guard_if: ast.If) -> ast.Try:
    for node in ast.walk(guard_if):
        if isinstance(node, ast.Try):
            return node
    raise AssertionError("Внутри этапа input-guard ожидался try/except (fail-closed)")


def test_guard_stage_exists_and_gated_by_profile():
    """Этап guard существует и гейтится наличием профиля в AGENT_PROFILES."""
    guard_if = _guard_if_node(_coordinate_unlocked_ast())
    # внутри этапа создаётся именно input_guard_agent
    created = [
        node
        for node in ast.walk(guard_if)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_agent"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "input_guard_agent"
    ]
    assert created, "Этап guard должен создавать агента input_guard_agent через фабрику"


def test_block_decision_returns_refusal():
    """Ветка decision == BLOCK завершает координацию (return), а не идёт дальше."""
    guard_if = _guard_if_node(_coordinate_unlocked_ast())
    block_ifs = [
        node
        for node in ast.walk(guard_if)
        if isinstance(node, ast.If)
        and any(
            isinstance(c, ast.Constant) and c.value == "BLOCK"
            for c in ast.walk(node.test)
        )
    ]
    assert block_ifs, "Ожидалась проверка decision == 'BLOCK'"
    assert any(
        isinstance(stmt, ast.Return)
        for block_if in block_ifs
        for stmt in ast.walk(block_if)
    ), "BLOCK-ветка обязана возвращать отказ (return error_report)"


def test_guard_failure_is_fail_closed():
    """Исключение в guard → return отказа (fail-closed), а не пропуск запроса."""
    guard_try = _guard_try_node(_guard_if_node(_coordinate_unlocked_ast()))
    assert guard_try.handlers, "Ожидался except-обработчик"
    for handler in guard_try.handlers:
        assert any(
            isinstance(stmt, ast.Return) for stmt in ast.walk(handler)
        ), "except guard-этапа обязан возвращать отказ (fail-closed)"


def test_guard_agent_removed_from_factory_in_finally():
    """finally чистит factory.agents — guard не должен утечь в managed_agents менеджера."""
    guard_try = _guard_try_node(_guard_if_node(_coordinate_unlocked_ast()))
    assert guard_try.finalbody, "Ожидался finally с очисткой factory.agents"
    assigns_factory_agents = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "agents"
            for t in node.targets
        )
        for stmt in guard_try.finalbody
        for node in ast.walk(stmt)
    )
    assert assigns_factory_agents, "finally должен перезаписывать factory.agents без guard-агента"
