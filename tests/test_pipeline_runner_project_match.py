"""Ф0 п.5: прогоны ищутся по точному id проекта, а не по LIKE '%id%'."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _make_runner(tmp_path, rows):
    db = tmp_path / "wf.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE workflow_checkpoints (workflow_id TEXT, timestamp TEXT, status TEXT,"
            " current_step TEXT, completed_steps TEXT, failed_steps TEXT, context TEXT,"
            " step_results TEXT, resumable BOOLEAN, metadata TEXT)"
        )
        for i, (wid, ctx) in enumerate(rows):
            conn.execute(
                "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                (wid, f"2026-01-01T00:00:{i:02d}", "running", "s", "[]", "[]",
                 json.dumps(ctx) if ctx is not None else None, "{}", 1, "{}"),
            )
    with patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"):
        from StoryBookManager.core.pipeline_runner import PipelineRunner
        runner = PipelineRunner()
    runner.engine = MagicMock()
    runner.engine.state_manager.store.db_path = str(db)
    runner.engine.state_manager.store.get_latest_checkpoint = AsyncMock(side_effect=lambda w: w)
    return runner


def _ids(res):
    return [r["workflow_id"] for r in res]


def test_incomplete_workflows_exact_project_only(tmp_path):
    r = _make_runner(tmp_path, [("sbm_a_deadbeef", None), ("sbm_a_b_cafebabe", None)])
    assert _ids(asyncio.run(r.get_incomplete_workflows("a"))) == ["sbm_a_deadbeef"]


def test_underscore_and_percent_not_wildcards(tmp_path):
    r = _make_runner(tmp_path, [("sbm_axb_12345678", None)])
    assert asyncio.run(r.get_incomplete_workflows("a_b")) == []
    assert asyncio.run(r.get_incomplete_workflows("%")) == []


def test_latest_checkpoint_exact(tmp_path):
    r = _make_runner(tmp_path, [("sbm_a_b_cafebabe", None)])
    assert asyncio.run(r._get_latest_project_checkpoint("a")) is None
    assert asyncio.run(r._get_latest_project_checkpoint("a_b")) == "sbm_a_b_cafebabe"


def test_single_step_workflow_matched(tmp_path):
    r = _make_runner(tmp_path, [("sbm_single_a_blockout_renderer_deadbeef", {"session_id": "a"})])
    assert _ids(asyncio.run(r.get_incomplete_workflows("a"))) == ["sbm_single_a_blockout_renderer_deadbeef"]


def test_single_step_ambiguity_resolved_by_session_id(tmp_path):
    r = _make_runner(tmp_path, [("sbm_single_a_x_step_deadbeef", {"session_id": "a_x"})])
    assert asyncio.run(r.get_incomplete_workflows("a")) == []
    assert _ids(asyncio.run(r.get_incomplete_workflows("a_x"))) == ["sbm_single_a_x_step_deadbeef"]


def test_latest_checkpoint_not_hidden_by_many_newer_foreign_runs(tmp_path):
    # свой прогон самый старый, а чужих свежих с подстрокой id больше прежнего LIMIT
    rows = [("sbm_a_deadbeef", None)] + [(f"sbm_a_b{i}_cafebabe", None) for i in range(60)]
    r = _make_runner(tmp_path, rows)
    assert asyncio.run(r._get_latest_project_checkpoint("a")) == "sbm_a_deadbeef"
