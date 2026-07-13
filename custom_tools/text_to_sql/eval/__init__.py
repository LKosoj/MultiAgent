"""Versioned Text-to-SQL evaluation and release-gate helpers."""

from .cases import (
    REQUIRED_RELEASE_SLICES,
    CaseCoverage,
    ReviewRecord,
    TextToSQLEvalCase,
    load_gold_cases,
    load_jsonl_cases,
    validate_case_coverage,
)
from .metrics import (
    EvalSummary,
    ReleaseMetrics,
    SchemaLinkingMetrics,
    compute_release_metrics,
    compute_release_metrics_by_repetition,
    compute_schema_linking_metrics,
    evaluate_release_thresholds,
    normalize_schema_links,
    summarize_results,
)
from .observability import (
    canonical_files_digest,
    eval_result_observability_record,
    write_eval_observability_jsonl,
)
from .release import (
    ApprovedLatencyBaseline,
    CandidateIdentity,
    ReleasePolicy,
    canonical_baseline_payload_digest,
    load_approved_latency_baseline,
)
from .runner import (
    AuthenticatedT13EvalAdapter,
    EvalGenerationRequest,
    EvalObservation,
    EvalResult,
    run_sqlite_eval,
    schema_links_from_sql,
)

__all__ = [
    "AuthenticatedT13EvalAdapter",
    "ApprovedLatencyBaseline",
    "CaseCoverage",
    "CandidateIdentity",
    "EvalGenerationRequest",
    "EvalObservation",
    "EvalResult",
    "EvalSummary",
    "REQUIRED_RELEASE_SLICES",
    "ReleaseMetrics",
    "ReleasePolicy",
    "ReviewRecord",
    "SchemaLinkingMetrics",
    "TextToSQLEvalCase",
    "canonical_files_digest",
    "canonical_baseline_payload_digest",
    "compute_release_metrics",
    "compute_release_metrics_by_repetition",
    "compute_schema_linking_metrics",
    "eval_result_observability_record",
    "evaluate_release_thresholds",
    "load_gold_cases",
    "load_approved_latency_baseline",
    "load_jsonl_cases",
    "normalize_schema_links",
    "run_sqlite_eval",
    "schema_links_from_sql",
    "summarize_results",
    "validate_case_coverage",
    "write_eval_observability_jsonl",
]
