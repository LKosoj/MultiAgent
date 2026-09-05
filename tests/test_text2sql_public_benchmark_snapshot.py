from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys

import pytest

from custom_tools.text_to_sql.eval import sandbox_snapshot
from custom_tools.text_to_sql.eval.public_benchmark_release import (
    json_digest,
    release_bundle_identity,
)
from custom_tools.text_to_sql.eval.sandbox import (
    BwrapSandboxSpec,
    CANONICAL_RUNTIME_SOURCE_PATHS,
    SandboxError,
    build_bwrap_command,
    create_source_snapshot,
    source_snapshot_from_manifest,
    source_snapshot_manifest,
    verify_source_snapshot,
)


def test_source_snapshot_is_positive_and_excludes_runtime_and_secrets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "config").mkdir()
    (source / "memory").mkdir()
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    (source / "config" / "pipeline.yaml").write_text("mode: strict\n", encoding="utf-8")
    (source / "memory" / "tools.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "memory" / "smolagents_memory.db").write_bytes(b"history")
    (source / "data").mkdir()
    (source / "data" / "agui_events.db").write_bytes(b"history")
    (source / "logs").mkdir()
    (source / "logs" / "sql_history.jsonl").write_text("history\n", encoding="utf-8")
    (source / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (source / "unrelated.txt").write_text("not executable input\n", encoding="utf-8")

    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"), Path("config"), Path("memory")),
    )

    assert snapshot.root == (tmp_path / "snapshot").resolve()
    assert snapshot.digest.startswith("sha256:")
    assert (snapshot.root / "backend" / "app.py").is_file()
    assert (snapshot.root / "config" / "pipeline.yaml").is_file()
    assert (snapshot.root / "memory" / "smolagents_memory.db").read_bytes() == b""
    assert not (snapshot.root / "workflow_state.db").exists()
    assert list((snapshot.root / "data").iterdir()) == []
    assert list((snapshot.root / "logs").iterdir()) == []
    assert not (snapshot.root / ".env").exists()
    assert not (snapshot.root / "unrelated.txt").exists()


@pytest.mark.parametrize("kind", ["symlink", "secret", "gold", "state"])
def test_source_snapshot_rejects_unsafe_content(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    if kind == "symlink":
        (source / "backend" / "linked.py").symlink_to(source / "backend" / "app.py")
    elif kind == "secret":
        (source / "backend" / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    elif kind == "gold":
        (source / "backend" / "gold.sql").write_text("SELECT 1\n", encoding="utf-8")
    else:
        (source / "backend" / "previous_run.db").write_bytes(b"state")

    with pytest.raises(SandboxError):
        create_source_snapshot(
            source,
            tmp_path / "snapshot",
            allowed_paths=(Path("backend"),),
        )


def test_source_snapshot_digest_changes_when_allowed_source_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    source_file = source / "backend" / "app.py"
    source_file.write_text("APP = 1\n", encoding="utf-8")

    first = create_source_snapshot(
        source,
        tmp_path / "snapshot-one",
        allowed_paths=(Path("backend"),),
    )
    source_file.write_text("APP = 2\n", encoding="utf-8")
    second = create_source_snapshot(
        source,
        tmp_path / "snapshot-two",
        allowed_paths=(Path("backend"),),
    )

    assert first.digest != second.digest


def test_reporting_snapshot_is_closed_against_source_substitution_and_unlisted_import(
    tmp_path: Path,
) -> None:
    """The reporting helper must execute only from its frozen source inventory."""
    assert (
        Path("scripts/text2sql_benchmark_reporting.py")
        in CANONICAL_RUNTIME_SOURCE_PATHS
    )
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    reporting = scripts / "text2sql_benchmark_reporting.py"
    reporting.write_text('VALUE = "frozen"\n', encoding="utf-8")
    allowed = (Path("scripts/text2sql_benchmark_reporting.py"),)
    frozen = create_source_snapshot(source, tmp_path / "frozen", allowed_paths=allowed)

    reporting.write_text('VALUE = "substituted"\n', encoding="utf-8")
    environment = {"PATH": os.environ["PATH"], "PYTHONNOUSERSITE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scripts import text2sql_benchmark_reporting as r; print(r.VALUE); print(r.__file__)",
        ],
        cwd=frozen.root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    value, module_path = result.stdout.splitlines()
    assert value == "frozen"
    assert Path(module_path).resolve().is_relative_to(frozen.root)

    reporting.write_text("from scripts.unlisted import VALUE\n", encoding="utf-8")
    (scripts / "unlisted.py").write_text('VALUE = "host-only"\n', encoding="utf-8")
    unclosed = create_source_snapshot(
        source, tmp_path / "unclosed", allowed_paths=allowed
    )
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.text2sql_benchmark_reporting"],
        cwd=unclosed.root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "scripts.unlisted" in result.stderr


def test_snapshot_rejects_tampered_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    backend_dir = snapshot.root / "backend"
    link = backend_dir / "linked.py"
    # `create_source_snapshot` seals every directory to 0o555 (no write bit),
    # so the owning user (not just other users) cannot add entries either.
    # Temporarily unlock the directory to plant the tamper, then reseal it so
    # the assertion below exercises the symlink rejection itself rather than
    # an incidental "backend dir mode changed" permission mismatch.
    backend_dir.chmod(0o755)
    link.symlink_to("../outside.py")
    backend_dir.chmod(0o555)
    spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
    )

    with pytest.raises(SandboxError, match="snapshot"):
        build_bwrap_command(spec)


def test_snapshot_verification_rejects_changed_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    # `create_source_snapshot` seals files to 0o444 and directories to 0o555,
    # so the tamper below must unlock/reseal around each write — otherwise the
    # owning user can't even write the "changed" content, and if the mode
    # were left unlocked the permission check would fire first and mask the
    # content-mismatch rejection this test is actually about.
    app_py = snapshot.root / "backend" / "app.py"
    app_py.chmod(0o644)
    app_py.write_text("APP = 2\n", encoding="utf-8")
    app_py.chmod(0o444)

    with pytest.raises(SandboxError, match="frozen manifest"):
        verify_source_snapshot(snapshot)

    app_py.chmod(0o644)
    app_py.write_text("APP = 1\n", encoding="utf-8")
    app_py.chmod(0o444)
    snapshot.root.chmod(0o755)
    unexpected_directory = snapshot.root / "unexpected-empty-directory"
    unexpected_directory.mkdir()
    unexpected_directory.chmod(0o555)
    snapshot.root.chmod(0o555)
    with pytest.raises(SandboxError, match="frozen manifest"):
        verify_source_snapshot(snapshot)


def test_canonical_snapshot_allowlist_is_minimal_and_exact() -> None:
    paths = {path.as_posix() for path in CANONICAL_RUNTIME_SOURCE_PATHS}

    assert "backend" in paths
    assert "workflow" in paths
    assert "custom_tools/sql_tools.py" in paths
    assert "custom_tools/text_to_sql" in paths
    assert "html_utils.py" in paths
    assert "llm_call_context.py" in paths
    assert "workflow_pipelines/text_to_sql_pipeline.yaml" in paths
    assert "config/pii/categories.yaml" in paths
    assert "test_agent.py" not in paths
    assert "tests" not in paths
    assert "agent_profiles/manager.yaml" not in paths
    assert "tool_definitions/web_search_tool.yaml" not in paths
    assert {
        path
        for path in paths
        if path.startswith("custom_tools/text_to_sql/eval/official_evaluator_")
    } == {
        "custom_tools/text_to_sql/eval/official_evaluator_attempt.py",
        "custom_tools/text_to_sql/eval/official_evaluator_bridge.py",
        "custom_tools/text_to_sql/eval/official_evaluator_contracts.py",
        "custom_tools/text_to_sql/eval/official_evaluator_worker.py",
    }
    assert {
        "agent_profiles/schema_research_agent.yaml",
        "agent_profiles/sql_solver_agent.yaml",
    } <= paths


def test_official_evaluator_helper_changes_snapshot_and_release_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    evaluator_paths = tuple(
        path
        for path in CANONICAL_RUNTIME_SOURCE_PATHS
        if path.as_posix().startswith(
            "custom_tools/text_to_sql/eval/official_evaluator_"
        )
    )
    for path in evaluator_paths:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"SOURCE = {path.name!r}\n", encoding="utf-8")
    helper = source / "custom_tools/text_to_sql/eval/official_evaluator_attempt.py"
    first = create_source_snapshot(
        source, tmp_path / "first", allowed_paths=evaluator_paths
    )
    helper.write_text("SOURCE = 'changed'\n", encoding="utf-8")
    second = create_source_snapshot(
        source, tmp_path / "second", allowed_paths=evaluator_paths
    )

    helper_path = "custom_tools/text_to_sql/eval/official_evaluator_attempt.py"
    first_files = {item.relative_path: item for item in first.files}
    assert first_files[helper_path].mode == 0o444
    assert first.digest != second.digest
    manifest = source_snapshot_manifest(second)
    assert helper_path in {item["relative_path"] for item in manifest["files"]}
    lock = {
        "canonical_environment_digest": "sha256:environment",
        "model_identity_digest": "sha256:model",
    }
    identity = release_bundle_identity(
        lock=lock,
        snapshot=second,
        source_snapshot_manifest_digest="sha256:manifest",
        configuration_digest="sha256:configuration",
    )
    assert identity["release_lock_digest"] == json_digest(lock)
    assert identity["source_snapshot_digest"] == second.digest


def test_snapshot_excludes_cache_and_unlisted_profile_tool_and_config(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend" / "__pycache__").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    (source / "backend" / "__pycache__" / "app.pyc").write_bytes(b"cache")
    (source / "agent_profiles").mkdir()
    (source / "agent_profiles" / "nlu_agent.yaml").write_text(
        "enable: true\n", encoding="utf-8"
    )
    (source / "agent_profiles" / "manager.yaml").write_text(
        "enable: true\n", encoding="utf-8"
    )
    (source / "tool_definitions").mkdir()
    (source / "tool_definitions" / "schema_info.yaml").write_text(
        "name: schema_info\n", encoding="utf-8"
    )
    (source / "tool_definitions" / "web_search_tool.yaml").write_text(
        "name: web_search_tool\n", encoding="utf-8"
    )
    (source / "config" / "text_to_sql").mkdir(parents=True)
    (source / "config" / "text_to_sql" / "safety.yaml").write_text(
        "version: 1\n", encoding="utf-8"
    )
    (source / "config" / "unrelated.yaml").write_text(
        "enabled: true\n", encoding="utf-8"
    )
    (source / "test_runtime.py").write_text("assert False\n", encoding="utf-8")

    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(
            Path("backend"),
            Path("agent_profiles/nlu_agent.yaml"),
            Path("tool_definitions/schema_info.yaml"),
            Path("config/text_to_sql/safety.yaml"),
        ),
    )

    assert (snapshot.root / "backend" / "app.py").is_file()
    assert not (snapshot.root / "backend" / "__pycache__").exists()
    assert not (snapshot.root / "agent_profiles" / "manager.yaml").exists()
    assert not (snapshot.root / "tool_definitions" / "web_search_tool.yaml").exists()
    assert not (snapshot.root / "config" / "unrelated.yaml").exists()
    assert not (snapshot.root / "test_runtime.py").exists()


def test_snapshot_manifest_is_complete_hashed_and_read_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    regular = source / "backend" / "app.py"
    executable = source / "backend" / "start.sh"
    regular.write_text("APP = 1\n", encoding="utf-8")
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    manifest = source_snapshot_manifest(snapshot)

    assert manifest["source_snapshot_digest"] == snapshot.digest
    assert manifest["files"] == [
        {
            "relative_path": item.relative_path,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "mode": item.mode,
        }
        for item in snapshot.files
    ]
    assert manifest["tree_paths"] == list(snapshot.tree_paths)
    assert snapshot.root.stat().st_mode & 0o777 == 0o555
    assert (snapshot.root / "backend").stat().st_mode & 0o777 == 0o555
    assert (snapshot.root / "backend" / "app.py").stat().st_mode & 0o777 == 0o444
    assert (snapshot.root / "backend" / "start.sh").stat().st_mode & 0o777 == 0o555


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"extra": True}),
        lambda value: value.pop("tree_paths"),
        lambda value: value.__setitem__("schema_version", True),
        lambda value: value.__setitem__("schema_version", "1"),
        lambda value: value.__setitem__("schema_version", 1.0),
        lambda value: value.__setitem__("files", "not-a-list"),
        lambda value: value.__setitem__("files", ["not-a-mapping"]),
        lambda value: value["files"][0].__setitem__("size_bytes", False),
        lambda value: value["files"][0].__setitem__("size_bytes", "1"),
        lambda value: value["files"][0].__setitem__("size_bytes", 1.0),
        lambda value: value["files"][0].__setitem__("sha256", "A" * 64),
        lambda value: value["files"][0].__setitem__("relative_path", "../unsafe.py"),
        lambda value: value["files"][0].__setitem__("relative_path", "/unsafe.py"),
        lambda value: value["files"][0].__setitem__("mode", "292"),
        lambda value: value["files"][0].__setitem__("mode", True),
        lambda value: value["files"][0].__setitem__("mode", 0o644),
        lambda value: value.__setitem__("tree_paths", "not-a-list"),
        lambda value: value.__setitem__("tree_paths", ["wrong:backend"]),
        lambda value: value.__setitem__("tree_paths", ["file:../unsafe"]),
        lambda value: value.__setitem__(
            "tree_paths", ["file:backend/a", "directory:backend/a"]
        ),
        lambda value: value.__setitem__(
            "tree_paths", list(reversed(value["tree_paths"]))
        ),
    ),
)
def test_source_snapshot_manifest_parser_rejects_noncanonical_values(
    tmp_path: Path, mutate: object
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    manifest = copy.deepcopy(source_snapshot_manifest(snapshot))
    mutate(manifest)  # type: ignore[operator]

    with pytest.raises(SandboxError, match="manifest|inventory|canonical"):
        source_snapshot_from_manifest(snapshot.root, manifest)


def test_source_snapshot_manifest_parses_before_snapshot_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    manifest = source_snapshot_manifest(snapshot)
    manifest["schema_version"] = True

    def verification_must_not_run(_snapshot: object) -> None:
        raise AssertionError("strict parser must reject before verification")

    monkeypatch.setattr(
        sandbox_snapshot, "verify_source_snapshot", verification_must_not_run
    )
    with pytest.raises(SandboxError, match="schema version"):
        source_snapshot_from_manifest(snapshot.root, manifest)


def _simple_snapshot_manifest(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    return snapshot.root, copy.deepcopy(source_snapshot_manifest(snapshot))


def _with_tree_path(manifest: dict[str, object], path: str) -> None:
    tree_paths = list(manifest["tree_paths"])
    tree_paths.append(path)
    manifest["tree_paths"] = sorted(
        tree_paths,
        key=lambda item: item.split(":", 1)[1],
    )


def test_source_snapshot_manifest_rejects_file_without_tree_file_entry(
    tmp_path: Path,
) -> None:
    root, manifest = _simple_snapshot_manifest(tmp_path)
    manifest["tree_paths"] = [
        item for item in manifest["tree_paths"] if item != "file:backend/app.py"
    ]

    with pytest.raises(SandboxError, match="does not match tree"):
        source_snapshot_from_manifest(root, manifest)


def test_source_snapshot_manifest_rejects_unlisted_nonruntime_tree_file(
    tmp_path: Path,
) -> None:
    root, manifest = _simple_snapshot_manifest(tmp_path)
    _with_tree_path(manifest, "file:backend/unlisted.py")

    with pytest.raises(SandboxError, match="unlisted file"):
        source_snapshot_from_manifest(root, manifest)


def test_source_snapshot_manifest_rejects_missing_parent_directory(
    tmp_path: Path,
) -> None:
    root, manifest = _simple_snapshot_manifest(tmp_path)
    manifest["tree_paths"] = [
        item for item in manifest["tree_paths"] if item != "directory:backend"
    ]

    with pytest.raises(SandboxError, match="tree is inconsistent"):
        source_snapshot_from_manifest(root, manifest)


def test_source_snapshot_manifest_rejects_file_as_parent_directory(
    tmp_path: Path,
) -> None:
    root, manifest = _simple_snapshot_manifest(tmp_path)
    manifest["tree_paths"] = [
        "file:backend" if item == "directory:backend" else item
        for item in manifest["tree_paths"]
    ]
    files = list(manifest["files"])
    files.append(
        {
            "relative_path": "backend",
            "size_bytes": 0,
            "sha256": "0" * 64,
            "mode": 0o444,
        }
    )
    manifest["files"] = sorted(files, key=lambda item: item["relative_path"])

    with pytest.raises(SandboxError, match="tree is inconsistent"):
        source_snapshot_from_manifest(root, manifest)


def test_source_snapshot_manifest_accepts_empty_source_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend" / "empty").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )

    restored = source_snapshot_from_manifest(
        snapshot.root,
        source_snapshot_manifest(snapshot),
    )

    assert "directory:backend/empty" in restored.tree_paths


def test_source_snapshot_manifest_rejects_forged_runtime_directory_file(
    tmp_path: Path,
) -> None:
    root, manifest = _simple_snapshot_manifest(tmp_path)
    _with_tree_path(manifest, "file:memory/chromadb/forged")

    with pytest.raises(SandboxError, match="unlisted file"):
        source_snapshot_from_manifest(root, manifest)
