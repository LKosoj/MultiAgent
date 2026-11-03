"""Журнал прогона: формат, склейка строк, лимит, чистка, секреты, хвост по смещению."""

import logging

import pytest

from custom_tools.storybook.studio import run_logs as rl
from custom_tools.storybook.studio.errors import StudioError


def test_events_and_tail_with_multiline(tmp_path):
    log = rl.RunLog(tmp_path, "run-1")
    try:
        log.event("первая")
        log.event("вторая\nпродолжение\nещё одна")
        logging.getLogger("x").warning("из логгера %s", 5)
    finally:
        log.close()
    result = rl.tail(tmp_path, "run-1")
    lines = result["lines"]
    assert len(lines) == 3 and lines[0].endswith("INFO первая")
    assert lines[1].endswith("вторая\nпродолжение\nещё одна")
    assert "WARNING из логгера 5" in lines[2]
    assert result["truncated"] is False


def test_incremental_read_by_offset(tmp_path):
    log = rl.RunLog(tmp_path, "r")
    try:
        log.event("a")
        first = rl.tail(tmp_path, "r")
        log.event("b")
    finally:
        log.close()
    more = rl.tail(tmp_path, "r", after_offset=first["offset"])
    assert len(more["lines"]) == 1 and more["lines"][0].endswith("b")
    assert rl.tail(tmp_path, "r", after_offset=more["offset"])["lines"] == []
    assert rl.tail(tmp_path, "r", after_offset=10 ** 9)["lines"] == []


def test_secrets_are_masked(tmp_path):
    log = rl.RunLog(tmp_path, "r")
    try:
        log.event("api_key=sk-SECRETVALUE123 и Authorization: Bearer abcdefgh12345678 token: tok123")
    finally:
        log.close()
    text = (tmp_path / ".studio" / "logs" / "r.log").read_text()
    assert "SECRETVALUE" not in text and "abcdefgh12345678" not in text and "tok123" not in text


def test_size_limit_writes_single_truncation_line(tmp_path):
    log = rl.RunLog(tmp_path, "r", limit=400)
    try:
        for i in range(50):
            log.event(f"строка номер {i} " + "x" * 30)
    finally:
        log.close()
    lines = rl.tail(tmp_path, "r", lines=500)["lines"]
    assert sum(rl.TRUNCATED_LINE in line for line in lines) == 1
    assert (tmp_path / ".studio" / "logs" / "r.log").stat().st_size < 700


def test_tail_caps_lines_and_bytes(tmp_path):
    log = rl.RunLog(tmp_path, "r")
    try:
        for i in range(30):
            log.event(f"n{i}")
    finally:
        log.close()
    capped = rl.tail(tmp_path, "r", lines=5)
    assert len(capped["lines"]) == 5 and capped["truncated"] is True and capped["lines"][-1].endswith("n29")
    path = tmp_path / ".studio" / "logs" / "r.log"
    path.write_text("2026-01-01T00:00:00 INFO " + "я" * 600_000 + "\n2026-01-01T00:00:01 INFO хвост\n")
    big = rl.tail(tmp_path, "r")
    assert big["truncated"] is True and big["lines"][-1].endswith("хвост")


def test_prune_keeps_recent_files(tmp_path):
    import os
    folder = tmp_path / ".studio" / "logs"
    folder.mkdir(parents=True)
    for i in range(40):
        path = folder / f"old{i}.log"
        path.write_text("x")
        os.utime(path, (1000 + i, 1000 + i))
    rl.RunLog(tmp_path, "new", keep=10).close()
    names = {p.stem for p in folder.glob("*.log")}
    assert len(names) == 10 and "new" in names and "old39" in names and "old0" not in names


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "a" * 200, None, 5, "x\0y"])
def test_run_id_must_be_safe(tmp_path, bad):
    with pytest.raises(StudioError) as exc:
        rl.tail(tmp_path, bad)
    assert exc.value.code == "invalid_argument"


def test_missing_log_is_not_found(tmp_path):
    with pytest.raises(StudioError) as exc:
        rl.tail(tmp_path, "nope")
    assert exc.value.code == "not_found"
