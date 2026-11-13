"""Шоты (кадры): список, карточка, признак «устарел», правка промптов и длительности (Ф6).

Шот = пара элементов shots.json (start + end) с общим ключом "S-N". «Устарел» считаем по
собственному отпечатку входа (<проект>/.studio/shot_inputs.json), см. _shots_fp.py.
Запись shots.json - только через merge_write_shots_json (слияние по полям под flock).
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import _shots_fp as fp
from .artifacts import obj_digest
from .capabilities import capability
from .errors import StudioError, success
from .paths import project_dir, resolve_in_project
from .reports import make_shot_key, parse_shot_key, parse_shot_keys, shot_sort_key

logger = logging.getLogger(__name__)

parse_timing_seconds = fp.parse_timing_seconds

SHOTS_REL = "97_shots/shots.json"
JOBS_REL = "97_shots/provider_jobs.json"
PLAN_REL = "97_shots/video_plan.json"
CAPS_REL = "97_shots/video_model_caps.json"
REVIEW_REL = "97_shots/review_state.json"

STATES = ("ready", "stale", "error", "generating", "missing", "partial")
STATE_TITLES = {
    "ready": "Готов", "stale": "Устарел", "error": "Ошибка", "generating": "Генерируется",
    "missing": "Нет материалов", "partial": "Не хватает материалов",
}
FRESHNESS_TITLES = {"fresh": "Актуален", "stale": "Устарел", "unknown": "Неизвестно"}
REVIEW_TITLES = {"pending": "Ожидает", "approved": "Одобрен", "redo": "Переделать", "rejected": "Отклонён"}
JOB_TITLES = {
    "downloaded": "Скачано", "completed": "Готово", "failed": "Ошибка", "download_failed": "Ошибка скачивания",
    "stale": "Устарело", "submitting": "Отправляется", "submitted": "Отправлено", "queued": "В очереди",
    "running": "Идёт", "polling": "Идёт", "pending": "Ожидает",
}
_JOB_ACTIVE = {"submitting", "submitted", "queued", "running", "polling", "pending"}
_JOB_FAILED = {"failed", "download_failed"}
_STATE_RANK = {"error": 0, "stale": 1, "partial": 2, "missing": 3, "generating": 4, "ready": 5}
_SORTS = ("scene_shot", "state", "duration", "cost")

EDIT_FIELDS = ("start_prompt", "end_prompt", "video_prompt", "start_negative_prompt",
               "end_negative_prompt", "duration_s")
_TEXT_FIELD = {  # имя в changes -> (элемент, поле shots.json)
    "start_prompt": ("start", "english_prompt"), "end_prompt": ("end", "english_prompt"),
    "video_prompt": ("start", "video_prompt"), "start_negative_prompt": ("start", "negative_prompt"),
    "end_negative_prompt": ("end", "negative_prompt"),
}
_DIGEST_FIELDS = ("english_prompt", "negative_prompt", "video_prompt", "duration_s", "duration_source", "timing")
MAX_TEXT = 20000
MAX_DURATION = 120
STORYBOARD_HINT = "Перезапуск шага «Раскадровка» перезапишет ручные правки промптов."
EDIT_NOTE = "Картинка не обновится сама: её нужно перегенерировать."

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
_VID_EXT = (".mp4", ".webm", ".mov")
_PREFIX = {"start": ("img_final_start_", _IMG_EXT), "end": ("img_final_end_", _IMG_EXT),
           "video": ("video_final_", _VID_EXT)}
_REASONS = {
    "prompt": "prompt_changed", "negative": "negative_changed", "refs": "references_changed",
    "roles": "roles_changed", "params": "params_changed", "duration": "duration_changed",
    "start_img": "start_image_changed", "end_img": "end_image_changed",
}
_DOWNSTREAM = {"start": ("end", "video"), "end": ("video",), "video": ()}
_IMAGE_STEP = "artist_batch_shots"
_JOB_FRESH_SEC = 2 * 3600
_VIDEO_NEWER_GAP_NS = 2 * 10**9
_RE_DIR = re.compile(r"scene_\d+_shot_\d+")


# --- Чистые функции --------------------------------------------------------------------


def shot_dir_name(scene: int, shot: int) -> str:
    return f"scene_{int(scene):02d}_shot_{int(shot):02d}"


def _canonical_name(part: str, scene: int, shot: int) -> str:
    s, m = f"{int(scene):02d}", f"{int(shot):02d}"
    return {"start": f"img_final_start_{s}_{m}.png", "end": f"img_final_end_{s}_{m}.png",
            "video": f"video_final_{s}_{m}.mp4"}[part]


def _normalize_rel(raw: Any, scene: int, shot: int, part: str) -> str:
    if isinstance(raw, str) and raw.strip() and "\0" not in raw:
        parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in ("/", "")]
        tail = parts[-2:]
        if len(tail) == 2 and _RE_DIR.fullmatch(tail[0]) and tail[1] not in ("..", ".") and not tail[1].startswith("."):
            return f"97_shots/{tail[0]}/{tail[1]}"
    return f"97_shots/{shot_dir_name(scene, shot)}/{_canonical_name(part, scene, shot)}"


def normalize_media_path(project_id: str, raw: Any, shot_key: str, part: str) -> str:
    """Путь картинки/видео из элемента -> относительный путь проекта (POSIX)."""
    key = _require_key(shot_key)
    if part not in fp.PARTS:
        raise StudioError("invalid_argument", "Часть шота: start, end или video")
    scene, shot = (int(x) for x in key.split("-"))
    rel = _normalize_rel(raw, scene, shot, part)
    resolve_in_project(project_id, rel, must_exist=False)
    return rel


def _require_key(value: Any) -> str:
    key = parse_shot_key(value)
    if not key:
        raise StudioError("invalid_argument", "Не удалось разобрать ключ шота (ожидается вид 2-3)")
    return key


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _digest_of(start: dict | None, end: dict | None) -> str:
    def part(item: dict | None):
        return None if item is None else {f: item.get(f) for f in _DIGEST_FIELDS}

    return obj_digest({"start": part(start), "end": part(end)})


def _current_values(start: dict | None, end: dict | None) -> dict:
    start, end = start or {}, end or {}
    return {"start_prompt": start.get("english_prompt"), "end_prompt": end.get("english_prompt"),
            "video_prompt": start.get("video_prompt"), "start_negative_prompt": start.get("negative_prompt"),
            "end_negative_prompt": end.get("negative_prompt"), "duration_s": start.get("duration_s")}


# --- Кэш разобранных файлов ------------------------------------------------------------

_CACHE: "OrderedDict[str, tuple[tuple, Any]]" = OrderedDict()
_CACHE_GUARD = threading.Lock()
_CACHE_SIZE = 32
STATS = {"shots_parse": 0}


def _cached(path: Path, loader):
    """Разбор файла по (mtime_ns, size); нет файла -> None."""
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    name = str(path)
    with _CACHE_GUARD:
        hit = _CACHE.get(name)
        if hit and hit[0] == sig:
            _CACHE.move_to_end(name)
            return hit[1]
    value = loader(path)
    with _CACHE_GUARD:
        _CACHE[name] = (sig, value)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return value


def _invalidate(root: Path) -> None:
    with _CACHE_GUARD:
        for name in [n for n in _CACHE if n.startswith(str(root))]:
            _CACHE.pop(name, None)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- shots.json -> пары ----------------------------------------------------------------


class _Pair:
    __slots__ = ("key", "scene", "shot", "start", "end", "digest", "warnings", "search")

    def __init__(self, key: str, scene: int, shot: int):
        self.key, self.scene, self.shot = key, scene, shot
        self.start: dict | None = None
        self.end: dict | None = None
        self.digest = ""
        self.warnings: list[str] = []
        self.search = ""


class _Doc:
    def __init__(self):
        self.pairs: dict[str, _Pair] = {}
        self.order: list[str] = []


def _light(item: dict) -> dict:
    refs = fp.parse_ref_list(item.get("reference_image_paths"))
    return {
        "prompt": _text(item.get("english_prompt")), "negative": _text(item.get("negative_prompt")),
        "video_prompt": _text(item.get("video_prompt")), "timing": item.get("timing"),
        "duration_s": item.get("duration_s"), "duration_source": item.get("duration_source"),
        "output_path": item.get("output_path"), "video_path": item.get("video_path"),
        "refs": refs, "bg": fp.parse_bg_sheet(item.get("background_reference")),
        "roles": _text(item.get("reference_image_roles")) or _text(item.get("reference_roles_instruction")),
        "width": item.get("width"), "height": item.get("height"), "seed": item.get("seed"),
        "cfg": item.get("true_cfg_scale"), "steps": item.get("num_inference_steps"),
        "shot_type": _text(item.get("shot_type")),
        "prev_end": _truthy(item.get("should_use_prev_end_as_reference")),
    }


def _group_items(items: list) -> dict[str, dict]:
    """Ключ шота -> {"start": raw, "end": raw, "warnings": [...]}; повторы - берём первый."""
    groups: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        phase = _text(item.get("shot_type")).strip().lower()
        key = make_shot_key(item.get("scene_number"), item.get("shot_number"))
        if phase not in ("start", "end") or not key:
            continue
        group = groups.setdefault(key, {"start": None, "end": None, "warnings": []})
        if group[phase] is None:
            group[phase] = item
        elif "duplicate_item" not in group["warnings"]:
            group["warnings"].append("duplicate_item")
    return groups


def _load_doc(path: Path) -> _Doc:
    STATS["shots_parse"] += 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudioError("unavailable", "Файл шотов не читается как JSON; откройте его в редакторе файлов") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise StudioError("unavailable", "В файле шотов нет списка items; откройте его в редакторе файлов")
    doc = _Doc()
    for key, group in _group_items(data["items"]).items():
        scene, shot = (int(x) for x in key.split("-"))
        pair = _Pair(key, scene, shot)
        pair.start = _light(group["start"]) if group["start"] else None
        pair.end = _light(group["end"]) if group["end"] else None
        pair.digest = _digest_of(group["start"], group["end"])
        pair.warnings = list(group["warnings"])
        if group["start"] is None:
            pair.warnings.append("start_item_missing")
        pair.search = " ".join(
            [key] + [(p or {}).get(f, "") for p in (pair.start, pair.end) for f in ("prompt", "video_prompt")]
        ).casefold()
        doc.pairs[key] = pair
    doc.order = sorted(doc.pairs, key=shot_sort_key)
    return doc


# --- Задачи провайдера, блоки видео, ревью ---------------------------------------------


def _clean_error(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"https?://\S+", "[ссылка]", " ".join(value.split()))
    return text[:200]


_SHA_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_SHA_CACHE_SIZE = 2048
_SHA_GUARD = threading.Lock()
_SHA_STATS = {"reads": 0}


def _sha256(path: Path) -> str:
    """sha256 файла; кэш по (путь, размер, mtime_ns), чтобы не читать картинку заново."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cache_key = (str(path), st.st_size, st.st_mtime_ns)
    with _SHA_GUARD:
        hit = _SHA_CACHE.get(cache_key)
        if hit is not None:
            _SHA_CACHE.move_to_end(cache_key)
            return hit
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    _SHA_STATS["reads"] += 1
    value = digest.hexdigest()
    with _SHA_GUARD:
        _SHA_CACHE[cache_key] = value
        while len(_SHA_CACHE) > _SHA_CACHE_SIZE:
            _SHA_CACHE.popitem(last=False)
    return value


def _image_hashes(value: Any) -> dict[str, str]:
    """Хеши картинок задачи по ролям: {"start": sha, "end": sha}."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for part in ("start", "end"):
        v = value.get(f"{part}_image")
        if isinstance(v, str) and v.strip():
            out[part] = v.strip().lower()
    return out


def _load_jobs(path: Path) -> dict[str, list[dict]]:
    data = _read_json(path)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out: dict[str, list[dict]] = {}
    if not isinstance(jobs, list):
        return out
    for order, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        key = parse_shot_key(job.get("shot_key"))
        if not key:
            continue
        stamps = job.get("timestamps") if isinstance(job.get("timestamps"), dict) else {}
        stamp = _text(stamps.get("updated_at")) or _text(stamps.get("created_at")) or _text(job.get("updated_at"))
        out.setdefault(key, []).append({
            "status": _text(job.get("status")), "provider": job.get("provider"), "model": job.get("model"),
            "cost": _num(job.get("cost")), "currency": job.get("currency"),
            "error": _clean_error(job.get("error")), "at": stamp, "_order": order,
            "image_hashes": _image_hashes(job.get("source_image_hashes")),
        })
    for items in out.values():
        items.sort(key=lambda j: (j["at"], j["_order"]))  # последний = актуальная попытка
    return out


def _load_units(path: Path) -> dict[str, dict]:
    data = _read_json(path)
    units = data.get("units") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or not isinstance(unit.get("video_path"), str):
            continue
        name = PurePosixPath(unit["video_path"].replace("\\", "/")).name
        keys = [k for k in (parse_shot_key(v) for v in unit.get("shot_keys") or []) if k]
        if not name or name.startswith("."):
            continue
        for key in keys:
            out.setdefault(key, {"keys": keys, "name": name})
    return out


def _review_status(value: Any) -> str:
    return value if value in REVIEW_TITLES else "pending"


def _load_review_file(path: Path) -> dict[str, dict] | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError("review_state")
    body = data.get("shots") if isinstance(data.get("shots"), dict) else data
    out: dict[str, dict] = {}
    for raw_key, entry in body.items():
        key = parse_shot_key(raw_key)
        if not key:
            continue
        if isinstance(entry, str):
            entry = {"status": entry}
        if isinstance(entry, dict):
            out[key] = {"status": entry.get("status"), "note": entry.get("note")}
    return out


def _job_brief(jobs: list[dict]) -> dict | None:
    if not jobs:
        return None
    last = jobs[-1]
    rub = [j["cost"] for j in jobs if j["cost"] is not None and j["currency"] == "RUB"]
    return {
        "status": last["status"], "status_title": JOB_TITLES.get(last["status"], last["status"] or "Неизвестно"),
        "provider": last["provider"], "model": last["model"], "attempts": len(jobs),
        "cost": last["cost"], "currency": last["currency"], "spent_rub": round(sum(rub), 4),
    }


# --- Контекст одного вызова ------------------------------------------------------------


def _art():
    return importlib.import_module(f"{__package__}.artifacts")


def _optional(module: str, name: str):
    """Функция из соседнего модуля другой задачи или None, если её ещё нет."""
    full = f"{__package__}.{module}"
    try:
        mod = importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None
        raise
    func = getattr(mod, name, None)
    return func if callable(func) else None


class _Ctx:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.root = project_dir(project_id)
        self.root_real = self.root.resolve()
        loaded = _cached(self.root / SHOTS_REL, _load_doc)
        self.doc: _Doc | None = loaded
        self.records = fp.load_records(project_id)
        self.jobs = _cached(self.root / JOBS_REL, _load_jobs) or {}
        self.units = _cached(self.root / PLAN_REL, _load_units) or {}
        self.hook_errors: list[str] = []
        self._cores: dict[str, dict] = {}
        self._ref_cache: dict[str, tuple[str | None, str | None]] = {}
        self._gen: bool | None = None
        self._active: bool | None = None
        self._review: dict | None | bool = False
        self._findings: dict | None | bool = False
        self._blockout = (self.root / "93_blockout").is_dir()

    # -- файлы
    def inside(self, path: Path) -> bool:
        try:
            Path(os.path.realpath(path)).relative_to(self.root_real)
            return True
        except ValueError:
            return False

    def token_of_rel(self, rel: str) -> str | None:
        if not rel or rel.startswith("/") or ".." in rel.split("/") or "\0" in rel:
            return None
        path = self.root / rel
        return fp.file_token(path) if self.inside(path) else None

    def resolve_ref(self, rel: str | None) -> tuple[str | None, str | None]:
        """Референс шота -> (путь для показа, токен файла). Файл мог переехать в shared/references:
        тогда его находим по каталогу, а старый путь остаётся лишь алиасом."""
        if not rel or rel.startswith("/") or ".." in rel.split("/") or "\0" in rel:
            return rel, None
        if rel in self._ref_cache:
            return self._ref_cache[rel]
        series = self.root.parent.name == "series"
        project_root = self.root.parent.parent if series else self.root
        candidates = [self.root / rel] + ([project_root / rel] if series else [])
        sid = self.root.name
        if series and rel.startswith("20_bible/references/"):  # единственный алиас записи — под _series/<sid>
            tail = rel[len("20_bible/references/"):]
            candidates.append(project_root / "20_bible/references/_series" / sid / tail)
        found: Path | None = None
        try:
            from custom_tools.storybook.reference_catalog import resolve_reference
        except ImportError:  # pragma: no cover
            resolve_reference = None
        for candidate in candidates:
            if candidate.is_file() and candidate.is_relative_to(self.root):
                found = candidate  # внутри серии: как есть
                break
            if resolve_reference is None:
                continue
            try:  # корень проекта и алиасы: только через каталог, файл «на месте» не доказательство
                resolved = resolve_reference(self.root, candidate)
            except (ValueError, OSError):  # запись не привязана к серии: файла для неё нет
                continue
            if resolved.is_file():
                found = resolved
                break
        display, token = rel, None
        if found is not None:
            real = Path(os.path.realpath(found))
            shared = Path(os.path.realpath(project_root / "shared/references"))
            if real.is_relative_to(shared):
                display = "shared/references/" + real.relative_to(shared).as_posix()
                token = fp.file_token(real)
            elif self.inside(found) or real.is_relative_to(Path(os.path.realpath(project_root))):
                token = fp.file_token(found)
        self._ref_cache[rel] = (display, token)
        return display, token

    def scan_files(self, pair: _Pair) -> dict[str, dict | None]:
        found: dict[str, dict | None] = {"start": None, "end": None, "video": None}
        folder = self.root / "97_shots" / shot_dir_name(pair.scene, pair.shot)
        if not self.inside(folder):
            return found
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return found
        for entry in entries:
            for part, (prefix, exts) in _PREFIX.items():
                if not entry.name.startswith(prefix) or not entry.name.lower().endswith(exts):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if not entry.is_file() or st.st_size <= 0 or (entry.is_symlink() and not self.inside(Path(entry.path))):
                    continue
                best = found[part]
                if best is None or st.st_mtime_ns > best["mtime_ns"]:
                    found[part] = {"rel": f"97_shots/{folder.name}/{entry.name}", "abs": Path(entry.path),
                                   "mtime_ns": st.st_mtime_ns, "size": st.st_size}
        for info in found.values():
            if info:
                info["token"] = fp.file_token(info["abs"])
        return found

    # -- хуки
    def images_generating(self) -> bool:
        if self._gen is None:
            self._gen = False
            func = _optional("runs", "run_progress")
            if func:
                try:
                    info = func(self.project_id)
                except Exception as exc:
                    self.hook_errors.append(f"runs.run_progress: {type(exc).__name__}")
                    info = None
                if isinstance(info, dict):
                    steps: list = []
                    for field in ("step_id", "step", "current_step", "steps", "current_steps"):
                        value = info.get(field)
                        steps += value if isinstance(value, (list, tuple)) else [value]
                    self._gen = _IMAGE_STEP in steps
        return self._gen

    def review_map(self) -> dict | None:
        if self._review is False:
            self._review = None
            hook = _optional("review", "shot_review_map")
            result = None
            if hook:
                try:
                    result = hook(self.project_id)
                except Exception as exc:
                    self.hook_errors.append(f"review.shot_review_map: {type(exc).__name__}")
            if isinstance(result, dict):
                self._review = {parse_shot_key(k) or k: v for k, v in result.items() if isinstance(v, dict)}
            elif hook is None or result is None:
                path = self.root / REVIEW_REL
                if path.exists():
                    try:
                        self._review = _cached(path, _load_review_file)
                    except ValueError:
                        self.hook_errors.append("review_state.json: битый файл")
        return self._review

    def findings_map(self) -> dict | None:
        if self._findings is False:
            self._findings = None
            try:
                counts = _art_reports().shot_counts(self.project_id)
                if isinstance(counts, dict):
                    self._findings = counts
            except Exception as exc:
                logger.warning("reports.shot_counts failed: %s", exc)
                self.hook_errors.append(f"reports.shot_counts: {type(exc).__name__}")
        return self._findings

    # -- ядро шота
    def core(self, key: str) -> dict:
        if key not in self._cores:
            self._cores[key] = self._build_core(self.doc.pairs[key])
        return self._cores[key]

    def _ref_entries(self, item: dict, skip_prefix: str | None) -> list:
        entries = []
        for raw in item["refs"]:
            rel = fp.ref_rel(raw)
            if rel and not (skip_prefix and rel.startswith(skip_prefix)):
                # ключ по пути из шота: перенос файла не меняет его, меняется только токен содержимого
                entries.append([fp.ref_key(rel), self.resolve_ref(rel)[1]])
        bg = fp.ref_rel(item["bg"]) if item["bg"] else None
        if bg:
            entries.append(["bg:" + fp.ref_key(bg), self.resolve_ref(bg)[1]])
        return entries

    def _image_components(self, item: dict, skip_prefix: str | None) -> dict:
        return {
            "prompt": fp.h16(item["prompt"]), "negative": fp.h16(item["negative"]),
            "refs": fp.h16(self._ref_entries(item, skip_prefix)), "roles": fp.h16(item["roles"]),
            "params": fp.h16([item["width"], item["height"], item["seed"], item["cfg"], item["steps"]]),
        }

    def _freshness(self, key: str, part: str, comps: dict, token: str | None) -> tuple[str | None, list[str]]:
        if token is None:
            return None, []
        record = (self.records.get(key) or {}).get(part)
        if not isinstance(record, dict) or not isinstance(record.get("components"), dict):
            return "unknown", ["no_baseline"]
        if record.get("file") != token:
            return "unknown", ["file_replaced"]
        if record.get("fp") == fp.fingerprint(comps) and record["components"] == comps:
            return "fresh", []
        reasons = [_REASONS[name] for name, value in comps.items() if record["components"].get(name) != value]
        return ("stale", reasons) if reasons else ("fresh", [])

    def run_active(self) -> bool:
        if self._active is None:
            try:
                self._active = bool(_art()._run_info(self.project_id).get("active"))
            except Exception:
                self._active = False
        return self._active

    def job_alive(self, job: dict) -> bool:
        """Незавершённая задача считается идущей, пока активен прогон или запись свежая."""
        if self.run_active():
            return True
        try:
            stamp = datetime.fromisoformat(job["at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - stamp).total_seconds() < _JOB_FRESH_SEC
        except (ValueError, AttributeError):
            return False

    def _source_image_newer(self, key: str, files: dict, tokens: dict, jobs: list[dict]) -> bool:
        """Видео старше своих картинок. Решает содержимое, mtime - только запасной вариант."""
        record = (self.records.get(key) or {}).get("video")
        comps = record.get("components") if isinstance(record, dict) else None
        if isinstance(comps, dict) and record.get("file") == tokens["video"] and comps.get("start_img") is not None:
            return False  # отпечаток записан и картинки те же: время файлов не важно
        images = [files[p] for p in ("start", "end") if files[p]]
        done = [j for j in jobs if j["status"] == "downloaded" and j["image_hashes"]]
        if done:  # хеши только из последней успешно скачанной задачи, по ролям
            hashes = done[-1]["image_hashes"]
            return any(_sha256(files[p]["abs"]) != h for p, h in hashes.items() if files[p])
        newest = max([i["mtime_ns"] for i in images] or [0])
        return newest - files["video"]["mtime_ns"] > _VIDEO_NEWER_GAP_NS

    def _build_core(self, pair: _Pair) -> dict:
        key = pair.key
        files = self.scan_files(pair)
        jobs = self.jobs.get(key) or []
        latest = jobs[-1]["status"] if jobs else None
        item_s, item_e = pair.start, pair.end
        shot_dir = f"97_shots/{shot_dir_name(pair.scene, pair.shot)}/"
        tokens = {p: (files[p]["token"] if files[p] else None) for p in fp.PARTS}

        unit = None
        if files["video"] is None and key in self.units:
            unit_rel = f"97_shots/video_units/{self.units[key]['name']}"
            upath = self.root / unit_rel
            if self.inside(upath) and upath.is_file():
                st = upath.stat()
                if st.st_size > 0:
                    unit = {"keys": self.units[key]["keys"], "path": unit_rel}
                    files["video"] = {"rel": unit_rel, "abs": upath, "mtime_ns": st.st_mtime_ns,
                                      "size": st.st_size, "token": fp.file_token(upath)}
                    tokens["video"] = files["video"]["token"]

        # компоненты отпечатка
        comps: dict[str, dict] = {}
        comps["start"] = self._image_components(item_s, None) if item_s else {}
        if item_e:
            comps["end"] = self._image_components(item_e, shot_dir + "img_final_start_")
            comps["end"]["start_img"] = fp.h16(tokens["start"])
        dur_item = item_s or item_e or {}
        seconds = _num(dur_item.get("duration_s"))
        if seconds is None:
            seconds = fp.parse_timing_seconds(dur_item.get("timing"))
        comps["video"] = {"prompt": fp.h16((item_s or {}).get("video_prompt", "")), "duration": fp.h16(seconds),
                          "start_img": fp.h16(tokens["start"]), "end_img": fp.h16(tokens["end"])}

        gen_images = self.images_generating()
        parts: dict[str, dict] = {}
        reasons: dict[str, list[str]] = {}
        for part in ("start", "end"):
            info = files[part]
            if info:
                state = "ready"
            elif part == "end" and item_e is None:
                state = "not_applicable"
            elif gen_images:
                state = "generating"
            else:
                state = "missing"
            item = item_s if part == "start" else item_e
            planned = _normalize_rel((item or {}).get("output_path"), pair.scene, pair.shot, part)
            fresh, why = self._freshness(key, part, comps.get(part, {}), tokens[part]) if state == "ready" else (None, [])
            parts[part] = {"state": state, "path": info["rel"] if info else (None if state == "not_applicable" else planned),
                           "exists": info is not None, "freshness": fresh}
            if state == "generating":
                parts[part]["approximate"] = True
            reasons[part] = why

        vinfo = files["video"]
        if latest in _JOB_ACTIVE and self.job_alive(jobs[-1]):
            vstate = "generating"
        elif latest in _JOB_FAILED:
            vstate = "error"
        elif vinfo:
            vstate = "ready"
        elif jobs:
            vstate = "missing"
        else:
            vstate = "not_started"
        vfresh, vwhy = (None, [])
        if vinfo:
            vfresh, vwhy = self._freshness(key, "video", comps["video"], tokens["video"])
            vwhy = list(vwhy)
            if latest == "stale":
                vfresh, vwhy = "stale", vwhy + ["provider_job_stale"]
            if self._source_image_newer(key, files, tokens, jobs):
                vfresh, vwhy = "stale", vwhy + ["source_image_newer"]
            if vfresh == "stale":
                vwhy = [r for r in vwhy if r not in ("no_baseline", "file_replaced")]
        planned_v = _normalize_rel((item_s or {}).get("video_path"), pair.scene, pair.shot, "video")
        parts["video"] = {"state": vstate, "path": vinfo["rel"] if vinfo else (planned_v if vstate != "not_started" else None),
                          "exists": vinfo is not None, "freshness": vfresh, "unit": unit}
        reasons["video"] = vwhy

        stale_parts = [p for p in fp.PARTS if parts[p]["freshness"] == "stale"]
        fresh_values = [parts[p]["freshness"] for p in fp.PARTS if parts[p]["freshness"]]
        if "stale" in fresh_values:
            freshness = "stale"
        elif "unknown" in fresh_values:
            freshness = "unknown"
        else:
            freshness = "fresh" if fresh_values else None

        any_image = parts["start"]["exists"] or parts["end"]["exists"]
        if vstate == "error":
            state = "error"
        elif "generating" in (parts["start"]["state"], parts["end"]["state"], vstate):
            state = "generating"
        elif not any_image and not parts["video"]["exists"]:
            state = "missing"
        elif (not parts["start"]["exists"] or (item_e is not None and not parts["end"]["exists"])
              or vstate == "missing"):
            state = "partial"
        elif stale_parts:
            state = "stale"
        else:
            state = "ready"
        return {"files": files, "tokens": tokens, "comps": comps, "parts": parts, "reasons": reasons,
                "stale_parts": stale_parts, "freshness": freshness, "state": state, "jobs": jobs}


def _art_reports():
    return importlib.import_module(f"{__package__}.reports")


# --- Строка списка и карточка ----------------------------------------------------------


def _duration_of(pair: _Pair) -> tuple[Any, str | None]:
    item = pair.start or pair.end or {}
    value = _num(item.get("duration_s"))
    if value is not None and value > 0:
        return (int(value) if float(value).is_integer() else value), (item.get("duration_source") or None)
    parsed = fp.parse_timing_seconds(item.get("timing"))
    return parsed, ("parsed" if parsed is not None else None)


def _row(ctx: _Ctx, key: str) -> dict:
    pair = ctx.doc.pairs[key]
    core = ctx.core(key)
    files = core["files"]
    thumb = files["start"] or files["end"]
    duration, source = _duration_of(pair)
    reviews = ctx.review_map()
    entry = reviews.get(key) if reviews else None
    review = None
    if entry is not None:
        status = _review_status(entry.get("status"))
        review = {"status": status, "status_title": REVIEW_TITLES[status]}
    counts = ctx.findings_map()
    findings = None
    if counts is not None:
        raw = counts.get(key) or {}
        findings = {lvl: int(raw.get(lvl) or 0) for lvl in ("error", "warning", "info")}
    scene_dir = ctx.root / "93_blockout" / shot_dir_name(pair.scene, pair.shot)
    blockout = {name: bool(ctx._blockout and (scene_dir / fname).is_file())
                for name, fname in (("start", "ref_start.png"), ("end", "ref_end.png"), ("video", "blockout_ref.mp4"))}
    first = pair.start or pair.end or {}
    return {
        "shot_key": key, "scene": pair.scene, "shot": pair.shot, "index": ctx.doc.order.index(key) + 1,
        "duration_s": duration, "duration_source": source,
        "prompt_preview": (first.get("prompt") or "")[:160],
        "thumb_path": thumb["rel"] if thumb else None, "thumb_rev": thumb["token"] if thumb else None,
        "parts": core["parts"], "state": core["state"], "state_title": STATE_TITLES[core["state"]],
        "stale": bool(core["stale_parts"]), "stale_parts": core["stale_parts"], "freshness": core["freshness"],
        "review": review, "findings": findings, "job": _job_brief(core["jobs"]), "blockout": blockout,
    }


def _cascade(stale_parts: list[str], parts: dict) -> dict:
    after: list[str] = []
    for part in stale_parts:
        for down in _DOWNSTREAM[part]:
            if down not in stale_parts and down not in after and parts[down]["exists"]:
                after.append(down)
    return {"stale_now": list(stale_parts), "stale_after_regeneration": [p for p in fp.PARTS if p in after]}


def _lock_state(project_id: str) -> tuple[bool, str | None, list[dict]]:
    """(заблокировано, причина, предупреждения): правка теряется, если идёт шаг «Раскадровка»."""
    try:
        info = _art()._run_info(project_id)
    except Exception:
        logger.exception("shots: run info failed")
        return False, None, []
    if not info.get("active"):
        return False, None, []
    steps = info.get("current_steps") or []
    if "screenplay_shots_generator" in steps:
        return True, "Сейчас идёт шаг «Раскадровка»: она пересоздаёт шоты, правка будет потеряна.", []
    return False, None, [{"code": "run_active", "message": "Идёт прогон, правка наложена по полям."}]


def _ref_view(ctx: _Ctx, item: dict | None) -> list[dict]:
    out = []
    for raw in (item or {}).get("refs", []):
        rel = fp.ref_rel(raw)
        display, token = ctx.resolve_ref(rel)
        out.append({"path": display, "exists": bool(token)})
    return out


def _card(ctx: _Ctx, key: str, include_lock: bool = True) -> dict:
    pair = ctx.doc.pairs[key]
    core = ctx.core(key)
    row = _row(ctx, key)
    card = dict(row)

    def image(item: dict | None) -> dict | None:
        if item is None:
            return None
        return {"prompt": item["prompt"], "negative_prompt": item["negative"], "shot_type": item["shot_type"],
                "output_path": _normalize_rel(item["output_path"], pair.scene, pair.shot,
                                              "start" if item["shot_type"].lower() != "end" else "end"),
                "width": item["width"], "height": item["height"], "seed": item["seed"],
                "true_cfg_scale": item["cfg"], "num_inference_steps": item["steps"],
                "references": _ref_view(ctx, item)}

    vinfo = core["files"]["video"]
    duration, source = _duration_of(pair)
    first = pair.start or pair.end or {}
    card["start"], card["end"] = image(pair.start), image(pair.end)
    card["video"] = {"prompt": (pair.start or {}).get("video_prompt", ""), "duration_s": duration,
                     "duration_source": source, "timing": first.get("timing"), "unit": core["parts"]["video"]["unit"],
                     "size": vinfo["size"] if vinfo else None, "mtime_ns": vinfo["mtime_ns"] if vinfo else None}
    card["digest"] = pair.digest
    fields = [f for f in EDIT_FIELDS if pair.end is not None or not f.startswith("end_")]
    locked, reason, _ = _lock_state(ctx.project_id) if include_lock else (False, None, [])
    if pair.start is None:
        locked, reason, fields = True, "В файле нет элемента start этого шота: правка запрещена.", []
    card["editable"] = {"fields": fields, "locked": locked, "locked_reason": reason, "hint": STORYBOARD_HINT}
    job = _job_brief(core["jobs"])
    if job:
        job["unknown_currency_attempts"] = sum(1 for j in core["jobs"] if j["cost"] is not None and j["currency"] is None)
        job["recent"] = [{"status": j["status"], "status_title": JOB_TITLES.get(j["status"], j["status"]),
                          "at": j["at"], "cost": j["cost"], "currency": j["currency"], "error": j["error"]}
                         for j in reversed(core["jobs"][-5:])]
    card["job"] = job
    card["stale_reasons"] = {p: r for p, r in core["reasons"].items() if p in core["stale_parts"]}
    card["unknown_reasons"] = {p: r for p, r in core["reasons"].items()
                               if core["parts"][p]["freshness"] == "unknown"}
    card["cascade"] = _cascade(core["stale_parts"], core["parts"])
    card["upstream_stale_keys"] = _upstream_stale(ctx, pair)
    card["edits"] = fp.read_edits(ctx.project_id, key)
    card["warnings"] = list(pair.warnings)
    reviews = ctx.review_map()
    if card["review"] and reviews:
        card["review"]["note"] = (reviews.get(key) or {}).get("note")
    hook = _optional("blockout", "shot_state")
    card["blockout_state"] = None
    if hook:
        try:
            card["blockout_state"] = hook(ctx.project_id, key)
        except Exception as exc:
            ctx.hook_errors.append(f"blockout.shot_state: {type(exc).__name__}")
    return card


def _upstream_stale(ctx: _Ctx, pair: _Pair) -> list[str]:
    found: list[str] = []
    for item in (pair.start, pair.end):
        for raw in (item or {})["refs"] if item else []:
            rel = fp.ref_rel(raw) or ""
            match = re.match(r"97_shots/(scene_\d+_shot_\d+)/img_final_", rel)
            other = parse_shot_key(match.group(1)) if match else None
            if other and other != pair.key and other in ctx.doc.pairs and other not in found:
                if ctx.core(other)["stale_parts"]:
                    found.append(other)
    return sorted(found, key=shot_sort_key)


# --- Фильтры списка --------------------------------------------------------------------


def _check_filters(scene, state, review, freshness, stale_only, with_findings, query, sort) -> dict:
    if scene is not None and (isinstance(scene, bool) or not isinstance(scene, int) or scene < 0):
        raise StudioError("invalid_argument", "Сцена: неотрицательное целое")
    for name, value, allowed in (("state", state, STATES), ("review", review, tuple(REVIEW_TITLES)),
                                 ("freshness", freshness, tuple(FRESHNESS_TITLES)), ("sort", sort, _SORTS)):
        if not isinstance(value, str) or (value and value not in allowed):
            raise StudioError("invalid_argument", f"Неизвестное значение «{name}»: допустимы {', '.join(allowed)}")
    if not isinstance(query, str) or len(query) > 200:
        raise StudioError("invalid_argument", "Запрос: строка до 200 символов")
    for name, value in (("stale_only", stale_only), ("with_findings", with_findings)):
        if not isinstance(value, bool):
            raise StudioError("invalid_argument", f"«{name}»: true или false")
    return {"scene": scene, "state": state, "review": review, "freshness": freshness, "stale_only": stale_only,
            "with_findings": with_findings, "query": query.strip().casefold(), "sort": sort or "scene_shot"}


def _matches(row: dict, pair: _Pair, f: dict) -> bool:
    if f["scene"] is not None and row["scene"] != f["scene"]:
        return False
    if f["state"] and row["state"] != f["state"]:
        return False
    if f["review"] and (row["review"] or {}).get("status") != f["review"]:
        return False
    if f["freshness"] and row["freshness"] != f["freshness"]:
        return False
    if f["stale_only"] and not row["stale"]:
        return False
    if f["with_findings"] and not (row["findings"] and row["findings"]["error"] + row["findings"]["warning"] > 0):
        return False
    return not f["query"] or f["query"] in pair.search


def _sorted_rows(rows: list[dict], sort: str) -> list[dict]:
    base = sorted(rows, key=lambda r: shot_sort_key(r["shot_key"]))
    if sort == "state":
        return sorted(base, key=lambda r: _STATE_RANK[r["state"]])
    if sort == "duration":
        return sorted(base, key=lambda r: -(r["duration_s"] or 0))
    if sort == "cost":
        return sorted(base, key=lambda r: -((r["job"] or {}).get("spent_rub") or 0))
    return base


def _facets(rows: list[dict]) -> dict:
    scenes: dict[int, int] = {}
    state = {s: 0 for s in STATES}
    review = {**{s: 0 for s in REVIEW_TITLES}, "none": 0}
    fresh = {s: 0 for s in FRESHNESS_TITLES}
    with_findings = stale = 0
    for row in rows:
        scenes[row["scene"]] = scenes.get(row["scene"], 0) + 1
        state[row["state"]] += 1
        review[(row["review"] or {}).get("status", "none")] += 1
        if row["freshness"]:
            fresh[row["freshness"]] += 1
        if row["findings"] and row["findings"]["error"] + row["findings"]["warning"] > 0:
            with_findings += 1
        stale += 1 if row["stale"] else 0
    return {"scenes": [{"scene": s, "count": c} for s, c in sorted(scenes.items())], "state": state,
            "review": review, "freshness": fresh, "with_findings": with_findings, "stale": stale}


def _baseline_summary(ctx: _Ctx) -> dict:
    missing = outdated = 0
    for key in ctx.doc.order:
        core = ctx.core(key)
        reasons = {r for p in fp.PARTS if core["parts"][p]["freshness"] == "unknown" for r in core["reasons"][p]}
        missing += 1 if "no_baseline" in reasons else 0
        outdated += 1 if "file_replaced" in reasons else 0
    return {"missing": missing, "outdated": outdated}


def _all_rows(ctx: _Ctx) -> list[dict]:
    return [_row(ctx, key) for key in ctx.doc.order]


# --- Возможности: чтение ---------------------------------------------------------------


@capability("shots_list", title="Список шотов", access="read", category="shots")
def shots_list(project_id: str, scene: int | None = None, state: str = "", review: str = "",
               freshness: str = "", stale_only: bool = False, with_findings: bool = False,
               query: str = "", sort: str = "scene_shot", limit: int = 60, offset: int = 0) -> dict:
    flt = _check_filters(scene, state, review, freshness, stale_only, with_findings, query, sort)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise StudioError("invalid_argument", "limit: целое от 1 до 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise StudioError("invalid_argument", "offset: неотрицательное целое")
    ctx = _Ctx(project_id)
    titles = {"state_titles": dict(STATE_TITLES), "freshness_titles": dict(FRESHNESS_TITLES),
              "review_titles": dict(REVIEW_TITLES)}
    if ctx.doc is None:
        return success(project_id=project_id, revision="r:empty", items=[], total=0, offset=offset, limit=limit,
                       facets=_facets([]), baseline={"missing": 0, "outdated": 0}, reason="no_shots_file",
                       hook_errors=[], **titles)
    rows = _all_rows(ctx)
    picked = [r for r in rows if _matches(r, ctx.doc.pairs[r["shot_key"]], flt)]
    picked = _sorted_rows(picked, flt["sort"])
    revision = "r:" + fp.h16(rows)
    return success(project_id=project_id, revision=revision, items=picked[offset:offset + limit], total=len(picked),
                   offset=offset, limit=limit, facets=_facets(rows), baseline=_baseline_summary(ctx),
                   reason="empty" if not rows else None, hook_errors=ctx.hook_errors, **titles)


@capability("shots_keys", title="Ключи шотов по фильтру", access="read", category="shots")
def shots_keys(project_id: str, scene: int | None = None, state: str = "", review: str = "",
               freshness: str = "", stale_only: bool = False, with_findings: bool = False,
               query: str = "", sort: str = "scene_shot", limit: int = 5000) -> dict:
    flt = _check_filters(scene, state, review, freshness, stale_only, with_findings, query, sort)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise StudioError("invalid_argument", "limit: целое от 1 до 5000")
    ctx = _Ctx(project_id)
    if ctx.doc is None:
        return success(keys=[], total=0, truncated=False)
    plain = (flt["scene"] is None and not flt["state"] and not flt["review"] and not flt["freshness"]
             and not flt["stale_only"] and not flt["with_findings"] and flt["sort"] == "scene_shot")
    if plain and not flt["query"]:
        keys = list(ctx.doc.order)  # без фильтров строки не строим
    elif plain:
        keys = [k for k in ctx.doc.order if flt["query"] in ctx.doc.pairs[k].search]
    else:
        rows = [r for r in _all_rows(ctx) if _matches(r, ctx.doc.pairs[r["shot_key"]], flt)]
        keys = [r["shot_key"] for r in _sorted_rows(rows, flt["sort"])]
    return success(keys=keys[:limit], total=len(keys), truncated=len(keys) > limit)


@capability("shot_get", title="Карточка шота", access="read", category="shots")
def shot_get(project_id: str, shot_key: str) -> dict:
    key = _require_key(shot_key)
    ctx = _Ctx(project_id)
    if ctx.doc is None or key not in ctx.doc.pairs:
        raise StudioError("not_found", f"Шот {key} не найден")
    card = _card(ctx, key)
    return success(shot=card, hook_errors=ctx.hook_errors)


# --- Отпечатки: запись -----------------------------------------------------------------


def _record_for(core: dict, part: str, source: str) -> dict:
    return fp.make_record(core["comps"][part], core["tokens"][part], source)


def _write_baselines(ctx: _Ctx, wanted: list[tuple[str, str, str]], only_missing: bool = False) -> int:
    """wanted: (ключ, часть, source); пишет текущий отпечаток части. Возвращает число записей.

    only_missing: части, у которых запись появилась, пока ждали замок, не трогаем."""
    if not wanted:
        return 0
    records = [(key, part, _record_for(ctx.core(key), part, source)) for key, part, source in wanted]

    def apply(shots: dict) -> int:
        done = 0
        for key, part, record in records:
            if only_missing and isinstance((shots.get(key) or {}).get(part), dict):
                continue
            shots.setdefault(key, {})[part] = record
            done += 1
        return done

    done = fp.update_records(ctx.project_id, apply)
    ctx.records = fp.load_records(ctx.project_id)
    ctx._cores.clear()
    return done


def _unknown_parts(ctx: _Ctx, key: str, only_no_baseline: bool = False) -> list[str]:
    core = ctx.core(key)
    out = []
    for part in fp.PARTS:
        if core["parts"][part]["freshness"] != "unknown":
            continue
        if only_no_baseline and "no_baseline" not in core["reasons"][part]:
            continue
        out.append(part)
    return out


def capture_baseline_before_write(project_id: str, shot_keys: list[str] | None = None) -> None:
    """Записать отпечатки частей без записи до правки shots.json (сырым редактором или shot_update)."""
    ctx = _Ctx(project_id)
    if ctx.doc is None:
        return
    keys = [k for k in (shot_keys or ctx.doc.order) if k in ctx.doc.pairs]
    wanted = [(k, p, "adopted") for k in keys for p in _unknown_parts(ctx, k, only_no_baseline=True)]
    _write_baselines(ctx, wanted, only_missing=True)


@capability("shots_adopt_baseline", title="Считать кадры актуальными", access="write", category="shots")
def shots_adopt_baseline(project_id: str, shot_keys: list[str] | None = None,
                         parts: list[str] | None = None) -> dict:
    if shot_keys is not None:
        if not isinstance(shot_keys, list) or any(parse_shot_key(k) is None for k in shot_keys):
            raise StudioError("invalid_argument", "shot_keys: список ключей вида 2-3")
    if parts is not None and (not isinstance(parts, list) or any(p not in fp.PARTS for p in parts)):
        raise StudioError("invalid_argument", "parts: start, end, video")
    ctx = _Ctx(project_id)
    if ctx.doc is None:
        return success(adopted=0, parts=0, skipped_stale=0)
    keys = parse_shot_keys(shot_keys) if shot_keys is not None else list(ctx.doc.order)
    keys = [k for k in keys if k in ctx.doc.pairs]
    wanted_parts = tuple(parts) if parts is not None else fp.PARTS
    wanted: list[tuple[str, str, str]] = []
    skipped = 0
    for key in keys:
        core = ctx.core(key)
        for part in wanted_parts:
            freshness = core["parts"][part]["freshness"]
            if freshness == "unknown":
                wanted.append((key, part, "adopted"))
            elif freshness == "stale":
                skipped += 1
    _write_baselines(ctx, wanted)
    return success(adopted=len({k for k, _, _ in wanted}), parts=len(wanted), skipped_stale=skipped)


def reconcile_after_run(project_id: str, since_ts: float, shot_keys: list[str] | None = None) -> dict:
    """Для файлов, появившихся с since_ts, записать текущий отпечаток (source=generated)."""
    ctx = _Ctx(project_id)
    if ctx.doc is None:
        return {"updated": 0, "shots": 0}
    keys = [k for k in (parse_shot_keys(shot_keys) if shot_keys is not None else ctx.doc.order) if k in ctx.doc.pairs]
    limit_ns = int((float(since_ts) - 1) * 1e9)
    wanted = []
    for key in keys:
        files = ctx.core(key)["files"]
        for part in fp.PARTS:
            if files[part] and files[part]["mtime_ns"] >= limit_ns:
                wanted.append((key, part, "generated"))
    _write_baselines(ctx, wanted)
    return {"updated": len(wanted), "shots": len({k for k, _, _ in wanted})}


def get_baseline_record(project_id: str, shot_key: str, part: str) -> dict | None:
    key = _require_key(shot_key)
    record = (fp.load_records(project_id).get(key) or {}).get(part)
    return copy.deepcopy(record) if isinstance(record, dict) else None


def set_baseline_record(project_id: str, shot_key: str, part: str, record: dict, source: str = "restored") -> None:
    key = _require_key(shot_key)
    if part not in fp.PARTS or not isinstance(record, dict) or not isinstance(record.get("components"), dict) \
            or not isinstance(record.get("fp"), str):
        raise StudioError("invalid_argument", "Запись отпечатка: нужны part, fp и components")
    if not isinstance(source, str) or not source.strip():
        raise StudioError("invalid_argument", "source: непустая строка")
    new = {**copy.deepcopy(record), "source": source, "at": fp.now_iso()}

    def apply(shots: dict) -> None:
        shots.setdefault(key, {})[part] = new

    fp.update_records(project_id, apply)


# --- Публичные помощники для соседей ---------------------------------------------------


def stale_shots_summary(project_id: str) -> dict | None:
    """{"count", "keys"} шотов со статусом stale; None, если шотов нет или файл не читается."""
    try:
        ctx = _Ctx(project_id)
        if ctx.doc is None or not ctx.doc.order:
            return None
        keys = [k for k in ctx.doc.order if ctx.core(k)["stale_parts"]]
    except StudioError:
        return None
    return {"count": len(keys), "keys": keys}


def shot_keys(project_id: str) -> list[str]:
    ctx = _Ctx(project_id)
    return list(ctx.doc.order) if ctx.doc else []


def shot_files(project_id: str, shot_key: str) -> dict:
    """Абсолютные пути существующих файлов шота: {"start": Path | None, "end": ..., "video": ...}."""
    key = _require_key(shot_key)
    ctx = _Ctx(project_id)
    if ctx.doc is None or key not in ctx.doc.pairs:
        raise StudioError("not_found", f"Шот {key} не найден")
    files = ctx.core(key)["files"]
    return {part: (files[part]["abs"] if files[part] else None) for part in fp.PARTS}


# --- Правка полей ----------------------------------------------------------------------


def _validate_shape(changes: Any) -> dict:
    if not isinstance(changes, dict):
        raise StudioError("invalid_argument", "changes: объект с полями для правки")
    extra = sorted(set(changes) - set(EDIT_FIELDS))
    if extra:
        raise StudioError("invalid_argument",
                          f"Эти поля править нельзя: {', '.join(map(str, extra))}. Допустимы: {', '.join(EDIT_FIELDS)}")
    clean: dict[str, Any] = {}
    for name, value in changes.items():
        if name == "duration_s":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DURATION:
                raise StudioError("invalid_argument", f"duration_s: целое от 1 до {MAX_DURATION}")
            clean[name] = value
            continue
        if not isinstance(value, str):
            raise StudioError("invalid_argument", f"{name}: нужна строка")
        if "\0" in value:
            raise StudioError("invalid_argument", f"{name}: нулевой символ недопустим")
        text = value.replace("\r\n", "\n")
        if len(text) > MAX_TEXT:
            raise StudioError("invalid_argument", f"{name}: не больше {MAX_TEXT} символов")
        if name in ("start_prompt", "end_prompt") and not text.strip():
            raise StudioError("invalid_argument", f"{name}: промпт не может быть пустым")
        clean[name] = text
    return clean


def _supported_durations(root: Path) -> list[int] | None:
    data = _read_json(root / CAPS_REL)
    values = data.get("supported_durations") if isinstance(data, dict) else None
    if isinstance(values, list) and values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return values
    return None


def _read_raw(root: Path) -> dict:
    try:
        raw = (root / SHOTS_REL).read_bytes()
    except FileNotFoundError as exc:
        raise StudioError("not_found", "Файл шотов не найден") from exc
    except OSError as exc:
        raise StudioError("unavailable", "Файл шотов не читается") from exc
    return _parse_raw(raw)


def _parse_raw(raw: bytes) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StudioError("unavailable", "Файл шотов не читается как JSON; откройте его в редакторе файлов") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise StudioError("unavailable", "В файле шотов нет списка items; откройте его в редакторе файлов")
    return doc


def _conflict(start: dict | None, end: dict | None) -> StudioError:
    return StudioError("conflict", "Шот изменён в другом месте. Обновите карточку и повторите правку.",
                       current_digest=_digest_of(start, end), current=_current_values(start, end))


def _duration_update(item: dict, seconds: int) -> dict:
    update = {"duration_s": seconds, "duration_source": "manual", "timing": fp.format_timing(seconds)}
    if "duration_requested_s" not in item:
        old = fp.parse_timing_seconds(item.get("timing"))
        update["duration_requested_s"] = old if old is not None else seconds
    return update


def _apply_changes(start: dict, end: dict | None, changes: dict) -> tuple[dict, dict | None, list[str]]:
    new_s, new_e = dict(start), (dict(end) if end is not None else None)
    changed: list[str] = []
    for name, value in changes.items():
        before = (copy.deepcopy(new_s), copy.deepcopy(new_e))
        if name == "duration_s":
            new_s.update(_duration_update(new_s, value))
            if new_e is not None:
                new_e.update(_duration_update(new_e, value))
        else:
            side, field = _TEXT_FIELD[name]
            (new_s if side == "start" else new_e)[field] = value
        if (new_s, new_e) != before:
            changed.append(name)
    return new_s, new_e, changed


def shot_update_impl(project_id: str, shot_key: str, changes: dict, base_digest: str | None) -> dict:
    key = _require_key(shot_key)
    root = project_dir(project_id)
    clean = _validate_shape(changes)
    if base_digest is None:
        pass
    elif not isinstance(base_digest, str) or not base_digest.strip():
        raise StudioError("invalid_argument", "base_digest: отпечаток из карточки шота")
    doc = _read_raw(root)
    group = _group_items(doc["items"]).get(key)
    if group is None:
        raise StudioError("not_found", f"Шот {key} не найден")
    start, end = group["start"], group["end"]
    if start is None:
        raise StudioError("invalid_argument", "В файле нет элемента start этого шота: правка запрещена")
    if end is None and ({"end_prompt", "end_negative_prompt"} & set(clean)):
        raise StudioError("invalid_argument", "У шота нет элемента end: промпт конца править нельзя")
    warnings: list[dict] = []
    if "duration_s" in clean:
        supported = _supported_durations(root)
        if supported is None:
            warnings.append({"code": "duration_unchecked",
                             "message": "Допустимые длительности модели неизвестны: значение не проверено."})
        elif clean["duration_s"] not in supported:
            raise StudioError("invalid_argument",
                              f"Модель не поддерживает длительность {clean['duration_s']} с. "
                              f"Допустимо: {', '.join(map(str, supported))}", supported=supported)
    if clean.get("video_prompt") == "":
        warnings.append({"code": "video_prompt_empty", "message": "Промпт видео пустой."})
    locked, reason, run_warnings = _lock_state(project_id)
    if locked:
        raise StudioError("locked", reason or "Шот сейчас нельзя править")
    warnings += run_warnings
    warnings.append({"code": "storyboard_rerun_overwrites", "message": STORYBOARD_HINT})
    if base_digest is not None and _digest_of(start, end) != base_digest:
        raise _conflict(start, end)

    new_s, new_e, changed = _apply_changes(start, end, clean)
    if not changed:
        ctx = _Ctx(project_id)
        card = _card(ctx, key)
        return success(shot=card, digest=card["digest"], changed=[], effects=_effects(card), warnings=warnings)

    fields = {}
    old_values = _current_values(start, end)
    new_values = _current_values(new_s, new_e)
    for name in changed:
        fields[name] = {"old": old_values[name], "new": new_values[name]}

    capture_baseline_before_write(project_id, [key])  # сначала отпечаток, потом правка

    expected = base_digest if base_digest is not None else _digest_of(start, end)

    def precondition(raw: bytes | None) -> None:
        if raw is None:
            raise StudioError("not_found", f"Шот {key} не найден")
        current = _group_items(_parse_raw(raw)["items"]).get(key)
        if current is None:
            raise StudioError("not_found", f"Шот {key} не найден")
        if _digest_of(current["start"], current["end"]) != expected:
            raise _conflict(current["start"], current["end"])

    from StoryBookManager.core.file_manager import merge_write_shots_json

    fresh = [new_s] + ([new_e] if new_e is not None else [])
    baseline = [copy.deepcopy(start)] + ([copy.deepcopy(end)] if end is not None else [])
    try:
        merge_write_shots_json(root / SHOTS_REL, fresh, baseline, precondition=precondition)
    except OSError as exc:
        logger.warning("shot_update write failed: %s", exc)
        raise StudioError("internal", "Не удалось записать файл шотов") from exc
    _invalidate(root)
    try:
        fp.append_edit(project_id, key, fields)
    except OSError:
        logger.exception("shot_edits journal write failed")
    ctx = _Ctx(project_id)
    card = _card(ctx, key)
    return success(shot=card, digest=card["digest"], changed=changed, effects=_effects(card), warnings=warnings)


def _effects(card: dict) -> dict:
    effects = dict(card["cascade"])
    effects["note"] = EDIT_NOTE if effects["stale_now"] else None
    return effects


@capability("shot_update", title="Правка промптов и длительности шота", access="write", category="shots")
def shot_update(project_id: str, shot_key: str, changes: dict, base_digest: str) -> dict:
    if not isinstance(base_digest, str) or not base_digest.strip():
        raise StudioError("invalid_argument", "base_digest обязателен: возьмите его из карточки шота")
    return shot_update_impl(project_id, shot_key, changes, base_digest)


def set_duration(project_id: str, shot_key: str, seconds: int, base_digest: str | None = None) -> dict:
    """Единый путь правки длительности для Ф7/Ф11; base_digest=None берёт текущий (для скриптов)."""
    return shot_update_impl(project_id, shot_key, {"duration_s": seconds}, base_digest)
