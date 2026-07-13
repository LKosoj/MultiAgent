from __future__ import annotations

import dataclasses
import re
import socket
from datetime import datetime
from pathlib import Path

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.connection_registry import (
    ConnectionAccessError,
    ConnectionDisabledError,
    ConnectionNotFoundError,
    ConnectionRef,
    ConnectionRegistry,
    ConnectionTargetKind,
    ConnectionTargetPolicy,
    ConnectionTargetPolicyError,
    ConnectionTargetReasonCode,
)


def _principal(
    subject: str,
    tenant_id: str = "tenant-a",
    *roles: str,
) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        roles=frozenset(roles or ("user",)),
    )


def _network_policy(
    *,
    targets: set[str] | None = None,
    cidrs: set[str] | None = None,
    resolver=None,
) -> ConnectionTargetPolicy:
    return ConnectionTargetPolicy(
        allowed_schemes={"postgresql"},
        allowed_network_targets=targets or set(),
        allowed_network_cidrs=cidrs or set(),
        dns_resolver=resolver,
    )


def test_network_target_is_deny_by_default_and_requires_exact_host_and_port() -> None:
    denied = _network_policy()

    result = denied.validate("postgresql://user:secret@db.example:5432/app")

    assert result.allowed is False
    assert result.reason is ConnectionTargetReasonCode.NETWORK_TARGET_DENIED

    policy = _network_policy(targets={"DB.EXAMPLE.:5432"})
    allowed = policy.require_allowed(
        "postgresql://user:secret@db.example:5432/app?sslmode=require"
    )

    assert allowed.reason is ConnectionTargetReasonCode.ALLOWED
    assert allowed.kind is ConnectionTargetKind.NETWORK
    assert allowed.scheme == "postgresql"
    assert allowed.canonical_target == "db.example:5432"

    cases = (
        (
            "postgresql://db.example:5433/app",
            ConnectionTargetReasonCode.NETWORK_TARGET_DENIED,
        ),
        (
            "postgresql://other.example:5432/app",
            ConnectionTargetReasonCode.NETWORK_TARGET_DENIED,
        ),
        ("postgresql://db.example/app", ConnectionTargetReasonCode.MISSING_PORT),
        ("postgresql://:5432/app", ConnectionTargetReasonCode.MISSING_HOST),
        (
            "postgresql://db.example:not-a-port/app",
            ConnectionTargetReasonCode.MALFORMED_DSN,
        ),
        (
            "postgresql://db.example:0/app",
            ConnectionTargetReasonCode.MALFORMED_DSN,
        ),
        (
            "mysql://db.example:5432/app",
            ConnectionTargetReasonCode.SCHEME_NOT_ALLOWED,
        ),
        ("", ConnectionTargetReasonCode.EMPTY_DSN),
    )
    for dsn, reason in cases:
        with pytest.raises(ConnectionTargetPolicyError) as exc_info:
            policy.require_allowed(dsn)
        assert exc_info.value.reason is reason


def test_cidr_policy_uses_injected_dns_and_requires_every_address_to_be_allowed() -> None:
    answers = ["10.42.0.7"]
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return list(answers)

    policy = _network_policy(cidrs={"10.42.0.0/24:5432"}, resolver=resolve)

    assert policy.validate("postgresql://db.internal:5432/app").allowed is True
    assert calls == ["db.internal"]

    answers.append("192.0.2.10")
    denied = policy.validate("postgresql://db.internal:5432/app")
    assert denied.allowed is False
    assert denied.reason is ConnectionTargetReasonCode.NETWORK_TARGET_DENIED

    answers.clear()
    unresolved = policy.validate("postgresql://db.internal:5432/app")
    assert unresolved.allowed is False
    assert unresolved.reason is ConnectionTargetReasonCode.DNS_RESOLUTION_FAILED


@pytest.mark.parametrize(
    "query_key",
    ["host", "HOST", "%68ost", "hostaddr", "port", "service", "servicefile"],
)
def test_postgres_routing_query_parameters_are_rejected_before_dns(
    query_key: str,
) -> None:
    dns_calls: list[str] = []
    policy = _network_policy(
        cidrs={"10.42.0.0/24:5432"},
        resolver=lambda host: dns_calls.append(host) or ["10.42.0.7"],
    )

    result = policy.validate(
        f"postgresql://db.internal:5432/app?{query_key}=attacker"
    )

    assert result.allowed is False
    assert result.reason is ConnectionTargetReasonCode.ROUTING_PARAMETER_DENIED
    assert dns_calls == []


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "psql", "pg"])
def test_postgres_family_rejects_authority_override_query_parameters(
    scheme: str,
) -> None:
    policy = ConnectionTargetPolicy(
        allowed_schemes={scheme},
        allowed_network_targets={"db.example:5432"},
    )

    result = policy.validate(
        f"{scheme}://db.example:5432/app?host=other.example"
    )

    assert result.reason is ConnectionTargetReasonCode.ROUTING_PARAMETER_DENIED


def test_postgres_nonrouting_query_parameters_remain_allowed() -> None:
    policy = _network_policy(targets={"db.example:5432"})

    result = policy.require_allowed(
        "postgresql://db.example:5432/app"
        "?sslmode=require&application_name=text-to-sql"
    )

    assert result.allowed is True
    assert result.canonical_target == "db.example:5432"


def test_file_target_requires_canonical_existing_path_under_allowed_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    database = root / "app.db"
    database.touch()
    outside = tmp_path / "outside.db"
    outside.touch()
    (root / "escape.db").symlink_to(outside)

    resolved_inputs: list[Path] = []

    def resolve_path(path: Path) -> Path:
        resolved_inputs.append(path)
        return path.resolve(strict=True)

    policy = ConnectionTargetPolicy(
        allowed_schemes={"sqlite", "duckdb"},
        allowed_file_roots={root},
        path_resolver=resolve_path,
    )

    sqlite_result = policy.require_allowed(f"sqlite://{database}")
    duckdb_result = policy.require_allowed(f"duckdb://{database}")
    assert sqlite_result.kind is ConnectionTargetKind.FILE
    assert sqlite_result.canonical_target == str(database.resolve())
    assert duckdb_result.kind is ConnectionTargetKind.FILE
    assert resolved_inputs

    cases = (
        ("sqlite:relative.db", ConnectionTargetReasonCode.FILE_PATH_NOT_ABSOLUTE),
        ("sqlite:///:memory:", ConnectionTargetReasonCode.FILE_MEMORY_DENIED),
        (
            f"sqlite://{root / '..' / root.name / database.name}",
            ConnectionTargetReasonCode.FILE_PATH_TRAVERSAL,
        ),
        (
            f"sqlite://{root / 'escape.db'}",
            ConnectionTargetReasonCode.FILE_ROOT_DENIED,
        ),
        (
            f"sqlite://{root / 'missing.db'}",
            ConnectionTargetReasonCode.FILE_PATH_UNRESOLVED,
        ),
        (
            f"sqlite://{root}/bad%00name.db",
            ConnectionTargetReasonCode.MALFORMED_DSN,
        ),
    )
    for dsn, reason in cases:
        with pytest.raises(ConnectionTargetPolicyError) as exc_info:
            policy.require_allowed(dsn)
        assert exc_info.value.reason is reason

    no_roots = ConnectionTargetPolicy(allowed_schemes={"sqlite"})
    assert no_roots.validate(f"sqlite://{database}").reason is (
        ConnectionTargetReasonCode.FILE_ROOT_DENIED
    )


def test_environment_policy_has_no_implicit_targets_or_schemes(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    database.touch()
    empty = ConnectionTargetPolicy.from_environment({})

    assert empty.validate("postgresql://db.example:5432/app").reason is (
        ConnectionTargetReasonCode.SCHEME_NOT_ALLOWED
    )
    assert empty.validate(f"sqlite://{database}").reason is (
        ConnectionTargetReasonCode.SCHEME_NOT_ALLOWED
    )

    configured = ConnectionTargetPolicy.from_environment(
        {
            "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "postgresql, sqlite",
            "TEXT_TO_SQL_ALLOWED_DB_TARGETS": "db.example:5432",
            "TEXT_TO_SQL_ALLOWED_DB_CIDRS": "10.0.0.0/8:5432",
            "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": str(tmp_path),
        },
        dns_resolver=lambda _host: ["10.0.0.8"],
    )

    assert configured.validate("postgresql://db.example:5432/app").allowed is True
    assert configured.validate(f"sqlite://{database}").allowed is True

    with pytest.raises(ValueError, match="unsupported database scheme"):
        ConnectionTargetPolicy(allowed_schemes={"https"})


def test_registry_keeps_secret_private_and_enforces_owner_tenant_acl() -> None:
    admin = _principal("root", "ops", "admin", "user")
    alice = _principal("alice")
    bob = _principal("bob")
    other_tenant_alice = _principal("alice", "tenant-b")
    dsn = "postgresql://alice:super-secret@db.example:5432/app"
    registry = ConnectionRegistry(
        _network_policy(targets={"db.example:5432"})
    )

    with pytest.raises(ConnectionAccessError):
        registry.register(
            alice,
            display_name="Production",
            dsn=dsn,
            owner_subject="alice",
            tenant_id="tenant-a",
        )

    record = registry.register(
        admin,
        display_name="Production",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    assert isinstance(record.connection_ref, ConnectionRef)
    assert re.fullmatch(
        r"conn-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        str(record.connection_ref),
    )
    assert record.target_kind is ConnectionTargetKind.NETWORK
    assert record.target_description == "postgresql at db.example:5432"
    assert "super-secret" not in repr(record)
    assert "super-secret" not in repr(record.to_public_dict())
    assert "dsn" not in record.to_public_dict()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.display_name = "changed"  # type: ignore[misc]

    assert registry.list_for(alice) == (record,)
    assert registry.list_for(bob) == ()
    assert registry.list_for(other_tenant_alice) == ()
    assert registry.list_for(admin) == (record,)
    assert registry.resolve(record.connection_ref, alice) == dsn

    with pytest.raises(ConnectionAccessError):
        registry.resolve(record.connection_ref, bob)
    with pytest.raises(ConnectionAccessError):
        registry.resolve(record.connection_ref, other_tenant_alice)
    with pytest.raises(ConnectionAccessError):
        registry.delete(record.connection_ref, alice)

    assert registry.delete(record.connection_ref, admin) == record
    with pytest.raises(ConnectionNotFoundError):
        registry.resolve(record.connection_ref, admin)


def test_tenant_wide_and_disabled_records_have_explicit_user_behavior() -> None:
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    bob = _principal("bob")
    other_tenant = _principal("charlie", "tenant-b")
    dsn = "postgresql://svc:secret@db.example:5432/app"
    registry = ConnectionRegistry(
        _network_policy(targets={"db.example:5432"})
    )

    tenant_wide = registry.register(
        admin,
        display_name="Tenant warehouse",
        dsn=dsn,
        owner_subject=None,
        tenant_id="tenant-a",
    )
    disabled = registry.register(
        admin,
        display_name="Disabled warehouse",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
        enabled_for_user=False,
    )

    assert registry.list_for(alice) == (tenant_wide,)
    assert registry.list_for(bob) == (tenant_wide,)
    assert registry.list_for(other_tenant) == ()
    assert registry.resolve(tenant_wide.connection_ref, bob) == dsn
    with pytest.raises(ConnectionDisabledError):
        registry.resolve(disabled.connection_ref, alice)

    assert registry.list_for(admin) == (tenant_wide, disabled)
    assert registry.resolve(disabled.connection_ref, admin) == dsn


def test_registry_rechecks_current_target_policy_on_every_resolution() -> None:
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    answers = ["10.42.0.7"]
    policy = _network_policy(
        cidrs={"10.42.0.0/24:5432"},
        resolver=lambda _host: list(answers),
    )
    registry = ConnectionRegistry(policy)
    record = registry.register(
        admin,
        display_name="Warehouse",
        dsn="postgresql://svc:secret@db.internal:5432/app",
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    assert registry.resolve(record.connection_ref, alice).startswith("postgresql://")

    answers[:] = ["192.0.2.10"]
    with pytest.raises(ConnectionTargetPolicyError) as exc_info:
        registry.resolve(record.connection_ref, alice)
    assert exc_info.value.reason is ConnectionTargetReasonCode.NETWORK_TARGET_DENIED


def test_legacy_resolution_is_explicit_admin_only_and_policy_checked() -> None:
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    calls: list[tuple[str, Principal]] = []
    legacy_dsn = "postgresql://legacy:secret@db.example:5432/app"

    def resolve_legacy(reference: str, principal: Principal) -> str:
        calls.append((reference, principal))
        return legacy_dsn

    registry = ConnectionRegistry(
        _network_policy(targets={"db.example:5432"}),
        legacy_resolver=resolve_legacy,
    )

    with pytest.raises(ConnectionAccessError):
        registry.resolve_legacy("db_config:prod", alice)
    assert calls == []

    assert registry.resolve_legacy("db_config:prod", admin) == legacy_dsn
    assert calls == [("db_config:prod", admin)]

    with pytest.raises(ValueError, match="db_config"):
        registry.resolve_legacy("conn-not-legacy", admin)

    denied_registry = ConnectionRegistry(
        _network_policy(targets={"other.example:5432"}),
        legacy_resolver=resolve_legacy,
    )
    with pytest.raises(ConnectionTargetPolicyError):
        denied_registry.resolve_legacy("db_config:prod", admin)


def test_registry_rejects_invalid_public_metadata_without_storing_a_secret() -> None:
    admin = _principal("root", "ops", "admin")
    registry = ConnectionRegistry(
        _network_policy(targets={"db.example:5432"})
    )

    for kwargs in (
        {"display_name": "", "owner_subject": "alice", "tenant_id": "tenant-a"},
        {"display_name": "db", "owner_subject": "", "tenant_id": "tenant-a"},
        {"display_name": "db", "owner_subject": "alice", "tenant_id": ""},
    ):
        with pytest.raises(ValueError):
            registry.register(
                admin,
                dsn="postgresql://svc:secret@db.example:5432/app",
                **kwargs,
            )

    assert registry.list_for(admin) == ()


def test_registry_restore_preserves_reference_across_restart_and_keeps_dsn_private() -> None:
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    dsn = "postgresql://alice:restart-secret@db.example:5432/app"
    policy = _network_policy(targets={"db.example:5432"})
    original_registry = ConnectionRegistry(policy)
    record = original_registry.register(
        admin,
        display_name="Production",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    restarted_registry = ConnectionRegistry(policy)
    restored = restarted_registry.restore(record, dsn)

    assert restored is record
    assert restarted_registry.list_for(alice) == (record,)
    assert restarted_registry.resolve(record.connection_ref, alice) == dsn
    assert restored.to_public_dict()["connection_ref"] == str(record.connection_ref)
    assert "restart-secret" not in repr(restored.to_public_dict())
    assert "dsn" not in restored.to_public_dict()


def test_registry_restore_rejects_duplicate_ref_without_replacing_secret() -> None:
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    original_dsn = "postgresql://alice:original@db.example:5432/app"
    replacement_dsn = "postgresql://alice:replacement@db.example:5432/app"
    policy = _network_policy(targets={"db.example:5432"})
    source = ConnectionRegistry(policy)
    record = source.register(
        admin,
        display_name="Production",
        dsn=original_dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )
    restarted = ConnectionRegistry(policy)
    restarted.restore(record, original_dsn)

    with pytest.raises(ValueError, match="already registered"):
        restarted.restore(record, replacement_dsn)

    assert restarted.resolve(record.connection_ref, alice) == original_dsn


def test_registry_restore_validates_persisted_metadata_secret_and_current_policy() -> None:
    admin = _principal("root", "ops", "admin")
    dsn = "postgresql://alice:secret@db.example:5432/app"
    source = ConnectionRegistry(_network_policy(targets={"db.example:5432"}))
    record = source.register(
        admin,
        display_name="Production",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    invalid_records = (
        dataclasses.replace(record, display_name=""),
        dataclasses.replace(record, owner_subject=""),
        dataclasses.replace(record, tenant_id=""),
        dataclasses.replace(record, target_kind=ConnectionTargetKind.FILE),
        dataclasses.replace(record, dialect="mysql"),
        dataclasses.replace(record, target_description="tampered"),
        dataclasses.replace(record, created_at=datetime(2026, 1, 1)),
        dataclasses.replace(record, enabled_for_user="yes"),  # type: ignore[arg-type]
    )
    for invalid_record in invalid_records:
        registry = ConnectionRegistry(_network_policy(targets={"db.example:5432"}))
        with pytest.raises(ValueError):
            registry.restore(invalid_record, dsn)
        assert registry.list_for(admin) == ()

    denied = ConnectionRegistry(_network_policy(targets={"other.example:5432"}))
    with pytest.raises(ConnectionTargetPolicyError):
        denied.restore(record, dsn)
    assert denied.list_for(admin) == ()

    empty_secret = ConnectionRegistry(_network_policy(targets={"db.example:5432"}))
    with pytest.raises(ConnectionTargetPolicyError):
        empty_secret.restore(record, "")
    assert empty_secret.list_for(admin) == ()


def test_policy_validation_and_registry_resolution_never_open_a_network_socket(
    monkeypatch,
) -> None:
    opened: list[tuple[object, ...]] = []

    def reject_network_open(*args, **kwargs):
        opened.append(args)
        raise AssertionError("target-policy validation must not open a database socket")

    monkeypatch.setattr(socket, "create_connection", reject_network_open)
    admin = _principal("root", "ops", "admin")
    alice = _principal("alice")
    dsn = "postgresql://alice:secret@db.example:5432/app"
    registry = ConnectionRegistry(
        _network_policy(targets={"db.example:5432"})
    )

    record = registry.register(
        admin,
        display_name="Production",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    assert registry.resolve(record.connection_ref, alice) == dsn
    assert opened == []
