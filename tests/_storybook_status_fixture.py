"""Общие помощники для тестов studio/status: маленький синтетический проект storybook.

Это не тест-файл: его импортируют test_storybook_studio_status*.py.
Файлы `run_<шаг>.json` пишутся в настоящем формате (Python-repr в строке step_results).
"""

from __future__ import annotations

import contextlib
import importlib
import fcntl
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities
from tests.studio_run_fakes import make_series_project

PROJECT_ID = "demo"


class _LiveStatus:
    """Всегда текущий модуль status: другие тесты пересоздают реестр и подменяют модули в sys.modules."""

    def _mod(self):
        capabilities.registry()
        return importlib.import_module("custom_tools.storybook.studio.status")

    def __getattr__(self, name):
        return getattr(self._mod(), name)

    def __setattr__(self, name, value):
        setattr(self._mod(), name, value)

    def __delattr__(self, name):
        delattr(self._mod(), name)


status = _LiveStatus()

_SCHEMA = """CREATE TABLE workflow_checkpoints (
                    workflow_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    completed_steps TEXT,
                    failed_steps TEXT,
                    context TEXT,
                    step_results TEXT,
                    resumable BOOLEAN DEFAULT TRUE,
                    metadata TEXT,
                    PRIMARY KEY (workflow_id, timestamp)
                )"""


@pytest.fixture(autouse=True)
def _fresh_status_cache():
    status._reset_cache_for_tests()
    yield
    status._reset_cache_for_tests()


def make_env(tmp_path: Path, monkeypatch) -> Path:
    """Корень проектов и путь к базе прогонов (файла базы ещё нет). -> папка проекта demo."""
    root = tmp_path / "plots" / "storybooks"
    project = make_series_project(root, PROJECT_ID)
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(root))
    monkeypatch.setenv("STORYBOOK_WORKFLOW_DB", str(tmp_path / "workflow_state.db"))
    return project


def _dt_repr(value: datetime) -> str:
    return "datetime.datetime({}, {}, {}, {}, {}, {}, {})".format(
        value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond)


def write_run_file(project: Path, step: str, *, ok: bool = True, start: datetime | None = None,
                   end: datetime | None = None, big_kb: int = 0, workflow_status: str = "COMPLETED",
                   output_conflict: bool = False, error: str = "Инструмент завершился с ошибкой") -> Path:
    """Файл в реальном формате. big_kb дописывает огромный output перед хвостом."""
    start = start or datetime(2026, 10, 24, 1, 18, 20, 801258)
    end = end or datetime(2026, 10, 24, 1, 18, 21, 229258)
    duration = (end - start).total_seconds()
    err = "None" if ok else repr(error)
    state = "COMPLETED: 'completed'" if ok else "FAILED: 'failed'"
    repr_text = (
        "{'%s': StepResult(step_id='%s', status=<StepStatus.%s>, output=%s, error=%s, "
        "start_time=%s, end_time=%s, duration_seconds=%s, attempt_number=1, agent_name=None, "
        "resource_usage={'duration_seconds': %s}, metadata={}, quality_score=0.0)}"
    ) % (step, step, state, "'" + "x" * (big_kb * 1024) + "'" if big_kb else "None", err,
         _dt_repr(start), _dt_repr(end), duration, duration)
    output: object = "ok"
    if output_conflict:
        output = {"status": "success", "message": "Сгенерировано 50 из 50"}
    payload = {"step": step, "status": f"WorkflowStatus.{workflow_status}", "output": output,
               "errors": "None", "step_results": repr_text}
    path = project / f"run_{step}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def touch(project: Path, rel: str, data: bytes = b"x", mtime: float | None = None) -> Path:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_shots(project: Path, n_scenes: int = 2, per_scene: int = 2, with_images: int | None = None,
               with_videos: int | None = None, relative_paths: bool = True) -> list[str]:
    """shots.json из пар start/end и файлы-заглушки у первых with_images/with_videos шотов."""
    items, keys = [], []
    index = 0
    prefix = f"plots/storybooks/{PROJECT_ID}/series/main" if relative_paths else str(project)
    total = n_scenes * per_scene
    with_images = total if with_images is None else with_images
    with_videos = total if with_videos is None else with_videos
    for scene in range(1, n_scenes + 1):
        for shot in range(1, per_scene + 1):
            folder = f"scene_{scene:02d}_shot_{shot:02d}"
            video = f"{prefix}/97_shots/{folder}/video_final_{scene:02d}_{shot:02d}.mp4"
            for kind in ("start", "end"):
                image = f"{prefix}/97_shots/{folder}/img_final_{kind}_{scene:02d}_{shot:02d}.png"
                item = {"scene_number": scene, "shot_number": shot, "shot_type": kind,
                        "output_path": image, "image_path": None, "video_path": video}
                if kind == "start":
                    item["video_prompt"] = "движение"
                items.append(item)
                if index < with_images:
                    touch(project, f"97_shots/{folder}/img_final_{kind}_{scene:02d}_{shot:02d}.png")
            (project / "97_shots" / folder).mkdir(parents=True, exist_ok=True)
            if index < with_videos:
                touch(project, f"97_shots/{folder}/video_final_{scene:02d}_{shot:02d}.mp4")
            keys.append(f"{scene}-{shot}")
            index += 1
    touch(project, "97_shots/shots.json",
          json.dumps({"items": items, "inputs_hash": "h"}, ensure_ascii=False).encode("utf-8"))
    return keys


def make_provider_jobs(project: Path, jobs: list[dict]) -> Path:
    """jobs: dict с ключами shot_key, status, cost, currency, provider, updated_at (необязательно)."""
    rows = []
    for number, job in enumerate(jobs):
        rows.append({
            "shot_key": job.get("shot_key", "1-1"), "status": job["status"], "cost": job.get("cost"),
            "currency": job.get("currency", "RUB"), "provider": job.get("provider", "routerai"),
            "timestamps": {"created_at": job.get("updated_at", f"2026-10-27T10:{number % 60:02d}:00Z"),
                           "updated_at": job.get("updated_at", f"2026-10-27T10:{number % 60:02d}:00Z")},
            "output_path": None, "error": None, "input_hash": "h",
        })
    return touch(project, "97_shots/provider_jobs.json",
                 json.dumps({"version": 2, "jobs": rows}).encode("utf-8"))


def make_final(project: Path, passed: bool = True, duration: float = 12.0, width: int = 1280,
               height: int = 720, render_report: bool = True) -> None:
    touch(project, "99_final/final_video.mp4", b"video")
    review = {"passed": passed, "checks": {"duration": {"actual_duration_seconds": duration}},
              "errors": [], "warnings": []}
    touch(project, "99_final/final_review.json", json.dumps(review).encode("utf-8"))
    if render_report:
        report = {"final_probe": {"streams": [{"width": width, "height": height}]}}
        touch(project, "99_final/render_report.json", json.dumps(report).encode("utf-8"))
    touch(project, "99_final/final_video.render.json",
          json.dumps({"duration_seconds": duration, "input_hash": "h"}).encode("utf-8"))


def make_db(path: Path, workflows: list[dict]) -> Path:
    """База с настоящей схемой. workflow: workflow_id, timestamp, status, current_step, resumable,
    context (dict), step_results (dict или str), metadata (dict)."""
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    for wf in workflows:
        step_results = wf.get("step_results", {})
        conn.execute(
            "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wf["workflow_id"], wf.get("timestamp", datetime.now().isoformat()), wf["status"],
             wf.get("current_step"), "[]", wf.get("failed_steps", "[]"),
             json.dumps(wf.get("context", {})),
             step_results if isinstance(step_results, str) else json.dumps(step_results),
             int(wf.get("resumable", 1)), json.dumps(wf.get("metadata", {}))))
    conn.commit()
    conn.close()
    return path


def db_step(status_name: str, start: datetime, end: datetime, error: str | None = None) -> dict:
    return {"status": status_name, "start_time": start.isoformat(), "end_time": end.isoformat(),
            "duration_seconds": (end - start).total_seconds(), "error": error, "output": "не должен попасть"}


@contextlib.contextmanager
def hold_lock(project: Path):
    """Занять замок проекта (отдельный дескриптор конфликтует с пробой в том же процессе)."""
    handle = open(project / ".pipeline.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def list_tree(project: Path) -> list[str]:
    return sorted(str(p.relative_to(project)) for p in project.rglob("*"))
