from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_drill_script_is_fail_closed_and_local_only() -> None:
    script = PROJECT_ROOT / "scripts" / "test_text2sql_release_drills.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    source = script.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "|| true" not in source
    assert "mktemp" in source
    for contract in (
        "test_deadline_sends_term_then_kill_and_finishes_timed_out",
        "test_running_cancel_terminates_local_process_and_finishes_once",
        "test_watcher_reaps_and_releases_capacity_once",
        "test_scheduler_passes_caps_and_spawns_with_run_id_and_persisted_spec",
        "test_two_connections_cannot_race_past_global_cap",
        "test_generic_crash_adopts_exact_pending_worker_result",
        "test_retention_lease_has_one_winner_and_persists_completion",
        "test_restart_reconciliation_preserves_live_pid_and_owner",
        "test_writer_retriever_and_crash_window_repair_use_one_exact_id",
        "test_rebuild_groups_duplicate_canonical_semantic_rows",
        "test_coordinator_runs_due_maintenance_and_records_exact_metrics",
        "test_workflow_report_never_returns_legacy_cache_without_source",
        "test_disposable_schema_backup_restore_and_rollback",
    ):
        assert contract in source
    for field in (
        "candidate_ref",
        "schema_ref",
        "backup_ref",
        "rollback_ref",
        "drills",
    ):
        assert field in source


def test_protected_rollback_mode_fails_closed_without_immutable_refs(tmp_path) -> None:
    script = PROJECT_ROOT / "scripts" / "test_text2sql_release_drills.sh"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "BACKEND_IMAGE",
            "FRONTEND_IMAGE",
            "TEXT2SQL_CANDIDATE_REF",
            "TEXT2SQL_PUBLIC_API_URL",
            "TEXT2SQL_BACKUP_REF",
            "TEXT2SQL_ROLLBACK_REF",
            "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256",
        }
    }
    report = tmp_path / "rollback-report.json"
    environment["TEXT2SQL_DRILL_REPORT"] = str(report)

    completed = subprocess.run(
        ["bash", str(script), "--protected-rollback"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "PROTECTED_ROLLBACK_REFS_MISSING"


def test_protected_rollback_executes_compose_restore_and_health_contract(
    tmp_path,
) -> None:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    backup = tmp_path / "backup"
    paths = {
        "event_store": backup / "agui_events.db",
        "memory_db": backup / "smolagents_memory.db",
        "result_outbox": backup / "workflow_result_outbox.db",
    }
    migrate_text2sql_state(
        manifest_path=PROJECT_ROOT / "config/text_to_sql/state_schema.yaml",
        project_root=PROJECT_ROOT,
        store_paths=paths,
    )
    digest = "a" * 64
    schema_ref = "sha256:" + hashlib.sha256(
        (PROJECT_ROOT / "config/text_to_sql/state_schema.yaml").read_bytes()
    ).hexdigest()
    backup_manifest = tmp_path / "backup-manifest.json"
    subprocess.run(
        [
            "python3",
            str(PROJECT_ROOT / "deploy/write_backup_manifest.py"),
            "--backup-reference",
            str(backup),
            "--candidate-digest",
            digest,
            "--schema-ref",
            schema_ref,
            "--output",
            str(backup_manifest),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    approved_manifest_sha256 = hashlib.sha256(backup_manifest.read_bytes()).hexdigest()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    for command in ("docker", "curl"):
        executable = fake_bin / command
        executable.write_text(
            '#!/usr/bin/env bash\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" '
            '>>"$ROLLBACK_COMMAND_LOG"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)

    report = tmp_path / "rollback-report.json"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ROLLBACK_COMMAND_LOG": str(command_log),
        "BACKEND_IMAGE": f"sha256:{digest}",
        "FRONTEND_IMAGE": f"sha256:{digest}",
        "TEXT2SQL_CANDIDATE_REF": digest,
        "TEXT2SQL_PREVIOUS_BACKEND_IMAGE": f"registry/backend@sha256:{'b' * 64}",
        "TEXT2SQL_PREVIOUS_FRONTEND_IMAGE": f"registry/frontend@sha256:{'c' * 64}",
        "TEXT2SQL_PUBLIC_API_URL": "https://text2sql.example/api",
        "TEXT2SQL_BACKUP_REF": str(backup),
        "TEXT2SQL_BACKUP_MANIFEST": str(backup_manifest),
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256": approved_manifest_sha256,
        "TEXT2SQL_DRILL_REPORT": str(report),
    }

    completed = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts/test_text2sql_release_drills.sh"),
            "--protected-rollback",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["reason_code"] == "PROTECTED_ROLLBACK_OK"
    assert payload["backup_manifest_sha256"] == hashlib.sha256(
        backup_manifest.read_bytes()
    ).hexdigest()
    assert [item["status"] for item in payload["drills"]] == ["passed"] * 8
    rollback_body = (
        PROJECT_ROOT / "scripts/test_text2sql_release_drills.sh"
    ).read_text(encoding="utf-8").split("protected_rollback() {", 1)[1]
    assert rollback_body.index("protected_rollback_preflight || return $?") < (
        rollback_body.index("run_rollback_step candidate_stop")
    ) < rollback_body.index("run_rollback_step state_restore")
    commands = command_log.read_text(encoding="utf-8")
    assert f"docker pull registry/backend@sha256:{'b' * 64}" in commands
    assert f"docker pull registry/frontend@sha256:{'c' * 64}" in commands
    assert "stop api worker frontend" in commands
    assert f"{backup}:/backup:ro" in commands
    assert "up -d --no-build --pull never --wait api worker frontend" in commands
    assert "/healthz" in commands
    assert "/readyz" in commands


@pytest.mark.parametrize(
    "invalid_manifest",
    ("missing", "wrong_candidate", "wrong_schema", "empty_databases", "tampered_db"),
)
def test_protected_rollback_rejects_untrusted_backup_manifest_before_stop(
    tmp_path,
    invalid_manifest,
) -> None:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    backup = tmp_path / "backup"
    migrate_text2sql_state(
        manifest_path=PROJECT_ROOT / "config/text_to_sql/state_schema.yaml",
        project_root=PROJECT_ROOT,
        store_paths={
            "event_store": backup / "agui_events.db",
            "memory_db": backup / "smolagents_memory.db",
            "result_outbox": backup / "workflow_result_outbox.db",
        },
    )
    candidate_digest = "a" * 64
    schema_ref = "sha256:" + hashlib.sha256(
        (PROJECT_ROOT / "config/text_to_sql/state_schema.yaml").read_bytes()
    ).hexdigest()
    backup_manifest = tmp_path / "backup-manifest.json"
    if invalid_manifest != "missing":
        subprocess.run(
            [
                "python3",
                str(PROJECT_ROOT / "deploy/write_backup_manifest.py"),
                "--backup-reference",
                str(backup),
                "--candidate-digest",
                candidate_digest,
                "--schema-ref",
                schema_ref,
                "--output",
                str(backup_manifest),
            ],
            cwd=PROJECT_ROOT,
            check=True,
        )
        if invalid_manifest == "tampered_db":
            connection = sqlite3.connect(backup / "agui_events.db")
            try:
                connection.execute("CREATE TABLE manifest_tamper(value TEXT)")
                connection.commit()
            finally:
                connection.close()
        else:
            payload = json.loads(backup_manifest.read_text(encoding="utf-8"))
            if invalid_manifest == "wrong_candidate":
                payload["candidate_digest"] = "b" * 64
            elif invalid_manifest == "wrong_schema":
                payload["schema_ref"] = f"sha256:{'c' * 64}"
            else:
                payload["databases"] = []
            backup_manifest.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    approved_manifest_sha256 = (
        hashlib.sha256(backup_manifest.read_bytes()).hexdigest()
        if backup_manifest.is_file()
        else "d" * 64
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    for command in ("docker", "curl"):
        executable = fake_bin / command
        executable.write_text(
            '#!/usr/bin/env bash\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" '
            '>>"$ROLLBACK_COMMAND_LOG"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    report = tmp_path / "rollback-report.json"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ROLLBACK_COMMAND_LOG": str(command_log),
        "BACKEND_IMAGE": f"sha256:{candidate_digest}",
        "FRONTEND_IMAGE": f"sha256:{candidate_digest}",
        "TEXT2SQL_CANDIDATE_REF": candidate_digest,
        "TEXT2SQL_PREVIOUS_BACKEND_IMAGE": f"registry/backend@sha256:{'b' * 64}",
        "TEXT2SQL_PREVIOUS_FRONTEND_IMAGE": f"registry/frontend@sha256:{'c' * 64}",
        "TEXT2SQL_PUBLIC_API_URL": "https://text2sql.example/api",
        "TEXT2SQL_BACKUP_REF": str(backup),
        "TEXT2SQL_BACKUP_MANIFEST": str(backup_manifest),
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256": approved_manifest_sha256,
        "TEXT2SQL_DRILL_REPORT": str(report),
    }

    completed = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts/test_text2sql_release_drills.sh"),
            "--protected-rollback",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["reason_code"] in {
        "PROTECTED_ROLLBACK_BACKUP_MANIFEST_MISSING",
        "PROTECTED_ROLLBACK_BACKUP_MANIFEST_INVALID",
    }
    assert not command_log.exists() or command_log.read_text(encoding="utf-8") == ""

    source = (
        PROJECT_ROOT / "scripts/test_text2sql_release_drills.sh"
    ).read_text(encoding="utf-8")
    expected_digest = source.index(
        '--expected-sha256 "$TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256"'
    )
    validate = source.index('--validate "$backup_manifest"')
    assert expected_digest < validate < source.index(
        "run_rollback_step candidate_stop"
    ) < source.index("run_rollback_step state_restore")


def test_backup_manifest_validator_rejects_changed_approved_bytes(tmp_path) -> None:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    backup = tmp_path / "backup"
    migrate_text2sql_state(
        manifest_path=PROJECT_ROOT / "config/text_to_sql/state_schema.yaml",
        project_root=PROJECT_ROOT,
        store_paths={
            "event_store": backup / "agui_events.db",
            "memory_db": backup / "smolagents_memory.db",
            "result_outbox": backup / "workflow_result_outbox.db",
        },
    )
    candidate_digest = "a" * 64
    schema_ref = "sha256:" + hashlib.sha256(
        (PROJECT_ROOT / "config/text_to_sql/state_schema.yaml").read_bytes()
    ).hexdigest()
    backup_manifest = tmp_path / "backup-manifest.json"
    command = [
        "python3",
        str(PROJECT_ROOT / "deploy/write_backup_manifest.py"),
        "--backup-reference",
        str(backup),
        "--candidate-digest",
        candidate_digest,
        "--schema-ref",
        schema_ref,
    ]
    subprocess.run(
        [*command, "--output", str(backup_manifest)],
        cwd=PROJECT_ROOT,
        check=True,
    )
    approved_sha256 = hashlib.sha256(backup_manifest.read_bytes()).hexdigest()
    payload = json.loads(backup_manifest.read_text(encoding="utf-8"))
    backup_manifest.write_text(
        json.dumps(payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert hashlib.sha256(backup_manifest.read_bytes()).hexdigest() != approved_sha256

    completed = subprocess.run(
        [
            *command,
            "--expected-sha256",
            approved_sha256,
            "--validate",
            str(backup_manifest),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "backup manifest digest does not match approved digest" in completed.stderr


def test_release_workflow_only_validates_external_approved_backup_manifest() -> None:
    workflow = PROJECT_ROOT / ".github/workflows/text2sql-release.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "approved_backup_manifest_path:" in source
    assert "approved_backup_manifest_sha256:" in source
    assert (
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH: "
        "${{ inputs.approved_backup_manifest_path || "
        "vars.TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH }}"
    ) in source
    assert (
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256: "
        "${{ inputs.approved_backup_manifest_sha256 || "
        "vars.TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256 }}"
    ) in source
    assert (
        ': "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH:'
        "?approved backup manifest path is required}"
    ) in source
    assert (
        ': "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256:'
        "?approved backup manifest SHA-256 is required}"
    ) in source
    prepare_start = source.index("- name: Prepare immutable candidate inputs")
    build = source.index("- name: Build and verify reproducible candidate images")
    prepare_step = source[prepare_start:build]
    assert 'case "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}" in' in source
    assert "grep -Eq '^[0-9a-f]{64}$'" in prepare_step
    assert "realpath -e --" in prepare_step
    assert 'workspace_path=$(realpath -e -- "${GITHUB_WORKSPACE}")' in prepare_step
    assert "approved backup manifest must be outside GITHUB_WORKSPACE" in prepare_step
    assert 'test -f "${approved_backup_manifest_path}"' in prepare_step
    assert 'test -r "${approved_backup_manifest_path}"' in prepare_step
    assert 'test ! -w "${approved_backup_manifest_path}"' in prepare_step
    source_digest = prepare_step.index(
        "approved_backup_manifest_sha256=$(sha256sum --"
    )
    digest_match = prepare_step.index(
        'test "${approved_backup_manifest_sha256}" ='
    )
    assert source_digest < digest_match
    assert "TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH=%s" in prepare_step

    manifest_start = source.index(
        "- name: Validate and preserve approved backup manifest"
    )
    manifest_end = source.index("\n      - name:", manifest_start)
    manifest_step = source[manifest_start:manifest_end]
    copy = manifest_step.index("install --mode=0600 --")
    copied_digest = manifest_step.index(
        "copied_backup_manifest_sha256=$(sha256sum --"
    )
    copied_digest_match = manifest_step.index(
        'test "${copied_backup_manifest_sha256}" ='
    )
    validate = manifest_step.index("--validate artifacts/protected/backup-manifest.json")

    assert build < manifest_start
    assert copy < copied_digest < copied_digest_match < validate
    assert '"${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"' in manifest_step
    assert "artifacts/protected/backup-manifest.json" in manifest_step
    assert '--backup-reference "${BACKUP_REFERENCE}"' in manifest_step
    assert '--candidate-digest "${CANDIDATE_DIGEST}"' in manifest_step
    assert '--schema-ref "${schema_ref}"' in manifest_step
    assert (
        '--expected-sha256 "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"'
        in manifest_step
    )
    assert "--output" not in manifest_step
    for later_step in (
        "- name: Probe required real drivers",
        "- name: Start and verify exact candidate runtime",
        "- name: Run release drills",
        "- name: Write release evidence index",
    ):
        assert manifest_start < source.index(later_step)
    rollback_start = source.index("- name: Run release drills")
    evidence_start = source.index("- name: Write release evidence index")
    assert (
        "TEXT2SQL_BACKUP_MANIFEST: artifacts/protected/backup-manifest.json"
        in source[rollback_start:evidence_start]
    )
    assert (
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256: "
        "${{ env.TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256 }}"
        in source[rollback_start:evidence_start]
    )
    assert (
        "--backup-manifest artifacts/protected/backup-manifest.json"
        in source[evidence_start:]
    )


def test_production_runbook_contains_exact_operations_and_honest_support_matrix() -> None:
    runbook = PROJECT_ROOT / "docs" / "operations" / "text2sql-production.md"
    source = runbook.read_text(encoding="utf-8")

    for heading in (
        "## Deploy",
        "## Migrate",
        "## Backup",
        "## Restore",
        "## Health and readiness",
        "## Rollback",
        "## Pre-approval backup-custodian step",
        "## Controlled protected release ceremony",
        "## Support matrix",
        "## Protected release gates",
    ):
        assert heading in source
    rollback_start = source.index("## Rollback")
    rollback_end = source.index(
        "## Pre-approval backup-custodian step",
        rollback_start,
    )
    rollback_section = source[rollback_start:rollback_end]
    for rollback_export in (
        'export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"',
        "export TEXT2SQL_BACKUP_MANIFEST="
        "'artifacts/protected/backup-manifest.json'",
        'export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="'
        "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256:"
        '?custodian-approved manifest digest is required}"',
    ):
        assert rollback_export in rollback_section
    assert "scripts/migrate_text2sql_state.py" in source
    assert "/healthz" in source and "/readyz" in source
    assert "EventStore 7" in source
    assert "memory DB 1" in source
    assert "result outbox 3" in source
    assert "UNVERIFIED" in source
    assert "must not" in source
    for exact in (
        "deploy/compose.production.yml",
        "multiagent-text2sql",
        "BACKEND_IMAGE",
        "FRONTEND_IMAGE",
        "TEXT2SQL_PUBLIC_API_URL",
        "@sha256:",
        "--protected-rollback",
    ):
        assert exact in source

    for required_input in (
        "TEXT2SQL_RELEASE_RUNNER_LABEL",
        "text2sql-release",
        "BACKEND_OCI_DIGEST",
        "FRONTEND_OCI_DIGEST",
        "CANDIDATE_DIGEST",
        "LOCK_DIGEST",
        "TEXT2SQL_PREVIOUS_BACKEND_IMAGE",
        "TEXT2SQL_PREVIOUS_FRONTEND_IMAGE",
        "TEXT2SQL_BACKUP_REF",
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH",
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256",
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES",
        "TEXT_TO_SQL_ALLOWED_DB_TARGETS",
        "TEXT_TO_SQL_ALLOWED_DB_CIDRS",
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS",
        "TEXT2SQL_EVAL_API_TOKEN",
        "HF_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_DB",
        "TEXT2SQL_FIXTURE_DSN_MAP_JSON",
        "TEXT2SQL_POSTGRES_DSN",
        "TEXT2SQL_MYSQL_DSN",
        "MODEL_ID",
        "PROVIDER_ID",
        "MODEL_CONFIG_DIGEST",
        "TEXT2SQL_LATENCY_BASELINE_ARTIFACT",
    ):
        assert required_input in source

    for database in (
        "agui_events.db",
        "smolagents_memory.db",
        "workflow_result_outbox.db",
    ):
        assert database in source

    assert "self-hosted runner" in source
    assert "HTTPS public origin" in source
    assert "TextToSqlApiClient" in source
    assert "register_connection" in source
    assert '"${BACKEND_IMAGE}" -m pytest' in source
    assert "write_connection_target_policy_artifact" in source
    assert (
        'write_connection_target_policy_artifact(\n'
        '    "artifacts/build/target-policy.json",\n'
        "    os.environ,\n"
        ")"
    ) in source
    assert (
        'export TEXT2SQL_TARGET_POLICY_SHA256="$(sha256sum '
        "artifacts/build/target-policy.json | cut -d ' ' -f 1)\""
    ) in source
    assert "export TEXT2SQL_ENABLED_PROBE_LANES=postgres,mysql" in source
    assert 'export TEXT2SQL_PROBE_POSTGRES_DSN="${TEXT2SQL_POSTGRES_DSN}"' in source
    assert 'export TEXT2SQL_PROBE_MYSQL_DSN="${TEXT2SQL_MYSQL_DSN}"' in source
    assert (
        '"target_policy_sha256": '
        'os.environ["TEXT2SQL_TARGET_POLICY_SHA256"],'
    ) in source
    assert "python3 deploy/write_backup_manifest.py" in source
    assert (
        'export TEXT2SQL_SCHEMA_REF="sha256:$(sha256sum '
        "config/text_to_sql/state_schema.yaml | cut -d ' ' -f 1)\""
    ) in source
    assert (
        "export TEXT2SQL_BACKUP_MANIFEST="
        "artifacts/protected/backup-manifest.json"
    ) in source
    assert '--candidate-digest "${CANDIDATE_DIGEST}"' in source
    assert '--schema-ref "${TEXT2SQL_SCHEMA_REF}"' in source
    custodian_start = source.index("## Pre-approval backup-custodian step")
    ceremony_start = source.index("## Controlled protected release ceremony")
    support_start = source.index("## Support matrix")
    custodian = source[custodian_start:ceremony_start]
    ceremony = source[ceremony_start:support_start]
    assert "outside the protected release ceremony" in custodian
    assert '--output "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"' in custodian
    assert '--output "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"' not in ceremony
    assert "chmod 0440 \"${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}\"" in custodian
    assert 'test ! -w "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"' in custodian
    assert (
        'export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="$(sha256sum --'
        in custodian
    )
    assert "grep -Eq '^[0-9a-f]{64}$'" in ceremony
    assert "export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256" in ceremony
    assert "approved backup manifest must be outside the checkout" in ceremony
    assert 'test ! -w "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"' in ceremony
    backup_validation_start = ceremony.index(
        "python3 deploy/write_backup_manifest.py"
    )
    backup_validation_end = ceremony.index("\n```", backup_validation_start)
    backup_validation = ceremony[backup_validation_start:backup_validation_end]
    assert "--output" not in backup_validation
    assert (
        '--expected-sha256 "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"'
        in backup_validation
    )
    assert '--validate "${TEXT2SQL_BACKUP_MANIFEST}"' in backup_validation
    copy = ceremony.index("install --mode=0600 --")
    copied_digest = ceremony.index(
        'sha256sum -- "${TEXT2SQL_BACKUP_MANIFEST}"', copy
    )
    validate = ceremony.index('--validate "${TEXT2SQL_BACKUP_MANIFEST}"')
    runtime = ceremony.index("Start the exact candidate")
    rollback = ceremony.index("Exercise the protected rollback")
    assert copy < copied_digest < validate < runtime < rollback
    assert "artifacts/deterministic-drills.json" in source
    assert "artifacts/build/target-policy.json" in source
    assert "artifacts/protected/backup-manifest.json" in source
    assert "artifacts/protected/real-driver-evidence.json" in source
    assert "artifacts/text2sql-eval-release.json" in source
    assert "artifacts/protected-rollback.json" in source
    assert "artifacts/protected-gates.json" in source
    assert (
        "export TEXT2SQL_DRILL_REPORT=artifacts/deterministic-drills.json\n"
        'export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"\n'
        "bash scripts/test_text2sql_release_drills.sh --deterministic"
    ) in source
    assert (
        "export TEXT2SQL_DRILL_REPORT=artifacts/protected-rollback.json\n"
        'export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"\n'
        "export TEXT2SQL_BACKUP_MANIFEST='artifacts/protected/backup-manifest.json'\n"
        'export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="'
        "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256:"
        '?custodian-approved manifest digest is required}"\n'
        "bash scripts/test_text2sql_release_drills.sh --protected-rollback"
    ) in source
    assert "UNVERIFIED locally" in source
    assert "pending approval" in source

    for release_argument in (
        "--gold tests/eval/gold",
        "--policy config/text_to_sql/eval_release.yaml",
        "--mode release",
        "--repetitions",
        "--commit",
        "--candidate-digest",
        "--lock-digest",
        "--model-id",
        "--provider-id",
        "--model-config-digest",
        "--latency-baseline",
        "--target-policy artifacts/build/target-policy.json",
        "--protected-evidence",
        "--connection-map",
        "--api-url",
        "--auth-token-env",
        "--output artifacts/text2sql-eval-release.json",
    ):
        assert release_argument in source

    writer_start = source.index("python3 deploy/write_release_evidence.py")
    writer_command = source[writer_start : source.index("\n```", writer_start)]
    for evidence_argument in (
        "--artifacts artifacts",
        '--dirty-tree false',
        "--lock requirements.lock",
        "--lock requirements-db-optional.lock",
        "--lock frontend/client/package-lock.json",
        "--migration-manifest config/text_to_sql/state_schema.yaml",
        "--model-id",
        "--provider-id",
        "--model-config-digest",
        "--latency-baseline",
        "--target-policy artifacts/build/target-policy.json",
        "--previous-backend-image",
        "--previous-frontend-image",
        "--backup-reference",
        "--backup-manifest artifacts/protected/backup-manifest.json",
        "--output artifacts/protected-gates.json",
    ):
        assert evidence_argument in writer_command
