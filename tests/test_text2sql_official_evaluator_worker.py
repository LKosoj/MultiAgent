from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from custom_tools.text_to_sql.eval import official_evaluator_worker as worker
from custom_tools.text_to_sql.eval.official_evaluator_bridge import (
    ContainerRequest,
    docker_command,
)
from custom_tools.text_to_sql.eval.official_evaluator_contracts import (
    IMAGE_ID,
    RAW_FREEZE_SHA256,
)


def test_validate_bird_results_requires_exact_binary_denominator() -> None:
    rows = [{"sql_idx": index, "res": index % 2} for index in range(500)]
    assert worker.validate_bird_results(rows)[499]["sql_idx"] == 499
    rows[-1]["res"] = 2
    with pytest.raises(ValueError, match="BIRD result"):
        worker.validate_bird_results(rows)


def test_validate_spider_results_rejects_intersection_shrink() -> None:
    keys = [f"local{i:03d}" for i in range(135)]
    rows = [
        {"instance_id": key, "score": index % 2, "pred_sql": "SELECT 1", "error_info": None}
        for index, key in enumerate(sorted(keys))
    ]
    assert len(worker.validate_spider_results(rows, keys)) == 135
    with pytest.raises(ValueError, match="Spider result keys"):
        worker.validate_spider_results(rows[:-1], keys)


def test_spider_worker_uses_returned_api_rows_and_contains_streams(tmp_path: Path) -> None:
    source = tmp_path / "evaluate.py"
    source.write_text(
        """
import sys
from pathlib import Path
class Tee:
    def __init__(self):
        self.console = sys.stdout
        self.file = open('log.txt', 'w')
    def write(self, value): self.console.write(value); self.file.write(value)
    def flush(self): self.console.flush(); self.file.flush()
    def close(self): self.file.close()
sys.stdout = Tee()
sys.stderr = sys.stdout
def evaluate_spider2sql(args, temp_dir):
    rows = [{"instance_id": key, "score": 1, "pred_sql": "SELECT 1", "error_info": None} for key in args.expected_keys]
    print({row["instance_id"]: row["score"] for row in rows})
    return rows
""",
        encoding="utf-8",
    )
    keys = [f"local{i:03d}" for i in range(135)]
    result = worker.run_spider_source(
        source,
        result_dir=tmp_path / "predictions",
        gold_dir=tmp_path / "gold",
        temp_dir=tmp_path / "temp",
        work_dir=tmp_path / "cwd",
        expected_keys=keys,
    )
    assert len(result) == 135
    assert (tmp_path / "cwd" / "log.txt").is_file()


@pytest.mark.host_integration
def test_bird_worker_main_supports_fork_pool_pickling_with_artificial_source(
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
    dataset_root = Path("/srv/text2sql-benchmarks/bird-mini-dev")
    identity_path = Path("/srv/text2sql-benchmarks/evaluator-image-identity.json")
    if not (dataset_root / "mini_dev_sqlite.json").is_file():
        pytest.skip("canonical BIRD mini-dev task is not installed")

    work = tmp_path / "work"
    staged = tmp_path / "staged"
    work.mkdir()
    staged.mkdir()
    os.chown(work, 65532, 65532)
    container_tmp = work / "tmp"
    container_tmp.mkdir()
    os.chown(container_tmp, 65532, 65532)
    predictions = staged / "bird_predictions.json"
    predictions.write_text(
        json.dumps({str(index): "SELECT 1" for index in range(500)}),
        encoding="utf-8",
    )
    entrypoint = staged / "artificial_evaluation_ex.py"
    entrypoint.write_text(
        """
import json
import multiprocessing
import os
from pathlib import Path

def artificial_score(index):
    return {"sql_idx": index, "res": index % 2, "worker_pid": os.getpid()}

if __name__ == "__main__":
    parent_pid = os.getpid()
    with multiprocessing.Pool(4) as pool:
        exec_result = pool.map(artificial_score, range(500))
    Path("/work/pool_evidence.json").write_text(json.dumps({
        "parent_pid": parent_pid,
        "worker_pids": sorted({row["worker_pid"] for row in exec_result}),
    }))
""",
        encoding="utf-8",
    )
    predictions.chmod(0o444)
    entrypoint.chmod(0o444)
    repo_root = Path(__file__).resolve().parents[1]
    request = ContainerRequest(
        name=f"text2sql-artificial-bird-{os.getpid()}",
        benchmark="bird",
        transaction_dir=work,
        worker_root=repo_root / "custom_tools/text_to_sql/eval",
        dataset_root=dataset_root,
        sqlite_root=None,
        identity_path=identity_path,
        worker_arguments=(
            "bird",
            "--entrypoint", "/work/artificial_evaluation_ex.py",
            "--predictions", "/work/bird_predictions.json",
            "--raw-results", "/work/raw_results.json",
            "--task", "/official/bird/mini_dev_sqlite.json",
            "--database-root", "/official/bird/dev_databases",
            "--gold-sql", "/official/bird/mini_dev_sqlite_gold.sql",
            "--difficulty-jsonl", "/work/difficulty.jsonl",
            "--source-log", "/work/source.log",
        ),
        readonly_work_mounts=(
            (entrypoint, "/work/artificial_evaluation_ex.py"),
            (predictions, "/work/bird_predictions.json"),
        ),
    )
    completed = subprocess.run(
        docker_command(request), capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    raw = json.loads((work / "raw_results.json").read_text(encoding="utf-8"))
    assert raw["freeze_before"] == RAW_FREEZE_SHA256
    assert raw["freeze_after"] == RAW_FREEZE_SHA256
    assert len(raw["results"]) == 500
    evidence = json.loads((work / "pool_evidence.json").read_text(encoding="utf-8"))
    assert len(evidence["worker_pids"]) > 1
    assert evidence["parent_pid"] not in evidence["worker_pids"]
