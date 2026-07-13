from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import workflow.state_manager as state_manager_module
from workflow.models import WorkflowCheckpoint, WorkflowStatus
from workflow.state_manager import (
    CheckpointSecretReferenceMissingError,
    CheckpointSecretStoreError,
    SQLiteWorkflowStore,
)


_SECRET_STORE_VERSION_KEY = "__workflow_secret_store_version__"


def _checkpoint(workflow_id: str, value: str, index: int = 0) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        workflow_id=workflow_id,
        timestamp=datetime(2026, 1, 1) + timedelta(microseconds=index),
        status=WorkflowStatus.RUNNING,
        metadata={"secret": value},
    )


def _write_checkpoints_in_spawned_process(
    db_path: str,
    process_name: str,
    iterations: int,
    barrier: object,
) -> None:
    store = SQLiteWorkflowStore(db_path)
    original_load = store._load_secrets
    synchronize_next_load = False

    def synchronized_load() -> dict[str, object]:
        nonlocal synchronize_next_load
        secrets = original_load()
        if synchronize_next_load:
            synchronize_next_load = False
            barrier.wait(timeout=20)
        return secrets

    store._load_secrets = synchronized_load

    async def write_all() -> None:
        nonlocal synchronize_next_load
        for index in range(iterations):
            synchronize_next_load = True
            workflow_id = f"{process_name}-{index}"
            await store.save_checkpoint(
                _checkpoint(workflow_id, f"value-{workflow_id}", index)
            )

    asyncio.run(write_all())


@pytest.mark.asyncio
async def test_spawned_checkpoint_writers_preserve_every_reference(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workflow.db"
    SQLiteWorkflowStore(str(db_path))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_write_checkpoints_in_spawned_process,
            args=(str(db_path), process_name, 25, barrier),
        )
        for process_name in ("writer-a", "writer-b")
    ]

    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=45)
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    store = SQLiteWorkflowStore(str(db_path))
    envelope = json.loads(store.secrets_path.read_text(encoding="utf-8"))
    references = {
        key for key in envelope if key.startswith("workflow_secret:")
    }
    assert envelope[_SECRET_STORE_VERSION_KEY] == 1
    assert len(references) == 50
    for process_name in ("writer-a", "writer-b"):
        for index in range(25):
            workflow_id = f"{process_name}-{index}"
            restored = await store.get_latest_checkpoint(workflow_id)
            assert restored is not None
            assert restored.metadata["secret"] == f"value-{workflow_id}"


def test_legacy_flat_store_is_merged_and_rewritten_as_version_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    store.secrets_path.write_text(
        json.dumps({"workflow_secret:legacy": "legacy-value"}),
        encoding="utf-8",
    )
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(state_manager_module.os, "fsync", recording_fsync)
    store._save_secrets({"workflow_secret:new": "new-value"})

    envelope = json.loads(store.secrets_path.read_text(encoding="utf-8"))
    assert envelope == {
        _SECRET_STORE_VERSION_KEY: 1,
        "workflow_secret:legacy": "legacy-value",
        "workflow_secret:new": "new-value",
    }
    assert fsync_calls
    assert store.secrets_path.stat().st_mode & 0o777 == 0o600
    assert store.secrets_lock_path.stat().st_mode & 0o777 == 0o600


def test_secret_replace_fsyncs_file_then_parent_directory_and_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    events: list[str] = []
    directory_fds: list[int] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("dir-fsync")
            directory_fds.append(fd)
        else:
            events.append("file-fsync")
        real_fsync(fd)

    def recording_replace(source: object, destination: object) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(state_manager_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(state_manager_module.os, "replace", recording_replace)

    store._save_secrets({"workflow_secret:new": "new-value"})

    assert events == ["file-fsync", "replace", "dir-fsync"]
    assert len(directory_fds) == 1
    with pytest.raises(OSError):
        os.fstat(directory_fds[0])
    assert not list(tmp_path.glob(f".{store.secrets_path.name}.*.tmp"))


def test_failed_replace_preserves_previous_valid_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    store._save_secrets({"workflow_secret:first": "first-value"})
    previous = store.secrets_path.read_bytes()

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(state_manager_module.os, "replace", fail_replace)
    with pytest.raises(CheckpointSecretStoreError, match="write checkpoint secret"):
        store._save_secrets({"workflow_secret:second": "second-value"})

    assert store.secrets_path.read_bytes() == previous
    assert store._load_secrets()["workflow_secret:first"] == "first-value"
    assert not list(tmp_path.glob(f".{store.secrets_path.name}.*.tmp"))


@pytest.mark.asyncio
async def test_corrupt_secret_store_is_typed_and_not_swallowed_by_history(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    await store.save_checkpoint(_checkpoint("corrupt", "preserved"))
    store.secrets_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CheckpointSecretStoreError, match="checkpoint secret store"):
        await store.get_latest_checkpoint("corrupt")
    with pytest.raises(CheckpointSecretStoreError, match="checkpoint secret store"):
        await store.get_workflow_history("corrupt")


@pytest.mark.asyncio
async def test_missing_reference_is_typed_with_checkpoint_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    await store.save_checkpoint(_checkpoint("missing", "preserved"))
    envelope = json.loads(store.secrets_path.read_text(encoding="utf-8"))
    reference = next(
        key for key in envelope if key.startswith("workflow_secret:")
    )
    envelope.pop(reference)
    store.secrets_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CheckpointSecretReferenceMissingError) as exc_info:
        await store.get_latest_checkpoint("missing")

    assert exc_info.value.reference == reference
    assert "missing" in exc_info.value.checkpoint_identity
    assert "preserved" not in str(exc_info.value)
    with pytest.raises(CheckpointSecretReferenceMissingError):
        await store.get_workflow_history("missing")


def test_worker_loaded_config_digest_is_private_and_durably_retrievable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql._yaml_config_loader import YamlConfigLoader
    from workflow import streamlit_api

    config_path = tmp_path / "worker-config.yaml"
    loaded_bytes = b"generation: worker-loaded\n"
    config_path.write_bytes(loaded_bytes)
    loader = YamlConfigLoader[dict[str, object]](
        env_path_var="T06_WORKER_CONFIG_PATH",
        default_path=config_path,
        parser=lambda raw, _source_path: dict(raw),
        not_found_message=lambda path, env: f"missing {path}; set {env}",
        mapping_error_message=lambda path: f"invalid mapping at {path}",
    )

    class WorkerEngine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            loader.load()
            config_path.write_text("generation: parent-later\n", encoding="utf-8")
            return SimpleNamespace(
                status=WorkflowStatus.COMPLETED,
                step_results={},
                terminal_outcome=None,
                workflow_id="worker-workflow",
                final_output={"ok": True},
            )

    manager = object.__new__(streamlit_api.WorkflowManager)
    manager._engine = WorkerEngine()
    manager.active_runs = {}
    manager._notify_progress = lambda *_args, **_kwargs: None
    workflow_definition = SimpleNamespace(
        name="worker_config_test",
        steps=[],
        metadata={"category": "general"},
    )
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        lambda _path: workflow_definition,
    )
    captured_versions: list[dict[str, dict[str, str | None]]] = []
    real_capture = streamlit_api._capture_active_yaml_config_versions

    def capture_versions() -> dict[str, dict[str, str | None]]:
        versions = real_capture()
        captured_versions.append(versions)
        return versions

    monkeypatch.setattr(
        streamlit_api,
        "_capture_active_yaml_config_versions",
        capture_versions,
    )
    persisted: list[dict[str, object]] = []

    def persist(
        run_id,
        result,
        status,
        error=None,
        *,
        artifacts=None,
        snapshot=None,
        terminal_outcome=None,
        run_incarnation=None,
        **_claim,
    ):
        payload = streamlit_api._build_workflow_result_event_payload(
            run_id,
            result,
            status,
            error,
            artifacts,
            snapshot,
            terminal_outcome,
            run_incarnation,
        )
        persisted.append(payload)
        return streamlit_api._WorkflowResultResolution(True, True, payload)

    monkeypatch.setattr(streamlit_api, "_persist_workflow_result", persist)
    parameters = {"question": "count rows"}
    try:
        manager._execute_workflow_in_context(
            "run-worker-config",
            tmp_path / "workflow.yaml",
            parameters,
            "session-worker",
            run_incarnation="inc-worker",
        )
        assert len(captured_versions) == 1
        expected_versions = captured_versions[0]
        registry_key = str(config_path.resolve(strict=False))

        assert expected_versions[registry_key]["content_sha256"] == (
            hashlib.sha256(loaded_bytes).hexdigest()
        )
        assert persisted[0]["artifacts"]["metadata"]["config_versions"] == (
            expected_versions
        )
        assert "config_versions" not in persisted[0]["snapshot"]["parameters"]
        assert "config_versions" not in parameters

        manager.active_runs.clear()
        monkeypatch.setattr(
            streamlit_api,
            "_workflow_result_payload_from_store",
            lambda _run_id, **_kwargs: persisted[0],
        )
        artifacts = manager.get_workflow_artifacts("run-worker-config")
        assert artifacts is not None
        assert artifacts.metadata["config_versions"] == expected_versions
    finally:
        loader.reset_cache()
