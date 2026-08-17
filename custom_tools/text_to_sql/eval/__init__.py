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
_OBSERVABILITY_EXPORTS = frozenset(
    {
        "AdaptiveReplayObservabilityRecord",
        "HistoricalReplayObservationCategory",
        "HistoricalReplayReasonCode",
        "PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION",
        "ReplayReuseReasonCode",
        "adaptive_replay_observability_record",
        "canonical_files_digest",
        "eval_result_observability_record",
        "pre_execution_gate_coverage_record",
        "write_adaptive_replay_observability_jsonl",
        "write_eval_observability_jsonl",
        "write_pre_execution_gate_coverage_jsonl",
    }
)
_RELEASE_EXPORTS = frozenset(
    {
        "ApprovedLatencyBaseline",
        "CandidateIdentity",
        "ReleasePolicy",
        "canonical_baseline_payload_digest",
        "load_approved_latency_baseline",
    }
)
_RUNNER_EXPORTS = frozenset(
    {
        "AuthenticatedT13EvalAdapter",
        "EvalGenerationRequest",
        "EvalObservation",
        "EvalResult",
        "run_sqlite_eval",
        "schema_links_from_sql",
    }
)

__all__ = [
    "AdaptiveReplayObservabilityRecord",
    "AuthenticatedT13EvalAdapter",
    "ApprovedLatencyBaseline",
    "CaseCoverage",
    "CandidateIdentity",
    "EvalGenerationRequest",
    "EvalObservation",
    "EvalResult",
    "EvalSummary",
    "HistoricalReplayObservationCategory",
    "HistoricalReplayReasonCode",
    "PRE_EXECUTION_GATE_COVERAGE_SCHEMA_VERSION",
    "REQUIRED_RELEASE_SLICES",
    "ReleaseMetrics",
    "ReleasePolicy",
    "ReplayReuseReasonCode",
    "ReviewRecord",
    "SchemaLinkingMetrics",
    "TextToSQLEvalCase",
    "canonical_files_digest",
    "canonical_baseline_payload_digest",
    "adaptive_replay_observability_record",
    "compute_release_metrics",
    "compute_release_metrics_by_repetition",
    "compute_schema_linking_metrics",
    "eval_result_observability_record",
    "evaluate_release_thresholds",
    "load_gold_cases",
    "load_approved_latency_baseline",
    "load_jsonl_cases",
    "normalize_schema_links",
    "pre_execution_gate_coverage_record",
    "run_sqlite_eval",
    "schema_links_from_sql",
    "summarize_results",
    "validate_case_coverage",
    "write_eval_observability_jsonl",
    "write_adaptive_replay_observability_jsonl",
    "write_pre_execution_gate_coverage_jsonl",
]


def __getattr__(name: str):
    if name in _OBSERVABILITY_EXPORTS:
        from . import observability

        return getattr(observability, name)
    if name in _RELEASE_EXPORTS:
        from . import release

        return getattr(release, name)
    if name in _RUNNER_EXPORTS:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(name)
