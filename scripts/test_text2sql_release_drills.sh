#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_PATH="${TEXT2SQL_DRILL_REPORT:-}"
MODE="${1:---deterministic}"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/text2sql-release-drills.XXXXXX")"
trap 'rm -rf "$STATE_DIR"' EXIT
cd "$PROJECT_ROOT"

if [[ "$MODE" == "--protected-rollback" ]]; then
  candidate_ref="${TEXT2SQL_CANDIDATE_REF:-}"
elif [[ -n "${TEXT2SQL_CANDIDATE_REF:-}" ]]; then
  candidate_ref="$TEXT2SQL_CANDIDATE_REF"
else
  candidate_ref="$(git rev-parse HEAD)"
fi
schema_ref="sha256:$(sha256sum config/text_to_sql/state_schema.yaml | awk '{print $1}')"
backup_manifest="${TEXT2SQL_BACKUP_MANIFEST:-}"
validated_backup_manifest_sha256=""

write_simple_report() {
  local status="$1"
  local reason_code="$2"
  local backup_ref="${TEXT2SQL_BACKUP_REF:-disposable-local}"
  local rollback_ref="${TEXT2SQL_ROLLBACK_REF:-not-executed-local}"
  local output="${REPORT_PATH:-$STATE_DIR/report.json}"
  mkdir -p "$(dirname "$output")"
  python3 - "$output" "$status" "$reason_code" "$candidate_ref" \
    "$schema_ref" "$backup_ref" "$rollback_ref" <<'PY'
import json
import pathlib
import sys

payload = {
    "status": sys.argv[2],
    "reason_code": sys.argv[3],
    "candidate_ref": sys.argv[4],
    "schema_ref": sys.argv[5],
    "backup_ref": sys.argv[6],
    "rollback_ref": sys.argv[7],
    "release_eligible": False,
    "drills": [],
}
rendered = json.dumps(payload, sort_keys=True)
pathlib.Path(sys.argv[1]).write_text(rendered + "\n", encoding="utf-8")
print(rendered)
PY
}

protected_rollback_preflight() {
  local required=(
    BACKEND_IMAGE
    FRONTEND_IMAGE
    TEXT2SQL_CANDIDATE_REF
    TEXT2SQL_PREVIOUS_BACKEND_IMAGE
    TEXT2SQL_PREVIOUS_FRONTEND_IMAGE
    TEXT2SQL_PUBLIC_API_URL
    TEXT2SQL_BACKUP_REF
    TEXT2SQL_BACKUP_MANIFEST
    TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256
    TEXT2SQL_DRILL_REPORT
  )
  local name
  for name in "${required[@]}"; do
    if [[ -z "${!name:-}" ]]; then
      write_simple_report "failed" "PROTECTED_ROLLBACK_REFS_MISSING"
      return 4
    fi
  done
  local current_image_pattern='^(sha256:[0-9a-f]{64}|[^[:space:]]+@sha256:[0-9a-f]{64})$'
  local previous_image_pattern='^[^[:space:]]+@sha256:[0-9a-f]{64}$'
  local candidate_pattern='^[0-9a-f]{64}$'
  local manifest_sha256_pattern='^[0-9a-f]{64}$'
  if [[ ! "$TEXT2SQL_CANDIDATE_REF" =~ $candidate_pattern ]] || \
     [[ ! "$BACKEND_IMAGE" =~ $current_image_pattern ]] || \
     [[ ! "$FRONTEND_IMAGE" =~ $current_image_pattern ]] || \
     [[ ! "$TEXT2SQL_PREVIOUS_BACKEND_IMAGE" =~ $previous_image_pattern ]] || \
     [[ ! "$TEXT2SQL_PREVIOUS_FRONTEND_IMAGE" =~ $previous_image_pattern ]]; then
    write_simple_report "failed" "PROTECTED_ROLLBACK_IMAGE_NOT_IMMUTABLE"
    return 4
  fi
  if [[ ! "$TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256" =~ $manifest_sha256_pattern ]]; then
    write_simple_report "failed" "PROTECTED_ROLLBACK_BACKUP_MANIFEST_INVALID"
    return 4
  fi
  if [[ ! -d "$TEXT2SQL_BACKUP_REF" ]]; then
    write_simple_report "failed" "PROTECTED_ROLLBACK_REFS_UNAVAILABLE"
    return 4
  fi
  if [[ ! -r "$backup_manifest" ]]; then
    write_simple_report "failed" "PROTECTED_ROLLBACK_BACKUP_MANIFEST_MISSING"
    return 4
  fi
  for database in agui_events.db smolagents_memory.db workflow_result_outbox.db; do
    if [[ ! -r "$TEXT2SQL_BACKUP_REF/$database" ]]; then
      write_simple_report "failed" "PROTECTED_ROLLBACK_BACKUP_INCOMPLETE"
      return 4
    fi
  done
  if ! validated_backup_manifest_sha256="$(
    python3 deploy/write_backup_manifest.py \
      --backup-reference "$TEXT2SQL_BACKUP_REF" \
      --candidate-digest "$candidate_ref" \
      --schema-ref "$schema_ref" \
      --expected-sha256 "$TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256" \
      --validate "$backup_manifest"
  )"; then
    write_simple_report "failed" "PROTECTED_ROLLBACK_BACKUP_MANIFEST_INVALID"
    return 4
  fi
  return 0
}

write_protected_report() {
  local status="$1"
  local reason_code="$2"
  local steps_path="$3"
  python3 - "$REPORT_PATH" "$status" "$reason_code" "$candidate_ref" \
    "$schema_ref" "$TEXT2SQL_BACKUP_REF" \
    "$TEXT2SQL_PREVIOUS_BACKEND_IMAGE" \
    "$TEXT2SQL_PREVIOUS_FRONTEND_IMAGE" \
    "$validated_backup_manifest_sha256" "$steps_path" <<'PY'
import json
import pathlib
import sys

drills = []
steps_path = pathlib.Path(sys.argv[10])
if steps_path.exists():
    for line in steps_path.read_text(encoding="utf-8").splitlines():
        name, status = line.split("\t", 1)
        drills.append({"name": name, "status": status})
payload = {
    "status": sys.argv[2],
    "reason_code": sys.argv[3],
    "candidate_ref": sys.argv[4],
    "schema_ref": sys.argv[5],
    "backup_ref": sys.argv[6],
    "rollback_ref": {
        "backend_image": sys.argv[7],
        "frontend_image": sys.argv[8],
    },
    "backup_manifest_sha256": sys.argv[9],
    "release_eligible": False,
    "drills": drills,
}
rendered = json.dumps(payload, sort_keys=True)
pathlib.Path(sys.argv[1]).write_text(rendered + "\n", encoding="utf-8")
print(rendered)
PY
}

protected_rollback() {
  local steps_path="$STATE_DIR/protected-rollback.tsv"
  protected_rollback_preflight || return $?
  local compose=(
    docker compose -p multiagent-text2sql
    -f deploy/compose.production.yml
  )

  run_rollback_step() {
    local name="$1"
    shift
    if "$@"; then
      printf '%s\tpassed\n' "$name" >>"$steps_path"
      return 0
    fi
    printf '%s\tfailed\n' "$name" >>"$steps_path"
    write_protected_report "failed" "PROTECTED_ROLLBACK_FAILED" "$steps_path"
    return 5
  }

  run_rollback_step backup_schema_validation \
    python3 scripts/migrate_text2sql_state.py --check-only \
      --event-store "$TEXT2SQL_BACKUP_REF/agui_events.db" \
      --memory-db "$TEXT2SQL_BACKUP_REF/smolagents_memory.db" \
      --result-outbox "$TEXT2SQL_BACKUP_REF/workflow_result_outbox.db" || return $?
  run_rollback_step previous_backend_pull \
    docker pull "$TEXT2SQL_PREVIOUS_BACKEND_IMAGE" || return $?
  run_rollback_step previous_frontend_pull \
    docker pull "$TEXT2SQL_PREVIOUS_FRONTEND_IMAGE" || return $?
  run_rollback_step candidate_stop \
    "${compose[@]}" stop api worker frontend || return $?

  cat >"$STATE_DIR/restore_sqlite.py" <<'PY'
import pathlib
import sqlite3

pairs = (
    ("/backup/agui_events.db", "/app/data/multiagent_state/agui_events.db"),
    ("/backup/smolagents_memory.db", "/var/lib/multiagent-memory/smolagents_memory.db"),
    ("/backup/workflow_result_outbox.db", "/app/data/multiagent_state/workflow_result_outbox.db"),
)
for source_name, target_name in pairs:
    target_path = pathlib.Path(target_name)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_name}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
PY
  run_rollback_step state_restore \
    "${compose[@]}" run --rm --no-deps \
      --volume "$TEXT2SQL_BACKUP_REF:/backup:ro" \
      --volume "$STATE_DIR:/drill:ro" \
      --entrypoint python3 migrate /drill/restore_sqlite.py || return $?

  export BACKEND_IMAGE="$TEXT2SQL_PREVIOUS_BACKEND_IMAGE"
  export FRONTEND_IMAGE="$TEXT2SQL_PREVIOUS_FRONTEND_IMAGE"
  run_rollback_step previous_images_start \
    "${compose[@]}" up -d --no-build --pull never --wait \
      api worker frontend || return $?
  run_rollback_step healthz \
    curl --fail --silent --show-error --max-time 10 \
      "http://127.0.0.1:${API_PORT:-8000}/healthz" || return $?
  run_rollback_step readyz \
    curl --fail --silent --show-error --max-time 10 \
      "http://127.0.0.1:${API_PORT:-8000}/readyz" || return $?

  write_protected_report "passed" "PROTECTED_ROLLBACK_OK" "$steps_path"
}

if [[ "$MODE" == "--protected-rollback" ]]; then
  set +e
  protected_rollback
  rollback_status=$?
  set -e
  exit "$rollback_status"
fi
if [[ "$MODE" != "--deterministic" ]]; then
  write_simple_report "failed" "DRILL_MODE_INVALID"
  exit 2
fi

export TMPDIR="$STATE_DIR"
junit_path="$STATE_DIR/drills.xml"
set +e
python3 -m pytest -q -W error --junitxml="$junit_path" \
  tests/test_workflow_process_supervisor.py::test_scheduler_passes_caps_and_spawns_with_run_id_and_persisted_spec \
  tests/test_workflow_process_supervisor.py::test_watcher_reaps_and_releases_capacity_once \
  tests/test_workflow_process_supervisor.py::test_deadline_sends_term_then_kill_and_finishes_timed_out \
  tests/test_workflow_process_supervisor.py::test_running_cancel_terminates_local_process_and_finishes_once \
  tests/test_workflow_supervisor_store.py::test_two_connections_cannot_race_past_global_cap \
  tests/test_text_to_sql_run_lifecycle.py::test_restart_reconciliation_preserves_live_pid_and_owner \
  tests/test_workflow_supervisor_integration.py::test_generic_crash_adopts_exact_pending_worker_result \
  tests/test_result_outbox_claims.py::test_exact_v2_migrates_to_v3_preserving_sequence_as_unclaimed \
  tests/test_text_to_sql_successful_sql_memory.py::test_writer_retriever_and_crash_window_repair_use_one_exact_id \
  tests/test_memory_rebuild_wave1b.py::test_rebuild_groups_duplicate_canonical_semantic_rows \
  tests/test_operational_retention.py::test_retention_lease_has_one_winner_and_persists_completion \
  tests/test_operational_retention.py::test_coordinator_runs_due_maintenance_and_records_exact_metrics \
  tests/test_t14_service_client_cutover.py::test_workflow_report_never_returns_legacy_cache_without_source \
  tests/test_text_to_sql_state_migrations.py::test_disposable_schema_backup_restore_and_rollback
pytest_status=$?
set -e

lock_leak=0
if find "$STATE_DIR" -type f -name '*.lock' -print -quit | grep -q .; then
  lock_leak=1
fi

output="${REPORT_PATH:-$STATE_DIR/report.json}"
mkdir -p "$(dirname "$output")"
python3 - "$junit_path" "$output" "$pytest_status" "$lock_leak" \
  "$candidate_ref" "$schema_ref" <<'PY'
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

groups = {
    "supervisor_admission_and_reaping": {
        "test_scheduler_passes_caps_and_spawns_with_run_id_and_persisted_spec",
        "test_watcher_reaps_and_releases_capacity_once",
        "test_deadline_sends_term_then_kill_and_finishes_timed_out",
        "test_running_cancel_terminates_local_process_and_finishes_once",
        "test_two_connections_cannot_race_past_global_cap",
    },
    "outbox_crash_and_restart": {
        "test_restart_reconciliation_preserves_live_pid_and_owner",
        "test_generic_crash_adopts_exact_pending_worker_result",
        "test_exact_v2_migrates_to_v3_preserving_sequence_as_unclaimed",
    },
    "memory_reconciliation_and_rebuild": {
        "test_writer_retriever_and_crash_window_repair_use_one_exact_id",
        "test_rebuild_groups_duplicate_canonical_semantic_rows",
    },
    "retention_and_report_fallback": {
        "test_retention_lease_has_one_winner_and_persists_completion",
        "test_coordinator_runs_due_maintenance_and_records_exact_metrics",
        "test_workflow_report_never_returns_legacy_cache_without_source",
    },
    "schema_backup_restore_rollback": {
        "test_disposable_schema_backup_restore_and_rollback",
    },
}
cases = {}
root = ET.parse(sys.argv[1]).getroot()
for case in root.iter("testcase"):
    case_name = case.attrib["name"].split("[", 1)[0]
    passed = not any(
        case.find(tag) is not None for tag in ("failure", "error", "skipped")
    )
    cases[case_name] = cases.get(case_name, True) and passed
drills = []
for name, expected in groups.items():
    passed = all(cases.get(case_name) is True for case_name in expected)
    drills.append({"name": name, "status": "passed" if passed else "failed"})
pytest_ok = int(sys.argv[3]) == 0
lock_ok = int(sys.argv[4]) == 0
passed = pytest_ok and lock_ok and all(item["status"] == "passed" for item in drills)
payload = {
    "status": "passed" if passed else "failed",
    "reason_code": "DRILL_OK" if passed else "DRILL_FAILED",
    "candidate_ref": sys.argv[5],
    "schema_ref": sys.argv[6],
    "backup_ref": "disposable-local",
    "rollback_ref": "not-executed-local",
    "release_eligible": False,
    "drills": drills,
}
rendered = json.dumps(payload, sort_keys=True)
pathlib.Path(sys.argv[2]).write_text(rendered + "\n", encoding="utf-8")
print(rendered)
sys.exit(0 if passed else 1)
PY
