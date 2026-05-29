"""Constants for Text-to-SQL pipeline."""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_HISTORY_FILE = _PROJECT_ROOT / "logs" / "sql_history.jsonl"

# Канонические формы типов JOIN — единый источник истины для join_builder и sql_builder.
# join_builder.py использует для валидации уже-нормализованных типов (VALID_JOIN_TYPES).
# sql_builder.py использует для нормализации сырого LLM-вывода (JOIN_TYPE_ALIASES).
CANONICAL_JOIN_TYPES: frozenset = frozenset({
    "LEFT", "RIGHT", "INNER", "FULL", "FULL OUTER", "CROSS", "NATURAL", "JOIN",
})

# Алиасы → канонические формы. «JOIN» без префикса — самостоятельный тип (INNER JOIN по умолчанию).
JOIN_TYPE_ALIASES: dict = {
    "JOIN": "JOIN",
    "INNER": "INNER",
    "INNER JOIN": "INNER",
    "LEFT": "LEFT",
    "LEFT JOIN": "LEFT",
    "LEFT OUTER": "LEFT",
    "LEFT OUTER JOIN": "LEFT",
    "RIGHT": "RIGHT",
    "RIGHT JOIN": "RIGHT",
    "RIGHT OUTER": "RIGHT",
    "RIGHT OUTER JOIN": "RIGHT",
    "FULL": "FULL",
    "FULL JOIN": "FULL",
    "FULL OUTER": "FULL",
    "FULL OUTER JOIN": "FULL",
    "CROSS": "CROSS",
    "CROSS JOIN": "CROSS",
    "NATURAL": "NATURAL",
    "NATURAL JOIN": "NATURAL",
}
