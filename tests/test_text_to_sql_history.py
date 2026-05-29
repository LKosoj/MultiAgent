"""EPIC 7.21: thread-safe + atomic SQLHistoryManager.

Покрывает:
- concurrent append от нескольких потоков сохраняет все записи и каждая строка
  остаётся валидным JSON;
- per-path mutex переиспользуется между instance с одним и тем же путём;
- get_history корректно читает накопленную историю;
- append не оставляет .tmp файлов в директории.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from custom_tools.text_to_sql.tool import SQLHistoryManager, _get_history_lock


def test_history_append_creates_file(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    mgr.append({"id": "1", "timestamp": "2025-01-01", "generated_sql": "SELECT 1"})
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    parsed = json.loads(text.strip())
    assert parsed["id"] == "1"


def test_history_get_history_after_append(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    for i in range(5):
        mgr.append({"id": str(i), "timestamp": "2025-01-01", "generated_sql": f"SELECT {i}"})
    rows = mgr.get_history(max_entries=100)
    assert len(rows) == 5
    assert [r["id"] for r in rows] == ["0", "1", "2", "3", "4"]


def test_history_get_history_max_entries_caps(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    for i in range(10):
        mgr.append({"id": str(i), "timestamp": "2025-01-01"})
    rows = mgr.get_history(max_entries=3)
    assert len(rows) == 3
    assert [r["id"] for r in rows] == ["7", "8", "9"]


def test_history_per_path_lock_shared_across_instances(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    a = SQLHistoryManager(path)
    b = SQLHistoryManager(path)
    assert a._lock is b._lock
    # Разные пути → разные локи
    other = SQLHistoryManager(tmp_path / "other.jsonl")
    assert other._lock is not a._lock


def test_history_concurrent_append_no_corruption(tmp_path: Path):
    """Запускаем N потоков, каждый делает M append → итог N*M валидных JSONL строк."""
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    n_threads = 8
    per_thread = 25
    errors: list[Exception] = []

    def worker(thread_id: int):
        try:
            for i in range(per_thread):
                mgr.append(
                    {
                        "id": f"{thread_id}-{i}",
                        "timestamp": "2025-01-01",
                        "generated_sql": f"SELECT {thread_id} AS t, {i} AS i",
                    }
                )
        except Exception as exc:  # pragma: no cover - тест провалится через assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"Append errors: {errors}"
    raw = path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == n_threads * per_thread

    # Каждая строка — валидный JSON
    ids = set()
    for line in raw:
        rec = json.loads(line)
        ids.add(rec["id"])
    assert len(ids) == n_threads * per_thread, "Дубликаты или потерянные записи"


def test_history_append_no_orphan_tmp_files(tmp_path: Path):
    """7.21: atomic-rename удаляет tmp при успехе и при ошибке."""
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    for i in range(3):
        mgr.append({"id": str(i)})
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"найдены orphan tmp: {leftovers}"


def test_history_concurrent_read_during_append_returns_consistent_view(tmp_path: Path):
    """Reader не должен видеть partial-line: после get_history() все entries полные."""
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    stop = threading.Event()
    read_errors: list[Exception] = []

    def writer():
        for i in range(200):
            mgr.append({"id": str(i), "generated_sql": "SELECT " + ("x" * 1000)})

    def reader():
        try:
            while not stop.is_set():
                rows = mgr.get_history(max_entries=10000)
                for r in rows:
                    # Все обязательные поля прочитаны полностью
                    assert isinstance(r.get("id"), str)
        except Exception as exc:  # pragma: no cover
            read_errors.append(exc)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    r.start()
    w.start()
    w.join(timeout=30)
    stop.set()
    r.join(timeout=5)
    assert not read_errors


def test_history_get_history_empty_when_file_missing(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    assert mgr.get_history() == []


def test_history_append_preserves_unicode(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    mgr = SQLHistoryManager(path)
    mgr.append({"id": "1", "generated_sql": "SELECT 'привет' AS msg"})
    rows = mgr.get_history()
    assert rows[0]["generated_sql"] == "SELECT 'привет' AS msg"


def test_history_lock_helper_is_idempotent(tmp_path: Path):
    """_get_history_lock возвращает один и тот же лок для одного path."""
    path = tmp_path / "x.jsonl"
    lock_a = _get_history_lock(path)
    lock_b = _get_history_lock(path)
    assert lock_a is lock_b
