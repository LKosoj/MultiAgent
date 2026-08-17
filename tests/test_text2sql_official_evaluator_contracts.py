from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from custom_tools.text_to_sql.eval.official_evaluator_contracts import (
    IMAGE_ID,
    IMAGE_IDENTITY,
    bird_difficulty_jsonl,
    validate_bird_predictions,
    validate_image_identity,
)
from custom_tools.text_to_sql.eval.sandbox import SandboxError
from scripts.text2sql_benchmark_reporting import evaluator_receipt_is_closed


def test_image_identity_is_exact_and_closed(tmp_path: Path) -> None:
    payload = dict(IMAGE_IDENTITY)
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert validate_image_identity(path, expected_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())["image_id"] == IMAGE_ID
    payload["image_id"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SandboxError, match="image identity"):
        validate_image_identity(path, expected_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_bird_predictions_require_exact_order_and_jsonl_is_canonical(tmp_path: Path) -> None:
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({str(i): f"SELECT {i}" for i in range(500)}), encoding="utf-8")
    assert len(validate_bird_predictions(predictions)) == 500

    rows = [{"difficulty": "simple", "question_id": i} for i in range(500)]
    payload = bird_difficulty_jsonl(rows)
    assert len(payload.splitlines()) == 500
    assert payload.startswith(b'{"difficulty":"simple","question_id":0}\n')

    predictions.write_text(json.dumps({str(i): "SELECT 1" for i in reversed(range(500))}), encoding="utf-8")
    with pytest.raises(SandboxError, match="ordered keys"):
        validate_bird_predictions(predictions)


def test_v2_receipt_requires_closed_evaluator_and_execution_identity() -> None:
    receipt = {
        "schema_version": 2,
        "record_kind": "text2sql_official_evaluator_receipt",
        "evaluator_identity": {
            "origin": "https://example.test/evaluator",
            "revision": "revision",
            "entrypoint": "evaluate.py",
            "sha256": "a" * 64,
            "call_surface": "python API:evaluate",
            "source_closure_sha256": "b" * 64,
            "data_closure_sha256": "c" * 64,
            "runtime_identity_sha256": "d" * 64,
        },
        "evaluator_input_sha256": "sha256:input",
        "score_sha256": "sha256:score",
        "case_manifest_sha256": "sha256:cases",
        "run_manifest_sha256": "sha256:run",
        "execution_evidence_sha256": "sha256:execution",
        "case_keys": ["bird:0"],
    }
    assert evaluator_receipt_is_closed(receipt)
    receipt.pop("execution_evidence_sha256")
    assert not evaluator_receipt_is_closed(receipt)
