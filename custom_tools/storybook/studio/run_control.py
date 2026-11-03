"""Управление прогоном: замок, `.studio/control.json`, `.studio/active_run.json`, бронь.

Общий слой для React и Tk. Писатель `active_run.json` - держатель замка проекта
(и короткая бронь при постановке в очередь). `control.json` пишут обе стороны
(запрос паузы/отмены) под отдельным коротким замком.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from custom_tools.storybook import project_paths

from .errors import StudioError
from .project_store import write_json_atomic

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
STUDIO_DIRNAME = ".studio"
CONTROL_NAME = "control.json"
ACTIVE_NAME = "active_run.json"
RESERVE_TTL_S = 120.0
HEARTBEAT_STALE_S = 60.0
LOCK_RETRIES = 5
LOCK_RETRY_DELAY_S = 0.1
_MAX_JSON_BYTES = 1024 * 1024
_LIVE_STATES = frozenset({"running", "paused", "pause_requested", "stopping", "queued"})

_now = time.time  # подменяется в тестах


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(_now() if ts is None else ts).isoformat(timespec="seconds")


def _studio(root: Path) -> Path:
    return Path(root) / STUDIO_DIRNAME


@contextlib.contextmanager
def _file_lock(path: Path, timeout_s: float = 5.0) -> Iterator[None]:
    """Короткий замок на файле рядом с данными (блокирующий, с ограничением по времени)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise StudioError("locked", "Служебный файл прогона занят, повторите запрос") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ замок проекта

def probe_lock(root: Path) -> bool:
    """Занят ли замок проекта (только чтение, через общую пробу Ф3)."""
    status = importlib.import_module(f"{_PACKAGE}.status")
    return bool(status._probe_lock(Path(root)))


def acquire_lock_with_retries(project_id: str):
    """Замок проекта; пробы Ф3/Ф10 берут LOCK_SH на миг, поэтому пара коротких повторов."""
    for attempt in range(LOCK_RETRIES):
        handle = project_paths.acquire_storybook_project_lock(project_id)
        if handle is not None:
            return handle
        if attempt < LOCK_RETRIES - 1:
            time.sleep(LOCK_RETRY_DELAY_S)
    return None


def release_lock(handle) -> None:
    project_paths.release_storybook_project_lock(handle)


# ------------------------------------------------------------------ control.json

def control_path(root: Path) -> Path:
    return _studio(root) / CONTROL_NAME


def read_control(root: Path) -> dict:
    return _read_json(control_path(root))


def _control_lock(root: Path):
    return _file_lock(_studio(root) / "control.lock")


def control_create(root: Path, run_id: str, surface: str, workflow_id: str | None) -> dict:
    """Создаёт control.json нового прогона; ранний запрос паузы/отмены этого же run_id сохраняется."""
    with _control_lock(root):
        old = read_control(root)
        keep = old if old.get("run_id") == run_id else {}
        data = {
            "version": 1, "run_id": run_id, "surface": surface, "workflow_id": workflow_id,
            "pause_requested": bool(keep.get("pause_requested")),
            "cancel_requested": bool(keep.get("cancel_requested")),
            "requested_by": keep.get("requested_by"), "requested_at": keep.get("requested_at"),
            "ack": {"pause": False, "cancel": False}, "paused": False,
            "updated_at": iso(), "seq": int(keep.get("seq") or 0) + 1,
        }
        write_json_atomic(control_path(root), data)
        return data


def control_request(root: Path, run_id: str | None, *, pause: bool | None = None,
                    cancel: bool | None = None, by: str = "react",
                    expected_run_id: str | None = None) -> dict | None:
    """Запрос паузы/отмены. Чужой run_id игнорируется (None). Создаёт файл для брони без исполнителя."""
    with _control_lock(root):
        data = read_control(root)
        target = run_id or data.get("run_id")
        if not target or (data.get("run_id") and data.get("run_id") != target):
            return None
        if expected_run_id and target != expected_run_id:
            return None
        if not data:
            data = {"version": 1, "run_id": target, "surface": by, "workflow_id": None,
                    "pause_requested": False, "cancel_requested": False,
                    "ack": {"pause": False, "cancel": False}, "paused": False, "seq": 0}
        if pause is not None:
            data["pause_requested"] = bool(pause)
            if not pause:
                data["ack"]["pause"] = False
        if cancel:
            data["cancel_requested"] = True
        data["requested_by"] = by
        data["requested_at"] = iso()
        data["updated_at"] = iso()
        data["seq"] = int(data.get("seq") or 0) + 1
        write_json_atomic(control_path(root), data)
        return data


def control_patch(root: Path, run_id: str, **fields: Any) -> bool:
    """Исполнитель отмечает ack/paused. Файл другого прогона не трогаем."""
    with _control_lock(root):
        data = read_control(root)
        if data.get("run_id") != run_id:
            return False
        for key, value in fields.items():
            if key == "ack" and isinstance(value, dict):
                data["ack"] = {**(data.get("ack") or {}), **value}
            else:
                data[key] = value
        data["updated_at"] = iso()
        write_json_atomic(control_path(root), data)
        return True


def control_clear(root: Path, run_id: str) -> None:
    with _control_lock(root):
        data = read_control(root)
        if data.get("run_id") == run_id:
            with contextlib.suppress(OSError):
                control_path(root).unlink()


# ------------------------------------------------------------------ active_run.json и бронь

def active_path(root: Path) -> Path:
    return _studio(root) / ACTIVE_NAME


def read_active(root: Path) -> dict:
    return _read_json(active_path(root))


def _runs_lock(root: Path):
    return _file_lock(_studio(root) / "runs.lock")


def write_active(root: Path, record: dict) -> None:
    with _runs_lock(root):
        write_json_atomic(active_path(root), record)


def update_active(root: Path, run_id: str, **fields: Any) -> bool:
    """Правка записи только своего прогона."""
    with _runs_lock(root):
        data = read_active(root)
        if data.get("run_id") != run_id:
            return False
        data.update(fields)
        write_json_atomic(active_path(root), data)
        return True


def effective_record(active: dict, now: float | None = None) -> dict:
    """Запись с учётом брони: протухшая бронь заменяется тем, что было до неё."""
    now = _now() if now is None else now
    if active.get("state") != "queued":
        return active
    if now - float(active.get("reserved_ts") or 0) < RESERVE_TTL_S:
        return active
    prev = active.get("prev")
    return prev if isinstance(prev, dict) else {}


def locked_error(root: Path, active: dict | None = None) -> StudioError:
    record = effective_record(active if active is not None else read_active(root))
    live = record.get("state") in _LIVE_STATES
    details = {
        "surface": record.get("surface") if live else None,
        "run_id": record.get("run_id") if live else None,
        "step": record.get("step_id") if live else None,
        "started_at": record.get("started_at") if live else None,
    }
    where = {"tk": "Tk", "react": "React"}.get(details["surface"] or "")
    text = (f"Прогон запущен в приложении {where}. Здесь доступны наблюдение, пауза и остановка."
            if where else "Проект уже выполняется. Дождитесь конца прогона или остановите его.")
    return StudioError("locked", text, **details)


def reserve(root: Path, record: dict) -> dict:
    """Бронь места под запуск. Занято замком или чужой свежей бронью -> locked."""
    with _runs_lock(root):
        active = read_active(root)
        if probe_lock(root):
            raise locked_error(root, active)
        current = effective_record(active)
        if active.get("state") == "queued" and current is active and active.get("run_id") != record.get("run_id"):
            raise locked_error(root, active)
        prev = current if current.get("state") != "queued" else current.get("prev")
        data = {**record, "state": "queued", "reserved_ts": _now(), "prev": prev or None}
        write_json_atomic(active_path(root), data)
        return data


def release_reservation(root: Path, run_id: str) -> None:
    """Снять бронь (запуск не удался): возвращаем прежнюю запись."""
    with _runs_lock(root):
        active = read_active(root)
        if active.get("run_id") != run_id or active.get("state") != "queued":
            return
        prev = active.get("prev")
        if isinstance(prev, dict) and prev:
            write_json_atomic(active_path(root), prev)
        else:
            with contextlib.suppress(OSError):
                active_path(root).unlink()


HISTORY_NAME = "run_history.json"
HISTORY_KEEP = 50


def read_history(root: Path) -> list[dict]:
    data = _read_json(_studio(root) / HISTORY_NAME)
    runs = data.get("runs")
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def append_history(root: Path, record: dict) -> None:
    """Короткая запись о завершённом прогоне (последние HISTORY_KEEP)."""
    with _runs_lock(root):
        runs = [r for r in read_history(root) if r.get("run_id") != record.get("run_id")]
        runs.append(record)
        write_json_atomic(_studio(root) / HISTORY_NAME, {"version": 1, "runs": runs[-HISTORY_KEEP:]})


# ------------------------------------------------------------------ вычисление состояния

def compute_state(root: Path, now: float | None = None) -> dict:
    """Единое состояние прогона проекта. Словарь состояний общий с Ф3 (`status._active_run`)."""
    now = _now() if now is None else now
    locked = probe_lock(root)
    raw_active = read_active(root)
    active = effective_record(raw_active, now)
    control = read_control(root)
    stale_control = bool(control) and not locked
    pause = bool(control.get("pause_requested")) and not stale_control
    cancel = bool(control.get("cancel_requested")) and not stale_control
    paused = bool(control.get("paused")) and not stale_control
    queued = raw_active.get("state") == "queued" and active is raw_active
    heartbeat = active.get("heartbeat_ts")
    age = (now - float(heartbeat)) if isinstance(heartbeat, (int, float)) else None
    unresponsive = False
    if locked or queued:
        if cancel:
            state = "stopping"
        elif pause:
            state = "paused" if paused else "pause_requested"
        else:
            state = "running"
        unresponsive = (locked and active.get("state") in _LIVE_STATES - {"queued"}
                        and age is not None and age > HEARTBEAT_STALE_S)
    else:
        recorded = active.get("state")
        if recorded in _LIVE_STATES or recorded == "interrupted":
            state = "interrupted"
        elif recorded in ("completed", "failed"):
            state = recorded
        else:
            state = "idle"
    return {
        "state": state, "locked": locked, "queued": queued, "active": active,
        "control": control if not stale_control else {}, "stale_control": stale_control,
        "unresponsive": unresponsive, "heartbeat_age_s": age,
    }


def list_lock_holders(root: Path) -> list[int]:
    """Best-effort: pid процессов, у которых открыт файл замка (по /proc)."""
    target = Path(root) / ".pipeline.lock"
    pids: list[int] = []
    try:
        real = target.resolve()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                for fd in (entry / "fd").iterdir():
                    if os.path.realpath(fd) == str(real):
                        pids.append(int(entry.name))
                        break
            except OSError:
                continue
    except OSError:
        return []
    return pids
