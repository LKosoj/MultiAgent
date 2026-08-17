"""Каноническая сериализация закрытых контрактов adaptive Text-to-SQL."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from hashlib import new as new_hash
import hmac
import json
import re
import sys
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError, field_validator

from .models import (
    ColumnRef,
    ContractModel,
    Digest,
    DocumentRef,
    EvidenceRecord,
    ExpressionRef,
    Id,
    MissingEvidenceRequest,
    NonNegativeInt,
    QueryProbeRef,
    QuerySpec,
    ResearchState,
    SolverState,
    StrictModel,
    TableRef,
)


class AdaptiveSerializationError(ValueError):
    """Базовая ошибка сериализации adaptive-контрактов."""


class CanonicalJsonError(AdaptiveSerializationError):
    """Значение нельзя представить в каноническом JSON."""


class ContractDecodeError(AdaptiveSerializationError):
    """Входной JSON не является допустимым закрытым контрактом."""


class ContractValidationError(ContractDecodeError):
    """JSON не проходит строгую проверку модели."""


class ContractVersionError(ContractDecodeError):
    """Версия контракта отсутствует или имеет неподдерживаемый тип."""


class UnsupportedContractVersionError(ContractVersionError):
    """Версия контракта не относится к явно поддержанным версиям."""


class FutureContractVersionError(UnsupportedContractVersionError):
    """Версия новее текущей и поэтому отклонена без догадок."""


class ContractMigrationError(ContractVersionError):
    """Строго определённый predecessor v0 не совпал с формой v1."""


class StateSizeLimitError(AdaptiveSerializationError):
    """Размер inline-состояния превышает лимит."""


class InlineRowsLimitError(AdaptiveSerializationError):
    """Количество inline-строк превышает лимит."""


class ArtifactReferenceError(AdaptiveSerializationError):
    """Ссылка на внешний артефакт не прошла проверку содержимого."""


MAX_NESTING_DEPTH = 64


@dataclass(frozen=True, slots=True)
class SerializationLimits:
    """Локальные пределы для одного сериализуемого состояния."""

    max_state_bytes: int = 2 * 1024 * 1024
    max_inline_rows: int = 5_000
    max_nesting_depth: int = MAX_NESTING_DEPTH

    def __post_init__(self) -> None:
        if type(self.max_state_bytes) is not int or self.max_state_bytes <= 0:
            raise ValueError("max_state_bytes must be a positive integer")
        if type(self.max_inline_rows) is not int or self.max_inline_rows < 0:
            raise ValueError("max_inline_rows must be a non-negative integer")
        if (
            type(self.max_nesting_depth) is not int
            or not 1 <= self.max_nesting_depth <= MAX_NESTING_DEPTH
        ):
            raise ValueError(
                f"max_nesting_depth must be an integer from 1 to {MAX_NESTING_DEPTH}"
            )


DEFAULT_LIMITS = SerializationLimits()


class ArtifactReference(StrictModel):
    """Immutable-указатель на байты, хранимые вне inline-состояния."""

    artifact_id: Id
    digest: Digest
    byte_count: NonNegativeInt

    @field_validator("digest")
    @classmethod
    def validate_sha256_digest(cls, value: str) -> str:
        if not _is_sha256_digest(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value


ModelType = TypeVar("ModelType", bound=BaseModel)

_CONTRACT_MODELS: Mapping[str, type[ContractModel]] = MappingProxyType(
    {
        "query_spec": QuerySpec,
        "evidence_record": EvidenceRecord,
        "research_state": ResearchState,
        "missing_evidence_request": MissingEvidenceRequest,
        "solver_state": SolverState,
    }
)
_SUPPORTED_DECODE_MODELS = {
    *_CONTRACT_MODELS.values(),
    ArtifactReference,
    TableRef,
    ColumnRef,
    DocumentRef,
    QueryProbeRef,
    ExpressionRef,
}
_INTERNAL_DECODE_MODEL_NAMES = frozenset(
    {
        (
            "custom_tools.text_to_sql.adaptive.research_decision",
            "ResearchDecisionV1",
        ),
        (
            "custom_tools.text_to_sql.adaptive.solver_protocol",
            "SolverProposalV1",
        ),
        *{
            ("custom_tools.text_to_sql.adaptive.model_budget", name)
            for name in (
                "ModelBudgetLimits",
                "ModelBudgetState",
                "ModelTokenUsage",
                "ModelCallReservation",
                "ModelCallStarted",
                "ModelCallResult",
                "ModelCallReconciliation",
                "ModelBudgetLedgerRecord",
            )
        },
    }
)
_INTERNAL_DECODE_MODEL_OBJECTS: dict[tuple[str, str], type[BaseModel]] = {}
_UTC_TIMESTAMP_FIELDS = frozenset({"observed_at", "created_at"})
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def canonical_json_bytes(
    value: Any,
    *,
    limits: SerializationLimits | None = None,
) -> bytes:
    """Вернуть UTF-8 JSON с сортировкой ключей и без незначащих пробелов."""

    depth_limit = (
        limits.max_nesting_depth
        if limits is not None
        else DEFAULT_LIMITS.max_nesting_depth
    )
    _check_nesting_depth(
        value,
        depth_limit,
        CanonicalJsonError,
    )
    normalized = _normalize_for_json(value)
    if limits is not None:
        _check_inline_rows(normalized, limits)
    try:
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalJsonError("value is not canonical JSON") from exc
    if limits is not None and len(encoded) > limits.max_state_bytes:
        raise StateSizeLimitError("canonical state exceeds max_state_bytes")
    return encoded


def canonical_digest(value: Any) -> str:
    """Вернуть SHA-256 digest канонического представления."""

    return f"sha256:{new_hash('sha256', canonical_json_bytes(value)).hexdigest()}"


def serialize_contract(
    contract: ContractModel,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> bytes:
    """Сериализовать один из шести top-level контрактов W1."""

    _require_limits(limits)
    contract_name = getattr(contract, "contract_name", None)
    model_type = _CONTRACT_MODELS.get(contract_name)
    if model_type is None or type(contract) is not model_type:
        raise ContractValidationError("contract is not a registered top-level W1 model")
    return canonical_json_bytes(contract, limits=limits)


def deserialize_contract(
    payload: bytes | str,
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> ContractModel:
    """Декодировать top-level контракт, выбрав модель только по registry."""

    _require_limits(limits)
    mapping = _decode_mapping(payload, limits)
    migrated = migrate_contract_mapping(mapping, limits=limits)
    contract_name = migrated.get("contract_name")
    model_type = _CONTRACT_MODELS.get(contract_name)
    if model_type is None:
        raise ContractDecodeError("unknown contract_name")
    return _validate_model_mapping(migrated, model_type, limits)


def deserialize_as(
    payload: bytes | str,
    model_type: type[ModelType],
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> ModelType:
    """Декодировать JSON как конкретную строгую Pydantic-модель."""

    _require_limits(limits)
    if not isinstance(model_type, type) or not _is_supported_decode_model(model_type):
        raise TypeError("model_type is not a supported immutable W1 decode model")
    mapping = _decode_mapping(payload, limits)
    if issubclass(model_type, ContractModel):
        mapping = migrate_contract_mapping(mapping, limits=limits)
        expected = _CONTRACT_MODELS.get(mapping.get("contract_name"))
        if expected is not model_type:
            raise ContractDecodeError(
                "contract_name does not match the requested model"
            )
    return _validate_model_mapping(mapping, model_type, limits)


def _is_supported_decode_model(model_type: type[BaseModel]) -> bool:
    return any(candidate is model_type for candidate in _SUPPORTED_DECODE_MODELS)


def _register_internal_decode_models(*model_types: type[BaseModel]) -> None:
    validated: dict[tuple[str, str], type[BaseModel]] = {}
    for model_type in model_types:
        if not isinstance(model_type, type) or not issubclass(model_type, BaseModel):
            raise TypeError("internal decode model identity is not allowed")
        identity = (model_type.__module__, model_type.__qualname__)
        if identity not in _INTERNAL_DECODE_MODEL_NAMES:
            raise TypeError("internal decode model identity is not allowed")
        declared_module = sys.modules.get(model_type.__module__)
        if (
            declared_module is None
            or getattr(declared_module, model_type.__qualname__, None) is not model_type
        ):
            raise TypeError("internal decode model identity is not allowed")
        registered = _INTERNAL_DECODE_MODEL_OBJECTS.get(identity)
        if registered is not None and registered is not model_type:
            raise TypeError("internal decode model identity is not allowed")
        pending = validated.get(identity)
        if pending is not None and pending is not model_type:
            raise TypeError("internal decode model identity is not allowed")
        validated[identity] = model_type
    _INTERNAL_DECODE_MODEL_OBJECTS.update(validated)
    _SUPPORTED_DECODE_MODELS.update(validated.values())


def migrate_contract_mapping(
    value: Mapping[str, Any],
    *,
    limits: SerializationLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Перевести единственную допустимую predecessor-форму v0 в v1.

    v0 определён как точная закрытая форма соответствующего v1-контракта,
    отличающаяся только ``contract_version=0``. Входной mapping не меняется.
    """

    _require_limits(limits)
    if not isinstance(value, Mapping):
        raise ContractMigrationError("contract root must be an object")
    copied = dict(value)
    contract_name = copied.get("contract_name")
    model_type = _CONTRACT_MODELS.get(contract_name)
    if model_type is None:
        raise ContractDecodeError("unknown contract_name")
    if "contract_version" not in copied:
        raise ContractVersionError("contract_version is required")
    version = copied["contract_version"]
    if type(version) is not int:
        raise ContractVersionError("contract_version must be an integer")
    if version == 1:
        return _upgrade_additive_v1(copied)
    if version == 0:
        migrated = _upgrade_additive_v1({**copied, "contract_version": 1})
        try:
            _validate_model_mapping(migrated, model_type, limits)
        except ContractValidationError:
            raise ContractMigrationError(
                "v0 must exactly match the closed v1 shape"
            ) from None
        return migrated
    if version > 1:
        raise FutureContractVersionError("future contract_version is not supported")
    raise UnsupportedContractVersionError("contract_version is not supported")


def _upgrade_additive_v1(value: dict[str, Any]) -> dict[str, Any]:
    """Fill only deterministic additive fields absent from early v1 payloads."""

    if value.get("contract_name") != "solver_state":
        return value
    upgraded = dict(value)
    if "research_reentries" not in upgraded:
        upgraded["research_reentries"] = []
    schema_version = upgraded.get("schema_namespace_version")
    raw_query_spec = upgraded.get("query_spec")
    if isinstance(raw_query_spec, Mapping):
        query_spec = dict(raw_query_spec)
        if query_spec.get("schema_namespace_version") is None:
            query_spec["schema_namespace_version"] = schema_version
        upgraded["query_spec"] = query_spec

    if "action_history" not in upgraded:
        empty_fields = (
            "sql_candidates",
            "check_results",
            "execution_results",
            "missing_evidence_requests",
        )
        if (
            all(
                type(upgraded.get(field)) is list and not upgraded[field]
                for field in empty_fields
            )
            and upgraded.get("selected_candidate_id") is None
            and upgraded.get("stop_reason") is None
        ):
            upgraded["action_history"] = []
        else:
            raise ContractMigrationError(
                "nonempty solver_state without action_history cannot be migrated"
            )
    return upgraded


def verify_artifact_reference(
    reference: ArtifactReference,
    read_bytes: Callable[[ArtifactReference], bytes],
) -> bytes:
    """Прочитать артефакт через read-only callback и проверить его digest."""

    if not isinstance(reference, ArtifactReference):
        raise TypeError("reference must be ArtifactReference")
    if not callable(read_bytes):
        raise TypeError("read_bytes must be callable")
    if not _is_sha256_digest(reference.digest):
        raise ArtifactReferenceError(
            "artifact digest must be exact sha256 lowercase hex"
        )
    try:
        content = read_bytes(reference)
    except Exception:
        raise ArtifactReferenceError("artifact reader failed") from None
    if not isinstance(content, bytes):
        raise ArtifactReferenceError("artifact reader must return bytes")
    if len(content) != reference.byte_count:
        raise ArtifactReferenceError("artifact byte_count does not match")
    _, expected = reference.digest.split(":", maxsplit=1)
    actual = new_hash("sha256", content).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ArtifactReferenceError("artifact digest does not match")
    return content


def _decode_mapping(
    payload: bytes | str, limits: SerializationLimits
) -> dict[str, Any]:
    raw = _payload_to_bytes(payload)
    if len(raw) > limits.max_state_bytes:
        raise StateSizeLimitError("input state exceeds max_state_bytes")
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError:
        raise ContractDecodeError("payload exceeds max_nesting_depth") from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ContractDecodeError("payload is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise ContractDecodeError("contract root must be a JSON object")
    _check_nesting_depth(
        decoded,
        limits.max_nesting_depth,
        ContractDecodeError,
    )
    _check_inline_rows(decoded, limits)
    return decoded


def _payload_to_bytes(payload: bytes | str) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        try:
            return payload.encode("utf-8")
        except UnicodeEncodeError:
            raise ContractDecodeError("payload is not UTF-8 encodable") from None
    raise TypeError("payload must be bytes or str")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_model_mapping(
    mapping: Mapping[str, Any],
    model_type: type[ModelType],
    limits: SerializationLimits,
) -> ModelType:
    _validate_utc_timestamp_strings(mapping, limits.max_nesting_depth)
    try:
        validated = model_type.model_validate_json(
            canonical_json_bytes(mapping, limits=limits)
        )
        canonical_json_bytes(validated, limits=limits)
        return validated
    except ValidationError:
        raise ContractValidationError(
            "payload does not satisfy the strict model"
        ) from None
    except CanonicalJsonError:
        raise ContractValidationError(
            "payload cannot be normalized for model validation"
        ) from None


def _validate_utc_timestamp_strings(value: Any, max_nesting_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if isinstance(current, Mapping):
            depth = parent_depth + 1
            if depth > max_nesting_depth:
                raise ContractValidationError("payload exceeds max_nesting_depth")
            for key, item in current.items():
                if key in _UTC_TIMESTAMP_FIELDS:
                    if not isinstance(item, str) or not _is_utc_z_timestamp(item):
                        raise ContractValidationError(
                            "timestamp must use an ISO-8601 UTC Z representation"
                        )
                stack.append((item, depth))
        elif isinstance(current, list | tuple):
            depth = parent_depth + 1
            if depth > max_nesting_depth:
                raise ContractValidationError("payload exceeds max_nesting_depth")
            stack.extend((item, depth) for item in current)


def _is_utc_z_timestamp(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return "T" in value


def _is_sha256_digest(value: str) -> bool:
    return bool(_SHA256_DIGEST_RE.fullmatch(value))


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_for_json(
            value.model_dump(
                mode="python", by_alias=True, exclude_none=False, round_trip=True
            )
        )
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise CanonicalJsonError("datetime must be UTC")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, Enum):
        return _normalize_for_json(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError("JSON object keys must be strings")
            result[key] = _normalize_for_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise CanonicalJsonError(f"unsupported JSON value type: {type(value).__name__}")


def _check_nesting_depth(
    value: Any,
    max_nesting_depth: int,
    error_type: type[AdaptiveSerializationError],
) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if isinstance(current, BaseModel):
            current = current.model_dump(
                mode="python",
                by_alias=True,
                exclude_none=False,
                round_trip=True,
            )
        if isinstance(current, Mapping):
            depth = parent_depth + 1
            if depth > max_nesting_depth:
                raise error_type("payload exceeds max_nesting_depth")
            stack.extend((item, depth) for item in current.values())
        elif isinstance(current, list | tuple):
            depth = parent_depth + 1
            if depth > max_nesting_depth:
                raise error_type("payload exceeds max_nesting_depth")
            stack.extend((item, depth) for item in current)


def _check_inline_rows(value: Any, limits: SerializationLimits) -> None:
    _check_nesting_depth(
        value,
        limits.max_nesting_depth,
        CanonicalJsonError,
    )
    row_count = _count_inline_rows(value)
    if row_count > limits.max_inline_rows:
        raise InlineRowsLimitError("inline rows exceed max_inline_rows")


def _count_inline_rows(value: Any) -> int:
    total = 0
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if key == "rows" and isinstance(item, list):
                    total += len(item)
                stack.append(item)
        elif isinstance(current, list | tuple):
            stack.extend(current)
    return total


def _require_limits(limits: SerializationLimits) -> None:
    if not isinstance(limits, SerializationLimits):
        raise TypeError("limits must be SerializationLimits")
