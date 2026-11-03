"""Ревью шотов (Ф7): файл 97_shots/review_state.json, запись под замком с проверкой отпечатка.

Статусы информационные: пайплайн их не читает. Нет записи у шота = «Ожидает».
Вспомогательный модуль: возможностей (@capability) здесь нет, они в review.py.
"""

from __future__ import annotations

import copy
import fcntl
import importlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .artifacts import obj_digest
from .errors import StudioError
from .paths import project_dir, studio_dir
from .project_store import write_json_atomic
from .reports import parse_shot_key
from .shot_versions import now_iso

REVIEW_REL = "97_shots/review_state.json"
STATUSES = ("pending", "approved", "redo", "rejected")
TITLES = {"pending": "Ожидает", "approved": "Одобрен", "redo": "Переделать", "rejected": "Отклонён"}
NONE_DIGEST = "j1:none"
MAX_NOTE = 2000
MAX_HISTORY = 30
MAX_KEYS = 500
SURFACES = ("tk", "react", "system")

_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def status_title(status: Any) -> str:
    return TITLES.get(status if status in TITLES else "pending", TITLES["pending"])


def entry_digest(entry: dict | None) -> str:
    return obj_digest(entry) if entry else NONE_DIGEST


def surface_of(value: Any) -> str:
    return value if value in SURFACES else "react"


def _path(project_id: str) -> Path:
    return project_dir(project_id) / REVIEW_REL


def _thread_lock(path: str) -> threading.RLock:
    with _GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.RLock())


@contextmanager
def _locked(project_id: str) -> Iterator[None]:
    """Замок записи ревью (не замок проекта: ревью можно менять во время прогона)."""
    locks = studio_dir(project_id, create=True) / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    with _thread_lock(str(locks)):
        fd = os.open(locks / "review_state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _clean_entry(raw: Any) -> dict | None:
    if isinstance(raw, str):
        raw = {"status": raw}
    if not isinstance(raw, dict):
        return None
    entry = copy.deepcopy(raw)
    if entry.get("status") not in STATUSES:
        entry["status"] = "pending"
    if not isinstance(entry.get("note"), str):
        entry.pop("note", None)
    if not isinstance(entry.get("history"), list):
        entry["history"] = []
    return entry


def load(project_id: str) -> tuple[dict, bool]:
    """(документ, битый ли файл). Нет файла или битый JSON -> пустое состояние."""
    path = _path(project_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"version": 1, "updated_at": None, "shots": {}}, False
    try:
        data = json.loads(raw)
    except ValueError:
        return {"version": 1, "updated_at": None, "shots": {}}, True
    if not isinstance(data, dict):
        return {"version": 1, "updated_at": None, "shots": {}}, True
    body = data.get("shots") if isinstance(data.get("shots"), dict) else data
    shots: dict[str, dict] = {}
    for raw_key, raw_entry in body.items():
        key = parse_shot_key(raw_key)
        entry = _clean_entry(raw_entry)
        if key and entry is not None:
            shots[key] = entry
    return {**{k: v for k, v in data.items() if k != "shots"}, "version": 1, "shots": shots}, False


def doc_digest(doc: dict) -> str:
    return obj_digest(doc.get("shots") or {})


def _known_keys(project_id: str) -> list[str]:
    try:
        shots = importlib.import_module(f"{__package__}.shots")
        return list(shots.shot_keys(project_id))
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.shots":
            raise
        return []


def check_keys(project_id: str, keys: Any) -> list[str]:
    """Нормализованные известные ключи; неизвестные -> not_found со списком."""
    if not isinstance(keys, (list, tuple)) or not keys:
        raise StudioError("invalid_argument", "keys: нужен непустой список ключей шотов")
    if len(keys) > MAX_KEYS:
        raise StudioError("invalid_argument", f"Не больше {MAX_KEYS} шотов за раз")
    result: list[str] = []
    bad: list[str] = []
    for raw in keys:
        key = parse_shot_key(raw)
        if key is None:
            bad.append(str(raw))
        elif key not in result:
            result.append(key)
    known = set(_known_keys(project_id))
    bad += [k for k in result if k not in known]
    if bad:
        raise StudioError("not_found", "Неизвестные шоты: " + ", ".join(bad), keys=bad)
    return result


def _view(entry: dict | None, *, history: bool = False) -> dict:
    entry = entry or {}
    status = entry.get("status") if entry.get("status") in STATUSES else "pending"
    out = {
        "status": status, "status_title": status_title(status), "note": entry.get("note") or "",
        "selected_take": entry.get("selected_take"), "status_by": entry.get("status_by"),
        "status_changed_at": entry.get("status_changed_at"), "entry_digest": entry_digest(entry or None),
    }
    if history:
        out["history"] = list(entry.get("history") or [])
    return out


def view(entry: dict | None, *, history: bool = False) -> dict:
    return _view(entry, history=history)


def counts_for(shots: dict, keys: list[str]) -> dict:
    result = {s: 0 for s in STATUSES}
    for key in keys:
        entry = shots.get(key)
        status = entry.get("status") if isinstance(entry, dict) and entry.get("status") in STATUSES else "pending"
        result[status] += 1
    return result


def _ensure_writable(project_id: str) -> dict:
    doc, corrupt = load(project_id)
    if corrupt:
        raise StudioError("conflict", "Файл ревью повреждён. Откройте 97_shots/review_state.json в режиме JSON",
                          reason="review_file_corrupt", path=REVIEW_REL)
    return doc


def _save(project_id: str, doc: dict) -> None:
    doc["version"] = 1
    doc["updated_at"] = now_iso()
    path = _path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, doc)


def _push(entry: dict, record: dict) -> None:
    history = list(entry.get("history") or [])
    history.append(record)
    entry["history"] = history[-MAX_HISTORY:]


def clean_note(note: Any) -> str:
    if not isinstance(note, str):
        raise StudioError("invalid_argument", "note: нужна строка")
    text = note.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text):
        raise StudioError("invalid_argument", "В заметке недопустимы управляющие символы")
    if len(text) > MAX_NOTE:
        raise StudioError("invalid_argument", f"Заметка длиннее {MAX_NOTE} символов")
    return text


def set_status(project_id: str, keys: list[str], status: str, *, expected_digests: dict | None = None,
               note: str | None = None, by: str = "react") -> dict:
    if status not in STATUSES:
        raise StudioError("invalid_argument", "status: " + ", ".join(STATUSES))
    if expected_digests is not None and not isinstance(expected_digests, dict):
        raise StudioError("invalid_argument", "expected_digests: объект {ключ: отпечаток}")
    text = clean_note(note) if note is not None else None
    keys = check_keys(project_id, keys)
    expected = {parse_shot_key(k) or k: v for k, v in (expected_digests or {}).items()}
    with _locked(project_id):
        doc = _ensure_writable(project_id)
        shots = doc["shots"]
        changed_against = {k: entry_digest(shots.get(k)) for k in keys if k in expected}
        stale = {k: shots.get(k) for k, d in changed_against.items() if d != expected[k]}
        if stale:
            raise StudioError("conflict", "Ревью шота изменено в другом месте", reason="review_changed",
                              keys=sorted(stale), current={k: _view(v) for k, v in stale.items()})
        updated, unchanged = [], []
        at = now_iso()
        for key in keys:
            entry = shots.get(key) or {"status": "pending", "history": []}
            same_note = text is None or text == (entry.get("note") or "")
            if entry.get("status") == status and same_note:
                unchanged.append(key)
                continue
            if entry.get("status") != status:
                _push(entry, {"at": at, "by": by, "action": "status", "from": entry.get("status", "pending"),
                              "to": status})
                entry["status"], entry["status_changed_at"], entry["status_by"] = status, at, by
            if text is not None:
                entry["note"] = text
            shots[key] = entry
            updated.append(key)
        if updated:
            _save(project_id, doc)
        return {"updated": updated, "unchanged": unchanged, "counts": counts_for(shots, _known_keys(project_id)),
                "digest": doc_digest(doc), "entries": {k: entry_digest(shots.get(k)) for k in keys}}


def set_note(project_id: str, key: str, note: str, expected_digest: str, *, by: str = "react") -> dict:
    if not isinstance(expected_digest, str) or not expected_digest:
        raise StudioError("invalid_argument", "expected_digest: нужен отпечаток записи шота")
    text = clean_note(note)
    key = check_keys(project_id, [key])[0]
    with _locked(project_id):
        doc = _ensure_writable(project_id)
        shots = doc["shots"]
        current = shots.get(key)
        if entry_digest(current) != expected_digest:
            raise StudioError("conflict", "Заметка шота изменена в другом месте", reason="review_changed",
                              keys=[key], current={key: _view(current)})
        entry = current or {"status": "pending", "history": []}
        if text != (entry.get("note") or ""):
            entry["note"] = text
            _push(entry, {"at": now_iso(), "by": by, "action": "note"})
            shots[key] = entry
            _save(project_id, doc)
        return {"entry": _view(shots.get(key), history=True), "entry_digest": entry_digest(shots.get(key)),
                "digest": doc_digest(doc)}


def mark_content_changed(project_id: str, keys: list[str], reason: str, regen_id: str | None = None,
                         take: str | None = None) -> list[str]:
    """Содержимое шота заменено: одобренное/отклонённое/«переделать» -> «Ожидает». Возвращает сброшенные ключи."""
    reset: list[str] = []
    if not keys:
        return reset
    with _locked(project_id):
        doc, corrupt = load(project_id)
        if corrupt:  # битый файл не затираем: сброс не обязателен, потеря данных хуже
            return reset
        shots = doc["shots"]
        dirty = False
        at = now_iso()
        for key in keys:
            entry = shots.get(key)
            if entry is None:
                if take is None:
                    continue
                entry = {"status": "pending", "history": []}
            if entry.get("status", "pending") != "pending":
                record = {"at": at, "by": "system", "action": "auto_reset", "from": entry["status"],
                          "to": "pending", "reason": reason}
                if regen_id:
                    record["regen_id"] = regen_id
                _push(entry, record)
                entry["status"], entry["status_changed_at"], entry["status_by"] = "pending", at, "system"
                reset.append(key)
                dirty = True
            if entry.get("selected_take") != take:
                entry["selected_take"] = take
                dirty = True
            shots[key] = entry
        if dirty:
            _save(project_id, doc)
    return reset


def read_for_hooks(project_id: str) -> dict:
    """Записи шотов для хуков соседей; битый файл читается как пустой."""
    return load(project_id)[0]["shots"]
