"""CI-гейт: каждый `contract_name` из adaptive/models.py задокументирован.

Список контрактов состояния adaptive-пайплайна Text-to-SQL
(``custom_tools/text_to_sql/adaptive/models.py``) не имеет отдельного
JSON-зеркала (в отличие от терминального контракта, см.
``tests/test_text_to_sql_contract_schema_sync.py``), поэтому единственная
защита от рассинхрона документации и кода — этот AST-обход исходника:
он находит все классы с полем ``contract_name: Literal["..."] = "..."``
и проверяет, что имя присутствует в ``docs/text_to_sql_contracts.md`` как
элемент списка контрактов, и что документ не содержит лишних (более не
существующих в коде) имён.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MODELS_PATH = _PROJECT_ROOT / "custom_tools" / "text_to_sql" / "adaptive" / "models.py"
_DOC_PATH = _PROJECT_ROOT / "docs" / "text_to_sql_contracts.md"

# Матчит строки вида `- **`query_spec`** (класс `QuerySpec`) — ...`
# из раздела "Контракты состояния adaptive-пайплайна" в docs/text_to_sql_contracts.md.
_DOC_CONTRACT_BULLET_RE = re.compile(r"^- \*\*`([a-z][a-z0-9_]*)`\*\*", re.MULTILINE)

# Заголовок раздела 2 ("## 2. Контракты состояния adaptive-пайплайна (...)")
# и заголовок любого следующего раздела верхнего уровня — граница, за которую
# разбор буллетов заходить не должен (раздел 1 и раздел 3 не про этот список
# контрактов и не должны участвовать в сопоставлении имён).
_SECTION_2_HEADING_RE = re.compile(r"^## 2\.[^\n]*$", re.MULTILINE)
_ANY_SECTION_HEADING_RE = re.compile(r"^## ", re.MULTILINE)


def _extract_doc_section(text: str, heading_re: re.Pattern[str]) -> str:
    """Return the text between a heading match and the next `## ` heading (or EOF).

    Fails loudly (rather than silently falling back to scanning the whole
    document) if the heading cannot be located, e.g. because the section was
    renamed.
    """
    match = heading_re.search(text)
    if match is None:
        pytest.fail(
            f"could not find a heading matching {heading_re.pattern!r} in the "
            "contracts doc; update the heading regex to match the renamed "
            "section title"
        )
    start = match.end()
    next_match = _ANY_SECTION_HEADING_RE.search(text, start)
    end = next_match.start() if next_match is not None else len(text)
    return text[start:end]


def _contract_names_from_models() -> set[str]:
    """AST-обход models.py: имена всех `contract_name: Literal[...] = "..."`."""
    tree = ast.parse(_MODELS_PATH.read_text(encoding="utf-8"), filename=str(_MODELS_PATH))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "contract_name"
            ):
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                names.add(value.value)
            else:
                raise AssertionError(
                    f"{_MODELS_PATH}:{statement.lineno}: contract_name must be assigned "
                    "a string literal so this test can discover it via AST"
                )
    return names


def _contract_names_from_doc() -> set[str]:
    text = _DOC_PATH.read_text(encoding="utf-8")
    section = _extract_doc_section(text, _SECTION_2_HEADING_RE)
    return set(_DOC_CONTRACT_BULLET_RE.findall(section))


def test_every_model_contract_name_is_documented() -> None:
    code_names = _contract_names_from_models()
    assert code_names, "AST walk found no contract_name literals in models.py; regex/AST drifted"

    doc_names = _contract_names_from_doc()

    missing_from_doc = sorted(code_names - doc_names)
    assert not missing_from_doc, (
        "contract_name(s) defined in "
        f"{_MODELS_PATH} but missing from {_DOC_PATH}: {missing_from_doc}. "
        "Add a `- **`<name>`** (класс `<Class>`) — ...` bullet to the "
        "\"Контракты состояния adaptive-пайплайна\" section."
    )

    stale_in_doc = sorted(doc_names - code_names)
    assert not stale_in_doc, (
        f"{_DOC_PATH} documents contract_name(s) that no longer exist in "
        f"{_MODELS_PATH}: {stale_in_doc}. Remove the stale bullet(s) or fix the typo."
    )


# --- _extract_doc_section --------------------------------------------------

_THREE_SECTION_DOC_FIXTURE = """# Title

intro text before any section.

## 1. First section

- **`alpha`** (класс `Alpha`) — must not leak into section 2.

## 2. Second section

- **`beta`** (класс `Beta`) — kept.
- **`gamma`** (класс `Gamma`) — kept.

## 3. Third section

- **`delta`** (класс `Delta`) — must not leak into section 2.
"""


def test_extract_doc_section_returns_only_the_matched_section() -> None:
    heading_re = re.compile(r"^## 2\.[^\n]*$", re.MULTILINE)

    section = _extract_doc_section(_THREE_SECTION_DOC_FIXTURE, heading_re)

    assert "beta" in section
    assert "gamma" in section
    assert "alpha" not in section
    assert "delta" not in section


def test_extract_doc_section_fails_when_heading_is_missing() -> None:
    heading_re = re.compile(r"^## 9\.[^\n]*$", re.MULTILINE)

    with pytest.raises(pytest.fail.Exception):
        _extract_doc_section(_THREE_SECTION_DOC_FIXTURE, heading_re)
