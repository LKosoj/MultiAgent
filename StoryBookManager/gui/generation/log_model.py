"""Журнал прогона: разбор строк, докачка по смещению, текст для копирования (без tkinter)."""

import re
from dataclasses import dataclass, field, replace
from typing import List, Optional

from StoryBookManager.gui.generation.texts import LOG_LEVEL_WORDS, fmt

LOG_LINES_CAP = 2000

_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+([\s\S]*)$")


def parse_line(raw: str) -> dict:
    """{time, level, word, text}; строка не в формате журнала - только text."""
    match = _LINE.match(raw)
    if not match:
        return {"time": None, "level": None, "word": None, "text": raw}
    level = match.group(2)
    return {"time": match.group(1), "level": level, "word": LOG_LEVEL_WORDS.get(level, level), "text": match.group(3)}


def log_text(lines) -> str:
    return "\n".join(lines)


def append_tail(current: List[str], new: List[str], cap: int = LOG_LINES_CAP) -> List[str]:
    merged = list(current) + list(new)
    return merged[-cap:] if len(merged) > cap else merged


@dataclass(frozen=True)
class LogState:
    run_id: Optional[str] = None
    lines: tuple = field(default_factory=tuple)
    offset: int = 0
    truncated: bool = False
    error: Optional[str] = None


EMPTY_LOG = LogState()


def reset_for(state: LogState, run_id: Optional[str]) -> LogState:
    """Смена прогона сбрасывает показанное."""
    return state if state.run_id == run_id else LogState(run_id=run_id)


def apply_tail(state: LogState, run_id: str, result: dict) -> LogState:
    """Добавить ответ run_log_tail; ответ для другого прогона отбрасывается."""
    if state.run_id != run_id:
        return state
    lines = append_tail(list(state.lines), [str(x) for x in result.get("lines") or []])
    return LogState(run_id=run_id, lines=tuple(lines), offset=int(result.get("offset") or 0),
                    truncated=bool(result.get("truncated")) or state.truncated, error=None)


def apply_error(state: LogState, run_id: str, code: str, message: str) -> LogState:
    if state.run_id != run_id:
        return state
    text = fmt("log_deleted") if code == "not_found" else (message or fmt("log_deleted"))
    return replace(state, error=text)


def cleared(state: LogState) -> LogState:
    """«Очистить вид»: скрыть показанное, дальше только новое."""
    return replace(state, lines=())
