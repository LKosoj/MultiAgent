"""Состояние проекта storybook: этапы, шаги, прогоны, расходы, следующее действие.

Собирает картину из нескольких мест (база прогонов, файлы `run_<шаг>.json`,
файлы результатов, задачи видеопровайдера, замок проекта) и отдаёт один словарь.
Только читает; ничего не создаёт в проекте (ни `.studio/`, ни `.pipeline.lock`).
Проект огромный, поэтому читаются маленькие куски файлов, а разобранное
запоминается, пока файлы не изменились.
"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from custom_tools.storybook.project_paths import storybook_projects_root

from .capabilities import capability
from .errors import StudioError, success
from .labels import step_title
from .paths import project_dir

try:  # на Windows замка через fcntl нет: считаем, что замок свободен
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
_PIPELINE_PATH = Path(__file__).resolve().parents[3] / "workflow_pipelines" / "storybook_pipeline.yaml"

STATUS_VERSION = 1
_SURFACES = ("", "tk", "react")
_SURFACE_NAMES = {"tk": "Tk", "react": "React"}

# ---------------------------------------------------------------------------
# Константы: этапы, необязательные шаги, маркеры, ключевые артефакты
# ---------------------------------------------------------------------------

# (id, название, шаги). «Проверка перед видео» слита с «Видео» (решение Ф3-3).
STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("story", "Сюжет", ("brief_from_prompt", "init_project", "story_planner", "bible_builder",
                        "style_keeper", "story_writer", "story_editor", "prompt_engineer")),
    ("book", "Книга", ("items_builder", "artist_batch", "assemble_md", "md_to_pdf")),
    ("screenplay", "Сценарий, речь и кадры", ("screenplay_generator", "storybook_continuity_plan",
                                              "storybook_speech", "screenplay_shots_generator", "shots_prompt_qa")),
    ("blockout", "Болванка", ("blockout_scene_builder", "blockout_renderer", "blockout_preview")),
    ("images", "Картинки", ("character_reference_states", "storybook_video_plan",
                            "background_reference_plan", "background_references",
                            "artist_batch_shots", "storybook_continuity_images_review")),
    ("video", "Видео", ("storybook_video_preflight", "storybook_video_delivery_promise",
                        "video_generator", "storybook_continuity_videos_review")),
    ("sound", "Музыка и субтитры", ("storybook_audio_subtitle", "music_planner", "storybook_music_generator",
                       "storybook_sfx_generator")),
    ("montage", "Монтаж", ("storybook_photo_montage", "montage_assembler",
                           "storybook_video_decision_log")),
)
DONE_STAGE = ("done", "Готово")

# не мешают «Готово» и не предлагаются как следующее действие
OPTIONAL_STEPS = frozenset({
    "blockout_scene_builder", "blockout_renderer", "blockout_preview", "md_to_pdf",
    "assemble_md", "music_planner", "storybook_music_generator", "storybook_sfx_generator",
    "storybook_photo_montage", "storybook_continuity_images_review",
    "storybook_continuity_videos_review", "storybook_video_decision_log",
})

# шаг -> файлы-результаты (основа: PipelineRunner._step_output_artifacts() + недостающие шаги).
# Файл, который указан у нескольких шагов, шаг не доказывает (см. _own_markers).
STEP_MARKERS: dict[str, tuple[str, ...]] = {
    "brief_from_prompt": ("00_brief.json",),
    "init_project": ("00_brief.json",),
    "story_planner": ("10_synopsis/synopsis.json", "10_synopsis/beats.json"),
    "bible_builder": ("20_bible/characters.json", "20_bible/locations.json",
                      "20_bible/consistency_rules.json"),
    "style_keeper": ("30_style/style_text.json", "30_style/style_images.json"),
    "story_writer": ("20_story/story.json",),
    "story_editor": ("20_story/story.json",),
    "prompt_engineer": ("40_prompts",),
    "items_builder": ("50_items/items.json",),
    "artist_batch": ("50_images",),
    "assemble_md": ("90_md/book.md",),
    "md_to_pdf": ("95_pdf/book.pdf",),
    "screenplay_generator": ("91_screenplay/screenplay.json",),
    "storybook_continuity_plan": ("20_bible/continuity_plan.json",),
    "storybook_speech": ("98_audio/speech.json",),
    "screenplay_shots_generator": ("97_shots/shots.json",),
    "shots_prompt_qa": ("97_shots/shots.json",),
    "blockout_scene_builder": ("93_blockout/chains.json", "93_blockout/scene_spec.json",
                               "93_blockout/asset_map.json", "93_blockout/report.json"),
    "blockout_preview": ("93_blockout/preview/blockout_all.mp4",
                         "93_blockout/preview/contact_sheet.png"),
    "character_reference_states": ("97_shots/character_reference_states.json",),
    "storybook_video_plan": ("97_shots/video_plan.json",),
    "background_reference_plan": ("97_shots/background_references.json",),
    "background_references": ("97_shots/background_references.json",),
    "artist_batch_shots": ("97_shots/shots.json",),
    "storybook_continuity_images_review": ("97_shots/continuity_images_review.json",),
    "storybook_video_preflight": ("96_video_contract/provider_menu_summary.json",),
    "storybook_video_delivery_promise": ("96_video_contract/delivery_promise.json",),
    "video_generator": ("97_shots/provider_jobs.json",),
    "storybook_continuity_videos_review": ("97_shots/continuity_videos_review.json",),
    "storybook_audio_subtitle": ("98_audio/subtitles.srt", "98_audio/audio_manifest.json",
                                 "98_audio/cue_sheet.json"),
    "music_planner": ("98_audio/music_plan.json",),
    "storybook_music_generator": ("98_audio/audio_manifest.json", "98_audio/music_manifest.json"),
    "storybook_sfx_generator": ("98_audio/sfx_manifest.json", "98_audio/audio_manifest.json"),
    "storybook_photo_montage": ("99_final/photo_video.mp4",),
    "montage_assembler": ("99_final/final_video.mp4", "99_final/timeline.fcpxml",
                          "99_final/subtitles.srt", "99_final/manifest.json",
                          "99_final/final_review.json"),
    "storybook_video_decision_log": ("96_video_contract/decision_log.json",),
}

# id, подпись, путь. Подписи держим здесь (labels.py в волне 1 не трогаем).
KEY_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("brief", "Бриф", "00_brief.json"),
    ("synopsis", "Синопсис", "10_synopsis/synopsis.json"),
    ("characters", "Персонажи", "20_bible/characters.json"),
    ("style", "Стиль", "30_style/style_text.json"),
    ("story", "Текст истории", "20_story/story.json"),
    ("book_md", "Книга (Markdown)", "90_md/book.md"),
    ("book_pdf", "Книга (PDF)", "95_pdf/book.pdf"),
    ("screenplay", "Сценарий", "91_screenplay/screenplay.json"),
    ("shots", "Шоты", "97_shots/shots.json"),
    ("video_plan", "План видео", "97_shots/video_plan.json"),
    ("character_refs", "Эталоны героев", "97_shots/character_reference_states.json"),
    ("background_refs", "Эталоны фонов", "97_shots/background_references.json"),
    ("images_review", "Ревью кадров", "97_shots/continuity_images_review.json"),
    ("videos_review", "Ревью видео", "97_shots/continuity_videos_review.json"),
    ("delivery_promise", "Обещание результата", "96_video_contract/delivery_promise.json"),
    ("provider_jobs", "Задачи провайдера", "97_shots/provider_jobs.json"),
    ("audio", "Озвучка", "98_audio/audio_manifest.json"),
    ("subtitles", "Субтитры", "98_audio/subtitles.srt"),
    ("music", "Музыка", "98_audio/music_manifest.json"),
    ("sfx", "Звуковые эффекты", "98_audio/sfx_manifest.json"),
    ("final_video", "Финальное видео", "99_final/final_video.mp4"),
    ("final_review", "Итоговая проверка", "99_final/final_review.json"),
)
# эти JSON мы всё равно разбираем: не разобрался -> «invalid»
_PARSED_ARTIFACTS = {
    "97_shots/shots.json", "97_shots/provider_jobs.json",
    "99_final/final_review.json",
}

_SHOTS_JSON = "97_shots/shots.json"
_JOBS_JSON = "97_shots/provider_jobs.json"
_FINAL_MP4 = "99_final/final_video.mp4"
_FINAL_REVIEW = "99_final/final_review.json"
_RENDER_REPORT = "99_final/render_report.json"
_FINAL_RENDER = "99_final/final_video.render.json"
_PHOTO_VIDEO = "99_final/photo_video.mp4"
_BRIEF = "00_brief.json"
_RUN_SETTINGS = ".studio/run_settings.json"
_CONTROL = ".studio/control.json"
_STEP_RUNS = ".studio/step_runs.json"

STATE_TITLES = {
    "not_run": "Не выполнялся", "running": "Идёт", "done": "Готов", "failed": "Ошибка",
    "skipped": "Пропущен", "outdated": "Возможно устарел", "waiting": "Ждёт",
}
STAGE_STATE_TITLES = {
    "not_started": "Не начат", "partial": "Начат", "running": "Идёт", "done": "Готов",
    "problems": "Есть проблемы",
}

# Хуки к модулям других задач: (модуль, функция). Любого из них может не быть.
_HOOKS = {
    "warnings_count": ("reports", "warnings_count"),
    "stale_shots": ("shots", "stale_shots_summary"),
    "review_counts": ("review", "review_counts"),
    "run_progress": ("runs", "run_progress"),
}

_OUTDATED_GAP_SEC = 60
_RECOVER_GAP_SEC = 60
_SAME_RUN_GAP_SEC = 5
_TAIL_BYTES = 8192
_TAIL_BIG_BYTES = 65536
_HEAD_BYTES = 1024
_MAX_JSON_BYTES = 64 * 1024 * 1024
_STATIC_TTL_SEC = 30.0  # запас на файлы, которые растут без смены mtime папки
_PROJECT_CACHE_SIZE = 16
_FILE_CACHE_SIZE = 512
_DB_DAYS = 30
_DB_MAX_RUNS = 20
_DB_CONTEXT_ROWS = 500  # сколько последних записей смотрит поиск по context
_DB_BUDGET_SEC = 0.3
_DB_PROGRESS_OPS = 10000
_DB_TTL_ACTIVE_SEC = 2.0
_DB_TTL_IDLE_SEC = 15.0
_IN_FLIGHT_JOB = frozenset({"submitting", "submitted", "queued", "pending", "running", "polling",
                            "in_progress"})
_FAILED_JOB = frozenset({"failed", "download_failed"})

_LOCK = threading.RLock()
_PROJECT_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_FILE_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_LAST_GOOD: dict[tuple, tuple] = {}
_DB_CACHE: dict[tuple, tuple] = {}
_PIPELINE_CACHE: dict[str, Any] = {}


def _is_inside(path: Any, root: Path) -> bool:
    """Путь лежит в папке root (сравнение по частям пути, «demo2» не входит в «demo»)."""
    try:
        return Path(str(path)) == root or Path(str(path)).is_relative_to(root)
    except (ValueError, TypeError):
        return False


def _reset_cache_for_tests() -> None:
    with _LOCK:
        _PROJECT_CACHE.clear()
        _FILE_CACHE.clear()
        _LAST_GOOD.clear()
        _DB_CACHE.clear()
        _PIPELINE_CACHE.clear()


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------

def shot_key(scene: int, shot: int) -> str:
    """Ключ шота «сцена-шот», например «2-3» (общий договор с Ф6)."""
    return f"{int(scene)}-{int(shot)}"


def shot_dir_name(scene: int, shot: int) -> str:
    """Имя папки шота, например «scene_02_shot_03»."""
    return f"scene_{int(scene):02d}_shot_{int(shot):02d}"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _clean(text: Any, limit: int, root: Path | None = None) -> str | None:
    """Обрезать строку и заменить абсолютный путь проекта на «.»."""
    if text is None:
        return None
    value = str(text)
    if root is not None:
        for variant in {str(root), str(root.resolve())}:
            if variant and variant != "/":
                value = value.replace(variant, ".")
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _stat(path: Path | str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _local_ts(parts: list[int]) -> float | None:
    """Наивное локальное время (как у datetime.now()) -> секунды Unix."""
    try:
        return datetime(*parts[:7]).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _parse_dt(value: Any) -> float | None:
    """ISO-строка из базы -> секунды Unix; наивное время считаем локальным."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _norm_status(value: Any) -> str:
    """'WorkflowStatus.FAILED' / "<StepStatus.COMPLETED: 'completed'>" / 'failed' -> 'failed'."""
    text = str(value or "").lower()
    for word in ("completed", "failed", "running", "paused", "cancelled", "pending", "skipped",
                 "retrying"):
        if word in text:
            return word
    return text.strip()


# ---------------------------------------------------------------------------
# Чтение файлов с кэшем по (mtime, size)
# ---------------------------------------------------------------------------

class _Unreadable(Exception):
    pass


def _load_json(path: str) -> Any:
    size = os.stat(path).st_size
    if size > _MAX_JSON_BYTES:
        raise _Unreadable("file too large")
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _load_tail(path: str, tail_bytes: int) -> tuple[str, str, int]:
    size = os.stat(path).st_size
    with open(path, "rb") as handle:
        if size <= _HEAD_BYTES + tail_bytes:
            text = handle.read().decode("utf-8", errors="replace")
            return text[:_HEAD_BYTES], text, size
        head = handle.read(_HEAD_BYTES).decode("utf-8", errors="replace")
        handle.seek(size - tail_bytes)
        tail = handle.read(tail_bytes).decode("utf-8", errors="replace")
    return head, tail, size


def _cached(kind: str, path: Path | str, extra: Any, loader) -> Any:
    """Общая обёртка: стат -> попадание в кэш или загрузка. Ошибки тоже запоминаются."""
    path = str(path)
    st = _stat(path)
    if st is None:
        raise FileNotFoundError(path)
    key = (kind, path, extra)
    with _LOCK:
        hit = _FILE_CACHE.get(key)
        if hit is not None and hit[0] == st:
            _FILE_CACHE.move_to_end(key)
            if isinstance(hit[1], Exception):
                raise hit[1]
            return hit[1]
    try:
        value: Any = loader()
    except FileNotFoundError:
        raise
    except Exception as exc:  # битый файл: запоминаем, пока он не изменился
        value = _Unreadable(f"{type(exc).__name__}")
    with _LOCK:
        _FILE_CACHE[key] = (st, value)
        _FILE_CACHE.move_to_end(key)
        while len(_FILE_CACHE) > _FILE_CACHE_SIZE:
            _FILE_CACHE.popitem(last=False)
    if isinstance(value, Exception):
        raise value
    return value


def _read_json_cached(path: Path | str) -> Any:
    """JSON-файл целиком; FileNotFoundError - нет файла, _Unreadable - не разобрался."""
    return _cached("json", path, None, lambda: _load_json(str(path)))


def _read_tail(path: Path | str, tail_bytes: int = _TAIL_BYTES) -> tuple[str, str, int]:
    """Первые 1 КБ и последние tail_bytes файла (маленький файл читается целиком)."""
    return _cached("tail", path, tail_bytes, lambda: _load_tail(str(path), tail_bytes))


def _read_studio_json(path: Path) -> dict:
    """Крошечный служебный файл `.studio/*.json`: терпимо, любая беда -> {}."""
    try:
        st = _stat(path)
        if st is None or st[1] > 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Описание пайплайна (YAML)
# ---------------------------------------------------------------------------

def _expr_names(expr: Any) -> list[str]:
    if not isinstance(expr, str):
        return []
    return re.findall(r"\{([^{}]+)\}", expr)


def _load_pipeline() -> dict:
    """Шаги в порядке YAML: id, depends_on, condition, enable, переопределённые артефакты."""
    st = _stat(_PIPELINE_PATH)
    with _LOCK:
        cached = _PIPELINE_CACHE.get("data")
        if cached is not None and _PIPELINE_CACHE.get("stat") == st:
            return cached
    raw = yaml.safe_load(_PIPELINE_PATH.read_text(encoding="utf-8")) or {}
    steps = []
    for index, step in enumerate(raw.get("steps") or [], start=1):
        params = step.get("tool_params") or {}
        meta = step.get("metadata") or {}
        steps.append({
            "id": step["id"],
            "order": index,
            "depends_on": list(step.get("depends_on") or []),
            "condition": step.get("condition") if isinstance(step.get("condition"), str) else None,
            "enable": params.get("enable") if isinstance(params.get("enable"), str) else None,
            "output_artifacts": list(meta.get("output_artifacts") or []),
        })
    data = {"steps": steps, "inputs": dict(raw.get("inputs") or {}), "stat": st}
    with _LOCK:
        _PIPELINE_CACHE["data"] = data
        _PIPELINE_CACHE["stat"] = st
    return data


def _step_markers(step: dict) -> tuple[str, ...]:
    """Файлы шага: переопределение из YAML главнее встроенной таблицы."""
    return tuple(step["output_artifacts"]) or STEP_MARKERS.get(step["id"], ())


def _own_markers(steps: list[dict]) -> dict[str, tuple[str, ...]]:
    """Только «собственные» файлы: те, что не указаны у нескольких шагов сразу."""
    counts: dict[str, int] = {}
    for step in steps:
        for rel in set(_step_markers(step)):
            counts[rel] = counts.get(rel, 0) + 1
    return {step["id"]: tuple(r for r in _step_markers(step) if counts[r] == 1) for step in steps}


# ---------------------------------------------------------------------------
# Условия: {флаг}, and, or, not, скобки (без eval), трёхзначная логика
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(\{[^{}]*\}|\(|\)|[A-Za-z_]+)")


class _BadExpr(Exception):
    pass


def _tokenize(expr: str) -> list[str]:
    tokens, pos = [], 0
    text = expr.rstrip()
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise _BadExpr
        tokens.append(match.group(1))
        pos = match.end()
    return tokens


def _eval_condition(expr: Any, flags: dict[str, bool | None]) -> bool | None:
    """True/False, либо None, если однозначно не вычислить. Мусор -> None."""
    if not isinstance(expr, str) or not expr.strip():
        return True
    try:
        tokens = _tokenize(expr)
    except _BadExpr:
        return None
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise _BadExpr
        pos += 1
        return tokens[pos - 1]

    def parse_or() -> bool | None:
        values = [parse_and()]
        while peek() == "or":
            take()
            values.append(parse_and())
        if True in values:
            return True
        return None if None in values else False

    def parse_and() -> bool | None:
        values = [parse_not()]
        while peek() == "and":
            take()
            values.append(parse_not())
        if False in values:
            return False
        return None if None in values else True

    def parse_not() -> bool | None:
        if peek() == "not":
            take()
            value = parse_not()
            return None if value is None else (not value)
        return parse_atom()

    def parse_atom() -> bool | None:
        token = take()
        if token == "(":
            value = parse_or()
            if take() != ")":
                raise _BadExpr
            return value
        if token == "true":
            return True
        if token == "false":
            return False
        if token.startswith("{"):
            name = token[1:-1].strip()
            if "." in name or not name:
                return None
            value = flags.get(name)
            return value if isinstance(value, bool) else None
        raise _BadExpr

    try:
        result = parse_or()
        if pos != len(tokens):
            return None
    except _BadExpr:
        return None
    return result


def _bool_in(mapping: Any, name: str) -> bool | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(name)
    return value if isinstance(value, bool) else None


def _resolve_flags(names: list[str], yaml_inputs: dict, brief: dict, settings: dict,
                   db_vars: dict) -> dict[str, tuple[bool | None, str]]:
    """Флаг по убыванию приоритета: run_settings > база > бриф > значение в YAML."""
    result: dict[str, tuple[bool | None, str]] = {}
    for name in names:
        value, source = None, "unknown"
        for src, mapping in (
            ("run_settings", settings),
            ("run_settings", settings.get("inputs") if isinstance(settings, dict) else None),
            ("run_settings", settings.get("flags") if isinstance(settings, dict) else None),
            ("db", db_vars), ("brief", brief), ("default", yaml_inputs),
        ):
            found = _bool_in(mapping, name)
            if found is not None:
                value, source = found, src
                break
        result[name] = (value, source)
    return result


# ---------------------------------------------------------------------------
# Файл run_<шаг>.json: Python-repr в строке, читаем только голову и хвост
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"start_time=(?:datetime\.datetime\(([^)]*)\)|None), "
    r"end_time=(?:datetime\.datetime\(([^)]*)\)|None), duration_seconds=([\d.]+|None)")
_ERROR_RE = re.compile(
    r"error=(None|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\s*,\s*start_time=", re.DOTALL)
_OUTPUT_SUCCESS_RE = re.compile(r'"output"\s*:\s*\{\s*"status"\s*:\s*"success"')


def _dt_parts(body: str | None) -> list[int] | None:
    if not body:
        return None
    body = body.split("tzinfo")[0]
    nums = [int(x) for x in re.findall(r"\d+", body)]
    return nums[:7] if len(nums) >= 3 else None


def _parse_run_text(head: str, tail: str) -> dict | None:
    """-> {ok, error, started, finished, duration, conflict} или None, если формат не узнан."""
    error_found = _ERROR_RE.findall(tail)
    time_found = _TIME_RE.findall(tail)
    if not error_found and not time_found:
        return None
    ok, error_text = True, None
    if error_found:
        raw = error_found[-1]
        if raw != "None":
            ok = False
            try:
                error_text = str(ast.literal_eval(raw))
            except (ValueError, SyntaxError):
                error_text = raw.strip("'\"")
    started = finished = duration = None
    if time_found:
        start_body, end_body, dur = time_found[-1]
        sp, ep = _dt_parts(start_body), _dt_parts(end_body)
        started = _local_ts(sp) if sp else None
        finished = _local_ts(ep) if ep else None
        try:
            duration = float(dur)
        except ValueError:
            duration = None
    return {
        "ok": ok, "error": error_text, "started": started, "finished": finished,
        "duration": duration,
        "conflict": (not ok) and bool(_OUTPUT_SUCCESS_RE.search(head)),
    }


def _read_run_file(root: Path, step_id: str) -> tuple[dict | None, str]:
    """-> (запись, 'ok'|'missing'|'error')."""
    path = root / f"run_{step_id}.json"
    try:
        head, tail, size = _read_tail(path)
        record = _parse_run_text(head, tail)
        if (record is None or record["finished"] is None) and size > _HEAD_BYTES + _TAIL_BYTES:
            head, tail, size = _read_tail(path, _TAIL_BIG_BYTES)
            record = _parse_run_text(head, tail)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, _Unreadable):
        return None, "error"
    if record is None:
        return None, "error"
    return record, "ok"


# ---------------------------------------------------------------------------
# Статическая часть: всё, что выводится из файлов проекта
# ---------------------------------------------------------------------------

def _dir_nonempty(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


def _marker_mtime(root: Path, rel: str) -> float | None:
    """mtime маркера; пустая папка не считается."""
    path = root / rel
    try:
        st = os.stat(path)
    except OSError:
        return None
    import stat as stat_mod
    if stat_mod.S_ISDIR(st.st_mode):
        return st.st_mtime if _dir_nonempty(path) else None
    return st.st_mtime if st.st_size > 0 else None


def _shot_scene_dirs(root: Path) -> list[tuple[str, int]]:
    """Папки шотов одним scandir; для подписи нужен только mtime папки."""
    out = []
    try:
        with os.scandir(root / "97_shots") as it:
            for entry in it:
                if entry.name.startswith("scene_"):
                    try:
                        out.append((entry.name, entry.stat().st_mtime_ns))
                    except OSError:
                        continue
    except OSError:
        pass
    return sorted(out)


def _signature(root: Path, pipeline: dict) -> tuple:
    rels: dict[str, None] = {}
    for step in pipeline["steps"]:
        rels[f"run_{step['id']}.json"] = None
        for rel in _step_markers(step):
            rels[rel] = None
    for _id, _title, rel in KEY_ARTIFACTS:
        rels[rel] = None
    for rel in (_SHOTS_JSON, _JOBS_JSON, _FINAL_REVIEW, _RENDER_REPORT, _FINAL_RENDER,
                _PHOTO_VIDEO, _BRIEF, _RUN_SETTINGS, _STEP_RUNS):
        rels[rel] = None
    parts = [(rel, _stat(root / rel)) for rel in rels]
    return (pipeline["stat"], tuple(parts), tuple(_shot_scene_dirs(root)))


def _pick_shot_rel(path: Any) -> str | None:
    """Путь из shots.json (от корня репозитория) -> путь внутри 97_shots проекта."""
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [p for p in PurePosixPath(path.replace("\\", "/")).parts if p not in ("/", "")]
    tail = parts[-2:]
    if not tail or any(p in ("..", ".") for p in tail):
        return None
    return "97_shots/" + "/".join(tail)


def _file_ok(root: Path, rel: str, sink: list[float]) -> bool:
    st = _stat(root / rel)
    if st is None or st[1] <= 0:
        return False
    sink.append(st[0] / 1e9)
    return True


def _jobs_by_shot(jobs: list) -> dict[str, dict]:
    """Последняя задача шота по timestamps.updated_at."""
    last: dict[str, tuple[str, dict]] = {}
    for job in jobs:
        if not isinstance(job, dict) or not job.get("shot_key"):
            continue
        stamps = job.get("timestamps") if isinstance(job.get("timestamps"), dict) else {}
        stamp = str(stamps.get("updated_at") or stamps.get("created_at") or "")
        key = str(job["shot_key"])
        if key not in last or stamp >= last[key][0]:
            last[key] = (stamp, job)
    return {key: value[1] for key, value in last.items()}


def _shots_static(root: Path) -> dict:
    """Счётчики шотов по shots.json и файлам на диске (пути проверяются через os.stat)."""
    block: dict[str, Any] = {
        "source": "missing", "partial": False, "total": None, "with_images": None,
        "with_videos": None, "video_expected": None, "ready": None, "errors": None,
        "stale": None, "approved": None, "review_pending": None, "redo": None,
        "ready_basis": "files", "items": None,
    }
    aux: dict[str, Any] = {"per_shot": [], "images_all": False, "videos_all": False,
                           "image_mtime": None, "video_mtime": None}
    key = (str(root), "shots")
    try:
        data = _read_json_cached(root / _SHOTS_JSON)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise _Unreadable("shape")
    except FileNotFoundError:
        return {"block": block, "aux": aux}
    except _Unreadable:
        with _LOCK:
            good = _LAST_GOOD.get(key)
        if good and time.time() - good[0] < 300:
            old = copy.deepcopy(good[1])
            old["block"]["partial"] = True
            return old
        block["source"] = "invalid"
        return {"block": block, "aux": aux}

    jobs = {}
    try:
        raw = _read_json_cached(root / _JOBS_JSON)
        job_list = raw.get("jobs") if isinstance(raw, dict) else raw
        jobs = _jobs_by_shot(job_list if isinstance(job_list, list) else [])
    except (FileNotFoundError, _Unreadable):
        pass

    groups: dict[tuple[int, int], dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        scene, shot = item.get("scene_number"), item.get("shot_number")
        if not isinstance(scene, int) or not isinstance(shot, int) or isinstance(scene, bool):
            continue
        group = groups.setdefault((scene, shot), {"images": [], "video": None, "expects": False})
        image = _pick_shot_rel(item.get("output_path"))
        if image and image not in group["images"]:
            group["images"].append(image)
        video = _pick_shot_rel(item.get("video_path"))
        if video:
            group["video"] = video
        if video or item.get("video_prompt"):
            group["expects"] = True

    image_mtimes: list[float] = []
    video_mtimes: list[float] = []
    per_shot = []
    for (scene, shot), group in sorted(groups.items()):
        has_images = bool(group["images"]) and all(
            _file_ok(root, rel, image_mtimes) for rel in group["images"])
        expects = group["expects"]
        has_video = bool(group["video"]) and _file_ok(root, group["video"], video_mtimes)
        skey = shot_key(scene, shot)
        last_job = jobs.get(skey)
        job_failed = bool(last_job) and str(last_job.get("status")) in _FAILED_JOB
        per_shot.append({
            "key": skey, "has_images": has_images, "expects_video": expects,
            "has_video": has_video, "error": bool(expects and not has_video and job_failed),
        })
    block.update(source="shots.json", total=len(per_shot))
    aux.update(
        per_shot=per_shot,
        images_all=bool(per_shot) and all(s["has_images"] for s in per_shot),
        videos_all=bool([s for s in per_shot if s["expects_video"]]) and all(
            s["has_video"] for s in per_shot if s["expects_video"]),
        image_mtime=max(image_mtimes) if image_mtimes else None,
        video_mtime=max(video_mtimes) if video_mtimes else None,
    )
    result = {"block": block, "aux": aux}
    with _LOCK:
        _LAST_GOOD[key] = (time.time(), copy.deepcopy(result))
    return result


def _shots_block(static_shots: dict, stale_keys: set[str] | None, review: dict | None) -> dict:
    """Счётчики шотов; хуки Ф6/Ф7 (устарело, ревью) подмешиваются на каждый вызов."""
    block = copy.deepcopy(static_shots["block"])
    per_shot = static_shots["aux"]["per_shot"]
    if block["source"] != "shots.json" and not block["partial"]:
        return block
    expected = [s for s in per_shot if s["expects_video"]]
    block["with_images"] = sum(1 for s in per_shot if s["has_images"])
    block["with_videos"] = sum(1 for s in expected if s["has_video"])
    block["video_expected"] = len(expected)
    block["errors"] = sum(1 for s in per_shot if s["error"])
    stale = stale_keys or set()
    block["ready"] = sum(
        1 for s in per_shot
        if s["has_images"] and (not s["expects_video"] or s["has_video"])
        and not s["error"] and s["key"] not in stale)
    if stale_keys is not None:
        block["stale"] = len(stale_keys)
        block["ready_basis"] = "files+stale"
    if review:
        block["approved"] = review.get("approved")
        block["review_pending"] = review.get("pending")
        block["redo"] = review.get("redo")
    return block


def _final_static(root: Path, shots_aux: dict) -> dict:
    st = _stat(root / _FINAL_MP4)
    block: dict[str, Any] = {
        "exists": bool(st and st[1] > 0), "path": _FINAL_MP4,
        "size_bytes": st[1] if st else None, "mtime": st[0] / 1e9 if st else None,
        "duration_seconds": None, "width": None, "height": None, "review_passed": None,
        "review_errors": None, "review_warnings": None,
        "photo_video_exists": bool((_stat(root / _PHOTO_VIDEO) or (0, 0))[1] > 0),
        "older_than_shots": False,
    }

    def read(rel: str) -> dict:
        try:
            data = _read_json_cached(root / rel)
        except (FileNotFoundError, _Unreadable):
            return {}
        return data if isinstance(data, dict) else {}

    review = read(_FINAL_REVIEW)
    if "passed" in review:
        block["review_passed"] = bool(review["passed"])
    for field, name in (("review_errors", "errors"), ("review_warnings", "warnings")):
        value = review.get(name)
        if isinstance(value, list):
            block[field] = len(value)
        elif _is_number(value):
            block[field] = int(value)
    checks = review.get("checks") if isinstance(review.get("checks"), dict) else {}
    duration = (checks.get("duration") or {}).get("actual_duration_seconds") \
        if isinstance(checks.get("duration"), dict) else None
    if not _is_number(duration):
        duration = read(_FINAL_RENDER).get("duration_seconds")
    block["duration_seconds"] = duration if _is_number(duration) else None
    probe = read(_RENDER_REPORT).get("final_probe")
    streams = probe.get("streams") if isinstance(probe, dict) else None
    if isinstance(streams, list) and streams and isinstance(streams[0], dict):
        for field in ("width", "height"):
            if _is_number(streams[0].get(field)):
                block[field] = int(streams[0][field])
    newest = shots_aux.get("video_mtime")
    if block["exists"] and newest is not None and block["mtime"] is not None:
        block["older_than_shots"] = block["mtime"] < newest
    return block


def _spend_static(root: Path, settings: dict) -> dict:
    block: dict[str, Any] = {
        "scope": "video_jobs", "currency": "RUB", "total": 0.0, "by_status": {}, "wasted": 0.0,
        "by_provider": {}, "other_currencies": {}, "jobs_total": 0, "jobs_failed": 0,
        "jobs_unknown_cost": 0, "assumed_rub": 0.0, "budget_rub": None, "budget_left_rub": None,
    }
    jobs: list = []
    try:
        raw = _read_json_cached(root / _JOBS_JSON)
        listed = raw.get("jobs") if isinstance(raw, dict) else raw
        jobs = listed if isinstance(listed, list) else []
    except (FileNotFoundError, _Unreadable):
        pass
    for job in jobs:
        if not isinstance(job, dict):
            continue
        block["jobs_total"] += 1
        status = str(job.get("status") or "")
        if status == "failed":
            block["jobs_failed"] += 1
        cost = job.get("cost")
        if not _is_number(cost):
            if status in _IN_FLIGHT_JOB:
                block["jobs_unknown_cost"] += 1
            continue
        currency = job.get("currency") or None
        if currency not in (None, "RUB"):
            if cost > 0:
                block["other_currencies"][str(currency)] = (
                    block["other_currencies"].get(str(currency), 0.0) + cost)
            continue
        if currency is None:  # в реальных проектах у части задач валюта не записана: это рубли
            block["assumed_rub"] += cost
        block["total"] += cost
        block["by_status"][status] = block["by_status"].get(status, 0.0) + cost
        if status == "stale":
            block["wasted"] += cost
        provider = str(job.get("provider") or "unknown")
        block["by_provider"][provider] = block["by_provider"].get(provider, 0.0) + cost
    budget = settings.get("budget_rub")
    if _is_number(budget) and budget > 0:
        block["budget_rub"] = budget
        block["budget_left_rub"] = budget - block["total"]
    return block


def _artifacts_static(root: Path) -> list[dict]:
    rows = []
    for art_id, title, rel in KEY_ARTIFACTS:
        st = _stat(root / rel)
        state = "missing"
        if st is not None and st[1] > 0:
            state = "present"
            if rel in _PARSED_ARTIFACTS and rel.endswith(".json"):
                try:
                    _read_json_cached(root / rel)
                except (FileNotFoundError, _Unreadable):
                    state = "invalid"
        rows.append({"id": art_id, "title": title, "rel": rel, "state": state,
                     "mtime": st[0] / 1e9 if st else None, "size_bytes": st[1] if st else None})
    return rows


def _build_static(project_id: str, root: Path, pipeline: dict) -> dict:
    steps = pipeline["steps"]
    records: dict[str, dict] = {}
    parsed = unparsed = 0
    for step in steps:
        record, state = _read_run_file(root, step["id"])
        if record is not None:
            records[step["id"]] = record
            parsed += 1
        elif state == "error":
            unparsed += 1
    own = _own_markers(steps)
    marker_times: dict[str, float | None] = {}
    step_marker_state: dict[str, list[dict]] = {}
    mt_cache: dict[str, float | None] = {}
    for step in steps:
        rows = []
        for rel in _step_markers(step):
            if rel not in mt_cache:
                mt_cache[rel] = _marker_mtime(root, rel)
            rows.append({"rel": rel, "state": "present" if mt_cache[rel] is not None else "missing",
                         "mtime": mt_cache[rel]})
        step_marker_state[step["id"]] = rows
        evidence = [mt_cache[r] for r in own[step["id"]] if mt_cache.get(r) is not None]
        marker_times[step["id"]] = max(evidence) if evidence else None
    any_marker = {sid: max((r["mtime"] for r in rows if r["mtime"] is not None), default=None)
                  for sid, rows in step_marker_state.items()}

    settings = _read_studio_json(root / _RUN_SETTINGS)
    brief: dict = {}
    try:
        data = _read_json_cached(root / _BRIEF)
        brief = data if isinstance(data, dict) else {}
    except (FileNotFoundError, _Unreadable):
        pass
    shots = _shots_static(root)
    final = _final_static(root, shots["aux"])
    artifacts = _artifacts_static(root)
    spend = _spend_static(root, settings)
    jobs_state = "ok"
    try:
        _read_json_cached(root / _JOBS_JSON)
    except FileNotFoundError:
        jobs_state = "missing"
    except _Unreadable:
        jobs_state = "error"
    return {
        "records": records, "marker_times": marker_times, "any_marker_times": any_marker,
        "step_markers": step_marker_state, "settings": settings, "brief": brief,
        "shots": shots, "final": final, "spend": spend, "artifacts": artifacts,
        "sources": {"run_files": parsed, "run_files_unparsed": unparsed,
                    "shots": shots["block"]["source"] if shots["block"]["source"] != "shots.json" else "ok",
                    "provider_jobs": jobs_state,
                    "step_runs": "present" if _stat(root / _STEP_RUNS) else "absent"},
        "built_at": time.time(),
    }


def _static(project_id: str, root: Path, pipeline: dict) -> tuple[dict, bool]:
    """Статическая часть из кэша, если файлы не изменились. -> (данные, попадание)."""
    key = str(root)
    sig = _signature(root, pipeline)
    with _LOCK:
        entry = _PROJECT_CACHE.get(key)
        if entry is not None and entry["sig"] == sig and time.time() - entry["data"]["built_at"] < _STATIC_TTL_SEC:
            _PROJECT_CACHE.move_to_end(key)
            return entry["data"], True
    data = _build_static(project_id, root, pipeline)
    with _LOCK:
        _PROJECT_CACHE[key] = {"sig": sig, "data": data}
        _PROJECT_CACHE.move_to_end(key)
        while len(_PROJECT_CACHE) > _PROJECT_CACHE_SIZE:
            _PROJECT_CACHE.popitem(last=False)
    return data, False


# ---------------------------------------------------------------------------
# База прогонов (только чтение, с таймаутом и кэшем по времени)
# ---------------------------------------------------------------------------

class _DbTimeout(Exception):
    pass


def _db_path() -> Path:
    return Path(os.environ.get("STORYBOOK_WORKFLOW_DB") or "workflow_state.db")


def _id_pattern(project_id: str, step_ids: list[str]) -> re.Pattern:
    pid = re.escape(project_id)
    steps = "|".join(re.escape(s) for s in step_ids) or "x"
    return re.compile(
        rf"^sbm_(?:partial_)?{pid}_[0-9a-f]{{8}}$|^sbm_single_{pid}_(?:{steps})_[0-9a-f]{{8}}$")


_PARTIAL_WID = re.compile(r"^sbm_partial_(?P<x>.+)_[0-9a-f]{8}$")


def _is_interrupt(exc: sqlite3.Error) -> bool:
    return "interrupt" in str(exc).lower()


def _project_runs(conn: sqlite3.Connection, project_id: str, step_ids: list[str],
                  flag_names: list[str], projects_dir: Path | None = None,
                  extend_budget=None) -> tuple[list[dict], bool]:
    """Прогоны проекта: префикс идентификатора `sbm_<id>_...` ИЛИ context.variables.project_id.

    Общее правило поиска (решение Ф3-1). Идентификатор сравнивается точно,
    без LIKE: «_» и «%» в id ничего не значат. Второй запрос (по context) смотрит
    только последние _DB_CONTEXT_ROWS записей и при таймауте не теряет найденное
    первым: возвращается (прогоны, неполно).
    """
    cutoff = datetime.fromtimestamp(time.time() - _DB_DAYS * 86400).isoformat()
    pattern = _id_pattern(project_id, step_ids)
    found: dict[str, str] = {}
    prefixes = [f"sbm_{project_id}_", f"sbm_partial_{project_id}_", f"sbm_single_{project_id}_"]
    where = " OR ".join("(workflow_id >= ? AND workflow_id < ?)" for _ in prefixes)
    args: list[Any] = [cutoff]
    for prefix in prefixes:
        args += [prefix, prefix + "￿"]
    for wid, stamp in conn.execute(
        f"SELECT workflow_id, MAX(timestamp) FROM workflow_checkpoints "
        f"WHERE timestamp >= ? AND ({where}) GROUP BY workflow_id", args):
        if pattern.match(wid):
            found[wid] = stamp
    # «sbm_partial_X_hex» можно прочитать как проект X (частичный прогон) и как проект
    # «partial_X». Если существуют оба, решает context.variables.project_id этой записи;
    # без контекста прогон не засчитывается никому.
    if projects_dir is not None:
        for wid in list(found):
            match = _PARTIAL_WID.match(wid)
            if not match or not ((projects_dir / match["x"]).is_dir()
                                 and (projects_dir / f"partial_{match['x']}").is_dir()):
                continue
            row = conn.execute(
                "SELECT CASE WHEN json_valid(context) THEN "
                "json_extract(context, '$.variables.project_id') END FROM workflow_checkpoints "
                "WHERE workflow_id = ? AND timestamp = ?", (wid, found[wid])).fetchone()
            if not row or row[0] != project_id:
                found.pop(wid)
    incomplete = False
    try:
        for wid, stamp in conn.execute(
            "SELECT workflow_id, MAX(timestamp) FROM (SELECT workflow_id, timestamp, context "
            "FROM workflow_checkpoints WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?) "
            "WHERE CASE WHEN json_valid(context) THEN json_extract(context, '$.variables.project_id') END = ? "
            "GROUP BY workflow_id", (cutoff, _DB_CONTEXT_ROWS, project_id)):
            found[wid] = stamp
        # Окно заполнено целиком: более старые прогоны, найденные только по
        # context, могли в него не попасть — честно помечаем историю неполной
        (window_rows,) = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM workflow_checkpoints WHERE timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT ?)", (cutoff, _DB_CONTEXT_ROWS + 1)).fetchone()
        if window_rows > _DB_CONTEXT_ROWS:
            incomplete = True
    except sqlite3.Error as exc:
        if not _is_interrupt(exc):
            raise
        incomplete = True
        if extend_budget is not None:
            extend_budget()  # остальное чтение (шаги найденных прогонов) получает свежий бюджет
    from custom_tools.storybook.project_paths import current_series_scope
    scope = current_series_scope()
    if scope and scope[0] == project_id:
        for wid, stamp in list(found.items()):
            row = conn.execute(
                "SELECT CASE WHEN json_valid(context) THEN json_extract(context, '$.variables.project_id') END, "
                "CASE WHEN json_valid(context) THEN json_extract(context, '$.variables.series_id') END "
                "FROM workflow_checkpoints WHERE workflow_id = ? AND timestamp = ?", (wid, stamp)).fetchone()
            if not row or tuple(row) != (project_id, scope[1]):
                found.pop(wid)
    ordered = sorted(found.items(), key=lambda kv: kv[1], reverse=True)[:_DB_MAX_RUNS]

    runs = []
    for index, (wid, last_stamp) in enumerate(ordered):
        row = conn.execute(
            "SELECT status, current_step, resumable, CASE WHEN json_valid(metadata) THEN "
            "json_extract(metadata, '$.error') END, (SELECT MIN(timestamp) FROM workflow_checkpoints "
            "WHERE workflow_id = ?) FROM workflow_checkpoints WHERE workflow_id = ? AND timestamp = ?",
            (wid, wid, last_stamp)).fetchone()
        if row is None:
            continue
        steps: dict[str, dict] = {}
        try:
            for key, status, start, end, dur, err in conn.execute(
                "SELECT j.key, json_extract(j.value, '$.status'), json_extract(j.value, '$.start_time'), "
                "json_extract(j.value, '$.end_time'), json_extract(j.value, '$.duration_seconds'), "
                "substr(json_extract(j.value, '$.error'), 1, 400) "
                "FROM workflow_checkpoints w, json_each(w.step_results) j "
                "WHERE w.workflow_id = ? AND w.timestamp = ?", (wid, last_stamp)):
                steps[str(key)] = {
                    "status": _norm_status(status), "started": _parse_dt(start),
                    "finished": _parse_dt(end),
                    "duration": float(dur) if _is_number(dur) else None,
                    "error": err if isinstance(err, str) and err else None,
                }
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise
            steps = {}  # step_results не JSON: у прогона просто нет данных по шагам
        variables: dict[str, Any] = {}
        if index == 0 and flag_names:
            cols = ", ".join(
                f"CASE WHEN json_valid(context) THEN json_extract(context, '$.variables.{n}') END"
                for n in flag_names)
            vrow = conn.execute(
                f"SELECT {cols} FROM workflow_checkpoints WHERE workflow_id = ? AND timestamp = ?",
                (wid, last_stamp)).fetchone()
            if vrow:
                variables = {n: bool(v) if v in (0, 1) and v is not None else None
                             for n, v in zip(flag_names, vrow)}
        runs.append({
            "workflow_id": wid, "status": _norm_status(row[0]), "current_step": row[1],
            "resumable": bool(row[2]), "error": row[3] if isinstance(row[3], str) else None,
            "started": _parse_dt(row[4]), "finished": _parse_dt(last_stamp), "steps": steps,
            "variables": variables,
        })
    return runs, incomplete


def _summarize_runs(runs: list[dict], incomplete: bool = False) -> dict:
    """E1: последняя запись по каждому шагу (максимальный end_time среди прогонов)."""
    best: dict[str, dict] = {}
    for run in runs:
        for sid, info in run["steps"].items():
            if info["status"] not in ("completed", "failed"):
                continue
            finished = info["finished"]
            current = best.get(sid)
            if current is None or (finished or 0) > (current["finished"] or 0):
                best[sid] = {
                    "ok": info["status"] == "completed", "error": info["error"],
                    "started": info["started"], "finished": finished,
                    "duration": info["duration"],
                }
    latest = runs[0] if runs else None
    return {"available": True, "reason": "partial" if incomplete else "ok", "runs": runs, "steps": best, "latest": latest,
            "variables": (latest or {}).get("variables") or {}}


def _read_runs_db(project_id: str, step_ids: list[str], flag_names: list[str],
                  locked: bool, allow_connect: bool = True, projects_dir: Path | None = None) -> dict:
    path = _db_path()
    from custom_tools.storybook.project_paths import current_series_scope
    key = (str(path.resolve()) if path.exists() else str(path), project_id, current_series_scope())
    ttl = _DB_TTL_ACTIVE_SEC if locked else _DB_TTL_IDLE_SEC
    with _LOCK:
        hit = _DB_CACHE.get(key)
    if hit is not None and (not allow_connect or time.time() - hit[0] < ttl):
        return hit[1]
    if not allow_connect:
        return {"available": False, "reason": "not_read", "runs": [], "steps": {}, "latest": None,
                "variables": {}}
    result: dict
    if not path.is_file():
        result = {"available": False, "reason": "no_file", "runs": [], "steps": {}, "latest": None,
                  "variables": {}}
    else:
        deadline = [time.monotonic() + _DB_BUDGET_SEC]

        def extend_budget() -> None:
            deadline[0] = time.monotonic() + _DB_BUDGET_SEC

        conn = None
        try:
            uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline[0] else 0, _DB_PROGRESS_OPS)
            result = _summarize_runs(*_project_runs(conn, project_id, step_ids, flag_names,
                                                    projects_dir, extend_budget))
        except sqlite3.Error as exc:
            reason = "timeout" if _is_interrupt(exc) else "error"
            result = {"available": False, "reason": reason, "runs": [], "steps": {}, "latest": None,
                      "variables": {}}
        finally:
            if conn is not None:
                conn.close()
    with _LOCK:
        _DB_CACHE[key] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# Замок проекта, управление, хуки
# ---------------------------------------------------------------------------

def pipeline_lock_held(project_id: str) -> bool:
    """Держит ли кто-то замок прогона проекта (проверка только чтением, файл не создаётся).

    Принятое ограничение: между проверкой и действием замок может смениться.
    Повторный захват замка при запуске прогона делает Ф4; ответу этой функции
    нельзя доверять как защите от гонки.
    """
    return _probe_lock(project_dir(project_id))


def _probe_lock(root: Path) -> bool:
    """Занят ли замок проекта. Только чтение: файл не создаётся, замок сразу отпускается."""
    if fcntl is None:
        return False
    try:
        fd = os.open(root / ".pipeline.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _call_hook(name: str, project_id: str) -> tuple[Any, str | None]:
    """Необязательная функция из модуля другой задачи -> (значение, ошибка)."""
    module, func = _HOOKS[name]
    full = f"{_PACKAGE}.{module}"
    try:
        mod = importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return None, None
        logger.exception("studio status hook %s.%s: сломан импорт", module, func)
        return None, f"{module}.{func}: {type(exc).__name__}"
    except Exception as exc:
        logger.exception("studio status hook %s.%s: сломан импорт", module, func)
        return None, f"{module}.{func}: {type(exc).__name__}"
    fn = getattr(mod, func, None)
    if not callable(fn):
        return None, None
    try:
        return fn(project_id), None
    except Exception as exc:
        logger.exception("studio status hook %s.%s failed", module, func)
        return None, f"{module}.{func}: {type(exc).__name__}"


def _int_or_none(value: Any) -> int | None:
    return int(value) if _is_number(value) else None


def _hooks_data(project_id: str, locked: bool) -> tuple[dict, list[str]]:
    errors: list[str] = []
    out: dict[str, Any] = {}
    for name in ("warnings_count", "stale_shots", "review_counts") + (("run_progress",) if locked else ()):
        value, err = _call_hook(name, project_id)
        if err:
            errors.append(err)
        out[name] = value
    warnings = _int_or_none(out.get("warnings_count"))
    stale = out.get("stale_shots")
    stale_keys = None
    if isinstance(stale, dict) and isinstance(stale.get("keys"), list):
        stale_keys = {str(k) for k in stale["keys"]}
    elif isinstance(stale, dict) and _is_number(stale.get("count")):
        stale_keys = set()
    review = out.get("review_counts")
    review = ({k: _int_or_none(review.get(k)) for k in ("approved", "pending", "redo", "rejected")}
              if isinstance(review, dict) else None)
    progress = out.get("run_progress") if isinstance(out.get("run_progress"), dict) else None
    return {"warnings": warnings, "stale_keys": stale_keys,
            "stale_count": _int_or_none(stale.get("count")) if isinstance(stale, dict) else None,
            "review": review, "progress": progress}, errors


# ---------------------------------------------------------------------------
# Статусы шагов
# ---------------------------------------------------------------------------

def _pick_record(e1: dict | None, e2: dict | None) -> tuple[dict | None, str]:
    """Из записей базы (E1) и файла (E2) берём ту, где finished_at позже; равенство - E1."""
    if e1 is None and e2 is None:
        return None, "none"
    if e1 is None:
        return e2, "run_file"
    if e2 is None:
        return e1, "db"
    f1, f2 = e1["finished"], e2["finished"]
    if f1 is not None and f2 is not None and abs(f1 - f2) < _SAME_RUN_GAP_SEC:
        return e1, "db"
    if (f2 or 0) > (f1 or 0):
        return e2, "run_file"
    return e1, "db"


def _can_recover(step_id: str, record: dict, static: dict) -> bool:
    """Шаг записан как ошибочный, но потом результат дошёл до конца (ручной дозапуск)."""
    finished = record.get("finished")
    if finished is None:
        return False
    aux = static["shots"]["aux"]
    if step_id == "artist_batch_shots":
        mtime = aux["image_mtime"]
        return bool(aux["images_all"] and mtime and mtime > finished + _RECOVER_GAP_SEC)
    if step_id == "video_generator":
        mtimes = [m for m in (aux["video_mtime"], static["marker_times"].get(step_id)) if m]
        return bool(aux["videos_all"] and mtimes and max(mtimes) > finished + _RECOVER_GAP_SEC)
    if step_id == "montage_assembler":
        final = static["final"]
        return bool(final["exists"] and final["mtime"] and final["mtime"] > finished)
    return False


def _resolve_steps(pipeline: dict, static: dict, db: dict, locked: bool, active_step: str | None,
                   flags: dict, root: Path) -> list[dict]:
    steps = pipeline["steps"]
    by_id = {s["id"]: s for s in steps}
    latest = db.get("latest") if db.get("available") else None
    interrupted_step = None
    if not locked and latest and latest["status"] in ("running", "paused"):
        interrupted_step = latest.get("current_step")
    flag_values = {name: val for name, (val, _src) in flags.items()}
    out: dict[str, dict] = {}

    for step in steps:
        sid = step["id"]
        row: dict[str, Any] = {
            "id": sid, "order": step["order"], "title": step_title(sid), "stage": _stage_of(sid),
            "optional": sid in OPTIONAL_STEPS, "state": "not_run", "display_state": "not_run",
            "state_title": "", "source": "none", "confidence": "medium", "started_at": None,
            "finished_at": None, "duration_seconds": None, "error": None, "note_code": None,
            "outdated_hint": None, "depends_on": list(step["depends_on"]),
            "artifacts": [dict(a) for a in static["step_markers"].get(sid, [])],
            "_finished": None,
        }
        out[sid] = row
        record, source = _pick_record(db.get("steps", {}).get(sid) if db.get("available") else None,
                                      static["records"].get(sid))
        cond = _eval_condition(step["condition"], flag_values)
        enable = _eval_condition(step["enable"], flag_values)

        def fill(rec: dict, src: str) -> None:
            row["source"] = src
            row["started_at"] = _iso(rec.get("started"))
            row["finished_at"] = _iso(rec.get("finished"))
            row["_finished"] = rec.get("finished")
            row["duration_seconds"] = rec.get("duration")
            row["confidence"] = "high" if src == "db" else ("medium" if rec.get("finished") else "low")

        if locked and active_step == sid:
            row.update(state="running", source="db", confidence="high")
        elif interrupted_step == sid:
            row.update(state="failed", note_code="interrupted", source="db", confidence="medium",
                       error="Запуск прерван")
        elif record is not None:
            fill(record, source)
            if record["ok"]:
                row["state"] = "done"
            elif _can_recover(sid, record, static):
                row.update(state="done", note_code="recovered_after_failure", confidence="low")
            else:
                row["state"] = "failed"
                row["error"] = _clean(record.get("error") or "Шаг завершился ошибкой", 300, root)
                if record.get("conflict") and source == "run_file":
                    row["note_code"] = "run_file_conflict"
        elif static["marker_times"].get(sid) is not None:
            mtime = static["marker_times"][sid]
            row.update(state="done", source="artifacts", confidence="low",
                       note_code="artifacts_only", finished_at=_iso(mtime), _finished=mtime)
        elif cond is False or enable is False:
            names = _expr_names(step["condition"]) + _expr_names(step["enable"])
            sources = [flags.get(n, (None, "default"))[1] for n in names]
            row.update(state="skipped", note_code="flag_off", source="flag",
                       confidence="high" if any(s in ("run_settings", "db") for s in sources) else "medium")
        elif cond is None and any("." in n for n in _expr_names(step["condition"])):
            row["note_code"] = "condition_unresolved"

    dependents: dict[str, list[str]] = {}
    for step in steps:
        for dep in step["depends_on"]:
            dependents.setdefault(dep, []).append(step["id"])

    def has_done_descendant(start: str) -> bool:
        seen, stack = set(), list(dependents.get(start, []))
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            if out[sid]["state"] == "done":
                return True
            stack.extend(dependents.get(sid, []))
        return False

    pending = [sid for sid, row in out.items() if row["state"] == "not_run"]
    bypass = [sid for sid in pending if has_done_descendant(sid)]
    for sid in bypass:
        out[sid].update(state="skipped", source="downstream", note_code="bypassed", confidence="low")
    changed = True  # вперёд по цепочке: шаг без следов, чья зависимость обойдена, тоже не нужен
    while changed:
        changed = False
        for sid in pending:
            row = out[sid]
            if row["state"] == "not_run" and any(
                    out[d]["state"] == "skipped" and out[d]["note_code"] in ("bypassed", "flag_off")
                    for d in by_id[sid]["depends_on"] if d in out):
                row.update(state="skipped", source="downstream", note_code="bypassed",
                           confidence="low")
                changed = True

    for step in steps:  # подсказка «возможно устарел»: только подсказка, состояние не меняет
        row = out[step["id"]]
        if row["state"] != "done" or row["source"] not in ("db", "run_file") or row["_finished"] is None:
            continue
        latest_dep = None
        for dep in step["depends_on"]:
            drow = out.get(dep)
            if (drow and drow["state"] == "done" and drow["source"] in ("db", "run_file")
                    and drow["_finished"] is not None
                    and drow["_finished"] > row["_finished"] + _OUTDATED_GAP_SEC
                    and (latest_dep is None or drow["_finished"] > out[latest_dep]["_finished"])):
                latest_dep = dep
        if latest_dep:
            row["outdated_hint"] = {"since_step": latest_dep, "reliability": "low"}

    ordered = []
    for step in steps:
        row = out[step["id"]]
        row["display_state"] = row["state"]
        if row["outdated_hint"]:
            row["display_state"] = "outdated"
        elif locked and row["state"] == "not_run" and any(
                out[d]["state"] not in ("done", "skipped") for d in step["depends_on"] if d in out):
            row["display_state"] = "waiting"
        row["state_title"] = STATE_TITLES[row["display_state"]]
        row.pop("_finished", None)
        ordered.append(row)
    return ordered


def _stage_of(step_id: str) -> str | None:
    for stage_id, _title, members in STAGES:
        if step_id in members:
            return stage_id
    return None


def _stages(steps: list[dict]) -> tuple[str, list[dict]]:
    by_id = {s["id"]: s for s in steps}
    items = []
    for stage_id, title, members in STAGES:
        rows = [by_id[m] for m in members if m in by_id]
        relevant = [r for r in rows if not r["optional"]] or rows
        states = {r["state"] for r in rows}
        if "running" in states:
            state = "running"
        elif "failed" in states:
            state = "problems"
        elif all(r["state"] in ("done", "skipped") for r in relevant):
            state = "done"
        elif "done" not in states:
            state = "not_started"
        else:
            state = "partial"
        items.append({
            "id": stage_id, "title": title, "state": state, "state_title": STAGE_STATE_TITLES[state],
            "done": sum(1 for r in rows if r["state"] in ("done", "skipped")), "total": len(rows),
        })
    current = next((i["id"] for i in items if i["state"] != "done"), DONE_STAGE[0])
    return current, items


def _stage_title(stage_id: str) -> str:
    for sid, title, _m in STAGES:
        if sid == stage_id:
            return title
    return DONE_STAGE[1]


# ---------------------------------------------------------------------------
# Активный прогон
# ---------------------------------------------------------------------------

def _run_text(state: str, *, step: dict | None, total: int, percent: Any, surface_note: str | None,
              duration: float | None) -> str:
    where = f"шаг {step['index']} из {total}" if step and step.get("index") else None
    name = f" «{step['title']}»" if step and step.get("title") else ""
    if state == "idle":
        return "Прогон не запущен"
    if state == "running":
        if surface_note:
            return f"Идёт в другом приложении ({surface_note})" + (f", {where}" if where else "")
        if not where:
            return "Идёт: прогон запущен, шаг уточняется"
        tail = f", {int(percent)} %" if _is_number(percent) else ""
        return f"Идёт: {where}{name}{tail}"
    if state == "pause_requested":
        return "Пауза после текущего шага"
    if state == "paused":
        return "Прогон на паузе" + (f", {where}" if where else "")
    if state == "stopping":
        return "Остановка…"
    if state == "failed":
        return "Прогон остановился с ошибкой" + (f" на шаге{name}" if name else "")
    if state == "interrupted":
        return "Прогон прерван" + (f" на шаге{name}" if name else "")
    if state == "completed":
        minutes = max(1, int(round((duration or 0) / 60)))
        return f"Прогон завершён, {minutes} мин"
    return ""


def _active_run(db: dict, locked: bool, control: dict, control_exists: bool,
                progress: dict | None, surface: str, steps: list[dict]) -> dict:
    total = len(steps)
    order = {s["id"]: s for s in steps}
    latest = db.get("latest") if db.get("available") else None
    stale_control = control_exists and not locked
    pause = bool(control.get("pause_requested")) and not stale_control
    cancel = bool(control.get("cancel_requested")) and not stale_control

    step_id = None
    index = percent = None
    run_surface = None
    run_id = control.get("run_id") if not stale_control else None
    if locked and progress:
        step_id = progress.get("step_id")
        index = progress.get("step_index")
        percent = progress.get("percent")
        run_surface = progress.get("surface")
        run_id = progress.get("run_id") or run_id
    if locked and not step_id and latest and latest["status"] in ("running", "paused"):
        step_id = latest.get("current_step")
    if locked and not run_surface and not stale_control and isinstance(control.get("surface"), str):
        run_surface = control["surface"]
    if step_id and not _is_number(index) and step_id in order:
        index = order[step_id]["order"]
    step = ({"id": step_id, "title": step_title(step_id) if step_id else None, "index": index}
            if step_id else None)

    state = "idle"
    failed_step = None
    duration = None
    if locked:
        if cancel:
            state = "stopping"
        elif pause:
            state = "pause_requested"
        elif latest and latest["status"] == "paused":
            state = "paused"
        else:
            state = "running"
    elif latest and latest["status"] in ("running", "paused"):
        state = "interrupted"
        step = {"id": latest.get("current_step"), "index": None,
                "title": step_title(latest["current_step"]) if latest.get("current_step") else None}
    elif latest and latest["status"] == "failed":
        state = "failed"
        failed = next((sid for sid, info in latest["steps"].items() if info["status"] == "failed"), None)
        failed_step = failed or latest.get("current_step")
        step = {"id": failed_step, "index": order[failed_step]["order"] if failed_step in order else None,
                "title": step_title(failed_step) if failed_step else None}
    elif latest and latest["status"] == "completed":
        state = "completed"
        if latest["started"] and latest["finished"]:
            duration = latest["finished"] - latest["started"]

    note = None
    if state == "running" and surface in _SURFACE_NAMES and run_surface in _SURFACE_NAMES \
            and run_surface != surface:
        note = _SURFACE_NAMES[run_surface]
    return {
        "state": state, "locked": locked, "run_id": run_id,
        "workflow_id": latest["workflow_id"] if latest else None,
        "surface": run_surface if locked else None,
        "current_step": step["id"] if step else None,
        "step_index": step["index"] if step else None, "step_total": total,
        "percent": percent if _is_number(percent) else None,
        "pause_requested": pause, "cancel_requested": cancel, "stale_control": stale_control,
        "text": _run_text(state, step=step, total=total, percent=percent, surface_note=note,
                          duration=duration),
        "_step": step,
    }


def _last_run(db: dict, mode: str | None) -> dict | None:
    latest = db.get("latest") if db.get("available") else None
    if not latest:
        return None
    steps = latest["steps"].values()
    duration = None
    if latest["started"] and latest["finished"]:
        duration = max(0.0, latest["finished"] - latest["started"])
    return {
        "workflow_id": latest["workflow_id"], "status": latest["status"], "mode": mode,
        "started_at": _iso(latest["started"]), "finished_at": _iso(latest["finished"]),
        "duration_seconds": duration,
        "steps_done": sum(1 for s in steps if s["status"] == "completed"),
        "steps_failed": sum(1 for s in steps if s["status"] == "failed"),
        "error": _clean(latest["error"], 300),
    }


def _incomplete(db: dict) -> list[str]:
    if not db.get("available"):
        return []
    return [r["workflow_id"] for r in db["runs"]
            if r["resumable"] and r["status"] not in ("completed", "cancelled")]


# ---------------------------------------------------------------------------
# Следующее действие
# ---------------------------------------------------------------------------

def _act(code: str, title: str, screen: str, *, severity: str = "info", detail: str = "",
         filter: str | None = None, step_id: str | None = None, mode: str | None = None) -> dict:
    return {"code": code, "title": title, "detail": detail, "severity": severity,
            "target": {"screen": screen, "filter": filter, "step_id": step_id, "mode": mode}}


def _next_action(steps: list[dict], shots: dict, final: dict, warnings: int | None,
                 run: dict) -> dict:
    by_id = {s["id"]: s for s in steps}
    # 1
    if run["locked"]:
        n = run.get("step_index")
        text = f"Идёт прогон, шаг {n} из {run['step_total']}" if n else "Идёт прогон"
        return _act("run_active", text, "generation")
    # 2
    if run["state"] == "interrupted":
        step = run.get("_step") or {}
        name = f" на шаге «{step['title']}»" if step.get("title") else ""
        return _act("resume_interrupted", f"Прогон остановился{name} — продолжить", "generation",
                    severity="warning", step_id=step.get("id"), mode="resume")
    # 3
    if not any(s["state"] in ("done", "failed", "running") for s in steps):
        return _act("empty_project", "Проект пуст — запустите первый прогон", "generation",
                    mode="full")
    # 4
    failed = next((s for s in steps if s["state"] == "failed"), None)
    if failed:
        return _act("fix_failed_step", f"Шаг «{failed['title']}» завершился ошибкой — повторите",
                    "generation", severity="error", step_id=failed["id"], mode="from_step")
    # 5-8
    if (shots.get("stale") or 0) > 0:
        return _act("regenerate_stale", f"Кадры: {shots['stale']} устарели — перегенерировать",
                    "shots", severity="warning", filter="stale")
    if (shots.get("errors") or 0) > 0:
        return _act("fix_shot_errors", f"Видео: ошибки у {_plural_shots(shots['errors'])} — повторить",
                    "shots", severity="error", filter="errors")
    total, with_images = shots.get("total"), shots.get("with_images")
    if total is not None and with_images is not None and with_images < total:
        return _act("generate_images",
                    f"Кадры: нет картинок у {_plural_shots(total - with_images)} — сгенерировать",
                    "shots", severity="warning", filter="missing_images")
    expected, with_videos = shots.get("video_expected"), shots.get("with_videos")
    if expected is not None and with_videos is not None and with_videos < expected:
        return _act("generate_videos",
                    f"Видео: нет у {_plural_shots(expected - with_videos)} — сгенерировать",
                    "shots", severity="warning", filter="missing_videos")
    # 9-10
    if (shots.get("redo") or 0) > 0:
        return _act("redo_shots", f"Переделать: {_plural_shots(shots['redo'])}", "shots",
                    severity="warning", filter="redo")
    if (shots.get("review_pending") or 0) > 0:
        return _act("review_shots", f"Проверьте кадры: {shots['review_pending']} не просмотрены",
                    "shots", filter="pending")
    # 11
    for step in steps:
        if step["optional"] or step["state"] != "not_run":
            continue
        if all(by_id[d]["state"] in ("done", "skipped") for d in step["depends_on"] if d in by_id):
            return _act("run_next_step", f"Следующий шаг: «{step['title']}»", "generation",
                        step_id=step["id"], mode="from_step")
    # 12
    mandatory_ok = all(s["state"] in ("done", "skipped") for s in steps if not s["optional"])
    if mandatory_ok and (not final.get("exists") or final.get("older_than_shots")):
        all_approved = (shots.get("approved") is not None and shots.get("total")
                        and shots["approved"] == shots["total"])
        title = "Все кадры одобрены — собрать фильм" if all_approved else "Собрать фильм"
        return _act("build_film", title, "generation", step_id="montage_assembler",
                    mode="single_step")
    # 13
    if warnings:
        return _act("look_at_warnings", f"Есть предупреждения по отчётам: {warnings}", "overview",
                    severity="warning")
    return _act("done", "Проект готов — посмотрите фильм", "movie", severity="success")


def _plural_shots(n: int) -> str:
    tail, last2 = n % 10, n % 100
    if tail == 1 and last2 != 11:
        word = "шот"
    elif 2 <= tail <= 4 and not 12 <= last2 <= 14:
        word = "шота"
    else:
        word = "шотов"
    return f"{n} {word}"


# ---------------------------------------------------------------------------
# Сборка ответов
# ---------------------------------------------------------------------------

def _check_surface(surface: Any) -> str:
    if not isinstance(surface, str) or surface not in _SURFACES:
        raise StudioError("invalid_argument", "surface: допустимы tk, react или пустое значение")
    return surface


def _flag_names(pipeline: dict) -> list[str]:
    names: list[str] = []
    for step in pipeline["steps"]:
        for name in _expr_names(step["condition"]) + _expr_names(step["enable"]):
            if "." not in name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in names:
                names.append(name)
    return names


def _collect(project_id: str, root: Path, surface: str, refresh: bool, full: bool) -> dict:
    pipeline = _load_pipeline()
    if refresh:
        with _LOCK:
            _PROJECT_CACHE.pop(str(root), None)
            for key in [k for k in _FILE_CACHE if _is_inside(k[1], root)]:
                _FILE_CACHE.pop(key, None)
            _LAST_GOOD.pop((str(root), "shots"), None)
            for key in [k for k in _DB_CACHE if k[1] == project_id]:
                _DB_CACHE.pop(key, None)
    static, hit = _static(project_id, root, pipeline)
    step_ids = [s["id"] for s in pipeline["steps"]]
    flag_names = _flag_names(pipeline)

    locked = _probe_lock(root)
    control_path = root / _CONTROL
    control_exists = _stat(control_path) is not None
    control = _read_studio_json(control_path) if control_exists else {}

    cached_db = _read_runs_db(project_id, step_ids, flag_names, locked, allow_connect=False,
                                projects_dir=storybook_projects_root())
    cached_latest = cached_db.get("latest") if cached_db.get("available") else None
    unfinished = bool(cached_latest and cached_latest["status"] in ("running", "paused"))
    read_db = full or locked or unfinished or control_exists
    db = _read_runs_db(project_id, step_ids, flag_names, locked, projects_dir=storybook_projects_root()) if read_db else cached_db

    hooks, hook_errors = _hooks_data(project_id, locked) if full else ({
        "warnings": None, "stale_keys": None, "stale_count": None, "review": None,
        "progress": None}, [])
    if not full and locked:
        value, err = _call_hook("run_progress", project_id)
        hooks["progress"] = value if isinstance(value, dict) else None
        if err:
            hook_errors.append(err)

    flags = _resolve_flags(flag_names, pipeline["inputs"], static["brief"], static["settings"],
                           db.get("variables") or {})
    active_step = None
    if locked:
        active_step = (hooks["progress"] or {}).get("step_id")
        if not active_step and db.get("latest") and db["latest"]["status"] in ("running", "paused"):
            active_step = db["latest"].get("current_step")
    steps = _resolve_steps(pipeline, static, db, locked, active_step, flags, root)
    run = _active_run(db, locked, control, control_exists, hooks["progress"], surface, steps)
    current_stage, stage_items = _stages(steps)
    return {
        "pipeline": pipeline, "static": static, "hit": hit, "db": db, "steps": steps, "run": run,
        "hooks": hooks, "hook_errors": hook_errors, "flags": flags, "stage": current_stage,
        "stage_items": stage_items,
    }


def _project_block(project_id: str, static: dict) -> dict:
    brief = static["brief"]
    title = brief.get("title") if isinstance(brief.get("title"), str) and brief["title"].strip() else None
    language = brief.get("language") if isinstance(brief.get("language"), str) else None
    return {"id": project_id, "title": _clean(title or project_id, 200), "language": language}


def _public_run(run: dict) -> dict:
    return {k: v for k, v in run.items() if not k.startswith("_")}


@capability("project_status", title="Состояние проекта", access="read", category="project_state")
def project_status(project_id: str, surface: str = "", refresh: bool = False) -> dict:
    surface = _check_surface(surface)
    root = project_dir(project_id)
    c = _collect(project_id, root, surface, bool(refresh), full=True)
    static, hooks = c["static"], c["hooks"]
    shots = _shots_block(static["shots"], hooks["stale_keys"], hooks["review"])
    if hooks["stale_keys"] is not None and hooks["stale_count"] is not None:
        shots["stale"] = hooks["stale_count"]
    final = copy.deepcopy(static["final"])
    spend = copy.deepcopy(static["spend"])
    sources = dict(static["sources"])
    sources["runs_db"] = c["db"]["reason"]
    mode = (hooks["progress"] or {}).get("mode") if hooks["progress"] else None
    next_action = _next_action(c["steps"], shots, final, hooks["warnings"], c["run"])
    return success(
        version=STATUS_VERSION, generated_at=time.time(), cache={"static_hit": c["hit"]},
        project=_project_block(project_id, static),
        stage={"current": c["stage"], "title": _stage_title(c["stage"]), "items": c["stage_items"]},
        steps=c["steps"], artifacts=copy.deepcopy(static["artifacts"]), shots=shots,
        final_video=final, spend=spend,
        warnings={"count": hooks["warnings"], "source": "reports" if hooks["warnings"] is not None else None},
        active_run=_public_run(c["run"]), last_run=_last_run(c["db"], mode),
        incomplete_runs=_incomplete(c["db"]), next_action=next_action,
        flags={n: {"value": v, "source": s} for n, (v, s) in c["flags"].items()},
        sources=sources, hook_errors=c["hook_errors"],
    )


@capability("project_status_brief", title="Краткое состояние проекта", access="read",
            category="project_state")
def project_status_brief(project_id: str, surface: str = "") -> dict:
    surface = _check_surface(surface)
    root = project_dir(project_id)
    c = _collect(project_id, root, surface, False, full=False)
    static, run = c["static"], c["run"]
    spend = static["spend"]
    failed = next((s for s in c["steps"] if s["state"] == "failed"), None)
    step = run.get("_step")
    return success(
        generated_at=time.time(),
        project={"id": project_id, "title": _project_block(project_id, static)["title"]},
        stage={"id": c["stage"], "title": _stage_title(c["stage"])},
        run={
            "state": run["state"], "text": run["text"],
            "step": ({"id": step["id"], "title": step["title"], "index": step["index"],
                      "total": run["step_total"]} if step and step.get("id") else None),
            "percent": run["percent"], "pause_requested": run["pause_requested"],
            "cancel_requested": run["cancel_requested"], "surface": run["surface"],
        },
        spend={"total": spend["total"], "currency": spend["currency"], "budget_rub": spend["budget_rub"]},
        failed_step=({"id": failed["id"], "title": failed["title"]} if failed else None),
        final_video_exists=bool(static["final"]["exists"]),
    )
