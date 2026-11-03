"""Шоты: токены файлов, отпечатки входа, хранилище отпечатков и журнал правок (без capability)."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .paths import studio_dir

logger = logging.getLogger(__name__)

FP_VERSION = 1
INPUTS_FILE = "shot_inputs.json"
EDITS_FILE = "shot_edits.jsonl"
EDITS_MAX_BYTES = 2 * 1024 * 1024
PARTS = ("start", "end", "video")

# счётчики для тестов производительности
STATS = {"token_reads": 0}

_TOKEN_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_TOKEN_CACHE_SIZE = 8192
_TOKEN_GUARD = threading.Lock()
_CHUNK = 64 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")


def h16(obj: Any) -> str:
    return hashlib.sha1(canon(obj)).hexdigest()[:16]


def fingerprint(components: dict) -> str:
    return "sfp1:" + hashlib.sha256(canon(components)).hexdigest()


# --- Токен файла -----------------------------------------------------------------------


def file_token(path: Path) -> str | None:
    """t1:<размер>:<sha1(голова+хвост)[:8]>; mtime не входит (копирование проекта его меняет)."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    cache_key = (str(path), st.st_size, st.st_mtime_ns)
    with _TOKEN_GUARD:
        hit = _TOKEN_CACHE.get(cache_key)
        if hit is not None:
            _TOKEN_CACHE.move_to_end(cache_key)
            return hit
    try:
        with open(path, "rb") as fh:
            head = fh.read(_CHUNK)
            tail = b""
            if st.st_size > _CHUNK:
                fh.seek(max(_CHUNK, st.st_size - _CHUNK))
                tail = fh.read(_CHUNK)
    except OSError:
        return None
    STATS["token_reads"] += 1
    token = f"t1:{st.st_size}:{hashlib.sha1(head + tail).hexdigest()[:8]}"
    with _TOKEN_GUARD:
        _TOKEN_CACHE[cache_key] = token
        while len(_TOKEN_CACHE) > _TOKEN_CACHE_SIZE:
            _TOKEN_CACHE.popitem(last=False)
    return token


# --- Длительность ----------------------------------------------------------------------


def _clock_seconds(text: str) -> int | None:
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse_timing_seconds(timing: Any) -> int | None:
    """Копия правил video_generator_common.parse_duration_seconds_from_timing; мусор -> None."""
    if isinstance(timing, bool):
        return None
    if isinstance(timing, (int, float)):
        return int(timing) if timing >= 1 else None
    if not isinstance(timing, str) or not timing.strip():
        return None
    text = timing.strip()
    try:
        if " - " in text:
            left, right = text.split(" - ")
            a, b = _clock_seconds(left.strip()), _clock_seconds(right.strip())
            if a is None or b is None:
                return None
            value = b - a
        elif text.endswith("s"):
            value = int(text[:-1])
        elif ":" in text:
            parsed = _clock_seconds(text)
            if parsed is None:
                return None
            value = parsed
        else:
            value = int(text)
    except ValueError:
        return None
    return max(value, 1)


def format_timing(seconds: Any) -> str:
    total = int(round(float(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


# --- Ссылки на референсы ---------------------------------------------------------------

_RE_REPO_PATH = re.compile(r"(?:^|/)plots/storybooks/[^/]+/(.+)$")


def ref_rel(raw: Any) -> str | None:
    """Путь референса -> путь внутри проекта (без id проекта, чтобы копия не «устаревала»)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.replace("\\", "/").strip()
    if text.startswith("/references/"):
        return "20_bible/references/" + text[len("/references/"):]
    match = _RE_REPO_PATH.search(text)
    if match:
        return match.group(1)
    if text.startswith("./"):
        text = text[2:]
    return text


def parse_ref_list(value: Any) -> list[str]:
    """reference_image_paths бывает списком или строкой-репрезентацией списка."""
    if isinstance(value, str):
        loaded: Any = None
        for loader in (json.loads, ast.literal_eval):
            try:
                loaded = loader(value)
                break
            except (ValueError, SyntaxError):
                continue
        value = loaded
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def parse_bg_sheet(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
    if isinstance(value, dict) and isinstance(value.get("sheet_path"), str) and value["sheet_path"].strip():
        return value["sheet_path"]
    return None


# --- Хранилище отпечатков --------------------------------------------------------------

_STORE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_STORE_GUARD = threading.Lock()
_STORE_CACHE: dict[str, tuple[tuple, dict]] = {}


def _thread_lock(name: str) -> threading.Lock:
    with _STORE_GUARD:
        return _STORE_THREAD_LOCKS.setdefault(name, threading.Lock())


def _inputs_path(project_id: str) -> Path:
    return studio_dir(project_id) / INPUTS_FILE


def _empty_store() -> dict:
    return {"version": 1, "fp_version": FP_VERSION, "shots": {}}


def _read_store_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_store()
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("fp_version") != FP_VERSION
            or not isinstance(data.get("shots"), dict)):
        return _empty_store()  # другая формула: записи читаются как «неизвестно»
    return data


def load_records(project_id: str) -> dict:
    """{"2-3": {"start": {...}, ...}}; пишущих побочных эффектов нет."""
    path = _inputs_path(project_id)
    try:
        st = path.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    cached = _STORE_CACHE.get(str(path))
    if cached and cached[0] == sig:
        return cached[1]
    shots = _read_store_file(path)["shots"]
    _STORE_CACHE[str(path)] = (sig, shots)
    return shots


def update_records(project_id: str, mutate: Callable[[dict], Any]) -> Any:
    """Прочитать под замком, применить mutate(shots), записать через tmp + os.replace."""
    root = studio_dir(project_id, create=True)
    locks = root / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    path = root / INPUTS_FILE
    with _thread_lock(str(path)):
        fd = os.open(locks / "shot_inputs.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            store = _read_store_file(path)
            result = mutate(store["shots"])
            tmp = path.with_name(f"{INPUTS_FILE}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            _STORE_CACHE.pop(str(path), None)
            return result
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def make_record(components: dict, token: str | None, source: str) -> dict:
    return {"fp": fingerprint(components), "components": dict(components), "file": token,
            "source": source, "at": now_iso()}


# --- Журнал правок ---------------------------------------------------------------------


def append_edit(project_id: str, shot_key: str, fields: dict) -> None:
    root = studio_dir(project_id, create=True)
    locks = root / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    path = root / EDITS_FILE
    line = json.dumps({"at": now_iso(), "shot_key": shot_key, "fields": fields}, ensure_ascii=False) + "\n"
    with _thread_lock(str(path)):
        fd = os.open(locks / "shot_edits.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if path.stat().st_size > EDITS_MAX_BYTES:
                    os.replace(path, path.with_name(EDITS_FILE + ".1"))  # один архив
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def read_edits(project_id: str, shot_key: str, limit: int = 5) -> list[dict]:
    path = studio_dir(project_id) / EDITS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("shot_key") == shot_key:
            out.append(entry)
            if len(out) >= limit:
                break
    return out
