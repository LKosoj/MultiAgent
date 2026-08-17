"""Independent exact validation for trusted adaptive contract boundaries."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import math
from types import UnionType
from typing import Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from .models import StrictModel
from .serialization import canonical_json_bytes


class ExactContractError(ValueError):
    """A trusted value is not an exact canonical contract."""


_ModelT = TypeVar("_ModelT", bound=StrictModel)
_DataclassT = TypeVar("_DataclassT")
_EnumDeclaration = tuple[Enum, str, object, str | None]
_ENUM_DECLARATIONS: dict[type[Enum], tuple[_EnumDeclaration, ...]] = {}


def revalidate_exact_model(
    value: object,
    model_type: type[_ModelT],
    error_type: type[Exception],
    label: str,
) -> _ModelT:
    """Round-trip one exact Pydantic model and reject internal forgeries."""

    try:
        return _revalidate_model(value, model_type)
    except (ValidationError, TypeError, ValueError) as exc:
        raise error_type(f"{label} is not an exact canonical contract") from exc


def require_exact_dataclass(
    value: object,
    model_type: type[_DataclassT],
    error_type: type[Exception],
    label: str,
) -> _DataclassT:
    """Recursively validate an exact typed dataclass authority tree."""

    try:
        _require_typed_value(value, model_type, set())
    except (ValidationError, TypeError, ValueError) as exc:
        raise error_type(f"{label} is not an exact contract tree") from exc
    return value


def exact_value(value: object, canonical: object) -> bool:
    """Compare values recursively without scalar-subclass equality shortcuts."""

    if type(value) is not type(canonical):
        return False
    if isinstance(canonical, Enum):
        return _canonical_enum_member(value, type(canonical)) is canonical
    if isinstance(canonical, BaseModel):
        if not _has_exact_model_fields(value) or not _has_exact_model_fields(canonical):
            return False
        return all(
            exact_value(getattr(value, name), getattr(canonical, name))
            for name in type(canonical).model_fields
        )
    if is_dataclass(canonical) and not isinstance(canonical, type):
        return all(
            exact_value(getattr(value, field.name), getattr(canonical, field.name))
            for field in fields(canonical)
        )
    if isinstance(canonical, tuple | list):
        return len(value) == len(canonical) and all(
            exact_value(item, expected)
            for item, expected in zip(value, canonical, strict=True)
        )
    if isinstance(canonical, dict):
        if len(value) != len(canonical):
            return False
        unmatched = list(canonical.items())
        for key, item in value.items():
            match = next(
                (
                    index
                    for index, (expected_key, expected_item) in enumerate(unmatched)
                    if exact_value(key, expected_key)
                    and exact_value(item, expected_item)
                ),
                None,
            )
            if match is None:
                return False
            unmatched.pop(match)
        return not unmatched
    if isinstance(canonical, frozenset | set):
        if len(value) != len(canonical):
            return False
        unmatched = list(canonical)
        for item in value:
            match = next(
                (
                    index
                    for index, expected in enumerate(unmatched)
                    if exact_value(item, expected)
                ),
                None,
            )
            if match is None:
                return False
            unmatched.pop(match)
        return not unmatched
    return value == canonical


def _canonical_enum_member(value: object, enum_type: type[Enum]) -> Enum | None:
    if type(value) is not enum_type:
        return None
    declaration = next(
        (item for item in _enum_declarations(enum_type) if item[0] is value),
        None,
    )
    if declaration is None:
        return None
    canonical, declared_name, declared_value, declared_payload = declaration
    try:
        current_name = object.__getattribute__(value, "_name_")
        current_value = object.__getattribute__(value, "_value_")
    except AttributeError:
        return None
    if (
        type(current_name) is not str
        or current_name != declared_name
        or not _exact_enum_scalar(current_value, declared_value)
    ):
        return None
    if declared_payload is not None:
        current_payload = str.__str__(value)
        if not _exact_enum_scalar(current_payload, declared_payload):
            return None
    return canonical


def _enum_declarations(enum_type: type[Enum]) -> tuple[_EnumDeclaration, ...]:
    cached = _ENUM_DECLARATIONS.get(enum_type)
    if cached is not None:
        return cached
    declarations: list[_EnumDeclaration] = []
    for declared_name, member in enum_type.__members__.items():
        if any(item[0] is member for item in declarations):
            continue
        declared_values = tuple(
            declared_value
            for declared_value, declared_member in enum_type._value2member_map_.items()
            if declared_member is member
        )
        if len(declared_values) != 1:
            return ()
        payload = str.__str__(member) if issubclass(enum_type, str) else None
        declarations.append((member, declared_name, declared_values[0], payload))
    cached = tuple(declarations)
    _ENUM_DECLARATIONS[enum_type] = cached
    return cached


def _exact_enum_scalar(value: object, declared: object) -> bool:
    if type(value) is not type(declared):
        return False
    if value is None or type(value) in {str, int, bool}:
        return value == declared
    if type(value) is float:
        return math.isfinite(value) and math.isfinite(declared) and value == declared
    return value is declared


def _literal_matches(value: object, declared: object) -> bool:
    if isinstance(declared, Enum):
        return _canonical_enum_member(value, type(declared)) is declared
    return _exact_enum_scalar(value, declared)


def _revalidate_model(value: object, model_type: type[_ModelT]) -> _ModelT:
    if type(value) is not model_type:
        raise TypeError(f"value must be an exact {model_type.__name__}")
    checked = model_type.model_validate_json(
        canonical_json_bytes(_declared_payload(value)),
        strict=True,
    )
    if not exact_value(value, checked):
        raise ValueError("value is not exactly canonical")
    return checked


def _declared_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        if not _has_exact_model_fields(value):
            raise ValueError("model internals must contain exact declared fields")
        return {
            name: _declared_payload(getattr(value, name))
            for name in type(value).model_fields
        }
    if isinstance(value, tuple):
        return tuple(_declared_payload(item) for item in value)
    if isinstance(value, list):
        return [_declared_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            _declared_payload(key): _declared_payload(item)
            for key, item in value.items()
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _declared_payload(getattr(value, field.name))
            for field in fields(value)
        }
    return value


def _has_exact_model_fields(value: BaseModel) -> bool:
    model_dict = value.__dict__
    return (
        type(model_dict) is dict
        and set(dict.keys(model_dict)) == set(type(value).model_fields)
        and value.__pydantic_extra__ is None
        and value.__pydantic_private__ is None
    )


def _require_typed_value(
    value: object,
    expected_type: object,
    active: set[int],
) -> None:
    origin = get_origin(expected_type)
    arguments = get_args(expected_type)
    if origin in {Union, UnionType}:
        if not any(_matches_typed_value(value, option, active) for option in arguments):
            raise TypeError("value does not match its declared union")
        return
    if origin is Literal:
        if not any(_literal_matches(value, option) for option in arguments):
            raise TypeError("value does not match its declared literal")
        return
    if origin is tuple:
        if type(value) is not tuple:
            raise TypeError("value must be an exact tuple")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            for item in value:
                _require_typed_value(item, arguments[0], active)
            return
        if len(value) != len(arguments):
            raise ValueError("tuple length does not match its declaration")
        for item, item_type in zip(value, arguments, strict=True):
            _require_typed_value(item, item_type, active)
        return
    if origin is list:
        if type(value) is not list:
            raise TypeError("value must be an exact list")
        for item in value:
            _require_typed_value(item, arguments[0], active)
        return
    if origin is dict:
        if type(value) is not dict:
            raise TypeError("value must be an exact dict")
        for key, item in value.items():
            _require_typed_value(key, arguments[0], active)
            _require_typed_value(item, arguments[1], active)
        return
    if expected_type is type(None):
        if value is not None:
            raise TypeError("value must be None")
        return
    if isinstance(expected_type, type) and issubclass(expected_type, BaseModel):
        _revalidate_model(value, expected_type)
        return
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        _require_dataclass_value(value, expected_type, active)
        return
    if isinstance(expected_type, type) and issubclass(expected_type, Enum):
        if _canonical_enum_member(value, expected_type) is not value:
            raise TypeError("enum value is not its canonical member")
        return
    if expected_type in {str, int, float, bool}:
        if type(value) is not expected_type:
            raise TypeError("scalar value has the wrong exact type")
        if expected_type is float and not math.isfinite(value):
            raise ValueError("float value must be finite")
        return
    raise TypeError("unsupported exact contract annotation")


def _matches_typed_value(
    value: object,
    expected_type: object,
    active: set[int],
) -> bool:
    try:
        _require_typed_value(value, expected_type, active)
    except (ValidationError, TypeError, ValueError):
        return False
    return True


def _require_dataclass_value(
    value: object,
    model_type: type,
    active: set[int],
) -> None:
    if type(value) is not model_type:
        raise TypeError(f"value must be an exact {model_type.__name__}")
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic dataclass authority is not allowed")
    active.add(identity)
    try:
        declared_fields = tuple(field.name for field in fields(model_type))
        annotations = get_type_hints(model_type)
        if set(annotations) != set(declared_fields):
            raise TypeError("dataclass fields do not match annotations")
        model_dict = getattr(value, "__dict__", None)
        if model_dict is not None and (
            type(model_dict) is not dict
            or set(dict.keys(model_dict)) != set(declared_fields)
        ):
            raise ValueError("dataclass contains undeclared fields")
        for name in declared_fields:
            _require_typed_value(getattr(value, name), annotations[name], active)
    finally:
        active.remove(identity)
