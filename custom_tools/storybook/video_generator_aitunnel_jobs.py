"""Durable provider job ledger for AITunnel storybook video generation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


_PROVIDER_NAME = "aitunnel"
_PROVIDER_JOBS_VERSION = 2
_CURRENT_HASH_INPUTS_VERSION = 2
_PROVIDER_JOBS_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_PROVIDER_JOBS_THREAD_LOCKS_GUARD = threading.Lock()


class _ProviderJobStore:
    def __init__(self, path: str, provider_name: str = _PROVIDER_NAME):
        self.path = Path(path)
        self.provider_name = provider_name
        self._lock = threading.RLock()
        self._data = self._load()

    def _lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def _acquire_file_lock(self):
        return _ProviderJobsFileLock(self._lock_path())

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": _PROVIDER_JOBS_VERSION, "jobs": []}

        with self.path.open("r", encoding="utf-8") as jobs_file:
            payload = json.load(jobs_file)

        if isinstance(payload, list):
            data = {"version": _PROVIDER_JOBS_VERSION, "jobs": payload}
        elif isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            data = {
                "version": payload.get("version", _PROVIDER_JOBS_VERSION),
                "jobs": payload["jobs"],
            }
        else:
            raise ValueError(f"Неверная структура provider_jobs.json: {self.path}")

        # Legacy jobs predate per-job hash_inputs_version (M-6): treat as v1 so the
        # migration compat measures (no-stale + shot-identity resume) engage.
        for job in data["jobs"]:
            if isinstance(job, dict):
                job.setdefault("hash_inputs_version", 1)
        return data

    def mark_stale_for_changed_input(self, shot_key: str, input_hash: str) -> None:
        now = _utc_now_iso()
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            changed = False
            for job in self._data["jobs"]:
                if job.get("provider") != self.provider_name:
                    continue
                if job.get("shot_key") != shot_key:
                    continue
                if job.get("hash_inputs_version", 1) != _CURRENT_HASH_INPUTS_VERSION:
                    # M-6 compat: a hash-formula bump changes input_hash for unchanged
                    # shots; never stale a legacy job just because of that, or its live
                    # task_id would be lost and the paid task resubmitted.
                    continue
                if job.get("input_hash") == input_hash or job.get("status") == "stale":
                    continue
                job["status"] = "stale"
                job["error"] = "Input hash changed"
                _touch_job(job, now, "stale_at")
                changed = True
            if changed:
                self._save_locked()

    def ensure_job(self, job_data: Dict[str, Any]) -> Dict[str, Any]:
        now = _utc_now_iso()
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            job = self._find_latest_locked(job_data["shot_key"], job_data["input_hash"])
            if job is not None and job.get("status") == "failed":
                job = None
            if job is None:
                job = job_data.copy()
                job["timestamps"] = dict(job_data.get("timestamps") or {})
                job["timestamps"].setdefault("created_at", now)
                job["timestamps"]["updated_at"] = now
                self._data["jobs"].append(job)
            else:
                for key, value in job_data.items():
                    if key == "timestamps":
                        continue
                    job.setdefault(key, value)
                _touch_job(job, now)
            self._save_locked()
            return _copy_job(job)

    def find_current_job(self, shot_key: str, input_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            job = self._find_latest_locked(shot_key, input_hash)
            return _copy_job(job) if job else None

    def find_resumable_job(
        self,
        shot_key: str,
        input_hash: str,
        prompt_hash: Optional[str] = None,
        source_image_hashes: Optional[Dict[str, Optional[str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            for job in reversed(self._data["jobs"]):
                if job.get("provider") != self.provider_name:
                    continue
                if not _job_matches_identity(
                    job,
                    shot_key,
                    input_hash,
                    prompt_hash,
                    source_image_hashes,
                    allow_legacy_identity=self.provider_name == _PROVIDER_NAME,
                ):
                    continue
                if job.get("status") in {"stale", "failed", "prepared"}:
                    continue
                return _copy_job(job)
            return None

    def find_task_id_for_resubmit(
        self,
        shot_key: str,
        input_hash: str,
        prompt_hash: Optional[str] = None,
        source_image_hashes: Optional[Dict[str, Optional[str]]] = None,
    ) -> Optional[str]:
        """task_id of a prior non-resumable job for the same shot inputs.

        Used to verify (single free GET) whether an already-paid task actually
        completed before spending money on a resubmit; matches by input_hash or,
        for legacy-hash jobs, by shot identity (M-6 migration).
        """
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            for job in reversed(self._data["jobs"]):
                if job.get("provider") != self.provider_name:
                    continue
                if not job.get("task_id"):
                    continue
                if job.get("status") == "stale":
                    continue
                if not _job_matches_identity(
                    job,
                    shot_key,
                    input_hash,
                    prompt_hash,
                    source_image_hashes,
                    allow_legacy_identity=self.provider_name == _PROVIDER_NAME,
                ):
                    continue
                return job.get("task_id")
            return None

    def find_latest_output_job_for_shot(self, shot_key: str, output_path: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            for job in reversed(self._data["jobs"]):
                if job.get("provider") != self.provider_name:
                    continue
                if job.get("shot_key") != shot_key:
                    continue
                if job.get("output_path") != output_path:
                    continue
                if job.get("status") in {"stale", "failed"}:
                    continue
                return _copy_job(job)
            return None

    def has_job_for_shot(self, shot_key: str) -> bool:
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            return any(
                job.get("provider") == self.provider_name and job.get("shot_key") == shot_key
                for job in self._data["jobs"]
            )

    def update_job(
        self,
        shot_key: str,
        input_hash: str,
        updates: Dict[str, Any],
        timestamp_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _utc_now_iso()
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            job = self._find_latest_locked(shot_key, input_hash)
            if job is None:
                job = _new_provider_job(
                    shot_key=shot_key,
                    model=str(updates.get("model") or ""),
                    prompt_hash=str(updates.get("prompt_hash") or ""),
                    source_image_hashes=updates.get("source_image_hashes") or {},
                    input_hash=input_hash,
                    output_path=str(updates.get("output_path") or ""),
                    provider_name=self.provider_name,
                )
                self._data["jobs"].append(job)

            job.update(updates)
            _touch_job(job, now, timestamp_key)
            self._save_locked()
            return _copy_job(job)

    def claim_submitting_job(
        self,
        shot_key: str,
        input_hash: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically mark a prepared job as submitting before a paid POST."""
        now = _utc_now_iso()
        with self._lock, self._acquire_file_lock():
            self._data = self._load()
            job = self._find_latest_locked(shot_key, input_hash)
            if job is None:
                job = _new_provider_job(
                    shot_key=shot_key,
                    model=str(updates.get("model") or ""),
                    prompt_hash=str(updates.get("prompt_hash") or ""),
                    source_image_hashes=updates.get("source_image_hashes") or {},
                    input_hash=input_hash,
                    output_path=str(updates.get("output_path") or ""),
                    provider_name=self.provider_name,
                )
                self._data["jobs"].append(job)

            if (
                job.get("task_id")
                or job.get("video_url")
                or job.get("status") not in {None, "prepared", "failed"}
            ):
                return {"claimed": False, "job": _copy_job(job)}

            job.update(updates)
            job["status"] = "submitting"
            _touch_job(job, now, "submitting_at")
            self._save_locked()
            return {"claimed": True, "job": _copy_job(job)}

    def _find_latest_locked(self, shot_key: str, input_hash: str) -> Optional[Dict[str, Any]]:
        for job in reversed(self._data["jobs"]):
            if job.get("provider") != self.provider_name:
                continue
            if job.get("shot_key") != shot_key:
                continue
            if job.get("input_hash") != input_hash:
                continue
            if job.get("status") == "stale":
                continue
            return job
        return None

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, self._data)


def _copy_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(job))


def _job_matches_identity(
    job: Dict[str, Any],
    shot_key: str,
    input_hash: str,
    prompt_hash: Optional[str],
    source_image_hashes: Optional[Dict[str, Optional[str]]],
    allow_legacy_identity: bool = True,
) -> bool:
    if job.get("shot_key") != shot_key:
        return False
    if job.get("input_hash") == input_hash:
        return True
    # M-6 compat: a legacy-hash job has a different input_hash for the same shot;
    # fall back to shot identity so its live task_id is reused, not resubmitted.
    if (
        allow_legacy_identity
        and job.get("hash_inputs_version", 1) != _CURRENT_HASH_INPUTS_VERSION
        and prompt_hash is not None
    ):
        return (
            job.get("prompt_hash") == prompt_hash
            and (job.get("source_image_hashes") or {}) == (source_image_hashes or {})
        )
    return False


class _ProviderJobsFileLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self._thread_lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = _provider_jobs_thread_lock(self.path)
        self._thread_lock.acquire()
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._handle is not None:
                try:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass
                self._handle.close()
        finally:
            self._handle = None
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None


def _provider_jobs_thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROVIDER_JOBS_THREAD_LOCKS_GUARD:
        lock = _PROVIDER_JOBS_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROVIDER_JOBS_THREAD_LOCKS[key] = lock
        return lock


def _new_provider_job(
    shot_key: str,
    model: str,
    prompt_hash: str,
    source_image_hashes: Dict[str, Optional[str]],
    input_hash: str,
    output_path: str,
    resolved_size_params: Optional[Dict[str, str]] = None,
    resolved_duration: Optional[int] = None,
    provider_name: str = _PROVIDER_NAME,
) -> Dict[str, Any]:
    return {
        "shot_key": shot_key,
        "provider": provider_name,
        "model": model,
        "prompt_hash": prompt_hash,
        "source_image_hashes": source_image_hashes,
        "input_hash": input_hash,
        "hash_inputs_version": _CURRENT_HASH_INPUTS_VERSION,
        "resolved_size_params": resolved_size_params,
        "resolved_duration": resolved_duration,
        "task_id": None,
        "status": "prepared",
        "provider_status": None,
        "cost": None,
        "currency": None,
        "output_path": output_path,
        "video_url": None,
        "video_url_requires_auth": None,
        "error": None,
        "timestamps": {},
    }


def _touch_job(job: Dict[str, Any], now: str, timestamp_key: Optional[str] = None) -> None:
    timestamps = job.setdefault("timestamps", {})
    timestamps["updated_at"] = now
    if timestamp_key:
        timestamps[timestamp_key] = now


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(encoded)


def _hash_source_image(image_ref: Optional[str]) -> Optional[str]:
    if not image_ref:
        return None
    if _is_url_or_data_url(image_ref):
        return _hash_text(image_ref)

    path = Path(image_ref)
    if not path.exists():
        return None

    digest = hashlib.sha256()
    with path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_input_hash(
    model_name: str,
    prompt_hash: str,
    source_image_hashes: Dict[str, Optional[str]],
    requested_duration: int,
    requested_width: int,
    requested_height: int,
    seed: Optional[int],
    frame_types: list[str],
    generate_audio: bool = False,
    provider_name: str = _PROVIDER_NAME,
) -> str:
    # M-6: hash only user-controlled inputs. The resolved duration/size come from
    # the provider's (process-cached, mutable) model catalog and must NOT enter the
    # hash, or a catalog change would force mass paid regeneration of unchanged shots.
    return _hash_json(
        {
            "provider": provider_name,
            "model": model_name,
            "prompt_hash": prompt_hash,
            "source_image_hashes": source_image_hashes,
            "requested_duration": requested_duration,
            "requested_width": requested_width,
            "requested_height": requested_height,
            "seed": seed,
            "generate_audio": generate_audio,
            "frame_types": frame_types,
        }
    )


def _is_url_or_data_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://") or value.startswith("data:")
