# Text-to-SQL production operations

This runbook covers the T15 release boundary. Commands must be executed from
the repository root against immutable candidate artifacts. Local deterministic
tests are not evidence that an external database, model, image, or driver lane
is production-ready.

## Deploy

1. Record the candidate commit and immutable image references. On the
   controlled release runner, `BACKEND_IMAGE` and `FRONTEND_IMAGE` are the raw
   `sha256:<64-hex>` runtime IDs emitted in
   `artifacts/build/candidate-metadata.json`. Previous images used for rollback
   must be pullable, digest-qualified registry references:

   ```bash
   export BACKEND_IMAGE='sha256:<64-hex>'
   export FRONTEND_IMAGE='sha256:<64-hex>'
   export TEXT2SQL_PUBLIC_API_URL='https://text2sql.example/api'
   export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
   export NEXT_BUILD_ID="$(git rev-parse HEAD)"
   ```

   Reject a dirty checkout, mutable candidate tags, and mutable previous-image
   references.
2. Back up all three SQLite stores as described below.
3. Provide readable secret files through `AG_UI_AUTH_TOKEN_MAP_FILE`,
   `HF_TOKEN_FILE`, `OPENAI_API_KEY_FILE`, and `OPENAI_API_KEY_DB_FILE`.
   Never put their values in Compose YAML or evidence JSON.
4. Validate Compose and run the B-owned `migrate` service:

   ```bash
   docker compose -p multiagent-text2sql \
     -f deploy/compose.production.yml config --quiet
   docker compose -p multiagent-text2sql \
     -f deploy/compose.production.yml run --rm migrate
   ```

5. Start the exact `api`, `worker`, and `frontend` services:

   ```bash
   docker compose -p multiagent-text2sql \
     -f deploy/compose.production.yml up -d api worker frontend
   ```

6. Require `/healthz` and `/readyz` to pass before opening admission.

Never run development launchers, generate migrations at container startup, or
silently continue after a migration/readiness failure.

## Migrate

The authoritative migration heads are EventStore 7, memory DB 1, and workflow
result outbox 3. `config/text_to_sql/state_schema.yaml` records those heads.
The runner invokes each store's owning initializer; it does not contain a
second DDL or Alembic history.

Use explicit paths for a staged/disposable migration:

```bash
python3 scripts/migrate_text2sql_state.py \
  --event-store /state/agui_events.db \
  --memory-db /state/smolagents_memory.db \
  --result-outbox /state/workflow_result_outbox.db
```

A future head is a hard failure. Do not downgrade it in place.

## Backup

Stop new admission and let active workers drain or cancel. Back up each SQLite
database through SQLite's online backup API, not by copying a live `.db` file:

```bash
python3 - "$SOURCE_DB" "$BACKUP_DB" <<'PY'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
```

Run it for `agui_events.db`, `smolagents_memory.db`, and
`workflow_result_outbox.db`; record the candidate digest and schema heads with
the backup. Verify the backup with `--check-only` using explicit paths.

## Restore

Keep admission closed. Stop API and worker processes, preserve the failed
volume for forensics, and restore all three databases from one consistent
backup set using the same SQLite backup API with source and target reversed.
Then run:

```bash
python3 scripts/migrate_text2sql_state.py --check-only \
  --event-store /state/agui_events.db \
  --memory-db /state/smolagents_memory.db \
  --result-outbox /state/workflow_result_outbox.db
```

Do not mix stores from different backup timestamps.

## Health and readiness

- `GET /healthz` is liveness only and returns `{"status":"ok"}`.
- `GET /readyz` returns HTTP 200 only with `status=ready`; HTTP 503 and
  `status=not_ready` means admission must remain closed.
- Readiness requires writable/current heads, a running supervisor that accepts
  admission, valid required configuration, a running retention lifecycle, and
  every lane in `TEXT2SQL_ENABLED_PROBE_LANES` to pass its live T12 probe.
- Configure a lane DSN as `TEXT2SQL_PROBE_<DIALECT>_DSN`. Responses expose only
  stable reason codes and must not expose the DSN or driver exception.

Exact checks:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
curl --fail --silent --show-error http://127.0.0.1:8000/readyz
```

## Rollback

Required protected inputs are immutable candidate `BACKEND_IMAGE` and
`FRONTEND_IMAGE` (either raw local Docker IDs `sha256:<64-hex>` or
digest-qualified names), strictly digest-qualified
`TEXT2SQL_PREVIOUS_BACKEND_IMAGE` and
`TEXT2SQL_PREVIOUS_FRONTEND_IMAGE`, mandatory `TEXT2SQL_PUBLIC_API_URL`, a
readable `TEXT2SQL_BACKUP_REF` directory containing `agui_events.db`,
`smolagents_memory.db`, and `workflow_result_outbox.db`, plus a writable
`TEXT2SQL_DRILL_REPORT` path, the current `CANDIDATE_DIGEST`, and the approved
manifest copied to `artifacts/protected/backup-manifest.json` with its separately
approved `TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256`. Missing or mutable
references fail with exit 4.

The protected command verifies both previous immutable images with explicit
`docker pull` steps before downtime, stops candidate services without deleting
volumes, restores the three databases through the candidate backend `migrate`
service with the backup mounted read-only, starts the previous images using
`--no-build --pull never --wait`, and verifies local liveness/readiness:

```bash
export TEXT2SQL_PREVIOUS_BACKEND_IMAGE='registry.example/backend@sha256:<64-hex>'
export TEXT2SQL_PREVIOUS_FRONTEND_IMAGE='registry.example/frontend@sha256:<64-hex>'
export TEXT2SQL_BACKUP_REF='/secure/backups/release-previous'
export TEXT2SQL_DRILL_REPORT='artifacts/protected-rollback.json'
export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"
export TEXT2SQL_BACKUP_MANIFEST='artifacts/protected/backup-manifest.json'
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256:?custodian-approved manifest digest is required}"
bash scripts/test_text2sql_release_drills.sh --protected-rollback
```

Controlled rollback procedure:

1. Close admission and cancel/drain through the supervisor.
2. Save the current failed volumes and evidence, then stop the B-owned services:

   ```bash
   docker compose -p multiagent-text2sql \
     -f deploy/compose.production.yml down
   ```

3. The rollback script restores the complete pre-upgrade backup; do not mutate
   `PRAGMA user_version` manually or copy live SQLite files directly.
4. Preserve its per-step report with candidate/schema/backup/previous-image
   references in the protected evidence index.
5. Require a subsequent authenticated read-only smoke request before reopening
   admission.

## Pre-approval backup-custodian step

This step is outside the protected release ceremony. A backup custodian creates
the runner-local backup set and its candidate-bound manifest before release
approval. The manifest output path must be absolute, access-controlled, and
different from the release evidence path. The release runner receives read-only
access to the approved file; it must never generate or overwrite that source.

Close admission, drain or cancel active work, stop the current services, and
create one runner-local backup set. All three databases are backed up through
SQLite's online backup API. Generate the manifest only after all three databases
pass the schema check:

```bash
export TEXT2SQL_BACKUP_REF='/secure/backups/release-previous'
export CANDIDATE_DIGEST='<64-hex-candidate-digest>'
export TEXT2SQL_SCHEMA_REF="sha256:$(sha256sum config/text_to_sql/state_schema.yaml | cut -d ' ' -f 1)"
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH='/secure/approved-manifests/text2sql-backup.json'
docker compose -p multiagent-text2sql -f deploy/compose.production.yml \
  stop api worker frontend
sudo install -d -o 10001 -g 10001 -m 0750 "${TEXT2SQL_BACKUP_REF}"
test -z "$(find "${TEXT2SQL_BACKUP_REF}" -mindepth 1 -maxdepth 1 -print -quit)"
docker compose -p multiagent-text2sql -f deploy/compose.production.yml \
  run --rm --no-deps \
  --volume "${TEXT2SQL_BACKUP_REF}:/backup" \
  --entrypoint python3 migrate - <<'PY'
import sqlite3

pairs = (
    ("/app/data/multiagent_state/agui_events.db", "/backup/agui_events.db"),
    ("/var/lib/multiagent-memory/smolagents_memory.db", "/backup/smolagents_memory.db"),
    ("/app/data/multiagent_state/workflow_result_outbox.db", "/backup/workflow_result_outbox.db"),
)
for source_name, backup_name in pairs:
    source = sqlite3.connect(f"file:{source_name}?mode=ro", uri=True)
    backup = sqlite3.connect(backup_name)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
PY
sudo chown -R "$(id -u):10001" "${TEXT2SQL_BACKUP_REF}"
chmod 0750 "${TEXT2SQL_BACKUP_REF}"
chmod 0640 "${TEXT2SQL_BACKUP_REF}"/*.db
python3 scripts/migrate_text2sql_state.py --check-only \
  --event-store "${TEXT2SQL_BACKUP_REF}/agui_events.db" \
  --memory-db "${TEXT2SQL_BACKUP_REF}/smolagents_memory.db" \
  --result-outbox "${TEXT2SQL_BACKUP_REF}/workflow_result_outbox.db"
python3 deploy/write_backup_manifest.py \
  --backup-reference "${TEXT2SQL_BACKUP_REF}" \
  --candidate-digest "${CANDIDATE_DIGEST}" \
  --schema-ref "${TEXT2SQL_SCHEMA_REF}" \
  --output "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="$(sha256sum -- \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}" | cut -d ' ' -f 1)"
chmod 0440 "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
test -r "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
test ! -w "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
printf 'approved backup manifest sha256: %s\n' \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"
```

The custodian records `TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256` and approval
separately, then supplies that exact lowercase digest to the protected
environment. Once approved, the non-writable source bytes and the three
referenced backup files are immutable for the release ceremony.

## Controlled protected release ceremony

Status: the external protected gates are **UNVERIFIED locally** and are
**pending approval**. A deterministic local pass is not a production approval.
Run this ceremony only from a clean checkout on a controlled self-hosted runner
registered with the `text2sql-release` label, with an authenticated container
registry session and access to the protected databases and model provider. Do
not run it on a developer workstation or a generic hosted runner.

The following blocks are one fail-closed Bash session, executed from the
repository root. The runner secret store must inject the values; do not paste
secret values into the runbook, command history, Compose YAML, or evidence.
`TEXT2SQL_BACKUP_REF` must be an absolute local directory on this runner, not a
remote URI. `TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH` must be the absolute
runner-local path to the pre-created and approved source manifest; the ceremony
only validates and copies its exact bytes.
`TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256` must be the separately approved
lowercase SHA-256 recorded by the custodian. `TEXT2SQL_FIXTURE_DSN_MAP_JSON` is
a secret JSON object mapping every release fixture name to its DSN. The
generated connection map contains only opaque connection references.

Preflight every required input:

```bash
set -euo pipefail
umask 077

: "${TEXT2SQL_RELEASE_RUNNER_LABEL:?runner label is required}"
test "${TEXT2SQL_RELEASE_RUNNER_LABEL}" = text2sql-release
test -z "$(git status --porcelain)"

export GITHUB_SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git show -s --format=%ct HEAD)}"
export NEXT_BUILD_ID="${NEXT_BUILD_ID:-${GITHUB_SHA}}"
export EVAL_API_URL="${EVAL_API_URL:-http://127.0.0.1:8000}"
export RELEASE_REPETITIONS="${RELEASE_REPETITIONS:-3}"
export TEXT2SQL_ENABLED_PROBE_LANES=postgres,mysql

required_inputs=(
  BACKEND_IMAGE
  FRONTEND_IMAGE
  BACKEND_OCI_DIGEST
  FRONTEND_OCI_DIGEST
  CANDIDATE_DIGEST
  LOCK_DIGEST
  TEXT2SQL_PREVIOUS_BACKEND_IMAGE
  TEXT2SQL_PREVIOUS_FRONTEND_IMAGE
  TEXT2SQL_BACKUP_REF
  TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH
  TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256
  TEXT2SQL_PUBLIC_API_URL
  TEXT_TO_SQL_ALLOWED_DB_SCHEMES
  TEXT_TO_SQL_ALLOWED_DB_TARGETS
  TEXT_TO_SQL_ALLOWED_DB_CIDRS
  TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS
  TEXT2SQL_EVAL_API_TOKEN
  HF_TOKEN
  OPENAI_API_KEY
  OPENAI_API_KEY_DB
  TEXT2SQL_FIXTURE_DSN_MAP_JSON
  TEXT2SQL_POSTGRES_DSN
  TEXT2SQL_MYSQL_DSN
  MODEL_ID
  PROVIDER_ID
  MODEL_CONFIG_DIGEST
  TEXT2SQL_LATENCY_BASELINE_ARTIFACT
)
for name in "${required_inputs[@]}"; do
  test -n "${!name:-}" || { echo "missing required input: ${name}" >&2; exit 2; }
done
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256
export TEXT2SQL_PROBE_POSTGRES_DSN="${TEXT2SQL_POSTGRES_DSN}"
export TEXT2SQL_PROBE_MYSQL_DSN="${TEXT2SQL_MYSQL_DSN}"

case "${TEXT2SQL_PUBLIC_API_URL}" in
  https://*) ;;
  *) echo "TEXT2SQL_PUBLIC_API_URL must be an HTTPS public origin" >&2; exit 2 ;;
esac
case "${TEXT2SQL_BACKUP_REF}" in
  /*) ;;
  *) echo "TEXT2SQL_BACKUP_REF must be an absolute runner-local directory" >&2; exit 2 ;;
esac
case "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}" in
  /*) ;;
  *) echo "TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH must be absolute" >&2; exit 2 ;;
esac
printf '%s\n' "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}" \
  | grep -Eq '^[0-9a-f]{64}$'
approved_backup_manifest_path=$(realpath -e -- \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}")
workspace_path=$(realpath -e -- "$(pwd)")
case "${approved_backup_manifest_path}" in
  "${workspace_path}"|"${workspace_path}/"*)
    echo "approved backup manifest must be outside the checkout" >&2
    exit 2
    ;;
esac
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH="${approved_backup_manifest_path}"
for image in "${BACKEND_IMAGE}" "${FRONTEND_IMAGE}"; do
  printf '%s\n' "${image}" | grep -Eq '^sha256:[0-9a-f]{64}$'
done
for image in "${TEXT2SQL_PREVIOUS_BACKEND_IMAGE}" \
  "${TEXT2SQL_PREVIOUS_FRONTEND_IMAGE}"; do
  printf '%s\n' "${image}" | grep -Eq '^.+@sha256:[0-9a-f]{64}$'
done
test "${MODEL_CONFIG_DIGEST}" = \
  "$(sha256sum config/text_to_sql/llm_models.yaml | cut -d ' ' -f 1)"
test -r "${TEXT2SQL_LATENCY_BASELINE_ARTIFACT}"
test -f "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
test -r "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
test ! -w "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}"
test "$(sha256sum -- "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}" | cut -d ' ' -f 1)" = \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"
test -r artifacts/build/candidate-metadata.json
```

Generate the canonical target-policy artifact from the same four settings that
the candidate runtime receives, then export the digest consumed by the image,
runtime, evaluation, and evidence contracts:

```bash
python3 - <<'PY'
import os

from backend.fastapi_app.agui.connection_registry import (
    write_connection_target_policy_artifact,
)

write_connection_target_policy_artifact(
    "artifacts/build/target-policy.json",
    os.environ,
)
PY
export TEXT2SQL_TARGET_POLICY_SHA256="$(sha256sum artifacts/build/target-policy.json | cut -d ' ' -f 1)"
test -n "${TEXT2SQL_TARGET_POLICY_SHA256}"
```

Create the Compose secret files without exposing their values. The token map
gives the evaluation client only the release tenant identity it needs:

```bash
export AG_UI_AUTH_TOKEN_MAP_FILE="${RUNNER_TEMP}/text2sql-secrets/ag_ui_auth_token_map"
export HF_TOKEN_FILE="${RUNNER_TEMP}/text2sql-secrets/hf_token"
export OPENAI_API_KEY_FILE="${RUNNER_TEMP}/text2sql-secrets/openai_api_key"
export OPENAI_API_KEY_DB_FILE="${RUNNER_TEMP}/text2sql-secrets/openai_api_key_db"
install -d -m 0700 "${RUNNER_TEMP}/text2sql-secrets" artifacts/protected artifacts/runtime
python3 - <<'PY'
import json
import os
from pathlib import Path

token = os.environ["TEXT2SQL_EVAL_API_TOKEN"]
payload = {
    token: {
        "subject": "text2sql-release-eval",
        "tenant_id": "text2sql-release",
        "roles": ["admin", "user"],
    }
}
Path(os.environ["AG_UI_AUTH_TOKEN_MAP_FILE"]).write_text(
    json.dumps(payload, sort_keys=True), encoding="utf-8"
)
PY
printf '%s' "${HF_TOKEN}" > "${HF_TOKEN_FILE}"
printf '%s' "${OPENAI_API_KEY}" > "${OPENAI_API_KEY_FILE}"
printf '%s' "${OPENAI_API_KEY_DB}" > "${OPENAI_API_KEY_DB_FILE}"
chmod 0600 "${AG_UI_AUTH_TOKEN_MAP_FILE}" "${HF_TOKEN_FILE}" \
  "${OPENAI_API_KEY_FILE}" "${OPENAI_API_KEY_DB_FILE}"
```

After the reproducible candidate build has established `CANDIDATE_DIGEST`, copy
the exact approved source bytes into the protected evidence directory with a
restrictive mode. Validate the copied artifact against the local backup,
candidate digest, and schema before starting any candidate runtime or rollback:

```bash
export TEXT2SQL_SCHEMA_REF="sha256:$(sha256sum config/text_to_sql/state_schema.yaml | cut -d ' ' -f 1)"
export TEXT2SQL_BACKUP_MANIFEST=artifacts/protected/backup-manifest.json
install --mode=0600 -- \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH}" \
  "${TEXT2SQL_BACKUP_MANIFEST}"
test "$(sha256sum -- "${TEXT2SQL_BACKUP_MANIFEST}" | cut -d ' ' -f 1)" = \
  "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"
python3 deploy/write_backup_manifest.py \
  --backup-reference "${TEXT2SQL_BACKUP_REF}" \
  --candidate-digest "${CANDIDATE_DIGEST}" \
  --schema-ref "${TEXT2SQL_SCHEMA_REF}" \
  --expected-sha256 "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}" \
  --validate "${TEXT2SQL_BACKUP_MANIFEST}"
```

Write the deterministic drill report. It remains explicitly ineligible and is
only one artifact in the protected evidence bundle:

```bash
export TEXT2SQL_DRILL_REPORT=artifacts/deterministic-drills.json
export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"
bash scripts/test_text2sql_release_drills.sh --deterministic
test "$(python3 -c 'import json; print(json.load(open("artifacts/deterministic-drills.json"))["status"])')" = passed
```

Generate real-driver evidence **inside the exact `BACKEND_IMAGE`**, not in the
runner's Python environment. The bind contains only the resulting evidence;
the PostgreSQL and MySQL DSNs remain protected runner secrets:

```bash
docker run --rm --network host --read-only \
  --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --env TEXT2SQL_REQUIRED_REAL_DRIVER_LANES=postgres,mysql \
  --env TEXT2SQL_REAL_DRIVER_EVIDENCE=/artifacts/real-driver-evidence.json \
  --env TEXT2SQL_EVAL_COMMIT="${GITHUB_SHA}" \
  --env TEXT2SQL_EVAL_CANDIDATE_DIGEST="${CANDIDATE_DIGEST}" \
  --env TEXT2SQL_EVAL_LOCK_DIGEST="${LOCK_DIGEST}" \
  --env TEXT2SQL_POSTGRES_DSN \
  --env TEXT2SQL_MYSQL_DSN \
  --volume "$(pwd)/artifacts/protected:/artifacts" \
  --entrypoint python3 \
  "${BACKEND_IMAGE}" -m pytest -q -W error -m db_integration \
  tests/integration/test_text_to_sql_real_drivers.py
```

Start the exact candidate, verify container image IDs, and preserve readiness
plus candidate/runtime identity:

```bash
docker compose -p multiagent-text2sql -f deploy/compose.production.yml config --quiet
docker compose -p multiagent-text2sql -f deploy/compose.production.yml up \
  --detach --no-build --pull never --wait --wait-timeout 180
api_container=$(docker compose -p multiagent-text2sql -f deploy/compose.production.yml ps -q api)
worker_container=$(docker compose -p multiagent-text2sql -f deploy/compose.production.yml ps -q worker)
frontend_container=$(docker compose -p multiagent-text2sql -f deploy/compose.production.yml ps -q frontend)
test "$(docker inspect --format '{{.Image}}' "${api_container}")" = "${BACKEND_IMAGE}"
test "$(docker inspect --format '{{.Image}}' "${worker_container}")" = "${BACKEND_IMAGE}"
test "$(docker inspect --format '{{.Image}}' "${frontend_container}")" = "${FRONTEND_IMAGE}"
curl --fail --silent --show-error "${EVAL_API_URL}/healthz"
curl --fail --silent --show-error "${EVAL_API_URL}/readyz" \
  > artifacts/runtime/readiness.json
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

readiness = Path("artifacts/runtime/readiness.json")
payload = {
    "schema_version": 1,
    "candidate_digest": os.environ["CANDIDATE_DIGEST"],
    "backend_image": os.environ["BACKEND_IMAGE"],
    "backend_oci_digest": os.environ["BACKEND_OCI_DIGEST"],
    "frontend_image": os.environ["FRONTEND_IMAGE"],
    "frontend_oci_digest": os.environ["FRONTEND_OCI_DIGEST"],
    "readiness_sha256": hashlib.sha256(readiness.read_bytes()).hexdigest(),
    "target_policy_sha256": os.environ["TEXT2SQL_TARGET_POLICY_SHA256"],
}
Path("artifacts/runtime/candidate-runtime.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
```

Provision the secret fixture-to-DSN map through authenticated HTTP. This is the
only supported conversion from fixture names to connection references:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

from streamlit_app.text_to_sql_client import TextToSqlApiClient

fixture_dsns = json.loads(os.environ["TEXT2SQL_FIXTURE_DSN_MAP_JSON"])
if not isinstance(fixture_dsns, dict) or not fixture_dsns:
    raise SystemExit("fixture DSN map must be a non-empty object")
if not all(
    isinstance(fixture, str) and fixture.strip()
    and isinstance(dsn, str) and dsn.strip()
    for fixture, dsn in fixture_dsns.items()
):
    raise SystemExit("fixture DSN map entries must be non-empty strings")
token = os.environ["TEXT2SQL_EVAL_API_TOKEN"]
client = TextToSqlApiClient(
    base_url=os.environ["EVAL_API_URL"],
    auth_headers=lambda: {"Authorization": f"Bearer {token}"},
)
connection_map = {}
for fixture in sorted(fixture_dsns):
    connection = client.register_connection(
        display_name=f"release-{fixture}",
        dsn=fixture_dsns[fixture],
        owner_subject="text2sql-release-eval",
        tenant_id="text2sql-release",
    )
    connection_map[fixture] = connection.connection_ref
Path("artifacts/protected/connection-map.json").write_text(
    json.dumps(connection_map, sort_keys=True) + "\n", encoding="utf-8"
)
PY
```

The approved latency-baseline artifact is schema_version 2. Its
`baseline_commit`/`baseline_digest` identify the commit and candidate digest
the p95 was actually measured on, which must be the **previous approved
release**, not the current candidate (`provenance_kind: prior_release`). Only
the first-ever release may use `provenance_kind: bootstrap`, in which case
`baseline_commit`/`baseline_digest` must equal the current candidate's own
commit and digest. schema_version 1 (candidate-bound) baselines are rejected
and must be re-approved under schema_version 2.

Run the canonical release CLI with its complete candidate, provider, model,
approved versioned latency-baseline, protected-driver, connection, and
authentication interface:

```bash
python3 -m custom_tools.text_to_sql.eval.cli \
  --gold tests/eval/gold \
  --policy config/text_to_sql/eval_release.yaml \
  --mode release \
  --repetitions "${RELEASE_REPETITIONS}" \
  --commit "${GITHUB_SHA}" \
  --candidate-digest "${CANDIDATE_DIGEST}" \
  --lock-digest "${LOCK_DIGEST}" \
  --model-id "${MODEL_ID}" \
  --provider-id "${PROVIDER_ID}" \
  --model-config-digest "${MODEL_CONFIG_DIGEST}" \
  --latency-baseline "${TEXT2SQL_LATENCY_BASELINE_ARTIFACT}" \
  --target-policy artifacts/build/target-policy.json \
  --protected-evidence artifacts/protected/real-driver-evidence.json \
  --connection-map artifacts/protected/connection-map.json \
  --api-url "${EVAL_API_URL}" \
  --auth-token-env TEXT2SQL_EVAL_API_TOKEN \
  --output artifacts/text2sql-eval-release.json
```

Exercise the protected rollback against the local three-database backup and
preserve its report separately from the deterministic report. The standalone
rollback contract requires the custodian-approved manifest digest as an
explicit input:

```bash
export TEXT2SQL_DRILL_REPORT=artifacts/protected-rollback.json
export TEXT2SQL_CANDIDATE_REF="${CANDIDATE_DIGEST}"
export TEXT2SQL_BACKUP_MANIFEST='artifacts/protected/backup-manifest.json'
export TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256="${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256:?custodian-approved manifest digest is required}"
bash scripts/test_text2sql_release_drills.sh --protected-rollback
test "$(python3 -c 'import json; print(json.load(open("artifacts/protected-rollback.json"))["status"])')" = passed
```

Finally, write the strict release evidence index with every required argument.
It exits non-zero for a dirty tree, missing evidence, unsuccessful release
evaluation, or failed rollback:

```bash
python3 deploy/write_release_evidence.py \
  --artifacts artifacts \
  --commit "${GITHUB_SHA}" \
  --dirty-tree false \
  --policy config/text_to_sql/eval_release.yaml \
  --gold tests/eval/gold \
  --lock requirements.lock \
  --lock requirements-db-optional.lock \
  --lock frontend/client/package-lock.json \
  --migration-manifest config/text_to_sql/state_schema.yaml \
  --model-id "${MODEL_ID}" \
  --provider-id "${PROVIDER_ID}" \
  --model-config-digest "${MODEL_CONFIG_DIGEST}" \
  --latency-baseline "${TEXT2SQL_LATENCY_BASELINE_ARTIFACT}" \
  --target-policy artifacts/build/target-policy.json \
  --previous-backend-image "${TEXT2SQL_PREVIOUS_BACKEND_IMAGE}" \
  --previous-frontend-image "${TEXT2SQL_PREVIOUS_FRONTEND_IMAGE}" \
  --backup-reference "${TEXT2SQL_BACKUP_REF}" \
  --backup-manifest artifacts/protected/backup-manifest.json \
  --output artifacts/protected-gates.json
```

The resulting evidence index names and hashes
`artifacts/build/target-policy.json` and
`artifacts/protected/backup-manifest.json`; the rollback report is bound to the
same backup-manifest digest. Preserve those files with
`artifacts/deterministic-drills.json`,
`artifacts/protected/real-driver-evidence.json`,
`artifacts/text2sql-eval-release.json`,
`artifacts/protected-rollback.json`, and `artifacts/protected-gates.json`.

Do not reopen admission unless the policy approval and the latency-baseline
approval are current, all commands above exit zero, and an authorized release
owner approves the resulting `artifacts/protected-gates.json`. Until that
external ceremony succeeds, production eligibility remains UNVERIFIED.

Before approving, the release owner must inspect
`artifacts/protected-gates.json`'s `latency_baseline.provenance_kind` (also
recorded as `provenance.baseline_provenance_kind` in
`artifacts/text2sql-eval-release.json`) and reject the approval if it reads
`bootstrap` for any release after the first — a `bootstrap` baseline on a
later release means the latency gate was measured on the current candidate
itself rather than on the previously approved release, which silently
disables the regression check.

## Support matrix

| Lane | Deterministic local contract | Production release status |
|---|---|---|
| SQLite | Available | UNVERIFIED until candidate-bound probe/eval evidence |
| DuckDB | Available | UNVERIFIED until candidate-bound probe/eval evidence |
| PostgreSQL | Capability contract only | UNVERIFIED without protected real-driver probe |
| MySQL | Capability contract only | UNVERIFIED without protected real-driver probe |
| Impala | Capability contract only | UNVERIFIED without protected real-driver probe |
| SAP IQ | Capability contract only | UNVERIFIED without protected real-driver probe |

An unlisted or unprobed lane must not be enabled or advertised.

## Protected release gates

Run deterministic disposable drills locally:

```bash
bash scripts/test_text2sql_release_drills.sh
```

Run the deterministic evaluation separately; it cannot make a release eligible:

```bash
python3 -m custom_tools.text_to_sql.eval.cli \
  --gold tests/eval/gold \
  --policy config/text_to_sql/eval_release.yaml \
  --mode deterministic-contract \
  --output artifacts/text2sql-eval-contract.json
```

The protected environment runs the same CLI with `--mode release`, approved
policy/model/provider configuration, required repetitions, and real-driver
probe evidence. It also runs Compose readiness, deterministic drills, SBOM and
reproducible-image gates. Preserve candidate commit, state-schema digest,
backup reference, rollback reference, immutable image digests, eval report,
drill report, and protected gate report in the release evidence index.

Protected real-driver/model/image/SBOM/reproducibility gates require controlled
infrastructure and candidate-bound evidence. Missing credentials, services,
approvals, or evidence are release failures; they must not be converted to a
skip or reported as passing. A local drill pass means only that deterministic
contracts passed, never that the production release is eligible.
