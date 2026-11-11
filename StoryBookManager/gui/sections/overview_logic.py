"""Модель экрана «Обзор проекта» из ответа project_status (без tkinter).

Подписи берутся из данных сервера; своих копий правил «следующего действия» здесь нет.
Тексты совпадают с lib/storybook/overviewView.ts и texts.ts (React).
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from StoryBookManager.gui.sections.projects_logic import format_bytes, format_datetime
from StoryBookManager.gui.shell.run_status import TONE_BY_STATE, format_rub

TXT_TITLE = "Обзор серии"
TXT_LOADING = "Загрузка состояния серии…"
TXT_NO_PROJECT = "Сначала выберите проект"
TXT_TO_PROJECTS = "К списку проектов"
TXT_REFRESH = "Обновить"
TXT_RETRY = "Повторить"
TXT_STALE = "Данные устарели"
TXT_NEXT = "Следующее действие"
TXT_GO = "Перейти"
TXT_STAGES = "Этапы"
TXT_DONE_STAGE = "Готово"
TXT_FINAL = "Финальное видео"
TXT_WATCH = "Смотреть со звуком"
TXT_SHOW_FOLDER = "Показать в папке"
TXT_NO_FINAL = "Видео ещё не собрано"
TXT_FINAL_OLDER = "Фильм собран раньше, чем изменились кадры"
TXT_STEPS = "Шаги пайплайна"
TXT_WARNINGS = "Предупреждения по отчётам"
TXT_NO_REPORTS = "Отчётов пока нет"
TXT_LAST_RUN = "Последний прогон"
TXT_NO_RUNS = "Прогонов пока не было"
TXT_EMPTY = "Серия ещё не запускалась. Настройте запуск и оцените стоимость."
TXT_GO_GENERATION = "Перейти в Генерацию"
TXT_NO_STALE = "нет данных об устаревании"
TXT_ASSUMED = "Часть суммы - по допущению (нет записанной валюты)"
TXT_OTHER_CURRENCY = "+ в другой валюте"
TXT_LOW_CONFIDENCE = "Восстановлено по косвенным признакам"
TXT_GONE = "Серия не найдена — возможно, её удалили"
DASH = "—"

RUN_RESULT = {
    "completed": "Завершён", "failed": "Сбой", "cancelled": "Остановлен", "canceled": "Остановлен",
    "interrupted": "Прерван", "running": "Идёт",
}
RUN_MODE = {
    "full": "Полный", "resume": "Продолжение", "from_step": "С выбранного шага", "single_step": "Один шаг",
}
NOTE_LABELS = {
    "recovered_after_failure": "Восстановлено после сбоя",
    "run_file_conflict": "Файл прогона расходится с базой",
    "bypassed": "Обойдён другими шагами",
    "flag_off": "Выключен настройкой",
    "interrupted": "Прерван",
    "artifacts_only": "Определён только по файлам",
    "condition_unresolved": "Условие не определено",
}
STAGE_TONES = {"done": "success", "running": "running", "problems": "error", "partial": "text_primary"}
SCREEN_IDS = ("generation", "shots", "movie", "overview")


@dataclass(frozen=True)
class StageCell:
    id: str
    title: str
    state_title: str
    progress: str
    current: bool
    tone: str


@dataclass(frozen=True)
class Counter:
    key: str
    label: str
    value: str
    sub: str = ""
    note: str = ""
    target: str = ""        # раздел оболочки при щелчке; "" - не кликается
    to_warnings: bool = False


@dataclass(frozen=True)
class StepRow:
    title: str
    state_title: str
    duration: str
    note: str
    tone: str


@dataclass(frozen=True)
class NextAction:
    title: str
    detail: str
    severity: str
    screen: str
    button: str = TXT_GO


@dataclass(frozen=True)
class FinalVideo:
    exists: bool
    path: str = ""
    meta: str = ""
    warning: str = ""


@dataclass(frozen=True)
class OverviewView:
    mode: str = "no_project"            # no_project | loading | ready | error
    title: str = ""
    run_text: str = ""
    run_tone: str = "text_muted"
    next_action: Optional[NextAction] = None
    empty: bool = False
    stages: Tuple[StageCell, ...] = ()
    counters: Tuple[Counter, ...] = ()
    steps: Tuple[StepRow, ...] = ()
    steps_title: str = TXT_STEPS
    final: FinalVideo = field(default_factory=lambda: FinalVideo(False))
    warnings_text: str = ""
    last_run: Tuple[Tuple[str, str], ...] = ()
    last_run_error: str = ""
    last_run_note: str = ""
    error: str = ""                     # текст баннера ошибки
    gone: bool = False                  # проект не найден
    stale: bool = False                 # показаны прежние данные


def no_project_view() -> OverviewView:
    return OverviewView(mode="no_project")


def loading_view() -> OverviewView:
    return OverviewView(mode="loading")


def error_text(error: Optional[dict]) -> str:
    reason = str((error or {}).get("message") or "ошибка").rstrip(".")
    return f"Не удалось прочитать состояние серии: {reason}."


def with_error(previous: Optional[OverviewView], error: Optional[dict]) -> OverviewView:
    """Баннер ошибки; прежние данные остаются и помечаются устаревшими."""
    gone = (error or {}).get("code") == "not_found"
    text = TXT_GONE if gone else error_text(error)
    if previous is not None and previous.mode in ("ready", "error") and not previous.gone:
        return replace(previous, mode="ready", error=text, stale=True, gone=gone)
    return OverviewView(mode="error", error=text, gone=gone)


# --- Форматирование ----------------------------------------------------------------
def format_duration(seconds) -> str:
    """«42 мин», «2 ч 5 мин»; меньше минуты - секунды."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return DASH
    if seconds < 60:
        return f"{int(round(seconds))} с"
    total = int(round(seconds / 60))
    hours, minutes = divmod(total, 60)
    if hours == 0:
        return f"{minutes} мин"
    return f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"


def format_clock(seconds) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        return DASH
    total = int(round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_maybe(value) -> str:
    return DASH if value is None or value == "" else str(value)


def format_moment(iso) -> str:
    """Момент из статуса: строка с поясом (Z, +03:00) переводится в местное время."""
    if not isinstance(iso, str):
        return DASH
    if re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", iso):
        try:
            moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return DASH
        return moment.strftime("%d.%m.%Y %H:%M")
    return format_datetime(iso)


# --- Сборка --------------------------------------------------------------------------------
def is_empty_project(status: dict) -> bool:
    action = status.get("next_action") or {}
    steps = status.get("steps") or []
    return action.get("code") == "empty_project" or (
        status.get("last_run") is None and all(s.get("state") == "not_run" for s in steps))


def stage_cells(status: dict) -> Tuple[StageCell, ...]:
    stage = status.get("stage") or {}
    current = stage.get("current")
    cells: List[StageCell] = []
    for item in stage.get("items") or []:
        total = item.get("total") or 0
        cells.append(StageCell(
            id=str(item.get("id")), title=str(item.get("title") or ""),
            state_title=str(item.get("state_title") or ""),
            progress=f"{item.get('done', 0)}/{total}" if total > 0 else "",
            current=current == item.get("id"), tone=STAGE_TONES.get(item.get("state"), "text_muted")))
    finished = current == "done"
    cells.append(StageCell("done", TXT_DONE_STAGE, "Готов" if finished else "Не начат", "", finished,
                           "success" if finished else "text_muted"))
    return tuple(cells)


def counters(status: dict) -> Tuple[Counter, ...]:
    shots = status.get("shots") or {}
    out: List[Counter] = []
    for key, label in (("total", "Шотов"), ("ready", "Готово"), ("stale", "Устарело"),
                       ("errors", "Ошибки"), ("approved", "Одобрено")):
        note = TXT_NO_STALE if key == "stale" and shots.get("ready_basis") == "files" else ""
        out.append(Counter(key, label, format_maybe(shots.get(key)), note=note, target="shots"))
    spend = status.get("spend") or {}
    notes = []
    if (spend.get("assumed_rub") or 0) > 0:
        notes.append(TXT_ASSUMED)
    if spend.get("other_currencies"):
        notes.append(TXT_OTHER_CURRENCY)
    budget = spend.get("budget_rub")
    out.append(Counter("spend", "Траты", format_rub(spend.get("total")) or DASH,
                       sub=f"из {format_rub(budget)}" if isinstance(budget, (int, float)) else "",
                       note="; ".join(notes)))
    out.append(Counter("warnings", "Предупреждений", format_maybe((status.get("warnings") or {}).get("count")),
                       to_warnings=True))
    return tuple(out)


def step_rows(status: dict) -> Tuple[StepRow, ...]:
    rows = []
    for step in sorted(status.get("steps") or [], key=lambda s: s.get("order", 0)):
        display = step.get("display_state") or step.get("state")
        tone = {"done": "success", "running": "running", "failed": "error", "outdated": "warning"}.get(display, "text_muted")
        note = NOTE_LABELS.get(step.get("note_code") or "", step.get("note_code") or "")
        if step.get("confidence") == "low" and not note:
            note = TXT_LOW_CONFIDENCE
        duration = step.get("duration_seconds")
        rows.append(StepRow(str(step.get("title") or step.get("id")), str(step.get("state_title") or display or ""),
                            DASH if duration is None else format_duration(duration), note, tone))
    return tuple(rows)


def final_video(status: dict) -> FinalVideo:
    video = status.get("final_video") or {}
    if not video.get("exists"):
        return FinalVideo(False)
    parts = []
    if video.get("duration_seconds") is not None:
        parts.append(format_clock(video["duration_seconds"]))
    if video.get("height") is not None:
        parts.append(f"{video['height']}p")
    if video.get("size_bytes") is not None:
        parts.append(format_bytes(video["size_bytes"]))
    warnings = []
    if video.get("older_than_shots"):
        warnings.append(TXT_FINAL_OLDER)
    if video.get("review_passed") is False:
        warnings.append(f"Проверка фильма: ошибок {video.get('review_errors') or 0}, "
                        f"замечаний {video.get('review_warnings') or 0}")
    return FinalVideo(True, str(video.get("path") or ""), " · ".join(parts), "; ".join(warnings))


def last_run_rows(status: dict) -> Tuple[Tuple[Tuple[str, str], ...], str]:
    run = status.get("last_run")
    if not run:
        return (), ""
    failed = run.get("steps_failed") or 0
    done = str(run.get("steps_done") or 0)
    rows = (
        ("Начат", format_moment(run.get("started_at"))),
        ("Режим", RUN_MODE.get(run.get("mode"), run.get("mode")) if run.get("mode") else DASH),
        ("Длительность", format_duration(run.get("duration_seconds"))),
        ("Итог", RUN_RESULT.get(run.get("status"), str(run.get("status") or DASH))),
        ("Шагов готово", f"{done}, с ошибкой {failed}" if failed > 0 else done),
    )
    return rows, str(run.get("error") or "")


def build_overview(status: dict) -> OverviewView:
    """Полная модель из ответа project_status."""
    project = status.get("project") or {}
    run = status.get("active_run") or {}
    state = run.get("state") or "idle"
    action = status.get("next_action") or {}
    target = action.get("target") or {}
    screen = target.get("screen") if target.get("screen") in SCREEN_IDS else "generation"
    rows, run_error = last_run_rows(status)
    count = (status.get("warnings") or {}).get("count")
    incomplete = len(status.get("incomplete_runs") or [])
    return OverviewView(
        mode="ready", title=str(project.get("title") or project.get("id") or ""),
        run_text=str(run.get("text") or ""), run_tone=TONE_BY_STATE.get(state, "text_muted"),
        next_action=NextAction(str(action.get("title") or ""), str(action.get("detail") or ""),
                               str(action.get("severity") or "info"), screen) if action else None,
        empty=is_empty_project(status), stages=stage_cells(status), counters=counters(status),
        steps=step_rows(status), final=final_video(status),
        warnings_text=TXT_NO_REPORTS if count is None else f"{TXT_WARNINGS}: {count}",
        last_run=rows, last_run_error=run_error,
        last_run_note=f"Есть незавершённые прогоны: {incomplete}" if incomplete > 0 else "")


def final_video_path(project_dir, final: FinalVideo):
    """Полный путь к файлу фильма или None."""
    if not final.exists or not final.path or project_dir is None:
        return None
    path = Path(final.path)
    return path if path.is_absolute() else Path(project_dir) / path
