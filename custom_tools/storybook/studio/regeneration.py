"""Перегенерация шотов (Ф7): область, журнал, prepare / finish / recover, защита оплаченных задач.

Инструменты генерации не правим: перед запуском (под замком проекта) переносим старые файлы
в _versions, и инструмент сам рисует заново. Журнал .studio/regeneration.json пишется ДО
переноса, по нему после сбоя файлы возвращаются на место.
Вспомогательный модуль: возможностей (@capability) здесь нет, они в review.py.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import review_state as rs
from . import shot_versions as sv
from .errors import StudioError
from .paths import project_dir, studio_dir
from .reports import make_shot_key, parse_shot_key, shot_sort_key

logger = logging.getLogger(__name__)

WHATS = ("images", "video", "both", "music", "film")
JOURNAL = "regeneration.json"
LOG = "regeneration.log.jsonl"
LOG_MAX_BYTES = 2 * 1024 * 1024
IMAGE_STEP = "artist_batch_shots"
VIDEO_STEP = "video_generator"
MUSIC_STEP = "storybook_music_generator"
FILM_STEP = "montage_assembler"
PENDING_JOB_STATUSES = frozenset({"submitting", "submitted", "pending", "in_progress", "completed", "downloading",
                                  "poll_timeout", "download_failed"})
TERMINAL_JOB_STATUSES = frozenset({"downloaded", "failed", "stale", "cancelled"})


def is_job_pending(job: Any) -> bool:
    """Оплаченная задача ещё в работе: известный нетерминальный статус или есть task_id/video_url без итога."""
    if not isinstance(job, dict):
        return False
    status = job.get("status")
    if status in TERMINAL_JOB_STATUSES:
        return False
    return status in PENDING_JOB_STATUSES or bool(job.get("task_id") or job.get("video_url"))
MUSIC_FILES = ("98_audio/music_manifest.json", "98_audio/audio_manifest.json")
FILM_FILES = ("99_final/final_video.mp4", "99_final/timeline.fcpxml", "99_final/subtitles.srt",
              "99_final/manifest.json", "99_final/asset_manifest.json", "99_final/edit_decisions.json",
              "99_final/render_report.json", "99_final/final_review.json")
ALL_IMAGE_PARTS = ("start", "end")


_mod = sv._mod
_optional = sv._optional


def error_result(exc: StudioError) -> dict:
    """Ответ-ошибка в обоих видах: плоский (его читает Ф4) и вложенный (общий формат studio)."""
    return {"status": "error", "code": exc.code, "message": exc.message,
            "error": {"code": exc.code, "message": exc.message, **({"details": exc.details} if exc.details else {})}}


# ------------------------------------------------------------------ файлы: журнал, намерение, лог

def _sdir(project_id: str) -> Path:
    return studio_dir(project_id, create=True)


def read_journal(project_id: str) -> dict | None:
    try:
        data = json.loads((studio_dir(project_id) / JOURNAL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_journal(project_id: str, journal: dict) -> None:
    sv._write_json(_sdir(project_id) / JOURNAL, journal)


def drop_journal(project_id: str) -> None:
    (studio_dir(project_id) / JOURNAL).unlink(missing_ok=True)


def append_log(project_id: str, record: dict) -> None:
    path = _sdir(project_id) / LOG
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(LOG + ".1"))
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_ts(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ область

def check_what(what: Any) -> str:
    if what not in WHATS:
        raise StudioError("invalid_argument", "what: " + ", ".join(WHATS))
    return what


def check_image_parts(parts: Any) -> tuple[str, ...]:
    if parts is None:
        return ALL_IMAGE_PARTS
    if not isinstance(parts, (list, tuple)) or not parts or any(p not in ALL_IMAGE_PARTS for p in parts):
        raise StudioError("invalid_argument", "image_parts: start и/или end")
    return tuple(p for p in ALL_IMAGE_PARTS if p in parts)


def check_keys(project_id: str, keys: Any) -> list[str]:
    return rs.check_keys(project_id, keys)


def expand_video_keys(project_id: str, keys: list[str]) -> list[str]:
    root = project_dir(project_id)
    out: list[str] = []
    for key in keys:
        for k in sv.unit_keys_for(root, key):
            if k not in out:
                out.append(k)
    return out


def _frame_key(item: dict) -> str | None:
    return make_shot_key(item.get("scene_number"), item.get("shot_number"))


def _resolved(value: Any) -> str | None:
    return str(Path(str(value)).resolve()) if value else None


def image_frames(project_id: str) -> tuple[dict, list[dict]]:
    """(shots.json, кадры после video_plan.select_image_items). Нет зависимой картинки -> conflict."""
    doc = sv._read_json(project_dir(project_id) / sv.SHOTS_REL)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise StudioError("unavailable", "Файл шотов не читается; откройте его в редакторе файлов")
    from custom_tools.storybook.video_plan import select_image_items
    try:
        frames = select_image_items(project_id, doc["items"])
    except ValueError as exc:
        raise StudioError("conflict", f"Не хватает картинки, от которой зависит шот: {exc}",
                          reason="missing_image_dependency") from exc
    return doc, frames


def linked_closure(frames: list[dict], keys: list[str], image_parts: tuple[str, ...]) -> list[str]:
    """Шоты, чей начальный кадр копируется из конца выбранного (copy_from_previous_end)."""
    affected = set(keys)
    linked: list[str] = []
    changed = {_resolved(f.get("output_path")) for f in frames
               if _frame_key(f) in affected and str(f.get("shot_type") or "start").lower() in image_parts}
    changed.discard(None)
    while True:
        copied = [k for f in frames
                  if (k := _frame_key(f)) and k not in affected and f.get("copy_from_previous_end")
                  and f.get("source_end_path") and _resolved(f["source_end_path"]) in changed]
        if not copied:
            return linked
        for k in copied:
            if k not in affected:
                affected.add(k)
                linked.append(k)
        # перерисовывается только скопированный кадр (его end остаётся прежним)
        changed |= {_resolved(f.get("output_path")) for f in frames
                    if _frame_key(f) in copied and f.get("copy_from_previous_end")}


def build_items(doc: dict, frames: list[dict], keys: list[str], linked: list[str],
                image_parts: tuple[str, ...]) -> dict:
    """Свежий срез items для artist_batch_shots (та же форма, что у continuity_review)."""
    scoped = []
    for item in frames:
        key = _frame_key(item)
        phase = str(item.get("shot_type") or "start").lower()
        if (key in keys and phase in image_parts) or (key in linked and item.get("copy_from_previous_end")):
            scoped.append(item)
    return {**doc, "items": scoped, "scope_shot_keys": sorted(set(keys) | set(linked))}


# ------------------------------------------------------------------ задачи провайдера

def _jobs_doc(root: Path) -> tuple[Any, list]:
    data = sv._read_json(root / sv.JOBS_REL)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return data, (jobs if isinstance(jobs, list) else [])


def _job_keys(job: dict) -> set[str]:
    keys = {parse_shot_key(p) for p in str(job.get("shot_key") or "").split("|")}
    keys.add(parse_shot_key(str(job.get("output_path") or "")))
    keys.discard(None)
    return keys


def pending_paid_jobs(root: Path, keys: list[str]) -> list[dict]:
    _, jobs = _jobs_doc(root)
    out = []
    for job in jobs:
        if is_job_pending(job) and _job_keys(job) & set(keys):
            out.append({"shot_key": job.get("shot_key"), "status": job.get("status"), "provider": job.get("provider")})
    return out


def mark_jobs_stale(root: Path, keys: list[str], regen_id: str, abandon: bool) -> list[dict]:
    """Все не stale задачи шотов -> stale (чтобы инструмент создал новую, а не скачал старую)."""
    path = root / sv.JOBS_REL
    if not path.is_file():
        return []
    from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobsFileLock, _atomic_write_json
    marker = f"Regeneration requested {regen_id}"
    changed: list[dict] = []
    with _ProviderJobsFileLock(path.with_name(f"{path.name}.lock")):
        data, jobs = _jobs_doc(root)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for job in jobs:
            if not isinstance(job, dict) or job.get("status") == "stale" or not (_job_keys(job) & set(keys)):
                continue
            if is_job_pending(job) and not abandon:
                raise StudioError("conflict", "По шоту уже отправлена оплаченная задача. Сначала дождитесь её "
                                  "результата, иначе вы заплатите дважды", reason="paid_job_pending",
                                  keys=sorted(_job_keys(job) & set(keys)))
            changed.append({"shot_key": job.get("shot_key"), "task_id": job.get("task_id"),
                            "input_hash": job.get("input_hash"), "prior_status": job.get("status"),
                            "prior_error": job.get("error"), "abandoned": is_job_pending(job)})
            job["status"], job["error"] = "stale", marker
            stamps = job.setdefault("timestamps", {})
            stamps["updated_at"] = stamps["stale_at"] = now
        if changed:
            payload = data if isinstance(data, dict) else {"version": 2, "jobs": jobs}
            _atomic_write_json(path, payload)
    return changed


def restore_jobs(root: Path, changed: list[dict], regen_id: str, only_keys: set[str] | None = None) -> None:
    path = root / sv.JOBS_REL
    if not changed or not path.is_file():
        return
    from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobsFileLock, _atomic_write_json
    marker = f"Regeneration requested {regen_id}"
    with _ProviderJobsFileLock(path.with_name(f"{path.name}.lock")):
        data, jobs = _jobs_doc(root)
        dirty = False
        for prior in changed:
            if only_keys is not None and not (_job_keys({"shot_key": prior["shot_key"]}) & only_keys):
                continue
            for job in jobs:
                if (isinstance(job, dict) and job.get("status") == "stale" and job.get("error") == marker
                        and job.get("shot_key") == prior["shot_key"] and job.get("task_id") == prior["task_id"]
                        and job.get("input_hash") == prior["input_hash"]):
                    job["status"] = prior["prior_status"]
                    if prior["prior_error"] is None:
                        job.pop("error", None)
                    else:
                        job["error"] = prior["prior_error"]
                    dirty = True
        if dirty:
            _atomic_write_json(path, data if isinstance(data, dict) else {"version": 2, "jobs": jobs})


def new_pending_jobs(root: Path, keys: set[str], since: float) -> set[str]:
    """Шоты, у которых после since появилась незавершённая (оплаченная) задача."""
    _, jobs = _jobs_doc(root)
    found: set[str] = set()
    for job in jobs:
        if not is_job_pending(job):
            continue
        created = _parse_ts((job.get("timestamps") or {}).get("created_at"))
        if created >= since - 1 and _job_keys(job) & keys:
            found |= _job_keys(job) & keys
    return found


# ------------------------------------------------------------------ план операций

def _music_files(root: Path) -> list[str]:
    music = root / sv.AUDIO_ROOT / "music"
    tracks = sorted(p for p in music.glob("*.mp3") if p.is_file()) if music.is_dir() else []
    return [p.relative_to(root).as_posix() for p in tracks] + list(MUSIC_FILES)


def dry_run(project_id: str, keys: list[str], what: str, *, image_parts: tuple[str, ...] = ALL_IMAGE_PARTS,
            with_film: bool = False) -> dict:
    """Что будет снято в версии и какие оплаченные задачи висят. Ничего не пишет."""
    root = project_dir(project_id)
    will: list[dict] = []
    linked: list[str] = []
    expanded = list(keys)
    if what in ("images", "both"):
        doc, frames = image_frames(project_id)
        linked = linked_closure(frames, keys, image_parts)
    if what in ("video", "both"):
        expanded = expand_video_keys(project_id, keys)
    seen_files: set[str] = set()
    for step in _shot_ops_plan_v2(keys, linked, expanded, what, image_parts):
        try:
            collected = sv.collect_parts(project_id, step["key"], step["parts"])
        except StudioError:
            continue
        for part, info in collected.items():
            primary = str(info["primary"])
            if primary in seen_files:
                continue
            seen_files.add(primary)
            will.append({"key": step["key"], "part": part, "size": info["primary"].stat().st_size, "mode": "move",
                         "reason": step["reason"]})
    if what == "music":
        will += [{"key": None, "part": "music", "size": (root / r).stat().st_size, "mode": "copy", "reason": "music"}
                 for r in _music_files(root) if (root / r).is_file()]
    if what == "film" or with_film:
        will += [{"key": None, "part": "film", "size": (root / r).stat().st_size, "mode": "copy", "reason": "film"}
                 for r in FILM_FILES if (root / r).is_file()]
    job_keys = expanded if what in ("video", "both") else []
    return {"will_snapshot": will, "linked_keys": linked, "expanded_keys": expanded,
            "pending_paid_jobs": pending_paid_jobs(root, job_keys) if job_keys else [],
            "disk_needed_bytes": sum(w["size"] for w in will if w["mode"] == "copy")}


def _shot_ops_plan_v2(keys: list[str], linked: list[str], expanded: list[str], what: str,
                      image_parts: tuple[str, ...]) -> list[dict]:
    plan: list[dict] = []
    if what in ("images", "both"):
        plan += [{"key": k, "parts": list(image_parts), "reason": "regenerate_images"} for k in keys]
        plan += [{"key": k, "parts": ["start"], "reason": "linked_copy"} for k in linked]
    if what in ("video", "both"):
        plan += [{"key": k, "parts": ["video"], "reason": "regenerate_video"} for k in expanded]
    return plan


# ------------------------------------------------------------------ замок проекта

def lock_held(project_id: str) -> bool:
    status = _mod("status")
    return bool(status._probe_lock(project_dir(project_id)))


def acquire_lock(project_id: str):
    """Замок проекта без ожидания: (handle) или locked-ошибка."""
    paths = importlib.import_module("custom_tools.storybook.project_paths")
    handle = paths.acquire_storybook_project_lock(project_id)
    if handle is None:
        runs = _optional("run_control")
        if runs is not None and hasattr(runs, "locked_error"):
            raise runs.locked_error(project_dir(project_id))
        raise StudioError("locked", "Проект занят прогоном. Дождитесь его конца или остановите его")
    return handle


def release_lock(handle) -> None:
    importlib.import_module("custom_tools.storybook.project_paths").release_storybook_project_lock(handle)


# ------------------------------------------------------------------ prepare

def _new_regen_id() -> str:
    return "rg-" + uuid.uuid4().hex[:12]


def _plan_ops(project_id: str, keys: list[str], linked: list[str], expanded: list[str], what: str,
              image_parts: tuple[str, ...], with_film: bool, regen_id: str, surface: str | None) -> list[dict]:
    """Создать каталоги версий и описать операции (файлы ещё не двигаем)."""
    root = project_dir(project_id)
    ops: list[dict] = []
    taken: set[str] = set()
    try:
        for step in _shot_ops_plan_v2(keys, linked, expanded, what, image_parts):
            collected = sv.collect_parts(project_id, step["key"], step["parts"])
            collected = {p: i for p, i in collected.items() if str(i["primary"]) not in taken}
            if not collected:
                continue
            taken |= {str(i["primary"]) for i in collected.values()}
            op = sv.plan_shot_op(project_id, step["key"], collected, mode="move", reason=step["reason"],
                                 regen_id=regen_id, surface=surface)
            if op is not None:
                ops.append(op)
        if what == "music":
            op = sv.plan_area_op(project_id, sv.AUDIO_ROOT, _music_files(root), reason="music", regen_id=regen_id,
                                 surface=surface)
            if op is not None:
                ops.append(op)
        if what == "film" or with_film:
            op = sv.plan_area_op(project_id, sv.FILM_ROOT, list(FILM_FILES), reason="film", regen_id=regen_id,
                                 surface=surface)
            if op is not None:
                ops.append(op)
    except BaseException:
        for op in ops:  # каталоги версий, под которые ещё нет журнала, не оставляем сиротами
            shutil.rmtree(root / op["version_rel"], ignore_errors=True)
        raise
    return ops


def prepare_regeneration(project_id: str, shot_keys: Any, what: str, *, steps_to_run: list[str] | None = None,
                         abandon_pending_jobs: bool = False, image_parts: Any = ALL_IMAGE_PARTS,
                         surface: str | None = None) -> dict:
    """Хук Ф4 (под замком проекта): версии, пометка задач stale, область картинок. Ответ - см. F7 §5.4."""
    try:
        return _prepare(project_id, shot_keys, what, steps_to_run, abandon_pending_jobs, image_parts, surface)
    except StudioError as exc:
        return error_result(exc)


def _prepare(project_id, shot_keys, what, steps_to_run, abandon, image_parts, surface) -> dict:
    root = project_dir(project_id)
    what = check_what(what)
    image_parts = check_image_parts(image_parts)
    if not lock_held(project_id):
        raise StudioError("internal", "prepare_regeneration нужно вызывать под замком проекта")
    recover_pending(project_id, lock_held=True)
    keys = check_keys(project_id, shot_keys) if what in ("images", "video", "both") else []
    steps = list(steps_to_run) if steps_to_run is not None else None
    with_film = what in ("images", "video", "both") and steps is not None and FILM_STEP in steps
    step_outputs: dict = {}
    linked: list[str] = []
    expanded = list(keys)
    if what in ("images", "both"):
        doc, frames = image_frames(project_id)
        linked = linked_closure(frames, keys, image_parts)
        step_outputs = {"background_references": build_items(doc, frames, keys, linked, image_parts)}
    if what in ("video", "both"):
        expanded = expand_video_keys(project_id, keys)
        pending = pending_paid_jobs(root, expanded)
        if pending and not abandon:
            raise StudioError("conflict", "По шоту уже отправлена оплаченная задача. Сначала дождитесь её "
                              "результата («Продолжить после сбоя»), иначе вы заплатите дважды",
                              reason="paid_job_pending", keys=sorted({str(p["shot_key"]) for p in pending}))
    regen_id = _new_regen_id()
    probe = dry_run(project_id, keys, what, image_parts=image_parts, with_film=with_film)
    if probe["disk_needed_bytes"]:
        sv.check_space(root, probe["disk_needed_bytes"])
    ops = _plan_ops(project_id, keys, linked, expanded, what, image_parts, with_film, regen_id, surface)
    journal = {"version": 1, "regen_id": regen_id, "state": "prepared", "what": what, "surface": surface,
               "run_id": None, "prepared_at": sv.now_iso(), "keys": keys, "expanded_keys": expanded,
               "linked_keys": linked, "ops": ops, "jobs": []}
    write_journal(project_id, journal)  # сначала журнал, потом перенос
    try:
        for op in ops:
            sv.apply_op(project_id, op)
            write_journal(project_id, journal)
        if what in ("video", "both"):
            journal["jobs"] = mark_jobs_stale(root, expanded, regen_id, abandon)
            write_journal(project_id, journal)
    except BaseException:
        _rollback_all(project_id, journal)
        drop_journal(project_id)
        raise
    warnings = []
    if linked:
        warnings.append({"code": "linked_copy_may_pay",
                         "message": "Связанные кадры будут скопированы; если копия не удастся, кадр нарисуют платно"})
    if journal["jobs"] and any(j["abandoned"] for j in journal["jobs"]):
        warnings.append({"code": "pending_jobs_abandoned",
                         "message": "Незавершённые оплаченные задачи помечены устаревшими по вашему подтверждению"})
    step_params: dict = {}
    if what == "music":
        step_params[MUSIC_STEP] = {"force_regenerate": True}
    return {"status": "success", "regen_id": regen_id, "step_outputs": step_outputs, "step_params": step_params,
            "snapshots": [{"key": o["key"], "part": p, "version_id": o["version_id"]}
                          for o in ops for p in (o["parts"] or [o["scope"]])],
            "warnings": warnings, "expanded_keys": expanded, "linked_keys": linked}


def _rollback_all(project_id: str, journal: dict) -> None:
    root = project_dir(project_id)
    for op in reversed(journal.get("ops") or []):
        try:
            sv.rollback_op(project_id, op)
        except OSError:
            logger.exception("rollback %s", op.get("version_rel"))
    try:
        restore_jobs(root, journal.get("jobs") or [], journal["regen_id"])
    except Exception:  # noqa: BLE001
        logger.exception("restore_jobs")


# ------------------------------------------------------------------ finish / recover

def _part_equal(root: Path, op: dict, part: str | None) -> bool:
    """Файлы части (или все файлы операции) на месте байт-в-байт равны снимку."""
    vdir = root / op["version_rel"]
    for item in op["files"]:
        if part is not None and item.get("part") != part:
            continue
        cur, old = root / item["rel"], vdir / item["name"]
        if not cur.is_file() or not old.is_file() or sv.sha256_file(cur) != sv.sha256_file(old):
            return False
    return True


def _was_moved(root: Path, op: dict, part: str) -> bool:
    """Старый файл части уже лежит в каталоге версии (перенос состоялся)."""
    vdir = root / op["version_rel"]
    return any((vdir / item["name"]).exists() for item in op["files"] if item.get("part") == part)


def _new_file_ready(root: Path, op: dict, part: str) -> bool:
    """Новый файл есть, и он новый: старый уже убран в версию (иначе это старый, перенос не успел)."""
    if not _was_moved(root, op, part):
        return False
    for item in op["files"]:
        if item.get("part") == part:
            path = root / item["rel"]
            return path.is_file() and path.stat().st_size > 0
    return False


def finish_regeneration(project_id: str, regen_id: str, *, since_ts: float | None = None, outcome: str = "") -> dict:
    """Хук Ф4 (в finally под замком, после reconcile_after_run): успех -> версии остаются, нет файла -> откат."""
    journal = read_journal(project_id)
    if journal is None or journal.get("regen_id") != regen_id:
        return {"status": "success", "replaced": [], "rolled_back": [], "awaiting_paid_job": [],
                "review_reset": [], "skipped": True}
    return {"status": "success", **_finish_journal(project_id, journal, outcome or "completed")}


def _finish_journal(project_id: str, journal: dict, outcome: str) -> dict:
    root = project_dir(project_id)
    prepared = _parse_ts(journal.get("prepared_at"))
    replaced: list[dict] = []
    rolled: list[dict] = []
    awaiting: set[str] = set()
    all_keys = set(journal.get("expanded_keys") or []) | set(journal.get("linked_keys") or [])
    paid_new = new_pending_jobs(root, all_keys, prepared) if all_keys else set()
    restore_keys: set[str] = set()
    for op in journal.get("ops") or []:
        if op["scope"] == "shot" and op["mode"] == "move":
            back: set[str] = set()
            for part in op["parts"]:
                unit = ((op["meta"].get("parts") or {}).get(part) or {}).get("unit_keys") or [op["key"]]
                if not _was_moved(root, op, part):
                    continue  # авария до переноса: файл не тронут, версии нет
                if _new_file_ready(root, op, part):
                    replaced += [{"key": k, "part": part} for k in unit]
                elif set(unit) & paid_new:
                    awaiting |= set(unit) & paid_new  # оплаченный результат ещё придёт: откат запрещён
                else:
                    back.add(part)
            if back:
                for part in sv.rollback_op(project_id, op, only_parts=back):
                    unit = ((op["meta"].get("parts") or {}).get(part) or {}).get("unit_keys") or [op["key"]]
                    rolled += [{"key": k, "part": part} for k in unit]
                    restore_keys |= set(unit)
            sv._remove_if_empty(root / op["version_rel"])  # пустой каталог версии убираем
        else:  # копии: дубль без изменений убираем, иначе версия остаётся
            changed_parts = [p for p in (op["parts"] or [None]) if not _part_equal(root, op, p)]
            if not changed_parts:
                shutil.rmtree(root / op["version_rel"], ignore_errors=True)
            elif op["scope"] == "shot":
                replaced += [{"key": op["key"], "part": p} for p in changed_parts]
    if restore_keys:
        restore_jobs(root, journal.get("jobs") or [], journal["regen_id"], only_keys=restore_keys)
    keys_changed = sorted({r["key"] for r in replaced}, key=shot_sort_key)
    reset: list[str] = []
    if keys_changed:
        rec = sv._optional("shots")
        if rec is not None and hasattr(rec, "reconcile_after_run"):
            try:
                rec.reconcile_after_run(project_id, since_ts=prepared, shot_keys=keys_changed)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_after_run")
        state = sv._optional("review_state")
        if state is not None:
            parts = sorted({r["part"] for r in replaced})
            reset = state.mark_content_changed(project_id, keys_changed, "regenerated:" + "+".join(parts),
                                               journal["regen_id"])
    result = {"replaced": replaced, "rolled_back": rolled, "awaiting_paid_job": sorted(awaiting),
              "review_reset": reset}
    try:
        append_log(project_id, {"at": sv.now_iso(), "regen_id": journal["regen_id"], "what": journal.get("what"),
                                "state": journal.get("state"), "outcome": outcome, "keys": journal.get("keys"),
                                **result})
    except OSError:
        logger.exception("regeneration log")
    drop_journal(project_id)
    return result


def recover_pending(project_id: str, *, lock_held: bool = False) -> dict:
    """Довести до конца журнал упавшего процесса. Замок проекта занят другим -> ничего не трогаем."""
    journal = read_journal(project_id)
    if journal is None:
        return {"recovered": False, "rolled_back": []}
    handle = None
    if not lock_held:
        paths = importlib.import_module("custom_tools.storybook.project_paths")
        handle = paths.acquire_storybook_project_lock(project_id)
        if handle is None:
            return {"recovered": False, "rolled_back": [], "busy": True}
    try:
        journal = read_journal(project_id)
        if journal is None:
            return {"recovered": False, "rolled_back": []}
        result = _finish_journal(project_id, journal, "recovered")
        return {"recovered": True, "rolled_back": result["rolled_back"], "replaced": result["replaced"],
                "regen_id": journal.get("regen_id")}
    finally:
        if handle is not None:
            release_lock(handle)


def has_live_journal(project_id: str) -> bool:
    return read_journal(project_id) is not None


# ------------------------------------------------------------------ protect_before_run (P2)

def protect_before_run(project_id: str, steps_to_run: list[str] | None = None, *, surface: str | None = None) -> dict:
    """Хук Ф4 перед обычным запуском: копия частей шотов, которые Ф6 считает устаревшими."""
    shots = sv._optional("shots")
    if shots is None or not hasattr(shots, "stale_shots_summary"):
        return {"status": "skipped"}
    steps = set(steps_to_run or [])
    want_parts: list[str] = []
    if not steps or IMAGE_STEP in steps:
        want_parts += list(ALL_IMAGE_PARTS)
    if not steps or VIDEO_STEP in steps:
        want_parts.append("video")
    if not want_parts:
        return {"status": "skipped"}
    ops: list[dict] = []
    try:
        summary = shots.stale_shots_summary(project_id)
        if not summary or not summary.get("keys"):
            return {"status": "success", "snapshots": []}
        recover_pending(project_id, lock_held=True)
        regen_id = _new_regen_id()
        root = project_dir(project_id)
        for key in summary["keys"]:
            card = shots.shot_get(project_id, key)["shot"]
            parts = [p for p in want_parts if (card["parts"].get(p) or {}).get("freshness") == "stale"]
            collected = sv.collect_parts(project_id, key, parts) if parts else {}
            ops += sv.plan_shot_ops(project_id, key, collected, mode="copy", reason="protect", regen_id=regen_id,
                                    surface=surface, dedupe=True)
        need = sum(sum(p.stat().st_size for p in (root / f["rel"] for f in o["files"])) for o in ops)
        if ops:
            sv.check_space(root, need)
    except StudioError as exc:
        for op in ops:
            shutil.rmtree(project_dir(project_id) / op["version_rel"], ignore_errors=True)
        return error_result(exc)
    if not ops:
        return {"status": "success", "snapshots": []}
    journal = {"version": 1, "regen_id": regen_id, "state": "protect", "what": "protect", "surface": surface,
               "run_id": None, "prepared_at": sv.now_iso(), "keys": [o["key"] for o in ops],
               "expanded_keys": [o["key"] for o in ops], "linked_keys": [], "ops": ops, "jobs": []}
    write_journal(project_id, journal)
    try:
        for op in ops:
            sv.apply_op(project_id, op)
            write_journal(project_id, journal)
    except BaseException:
        _rollback_all(project_id, journal)
        drop_journal(project_id)
        raise
    return {"status": "success", "regen_id": regen_id,
            "snapshots": [{"key": o["key"], "part": p, "version_id": o["version_id"]} for o in ops for p in o["parts"]]}
