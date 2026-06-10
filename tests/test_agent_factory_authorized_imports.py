"""
Тесты для critical #1 аудита 29.05.2026: additional_authorized_imports='*'.

CodeAgent больше не должен создаваться с неограниченными импортами ('*'):
- дефолт — курируемый allowlist AUTHORIZED_IMPORTS (модульная константа);
- профиль может переопределить список ключом authorized_imports;
- '*' возможен только явным указанием в профиле (осознанный opt-in).

Проверка AST-уровневая (без сборки агента через фабрику — тяжёлые зависимости),
по паттерну test_thread_safety_finish_generation.py.
"""

import ast
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "agent_factory.py"


def _module_tree() -> ast.Module:
    return ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))


def _authorized_imports_keywords(tree: ast.Module):
    """Все keyword-аргументы additional_authorized_imports в вызовах модуля."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "additional_authorized_imports":
                    found.append(kw)
    return found


def test_authorized_imports_constant_is_safe_allowlist():
    """Модульная константа AUTHORIZED_IMPORTS — непустой список строк без '*'."""
    tree = _module_tree()
    const_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "AUTHORIZED_IMPORTS" in targets:
                const_node = node
                break
    assert const_node is not None, "Константа AUTHORIZED_IMPORTS не найдена (снова мёртвая/удалена?)"

    value = ast.literal_eval(const_node.value)
    assert isinstance(value, list) and value, "AUTHORIZED_IMPORTS должен быть непустым списком"
    assert all(isinstance(item, str) for item in value)
    assert "*" not in value, "'*' в дефолтном allowlist отключает sandbox импортов"


def test_no_wildcard_literal_passed_to_agents():
    """Ни один вызов не передаёт additional_authorized_imports literal-'*'."""
    keywords = _authorized_imports_keywords(_module_tree())
    assert keywords, "CodeAgent должен получать additional_authorized_imports"
    for kw in keywords:
        if isinstance(kw.value, ast.Constant):
            assert kw.value.value != "*", (
                f"agent_factory.py:{kw.value.lineno}: additional_authorized_imports='*' "
                f"— неограниченный RCE (critical #1 аудита)"
            )


def test_authorized_imports_wired_to_profile_variable():
    """additional_authorized_imports берётся из переменной (profile-driven), не из литерала."""
    keywords = _authorized_imports_keywords(_module_tree())
    assert any(
        isinstance(kw.value, ast.Name) and kw.value.id == "authorized_imports"
        for kw in keywords
    ), "Ожидалась передача переменной authorized_imports (профиль или дефолтный allowlist)"


def test_profile_override_decision_logic():
    """Реплика решения фабрики: профиль переопределяет, пусто/None → дефолт."""
    default = ["pandas", "numpy"]

    def resolve(profile: dict):
        return profile.get("authorized_imports") or default

    assert resolve({}) == default
    assert resolve({"authorized_imports": None}) == default
    assert resolve({"authorized_imports": []}) == default
    assert resolve({"authorized_imports": ["json"]}) == ["json"]
    # '*' остаётся возможным ТОЛЬКО как явный opt-in профиля
    assert resolve({"authorized_imports": ["*"]}) == ["*"]
