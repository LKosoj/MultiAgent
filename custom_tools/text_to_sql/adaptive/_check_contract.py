"""Closed deterministic-check result and repair contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from ._model_primitives import (
    Digest,
    Id,
    NonEmptyText,
    StrictModel,
    require_canonical_ids,
)


class CheckKind(StrEnum):
    SAFETY = "safety"
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    EXPLAIN = "explain"
    EXECUTION = "execution"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class CheckFailureCode(StrEnum):
    AST_DIALECT_UNSUPPORTED = "AST_DIALECT_UNSUPPORTED"
    AST_PARSE_TIMEOUT = "AST_PARSE_TIMEOUT"
    AST_PARSE_FAILED = "AST_PARSE_FAILED"
    AST_MULTI_STATEMENT = "AST_MULTI_STATEMENT"
    AST_SHAPE_UNSUPPORTED = "AST_SHAPE_UNSUPPORTED"
    CHECK_INPUT_INVALID = "CHECK_INPUT_INVALID"
    CHECK_TIMEOUT = "CHECK_TIMEOUT"
    CHECK_MALFORMED = "CHECK_MALFORMED"
    MISSING_FILTER = "MISSING_FILTER"
    MISSING_METRIC = "MISSING_METRIC"
    GROUPING_MISMATCH = "GROUPING_MISMATCH"
    ORDERING_MISMATCH = "ORDERING_MISMATCH"
    LIMIT_MISMATCH = "LIMIT_MISMATCH"
    RESULT_SHAPE_MISMATCH = "RESULT_SHAPE_MISMATCH"
    FORMULA_SEMANTICS_MISMATCH = "FORMULA_SEMANTICS_MISMATCH"
    UNAUTHORIZED_TABLE = "UNAUTHORIZED_TABLE"
    UNAUTHORIZED_COLUMN = "UNAUTHORIZED_COLUMN"
    UNAUTHORIZED_LITERAL = "UNAUTHORIZED_LITERAL"
    UNAUTHORIZED_JOIN = "UNAUTHORIZED_JOIN"
    EAV_CATALOG_PREDICATE_MISSING = "EAV_CATALOG_PREDICATE_MISSING"
    EAV_VALUE_PREDICATE_MISSING = "EAV_VALUE_PREDICATE_MISSING"
    EAV_JOIN_MISMATCH = "EAV_JOIN_MISMATCH"
    SAFETY_REJECTED = "SAFETY_REJECTED"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    EXPLAIN_REJECTED = "EXPLAIN_REJECTED"
    EXECUTION_REJECTED = "EXECUTION_REJECTED"


_INCONCLUSIVE_FAILURE_CODES = frozenset(
    {
        CheckFailureCode.CHECK_TIMEOUT,
        CheckFailureCode.CHECK_MALFORMED,
        CheckFailureCode.CHECK_INPUT_INVALID,
    }
)
_ATTRIBUTED_FAILURE_CODES = frozenset(
    {
        CheckFailureCode.MISSING_FILTER,
        CheckFailureCode.MISSING_METRIC,
        CheckFailureCode.GROUPING_MISMATCH,
        CheckFailureCode.ORDERING_MISMATCH,
        CheckFailureCode.LIMIT_MISMATCH,
        CheckFailureCode.RESULT_SHAPE_MISMATCH,
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH,
        CheckFailureCode.UNAUTHORIZED_TABLE,
        CheckFailureCode.UNAUTHORIZED_COLUMN,
        CheckFailureCode.UNAUTHORIZED_LITERAL,
        CheckFailureCode.UNAUTHORIZED_JOIN,
        CheckFailureCode.EAV_CATALOG_PREDICATE_MISSING,
        CheckFailureCode.EAV_VALUE_PREDICATE_MISSING,
        CheckFailureCode.EAV_JOIN_MISMATCH,
    }
)


class RepairKind(StrEnum):
    REVISE_SQL = "REVISE_SQL"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"


class CheckRepair(StrictModel):
    kind: RepairKind
    source_ids: tuple[Id, ...] = ()
    ast_node_ids: tuple[Id, ...] = ()

    @model_validator(mode="after")
    def validate_targets(self) -> CheckRepair:
        for name in ("source_ids", "ast_node_ids"):
            require_canonical_ids(getattr(self, name), f"CheckRepair {name}")
        if self.kind is RepairKind.REQUEST_EVIDENCE and not self.source_ids:
            raise ValueError("REQUEST_EVIDENCE CheckRepair requires source_ids")
        return self


class FormulaSemanticCertificateEntry(StrictModel):
    source_id: Id
    binding_id: Id
    document_id: Id
    document_namespace: NonEmptyText
    evidence_ids: tuple[Id, ...]
    input_column_digests: tuple[Digest, ...]
    ast_node_id: Id

    @model_validator(mode="after")
    def validate_entry(self) -> FormulaSemanticCertificateEntry:
        require_canonical_ids(self.evidence_ids, "formula certificate evidence IDs")
        if not self.input_column_digests:
            raise ValueError("formula certificate requires input columns")
        return self


class FormulaSemanticCertificate(StrictModel):
    candidate_digest: Digest
    entries: tuple[FormulaSemanticCertificateEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> FormulaSemanticCertificate:
        source_ids = tuple(item.source_id for item in self.entries)
        if source_ids != tuple(sorted(set(source_ids))):
            raise ValueError("formula certificate sources must be canonical")
        return self


class CheckResult(StrictModel):
    check_id: Id
    candidate_id: Id
    check_kind: CheckKind
    status: CheckStatus
    failure_code: CheckFailureCode | None
    affected_source_ids: tuple[Id, ...]
    affected_ast_node_ids: tuple[Id, ...]
    observed_error: NonEmptyText | None
    repair: CheckRepair | None = None
    required_change: NonEmptyText | None = None
    formula_certificate: FormulaSemanticCertificate | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> CheckResult:
        for name in ("affected_source_ids", "affected_ast_node_ids"):
            require_canonical_ids(getattr(self, name), f"CheckResult {name}")
        if self.status is CheckStatus.PASSED:
            if (
                any(
                    value is not None
                    for value in (
                        self.failure_code,
                        self.observed_error,
                        self.repair,
                        self.required_change,
                    )
                )
                or self.affected_source_ids
                or self.affected_ast_node_ids
            ):
                raise ValueError("passed CheckResult cannot carry failure details")
            if self.formula_certificate is not None and self.check_kind is not CheckKind.SEMANTIC:
                raise ValueError("formula certificate is only valid for semantic checks")
        elif self.status is CheckStatus.FAILED:
            if self.formula_certificate is not None:
                raise ValueError("failed CheckResult cannot carry formula certificate")
            if self.failure_code is None:
                raise ValueError("failed CheckResult requires failure_code")
            if self.failure_code in _INCONCLUSIVE_FAILURE_CODES:
                raise ValueError(
                    "failed CheckResult cannot use an inconclusive-only failure_code"
                )
            self._validate_repair_representation()
            if (
                self.failure_code in _ATTRIBUTED_FAILURE_CODES
                and not self.affected_source_ids
                and not self.affected_ast_node_ids
            ):
                raise ValueError(
                    "semantic CheckResult failure must be attributed to a source or plan node"
                )
        elif self.status is CheckStatus.INCONCLUSIVE:
            if self.formula_certificate is not None:
                raise ValueError("inconclusive CheckResult cannot carry formula certificate")
            if self.failure_code not in _INCONCLUSIVE_FAILURE_CODES:
                raise ValueError(
                    "inconclusive CheckResult requires an inconclusive failure_code"
                )
            if self.observed_error is None:
                raise ValueError("inconclusive CheckResult requires observed_error")
            self._validate_repair_representation()
        return self

    def _validate_repair_representation(self) -> None:
        if (self.repair is None) == (self.required_change is None):
            raise ValueError("CheckResult requires exactly one repair representation")
        if self.repair is not None and (
            self.affected_source_ids != self.repair.source_ids
            or self.affected_ast_node_ids != self.repair.ast_node_ids
        ):
            raise ValueError("CheckResult affected IDs must match typed repair")


__all__ = [
    "CheckFailureCode",
    "CheckKind",
    "CheckRepair",
    "CheckResult",
    "CheckStatus",
    "FormulaSemanticCertificate",
    "FormulaSemanticCertificateEntry",
    "RepairKind",
]
