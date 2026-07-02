"""Local Text-to-SQL evaluation helpers."""

from .cases import TextToSQLEvalCase, load_gold_cases, load_jsonl_cases
from .metrics import (
    EvalSummary,
    SchemaLinkingMetrics,
    compute_schema_linking_metrics,
    normalize_schema_links,
    summarize_results,
)
from .observability import eval_result_observability_record, write_eval_observability_jsonl
from .runner import EvalResult, run_sqlite_eval

__all__ = [
    "EvalResult",
    "EvalSummary",
    "SchemaLinkingMetrics",
    "TextToSQLEvalCase",
    "compute_schema_linking_metrics",
    "eval_result_observability_record",
    "load_gold_cases",
    "load_jsonl_cases",
    "normalize_schema_links",
    "run_sqlite_eval",
    "summarize_results",
    "write_eval_observability_jsonl",
]
