"""Версии файлов шотов, музыки и фильма (Ф7): снимки, список, сравнение, возврат.

Версия - неизменяемый снимок файлов перед заменой. Лежит в 97_shots/_versions/<ключ>/<id>/,
98_audio/_versions/<id>/ и 99_final/_versions/<id>/. Версии не удаляются (правило О5).
Публичный Python-API для Ф11: snapshot_move / snapshot_copy.
Вспомогательный модуль: возможностей (@capability) здесь нет, они в review.py.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StudioError
from .paths import project_dir
from .reports import make_shot_key, parse_shot_key

VERSIONS = "_versions"
SHOTS_ROOT = "97_shots"
AUDIO_ROOT = "98_audio"
FILM_ROOT = "99_final"
SHOTS_REL = "97_shots/shots.json"
PLAN_REL = "97_shots/video_plan.json"
JOBS_REL = "97_shots/provider_jobs.json"
PARTS = ("start", "end", "video")
MIN_FREE_MARGIN = 200 * 1024 * 1024
DEFAULT_WARN_GB = 10.0

REASON_TITLES = {
    "regenerate_images": "Перед перегенерацией картинок",
    "regenerate_video": "Перед перегенерацией видео",
    "linked_copy": "Связанный кадр (копия конца предыдущего шота)",
    "restore": "Перед возвратом версии",
    "protect": "Перед запуском (кадр устарел)",
    "blockout": "Перед перерисовкой болванки",
    "film": "Перед пересборкой фильма",
    "music": "Перед перегенерацией музыки",
    "image_edit": "Перед правкой картинки ИИ",
    "manual": "Сохранено вручную",
}
# строка шота в shots.json -> поля промптов
_PROMPT_FIELDS = {
    "start": ("start_prompt", "start_negative_prompt"),
    "end": ("end_prompt", "end_negative_prompt"),
    "video": ("video_prompt",),
}
_PROMPT_KEYS = {"start_prompt": "start", "end_prompt": "end", "video_prompt": "video",
                "start_negative_prompt": "negative_start", "end_negative_prompt": "negative_end"}


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def _optional(name: str):
    full = f"{__package__}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None
        raise


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_token(path: Path) -> str | None:
    return _mod("_shots_fp").file_token(path)


def sidecar_of(path: Path) -> Path:
    return path.with_suffix(".continuity.json")


# ------------------------------------------------------------------ каталоги

def shot_versions_dir(root: Path, key: str) -> Path:
    return root / SHOTS_ROOT / VERSIONS / key


def area_versions_dir(root: Path, area: str) -> Path:
    return root / area / VERSIONS


def allocate_dir(parent: Path) -> tuple[str, Path]:
    """Свободный каталог версии: время UTC до секунды, при совпадении суффикс -2, -3."""
    parent.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 1
    while True:
        vid = base if n == 1 else f"{base}-{n}"
        try:
            (parent / vid).mkdir()
            return vid, parent / vid
        except FileExistsError:
            n += 1


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    store = _mod("project_store")
    store.write_json_atomic(path, data)


def read_meta(version_dir: Path) -> dict | None:
    meta = _read_json(version_dir / "meta.json")
    return meta if isinstance(meta, dict) else None


def _version_dirs(parent: Path) -> list[Path]:
    try:
        return sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return []


# ------------------------------------------------------------------ промпты на момент снимка

def video_units(root: Path) -> list[dict]:
    data = _read_json(root / PLAN_REL)
    units = data.get("units") if isinstance(data, dict) else None
    out = []
    for unit in units or []:
        if isinstance(unit, dict) and isinstance(unit.get("shot_keys"), list):
            keys = [k for k in (parse_shot_key(x) for x in unit["shot_keys"]) if k]
            if keys:
                out.append({"keys": keys, "unit": unit})
    return out


def unit_keys_for(root: Path, key: str) -> list[str]:
    """Все шоты блока видео, в который входит key (или [key])."""
    for unit in video_units(root):
        if key in unit["keys"] and len(unit["keys"]) > 1:
            return list(unit["keys"])
    return [key]


def current_prompts(project_id: str, key: str) -> dict:
    """Тексты промптов шота из shots.json: start/end/video и негативные."""
    root = project_dir(project_id)
    data = _read_json(root / SHOTS_REL)
    items = data.get("items") if isinstance(data, dict) else None
    out = {"start": "", "end": "", "video": "", "negative_start": "", "negative_end": ""}
    for item in items or []:
        if not isinstance(item, dict) or make_shot_key(item.get("scene_number"), item.get("shot_number")) != key:
            continue
        phase = str(item.get("shot_type") or "").strip().lower()
        if phase == "start":
            out["start"] = str(item.get("english_prompt") or "")
            out["negative_start"] = str(item.get("negative_prompt") or "")
            out["video"] = str(item.get("video_prompt") or "")
        elif phase == "end":
            out["end"] = str(item.get("english_prompt") or "")
            out["negative_end"] = str(item.get("negative_prompt") or "")
    return out


def prompts_as_of(project_id: str, key: str, ts: str | None, edits: list[dict] | None = None) -> tuple[dict, bool]:
    """(промпты на момент ts, надёжно ли). Текущие тексты минус правки Ф6 после ts (от новых к старым)."""
    prompts = current_prompts(project_id, key)
    if not ts:
        return prompts, False
    if edits is None:
        fp = _mod("_shots_fp")
        edits = fp.read_edits(project_id, key, limit=10**6)
    trusted = True
    for entry in edits:  # read_edits отдаёт новые первыми
        at = entry.get("at")
        if not isinstance(at, str) or at <= ts:
            continue
        for name, change in (entry.get("fields") or {}).items():
            target = _PROMPT_KEYS.get(name)
            if target is None:
                continue
            if not isinstance(change, dict) or "old" not in change:
                trusted = False
                continue
            prompts[target] = str(change.get("old") or "")
    if trusted and _edits_rotated(project_id, ts, edits):
        trusted = False
    return prompts, trusted


def _edits_rotated(project_id: str, ts: str, edits: list[dict]) -> bool:
    """Журнал правок мог быть обрезан: есть архив .1, а самая старая запись новее ts."""
    fp = _mod("_shots_fp")
    archive = _mod("paths").studio_dir(project_id) / (fp.EDITS_FILE + ".1")
    if not archive.exists():
        return False
    path = _mod("paths").studio_dir(project_id) / fp.EDITS_FILE
    try:
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return True
    return str(first.get("at") or "") > ts


def matches_current(project_id: str, key: str, version_prompts: dict, parts: list[str],
                    cur: dict | None = None) -> dict:
    cur = cur if cur is not None else current_prompts(project_id, key)
    result: dict[str, bool | None] = {}
    for part in PARTS:
        if part not in parts:
            result[part] = None
            continue
        names = {"start": ("start", "negative_start"), "end": ("end", "negative_end"), "video": ("video",)}[part]
        result[part] = all((version_prompts or {}).get(n, "") == cur.get(n, "") for n in names)
    return result


# ------------------------------------------------------------------ задачи провайдера и ревью для meta

def last_job(root: Path, keys: list[str]) -> dict | None:
    """Последняя запись шота в provider_jobs.json по updated_at (правило Ф8)."""
    data = _read_json(root / JOBS_REL)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    best, best_ts = None, ""
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        shot = job.get("shot_key")
        job_keys = [parse_shot_key(p) for p in str(shot or "").split("|")]
        if not any(k in keys for k in job_keys):
            continue
        ts = str((job.get("timestamps") or {}).get("updated_at") or "")
        if best is None or ts >= best_ts:
            best, best_ts = job, ts
    return best


def job_brief(job: dict | None) -> dict | None:
    if not job:
        return None
    cost = job.get("cost")
    if cost is None and isinstance(job.get("pricing"), dict):
        cost = job["pricing"].get("cost")
    return {"provider": job.get("provider"), "model": job.get("model"), "status": job.get("status"),
            "cost": cost if isinstance(cost, (int, float)) else None, "currency": job.get("currency"),
            "input_hash": job.get("input_hash"), "prompt_hash": job.get("prompt_hash"),
            "resolved_duration": job.get("resolved_duration"), "task_id": job.get("task_id")}


# ------------------------------------------------------------------ части шота -> файлы

def collect_parts(project_id: str, key: str, parts: list[str]) -> dict[str, dict]:
    """{часть: {"primary": Path, "files": [Path], "unit_keys": [..]|None}} для существующих файлов шота."""
    shots = _mod("shots")
    root = project_dir(project_id)
    files = shots.shot_files(project_id, key)
    out: dict[str, dict] = {}
    for part in parts:
        path = files.get(part)
        if path is None:
            continue
        path = Path(path)
        unit = None
        if part == "video":
            mates = unit_keys_for(root, key)
            unit = mates if len(mates) > 1 else None
        group = [path]
        if part != "video" and sidecar_of(path).is_file():
            group.append(sidecar_of(path))
        out[part] = {"primary": path, "files": group, "unit_keys": unit}
    return out


def _plain_name(name: Any) -> bool:
    return isinstance(name, str) and bool(name) and name not in (".", "..") and not any(c in name for c in "/\\\0")


def safe_origin(root: Path, info: dict) -> Path:
    """Путь origin из meta.json: внутри 97_shots проекта, не в _versions, имя совпадает с primary."""
    primary, origin = info.get("primary"), info.get("origin")
    names = [primary] + list(info.get("files") or [])
    if not isinstance(origin, str) or not all(_plain_name(n) for n in names):
        raise StudioError("conflict", "Описание версии повреждено", reason="version_corrupt")
    path = (root / origin).resolve()
    shots_root = (root / SHOTS_ROOT).resolve()
    try:
        inside = path.relative_to(shots_root).parts
    except ValueError:
        raise StudioError("conflict", "Описание версии повреждено", reason="version_corrupt") from None
    if VERSIONS in inside or path.name != primary:
        raise StudioError("conflict", "Описание версии повреждено", reason="version_corrupt")
    return path


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def newest_sha(project_id: str, key: str, part: str) -> str | None:
    """sha256 основного файла самой свежей версии этой части шота (для дедупликации)."""
    root = project_dir(project_id)
    for vdir in reversed(_version_dirs(shot_versions_dir(root, key))):
        meta = read_meta(vdir)
        info = ((meta or {}).get("parts") or {}).get(part)
        if info and info.get("sha256"):
            return info["sha256"]
    return None


def plan_shot_op(project_id: str, key: str, parts: dict[str, dict], *, mode: str, reason: str,
                 regen_id: str | None = None, run_id: str | None = None, surface: str | None = None,
                 restored_from: str | None = None, dedupe: bool = False) -> dict | None:
    """Создать каталог версии и описать операцию (без переноса файлов). None - переносить нечего."""
    root = project_dir(project_id)
    if dedupe:
        parts = {p: info for p, info in parts.items()
                 if sha256_file(info["primary"]) != newest_sha(project_id, key, p)}
    if not parts:
        return None
    dir_key = key
    for info in parts.values():
        if info["unit_keys"]:
            dir_key = info["unit_keys"][0]
    vid, vdir = allocate_dir(shot_versions_dir(root, dir_key))
    try:
        return _build_shot_op(project_id, root, key, dir_key, vid, vdir, parts, mode, reason, regen_id, run_id,
                              surface, restored_from)
    except BaseException:
        shutil.rmtree(vdir, ignore_errors=True)
        raise


def _build_shot_op(project_id, root, key, dir_key, vid, vdir, parts, mode, reason, regen_id, run_id, surface,
                   restored_from) -> dict:
    shots = _optional("shots")
    meta_parts: dict[str, dict] = {}
    files: list[dict] = []
    baseline: dict[str, Any] = {}
    prompts: dict[str, str] = {}
    trusted = True
    for part, info in parts.items():
        primary: Path = info["primary"]
        meta_parts[part] = {
            "files": [p.name for p in info["files"]], "primary": primary.name, "origin": _rel(root, primary),
            "size": primary.stat().st_size, "sha256": sha256_file(primary), "token": file_token(primary),
            "unit_keys": info["unit_keys"],
        }
        for path in info["files"]:
            files.append({"rel": _rel(root, path), "name": path.name, "part": part})
        record = shots.get_baseline_record(project_id, key, part) if shots else None
        baseline[part] = record
        as_of, ok = prompts_as_of(project_id, key, (record or {}).get("at"))
        trusted = trusted and ok
        for field in _PROMPT_FIELDS[part]:
            name = _PROMPT_KEYS[field]
            prompts[name] = as_of[name]
    review = _optional("review_state")
    entry = review.read_for_hooks(project_id).get(key) if review else None
    meta = {
        "version": 1, "id": vid, "kind": "shot", "shot_key": dir_key, "created_at": now_iso(),
        "reason": reason, "reason_title": REASON_TITLES.get(reason, reason), "mode": mode,
        "regen_id": regen_id, "run_id": run_id, "surface": surface, "parts": meta_parts,
        "prompts": prompts, "prompts_trusted": bool(trusted), "baseline": baseline,
        "job": job_brief(last_job(root, parts_keys(parts, key))) if "video" in parts else None,
        "review_before": ({"status": entry.get("status"), "note": entry.get("note")} if entry else None),
        "restored_from": restored_from,
    }
    return {"scope": "shot", "key": key, "dir_key": dir_key, "parts": sorted(parts), "mode": mode, "reason": reason,
            "version_id": vid, "version_rel": _rel(root, vdir), "files": files, "done": False, "meta": meta}


def parts_keys(parts: dict[str, dict], key: str) -> list[str]:
    for info in parts.values():
        if info["unit_keys"]:
            return list(info["unit_keys"])
    return [key]


def plan_area_op(project_id: str, area: str, rels: list[str], *, reason: str, regen_id: str | None = None,
                 run_id: str | None = None, surface: str | None = None, dedupe: bool = True) -> dict | None:
    """Копия файлов 98_audio / 99_final в <area>/_versions/<id>/. rels - пути относительно проекта."""
    root = project_dir(project_id)
    rels = [r for r in rels if (root / r).is_file()]
    if not rels:
        return None
    names = [(r, Path(r).relative_to(area).as_posix()) for r in rels]
    shas = {r: sha256_file(root / r) for r, _ in names}
    if dedupe:
        for vdir in reversed(_version_dirs(area_versions_dir(root, area))):
            meta = read_meta(vdir)
            old = {f["name"]: f.get("sha256") for f in (meta or {}).get("area_files", [])}
            if old and old == {n: shas[r] for r, n in names}:
                return None
            break
    vid, vdir = allocate_dir(area_versions_dir(root, area))
    kind = "music" if area == AUDIO_ROOT else "film"
    meta = {"version": 1, "id": vid, "kind": kind, "created_at": now_iso(), "reason": reason,
            "reason_title": REASON_TITLES.get(reason, reason), "mode": "copy", "regen_id": regen_id,
            "run_id": run_id, "surface": surface,
            "area_files": [{"name": n, "origin": r, "size": (root / r).stat().st_size, "sha256": shas[r]}
                           for r, n in names]}
    return {"scope": kind, "key": None, "dir_key": None, "parts": [], "mode": "copy", "reason": reason,
            "version_id": vid, "version_rel": _rel(root, vdir),
            "files": [{"rel": r, "name": n, "part": None} for r, n in names], "done": False, "meta": meta}


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def apply_op(project_id: str, op: dict) -> None:
    """Выполнить операцию: meta.json, затем перенос (move) или копия (copy) файлов в каталог версии."""
    root = project_dir(project_id)
    vdir = root / op["version_rel"]
    _write_json(vdir / "meta.json", op["meta"])
    for item in op["files"]:
        src, dst = root / item["rel"], vdir / item["name"]
        if op["mode"] == "move":
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
        else:
            _copy_atomic(src, dst)
    op["done"] = True


def rollback_op(project_id: str, op: dict, *, drop_copies: bool = True, only_parts: set[str] | None = None) -> list[str]:
    """Вернуть перенесённые файлы на место (все или только only_parts). Возвращает вернувшиеся части."""
    root = project_dir(project_id)
    vdir = root / op["version_rel"]
    restored: list[str] = []
    if op["mode"] == "move":
        for item in op["files"]:
            if only_parts is not None and item.get("part") not in only_parts:
                continue
            src, dst = vdir / item["name"], root / item["rel"]
            stub = dst.is_file() and dst.stat().st_size == 0  # нулевая заглушка не мешает вернуть файл
            if src.exists() and (not dst.exists() or stub):
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                if item.get("part") and item["part"] not in restored:
                    restored.append(item["part"])
        meta = read_meta(vdir)
        if meta and only_parts and (vdir / "meta.json").exists():
            left = {p: i for p, i in (meta.get("parts") or {}).items() if p not in restored}
            if left != meta.get("parts"):
                _write_json(vdir / "meta.json", {**meta, "parts": left})
    elif drop_copies and vdir.is_dir():
        shutil.rmtree(vdir, ignore_errors=True)
        return restored
    _remove_if_empty(vdir)
    return restored


def _remove_if_empty(vdir: Path) -> None:
    """Каталог версии без файлов (остался только наш meta.json) - временный, убираем."""
    if not vdir.is_dir():
        return
    rest = [p for p in vdir.rglob("*") if p.is_file() and p.name != "meta.json"]
    if rest:
        return
    shutil.rmtree(vdir, ignore_errors=True)


def plan_shot_ops(project_id: str, key: str, collected: dict[str, dict], **kw) -> list[dict]:
    """Операции шота: видео блока (каталог первого ключа блока) отдельно от картинок шота."""
    ops = []
    for group in ({p: i for p, i in collected.items() if p != "video"},
                  {p: i for p, i in collected.items() if p == "video"}):
        op = plan_shot_op(project_id, key, group, **kw) if group else None
        if op is not None:
            ops.append(op)
    return ops


def _snapshot(project_id: str, key: str, parts: list[str] | None, **kw) -> dict | None:
    ops = plan_shot_ops(project_id, key, collect_parts(project_id, key, list(parts or PARTS)), **kw)
    for op in ops:
        apply_op(project_id, op)
    if not ops:
        return None
    return {"version_id": ops[0]["version_id"], "version_ids": [o["version_id"] for o in ops],
            "parts": sorted(p for o in ops for p in o["parts"])}


def snapshot_move(project_id: str, key: str, parts: list[str] | None = None, reason: str = "manual", *,
                  surface: str | None = None, regen_id: str | None = None, run_id: str | None = None) -> dict | None:
    """Перенести файлы шота в версию (для Ф11 вместо Path.unlink). None - файлов нет."""
    return _snapshot(project_id, key, parts, mode="move", reason=reason, regen_id=regen_id, run_id=run_id,
                     surface=surface)


def snapshot_copy(project_id: str, key: str, parts: list[str] | None = None, reason: str = "manual", *,
                  surface: str | None = None, regen_id: str | None = None, run_id: str | None = None,
                  restored_from: str | None = None, dedupe: bool = True) -> dict | None:
    return _snapshot(project_id, key, parts, mode="copy", reason=reason, regen_id=regen_id, run_id=run_id,
                     surface=surface, restored_from=restored_from, dedupe=dedupe)


# ------------------------------------------------------------------ чтение версий

def _dirs_for_key(root: Path, key: str) -> list[tuple[str, Path]]:
    """Каталоги, где могут лежать версии шота: свой и каталоги блоков видео, в которые он входит."""
    keys = unit_keys_for(root, key)
    seen = []
    for k in [key] + [x for x in keys if x != key]:
        seen.append((k, shot_versions_dir(root, k)))
    return seen


def iter_versions(project_id: str, key: str) -> list[dict]:
    """Версии шота по возрастанию времени: [{"id","dir","dir_key","meta"}]."""
    root = project_dir(project_id)
    found = []
    for dir_key, parent in _dirs_for_key(root, key):
        for vdir in _version_dirs(parent):
            meta = read_meta(vdir)
            if meta is None:
                continue
            if dir_key != key:
                unit = [k for info in (meta.get("parts") or {}).values() for k in (info.get("unit_keys") or [])]
                if key not in unit:
                    continue
            found.append({"id": vdir.name, "dir": vdir, "dir_key": dir_key, "meta": meta})
    found.sort(key=lambda v: (v["meta"].get("created_at") or "", v["id"]))
    return found


def find_version(project_id: str, key: str, version_id: str) -> dict:
    for version in iter_versions(project_id, key):
        if version["id"] == version_id:
            return version
    raise StudioError("not_found", f"Версия {version_id} шота {key} не найдена")


def _part_view(root: Path, vdir: Path, info: dict) -> dict:
    path = vdir / info["primary"]
    return {"path": _rel(root, path) if path.exists() else (vdir.relative_to(root).as_posix() + "/" + info["primary"]),
            "size": info.get("size"), "exists": path.is_file(), "unit_keys": info.get("unit_keys"),
            "sha256": info.get("sha256")}


def version_view(project_id: str, key: str, version: dict, number: int, selected: str | None,
                 cur_prompts: dict | None = None) -> dict:
    root = project_dir(project_id)
    meta = version["meta"]
    parts = {p: _part_view(root, version["dir"], info) for p, info in (meta.get("parts") or {}).items()}
    job = meta.get("job") or {}
    cost = job.get("cost") if job.get("currency") == "RUB" and isinstance(job.get("cost"), (int, float)) else None
    return {
        "id": version["id"], "number": number, "created_at": meta.get("created_at"), "reason": meta.get("reason"),
        "reason_title": meta.get("reason_title") or REASON_TITLES.get(meta.get("reason"), meta.get("reason")),
        "mode": meta.get("mode"), "surface": meta.get("surface"), "parts": parts,
        "prompts": meta.get("prompts") or {}, "prompts_trusted": bool(meta.get("prompts_trusted")),
        "matches_current": matches_current(project_id, key, meta.get("prompts") or {}, list(parts), cur_prompts),
        "cost_rub": cost, "provider": job.get("provider"), "model": job.get("model"),
        "is_selected": version["id"] == selected, "restored_from": meta.get("restored_from"),
    }


def current_view(project_id: str, key: str) -> dict:
    root = project_dir(project_id)
    shots = _mod("shots")
    files = shots.shot_files(project_id, key)
    parts, tokens = {}, {}
    mates = unit_keys_for(root, key)
    for part in PARTS:
        path = files.get(part)
        if path is None:
            parts[part] = {"path": None, "size": None, "exists": False, "unit_keys": None}
            tokens[part] = None
            continue
        path = Path(path)
        parts[part] = {"path": _rel(root, path), "size": path.stat().st_size, "exists": True,
                       "unit_keys": mates if part == "video" and len(mates) > 1 else None}
        tokens[part] = file_token(path)
    return {"parts": parts, "tokens": tokens, "prompts": current_prompts(project_id, key)}


def usage(project_id: str) -> dict:
    """Расход места версиями по размерам из meta.json; одним проходом по каталогам."""
    root = project_dir(project_id)
    per_shot: dict[str, dict] = {}
    total = 0
    base = root / SHOTS_ROOT / VERSIONS
    for kdir in _version_dirs(base):
        count, size = 0, 0
        for vdir in _version_dirs(kdir):
            meta = read_meta(vdir)
            if meta is None:
                continue
            count += 1
            size += sum(int(i.get("size") or 0) for i in (meta.get("parts") or {}).values())
        if count:
            per_shot[kdir.name] = {"count": count, "bytes": size}
            total += size
    extra = {}
    for kind, area in (("music", AUDIO_ROOT), ("film", FILM_ROOT)):
        count, size = 0, 0
        for vdir in _version_dirs(area_versions_dir(root, area)):
            meta = read_meta(vdir)
            if meta is None:
                continue
            count += 1
            size += sum(int(f.get("size") or 0) for f in meta.get("area_files", []))
        extra[kind] = {"count": count, "bytes": size}
        total += size
    try:
        warn_gb = float(os.environ.get("STORYBOOK_VERSIONS_WARN_GB") or DEFAULT_WARN_GB)
    except ValueError:
        warn_gb = DEFAULT_WARN_GB
    return {"shots": per_shot, "music": extra["music"], "film": extra["film"],
            "usage": {"bytes_total": total, "shots_with_versions": len(per_shot),
                      "warn_bytes": int(warn_gb * 1024 ** 3), "warn": total >= warn_gb * 1024 ** 3}}


def area_versions(project_id: str, kind: str) -> list[dict]:
    root = project_dir(project_id)
    area = AUDIO_ROOT if kind == "music" else FILM_ROOT
    out = []
    for n, vdir in enumerate(_version_dirs(area_versions_dir(root, area)), start=1):
        meta = read_meta(vdir)
        if meta is None:
            continue
        out.append({"id": vdir.name, "number": n, "created_at": meta.get("created_at"), "reason": meta.get("reason"),
                    "reason_title": meta.get("reason_title"), "surface": meta.get("surface"),
                    "files": [{"name": f["name"], "size": f.get("size"),
                               "path": f"{area}/{VERSIONS}/{vdir.name}/{f['name']}"}
                              for f in meta.get("area_files", [])]})
    return out


# ------------------------------------------------------------------ возврат версии

def _free_bytes(root: Path) -> int:
    return shutil.disk_usage(root).free


def check_space(root: Path, need: int) -> None:
    if _free_bytes(root) < need + MIN_FREE_MARGIN:
        raise StudioError("unavailable", "Не хватает места на диске для возврата версии", reason="no_space",
                          need_bytes=need)


def project_dir_of(version: dict) -> Path:
    """Корень проекта по каталогу версии (<корень>/97_shots/_versions/<ключ>/<id>)."""
    return version["dir"].parents[3]


def verify_version(version: dict, parts: list[str]) -> None:
    for part in parts:
        info = (version["meta"].get("parts") or {}).get(part)
        if info:
            safe_origin(project_dir_of(version), info)
        path = version["dir"] / (info or {}).get("primary", "")
        if not info or not path.is_file() or (info.get("sha256") and sha256_file(path) != info["sha256"]):
            raise StudioError("conflict", "Файл версии повреждён или отсутствует", reason="version_corrupt",
                              part=part)


def replace_from_version(project_id: str, version: dict, parts: list[str]) -> None:
    """Скопировать файлы версии на место (рядом .tmp, затем os.replace). Текущего файла нет лишь на миг."""
    root = project_dir(project_id)
    for part in parts:
        info = version["meta"]["parts"][part]
        origin = safe_origin(root, info)
        side = sidecar_of(origin)
        stored = version["dir"] / sidecar_of(Path(info["primary"])).name
        if part != "video":
            side.unlink(missing_ok=True)  # сначала убираем старый sidecar: он подтвердил бы не ту картинку
        _copy_atomic(version["dir"] / info["primary"], origin)
        if part != "video" and stored.is_file():
            _copy_atomic(stored, side)  # новый sidecar кладём последним


def collect_by_origin(project_id: str, version: dict, parts: list[str]) -> dict[str, dict]:
    """Текущие файлы по путям origin из версии (а не по shot_files): именно их заменит возврат."""
    root = project_dir(project_id)
    out: dict[str, dict] = {}
    for part in parts:
        info = version["meta"]["parts"][part]
        path = safe_origin(root, info)
        if not path.is_file():
            continue
        group = [path] + ([sidecar_of(path)] if part != "video" and sidecar_of(path).is_file() else [])
        out[part] = {"primary": path, "files": group, "unit_keys": info.get("unit_keys")}
    return out
