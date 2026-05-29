"""W8-T3: тесты helper'а _chroma_safe_delete (strict / non-strict).

Покрывают:
  * strict=True + raise из collection.delete → RuntimeError;
  * strict=False + raise из collection.delete → warning, без падения;
  * collection=None / ids=[] — no-op для обоих режимов;
  * non-strict путь маскирует id'ы в лог-сообщении.
"""
from __future__ import annotations

import logging

import pytest

from custom_tools.text_to_sql.rag.indexing import (
    _chroma_safe_delete,
    _mask_chroma_id,
)


class _FailingCollection:
    def __init__(self):
        self.calls = 0

    def delete(self, ids):  # noqa: D401 — Chroma-like API
        self.calls += 1
        raise RuntimeError("chroma backend exploded")


class _OKCollection:
    def __init__(self):
        self.deleted_ids = None

    def delete(self, ids):
        self.deleted_ids = list(ids)


def test_strict_propagates_as_runtimeerror():
    col = _FailingCollection()
    with pytest.raises(RuntimeError, match="ChromaDB delete failed"):
        _chroma_safe_delete(col, ["sess-agent-1", "sess-agent-2"], strict=True)
    assert col.calls == 1


def test_non_strict_logs_warning_without_raise(caplog):
    col = _FailingCollection()
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.rag.indexing"):
        # Не должно бросать.
        _chroma_safe_delete(
            col,
            ["session-very-long-id-AAAA", "another-id-zzz"],
            strict=False,
        )
    # Был ровно один warning о падении Chroma.
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ChromaDB delete failed" in m for m in msgs)
    # Полный id не должен светиться (он маскируется в _mask_chroma_id).
    assert not any("session-very-long-id-AAAA" in m for m in msgs)


def test_noop_on_none_collection_or_empty_ids():
    # collection=None — не должен ничего делать.
    _chroma_safe_delete(None, ["a", "b"], strict=True)  # не raise
    _chroma_safe_delete(None, ["a", "b"], strict=False)  # не raise

    # ids=[] — тоже no-op даже на падающей коллекции.
    col = _FailingCollection()
    _chroma_safe_delete(col, [], strict=True)
    _chroma_safe_delete(col, [], strict=False)
    assert col.calls == 0


def test_ok_path_does_not_swallow_ids():
    col = _OKCollection()
    _chroma_safe_delete(col, ["x", "y", "z"], strict=True)
    assert col.deleted_ids == ["x", "y", "z"]


def test_mask_chroma_id_short_and_long():
    assert _mask_chroma_id("ab") == "***"
    masked = _mask_chroma_id("longidentifierABCDE")
    assert masked.startswith("long")
    assert masked.endswith("DE")
    assert "..." in masked
