"""Authorized, fail-closed Text-to-SQL connection references."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import socket
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

from .auth import Principal


_CONNECTION_REF_RE = re.compile(
    r"conn-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_FILE_SCHEMES = frozenset({"sqlite", "duckdb"})
_POSTGRES_SCHEMES = frozenset({"pg", "postgres", "postgresql", "psql"})
_POSTGRES_ROUTING_QUERY_KEYS = frozenset(
    {"host", "hostaddr", "port", "service", "servicefile"}
)
_PLUGIN_SCHEMES = frozenset(
    {
        "duckdb",
        "impala",
        "mysql",
        "pg",
        "postgres",
        "postgresql",
        "psql",
        "sapiq",
        "sqlite",
    }
)
_LEGACY_PREFIX = "db_config:"


class ConnectionTargetKind(str, Enum):
    NETWORK = "network"
    FILE = "file"


class ConnectionTargetReasonCode(str, Enum):
    ALLOWED = "allowed"
    EMPTY_DSN = "empty_dsn"
    MALFORMED_DSN = "malformed_dsn"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    MISSING_HOST = "missing_host"
    MISSING_PORT = "missing_port"
    NETWORK_TARGET_DENIED = "network_target_denied"
    ROUTING_PARAMETER_DENIED = "routing_parameter_denied"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    FILE_MEMORY_DENIED = "file_memory_denied"
    FILE_PATH_NOT_ABSOLUTE = "file_path_not_absolute"
    FILE_PATH_TRAVERSAL = "file_path_traversal"
    FILE_PATH_UNRESOLVED = "file_path_unresolved"
    FILE_ROOT_DENIED = "file_root_denied"


@dataclass(frozen=True)
class ConnectionTargetValidation:
    allowed: bool
    reason: ConnectionTargetReasonCode
    kind: ConnectionTargetKind | None = None
    scheme: str | None = None
    canonical_target: str | None = None


class ConnectionTargetPolicyError(ValueError):
    def __init__(self, validation: ConnectionTargetValidation) -> None:
        self.validation = validation
        self.reason = validation.reason
        super().__init__(f"connection target denied: {validation.reason.value}")


class ConnectionAccessError(PermissionError):
    pass


class ConnectionDisabledError(PermissionError):
    pass


class ConnectionNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ConnectionRef:
    value: str

    def __post_init__(self) -> None:
        if _CONNECTION_REF_RE.fullmatch(self.value) is None:
            raise ValueError("invalid connection reference")

    @classmethod
    def new(cls, uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> ConnectionRef:
        return cls(f"conn-{uuid_factory()}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ConnectionRecord:
    connection_ref: ConnectionRef
    display_name: str
    owner_subject: str | None
    tenant_id: str
    target_kind: ConnectionTargetKind
    dialect: str
    target_description: str
    created_at: datetime
    enabled_for_user: bool = True

    def to_public_dict(self) -> dict[str, object]:
        return {
            "connection_ref": str(self.connection_ref),
            "display_name": self.display_name,
            "owner_subject": self.owner_subject,
            "tenant_id": self.tenant_id,
            "target_kind": self.target_kind.value,
            "dialect": self.dialect,
            "target_description": self.target_description,
            "created_at": self.created_at.isoformat(),
            "enabled_for_user": self.enabled_for_user,
        }


@dataclass(frozen=True)
class _CidrRule:
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    port: int


class ConnectionTargetPolicy:
    """Parse and authorize DSN targets without opening a database connection."""

    def __init__(
        self,
        *,
        allowed_schemes: Iterable[str] = (),
        allowed_network_targets: Iterable[str] = (),
        allowed_network_cidrs: Iterable[str] = (),
        allowed_file_roots: Iterable[str | os.PathLike[str]] = (),
        dns_resolver: Callable[[str], Iterable[str]] | None = None,
        path_resolver: Callable[[Path], Path] | None = None,
    ) -> None:
        schemes = frozenset(str(item).strip().lower() for item in allowed_schemes)
        schemes = frozenset(item for item in schemes if item)
        unsupported = sorted(schemes - _PLUGIN_SCHEMES)
        if unsupported:
            raise ValueError(f"unsupported database scheme: {unsupported[0]}")

        self.allowed_schemes = schemes
        self.allowed_network_targets = frozenset(
            _parse_host_port(item) for item in allowed_network_targets if str(item).strip()
        )
        self.allowed_network_cidrs = tuple(
            _parse_cidr_rule(item) for item in allowed_network_cidrs if str(item).strip()
        )
        self._dns_resolver = dns_resolver or _default_dns_resolver
        self._path_resolver = path_resolver or _default_path_resolver
        self.allowed_file_roots = self._resolve_file_roots(allowed_file_roots)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dns_resolver: Callable[[str], Iterable[str]] | None = None,
        path_resolver: Callable[[Path], Path] | None = None,
    ) -> ConnectionTargetPolicy:
        values = os.environ if environ is None else environ
        return cls(
            allowed_schemes=_split_setting(values.get("TEXT_TO_SQL_ALLOWED_DB_SCHEMES")),
            allowed_network_targets=_split_setting(
                values.get("TEXT_TO_SQL_ALLOWED_DB_TARGETS")
            ),
            allowed_network_cidrs=_split_setting(
                values.get("TEXT_TO_SQL_ALLOWED_DB_CIDRS")
            ),
            allowed_file_roots=_split_setting(
                values.get("TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS")
            ),
            dns_resolver=dns_resolver,
            path_resolver=path_resolver,
        )

    def validate(self, dsn: str) -> ConnectionTargetValidation:
        if not isinstance(dsn, str) or not dsn:
            return _denied(ConnectionTargetReasonCode.EMPTY_DSN)
        if dsn != dsn.strip() or any(
            char in dsn for char in ("\x00", "\r", "\n", "\t", "\\")
        ):
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN)
        try:
            parsed = urlsplit(dsn)
        except (TypeError, ValueError):
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN)
        if not parsed.scheme or parsed.fragment:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN)

        scheme = parsed.scheme.lower()
        if scheme not in self.allowed_schemes:
            return _denied(ConnectionTargetReasonCode.SCHEME_NOT_ALLOWED, scheme=scheme)
        if scheme in _FILE_SCHEMES:
            return self._validate_file_target(parsed, scheme)
        return self._validate_network_target(parsed, scheme)

    def require_allowed(self, dsn: str) -> ConnectionTargetValidation:
        validation = self.validate(dsn)
        if not validation.allowed:
            raise ConnectionTargetPolicyError(validation)
        return validation

    def _resolve_file_roots(
        self,
        roots: Iterable[str | os.PathLike[str]],
    ) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for raw_root in roots:
            root = Path(raw_root)
            if not root.is_absolute():
                raise ValueError("allowed database file root must be absolute")
            try:
                canonical = Path(self._path_resolver(root))
            except (OSError, RuntimeError) as exc:
                raise ValueError("allowed database file root cannot be resolved") from exc
            if not canonical.is_absolute():
                raise ValueError("allowed database file root must resolve absolutely")
            if canonical not in resolved:
                resolved.append(canonical)
        return tuple(resolved)

    def _validate_network_target(self, parsed, scheme: str) -> ConnectionTargetValidation:
        if scheme in _POSTGRES_SCHEMES:
            try:
                query_keys = {
                    key.casefold()
                    for key, _value in parse_qsl(
                        parsed.query,
                        keep_blank_values=True,
                        max_num_fields=128,
                    )
                }
            except ValueError:
                return _denied(
                    ConnectionTargetReasonCode.MALFORMED_DSN,
                    kind=ConnectionTargetKind.NETWORK,
                    scheme=scheme,
                )
            if query_keys & _POSTGRES_ROUTING_QUERY_KEYS:
                return _denied(
                    ConnectionTargetReasonCode.ROUTING_PARAMETER_DENIED,
                    kind=ConnectionTargetKind.NETWORK,
                    scheme=scheme,
                )
        try:
            host = parsed.hostname
        except ValueError:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        if not host:
            return _denied(ConnectionTargetReasonCode.MISSING_HOST, scheme=scheme)
        try:
            port = parsed.port
        except ValueError:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        if port is None:
            return _denied(ConnectionTargetReasonCode.MISSING_PORT, scheme=scheme)
        if port == 0:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        try:
            normalized_host = _normalize_host(host)
        except ValueError:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)

        target = _format_host_port(normalized_host, port)
        if target in self.allowed_network_targets:
            return _allowed(ConnectionTargetKind.NETWORK, scheme, target)

        cidr_rules = tuple(rule for rule in self.allowed_network_cidrs if rule.port == port)
        if not cidr_rules:
            return _denied(
                ConnectionTargetReasonCode.NETWORK_TARGET_DENIED,
                kind=ConnectionTargetKind.NETWORK,
                scheme=scheme,
            )

        try:
            direct_address = ipaddress.ip_address(normalized_host)
        except ValueError:
            try:
                raw_addresses = tuple(self._dns_resolver(normalized_host))
                addresses = tuple(ipaddress.ip_address(item) for item in raw_addresses)
            except (OSError, RuntimeError, TypeError, ValueError):
                return _denied(
                    ConnectionTargetReasonCode.DNS_RESOLUTION_FAILED,
                    kind=ConnectionTargetKind.NETWORK,
                    scheme=scheme,
                )
            if not addresses:
                return _denied(
                    ConnectionTargetReasonCode.DNS_RESOLUTION_FAILED,
                    kind=ConnectionTargetKind.NETWORK,
                    scheme=scheme,
                )
        else:
            addresses = (direct_address,)

        if not all(_address_matches(address, cidr_rules) for address in addresses):
            return _denied(
                ConnectionTargetReasonCode.NETWORK_TARGET_DENIED,
                kind=ConnectionTargetKind.NETWORK,
                scheme=scheme,
            )
        return _allowed(ConnectionTargetKind.NETWORK, scheme, target)

    def _validate_file_target(self, parsed, scheme: str) -> ConnectionTargetValidation:
        if parsed.netloc:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        try:
            path_text = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        if "\x00" in path_text:
            return _denied(ConnectionTargetReasonCode.MALFORMED_DSN, scheme=scheme)
        if path_text in {":memory:", "/:memory:"}:
            return _denied(
                ConnectionTargetReasonCode.FILE_MEMORY_DENIED,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )

        path = Path(path_text)
        if not path.is_absolute():
            return _denied(
                ConnectionTargetReasonCode.FILE_PATH_NOT_ABSOLUTE,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        if ".." in path.parts:
            return _denied(
                ConnectionTargetReasonCode.FILE_PATH_TRAVERSAL,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        if not self.allowed_file_roots:
            return _denied(
                ConnectionTargetReasonCode.FILE_ROOT_DENIED,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        try:
            canonical = Path(self._path_resolver(path))
        except (OSError, RuntimeError, ValueError):
            return _denied(
                ConnectionTargetReasonCode.FILE_PATH_UNRESOLVED,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        if not canonical.is_absolute():
            return _denied(
                ConnectionTargetReasonCode.FILE_PATH_UNRESOLVED,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        if not any(
            canonical == root or canonical.is_relative_to(root)
            for root in self.allowed_file_roots
        ):
            return _denied(
                ConnectionTargetReasonCode.FILE_ROOT_DENIED,
                kind=ConnectionTargetKind.FILE,
                scheme=scheme,
            )
        return _allowed(ConnectionTargetKind.FILE, scheme, str(canonical))


class ConnectionRegistry:
    """Keep public connection metadata separate from server-side DSN secrets."""

    def __init__(
        self,
        policy: ConnectionTargetPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], uuid.UUID] | None = None,
        legacy_resolver: Callable[[str, Principal], str] | None = None,
    ) -> None:
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._uuid_factory = uuid_factory or uuid.uuid4
        self._legacy_resolver = legacy_resolver
        self._records: dict[ConnectionRef, ConnectionRecord] = {}
        self._secrets: dict[ConnectionRef, str] = {}
        self._lock = threading.RLock()

    def list_for(self, principal: Principal) -> tuple[ConnectionRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if _record_visible_to(record, principal)
            )

    def register(
        self,
        principal: Principal,
        *,
        display_name: str,
        dsn: str,
        owner_subject: str | None,
        tenant_id: str,
        enabled_for_user: bool = True,
    ) -> ConnectionRecord:
        _require_admin(principal, "register connections")
        name = _required_metadata(display_name, "display_name")
        tenant = _required_metadata(tenant_id, "tenant_id")
        if owner_subject is not None:
            owner = _required_metadata(owner_subject, "owner_subject")
        else:
            owner = None
        validation = self._policy.require_allowed(dsn)
        assert validation.kind is not None
        assert validation.scheme is not None
        assert validation.canonical_target is not None

        with self._lock:
            connection_ref = self._new_ref()
            record = ConnectionRecord(
                connection_ref=connection_ref,
                display_name=name,
                owner_subject=owner,
                tenant_id=tenant,
                target_kind=validation.kind,
                dialect=validation.scheme,
                target_description=_target_description(validation),
                created_at=self._clock(),
                enabled_for_user=enabled_for_user,
            )
            self._records[connection_ref] = record
            self._secrets[connection_ref] = dsn
            return record

    def resolve(
        self,
        connection_ref: ConnectionRef | str,
        principal: Principal,
    ) -> str:
        _record, dsn = self.resolve_with_record(connection_ref, principal)
        return dsn

    def resolve_with_record(
        self,
        connection_ref: ConnectionRef | str,
        principal: Principal,
    ) -> tuple[ConnectionRecord, str]:
        reference = _coerce_ref(connection_ref)
        with self._lock:
            record = self._records.get(reference)
            if record is None:
                raise ConnectionNotFoundError("connection reference not found")
            if not _record_in_principal_scope(record, principal):
                raise ConnectionAccessError("connection reference is not accessible")
            if not principal.has_role("admin") and not record.enabled_for_user:
                raise ConnectionDisabledError("connection reference is disabled")
            dsn = self._secrets.get(reference)
            if dsn is None:
                raise ConnectionNotFoundError("connection secret is unavailable")

        self._policy.require_allowed(dsn)
        return record, dsn

    def restore(self, record: ConnectionRecord, dsn: str) -> ConnectionRecord:
        if not isinstance(record, ConnectionRecord):
            raise TypeError("record must be a ConnectionRecord")
        validation = self._policy.require_allowed(dsn)
        _validate_restored_record(record, validation)

        with self._lock:
            if record.connection_ref in self._records:
                raise ValueError("connection reference is already registered")
            self._records[record.connection_ref] = record
            self._secrets[record.connection_ref] = dsn
        return record

    def delete(
        self,
        connection_ref: ConnectionRef | str,
        principal: Principal,
    ) -> ConnectionRecord:
        _require_admin(principal, "delete connections")
        reference = _coerce_ref(connection_ref)
        with self._lock:
            record = self._records.pop(reference, None)
            if record is None:
                raise ConnectionNotFoundError("connection reference not found")
            self._secrets.pop(reference, None)
            return record

    def resolve_legacy(self, reference: str, principal: Principal) -> str:
        _require_admin(principal, "resolve legacy connection references")
        if not isinstance(reference, str) or not reference.startswith(_LEGACY_PREFIX):
            raise ValueError("legacy connection reference must start with db_config:")
        if not reference.removeprefix(_LEGACY_PREFIX):
            raise ValueError("legacy db_config reference requires a name")
        if self._legacy_resolver is None:
            raise ValueError("legacy db_config resolver is not configured")
        dsn = self._legacy_resolver(reference, principal)
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("legacy db_config secret is unavailable")
        self._policy.require_allowed(dsn)
        return dsn

    def _new_ref(self) -> ConnectionRef:
        for _ in range(8):
            candidate = ConnectionRef.new(self._uuid_factory)
            if candidate not in self._records:
                return candidate
        raise RuntimeError("could not generate a unique connection reference")


def _split_setting(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def canonical_connection_target_policy(
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the deterministic release identity of the four target settings."""
    values = os.environ if environ is None else environ
    raw_schemes = _split_setting(values.get("TEXT_TO_SQL_ALLOWED_DB_SCHEMES"))
    ConnectionTargetPolicy(allowed_schemes=raw_schemes)
    dialects = sorted(
        {
            "postgres" if scheme.lower() in _POSTGRES_SCHEMES else scheme.lower()
            for scheme in raw_schemes
        }
    )
    targets = sorted(
        {_parse_host_port(value) for value in _split_setting(
            values.get("TEXT_TO_SQL_ALLOWED_DB_TARGETS")
        )}
    )
    cidrs = []
    for value in _split_setting(values.get("TEXT_TO_SQL_ALLOWED_DB_CIDRS")):
        rule = _parse_cidr_rule(value)
        network = rule.network.with_prefixlen
        if rule.network.version == 6:
            network = f"[{network}]"
        cidrs.append(f"{network}:{rule.port}")
    roots = []
    for value in _split_setting(values.get("TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS")):
        root = Path(os.path.normpath(value))
        if not root.is_absolute():
            raise ValueError("allowed database file root must be absolute")
        roots.append(root.as_posix())
    return {
        "schema_version": 1,
        "allowed_dialects": dialects,
        "allowed_network_targets": sorted(set(targets)),
        "allowed_network_cidrs": sorted(set(cidrs)),
        "allowed_file_roots": sorted(set(roots)),
    }


def write_connection_target_policy_artifact(
    path: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Write the canonical target-policy artifact used by release gates."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(connection_target_policy_artifact_bytes(environ))


def connection_target_policy_artifact_bytes(
    environ: Mapping[str, str] | None = None,
) -> bytes:
    """Serialize target-policy identity exactly as release artifacts do."""
    return (
        json.dumps(
            canonical_connection_target_policy(environ),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def connection_target_policy_sha256(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Hash the exact canonical target-policy artifact bytes."""
    return hashlib.sha256(connection_target_policy_artifact_bytes(environ)).hexdigest()


def _parse_host_port(raw: object) -> str:
    value = str(raw).strip()
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid allowed database target: {value}") from exc
    if (
        not host
        or port is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"allowed database target must be host:port: {value}")
    return _format_host_port(_normalize_host(host), port)


def _parse_cidr_rule(raw: object) -> _CidrRule:
    value = str(raw).strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or not value[end + 1 :].startswith(":"):
            raise ValueError(f"allowed database CIDR must include a port: {value}")
        network_text = value[1:end]
        port_text = value[end + 2 :]
    else:
        network_text, separator, port_text = value.rpartition(":")
        if not separator:
            raise ValueError(f"allowed database CIDR must include a port: {value}")
    try:
        port = int(port_text)
        network = ipaddress.ip_network(network_text, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid allowed database CIDR: {value}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid allowed database CIDR port: {value}")
    return _CidrRule(network=network, port=port)


def _normalize_host(host: str) -> str:
    candidate = host.rstrip(".").lower()
    if not candidate or "%" in candidate:
        raise ValueError("invalid host")
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid host") from exc
    if len(ascii_host) > 253 or any(
        _HOST_LABEL_RE.fullmatch(label) is None for label in ascii_host.split(".")
    ):
        raise ValueError("invalid host")
    return ascii_host


def _format_host_port(host: str, port: int) -> str:
    try:
        is_ipv6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        is_ipv6 = False
    formatted_host = f"[{host}]" if is_ipv6 else host
    return f"{formatted_host}:{port}"


def _default_dns_resolver(host: str) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


def _default_path_resolver(path: Path) -> Path:
    return path.resolve(strict=True)


def _address_matches(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    rules: tuple[_CidrRule, ...],
) -> bool:
    return any(
        address.version == rule.network.version and address in rule.network for rule in rules
    )


def _allowed(
    kind: ConnectionTargetKind,
    scheme: str,
    canonical_target: str,
) -> ConnectionTargetValidation:
    return ConnectionTargetValidation(
        allowed=True,
        reason=ConnectionTargetReasonCode.ALLOWED,
        kind=kind,
        scheme=scheme,
        canonical_target=canonical_target,
    )


def _denied(
    reason: ConnectionTargetReasonCode,
    *,
    kind: ConnectionTargetKind | None = None,
    scheme: str | None = None,
) -> ConnectionTargetValidation:
    return ConnectionTargetValidation(
        allowed=False,
        reason=reason,
        kind=kind,
        scheme=scheme,
    )


def _required_metadata(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _target_description(validation: ConnectionTargetValidation) -> str:
    assert validation.scheme is not None
    assert validation.canonical_target is not None
    if validation.kind is ConnectionTargetKind.FILE:
        return f"{validation.scheme} file {Path(validation.canonical_target).name}"
    return f"{validation.scheme} at {validation.canonical_target}"


def _validate_restored_record(
    record: ConnectionRecord,
    validation: ConnectionTargetValidation,
) -> None:
    if not isinstance(record.connection_ref, ConnectionRef):
        raise ValueError("connection_ref must be a ConnectionRef")
    _required_metadata(record.display_name, "display_name")
    _required_metadata(record.tenant_id, "tenant_id")
    if record.owner_subject is not None:
        _required_metadata(record.owner_subject, "owner_subject")
    if not isinstance(record.created_at, datetime) or record.created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    if type(record.enabled_for_user) is not bool:
        raise ValueError("enabled_for_user must be a boolean")
    if record.target_kind is not validation.kind:
        raise ValueError("target_kind does not match the connection secret")
    if record.dialect != validation.scheme:
        raise ValueError("dialect does not match the connection secret")
    if record.target_description != _target_description(validation):
        raise ValueError("target_description does not match the connection secret")


def _coerce_ref(connection_ref: ConnectionRef | str) -> ConnectionRef:
    if isinstance(connection_ref, ConnectionRef):
        return connection_ref
    if isinstance(connection_ref, str):
        return ConnectionRef(connection_ref)
    raise ValueError("invalid connection reference")


def _require_admin(principal: Principal, operation: str) -> None:
    if not principal.has_role("admin"):
        raise ConnectionAccessError(f"admin role is required to {operation}")


def _record_in_principal_scope(record: ConnectionRecord, principal: Principal) -> bool:
    if principal.has_role("admin"):
        return True
    return record.tenant_id == principal.tenant_id and (
        record.owner_subject is None or record.owner_subject == principal.subject
    )


def _record_visible_to(record: ConnectionRecord, principal: Principal) -> bool:
    if principal.has_role("admin"):
        return True
    return record.enabled_for_user and _record_in_principal_scope(record, principal)
