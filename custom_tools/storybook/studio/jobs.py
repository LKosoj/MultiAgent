"""Фоновые задания studio: функция в потоке, состояние в файле, опрос по job_id.

Запрос React ждёт не дольше 20 секунд, а бэкап проекта идёт минуты, поэтому долгие
операции возвращают job_id, а состояние читается отдельным вызовом.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .errors import StudioError

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
JOBS_DIRNAME = ".studio_jobs"
STALE_AFTER_S = 30 * 60
MAX_PARALLEL = 2
_WRITE_THROTTLE_S = 0.5

_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_active: set[str] = set()
_active_lock = threading.Lock()
_now = time.time  # подменяется в тестах


def _jobs_dir(root: Path) -> Path:
    return Path(root) / JOBS_DIRNAME


def _job_path(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise StudioError("invalid_argument", "Недопустимый идентификатор задания")
    return _jobs_dir(root) / f"{job_id}.json"


def _write(root: Path, record: dict) -> None:
    folder = _jobs_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record['job_id']}.json"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def start_job(root: Path, kind: str, project_id: str | None,
              func: Callable[[Callable[[int, int, str], None]], dict]) -> str:
    """Запустить func(progress) в потоке; вернуть job_id."""
    reap_old_jobs(root)
    job_id = uuid.uuid4().hex
    stamp = _now()
    record = {
        "job_id": job_id, "kind": kind, "project_id": project_id, "state": "running",
        "progress": {"current": 0, "total": 0, "message": "В очереди"},
        "pid": os.getpid(), "created_at": stamp, "updated_at": stamp,
    }
    from custom_tools.storybook.project_paths import current_series_scope
    scope = current_series_scope()
    if scope and scope[0] == project_id:
        record["series_id"] = scope[1]
    context = contextvars.copy_context()
    # сначала _active, потом файл: иначе read_job между ними увидит «running» без живого потока
    with _active_lock:
        _active.add(job_id)
    try:
        _write(root, record)
        threading.Thread(target=context.run, args=(_run, Path(root), record, func), name=f"studio-job-{job_id[:6]}",
                         daemon=True).start()
    except BaseException:
        with _active_lock:
            _active.discard(job_id)
        raise
    return job_id


def _run(root: Path, record: dict, func) -> None:
    last_write = [0.0]

    def progress(current: int, total: int, message: str = "") -> None:
        record["progress"] = {"current": current, "total": total, "message": message}
        moment = time.monotonic()
        if moment - last_write[0] >= _WRITE_THROTTLE_S or current >= total:
            last_write[0] = moment
            record["updated_at"] = _now()
            _try_write(root, record)

    try:
        # прогон пайплайна живёт часами и не должен занимать общий слот коротких заданий
        with (contextlib.nullcontext() if record.get("kind") == "run" else _slots):
            progress(0, 0, "Выполняется")
            try:
                record["result"] = func(progress)
                record["state"] = "done"
            except StudioError as exc:
                record["state"] = "error"
                record["error"] = {"code": exc.code, "message": exc.message}
            except Exception:
                logger.exception("studio job %s (%s) failed", record["job_id"], record["kind"])
                record["state"] = "error"
                record["error"] = {"code": "internal", "message": "Внутренняя ошибка"}
        record["updated_at"] = _now()
        _try_write(root, record)
    finally:
        with _active_lock:
            _active.discard(record["job_id"])


def _try_write(root: Path, record: dict) -> None:
    try:
        _write(root, record)
    except OSError:
        logger.warning("Не удалось записать состояние задания %s", record.get("job_id"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _effective(record: dict) -> dict:
    """Зависшее «running» (сервер перезапустили) превращаем в «interrupted»."""
    if record.get("state") != "running":
        return record
    with _active_lock:
        if record.get("job_id") in _active:
            return record
    pid = record.get("pid")
    stale = _now() - float(record.get("updated_at") or 0) > STALE_AFTER_S
    own_dead = pid == os.getpid()  # поток этого процесса уже не работает
    if own_dead or stale or not isinstance(pid, int) or not _pid_alive(pid):
        return {**record, "state": "interrupted"}
    return record


def _effective_fresh(path: Path, record: dict) -> dict:
    """_effective с защитой от гонки: поток мог дописать итог и выйти из _active после чтения файла.

    Итог пишется в файл раньше, чем задание уходит из _active, поэтому при «interrupted»
    достаточно перечитать файл: если там уже не «running», верим файлу.
    """
    result = _effective(record)
    if result.get("state") == "interrupted" and record.get("state") == "running":
        try:
            fresh = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return result
        if fresh.get("state") != "running":
            return fresh
    return result


def read_job(root: Path, job_id: str) -> dict:
    path = _job_path(root, job_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudioError("not_found", "Задание не найдено") from exc
    return _effective_fresh(path, record)


def active_project_ids(root: Path, kind: str) -> set[str]:
    """project_id заданий данного вида, которые ещё выполняются."""
    result: set[str] = set()
    folder = _jobs_dir(root)
    if not folder.is_dir():
        return result
    for path in folder.glob("*.json"):
        try:
            record = _effective_fresh(path, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if record.get("kind") == kind and record.get("state") == "running" and record.get("project_id"):
            result.add(str(record["project_id"]))
    return result


def reap_old_jobs(root: Path, max_age_s: int = 86400) -> None:
    """Удалить файлы завершённых заданий старше max_age_s."""
    folder = _jobs_dir(root)
    if not folder.is_dir():
        return
    for path in folder.glob("*.json"):
        try:
            record = _effective(json.loads(path.read_text(encoding="utf-8")))
            if record.get("state") != "running" and _now() - float(record.get("updated_at") or 0) > max_age_s:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
