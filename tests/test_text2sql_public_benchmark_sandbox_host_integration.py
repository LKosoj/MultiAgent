from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
from threading import Thread
import time

import pytest

from custom_tools.text_to_sql.eval.official_evaluator_contracts import (
    IMAGE_ID,
    IMAGE_USER,
)
from custom_tools.text_to_sql.eval.sandbox import (
    BwrapSandboxSpec,
    build_bwrap_command,
    create_source_snapshot,
    prepare_case_overlays,
    prepare_shared_schema_memory,
    validate_secret_dir,
    verify_private_case_files,
    verify_source_snapshot,
)


@pytest.mark.host_integration
def test_pinned_image_cold_imports_worker_from_frozen_snapshot(
    tmp_path: Path,
) -> None:
    image = subprocess.run(
        ["docker", "image", "inspect", IMAGE_ID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if image.returncode != 0:
        pytest.skip("exact pinned official evaluator image is not installed")
    repo_root = Path(__file__).resolve().parents[1]
    snapshot = create_source_snapshot(repo_root, tmp_path / "snapshot")
    evaluator_root = snapshot.root / "custom_tools/text_to_sql/eval"
    helper = evaluator_root / "official_evaluator_attempt.py"
    assert helper.is_file() and helper.stat().st_mode & 0o777 == 0o444

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            f"--user={IMAGE_USER}",
            "--mount",
            f"type=bind,src={evaluator_root},dst=/bridge,readonly",
            IMAGE_ID,
            "/opt/evaluator-venv/bin/python",
            "-c",
            (
                "import sys; sys.path.insert(0, '/bridge'); "
                "import official_evaluator_contracts, official_evaluator_worker; "
                "print(official_evaluator_worker.__file__)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/bridge/official_evaluator_worker.py"


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _FakeModelHandler(BaseHTTPRequestHandler):
    calls = 0

    @staticmethod
    def _message_texts(messages: object) -> tuple[str, ...]:
        if not isinstance(messages, list):
            raise AssertionError("fake model messages must be a list")
        texts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            if not isinstance(content, list):
                continue
            texts.extend(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        return tuple(texts)

    @classmethod
    def _research_state(cls, messages: object) -> dict[str, object]:
        for content in reversed(cls._message_texts(messages)):
            if '"research_context"' not in content:
                continue
            envelope = json.loads(content)
            input_data = envelope.get("input")
            if not isinstance(input_data, dict):
                continue
            context = input_data.get("research_context")
            if not isinstance(context, str):
                continue
            payload = json.loads(context)
            state = payload.get("state")
            if isinstance(state, dict):
                return state
        raise AssertionError("typed research prompt has no state")

    @staticmethod
    def _required_source_id(state: dict[str, object]) -> str:
        unresolved_items = state.get("unresolved_items")
        assert isinstance(unresolved_items, list)
        source_id = next(iter(unresolved_items))
        assert isinstance(source_id, str)
        return source_id

    @staticmethod
    def _evidence_id_for_action(state: dict[str, object], kind: str) -> str:
        evidence = state.get("evidence")
        assert isinstance(evidence, list)
        del kind
        evidence_id = next(
            item.get("evidence_id")
            for item in evidence
            if isinstance(item, dict)
        )
        assert isinstance(evidence_id, str)
        return evidence_id

    @classmethod
    def _typed_research_response(cls, messages: object) -> dict[str, object]:
        state = cls._research_state(messages)
        revision = state.get("revision")
        if revision == 0:
            return {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_column",
                        "arguments": {"table": "items", "column": "name"},
                    },
                },
            }

        source_id = cls._required_source_id(state)
        if revision == 1:
            return {
                "decision_version": 1,
                "proposals": [
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:items-name",
                        "source_id": source_id,
                        "candidate": {
                            "kind": "physical_column",
                            "physical_column": {"table": "items", "column": "name"},
                        },
                        "join_references": [],
                        "citation_evidence_ids": [
                            cls._evidence_id_for_action(state, "inspect_column")
                        ],
                    }
                ],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": "items"},
                    },
                },
            }

        if revision == 2:
            bindings = state.get("bindings")
            assert isinstance(bindings, list)
            binding = next(
                item
                for item in bindings
                if isinstance(item, dict)
                and item.get("source_id") == source_id
                and item.get("kind") == "physical_column"
            )
            binding_id = binding.get("binding_id")
            evidence_ids = binding.get("evidence_ids")
            assert isinstance(binding_id, str)
            assert isinstance(evidence_ids, list)
            assert isinstance(evidence_ids[0], str)
            return {
                "decision_version": 1,
                "proposals": [
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": [
                            evidence_ids[0]
                        ],
                    }
                ],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_relationships",
                        "arguments": {"table": "items", "top_k": 1, "depth": 1},
                    },
                },
            }

        if revision == 3:
            evidence = state.get("evidence")
            assert isinstance(evidence, list)
            return {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": sorted(
                        item["evidence_id"]
                        for item in evidence
                        if isinstance(item, dict)
                        and isinstance(item.get("evidence_id"), str)
                    ),
                },
            }

        raise AssertionError(f"unexpected typed research revision: {revision!r}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        messages = request.get("messages", [])
        prompt = "\n".join(self._message_texts(messages))
        if '"research_context"' in prompt:
            response = self._typed_research_response(messages)
        elif "Never generate, rewrite or execute SQL" in prompt:
            response = {
                "status": "consistent",
                "reason": "The returned item-name rows match the requested projection.",
                "source_id": None,
            }
        elif "verification_status" in prompt:
            response = {
                "verification_status": "Approved",
                "safety_check": {"is_safe": True, "issues": []},
                "performance_check": {
                    "plan": "SCAN items",
                    "estimated_cost": 1.0,
                    "issues": [],
                },
                "recommendations": [],
            }
        elif "Разбери пользовательский Text-to-SQL запрос" in prompt:
            response = {
                "expected_result_shape": "rows",
                "semantic_items": [
                    {
                        "kind": "dimension",
                        "source_text": "item names",
                        "normalized_meaning": "item names",
                        "required": True,
                        "operator": None,
                        "literal_or_reference": None,
                        "status": "unresolved",
                    }
                ],
            }
        elif "Проанализируй SQL на риски" in prompt:
            response = {"issues": []}
        elif "You are the SQL-solver proposal step" in prompt:
            response = {
                "proposal_version": 1,
                "proposal": {
                    "proposal_kind": "sql_candidate",
                    "sql": 'SELECT "items"."name" FROM "items"',
                },
            }
        else:
            raise AssertionError(f"unexpected fake-model prompt: {prompt}")
        content = json.dumps(
            {
                "name": "final_answer",
                "arguments": {"answer": response},
            }
            if request.get("tools")
            else response
        )
        encoded = json.dumps(
            {
                "id": "fake-model-response",
                "object": "chat.completion",
                "created": 0,
                "model": "fake-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_fake_model() -> tuple[ThreadingHTTPServer, Thread, int]:
    _FakeModelHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModelHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_address[1])


def _stop_process_group(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=15)


@pytest.mark.host_integration
def test_host_bwrap_api_and_spawned_workflow_child_preserve_isolation(
    tmp_path: Path,
) -> None:
    """Exercise the real bwrap API and one spawned Text-to-SQL workflow child."""

    from streamlit_app.text_to_sql_client import (
        TERMINAL_RUN_STATUSES,
        TextToSqlApiClient,
        TextToSqlApiError,
        TextToSqlRunRequest,
    )

    repo_root = Path(__file__).resolve().parents[1]
    snapshot = create_source_snapshot(repo_root, tmp_path / "source-snapshot")
    input_database = tmp_path / "tiny.sqlite"
    with sqlite3.connect(input_database) as connection:
        connection.execute("CREATE TABLE items (name TEXT NOT NULL)")
        connection.execute("INSERT INTO items(name) VALUES ('one')")
    input_digest = input_database.read_bytes()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)
    (secrets / "ag_ui_auth_token_map").write_text(
        json.dumps(
            {
                "benchmark-token": {
                    "subject": "benchmark",
                    "tenant_id": "benchmark",
                    "roles": ["admin", "user"],
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("openai_api_key", "openai_api_key_db", "hf_token"):
        path = secrets / name
        path.write_text("fake-test-key\n", encoding="utf-8")
        path.chmod(0o600)
    (secrets / "ag_ui_auth_token_map").chmod(0o600)
    validate_secret_dir(secrets)
    gold_sentinel = tmp_path / "gold-sentinel.sql"
    gold_sentinel.write_text("SELECT forbidden\n", encoding="utf-8")
    api_port = _free_local_port()
    fake_model, model_thread, model_port = _start_fake_model()
    shared_schema_memory_root = tmp_path / "schema-memory"
    prepare_shared_schema_memory(shared_schema_memory_root)
    spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=repo_root / ".venv",
        case_root=tmp_path / "case-state",
        database_path=input_database,
        database_id="tiny",
        secret_dir=secrets,
        port=api_port,
        shared_schema_memory_root=shared_schema_memory_root,
        runtime_env={
            "OPENAI_API_BASE_DB": f"http://127.0.0.1:{model_port}/v1",
            "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": "/benchmark-input",
            "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "sqlite",
            "MEMORY_CHROMA_DISABLED": "1",
        },
    )
    prepare_case_overlays(spec)
    command = build_bwrap_command(spec)
    probe_index = command.index("--chdir")
    probe = command[:probe_index] + [
        "--chdir",
        "/workspace",
        "/workspace/.venv/bin/python",
        "-c",
        f"from pathlib import Path; raise SystemExit(int(Path({str(gold_sentinel)!r}).exists() or Path('/etc/shadow').exists()))",
    ]
    assert subprocess.run(probe, check=False, timeout=30).returncode == 0

    log_path = tmp_path / "sandbox-api.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 35
            client = TextToSqlApiClient(
                base_url=f"http://127.0.0.1:{api_port}",
                auth_headers=lambda: {"Authorization": "Bearer benchmark-token"},
                poll_interval_seconds=0.1,
                max_poll_attempts=300,
            )
            while True:
                try:
                    me = client.get_me()
                    break
                except Exception as exc:
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            log_path.read_text(errors="replace")
                        ) from exc
                    time.sleep(0.2)
            assert me["subject"] == "benchmark"
            registered = client.register_connection(
                display_name="tiny SQLite",
                dsn="sqlite:////benchmark-input/tiny.sqlite",
                owner_subject="benchmark",
                tenant_id="benchmark",
            )
            handle = client.start(
                TextToSqlRunRequest(
                    query="List the item names",
                    connection_ref=registered.connection_ref,
                    idempotency_key="host-integration-tiny-run",
                    max_rows=5,
                )
            )
            while True:
                try:
                    status = client.get_run(handle.run_id)
                    break
                except TextToSqlApiError as exc:
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            f"workflow run was not registered: {handle}"
                        ) from exc
                    time.sleep(0.1)
            while status.status not in TERMINAL_RUN_STATUSES:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"workflow did not finish: {status}")
                time.sleep(0.1)
                status = client.get_run(handle.run_id)
            result = client.get_result(handle.run_id)
        finally:
            _stop_process_group(process)
            fake_model.shutdown()
            fake_model.server_close()
            model_thread.join(timeout=5)

    state_database = spec.case_root / "data" / "multiagent_state" / "agui_events.db"
    with sqlite3.connect(state_database) as state_connection:
        worker_claim = state_connection.execute(
            "SELECT worker_pid, worker_pid_started_at_ms, supervisor_id, "
            "attempt_started_at_ms FROM agui_runs WHERE run_id = ?",
            (handle.run_id,),
        ).fetchone()
        stored_events = [
            (row[0], json.loads(row[1]))
            for row in state_connection.execute(
                "SELECT event_type, payload FROM agui_events "
                "WHERE run_id = ? ORDER BY seq",
                (handle.run_id,),
            )
        ]
    assert status.status == "finished"
    assert result.run_id == handle.run_id
    assert result.status == "succeeded"
    assert result.reason_code == ""
    assert result.reason_code_recognized is True
    assert result.sql == 'SELECT "items"."name" FROM "items"'
    assert result.executed is True
    assert result.execution["applied_row_limit"] == 5
    assert len(result.rows) <= 5
    assert worker_claim is not None
    assert all(value is not None for value in worker_claim)
    service_result = next(
        payload
        for event_type, payload in stored_events
        if event_type == "CUSTOM" and payload.get("name") == "service.result"
    )
    workflow_result = next(
        payload
        for event_type, payload in stored_events
        if event_type == "WORKFLOW_RESULT"
    )
    assert (
        service_result["value"]["data"]["parameters"]["connection_ref"]
        == registered.connection_ref
    )
    assert (
        workflow_result["snapshot"]["parameters"]["connection_ref"]
        == registered.connection_ref
    )
    verify_private_case_files(spec)
    verify_source_snapshot(snapshot)
    assert _FakeModelHandler.calls > 0
    assert input_database.read_bytes() == input_digest
    assert gold_sentinel.read_text(encoding="utf-8") == "SELECT forbidden\n"
    assert (snapshot.root / "memory" / "smolagents_memory.db").read_bytes() == b""
    assert (shared_schema_memory_root / "smolagents_memory.db").stat().st_size > 0
    assert (spec.case_root / "plots").is_dir()
    assert (spec.case_root / "plots").stat().st_mode & 0o777 == 0o700
    assert "SQLite WAL mode is unavailable" not in log_path.read_text(errors="replace")
