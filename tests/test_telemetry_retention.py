import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telemetry.smolagents_telemetry import (
    LocalJSONLExporter,
    SmolagentsTelemetryManager,
    TraceEvent,
    _TRUNCATE_TEXT_PREFIX_BYTES,
    _json_size,
    _truncate_trace_event_dict,
)


def _event(run_id: str, index: int, *, parent_span_id: str | None = None) -> TraceEvent:
    start = datetime(2026, 7, 12, 10, 0, index, tzinfo=timezone.utc)
    return TraceEvent(
        run_id=run_id,
        span_id=f"span-{index}",
        parent_span_id=parent_span_id,
        name="agent_run_test" if parent_span_id is None else "tool_call",
        start_time=start,
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={"payload": "x" * 20},
        events=[],
    )


def _line(event: TraceEvent) -> bytes:
    return (json.dumps(event.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")


def _write_events(path: Path, events: list[TraceEvent]) -> None:
    path.write_bytes(b"".join(_line(event) for event in events))


def test_rotation_keeps_jsonl_lines_and_aggregate_reads_in_segment_order(tmp_path):
    run_id = "rotated-run"
    events = [_event(run_id, index) for index in range(1, 4)]
    max_bytes = len(_line(events[0])) + len(_line(events[1]))
    exporter = LocalJSONLExporter(
        str(tmp_path),
        max_trace_file_size_bytes=max_bytes,
    )

    for event in events:
        exporter._write_trace_event(event)

    segment = tmp_path / f"{run_id}.1.jsonl"
    active = tmp_path / f"{run_id}.jsonl"
    assert segment.exists()
    assert active.exists()
    assert segment.stat().st_size <= max_bytes
    assert active.stat().st_size <= max_bytes
    for path in (segment, active):
        assert path.read_bytes().endswith(b"\n")
        assert all(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())

    manager = SmolagentsTelemetryManager(
        str(tmp_path),
        enabled=False,
        max_trace_file_size_bytes=max_bytes,
    )
    trace_files = manager.get_trace_files()
    assert len(trace_files) == 1
    assert trace_files[0]["run_id"] == run_id
    assert trace_files[0]["events_count"] == 3
    assert trace_files[0]["file_paths"] == [str(segment), str(active)]
    assert [event["span_id"] for event in manager._read_raw_trace_events(run_id)] == [
        "span-1",
        "span-2",
        "span-3",
    ]
    assert [event.span_id for event in manager.read_trace_events(run_id)] == [
        "span-1",
        "span-2",
        "span-3",
    ]
    assert [span["span_id"] for span in manager.load_trace_file(run_id)["spans"]] == [
        "span-1",
        "span-2",
        "span-3",
    ]


def test_size_enforcement_atomically_splits_oversized_legacy_trace(tmp_path):
    run_id = "legacy-run"
    events = [_event(run_id, index) for index in range(1, 5)]
    legacy = tmp_path / f"{run_id}.jsonl"
    _write_events(legacy, events)
    max_bytes = len(_line(events[0])) + len(_line(events[1]))
    manager = SmolagentsTelemetryManager(
        str(tmp_path),
        enabled=False,
        max_trace_file_size_bytes=max_bytes,
    )

    result = manager.enforce_trace_size_limit()

    assert result["rotations"] == 1
    assert result["failures"] == []
    assert result["bytes_before"] == result["bytes_after"]
    assert result["files"] == [
        str(tmp_path / f"{run_id}.1.jsonl"),
        str(legacy),
    ]
    assert not list(tmp_path.glob("*.tmp"))
    assert [event["span_id"] for event in manager._read_raw_trace_events(run_id)] == [
        "span-1",
        "span-2",
        "span-3",
        "span-4",
    ]


def test_incomplete_marking_updates_events_across_segments(tmp_path):
    run_id = "incomplete-run"
    root = _event(run_id, 1)
    child = _event(run_id, 2, parent_span_id=root.span_id)
    max_bytes = len(_line(root))
    exporter = LocalJSONLExporter(
        str(tmp_path),
        max_trace_file_size_bytes=max_bytes,
    )
    exporter._write_trace_event(root)
    exporter._write_trace_event(child)
    manager = SmolagentsTelemetryManager(
        str(tmp_path),
        enabled=False,
        max_trace_file_size_bytes=max_bytes,
    )

    assert manager._mark_trace_as_error(run_id, "interrupted") is True

    raw = manager._read_raw_trace_events(run_id)
    assert [event["span_id"] for event in raw] == [root.span_id, child.span_id]
    assert raw[0]["status"] == "error"
    assert raw[0]["error_message"] == "interrupted"
    assert raw[1]["status"] == "ok"


def test_cleanup_report_is_exact_and_legacy_cleanup_keeps_integer_result(
    tmp_path,
    monkeypatch,
):
    old_files = [tmp_path / "old-run.1.jsonl", tmp_path / "old-run.jsonl"]
    failed_file = tmp_path / "failed-run.jsonl"
    recent_file = tmp_path / "recent-run.jsonl"
    for index, path in enumerate((*old_files, failed_file, recent_file), start=1):
        path.write_text(json.dumps({"run_id": path.stem, "index": index}) + "\n")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    for path in (*old_files, failed_file):
        os.utime(path, (old_timestamp, old_timestamp))
    expected_bytes = sum(path.stat().st_size for path in old_files)
    original_unlink = Path.unlink

    def _unlink(path: Path, *args, **kwargs):
        if path == failed_file:
            raise OSError("injected deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    report = manager.cleanup_old_traces_report(max_age_days=7)

    assert report["deleted_files"] == [str(path) for path in old_files]
    assert report["deleted_count"] == 2
    assert report["deleted_bytes"] == expected_bytes
    assert report["failures"] == [
        {
            "file_path": str(failed_file),
            "error": "injected deletion failure",
        }
    ]
    assert recent_file.exists()
    assert failed_file.exists()

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert manager.cleanup_old_traces(max_age_days=7) == 1


def test_cleanup_keeps_all_segments_when_any_part_of_trace_is_active(tmp_path):
    run_id = "active-run"
    segment = tmp_path / f"{run_id}.1.jsonl"
    active = tmp_path / f"{run_id}.jsonl"
    _write_events(segment, [_event(run_id, 1)])
    _write_events(active, [_event(run_id, 2)])
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(segment, (old_timestamp, old_timestamp))
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    report = manager.cleanup_old_traces_report(max_age_days=7)

    assert report == {
        "deleted_files": [],
        "deleted_count": 0,
        "deleted_bytes": 0,
        "failures": [],
    }
    assert segment.exists()
    assert active.exists()


def test_cleanup_holds_run_lock_while_deleting_trace(tmp_path, monkeypatch):
    run_id = "cleanup-race"
    active = tmp_path / f"{run_id}.jsonl"
    _write_events(active, [_event(run_id, 1)])
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(active, (old_timestamp, old_timestamp))
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)
    exporter = LocalJSONLExporter(str(tmp_path))
    delete_started = threading.Event()
    allow_delete = threading.Event()
    writer_done = threading.Event()
    original_unlink = Path.unlink

    def _blocking_unlink(path: Path, *args, **kwargs):
        if path == active:
            delete_started.set()
            assert allow_delete.wait(timeout=5)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _blocking_unlink)
    cleanup_thread = threading.Thread(
        target=manager.cleanup_old_traces_report,
        kwargs={"max_age_days": 7},
    )
    cleanup_thread.start()
    assert delete_started.wait(timeout=5)

    def _write_event():
        exporter._write_trace_event(_event(run_id, 2))
        writer_done.set()

    writer_thread = threading.Thread(target=_write_event)
    writer_thread.start()
    try:
        assert not writer_done.wait(timeout=0.2)
    finally:
        allow_delete.set()
        cleanup_thread.join(timeout=5)
        writer_thread.join(timeout=5)

    assert not cleanup_thread.is_alive()
    assert not writer_thread.is_alive()
    assert [event["span_id"] for event in manager._read_raw_trace_events(run_id)] == [
        "span-2"
    ]


def test_configure_telemetry_applies_explicit_nondefault_megabyte_cap(
    tmp_path,
    monkeypatch,
):
    import telemetry.smolagents_telemetry as telemetry_module

    monkeypatch.setattr(telemetry_module, "_telemetry_manager", None)
    manager = telemetry_module.configure_telemetry(
        enabled=False,
        traces_dir=str(tmp_path),
        max_trace_file_size_mb=0.25,
    )

    assert manager.max_trace_file_size_bytes == 256 * 1024
    same_manager = telemetry_module.get_telemetry_manager(
        max_trace_file_size_mb=0.5,
    )
    assert same_manager is manager
    assert same_manager.max_trace_file_size_bytes == 512 * 1024


def test_legacy_single_file_remains_readable(tmp_path):
    run_id = "single-file-run"
    event = _event(run_id, 1)
    _write_events(tmp_path / f"{run_id}.jsonl", [event])
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    assert manager._read_raw_trace_events(run_id)[0]["span_id"] == event.span_id
    assert manager.get_trace_files()[0]["file_paths"] == [
        str(tmp_path / f"{run_id}.jsonl")
    ]


def _huge_event(run_id: str, span_id: str) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=None,
        name="agent_run_test",
        start_time=datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={"payload": "x" * 5000},
        events=[],
    )


def test_write_trace_event_self_heals_oversized_single_record_without_full_rewrite(
    tmp_path, monkeypatch
):
    import telemetry.smolagents_telemetry as telemetry_module

    run_id = "oversized-run"
    max_bytes = 1000
    exporter = LocalJSONLExporter(str(tmp_path), max_trace_file_size_bytes=max_bytes)

    read_calls = []
    original_read_trace_lines = telemetry_module._read_trace_lines

    def _spy_read_trace_lines(paths):
        read_calls.append(list(paths))
        return original_read_trace_lines(paths)

    monkeypatch.setattr(telemetry_module, "_read_trace_lines", _spy_read_trace_lines)

    exporter._write_trace_event(_huge_event(run_id, "span-1"))
    exporter._write_trace_event(_huge_event(run_id, "span-2"))

    active = tmp_path / f"{run_id}.jsonl"
    lines = active.read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    for line in lines:
        assert len(line) <= max_bytes
        record = json.loads(line)
        assert record["attributes"]["_truncated"] is True
        assert record["attributes"]["_original_attributes_bytes"] > max_bytes
        assert record["run_id"] == run_id

    # Self-healing at write time means every written line already fits, so
    # the legacy oversized-file rewrite path (glob + re-read every line)
    # never had to run for either write.
    assert read_calls == []
    assert not (tmp_path / f"{run_id}.1.jsonl").exists()


def test_enforce_trace_size_limit_self_heals_oversized_legacy_record(tmp_path):
    run_id = "legacy-oversized-run"
    max_bytes = 500
    legacy = tmp_path / f"{run_id}.jsonl"
    _write_events(legacy, [_huge_event(run_id, "span-1")])
    manager = SmolagentsTelemetryManager(
        str(tmp_path),
        enabled=False,
        max_trace_file_size_bytes=max_bytes,
    )

    result = manager.enforce_trace_size_limit()

    assert any("truncated" in failure["error"] for failure in result["failures"])
    raw = manager._read_raw_trace_events(run_id)
    assert len(raw) == 1
    assert raw[0]["span_id"] == "span-1"
    assert raw[0]["attributes"]["_truncated"] is True
    for path in tmp_path.glob(f"{run_id}*.jsonl"):
        assert path.stat().st_size <= max_bytes

    second_result = manager.enforce_trace_size_limit()

    assert second_result["rotations"] == 0
    assert second_result["failures"] == []


def test_write_trace_event_cache_avoids_repeated_glob_until_external_drift(
    tmp_path, monkeypatch
):
    import telemetry.smolagents_telemetry as telemetry_module

    run_id = "cached-run"
    exporter = LocalJSONLExporter(str(tmp_path), max_trace_file_size_bytes=10_000)

    glob_calls = []
    original_ordered_paths = telemetry_module._ordered_trace_paths

    def _spy_ordered_paths(traces_dir, run_id_arg):
        glob_calls.append(run_id_arg)
        return original_ordered_paths(traces_dir, run_id_arg)

    monkeypatch.setattr(telemetry_module, "_ordered_trace_paths", _spy_ordered_paths)

    for index in range(1, 6):
        exporter._write_trace_event(_event(run_id, index))

    assert glob_calls == [run_id]  # only the first (cache-miss) write globs

    active = tmp_path / f"{run_id}.jsonl"
    segment = tmp_path / f"{run_id}.1.jsonl"
    os.replace(active, segment)  # simulate an external rewrite/rotation

    exporter._write_trace_event(_event(run_id, 6))

    assert glob_calls == [run_id, run_id]  # drift forced exactly one more recompute
    assert segment.exists()
    assert active.exists()
    assert [
        json.loads(line)["span_id"]
        for line in active.read_bytes().splitlines(keepends=True)
    ] == ["span-6"]

    # The cache was correctly refilled after the drift recompute, so the
    # next write is a hit again and does not glob.
    exporter._write_trace_event(_event(run_id, 7))
    assert glob_calls == [run_id, run_id]


def test_cleanup_removes_lock_file_only_when_run_fully_deleted(tmp_path, monkeypatch):
    ok_run_id = "cleanup-lock-ok"
    failed_run_id = "cleanup-lock-failed"
    ok_file = tmp_path / f"{ok_run_id}.jsonl"
    failed_file = tmp_path / f"{failed_run_id}.jsonl"
    _write_events(ok_file, [_event(ok_run_id, 1)])
    _write_events(failed_file, [_event(failed_run_id, 1)])
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(ok_file, (old_timestamp, old_timestamp))
    os.utime(failed_file, (old_timestamp, old_timestamp))
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    original_unlink = Path.unlink

    def _unlink(path: Path, *args, **kwargs):
        if path == failed_file:
            raise OSError("injected deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    manager.cleanup_old_traces_report(max_age_days=7)

    assert not (tmp_path / f".{ok_run_id}.lock").exists()
    assert (tmp_path / f".{failed_run_id}.lock").exists()


def test_read_trace_events_sorts_out_of_order_and_naive_datetime_without_typeerror(
    tmp_path,
):
    run_id = "sort-naive-run"
    later_child = TraceEvent(
        run_id=run_id,
        span_id="child-later",
        parent_span_id="root-x",
        name="tool_call",
        start_time=datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )
    earlier_child_missing_start = TraceEvent(
        run_id=run_id,
        span_id="child-earlier",
        parent_span_id="root-x",
        name="tool_call",
        start_time=None,
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={},
        events=[],
    )
    # Written out of chronological order: the later-starting span is first on
    # disk, and the earlier span is missing a start_time entirely.
    _write_events(
        tmp_path / f"{run_id}.jsonl",
        [later_child, earlier_child_missing_start],
    )
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    # Neither child has parent_span_id=None, so _mark_trace_as_error_locked's
    # strategy 3 appends a fresh error span using datetime.now() (naive,
    # since the last raw event on disk has no start_time to parse).
    assert manager._mark_trace_as_error(run_id, "interrupted") is True

    events = manager.read_trace_events(run_id)

    assert [event.span_id for event in events[:2]] == ["child-earlier", "child-later"]
    assert events[2].name == "trace_incomplete_error"
    assert events[2].start_time.tzinfo is None


def test_get_trace_files_can_skip_events_count(tmp_path, monkeypatch):
    run_id = "count-skip-run"
    events = [_event(run_id, 1), _event(run_id, 2)]
    _write_events(tmp_path / f"{run_id}.jsonl", events)
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)

    calls = []
    original_count_lines = SmolagentsTelemetryManager._count_lines

    def _spy_count_lines(self, file_path):
        calls.append(file_path)
        return original_count_lines(self, file_path)

    monkeypatch.setattr(SmolagentsTelemetryManager, "_count_lines", _spy_count_lines)

    skipped = manager.get_trace_files(include_events_count=False)
    assert skipped[0]["events_count"] is None
    assert calls == []

    full = manager.get_trace_files()
    assert full[0]["events_count"] == 2
    assert calls == [tmp_path / f"{run_id}.jsonl"]


def test_cache_hit_rotation_does_not_clobber_segment_claimed_by_other_writer(
    tmp_path,
):
    """Regression for the CRITICAL cache-hit rotation finding: a writer with a
    warm (verified_clean) cache must re-check disk before rotating, not
    blindly os.replace() onto the cached next_segment_number - a second
    process/exporter writing the same run_id (streamlit, agui service, a
    workflow worker) may already have claimed that segment number.
    """
    run_id = "shared-run"
    first_event = _event(run_id, 1)
    max_bytes = len(_line(first_event))
    # Two independent exporter instances targeting the same traces_dir and
    # run_id, simulating two processes sharing one traces directory.
    exporter_a = LocalJSONLExporter(str(tmp_path), max_trace_file_size_bytes=max_bytes)
    exporter_b = LocalJSONLExporter(str(tmp_path), max_trace_file_size_bytes=max_bytes)

    # exporter_a writes the first event: no rotation yet, but its cache is now
    # warm (verified_clean=True, base_size=max_bytes, next_segment_number=1).
    exporter_a._write_trace_event(first_event)
    assert exporter_a._run_states[run_id].next_segment_number == 1

    # exporter_b (a different process) writes next and rotates on its own,
    # claiming segment 1 on disk with span-1's content.
    exporter_b._write_trace_event(_event(run_id, 2))
    segment_1 = tmp_path / f"{run_id}.1.jsonl"
    assert segment_1.exists()
    segment_1_before = segment_1.read_bytes()
    assert json.loads(segment_1_before)["span_id"] == "span-1"

    # exporter_a's cache is still warm and unaware exporter_b already claimed
    # segment 1: the base file's size now coincidentally matches its cached
    # base_size (all these events serialize to the same length), so this is
    # a genuine stale-cache cache-hit, not a cache-miss recompute.
    exporter_a._write_trace_event(_event(run_id, 3))

    # exporter_a must NOT have clobbered exporter_b's segment 1.
    assert segment_1.read_bytes() == segment_1_before
    segment_2 = tmp_path / f"{run_id}.2.jsonl"
    assert segment_2.exists()
    assert json.loads(segment_2.read_bytes())["span_id"] == "span-2"
    active = tmp_path / f"{run_id}.jsonl"
    assert json.loads(active.read_bytes())["span_id"] == "span-3"


def test_stage2_truncation_of_cyrillic_name_stays_within_byte_budget(tmp_path):
    """Regression for the byte-vs-character truncation finding: stage-2 of
    _truncate_trace_event_dict caps name/error_message with a *byte* budget
    (_TRUNCATE_TEXT_PREFIX_BYTES, checked via _json_size which counts UTF-8
    bytes), but slicing a Python str slices by *character*. Multi-byte text
    (Cyrillic is 2 bytes/char) must not overshoot the byte budget.
    """
    cyrillic_name = "ошибка_выполнения_агента_" * 20
    event_dict = TraceEvent(
        run_id="cyrillic-run",
        span_id="span-1",
        parent_span_id=None,
        name=cyrillic_name,
        start_time=datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc),
        end_time=None,
        duration_ms=None,
        status="ok",
        attributes={"payload": "x" * 50},
        events=[],
    ).to_dict()
    # Small enough that stage 1 (attributes stripped) still doesn't fit
    # because of the long name, forcing stage 2 to run; large enough that
    # a correctly byte-bounded name fits.
    max_bytes = 500

    result = _truncate_trace_event_dict(event_dict, max_bytes)

    assert result is not None
    assert _json_size(result) <= max_bytes
    assert len(result["name"].encode("utf-8")) <= _TRUNCATE_TEXT_PREFIX_BYTES
    # The truncated string must itself be valid text (no dangling partial
    # multi-byte sequence at the cut boundary).
    result["name"].encode("utf-8").decode("utf-8")


def test_cleanup_invalidates_warm_exporter_cache_forcing_recompute(
    tmp_path, monkeypatch
):
    """Regression for the nitpick that every retention test builds its
    manager with enabled=False (exporter=None), so cleanup_old_traces_report's
    invalidate_run_cache() call is a no-op everywhere and never actually
    exercised. Wire a real exporter to the manager, warm its cache, run
    cleanup, and prove the cache entry is dropped and the next write
    recomputes from disk instead of trusting stale bookkeeping.
    """
    import telemetry.smolagents_telemetry as telemetry_module

    run_id = "cleanup-invalidate-run"
    exporter = LocalJSONLExporter(str(tmp_path))
    manager = SmolagentsTelemetryManager(str(tmp_path), enabled=False)
    manager.exporter = exporter

    exporter._write_trace_event(_event(run_id, 1))
    assert run_id in exporter._run_states

    active = tmp_path / f"{run_id}.jsonl"
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(active, (old_timestamp, old_timestamp))

    report = manager.cleanup_old_traces_report(max_age_days=7)

    assert report["deleted_count"] == 1
    assert run_id not in exporter._run_states

    glob_calls = []
    original_ordered_paths = telemetry_module._ordered_trace_paths

    def _spy_ordered_paths(traces_dir, run_id_arg):
        glob_calls.append(run_id_arg)
        return original_ordered_paths(traces_dir, run_id_arg)

    monkeypatch.setattr(telemetry_module, "_ordered_trace_paths", _spy_ordered_paths)

    exporter._write_trace_event(_event(run_id, 2))

    assert glob_calls == [run_id]  # invalidated cache forces exactly one recompute
    assert [
        json.loads(line)["span_id"]
        for line in active.read_bytes().splitlines(keepends=True)
    ] == ["span-2"]
