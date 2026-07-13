"""Bounded corrective feedback for Text-to-SQL output retries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping, Sequence


CORRECTIVE_FEEDBACK_MAX_MESSAGE_LENGTH = 4096
CORRECTIVE_FEEDBACK_MAX_RECOMMENDATIONS = 8
CORRECTIVE_FEEDBACK_MAX_RECOMMENDATION_LENGTH = 512
CORRECTIVE_FEEDBACK_MAX_SQL_LENGTH = 50_000
_FAILURE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class CorrectiveFeedbackSource(str, Enum):
    VERIFICATION = "VERIFICATION"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True, slots=True)
class CorrectiveFeedback:
    source: CorrectiveFeedbackSource
    failure_code: str
    message: str
    previous_sql: str
    recommendations: tuple[str, ...]
    attempt_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, CorrectiveFeedbackSource):
            raise TypeError("source must be a CorrectiveFeedbackSource")
        if not isinstance(self.failure_code, str) or not _FAILURE_CODE_RE.fullmatch(
            self.failure_code
        ):
            raise ValueError("failure_code must be a canonical uppercase code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if len(self.message) > CORRECTIVE_FEEDBACK_MAX_MESSAGE_LENGTH:
            raise ValueError("message exceeds the corrective feedback limit")
        if not isinstance(self.previous_sql, str) or not self.previous_sql.strip():
            raise ValueError("previous_sql must be a non-empty string")
        if len(self.previous_sql) > CORRECTIVE_FEEDBACK_MAX_SQL_LENGTH:
            raise ValueError("previous_sql exceeds the corrective feedback limit")
        if not isinstance(self.recommendations, tuple):
            raise TypeError("recommendations must be a tuple")
        if len(self.recommendations) > CORRECTIVE_FEEDBACK_MAX_RECOMMENDATIONS:
            raise ValueError("too many corrective recommendations")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > CORRECTIVE_FEEDBACK_MAX_RECOMMENDATION_LENGTH
            for item in self.recommendations
        ):
            raise ValueError("recommendations must contain bounded non-empty strings")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")

    @classmethod
    def create(
        cls,
        *,
        source: CorrectiveFeedbackSource | str,
        failure_code: str,
        message: str,
        previous_sql: str,
        recommendations: Sequence[str],
        attempt_number: int,
    ) -> "CorrectiveFeedback":
        try:
            normalized_source = CorrectiveFeedbackSource(source)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported corrective feedback source") from exc
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if isinstance(recommendations, (str, bytes)) or not isinstance(
            recommendations, Sequence
        ):
            raise TypeError("recommendations must be a sequence of strings")
        normalized_recommendations: list[str] = []
        for item in recommendations[:CORRECTIVE_FEEDBACK_MAX_RECOMMENDATIONS]:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("recommendations must contain non-empty strings")
            normalized_recommendations.append(
                item.strip()[:CORRECTIVE_FEEDBACK_MAX_RECOMMENDATION_LENGTH]
            )
        return cls(
            source=normalized_source,
            failure_code=failure_code,
            message=message.strip()[:CORRECTIVE_FEEDBACK_MAX_MESSAGE_LENGTH],
            previous_sql=previous_sql,
            recommendations=tuple(normalized_recommendations),
            attempt_number=attempt_number,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "failure_code": self.failure_code,
            "message": self.message,
            "previous_sql": self.previous_sql,
            "recommendations": list(self.recommendations),
            "attempt_number": self.attempt_number,
        }


def build_corrective_feedback(
    *,
    step_id: str,
    output: Any,
    previous_sql: str,
    attempt_number: int,
) -> CorrectiveFeedback:
    """Validate one retry-producing output and return bounded feedback."""
    if step_id == "sql_verification":
        if not isinstance(output, Mapping):
            raise TypeError("sql_verification output must be an object")
        if output.get("verification_status") != "Rejected":
            raise ValueError("sql_verification must be exactly Rejected")
        recommendations = output.get("recommendations")
        if not isinstance(recommendations, list):
            raise TypeError("verifier recommendations must be a list")
        return CorrectiveFeedback.create(
            source=CorrectiveFeedbackSource.VERIFICATION,
            failure_code="VERIFIER_REJECTED",
            message="SQL verifier rejected the generated SQL",
            previous_sql=previous_sql,
            recommendations=recommendations,
            attempt_number=attempt_number,
        )

    if step_id == "db_audit":
        if not isinstance(output, Mapping):
            raise TypeError("db_audit output must be an object")
        if output.get("reason_code") != "EXECUTION_FAILED":
            raise ValueError("db_audit feedback requires EXECUTION_FAILED")

        from .models import TextToSqlTerminalResult, TextToSqlTerminalStatus

        terminal = TextToSqlTerminalResult.from_mapping(output)
        if (
            terminal.status is not TextToSqlTerminalStatus.FAILED
            or terminal.reason_code != "EXECUTION_FAILED"
        ):
            raise ValueError("db_audit feedback requires failed EXECUTION_FAILED")
        if terminal.sql != previous_sql:
            raise ValueError("db_audit SQL does not match previous_sql")
        return CorrectiveFeedback.create(
            source=CorrectiveFeedbackSource.EXECUTION,
            failure_code=terminal.reason_code,
            message=terminal.error or "SQL execution failed",
            previous_sql=previous_sql,
            recommendations=(),
            attempt_number=attempt_number,
        )

    raise ValueError(f"unsupported corrective feedback step: {step_id!r}")
