from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_python_and_frontend_locks_are_frozen() -> None:
    for lock_path in ("requirements.lock", "requirements-db-optional.lock"):
        lock = _read(lock_path)
        assert "uv==0.10.10" in lock
        assert "--generate-hashes" in lock
        assert "--hash=sha256:" in lock

    package = json.loads(_read("frontend/client/package.json"))
    package_lock = json.loads(_read("frontend/client/package-lock.json"))
    direct = {**package["dependencies"], **package["devDependencies"]}
    assert all(not version.startswith(("^", "~", ">", "<", "*")) for version in direct.values())
    assert package_lock["lockfileVersion"] == 3
    assert package_lock["packages"][""]["dependencies"] == package["dependencies"]
    assert package_lock["packages"][""]["devDependencies"] == package["devDependencies"]
    assert len(package_lock["packages"]) > 20


def test_deploy_images_entrypoint_and_compose_are_process_isolated() -> None:
    backend = _read("deploy/Dockerfile.backend")
    frontend = _read("deploy/Dockerfile.frontend")
    entrypoint = _read("deploy/entrypoint-backend.sh")
    compose = yaml.safe_load(_read("deploy/compose.production.yml"))

    assert "--require-hashes" in backend
    assert "requirements-db-optional.lock" in backend
    assert "SOURCE_DATE_EPOCH" in backend
    assert "USER 10001:10001" in backend
    assert "npm ci" in frontend
    assert "NEXT_BUILD_ID" in frontend
    assert "SOURCE_DATE_EPOCH" in frontend
    assert "USER 10001:10001" in frontend
    assert "api|worker|migrate" in entrypoint
    assert "uvicorn backend.fastapi_app.main:app" in entrypoint
    assert "scripts/migrate_text2sql_state.py" in entrypoint
    assert "deploy/worker.py" in entrypoint

    services = compose["services"]
    assert set(services) == {"migrate", "api", "worker", "frontend"}
    for service in services.values():
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["stop_grace_period"]
        assert service["deploy"]["resources"]["limits"]
    assert services["migrate"]["command"] == ["migrate"]
    assert services["api"]["command"] == ["api"]
    assert services["worker"]["command"] == ["worker"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["worker"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "/readyz" in " ".join(services["api"]["healthcheck"]["test"])
    assert all(
        services[name]["image"].startswith("${BACKEND_IMAGE:?")
        for name in ("migrate", "api", "worker")
    )
    assert services["frontend"]["image"].startswith("${FRONTEND_IMAGE:?")
    public_api_origin = services["frontend"]["build"]["args"][
        "NEXT_PUBLIC_AG_UI_URL"
    ]
    assert public_api_origin.startswith("${TEXT2SQL_PUBLIC_API_URL:?")
    assert "127.0.0.1" not in public_api_origin
    for setting in (
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES",
        "TEXT_TO_SQL_ALLOWED_DB_TARGETS",
        "TEXT_TO_SQL_ALLOWED_DB_CIDRS",
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS",
    ):
        assert services["api"]["environment"][setting].startswith(f"${{{setting}:?")
    assert services["api"]["environment"]["TEXT2SQL_ENABLED_PROBE_LANES"] == (
        "${TEXT2SQL_ENABLED_PROBE_LANES:-postgres,mysql}"
    )
    assert services["api"]["environment"]["TEXT2SQL_TARGET_POLICY_SHA256"].startswith(
        "${TEXT2SQL_TARGET_POLICY_SHA256:?"
    )
    assert {"text2sql-data", "text2sql-memory", "text2sql-logs"} <= set(
        compose["volumes"]
    )


def test_release_workflow_is_clean_fail_closed_and_never_publishes() -> None:
    workflow = _read(".github/workflows/text2sql-release.yml")
    parsed = yaml.safe_load(workflow)
    normalized_workflow = " ".join(workflow.replace("\\\n", " ").split())

    for required in (
        "deploy/check-locks.sh",
        "--require-hashes",
        "npm ci --prefix frontend/client",
        "npm --prefix frontend/client test -- --run",
        "npm --prefix frontend/client run lint -- --max-warnings=0",
        "npm --prefix frontend/client run build",
        "--mode deterministic-contract",
        "scripts/migrate_text2sql_state.py",
        "docker compose -f deploy/compose.production.yml config",
        "SOURCE_DATE_EPOCH",
        "--mode release",
        "scripts/test_text2sql_release_drills.sh",
        "protected-gates.json",
        "if-no-files-found: error",
    ):
        assert required in normalized_workflow

    triggers = parsed.get("on", parsed.get(True))
    assert set(triggers["push"]) == {"branches", "tags"}
    manual_inputs = triggers["workflow_dispatch"]["inputs"]
    assert {
        "public_api_origin",
        "model_id",
        "provider_id",
        "model_config_digest",
        "repetitions",
        "previous_backend_image",
        "previous_frontend_image",
        "backup_reference",
        "approved_backup_manifest_path",
        "approved_backup_manifest_sha256",
    } <= set(manual_inputs)
    assert all(value["required"] is True for value in manual_inputs.values())

    protected = parsed["jobs"]["protected-release"]
    assert protected["runs-on"] == ["self-hosted", "text2sql-release"]
    assert protected["environment"]["name"] == "text2sql-production-release"
    protected_condition = protected["if"]
    assert "workflow_dispatch" in protected_condition
    assert "refs/tags/text2sql-v" in protected_condition
    step_names = [step["name"] for step in protected["steps"]]
    required_order = [
        "Prepare immutable candidate inputs",
        "Build and verify reproducible candidate images",
        "Validate and preserve approved backup manifest",
        "Run deterministic release drills",
        "Probe required real drivers",
        "Start and verify exact candidate runtime",
        "Provision release fixture connections",
        "Run canonical protected release evaluation",
        "Run release drills",
        "Write release evidence index",
    ]
    assert [step_names.index(name) for name in required_order] == sorted(
        step_names.index(name) for name in required_order
    )
    run_blocks = [step.get("run", "") for step in protected["steps"]]
    assert all("${{ secrets." not in run_block for run_block in run_blocks)
    checkout_steps = [
        step
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if step.get("name") == "Check out candidate"
    ]
    assert checkout_steps
    assert all(step["with"]["fetch-depth"] == 0 for step in checkout_steps)
    eval_step = next(
        step
        for step in protected["steps"]
        if step["name"] == "Run canonical protected release evaluation"
    )
    for argument in (
        "--repetitions",
        "--commit",
        "--candidate-digest",
        "--lock-digest",
        "--model-id",
        "--provider-id",
        "--model-config-digest",
        "--latency-baseline",
        "--target-policy",
        "--protected-evidence",
        "--connection-map",
        "--api-url",
        "--auth-token-env",
    ):
        assert argument in eval_step["run"]
    assert eval_step["run"].count("--protected-evidence") == 1
    assert "model-lane-evidence" not in workflow
    assert "artifacts/protected/real-driver-evidence.json" in workflow
    assert "/tmp/" not in eval_step["run"]
    driver_step = next(
        step for step in protected["steps"] if step["name"] == "Probe required real drivers"
    )
    for setting in (
        "TEXT2SQL_EVAL_COMMIT",
        "TEXT2SQL_EVAL_CANDIDATE_DIGEST",
        "TEXT2SQL_EVAL_LOCK_DIGEST",
    ):
        assert setting in driver_step["run"]
    assert "docker run" in driver_step["run"]
    assert driver_step["run"].count("-m pytest") == 1
    assert "--entrypoint python3" in driver_step["run"]
    assert "--user 10001:10001" in driver_step["run"]
    assert '"${BACKEND_IMAGE}"' in driver_step["run"]
    assert "artifacts/protected:/evidence" in driver_step["run"]
    assert "TEXT2SQL_EVAL_LATENCY_BASELINE_P95_MS" not in driver_step["run"]
    assert 'payload["candidate"]' not in driver_step["run"]
    provision_step = next(
        step
        for step in protected["steps"]
        if step["name"] == "Provision release fixture connections"
    )
    assert "TEXT2SQL_FIXTURE_DSN_MAP_JSON" in provision_step["env"]
    assert "TextToSqlApiClient" in provision_step["run"]
    assert "register_connection" in provision_step["run"]
    assert "connection-map.json" in provision_step["run"]
    assert "TEXT2SQL_CONNECTION_MAP_JSON" not in workflow
    assert "sha256sum config/text_to_sql/llm_models.yaml" in workflow
    assert "TEXT2SQL_LATENCY_BASELINE_JSON" in workflow
    assert "artifacts/protected/latency-baseline.json" in workflow
    assert "TEXT2SQL_LATENCY_BASELINE_P95_MS" not in workflow
    for setting in (
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES",
        "TEXT_TO_SQL_ALLOWED_DB_TARGETS",
        "TEXT_TO_SQL_ALLOWED_DB_CIDRS",
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS",
    ):
        assert setting in protected["env"]
    drill_step = next(
        step for step in protected["steps"] if step["name"] == "Run release drills"
    )
    assert "--protected-rollback" in drill_step["run"]
    for setting in (
        "TEXT2SQL_PREVIOUS_BACKEND_IMAGE",
        "TEXT2SQL_PREVIOUS_FRONTEND_IMAGE",
        "TEXT2SQL_BACKUP_REF",
        "TEXT2SQL_BACKUP_MANIFEST",
        "TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256",
        "TEXT2SQL_DRILL_REPORT",
    ):
        assert setting in drill_step["env"]
    deterministic_drill = next(
        step
        for step in protected["steps"]
        if step["name"] == "Run deterministic release drills"
    )
    assert "--deterministic" in deterministic_drill["run"]
    assert deterministic_drill["env"]["TEXT2SQL_DRILL_REPORT"] == (
        "artifacts/deterministic-drills.json"
    )
    prepare_step = next(
        step
        for step in protected["steps"]
        if step["name"] == "Prepare immutable candidate inputs"
    )
    assert protected["env"]["TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256"] == (
        "${{ inputs.approved_backup_manifest_sha256 || "
        "vars.TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256 }}"
    )
    prepare_run = prepare_step["run"]
    assert "grep -Eq '^[0-9a-f]{64}$'" in prepare_run
    assert "realpath -e --" in prepare_run
    assert 'workspace_path=$(realpath -e -- "${GITHUB_WORKSPACE}")' in prepare_run
    assert "approved backup manifest must be outside GITHUB_WORKSPACE" in prepare_run
    assert 'test -f "${approved_backup_manifest_path}"' in prepare_run
    assert 'test -r "${approved_backup_manifest_path}"' in prepare_run
    assert 'test ! -w "${approved_backup_manifest_path}"' in prepare_run
    assert prepare_run.index("approved_backup_manifest_sha256=$(sha256sum --") < (
        prepare_run.index('test "${approved_backup_manifest_sha256}" =')
    )
    assert "TEXT2SQL_APPROVED_BACKUP_MANIFEST_PATH=%s" in prepare_run
    for database in (
        "agui_events.db",
        "smolagents_memory.db",
        "workflow_result_outbox.db",
    ):
        assert database in prepare_step["run"]
    assert 'test -d "${BACKUP_REFERENCE}"' in prepare_step["run"]
    assert "target-policy.json" in prepare_step["run"]
    assert "write_connection_target_policy_artifact" in prepare_step["run"]
    manifest_step = next(
        step
        for step in protected["steps"]
        if step["name"] == "Validate and preserve approved backup manifest"
    )
    manifest_run = manifest_step["run"]
    assert manifest_run.index("install --mode=0600 --") < manifest_run.index(
        "copied_backup_manifest_sha256=$(sha256sum --"
    ) < manifest_run.index('test "${copied_backup_manifest_sha256}" =') < (
        manifest_run.index("--validate artifacts/protected/backup-manifest.json")
    )
    assert (
        '--expected-sha256 "${TEXT2SQL_APPROVED_BACKUP_MANIFEST_SHA256}"'
        in manifest_run
    )
    assert "--output" not in manifest_run
    assert "backup-manifest.json" in workflow
    diff_step = next(
        step
        for step in parsed["jobs"]["deterministic-contract"]["steps"]
        if step["name"] == "Reject malformed diffs"
    )
    assert "pull_request.base.sha" in diff_step["env"]["BASE_SHA"]
    assert 'git diff --check "${BASE_SHA}...${GITHUB_SHA}"' in diff_step["run"]
    assert "git show --check" in diff_step["run"]
    assert "continue-on-error: true" not in workflow
    assert "docker push" not in workflow
    assert "build-push-action" not in workflow


def test_reproducible_builds_compare_oci_digests_and_normalized_sboms() -> None:
    build_script = _read("deploy/build_reproducible_images.sh")
    normalizer = _read("deploy/normalize_sbom.py")

    assert "--no-cache" in build_script
    assert "--metadata-file" in build_script
    assert "containerimage.digest" in build_script
    assert "containerimage.config.digest" in build_script
    assert build_script.count("build_once") >= 5
    assert "first_oci_digest" in build_script
    assert "second_oci_digest" in build_script
    assert "normalize_sbom.py" in build_script
    assert "cmp" in build_script
    assert "candidate-metadata.json" in build_script
    assert "serialNumber" in normalizer
    assert "timestamp" in normalizer
    assert "CycloneDX" in normalizer
    assert "component version does not match runtime image" in normalizer


def test_sbom_normalizer_removes_only_volatile_document_metadata(tmp_path) -> None:
    first = {
        "bomFormat": "CycloneDX",
        "serialNumber": "urn:uuid:first",
        "metadata": {
            "timestamp": "2026-01-01T00:00:00Z",
            "component": {"name": "candidate-one", "version": "sha256:image"},
        },
        "components": [{"name": "b"}, {"name": "a"}],
    }
    second = {
        "bomFormat": "CycloneDX",
        "serialNumber": "urn:uuid:second",
        "metadata": {
            "timestamp": "2026-02-01T00:00:00Z",
            "component": {"name": "candidate-two", "version": "sha256:image"},
        },
        "components": [{"name": "a"}, {"name": "b"}],
    }
    outputs = []
    for index, payload in enumerate((first, second), start=1):
        source = tmp_path / f"sbom-{index}.json"
        output = tmp_path / f"normalized-{index}.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "deploy" / "normalize_sbom.py"),
                str(source),
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _combined_lock_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_sqlite_head(path: Path, head: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={head}")
    connection.close()


def _write_valid_release_evidence(
    tmp_path: Path, *, provenance_kind: str = "prior_release"
) -> dict:
    from backend.fastapi_app.agui.connection_registry import (
        write_connection_target_policy_artifact,
    )
    from custom_tools.text_to_sql.eval.cases import (
        load_gold_cases,
        validate_case_coverage,
    )
    from custom_tools.text_to_sql.eval.cli import _observation_record
    from custom_tools.text_to_sql.eval.metrics import (
        compute_release_metrics,
        compute_release_metrics_by_repetition,
    )
    from custom_tools.text_to_sql.eval.observability import canonical_files_digest
    from custom_tools.text_to_sql.eval.release import (
        ReleasePolicy,
        canonical_baseline_payload_digest,
        canonical_policy_scope_digest,
    )
    from custom_tools.text_to_sql.eval.runner import EvalObservation

    artifacts = tmp_path / "artifacts"
    build = artifacts / "build"
    protected = artifacts / "protected"
    runtime = artifacts / "runtime"
    for directory in (build, protected, runtime):
        directory.mkdir(parents=True)

    target_policy = build / "target-policy.json"
    write_connection_target_policy_artifact(
        target_policy,
        {
            "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "postgresql,mysql,sqlite",
            "TEXT_TO_SQL_ALLOWED_DB_TARGETS": "db.example:5432,db.example:3306",
            "TEXT_TO_SQL_ALLOWED_DB_CIDRS": "10.0.0.0/8:5432,10.0.0.0/8:3306",
            "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": "/var/lib/text2sql",
        },
    )
    target_policy_digest = _sha256(target_policy)

    commit = "a" * 40
    model_id = "text2sql-primary"
    provider_id = "provider-primary"
    model_config_digest = _sha256(ROOT / "config/text_to_sql/llm_models.yaml")
    locks = [
        tmp_path / "requirements.lock",
        tmp_path / "requirements-db-optional.lock",
        tmp_path / "package-lock.json",
    ]
    for index, lock in enumerate(locks):
        lock.write_text(f"lock-{index}\n", encoding="utf-8")
    lock_digest = _combined_lock_digest(locks)

    images = {}
    for index, component in enumerate(("backend", "frontend"), start=1):
        oci_digest = f"sha256:{str(index) * 64}"
        runtime_image = f"sha256:{str(index + 2) * 64}"
        sbom_content = (
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "metadata": {
                        "component": {"version": runtime_image},
                    },
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        sbom_digest = hashlib.sha256(sbom_content).hexdigest()
        builds = []
        sboms = []
        for sequence in (1, 2):
            metadata = build / f"{component}-build-{sequence}.metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "containerimage.digest": oci_digest,
                        "containerimage.config.digest": runtime_image,
                    }
                ),
                encoding="utf-8",
            )
            sbom = build / f"{component}-sbom-{sequence}.normalized.cdx.json"
            sbom.write_bytes(sbom_content)
            builds.append(
                {
                    "metadata": str(metadata),
                    "oci_digest": oci_digest,
                    "config_digest": runtime_image,
                }
            )
            sboms.append({"path": str(sbom), "sha256": sbom_digest})
        images[component] = {
            "component": component,
            "oci_digest": oci_digest,
            "runtime_image": runtime_image,
            "sbom_sha256": sbom_digest,
            "builds": builds,
            "sboms": sboms,
        }
    candidate_digest = hashlib.sha256(
        json.dumps(
            {
                component: images[component]["oci_digest"]
                for component in sorted(images)
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat()
    candidate_metadata = {
        "schema_version": 1,
        "commit": commit,
        "dirty_tree": False,
        "generated_at": generated_at,
        "candidate_digest": candidate_digest,
        "lock_digest": lock_digest,
        "target_policy_sha256": target_policy_digest,
        "images": images,
    }
    (build / "candidate-metadata.json").write_text(
        json.dumps(candidate_metadata), encoding="utf-8"
    )

    policy_raw = yaml.safe_load(
        (ROOT / "config/text_to_sql/eval_release.yaml").read_text(encoding="utf-8")
    )
    policy_raw["approval"] = {
        "status": "approved",
        "approved_by": "release-owner",
        "approved_at": generated_at,
        "approval_ref": "change-42",
        "thresholds_sha256": canonical_policy_scope_digest(
            policy_raw["thresholds"],
            policy_raw["required_slices"],
            policy_raw["required_dialects"],
            policy_raw["required_protected_lanes"],
        ),
    }
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(policy_raw), encoding="utf-8")
    gold = ROOT / "tests/eval/gold"
    gold_digest = canonical_files_digest(gold)

    if provenance_kind == "bootstrap":
        # Bootstrap baselines must be measured on the current candidate, and a
        # tiny baseline latency makes the observations below (duration_ms=1.0)
        # regress far past the policy threshold -- proving the latency gate is
        # actually skipped for bootstrap rather than incidentally passing.
        baseline_commit = commit
        baseline_digest = candidate_digest
        baseline_latency_p95_ms = 0.1
    else:
        baseline_commit = "e" * 40
        baseline_digest = "f" * 64
        baseline_latency_p95_ms = 100.0
    baseline_payload = {
        "baseline_id": "baseline-text2sql-1",
        "provenance_kind": provenance_kind,
        "baseline_commit": baseline_commit,
        "baseline_digest": baseline_digest,
        "for_candidate_commit": None,
        "for_candidate_digest": None,
        "measured_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "model_id": model_id,
        "provider_id": provider_id,
        "model_config_digest": model_config_digest,
        "gold_sha256": gold_digest,
        "latency_p95_ms": baseline_latency_p95_ms,
    }
    baseline = protected / "latency-baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "baseline": baseline_payload,
                "approval": {
                    "status": "approved",
                    "approved_by": "release-owner",
                    "approved_at": (
                        datetime.now(timezone.utc) - timedelta(hours=1)
                    ).isoformat(),
                    "valid_until": (
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    ).isoformat(),
                    "approval_ref": "change-42",
                    "payload_sha256": canonical_baseline_payload_digest(
                        baseline_payload
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    driver = protected / "real-driver-evidence.json"
    driver.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": {
                    "commit": commit,
                    "artifact_digest": candidate_digest,
                    "lock_digest": lock_digest,
                    "generated_at": generated_at,
                },
                "lanes": [
                    {
                        "lane_id": f"driver:{dialect}",
                        "dialect": dialect,
                        "production_ready": True,
                        "reason_code": "READY",
                        "probe_kind": "real_driver",
                        "health": {"dialect": dialect, "production_ready": True},
                    }
                    for dialect in ("postgres", "mysql")
                ],
            }
        ),
        encoding="utf-8",
    )

    readiness = runtime / "readiness.json"
    heads = {"event_store": 8, "memory_db": 1, "result_outbox": 3}
    readiness.write_text(
        json.dumps(
            {
                "status": "ready",
                "checks": {
                    name: {
                        "status": "ready",
                        "reason_code": "READY",
                    }
                    for name in (
                        *heads,
                        "config",
                        "supervisor",
                        "retention",
                        "probe_lanes",
                    )
                },
                "schema_heads": {
                    name: {"expected": head, "actual": head}
                    for name, head in heads.items()
                },
                "probe_lanes_enabled": ["mysql", "postgres"],
                "target_policy_sha256": target_policy_digest,
            }
        ),
        encoding="utf-8",
    )
    (runtime / "candidate-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_digest": candidate_digest,
                "backend_image": images["backend"]["runtime_image"],
                "backend_oci_digest": images["backend"]["oci_digest"],
                "frontend_image": images["frontend"]["runtime_image"],
                "frontend_oci_digest": images["frontend"]["oci_digest"],
                "readiness_sha256": _sha256(readiness),
                "target_policy_sha256": target_policy_digest,
            }
        ),
        encoding="utf-8",
    )

    eval_path = artifacts / "text2sql-eval-release.json"
    evidence_index = [
        {"kind": "gold", "path": str(gold), "sha256": gold_digest},
        {"kind": "policy", "path": str(policy), "sha256": _sha256(policy)},
        {"kind": "protected_lane", "path": str(driver), "sha256": _sha256(driver)},
        {
            "kind": "latency_baseline",
            "path": str(baseline),
            "sha256": _sha256(baseline),
        },
        {
            "kind": "target_policy",
            "path": str(target_policy),
            "sha256": target_policy_digest,
        },
    ]
    all_cases = load_gold_cases(gold)
    release_cases = [case for case in all_cases if case.profile == "release"]
    release_policy = ReleasePolicy.load(policy)
    coverage = validate_case_coverage(
        release_cases,
        required_slices=release_policy.required_slices,
        required_dialects=release_policy.required_dialects,
    )
    observations = []
    for repetition in range(1, 4):
        for case in release_cases:
            controlled_execution_failure = (
                case.expected_outcome == "failed"
                and "EXECUTION_FAILED" in case.expected_reason_codes
            )
            sql = (
                "SELECT missing_column FROM orders"
                if controlled_execution_failure
                else case.expected_sql or ""
            )
            succeeded = case.expected_outcome == "succeeded"
            dry_run = succeeded and case.request_options["dry_run_only"]
            rows = list(case.expected_rows or [])
            columns = list(rows[0]) if rows and succeeded and not dry_run else None
            observations.append(
                EvalObservation(
                    case_id=case.id,
                    run_id=f"run-{repetition}-{case.id}",
                    status=case.expected_outcome,
                    reason_code=(
                        case.expected_reason_codes[0]
                        if case.expected_reason_codes
                        else ""
                    ),
                    generated_sql=sql,
                    rows=rows,
                    columns=columns,
                    schema_links=case.expected_schema_links or {},
                    duration_ms=1.0,
                    error=(
                        "controlled database execution failure"
                        if controlled_execution_failure
                        else None
                    ),
                    repetition=repetition,
                    generated=bool(sql),
                    approved=bool(sql),
                    executed=(succeeded and not dry_run)
                    or controlled_execution_failure,
                    dry_run=dry_run,
                    audited=(succeeded and bool(sql))
                    or controlled_execution_failure,
                )
            )
    metrics = compute_release_metrics(
        release_cases,
        observations,
        latency_baseline_p95_ms=baseline_payload["latency_p95_ms"],
    )
    repetition_metrics = compute_release_metrics_by_repetition(
        release_cases,
        observations,
        expected_repetitions=3,
        latency_baseline_p95_ms=baseline_payload["latency_p95_ms"],
    )
    advisories = (
        [
            "latency gate skipped: bootstrap baseline (self-measured); "
            "valid only for the first release"
        ]
        if provenance_kind == "bootstrap"
        else []
    )
    eval_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "release",
                "contract_passed": True,
                "release_eligible": True,
                "model_lane_satisfied": True,
                "provenance": {
                    "commit": commit,
                    "candidate_digest": candidate_digest,
                    "lock_digest": lock_digest,
                    "model_id": model_id,
                    "provider_id": provider_id,
                    "model_config_digest": model_config_digest,
                    "baseline_id": "baseline-text2sql-1",
                    "baseline_payload_sha256": canonical_baseline_payload_digest(
                        baseline_payload
                    ),
                    "baseline_file_sha256": _sha256(baseline),
                    "baseline_provenance_kind": provenance_kind,
                    "target_policy_sha256": target_policy_digest,
                },
                "evidence_index": evidence_index,
                "advisories": advisories,
                "gold_sha256": gold_digest,
                "policy_sha256": _sha256(policy),
                "policy_scope_sha256": policy_raw["approval"]["thresholds_sha256"],
                "case_count": len(release_cases),
                "all_reviewed_case_count": len(all_cases),
                "coverage": {
                    "slices": list(coverage.slices),
                    "dialects": list(coverage.dialects),
                    "missing_slices": list(coverage.missing_slices),
                    "missing_dialects": list(coverage.missing_dialects),
                },
                "metrics": metrics.to_mapping(),
                "observations": [
                    _observation_record(observation)
                    for observation in observations
                ],
                "per_repetition_metrics": {
                    str(repetition): value.to_mapping()
                    for repetition, value in repetition_metrics.items()
                },
                "threshold_failures": [],
                "approval_status": "approved",
                "approval_digest_current": True,
                "missing_protected_lanes": [],
                "exit_code": 0,
                "result_code": "RELEASE_ELIGIBLE",
            }
        ),
        encoding="utf-8",
    )

    schema_ref = "sha256:" + _sha256(ROOT / "config/text_to_sql/state_schema.yaml")
    deterministic_names = {
        "supervisor_admission_and_reaping",
        "outbox_crash_and_restart",
        "memory_reconciliation_and_rebuild",
        "retention_and_report_fallback",
        "schema_backup_restore_rollback",
    }
    (artifacts / "deterministic-drills.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "reason_code": "DRILL_OK",
                "candidate_ref": candidate_digest,
                "schema_ref": schema_ref,
                "backup_ref": "disposable-local",
                "rollback_ref": "not-executed-local",
                "release_eligible": False,
                "drills": [
                    {"name": name, "status": "passed"}
                    for name in sorted(deterministic_names)
                ],
            }
        ),
        encoding="utf-8",
    )

    backup = tmp_path / "backup"
    backup.mkdir()
    for filename, head in (
        ("agui_events.db", 8),
        ("smolagents_memory.db", 1),
        ("workflow_result_outbox.db", 3),
    ):
        _write_sqlite_head(backup / filename, head)
    backup_manifest = protected / "backup-manifest.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/write_backup_manifest.py"),
            "--backup-reference",
            str(backup),
            "--candidate-digest",
            candidate_digest,
            "--schema-ref",
            schema_ref,
            "--output",
            str(backup_manifest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    previous_backend = "registry/backend@sha256:" + "b" * 64
    previous_frontend = "registry/frontend@sha256:" + "c" * 64
    rollback_names = {
        "backup_schema_validation",
        "previous_backend_pull",
        "previous_frontend_pull",
        "candidate_stop",
        "state_restore",
        "previous_images_start",
        "healthz",
        "readyz",
    }
    (artifacts / "protected-rollback.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "reason_code": "PROTECTED_ROLLBACK_OK",
                "candidate_ref": candidate_digest,
                "schema_ref": schema_ref,
                "backup_ref": str(backup),
                "rollback_ref": {
                    "backend_image": previous_backend,
                    "frontend_image": previous_frontend,
                },
                "backup_manifest_sha256": _sha256(backup_manifest),
                "release_eligible": False,
                "drills": [
                    {"name": name, "status": "passed"}
                    for name in sorted(rollback_names)
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifacts": artifacts,
        "backup": backup,
        "backup_manifest": backup_manifest,
        "baseline": baseline,
        "candidate_digest": candidate_digest,
        "commit": commit,
        "gold": gold,
        "locks": locks,
        "model_config_digest": model_config_digest,
        "model_id": model_id,
        "policy": policy,
        "previous_backend": previous_backend,
        "previous_frontend": previous_frontend,
        "provider_id": provider_id,
        "target_policy": target_policy,
    }


def _run_evidence_writer(context: dict) -> subprocess.CompletedProcess[str]:
    output = context["artifacts"] / "protected-gates.json"
    command = [
        sys.executable,
        str(ROOT / "deploy/write_release_evidence.py"),
        "--artifacts",
        str(context["artifacts"]),
        "--commit",
        context["commit"],
        "--dirty-tree",
        "false",
        "--policy",
        str(context["policy"]),
        "--gold",
        str(context["gold"]),
    ]
    for lock in context["locks"]:
        command.extend(("--lock", str(lock)))
    command.extend(
        (
            "--migration-manifest",
            str(ROOT / "config/text_to_sql/state_schema.yaml"),
            "--model-id",
            context["model_id"],
            "--provider-id",
            context["provider_id"],
            "--model-config-digest",
            context["model_config_digest"],
            "--latency-baseline",
            str(context["baseline"]),
            "--target-policy",
            str(context["target_policy"]),
            "--previous-backend-image",
            context["previous_backend"],
            "--previous-frontend-image",
            context["previous_frontend"],
            "--backup-reference",
            str(context["backup"]),
            "--backup-manifest",
            str(context["backup_manifest"]),
            "--output",
            str(output),
        )
    )
    return subprocess.run(command, check=False, capture_output=True, text=True)


def test_release_evidence_writer_script_entrypoint_imports_project() -> None:
    completed = subprocess.run(
        [sys.executable, "deploy/write_release_evidence.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_release_evidence_writer_accepts_only_fully_cross_bound_evidence(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)

    completed = _run_evidence_writer(context)

    assert completed.returncode == 0, completed.stderr
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert index["release_eligible"] is True
    assert index["validation_errors"] == []
    assert index["candidate_digest"] == context["candidate_digest"]
    assert index["latency_baseline"]["sha256"] == _sha256(context["baseline"])
    assert index["target_policy"]["sha256"] == _sha256(context["target_policy"])
    assert index["deterministic_drills"]["present"] is True
    assert index["rollback"]["report"]["present"] is True
    assert index["rollback"]["backup_manifest_sha256"] == _sha256(
        context["backup_manifest"]
    )


def test_release_evidence_writer_accepts_bootstrap_baseline_and_skips_latency_gate(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path, provenance_kind="bootstrap")
    eval_artifact = json.loads(
        (context["artifacts"] / "text2sql-eval-release.json").read_text(
            encoding="utf-8"
        )
    )
    # The baseline is deliberately much faster than the observed durations, so
    # a prior_release baseline would have failed the latency-regression gate.
    assert eval_artifact["metrics"]["latency_p95_regression_fraction"] > 0.2

    completed = _run_evidence_writer(context)

    assert completed.returncode == 0, completed.stderr
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert index["release_eligible"] is True
    assert index["validation_errors"] == []
    assert index["latency_baseline"]["provenance_kind"] == "bootstrap"


def test_release_evidence_writer_rejects_unknown_or_mismatched_evidence(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    runtime_path = context["artifacts"] / "runtime/candidate-runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["unknown"] = True
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert index["release_eligible"] is False
    assert any("runtime" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_fabricated_eval_contract(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    artifact["case_count"] = 1
    artifact["all_reviewed_case_count"] = 999_999
    artifact["coverage"] = {
        "slices": [],
        "dialects": [],
        "missing_slices": [],
        "missing_dialects": [],
    }
    artifact["metrics"] = {"unexpected_error_rate": -1.0}
    artifact["observations"][0].update(
        {
            "status": "invented",
            "generated": False,
            "approved": False,
            "executed": False,
            "audited": False,
        }
    )
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("evaluation" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_extra_schema_link(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-composite-join"
    )
    observation["schema_links"]["tables"].append("unreviewed_table")
    observation["schema_links"]["tables"].sort()
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("schema links" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_dry_run_rows(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-dry-run"
    )
    observation["rows"] = [{"fabricated": 1}]
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("dry-run" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_success_error(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-composite-join"
    )
    observation["error"] = "fabricated success error"
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("successful observation" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_dry_run_error(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-dry-run"
    )
    observation["error"] = "fabricated dry-run error"
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("successful observation" in error for error in index["validation_errors"])


def test_release_evidence_writer_requires_columns_for_mapping_rows(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-composite-join"
    )
    observation["columns"] = None
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("mapping row columns" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_mismatched_success_columns(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-composite-join"
    )
    observation["columns"] = ["fabricated"]
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("mapping row columns" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_all_false_execution_failure(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    eval_path = context["artifacts"] / "text2sql-eval-release.json"
    artifact = json.loads(eval_path.read_text(encoding="utf-8"))
    observation = next(
        item
        for item in artifact["observations"]
        if item["case_id"] == "sqlite-execution-failure"
    )
    observation.update(
        {
            "sql_sha256": hashlib.sha256(b"").hexdigest(),
            "generated": False,
            "approved": False,
            "executed": False,
            "audited": False,
        }
    )
    eval_path.write_text(json.dumps(artifact), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("execution failure" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_unbound_image_and_non_cyclonedx_sbom(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    metadata_path = context["artifacts"] / "build/backend-build-1.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["containerimage.config.digest"] = "sha256:" + "f" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("image" in error.lower() for error in index["validation_errors"])


def test_release_evidence_writer_rejects_semantically_unbound_sbom(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    candidate_path = context["artifacts"] / "build/candidate-metadata.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    invalid_sbom = b'{"component":"backend"}\n'
    invalid_digest = hashlib.sha256(invalid_sbom).hexdigest()
    backend = candidate["images"]["backend"]
    backend["sbom_sha256"] = invalid_digest
    for sbom in backend["sboms"]:
        Path(sbom["path"]).write_bytes(invalid_sbom)
        sbom["sha256"] = invalid_digest
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("sbom" in error.lower() for error in index["validation_errors"])


def test_release_evidence_writer_rejects_backup_without_bound_manifest(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    context["backup_manifest"].unlink()

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("backup" in error.lower() for error in index["validation_errors"])


def test_release_evidence_writer_recomputes_backup_database_digest(tmp_path) -> None:
    context = _write_valid_release_evidence(tmp_path)
    database = context["backup"] / "agui_events.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE post_manifest_tamper(value TEXT)")
        connection.commit()
    finally:
        connection.close()

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("backup" in error.lower() for error in index["validation_errors"])


def test_release_evidence_writer_rejects_release_without_bound_target_policy(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    target_policy = json.loads(
        context["target_policy"].read_text(encoding="utf-8")
    )
    target_policy["allowed_dialects"].append("impala")
    target_policy["allowed_dialects"].sort()
    context["target_policy"].write_text(
        json.dumps(target_policy, sort_keys=True),
        encoding="utf-8",
    )

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("target policy" in error.lower() for error in index["validation_errors"])


def test_release_evidence_writer_requires_network_policy_to_match_driver_lanes(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    target_policy = json.loads(
        context["target_policy"].read_text(encoding="utf-8")
    )
    target_policy["allowed_dialects"].remove("mysql")
    context["target_policy"].write_text(
        json.dumps(target_policy, sort_keys=True),
        encoding="utf-8",
    )

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("driver lanes" in error for error in index["validation_errors"])


def test_release_evidence_writer_rejects_protected_runtime_without_probe_lanes(
    tmp_path,
) -> None:
    context = _write_valid_release_evidence(tmp_path)
    readiness_path = context["artifacts"] / "runtime/readiness.json"
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    readiness["probe_lanes_enabled"] = []
    readiness["checks"]["probe_lanes"] = {
        "status": "ready",
        "reason_code": "NO_PROBE_LANES_ENABLED",
    }
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")

    completed = _run_evidence_writer(context)

    assert completed.returncode == 1
    index = json.loads(
        (context["artifacts"] / "protected-gates.json").read_text(encoding="utf-8")
    )
    assert any("readiness" in error for error in index["validation_errors"])


def test_docker_context_excludes_mutable_and_secret_state() -> None:
    ignored = set(_read(".dockerignore").splitlines())
    assert {
        ".git",
        ".env",
        ".env.*",
        "**/__pycache__",
        "**/node_modules",
        "frontend/client/.next",
        "data",
        "logs",
        "memory/*.db",
        "memory/*.db-*",
        "memory/chromadb",
        "artifacts",
        "deploy/secrets",
    } <= ignored
