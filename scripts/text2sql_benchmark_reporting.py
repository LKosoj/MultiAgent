"""Closed early-stop and diagnostic helpers for public Text-to-SQL benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence



_POLICY_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "block_size", "min_completed",
        "min_signature_cases",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "candidate_sha256", "decision",
        "reviewed_by", "reviewed_at", "root_hypothesis", "red_test_plan",
        "predicted_improvement", "safety_guarantees",
    }
)
_SEMANTIC_ERROR_CLASSES = frozenset(
    {"ambiguous_requirement", "unsupported_requirement", "missing_evidence"}
)
_SEMANTIC_REQUIREMENTS = frozenset(
    {
        "required_metric",
        "required_dimension",
        "required_filter",
        "required_ordering",
        "required_limit",
        "required_time",
        "required_formula",
    }
)
_SEMANTIC_PIPELINE_COMPONENTS = frozenset(
    {"adaptive_schema_research", "adaptive_sql_solver"}
)
_SEMANTIC_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "terminal_source",
        "root_mechanism",
        "error_class",
        "violated_typed_requirement",
        "pipeline_component",
        "state_sha256",
    }
)
_SEMANTIC_AUTHORITY_ROUTES = {
    ("research", "ambiguous"): (
        "RESEARCH_AMBIGUOUS",
        "ambiguous_requirement",
        "adaptive_schema_research",
    ),
    ("research", "unsupported"): (
        "RESEARCH_UNSUPPORTED",
        "unsupported_requirement",
        "adaptive_schema_research",
    ),
    ("solver", "missing_evidence"): (
        "SCHEMA_CLARIFICATION_REQUIRED",
        "missing_evidence",
        "adaptive_sql_solver",
    ),
}
_EVALUATOR_RECEIPT_V1_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "evaluator_identity",
        "evaluator_input_sha256", "score_sha256", "case_keys",
        "case_manifest_sha256", "run_manifest_sha256",
    }
)
_EVALUATOR_RECEIPT_V2_FIELDS = _EVALUATOR_RECEIPT_V1_FIELDS | frozenset(
    {"execution_evidence_sha256"}
)
_EVALUATOR_IDENTITY_V1_FIELDS = frozenset(
    {"origin", "revision", "entrypoint", "sha256"}
)
_EVALUATOR_IDENTITY_V2_FIELDS = _EVALUATOR_IDENTITY_V1_FIELDS | frozenset(
    {
        "call_surface", "source_closure_sha256", "data_closure_sha256",
        "runtime_identity_sha256",
    }
)


@dataclass(frozen=True)
class EarlyStopPolicy:
    block_size: int
    min_completed: int
    min_signature_cases: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def parse_early_stop_policy(value: object) -> EarlyStopPolicy:
    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise ValueError("early-stop policy has unknown or missing fields")
    if (
        value.get("schema_version") != 2
        or value.get("record_kind") != "text2sql_public_benchmark_early_stop_policy"
    ):
        raise ValueError("early-stop policy identity is invalid")
    policy = EarlyStopPolicy(
        block_size=_positive(value.get("block_size"), "block_size"),
        min_completed=_positive(value.get("min_completed"), "min_completed"),
        min_signature_cases=_positive(value.get("min_signature_cases"), "min_signature_cases"),
    )
    if (
        policy.block_size != 1
        or policy.min_completed != 2
        or policy.min_signature_cases != 2
    ):
        raise ValueError("early-stop policy weakens canonical minimum")
    return policy


def normalized_signature(observation: Mapping[str, object]) -> tuple[str, str, str] | None:
    """Return the dynamic, non-case-specific failure signature."""
    runtime = observation.get("runtime_evidence")
    if not isinstance(runtime, Mapping) or runtime.get("schema_version") != 2:
        return None
    outcome = observation.get("outcome")
    signature = runtime.get("semantic_evidence")
    if not isinstance(signature, Mapping) or set(signature) != {
        "availability", "error_class", "violated_typed_requirement", "pipeline_component"
    }:
        return None
    if signature.get("availability") != "verified":
        return None
    error_class = signature.get("error_class")
    violated_typed_requirement = signature.get("violated_typed_requirement")
    pipeline_component = signature.get("pipeline_component")
    if not all(
        type(item) is str and item
        for item in (
            error_class,
            violated_typed_requirement,
            pipeline_component,
        )
    ):
        return None
    if (
        error_class not in _SEMANTIC_ERROR_CLASSES
        or violated_typed_requirement not in _SEMANTIC_REQUIREMENTS
        or pipeline_component not in _SEMANTIC_PIPELINE_COMPONENTS
    ):
        return None
    authority = runtime.get("semantic_evidence_authority")
    if (
        not isinstance(authority, Mapping)
        or set(authority) != _SEMANTIC_AUTHORITY_FIELDS
    ):
        return None
    if (
        type(authority.get("schema_version")) is not int
        or authority.get("schema_version") != 1
        or authority.get("record_kind") != "text2sql_adaptive_early_stop_evidence"
        or authority.get("error_class") != error_class
        or authority.get("violated_typed_requirement")
        != violated_typed_requirement
        or authority.get("pipeline_component") != pipeline_component
    ):
        return None
    terminal_source = authority.get("terminal_source")
    root_mechanism = authority.get("root_mechanism")
    if type(terminal_source) is not str or type(root_mechanism) is not str:
        return None
    state_sha256 = authority.get("state_sha256")
    if (
        type(state_sha256) is not str
        or len(state_sha256) != 71
        or not state_sha256.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in state_sha256[7:])
    ):
        return None
    route = _SEMANTIC_AUTHORITY_ROUTES.get(
        (terminal_source, root_mechanism)
    )
    if (
        route is None
        or not isinstance(outcome, Mapping)
        or outcome.get("status") != "abstained"
        or (
            outcome.get("reason_code"),
            error_class,
            pipeline_component,
        )
        != route
    ):
        return None
    return (error_class, violated_typed_requirement, pipeline_component)


def normalized_signatures(
    observation: Mapping[str, object],
) -> tuple[tuple[str, str, str], ...]:
    """Return every independently countable closed benchmark signature."""

    runtime = observation.get("runtime_evidence")
    outcome = observation.get("outcome")
    classification = (
        runtime.get("stagnation_classification") if isinstance(runtime, Mapping) else None
    )
    terminal = runtime.get("terminal") if isinstance(runtime, Mapping) else None
    signatures = (
        classification.get("rejection_signatures")
        if isinstance(classification, Mapping)
        else None
    )
    if (
        observation.get("observation_status") == "completed"
        and isinstance(outcome, Mapping)
        and outcome.get("status") == "abstained"
        and outcome.get("reason_code") == "RESEARCH_BUDGET_EXHAUSTED"
        and isinstance(runtime, Mapping)
        and runtime.get("schema_version") == 2
        and isinstance(terminal, Mapping)
        and terminal.get("availability") == "available"
        and terminal.get("reason_code") == outcome.get("reason_code")
    ):
        return (("technical_terminal", "RESEARCH_BUDGET_EXHAUSTED", "adaptive_schema_research"),)
    if (
        observation.get("observation_status") == "completed"
        and isinstance(outcome, Mapping)
        and outcome.get("status") == "abstained"
        and outcome.get("reason_code") == "RESEARCH_STAGNATED"
        and isinstance(runtime, Mapping)
        and runtime.get("schema_version") == 2
        and isinstance(terminal, Mapping)
        and terminal.get("availability") == "available"
        and terminal.get("reason_code") == outcome.get("reason_code")
        and isinstance(classification, Mapping)
        and classification.get("availability") == "verified"
        and isinstance(signatures, list)
        and signatures
        and all(
            isinstance(item, list)
            and len(item) == 2
            and all(type(part) is str and part for part in item)
            for item in signatures
        )
        and signatures
        == [list(item) for item in sorted({tuple(item) for item in signatures})]
    ):
        return tuple(
            ("research_stagnation", path, code)
            for path, code in signatures
        )
    signature = normalized_signature(observation)
    return () if signature is None else (signature,)


def find_early_stop_candidate(
    observations: Sequence[Mapping[str, object]], policy: EarlyStopPolicy
) -> dict[str, object] | None:
    completed = len(observations)
    if completed < policy.min_completed or completed % policy.block_size:
        return None
    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for observation in observations:
        for signature in normalized_signatures(observation):
            grouped.setdefault(signature, []).append(observation)
    for signature, rows in sorted(grouped.items()):
        databases = sorted(
            {str(row.get("database_id")) for row in rows if isinstance(row.get("database_id"), str)}
        )
        count = len(rows)
        if (
            count >= policy.min_signature_cases
        ):
            error_class, violated_typed_requirement, pipeline_component = signature
            signature_payload: dict[str, str]
            if error_class == "technical_terminal":
                signature_payload = {
                    "error_class": error_class,
                    "terminal_reason_code": violated_typed_requirement,
                }
            elif error_class == "research_stagnation":
                signature_payload = {
                    "error_class": error_class,
                    "rejection_path": violated_typed_requirement,
                    "rejection_code": pipeline_component,
                }
            else:
                signature_payload = {
                    "error_class": error_class,
                    "violated_typed_requirement": violated_typed_requirement,
                    "pipeline_component": pipeline_component,
                }
            return {
                "schema_version": 1,
                "record_kind": "text2sql_public_benchmark_early_stop_candidate",
                "completed_case_count": completed,
                "signature": signature_payload,
                "signature_case_count": count,
                "signature_share": f"{count}/{completed}",
                "database_count": len(databases),
                "database_ids": databases,
                "completed_case_keys": [str(row["case_key"]) for row in observations],
            }
    return None


def _write_new(path: Path, write: Callable[[Any], None]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"output must be new: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError(f"output was created by another process: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_json_new(path: Path, payload: Mapping[str, object]) -> None:
    def write(handle: Any) -> None:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
        handle.write("\n")

    _write_new(path, write)


def write_text_new(path: Path, text: str) -> None:
    _write_new(path, lambda handle: handle.write(text))


def ensure_json_new_or_identical(path: Path, payload: Mapping[str, object]) -> None:
    expected = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    ensure_text_new_or_identical(path, expected)


def ensure_text_new_or_identical(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise ValueError(f"output differs from finalized artifact: {path}")
        return
    try:
        write_text_new(path, text)
    except ValueError:
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise


def parse_repair_decision(value: object, *, candidate_sha256: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _DECISION_FIELDS:
        raise ValueError("repair decision has unknown or missing fields")
    if (
        value.get("schema_version") != 1
        or value.get("record_kind") != "text2sql_public_benchmark_repair_decision"
        or value.get("candidate_sha256") != f"sha256:{candidate_sha256}"
        or value.get("decision") not in {"CONTINUE", "STOP_AND_REPAIR"}
    ):
        raise ValueError("repair decision candidate binding is invalid")
    for name in (
        "reviewed_by", "reviewed_at", "root_hypothesis", "red_test_plan", "predicted_improvement"
    ):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError(f"repair decision {name} is invalid")
    guarantees = value.get("safety_guarantees")
    if not isinstance(guarantees, list) or not guarantees or any(
        not isinstance(item, str) or not item.strip() for item in guarantees
    ):
        raise ValueError("repair decision safety_guarantees is invalid")
    return dict(value)


def evaluator_receipt_is_closed(value: Mapping[str, object]) -> bool:
    """Return whether the receipt has the immutable evaluator identity shape.

    Digest and case-list checks require files and intentionally live in the
    reporting CLI; this small predicate keeps case classification oracle-free.
    """
    identity = value.get("evaluator_identity")
    schema_version = value.get("schema_version")
    receipt_fields = {
        1: _EVALUATOR_RECEIPT_V1_FIELDS,
        2: _EVALUATOR_RECEIPT_V2_FIELDS,
    }.get(schema_version)
    identity_fields = {
        1: _EVALUATOR_IDENTITY_V1_FIELDS,
        2: _EVALUATOR_IDENTITY_V2_FIELDS,
    }.get(schema_version)
    return (
        receipt_fields is not None
        and set(value) == receipt_fields
        and value.get("record_kind") == "text2sql_official_evaluator_receipt"
        and isinstance(identity, Mapping)
        and set(identity) == identity_fields
        and all(isinstance(identity.get(name), str) and identity[name] for name in identity)
        and all(
            isinstance(value.get(name), str) and value[name].startswith("sha256:")
            for name in (
                "evaluator_input_sha256", "score_sha256",
                "case_manifest_sha256", "run_manifest_sha256",
                *(("execution_evidence_sha256",) if schema_version == 2 else ()),
            )
        )
        and isinstance(value.get("case_keys"), list)
    )


def failure_class(
    observation: Mapping[str, object], score: object = None,
    evaluator_receipt: Mapping[str, object] | None = None,
) -> str:
    """Classify without guessing an evaluator result that is not present."""
    if observation.get("observation_status") != "completed":
        return "runner_or_transport_error"
    outcome = observation.get("outcome")
    if not isinstance(outcome, Mapping):
        return "evidence_incomplete"
    status = outcome.get("status")
    reason = outcome.get("reason_code")
    if isinstance(reason, str) and reason.startswith(("SCHEMA_", "MISSING_EVIDENCE")):
        return "typed_abstention"
    if status != "succeeded":
        return "pipeline_terminal_failure"
    if evaluator_receipt is None:
        return "evidence_incomplete"
    if not evaluator_receipt_is_closed(evaluator_receipt) or not isinstance(score, int) or isinstance(score, bool):
        return "evaluator_failure"
    if score == 1:
        return "correct"
    if score == 0:
        return "wrong_result"
    return "evaluator_failure"
