"""Управление прогоном: план, старт, статус, пауза, остановка, журнал, история.

Тонкий слой возможностей поверх `run_plan` (что запускать), `run_exec` (как исполнять)
и `run_control` (замок и управляющие файлы). Здесь же две обычные функции для Ф3 и Ф10.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from . import run_control, run_exec, run_launchers, run_logs, run_plan
from .capabilities import capability
from .errors import StudioError, success
from .paths import project_dir
from .step_runs import jobs_cost_since, median_duration, read_jobs, read_step_runs

logger = logging.getLogger(__name__)

ACTIVE_STATES = frozenset({"running", "pause_requested", "paused", "stopping"})
MIN_HISTORY_SHARE = 0.25
DEFAULT_HISTORY = 20
MAX_HISTORY = 50
_DOWNLOADED = frozenset({"downloaded", "completed", "succeeded", "success"})
_FAILED = frozenset({"failed", "download_failed"})


def _root(project_id: str) -> Path:
    return project_dir(project_id)


# ------------------------------------------------------------------ план и старт

@capability("run_plan", title="План прогона", access="read")
def run_plan_get(project_id: str, mode: str, start_step: Optional[str] = None, step_id: Optional[str] = None,
                 shot_keys: Optional[list] = None, kinds: Optional[list] = None, rebuild_film: bool = False,
                 inputs: Optional[dict] = None, task: Optional[str] = None) -> dict:
    project_dir(project_id)
    plan = run_plan.build_plan(project_id, mode, start_step=start_step, step_id=step_id, shot_keys=shot_keys,
                               kinds=kinds, rebuild_film=rebuild_film, inputs=inputs, task=task)
    return success(plan=plan)


def _discard_unfinished(engine, project_id: str, keep_workflow_id: str, root: Optional[Path] = None) -> None:
    """«Начать сначала»: старые незавершённые прогоны помечаются отменёнными (best effort), новый не трогаем.

    Их id запоминаются: цепочка resume на отброшенном прогоне обрывается."""
    items = []
    for item in run_plan.find_runs(engine, project_id, limit=20):  # новые первыми
        if item["status"] == "completed":
            break
        if item["workflow_id"] != keep_workflow_id:
            items.append(item)
    if root is not None:
        try:
            run_plan.add_discarded(root, [i["workflow_id"] for i in items])
        except OSError as exc:
            logger.warning("Не записан список отброшенных прогонов: %s", exc)
    for item in items:
        if item["status"] == "cancelled":
            continue  # уже отменён, в список отброшенных он записан выше
        try:
            run_plan.run_coro(engine.cancel_workflow(item["workflow_id"], reason="Начать сначала (studio)"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отменить %s: %s", item["workflow_id"], exc)


@capability("run_start", title="Запуск прогона", access="run")
def run_start(project_id: str, mode: str, start_step: Optional[str] = None, step_id: Optional[str] = None,
              shot_keys: Optional[list] = None, kinds: Optional[list] = None, rebuild_film: bool = False,
              inputs: Optional[dict] = None, task: Optional[str] = None, launcher: str = "backend",
              expected_plan_digest: Optional[str] = None, discard_unfinished: bool = False,
              abandon_pending_jobs: bool = False, image_parts: Optional[list] = None,
              force_regenerate: bool = False) -> dict:
    root = _root(project_id)
    if launcher not in run_launchers.LAUNCHERS:
        raise StudioError("invalid_argument", "launcher: backend или local")
    plan = run_plan.build_plan(project_id, mode, start_step=start_step, step_id=step_id, shot_keys=shot_keys,
                               kinds=kinds, rebuild_film=rebuild_film, inputs=inputs, task=task, strict=True)
    if mode == "regenerate_shots":  # договор Ф4-Ф7: уходит в review.prepare_regeneration
        plan["scope"]["abandon_pending_jobs"] = bool(abandon_pending_jobs)
        if image_parts:
            plan["scope"]["image_parts"] = list(image_parts)
    if mode == "single_step" and force_regenerate:  # музыка из Ф7: не брать из кэша
        plan["scope"]["force_regenerate"] = True
    busy = [p for p in plan["problems"] if p["code"] == "project_busy"]
    if busy:
        raise run_control.locked_error(root)
    if expected_plan_digest and expected_plan_digest != plan["plan_digest"]:
        raise StudioError("conflict", "План изменился с момента показа, проверьте его ещё раз",
                          reason="plan_changed", plan_digest=plan["plan_digest"])
    if not plan["can_start"]:
        errors = [p for p in plan["problems"] if p["level"] == "error"]
        raise StudioError("conflict", errors[0]["message"] if errors else "План нельзя запустить",
                          reason="plan_blocked", problems=errors)
    run_id = run_launchers.new_run_id(launcher)
    run_control.reserve(root, run_launchers.reservation_record(run_id, plan, launcher))
    try:
        run_launchers.start(project_id, plan, run_id, launcher)
    except BaseException:
        run_control.release_reservation(root, run_id)
        raise
    if discard_unfinished:  # только после успешного старта: упавший старт не должен стереть старые прогоны
        _discard_unfinished(run_exec.get_engine(), project_id, plan["workflow_id"], root)
    return success(run_id=run_id, workflow_id=plan["workflow_id"], plan=plan)


# ------------------------------------------------------------------ статус

def _jobs(root: Path) -> list[dict]:
    return read_jobs(root)


def _shot_total(root: Path, known: set[str]) -> int:
    try:
        data = json.loads((root / "97_shots" / "shots.json").read_text(encoding="utf-8"))
        items = data.get("shots") if isinstance(data, dict) else data
        if isinstance(items, list) and items:
            return len(items)
    except (OSError, UnicodeError, ValueError):
        pass
    return len(known)


def video_progress(root: Path) -> dict:
    """K из N: различные шоты со скачанным видео против числа шотов."""
    done: set[str] = set()
    failed: set[str] = set()
    known: set[str] = set()
    for job in _jobs(root):
        key = job.get("shot_key")
        if not isinstance(key, str):
            continue
        known.add(key)
        status = str(job.get("status") or "").lower()
        if status in _DOWNLOADED:
            done.add(key)
        elif status in _FAILED:
            failed.add(key)
    return {"done": len(done), "total": max(_shot_total(root, known), len(known)), "failed": len(failed - done)}


def _job_seconds(job: dict) -> Optional[float]:
    stamps = job.get("timestamps") if isinstance(job.get("timestamps"), dict) else {}
    from .step_runs import _job_time
    start = _job_time({"timestamps": {"created_at": stamps.get("created_at") or stamps.get("submitting_at")}})
    end = _job_time({"timestamps": {"created_at": stamps.get("updated_at")}})
    return (end - start) if start is not None and end is not None and end >= start else None


def compute_eta(root: Path, plan_steps: list[str], finished: set[str], running: list[str],
                started_at: dict[str, float]) -> dict:
    """Критический путь по оставшимся шагам на медианах прошлых прогонов."""
    pipeline = run_plan._studio("status")._load_pipeline()
    deps = {s["id"]: [d for d in s["depends_on"]] for s in pipeline["steps"]}
    remaining = [s for s in plan_steps if s not in finished]
    if not remaining:
        return {"seconds": 0, "basis": "history"}
    now = time.time()
    video = video_progress(root)
    durations: dict[str, Optional[float]] = {}
    for sid in remaining:
        value = median_duration(root, sid)
        if sid == "video_generator":
            secs = [_job_seconds(j) for j in _jobs(root) if str(j.get("status")).lower() in _DOWNLOADED]
            secs = [x for x in secs if x is not None]
            left = max(video["total"] - video["done"], 0)
            if video["done"] >= 2 and secs:
                value = left * (sum(secs) / len(secs))
        if value is not None and sid in running and sid in started_at:
            value = max(value - (now - started_at[sid]), 0.0)
        durations[sid] = value
    missing = [s for s in remaining if durations[s] is None]
    if len(missing) > MIN_HISTORY_SHARE * len(remaining):
        return {"seconds": None, "basis": "no_history"}
    remaining_set = set(remaining)
    memo: dict[str, float] = {}

    def finish_time(sid: str, seen: frozenset = frozenset()) -> float:
        if sid in memo:
            return memo[sid]
        before = [finish_time(d, seen | {sid}) for d in deps.get(sid, []) if d in remaining_set and d not in seen]
        memo[sid] = max(before, default=0.0) + float(durations[sid] or 0.0)
        return memo[sid]

    return {"seconds": int(round(max(finish_time(s) for s in remaining))), "basis": "history"}


def _step_rows(root: Path, plan_steps: list[str], run_id: Optional[str]) -> list[dict]:
    steps = read_step_runs(root)["steps"]
    rows = []
    for sid in plan_steps:
        rec = steps.get(sid) or {}
        mine = rec.get("run_id") == run_id
        rows.append({
            "id": sid, "status": rec.get("status") if mine else "pending",
            "started_at": rec.get("started_at") if mine else None,
            "finished_at": rec.get("finished_at") if mine else None,
            "duration_s": rec.get("duration_s") if mine else None,
            "error": rec.get("error") if mine else None,
        })
    return rows


def _budget(project_id: str) -> Optional[float]:
    try:
        value = run_plan._studio("run_settings").settings_to_inputs(project_id).get("budget_rub")
    except Exception:  # noqa: BLE001
        return None
    return float(value) if value else None


def _public_state(info: dict) -> str:
    return "running" if info["state"] == "queued" else info["state"]


def _other_surface(active: dict, state: str) -> Optional[dict]:
    surface = active.get("surface")
    if state not in ACTIVE_STATES or surface not in ("tk", "react"):
        return None
    where = "Tk" if surface == "tk" else "React"
    return {"surface": surface,
            "text": f"Прогон запущен в приложении {where}. Здесь доступны наблюдение, пауза и остановка."}


def _unfinished(project_id: str) -> list[dict]:
    engine = run_plan.cached_engine()
    if engine is None:
        return []
    return run_plan.find_runs(engine, project_id, unfinished_only=True, limit=5, cached=True)


def _progress_dict(active: dict, state: str, control: dict) -> dict:
    return {"run_id": active.get("run_id"), "mode": active.get("mode"), "surface": active.get("surface"),
            "step_id": active.get("step_id"), "step_index": active.get("step_index"),
            "step_total": active.get("step_total"), "percent": active.get("percent"),
            "message": active.get("message"), "state": state,
            "paused": state == "paused", "pause_requested": bool(control.get("pause_requested")),
            "cancel_requested": bool(control.get("cancel_requested"))}


@capability("run_status", title="Состояние прогона", access="read")
def run_status(project_id: str, run_id: Optional[str] = None) -> dict:
    root = _root(project_id)
    info = run_control.compute_state(root)
    active = info["active"]
    if run_id is not None:
        run_logs.check_run_id(run_id)
        if active.get("run_id") != run_id:
            raise StudioError("not_found", "Прогон не найден")
    state = _public_state(info)
    live = state in ACTIVE_STATES
    steps_plan = list(active.get("steps") or [])
    steps = _step_rows(root, steps_plan, active.get("run_id")) if steps_plan else []
    started_ts = float(active.get("started_ts") or 0)
    cost = {"spent_rub": jobs_cost_since(root, started_ts) if started_ts else 0.0,
            "budget_rub": _budget(project_id), "scope": "video_jobs"}
    eta = None
    if live and steps_plan:
        finished = {r["id"] for r in steps if r["status"] in ("completed", "skipped", "failed")}
        started = {r["id"]: _iso_ts(r["started_at"]) for r in steps if r["status"] == "running" and r["started_at"]}
        eta = compute_eta(root, steps_plan, finished, list(active.get("running") or []),
                          {k: v for k, v in started.items() if v})
    failure = None
    if state == "failed":
        failed_step = next((r for r in steps if r["status"] == "failed"), None)
        failure = {"step_id": failed_step["id"] if failed_step else None,
                   "message": (failed_step or {}).get("error") or active.get("error")}
    engine = run_plan.engine_status()
    return success(
        state=state, locked=info["locked"], run=active or None, other_surface=_other_surface(active, state),
        progress=_progress_dict(active, state, info["control"]) if active else None, steps=steps,
        video=video_progress(root), cost=cost, eta=eta, control=info["control"],
        unresponsive=info["unresponsive"], heartbeat_age_s=info["heartbeat_age_s"], failure=failure,
        outcome=active.get("outcome"), resumable=state in ("failed", "interrupted") or bool(_unfinished(project_id)),
        engine=engine, stale_control=info["stale_control"])


def _iso_ts(value: Any) -> Optional[float]:
    from datetime import datetime
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ пауза, остановка, пометка

def _require_active(root: Path, run_id: Optional[str]) -> tuple[dict, str]:
    info = run_control.compute_state(root)
    if not (info["locked"] or info["queued"]):
        raise StudioError("conflict", "Нет активного прогона")
    active_id = info["active"].get("run_id") or info["control"].get("run_id")
    if run_id is not None:
        run_logs.check_run_id(run_id)
        if run_id != active_id:
            raise StudioError("conflict", "Это уже другой прогон")
    if not active_id:
        raise StudioError("conflict", "Нет активного прогона")
    return info, active_id


@capability("run_set_pause", title="Пауза прогона", access="run")
def run_set_pause(project_id: str, paused: bool, run_id: Optional[str] = None) -> dict:
    root = _root(project_id)
    if not isinstance(paused, bool):
        raise StudioError("invalid_argument", "paused: нужно true или false")
    _, active_id = _require_active(root, run_id)
    if run_control.control_request(root, active_id, pause=paused, by="react", expected_run_id=active_id) is None:
        raise StudioError("conflict", "Это уже другой прогон")
    state = _public_state(run_control.compute_state(root))
    return success(state=state, message=("Пауза сработает после текущего шага" if paused
                                          else "Прогон продолжится"))


@capability("run_cancel", title="Остановка прогона", access="run")
def run_cancel(project_id: str, run_id: Optional[str] = None, force: bool = False) -> dict:
    root = _root(project_id)
    info, active_id = _require_active(root, run_id)
    if force:
        if info["active"].get("launcher") != "backend":
            raise StudioError("unsupported", "Жёсткая остановка есть только у прогонов из React")
        try:
            run_launchers.cancel_backend(active_id)
        except Exception as exc:  # noqa: BLE001
            raise StudioError("unavailable", "Бэкенд не принял жёсткую остановку") from exc
    if run_control.control_request(root, active_id, cancel=True, by="react", expected_run_id=active_id) is None:
        raise StudioError("conflict", "Это уже другой прогон")
    if info["queued"] and not info["locked"] and info["active"].get("launcher") == "backend":
        try:  # прогон ещё в очереди и исполнителя нет
            run_launchers.cancel_backend(active_id)
        except Exception:  # noqa: BLE001
            pass
    state = _public_state(run_control.compute_state(root))
    return success(state=state, message="Прогон остановится после текущего шага" if not force
                   else "Прогон остановлен принудительно")


@capability("run_mark_interrupted", title="Пометить прогон прерванным", access="write")
def run_mark_interrupted(project_id: str, run_id: str) -> dict:
    root = _root(project_id)
    run_logs.check_run_id(run_id)
    if run_control.probe_lock(root):
        raise StudioError("locked", "Прогон ещё держит проект, пометить его прерванным нельзя",
                          pids=run_control.list_lock_holders(root))
    active = run_control.read_active(root)
    if active.get("run_id") != run_id:
        raise StudioError("not_found", "Прогон не найден")
    run_control.update_active(root, run_id, state="interrupted", outcome="interrupted",
                              message="Помечен прерванным", running=[], step_id=None)
    run_control.control_clear(root, run_id)
    engine = run_plan.cached_engine()
    workflow_id = active.get("workflow_id")
    if engine is not None and workflow_id:
        try:
            run_plan.run_coro(engine.cancel_workflow(workflow_id, reason="Прогон прерван (studio)"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Чекпоинт %s не отменён: %s", workflow_id, exc)
    return success(state="interrupted")


# ------------------------------------------------------------------ журнал, история, каталог шагов

def _latest_log_id(root: Path) -> Optional[str]:
    folder = run_logs.logs_dir(root)
    files = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    return files[0].stem if files else None


@capability("run_log_tail", title="Журнал прогона", access="read")
def run_log_tail(project_id: str, run_id: Optional[str] = None, lines: int = 500,
                 after_offset: Optional[int] = None) -> dict:
    root = _root(project_id)
    if run_id is None:
        run_id = run_control.read_active(root).get("run_id") or _latest_log_id(root)
        if not run_id:
            raise StudioError("not_found", "Журнал прогона не найден")
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        raise StudioError("invalid_argument", "lines: положительное число")
    if after_offset is not None and (isinstance(after_offset, bool) or not isinstance(after_offset, int)
                                     or after_offset < 0):
        raise StudioError("invalid_argument", "after_offset: неотрицательное число")
    return success(**run_logs.tail(root, run_id, lines, after_offset))


@capability("run_history", title="История прогонов", access="read")
def run_history(project_id: str, limit: int = DEFAULT_HISTORY) -> dict:
    root = _root(project_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StudioError("invalid_argument", "limit: положительное число")
    limit = min(limit, MAX_HISTORY)
    unfinished_ids = {item["workflow_id"] for item in _unfinished(project_id)}
    records = run_control.read_history(root)
    active = run_control.read_active(root)
    if active.get("run_id") and active.get("state") in ("running", "paused", "pause_requested", "stopping",
                                                          "interrupted") \
            and all(r.get("run_id") != active["run_id"] for r in records):
        records.append({"run_id": active["run_id"], "workflow_id": active.get("workflow_id"),
                        "mode": active.get("mode"), "surface": active.get("surface"),
                        "started_at": active.get("started_at"), "finished_at": None,
                        "outcome": active.get("outcome")})
    rows = []
    for rec in reversed(records[-limit:]):
        unfinished = rec.get("workflow_id") in unfinished_ids or rec.get("outcome") in (None, "interrupted")
        rows.append({"run_id": rec.get("run_id"), "mode": rec.get("mode"), "surface": rec.get("surface"),
                     "started_at": rec.get("started_at"), "finished_at": rec.get("finished_at"),
                     "outcome": rec.get("outcome"), "unfinished": bool(unfinished),
                     "resumable": bool(rec.get("workflow_id") in unfinished_ids)})
    return success(runs=rows)


@capability("run_steps_get", title="Шаги пайплайна", access="read", project_scoped=False)
def run_steps_get() -> dict:
    return success(steps=run_plan.steps_catalog())


# ------------------------------------------------------------------ хуки для Ф3 и Ф10 (не возможности)

def run_progress(project_id: str) -> Optional[dict]:
    """Прогресс активного прогона для статуса проекта; нет прогона - None."""
    root = project_dir(project_id)
    info = run_control.compute_state(root)
    if not (info["locked"] or info["queued"]):
        return None
    return _progress_dict(info["active"], _public_state(info), info["control"])


def active_run_info(project_id: str) -> dict:
    """Контракт Ф10: идёт ли прогон и какие шаги выполняются сейчас."""
    root = project_dir(project_id)
    info = run_control.compute_state(root)
    active = bool(info["locked"] or info["queued"])
    if not active:
        return {"active": False, "current_steps": None}
    running = info["active"].get("running")
    return {"active": True, "current_steps": list(running) if isinstance(running, list) else None}
