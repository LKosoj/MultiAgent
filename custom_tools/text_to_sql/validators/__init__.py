"""
Валидаторы и лимитеры для Text-to-SQL пайплайна.

Фасадный модуль: ре-экспортирует публичные точки входа, чтобы внешние импорты
`from custom_tools.text_to_sql.validators import SQLSafetyValidator` (и т.п.)
продолжали работать после декомпозиции на подмодули.
"""
from .safety import (
    SQLSafetyValidator,
    SQLStaticSafetyValidator,
    SQLLLMAdvisor,
    get_sqlglot_metrics,
    reset_sqlglot_metrics,
    record_sqlglot_metric,
    SQLGLOT_AVAILABLE,
    _SQLGLOT_METRICS,
)
from .safety_config import SafetyConfigMissing
from .schema_limiter import SchemaLimiter
from .schema_aware import SQLSchemaValidator

# `_SQLGLOT_METRICS` остаётся реэкспортированным для read-only-доступа и
# обратной совместимости тестов. Все мутации должны идти через
# `record_sqlglot_metric(...)` — иначе counter'ы теряются при конкурентных
# вызовах (EPIC 2.7).

__all__ = [
    "SQLSafetyValidator",
    "SQLStaticSafetyValidator",  # W9-A10: явное имя static-слоя
    "SQLLLMAdvisor",             # W9-A10: LLM-advisory слой (non-blocking)
    "SafetyConfigMissing",
    "SchemaLimiter",
    "SQLSchemaValidator",
    "get_sqlglot_metrics",
    "reset_sqlglot_metrics",
    "record_sqlglot_metric",
    "SQLGLOT_AVAILABLE",
]
