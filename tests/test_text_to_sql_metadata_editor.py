"""Unit tests for `custom_tools/text_to_sql/metadata_editor.py` (design doc
2026-09-05, `docs/plans/2026-09-05-text2sql-metadata-editor.md`).

Isolated from `service.py`: exercises `SchemaLoader`/`DsnProfile`/
`SchemaMemoryManager` through DB-plugin and `memory.tools` stubs, with
`tmp_path` as `project_root` (never writes into the repo's own `sqlrag/`).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_tools.text_to_sql.dsn_profile as dsn_profile_module
import custom_tools.text_to_sql.metadata_editor as editor_module
import db_plugins
from custom_tools.text_to_sql.dsn_profile import GlossaryEntry
from custom_tools.text_to_sql.metadata_editor import (
    ColumnDescriptionEdit,
    SchemaMetadataConflictError,
    TableDescriptionEdit,
    compute_glossary_digest,
    load_metadata_view,
    save_glossary,
    save_table_descriptions,
    set_fact_status,
)
from custom_tools.text_to_sql.schema_cache import _dsn_host_port_db
from custom_tools.text_to_sql.schema_loader import compute_editable_schema_digest
from custom_tools.text_to_sql.schema_memory_sqlite import SchemaMemoryManager, SemanticFact, _semantic_fact_key
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.utils import dsn_to_sanitized_name

_DSN = "postgresql://svc:secret@db.example:5432/app"

_LIVE_SCHEMA = {
    "public.orders": {
        "columns": {
            "amount": {"type": "DECIMAL"},
            "status": {"type": "TEXT"},
        },
    },
    "public.customers": {
        "columns": {"id": {"type": "INTEGER"}},
    },
}


class _FakePlugin:
    def __init__(self, schema: dict) -> None:
        self._schema = schema

    def connect(self, _dsn):
        return object()

    def parse_schema_from_dsn(self, _dsn):
        return None

    def introspect_schema(self, _conn, _schema_arg):
        return json.loads(json.dumps(self._schema))

    def normalize_schema_names(self, _dsn, schema):
        return schema

    def close(self, _conn):
        return None


def _install_live_plugin(monkeypatch, schema: dict = _LIVE_SCHEMA) -> None:
    monkeypatch.setattr(db_plugins, "get_plugin", lambda _dsn: _FakePlugin(schema))


def _install_fake_memory_tools(monkeypatch) -> list[dict]:
    saved: list[dict] = []
    monkeypatch.setitem(
        sys.modules,
        "memory.tools",
        SimpleNamespace(
            get_memory=lambda **kwargs: [
                {"data": record}
                for record in saved
                if record["schema_version"] == kwargs["session_id"]
                and record["cache_kind"] == kwargs["cache_kind"]
            ],
            save_memory=lambda **kwargs: saved.append(kwargs["data"]) or 1,
            memory_requester_context=lambda _agent: nullcontext(),
        ),
    )
    return saved


def _isolate_dsn_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dsn_profile_module, "get_repo_root", lambda: tmp_path)
    dsn_profile_module.reset_cache()


def _scope() -> SchemaScope:
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant-1",
            "access_scope_id": "owner:alice",
            "connection_view_id": "registry:orders",
            "transient": False,
        }
    )


def _write_sqlrag_file(tmp_path: Path, dsn: str, document: dict) -> None:
    sqlrag_dir = tmp_path / "sqlrag"
    sqlrag_dir.mkdir(parents=True, exist_ok=True)
    name = dsn_to_sanitized_name(dsn)
    (sqlrag_dir / f"{name}.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    _install_live_plugin(monkeypatch)
    _install_fake_memory_tools(monkeypatch)
    _isolate_dsn_profile(monkeypatch, tmp_path)
    # Fresh per-DSN process locks so a failed concurrency test cannot leave a
    # held lock behind for the next test using the same DSN.
    monkeypatch.setattr(editor_module, "_PROCESS_WRITE_LOCKS", {})
    yield


# ---------------------------------------------------------------------------
# load_metadata_view
# ---------------------------------------------------------------------------


def test_load_metadata_view_surfaces_live_schema_and_file_descriptions(tmp_path):
    _write_sqlrag_file(
        tmp_path,
        _DSN,
        {
            "enable": True,
            "schema_info": {
                "public.orders": {
                    "description": "Заказы",
                    "columns": {
                        "amount": {
                            "description": "Сумма заказа",
                            "examples": ["199.99", "5.00"],
                        }
                    },
                }
            },
        },
    )

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )

    assert view.dsn_dialect == "postgresql"
    assert view.editable_file_enabled is True
    assert view.schema_digest == compute_editable_schema_digest(
        {
            "public.orders": {
                "description": "Заказы",
                "columns": {
                    "amount": {
                        "description": "Сумма заказа",
                        "examples": ["199.99", "5.00"],
                    }
                },
            }
        }
    )
    orders = view.tables["public.orders"]
    assert orders.description == "Заказы"
    assert orders.description_source == "file"
    assert orders.columns["amount"].description == "Сумма заказа"
    assert orders.columns["amount"].description_source == "file"
    assert orders.columns["amount"].examples == ("199.99", "5.00")
    assert orders.columns["amount"].examples_source == "file"
    # status column has no file overlay.
    assert orders.columns["status"].description_source == "none"
    assert orders.columns["status"].examples_source == "none"
    # No profile.yaml yet.
    assert view.glossary_profile_exists is False
    assert view.glossary_digest == compute_glossary_digest(())
    assert view.glossary_entries == ()
    assert view.typed_probe_facts == ()


def test_load_metadata_view_without_file_reports_null_digest_and_disabled_flag(tmp_path):
    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )

    assert view.schema_digest is None
    assert view.editable_file_enabled is None
    assert view.tables["public.orders"].description_source == "none"


# ---------------------------------------------------------------------------
# save_table_descriptions
# ---------------------------------------------------------------------------


def test_save_table_descriptions_happy_path_round_trips_through_load(tmp_path):
    new_digest = save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=None,
        table_edits=(
            TableDescriptionEdit(
                table_fqn="public.orders",
                description="Заказы",
                columns=(
                    ColumnDescriptionEdit(
                        column="amount", description="Сумма", examples=["1", "2"]
                    ),
                ),
            ),
        ),
    )

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )
    assert view.schema_digest == new_digest
    orders = view.tables["public.orders"]
    assert orders.description == "Заказы"
    assert orders.columns["amount"].description == "Сумма"
    assert orders.columns["amount"].examples == ("1", "2")


def test_save_table_descriptions_partial_update_preserves_untouched_fields(tmp_path):
    first_digest = save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=None,
        table_edits=(
            TableDescriptionEdit(
                table_fqn="public.orders",
                description="Заказы",
                columns=(
                    ColumnDescriptionEdit(column="amount", description="Сумма", examples=None),
                ),
            ),
        ),
    )

    # Only touch the "status" column; "amount"'s description must survive.
    save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=first_digest,
        table_edits=(
            TableDescriptionEdit(
                table_fqn="public.orders",
                description=None,
                columns=(
                    ColumnDescriptionEdit(column="status", description="Статус", examples=None),
                ),
            ),
        ),
    )

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )
    orders = view.tables["public.orders"]
    assert orders.description == "Заказы"
    assert orders.columns["amount"].description == "Сумма"
    assert orders.columns["status"].description == "Статус"


def test_save_table_descriptions_empty_string_clears_description(tmp_path):
    digest = save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=None,
        table_edits=(
            TableDescriptionEdit(
                table_fqn="public.orders",
                description="Заказы",
                columns=(),
            ),
        ),
    )
    save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=digest,
        table_edits=(
            TableDescriptionEdit(table_fqn="public.orders", description="", columns=()),
        ),
    )

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )
    assert view.tables["public.orders"].description == ""


def test_save_table_descriptions_conflict_raises_schema_metadata_conflict_error(tmp_path):
    save_table_descriptions(
        project_root=tmp_path,
        dsn=_DSN,
        expected_schema_digest=None,
        table_edits=(
            TableDescriptionEdit(table_fqn="public.orders", description="Заказы", columns=()),
        ),
    )

    with pytest.raises(SchemaMetadataConflictError, match="version conflict"):
        save_table_descriptions(
            project_root=tmp_path,
            dsn=_DSN,
            expected_schema_digest="stale-digest",
            table_edits=(
                TableDescriptionEdit(table_fqn="public.orders", description="Другое", columns=()),
            ),
        )


def test_save_table_descriptions_rejects_unknown_table(tmp_path):
    with pytest.raises(ValueError, match="unknown table"):
        save_table_descriptions(
            project_root=tmp_path,
            dsn=_DSN,
            expected_schema_digest=None,
            table_edits=(
                TableDescriptionEdit(table_fqn="public.no_such_table", description="x", columns=()),
            ),
        )


def test_save_table_descriptions_rejects_unknown_column(tmp_path):
    with pytest.raises(ValueError, match="unknown column"):
        save_table_descriptions(
            project_root=tmp_path,
            dsn=_DSN,
            expected_schema_digest=None,
            table_edits=(
                TableDescriptionEdit(
                    table_fqn="public.orders",
                    description=None,
                    columns=(
                        ColumnDescriptionEdit(column="no_such_column", description="x", examples=None),
                    ),
                ),
            ),
        )


def test_save_table_descriptions_rejects_all_invalid_examples(tmp_path):
    with pytest.raises(ValueError, match="no valid scalar values"):
        save_table_descriptions(
            project_root=tmp_path,
            dsn=_DSN,
            expected_schema_digest=None,
            table_edits=(
                TableDescriptionEdit(
                    table_fqn="public.orders",
                    description=None,
                    columns=(
                        ColumnDescriptionEdit(
                            column="amount", description=None, examples=[float("nan")]
                        ),
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# save_glossary
# ---------------------------------------------------------------------------


def test_save_glossary_creates_new_profile_with_identity_fields_and_dedups_synonyms(tmp_path):
    new_digest = save_glossary(
        project_root=tmp_path,
        dsn=_DSN,
        expected_glossary_digest=compute_glossary_digest(()),
        entries=(
            GlossaryEntry(
                term="выручка",
                synonyms=("revenue", "revenue", "  выручка  "),
                table="public.orders",
                column="amount",
                kind="measure",
                note=None,
            ),
        ),
    )

    from custom_tools.text_to_sql.dsn_profile import load_dsn_profile

    profile = load_dsn_profile(_DSN)
    assert profile.dsn_fingerprint == _dsn_host_port_db(_DSN)
    assert profile.schema_namespace_version == canonical_schema_fingerprint(_LIVE_SCHEMA)
    assert profile.captured_at is not None
    assert profile.aliases == {}
    assert profile.type_hints == {}
    assert profile.nlu_hints == {}
    assert profile.few_shots_ref is None
    assert len(profile.glossary) == 1
    entry = profile.glossary[0]
    assert entry.term == "выручка"
    assert entry.synonyms == ("revenue",)  # deduped, self-duplicate dropped
    assert new_digest.digest == compute_glossary_digest(profile.glossary)
    assert new_digest.entries == profile.glossary


def test_save_glossary_conflict_raises_schema_metadata_conflict_error(tmp_path):
    digest = save_glossary(
        project_root=tmp_path,
        dsn=_DSN,
        expected_glossary_digest=compute_glossary_digest(()),
        entries=(
            GlossaryEntry(
                term="выручка", synonyms=(), table="public.orders", column="amount",
                kind="measure", note=None,
            ),
        ),
    )
    assert digest.digest

    with pytest.raises(SchemaMetadataConflictError, match="version conflict"):
        save_glossary(
            project_root=tmp_path,
            dsn=_DSN,
            expected_glossary_digest="stale-digest",
            entries=(),
        )


def test_save_glossary_rejects_unknown_table_and_column(tmp_path):
    with pytest.raises(ValueError, match="unknown table"):
        save_glossary(
            project_root=tmp_path,
            dsn=_DSN,
            expected_glossary_digest=compute_glossary_digest(()),
            entries=(
                GlossaryEntry(
                    term="ghost", synonyms=(), table="public.no_such_table",
                    column=None, kind=None, note=None,
                ),
            ),
        )

    with pytest.raises(ValueError, match="unknown column"):
        save_glossary(
            project_root=tmp_path,
            dsn=_DSN,
            expected_glossary_digest=compute_glossary_digest(()),
            entries=(
                GlossaryEntry(
                    term="ghost", synonyms=(), table="public.orders",
                    column="no_such_column", kind=None, note=None,
                ),
            ),
        )


def test_save_glossary_preserves_other_profile_sections_on_repeat_save(tmp_path):
    from custom_tools.text_to_sql.dsn_profile import load_dsn_profile
    from dataclasses import replace as dc_replace

    initial_digest = save_glossary(
        project_root=tmp_path,
        dsn=_DSN,
        expected_glossary_digest=compute_glossary_digest(()),
        entries=(
            GlossaryEntry(
                term="выручка", synonyms=(), table="public.orders", column="amount",
                kind="measure", note=None,
            ),
        ),
    )
    # Simulate a profile that also has other, editor-unowned sections filled in
    # (aliases/type_hints/etc.) by writing them directly, bypassing the editor.
    from custom_tools.text_to_sql.dsn_profile import save_dsn_profile_atomic

    profile_with_extras = dc_replace(
        load_dsn_profile(_DSN), aliases={"public.orders": ("orders_alias",)}
    )
    save_dsn_profile_atomic(profile_with_extras, _DSN)

    save_glossary(
        project_root=tmp_path,
        dsn=_DSN,
        expected_glossary_digest=initial_digest.digest,
        entries=(
            GlossaryEntry(
                term="доход", synonyms=(), table="public.orders", column="amount",
                kind="measure", note=None,
            ),
        ),
    )

    final_profile = load_dsn_profile(_DSN)
    assert final_profile.aliases == {"public.orders": ("orders_alias",)}
    assert [entry.term for entry in final_profile.glossary] == ["доход"]


# ---------------------------------------------------------------------------
# set_fact_status
# ---------------------------------------------------------------------------


def test_set_fact_status_round_trips_typed_probe_fact(tmp_path):
    namespace = SchemaNamespace(_scope(), canonical_schema_fingerprint(_LIVE_SCHEMA))
    fact = SemanticFact(
        subject="column",
        table_fqn="public.orders",
        column="amount",
        fact_kind="example",
        value="199.99",
        source="typed_probe",
        status="approved",
    )
    SchemaMemoryManager(tmp_path).save_approved_semantic_facts(namespace, (fact,))
    fact_key = _semantic_fact_key(fact)

    set_fact_status(
        project_root=tmp_path,
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
        fact_key=fact_key,
        status="rejected",
    )

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )
    [(returned_fact, status)] = view.typed_probe_facts
    assert returned_fact == fact
    assert status == "rejected"


def test_set_fact_status_rejects_unknown_fact_key(tmp_path):
    with pytest.raises(ValueError, match="unknown typed_probe fact_key"):
        set_fact_status(
            project_root=tmp_path,
            dsn=_DSN,
            scope_mapping=_scope().to_mapping(),
            fact_key="text2sql-semantic-fact-v1-does-not-exist",
            status="rejected",
        )


# ---------------------------------------------------------------------------
# write lock (per-DSN)
# ---------------------------------------------------------------------------


def _edit(table_fqn: str, description: str) -> tuple[TableDescriptionEdit, ...]:
    return (TableDescriptionEdit(table_fqn=table_fqn, description=description, columns=()),)


def test_concurrent_saves_for_same_dsn_are_serialised_and_second_sees_conflict(
    tmp_path, monkeypatch
):
    """Two admins pass the digest check "at the same time": without the write
    lock both would read the empty document and the second write would silently
    drop the first edit. With the lock the second save observes the first one
    and fails with a version conflict."""
    real_digest = editor_module.compute_editable_schema_digest
    first_inside = threading.Event()
    release_first = threading.Event()
    concurrent_inside = []
    inside = 0
    inside_guard = threading.Lock()

    def gated_digest(schema_info):
        nonlocal inside
        with inside_guard:
            inside += 1
            concurrent_inside.append(inside)
        try:
            if not first_inside.is_set():
                first_inside.set()
                # Hold the critical section until the second thread is parked
                # on the lock (or the test gives up waiting).
                release_first.wait(timeout=5)
            return real_digest(schema_info)
        finally:
            with inside_guard:
                inside -= 1

    monkeypatch.setattr(editor_module, "compute_editable_schema_digest", gated_digest)

    outcomes: dict[str, object] = {}

    def run(name: str, table_fqn: str) -> None:
        try:
            outcomes[name] = save_table_descriptions(
                project_root=tmp_path,
                dsn=_DSN,
                expected_schema_digest=None,
                table_edits=_edit(table_fqn, f"from {name}"),
            )
        except Exception as exc:  # noqa: BLE001 - recorded for assertions
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("first", "public.orders"))
    second = threading.Thread(target=run, args=("second", "public.customers"))
    first.start()
    assert first_inside.wait(timeout=5)
    second.start()
    # Give the second thread time to reach (and block on) the write lock.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not second.is_alive():
        time.sleep(0.01)
    time.sleep(0.2)
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()

    assert max(concurrent_inside) == 1, "digest check ran concurrently: lock is not held"
    assert isinstance(outcomes["first"], str)
    assert isinstance(outcomes["second"], SchemaMetadataConflictError)

    view = load_metadata_view(
        project_root=tmp_path,
        connection_ref="conn-1",
        dsn=_DSN,
        scope_mapping=_scope().to_mapping(),
    )
    assert view.tables["public.orders"].description == "from first"
    assert view.tables["public.customers"].description == ""
    assert view.tables["public.customers"].description_source == "none"


def test_write_lock_file_is_per_dsn_and_matches_sqlrag_file_name(tmp_path):
    other_dsn = "postgresql://svc:secret@db.example:5432/other"
    for dsn in (_DSN, other_dsn):
        save_table_descriptions(
            project_root=tmp_path,
            dsn=dsn,
            expected_schema_digest=None,
            table_edits=_edit("public.orders", "Заказы"),
        )

    sqlrag_dir = tmp_path / "sqlrag"
    lock_files = sorted(p.name for p in sqlrag_dir.glob(".metadata-editor.*.lock"))
    expected = sorted(
        f".metadata-editor.{dsn_to_sanitized_name(dsn)}.lock" for dsn in (_DSN, other_dsn)
    )
    assert lock_files == expected
    assert len(set(lock_files)) == 2
    for dsn in (_DSN, other_dsn):
        assert (sqlrag_dir / f"{dsn_to_sanitized_name(dsn)}.json").exists()
