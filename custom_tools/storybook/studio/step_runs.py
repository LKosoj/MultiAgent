"""Журнал шагов `.studio/step_runs.json`: что и когда выполнялось, сколько шло и стоило.

Пишет только держатель замка проекта (внутри процесса - под `threading.Lock`);
читают Ф3 и ETA. Запись атомарная (`project_store.write_json_atomic`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .project_store import write_json_atomic

logger = logging.getLogger(__name__)

STEP_RUNS_REL = ".studio/step_runs.json"
KEEP_DURATIONS = 5
MAX_ERROR_CHARS = 500
JOBS_REL = "97_shots/provider_jobs.json"
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LOCK = threading.Lock()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else datetime.now().isoformat(timespec="seconds")


def read_step_runs(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / STEP_RUNS_REL).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"version": 1, "steps": {}}
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        return {"version": 1, "steps": {}}
    return data


def _substitute(value: Any, variables: dict) -> Any:
    """Простая подстановка {var}; неизвестное остаётся как есть."""
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    return value


def compute_step_input_hash(step: Any, variables: dict, root: Path, artifacts_of: dict[str, list[str]],
                            depends_on: list[str] | None = None) -> str:
    """Первое приближение: инструмент + параметры (без session_id) + (путь, размер, mtime) входных файлов.

    Открыта для Ф3: сравнение хэшей двух прогонов показывает, поменялись ли входы шага.
    """
    params = dict(getattr(step, "tool_params", None) or {})
    params.pop("session_id", None)
    files = []
    for dep in sorted(depends_on if depends_on is not None else (getattr(step, "depends_on", None) or [])):
        for rel in artifacts_of.get(dep, []):
            target = Path(root) / rel
            try:
                st = target.stat()
                files.append([rel, st.st_size, st.st_mtime_ns])
            except OSError:
                files.append([rel, None, None])
    payload = {"tool": getattr(step, "tool_name", None), "params": _substitute(params, variables), "files": files}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_jobs(root: Path) -> list[dict]:
    """Видео-задачи из provider_jobs.json (список или словарь); нет файла или мусор - пусто."""
    try:
        data = json.loads((Path(root) / JOBS_REL).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def jobs_cost_since(root: Path, since_ts: float, until_ts: float | None = None) -> float:
    """Сумма `cost` видео-задач, созданных после since_ts (только видео, область video_jobs)."""
    total = 0.0
    for job in read_jobs(root):
        if not isinstance(job.get("cost"), (int, float)):
            continue
        stamp = _job_time(job)
        if stamp is None or stamp < since_ts - 1 or (until_ts is not None and stamp > until_ts + 1):
            continue
        total += float(job["cost"])
    return round(total, 2)


def _job_time(job: dict) -> float | None:
    stamps = job.get("timestamps") if isinstance(job.get("timestamps"), dict) else {}
    for key in ("created_at", "submitting_at", "submitted_at", "updated_at"):
        value = stamps.get(key, job.get(key))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def _status_value(result: Any) -> str:
    status = getattr(result, "status", None)
    value = getattr(status, "value", None) or status
    return str(value or "").lower()


class StepRunLog:
    """Писатель журнала одного прогона."""

    def __init__(self, root: Path, run_id: str, workflow_id: str | None) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.path = self.root / STEP_RUNS_REL
        self._started: dict[str, float] = {}

    def _update(self, step_id: str, record: dict) -> None:
        with _LOCK:
            data = read_step_runs(self.root)
            old = data["steps"].get(step_id) or {}
            durations = list(old.get("durations") or [])
            merged = {**old, **record}
            if record.get("status") == "completed" and isinstance(record.get("duration_s"), (int, float)):
                durations.append(float(record["duration_s"]))
            merged["durations"] = durations[-KEEP_DURATIONS:]
            data["steps"][step_id] = merged
            data["version"] = 1
            data["updated_at"] = _iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, data)

    def mark_started(self, step_id: str, input_hash: str | None, ts: float) -> None:
        self._started[step_id] = ts
        self._update(step_id, {
            "status": "running", "input_hash": input_hash, "started_at": _iso(ts), "finished_at": None,
            "duration_s": None, "run_id": self.run_id, "workflow_id": self.workflow_id, "error": None,
            "cost_rub": None,
        })

    def mark_finished(self, step_id: str, status: str, ts: float, *, duration_s: float | None = None,
                      error: str | None = None) -> None:
        started = self._started.get(step_id)
        if duration_s is None and started is not None:
            duration_s = max(0.0, ts - started)
        record: dict = {
            "status": status, "finished_at": _iso(ts), "duration_s": round(duration_s, 2) if duration_s is not None else None,
            "run_id": self.run_id, "workflow_id": self.workflow_id,
            "error": (error or None) and str(error)[:MAX_ERROR_CHARS],
        }
        if step_id == "video_generator" and started is not None:
            record["cost_rub"] = jobs_cost_since(self.root, started, ts)
        self._update(step_id, record)

    def reconcile(self, step_results: Any) -> None:
        """Финальная сверка: failed/skipped хук завершения движка не видит."""
        if not isinstance(step_results, dict):
            return
        for step_id, result in step_results.items():
            status = _status_value(result)
            if status not in ("completed", "failed", "skipped"):
                continue
            current = read_step_runs(self.root)["steps"].get(step_id) or {}
            if status == "completed" and current.get("status") == "completed" and current.get("run_id") == self.run_id:
                continue
            end = getattr(result, "end_time", None)
            self.mark_finished(
                step_id, status, end.timestamp() if hasattr(end, "timestamp") else datetime.now().timestamp(),
                duration_s=getattr(result, "duration_seconds", None),
                error=getattr(result, "error", None) if status == "failed" else None,
            )


def median_duration(root: Path, step_id: str) -> float | None:
    record = read_step_runs(root)["steps"].get(step_id) or {}
    values = [float(v) for v in record.get("durations") or [] if isinstance(v, (int, float))]
    return statistics.median(values) if values else None
