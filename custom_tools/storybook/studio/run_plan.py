"""Планировщик прогона: чистая сборка плана без записи на диск (кроме чтения).

План отвечает на вопрос «что именно будет запущено и почему нельзя». Исполнитель
(`run_exec`) собирает определение воркфлоу по плану, а Tk-обёртки `PipelineRunner`
берут отсюда общие правила (артефакты шагов, поиск прогонов проекта).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import re
import sqlite3
import time
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook import project_paths

from .errors import StudioError
from .step_runs import JOBS_REL

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
PIPELINE_PATH = Path(__file__).resolve().parents[3] / "workflow_pipelines" / "storybook_pipeline.yaml"
PLAN_VERSION = 1
MODES = ("full", "from_step", "single_step", "resume", "regenerate_shots")
KINDS = ("images", "video")
KIND_STEPS = {"images": "artist_batch_shots", "video": "video_generator"}
MONTAGE_STEP = "montage_assembler"
SHOT_SCOPE_PROVIDERS = frozenset({"routerai", "aitunnel"})
DEFAULT_PROVIDER = "routerai"
DB_CONTEXT_ROWS = 500
BRIEF_INPUTS = ("pages_min", "pages_max", "words_per_page_min", "words_per_page_max", "language",
                "screenplay_time", "generate_screenplay", "generate_end_shots")
_PARTIAL_WID = re.compile(r"^sbm_partial_(?P<x>.+)_[0-9a-f]{8}$")
_VIDEO_STEP = "video_generator"
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _studio(name: str):
    return importlib.import_module(f"{_PACKAGE}.{name}")


# ------------------------------------------------------------------ артефакты шагов (общие с Tk)

def step_output_artifacts(workflow_def=None) -> dict[str, list[str]]:
    """Канонические артефакты шагов; metadata.output_artifacts в YAML главнее таблицы."""
    artifacts: dict[str, list[str]] = {
        "brief_from_prompt": ["00_brief.json"],
        "init_project": ["00_brief.json"],
        "story_planner": ["10_synopsis/synopsis.json", "10_synopsis/beats.json"],
        "bible_builder": ["20_bible/characters.json", "20_bible/locations.json",
                          "20_bible/consistency_rules.json"],
        "style_keeper": ["30_style/style_text.json", "30_style/style_images.json"],
        "story_writer": ["20_story/story.json"],
        "story_editor": ["20_story/story.json"],
        "prompt_engineer": ["40_prompts"],
        "items_builder": ["50_items/items.json"],
        "artist_batch": ["50_images"],
        "assemble_md": ["90_md/book.md"],
        "md_to_pdf": ["95_pdf/book.pdf"],
        "screenplay_generator": ["91_screenplay/screenplay.json"],
        "storybook_speech": ["98_audio/speech.json"],
        "storybook_video_plan": ["97_shots/video_plan.json"],
        "screenplay_shots_generator": ["97_shots/shots.json"],
        "blockout_scene_builder": ["93_blockout/chains.json", "93_blockout/scene_spec.json",
                                   "93_blockout/asset_map.json", "93_blockout/report.json"],
        # у blockout_renderer записи нет: его файлы лежат по путям вида scene_NN_shot_MM,
        # то есть зависят от состава шотов; отчёт числится за blockout_scene_builder
        "blockout_preview": ["93_blockout/preview/blockout_all.mp4", "93_blockout/preview/contact_sheet.png"],
        "shots_prompt_qa": ["97_shots/shots.json"],
        "artist_batch_shots": ["97_shots/shots.json"],
        "storybook_video_preflight": ["96_video_contract/provider_menu_summary.json"],
        "storybook_video_delivery_promise": ["96_video_contract/delivery_promise.json"],
        # маркером реально начатой видео-генерации служит durable-леджер задач
        "video_generator": [JOBS_REL],
        "storybook_audio_subtitle": ["98_audio/subtitles.srt", "98_audio/audio_manifest.json",
                                     "98_audio/cue_sheet.json"],
        "music_planner": ["98_audio/music_plan.json"],
        "storybook_music_generator": ["98_audio/audio_manifest.json", "98_audio/music_manifest.json"],
        "storybook_sfx_generator": ["98_audio/sfx_manifest.json", "98_audio/audio_manifest.json"],
        "storybook_photo_montage": ["99_final/photo_video.mp4"],
        "montage_assembler": ["99_final/final_video.mp4", "99_final/timeline.fcpxml", "99_final/subtitles.srt",
                              "99_final/manifest.json", "99_final/final_review.json"],
        "storybook_video_decision_log": ["96_video_contract/decision_log.json"],
    }
    if workflow_def is not None:
        for step in getattr(workflow_def, "steps", None) or []:
            override = (getattr(step, "metadata", None) or {}).get("output_artifacts")
            if override:
                artifacts[step.id] = list(override)
    return artifacts


def _overlaps(marker: str, item: str) -> bool:
    return item == marker or item.startswith(marker + "/") or marker.startswith(item + "/")


def _active_outputs(root: Path, inputs: dict | None = None) -> tuple[dict, list]:
    status = _studio("status")
    pipeline = status._load_pipeline()
    values = dict(pipeline["inputs"])
    values.update(_read_brief(root))
    try:
        data = json.loads((root / "series.json").read_text(encoding="utf-8"))
        values.update(data.get("settings") or {})
    except (OSError, ValueError):
        pass
    from custom_tools.storybook.project_paths import current_series_scope
    scope = current_series_scope()
    if scope:
        try:
            values.update(_studio("run_settings").settings_to_inputs(scope[0]))
        except StudioError:
            pass
    values.update(inputs or {})
    flags = {name: value for name, value in values.items() if isinstance(value, bool)}
    markers = status.STEP_MARKERS
    fallback = step_output_artifacts()
    outputs = {}
    for step in pipeline["steps"]:
        condition = step.get("condition") or ""
        # Шаги с условием от результата другого шага (например, редактор)
        # учитываются по факту запуска, а не как обязательные всегда.
        if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\.", condition):
            continue
        if not _gate_state(step, flags):
            continue
        paths = step.get("output_artifacts") or markers.get(step["id"]) or fallback.get(step["id"], [])
        outputs[step["id"]] = list(paths)
    outputs["items_builder"] = ["50_items/items.json"]
    outputs["storybook_sfx_generator"] = ["98_audio/sfx_manifest.json", "98_audio/audio_manifest.json"] if "storybook_sfx_generator" in outputs else []
    return outputs, pipeline["steps"]


def stale_results_for_plan(project_id: str, steps: list[str], *, inputs: dict | None = None,
                           explicit_step: str | None = None) -> dict:
    from .paths import project_dir
    root = project_dir(project_id)
    try:
        data = json.loads((root / "series.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    stale = list(data.get("stale_results", []))
    try:
        speech = json.loads((root / "98_audio/speech.json").read_text(encoding="utf-8"))
        if speech.get("status") == "stale" and "98_audio/speech.json" not in stale:
            stale.append("98_audio/speech.json")
    except (OSError, ValueError):
        pass
    outputs, _ = _active_outputs(root, inputs)
    # Явно вызванный условный шаг тоже является производителем.
    fallback = step_output_artifacts()
    for step in steps:
        if step not in outputs and step in fallback and step == explicit_step and step == "story_editor":
            outputs[step] = fallback[step]
    by_step = {step: [marker for marker in stale if any(_overlaps(marker, item) for item in outputs.get(step, []))]
               for step in steps}
    by_step = {step: markers for step, markers in by_step.items() if markers}
    affected = [marker for marker in stale if any(marker in markers for markers in by_step.values())]
    requirements = {marker: [step for step, paths in outputs.items()
                             if any(_overlaps(marker, item) and (root / item).exists() for item in paths)]
                    for marker in affected}
    for marker in affected:
        entry = data.get("stale_confirmations", {}).get(marker, {})
        revision = entry.get("revision", {}) if isinstance(entry, dict) else {}
        if revision and all(data.get(key) == value for key, value in revision.items()):
            for step in entry.get("requirements", []):
                if (step in outputs or step == "story_editor") and step not in requirements[marker]:
                    requirements[marker].append(step)
    return {"affected_results": affected, "stale_steps": by_step, "stale_requirements": requirements}


def step_ancestors(step_id: str, steps: list[dict]) -> set[str]:
    step_map = {step["id"]: step for step in steps}
    dependencies: set[str] = set()
    def walk(current):
        for dependency in step_map.get(current, {}).get("depends_on", []):
            if dependency not in dependencies:
                dependencies.add(dependency)
                walk(dependency)
    walk(step_id)
    return dependencies


def stale_upstream_for_step(project_id: str, step_id: str, *, inputs: dict | None = None) -> list[str]:
    """Устаревшие выходы зависимостей шага для плана и принятия фильма."""
    from .paths import project_dir
    root = project_dir(project_id)
    outputs, steps = _active_outputs(root, inputs)
    dependencies = step_ancestors(step_id, steps)
    try:
        data = json.loads((root / "series.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    stale = data.get("stale_results", [])
    return [marker for marker in stale
            if not (step_id == "montage_assembler" and (marker == "99_final" or marker.startswith("99_final/")))
            and any(_overlaps(marker, item) for dependency in dependencies for item in outputs.get(dependency, []))]


def _read_brief(project_path: Optional[Path]) -> dict:
    if project_path is None:
        return {}
    try:
        data = json.loads((Path(project_path) / "00_brief.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_generate_blockout_flag(project_path: Optional[Path]) -> bool:
    """generate_blockout из брифа; нет файла/ключа - False."""
    return bool(_read_brief(project_path).get("generate_blockout", False))


def read_blockout_allowed_durations(project_path: Optional[Path]) -> list[int]:
    raw = _read_brief(project_path).get("blockout_allowed_durations") or []
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for value in raw:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def collect_required_artifacts(workflow_def, start_step: str, project_path: Optional[Path] = None) -> list[str]:
    """Артефакты предков start_step; шаги под {generate_blockout} при выключенном слое не требуются."""
    return collect_required_for_steps(workflow_def, [start_step], project_path)


def collect_required_for_steps(workflow_def, step_ids: list[str], project_path: Optional[Path] = None) -> list[str]:
    """Артефакты внешних предков каждого шага набора (предки внутри набора появятся по ходу прогона)."""
    step_map = {step.id: step for step in workflow_def.steps}
    artifact_map = step_output_artifacts(workflow_def)
    inside = set(step_ids)
    gated = {step.id for step in workflow_def.steps
             if (step.tool_params or {}).get("enable") == "{generate_blockout}"}
    generate_blockout = read_generate_blockout_flag(project_path)
    visited: set[str] = set()
    visiting: set[str] = set()
    required: list[str] = []

    def walk(step_id: str) -> None:
        if step_id in inside or step_id in visited or step_id in visiting or step_id not in step_map:
            return
        visiting.add(step_id)
        for dep in step_map[step_id].depends_on:
            walk(dep)
        visiting.discard(step_id)
        visited.add(step_id)
        if step_id in gated and not generate_blockout:
            return
        required.extend(artifact_map.get(step_id, []))

    for sid in step_ids:
        step = step_map.get(sid)
        for dep in (step.depends_on if step is not None else []):
            walk(dep)
    seen: set[str] = set()
    return [a for a in required if not (a in seen or seen.add(a))]


# ------------------------------------------------------------------ поиск прогонов проекта

def like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _context_project(context_json: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(variables.project_id, session_id) из JSON контекста чекпоинта."""
    if not context_json:
        return None, None
    try:
        ctx = json.loads(context_json)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(ctx, dict):
        return None, None
    variables = ctx.get("variables") if isinstance(ctx.get("variables"), dict) else {}
    var_pid = variables.get("project_id")
    return (var_pid if isinstance(var_pid, str) else None,
            ctx.get("session_id") if isinstance(ctx.get("session_id"), str) else None)


def workflow_id_matches_project(workflow_id: str, project_id: str, context_json: Optional[str] = None) -> bool:
    """Прогон проекта: префикс `sbm_<id>_`, `sbm_partial_<id>_`, `sbm_single_<id>_<шаг>_` или context.

    Форматы: sbm_{pid}_{hex8}, sbm_partial_{pid}_{hex8}, sbm_single_{pid}_{step}_{hex8}.
    Неоднозначность `sbm_partial_X_hex` (проект X или `partial_X`) решает context.
    """
    if not workflow_id or not project_id:
        return False
    var_pid, session_id = _context_project(context_json)
    if workflow_id.startswith("sbm_"):
        rest = workflow_id[len("sbm_"):]
        single = rest.startswith("single_")
        stripped = rest
        for prefix in ("partial_", "single_"):
            if rest.startswith(prefix):
                stripped = rest[len(prefix):]
                break
        m = re.fullmatch(r"(.+)_[0-9a-f]{8}", stripped)
        if m:
            middle = m.group(1)
            if middle == project_id:
                return var_pid in (None, project_id) if var_pid else True
            if single and middle.startswith(project_id + "_"):
                return (session_id == project_id) if session_id else True
        m2 = re.fullmatch(r"(.+)_[0-9a-f]{8}", rest)
        if m2 and m2.group(1) == project_id:  # проект с именем partial_X
            return var_pid in (None, project_id) if var_pid else True
    return var_pid == project_id


def store_db_path(engine) -> Optional[str]:
    try:
        return str(engine.state_manager.store.db_path)
    except Exception:  # noqa: BLE001
        return None


RUNS_CACHE_TTL_S = 20.0
_RUNS_CACHE: dict[tuple, tuple[float, list[dict]]] = {}


def clear_runs_cache() -> None:
    _RUNS_CACHE.clear()


_HISTORY_MAX = 2000  # потолок догрузки истории прогонов для resume


def find_runs(engine, project_id: str, *, unfinished_only: bool = False, limit: int = 20,
              cached: bool = False, slim_singles: bool = False) -> list[dict]:
    """Последние чекпоинты прогонов проекта (по одному на workflow_id), новые первыми.

    `cached=True` - для частого опроса статуса: база чекпоинтов бывает огромной, поэтому ответ живёт ~20 с.
    `slim_singles=True` - глубокая выборка: у завершённых `sbm_single_*` вместо всего context отдаются только
    session_id и variables.project_id (больше сопоставлению с проектом не нужно)."""
    db = store_db_path(engine)
    if not db:
        return []
    if not cached:
        return _find_runs(db, project_id, unfinished_only, limit, slim_singles)
    from custom_tools.storybook.project_paths import current_series_scope
    key = (str(db), project_id, current_series_scope(), unfinished_only, limit, slim_singles)
    hit = _RUNS_CACHE.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < RUNS_CACHE_TTL_S:
        return [dict(r) for r in hit[1]]
    result = _find_runs(db, project_id, unfinished_only, limit, slim_singles)
    _RUNS_CACHE[key] = (now, result)
    return [dict(r) for r in result]


def _find_runs(db, project_id: str, unfinished_only: bool, limit: int, slim_singles: bool = False) -> list[dict]:
    status_filter = "AND wc.status NOT IN ('completed', 'cancelled')" if unfinished_only else ""
    single_done = "lower(wc.status) = 'completed' AND wc.workflow_id LIKE 'sbm\\_single\\_%' ESCAPE '\\'"

    def build(slim_ctx: str) -> str:
        ctx = f"CASE WHEN {single_done} THEN {slim_ctx} ELSE wc.context END" if slim_singles else "wc.context"
        return f"""
        SELECT wc.workflow_id, wc.status, wc.current_step, wc.completed_steps, wc.timestamp,
               wc.resumable, {ctx} AS context
        FROM workflow_checkpoints wc
        INNER JOIN (SELECT workflow_id, MAX(timestamp) AS max_ts FROM workflow_checkpoints
                    GROUP BY workflow_id) latest
          ON wc.workflow_id = latest.workflow_id AND wc.timestamp = latest.max_ts
        WHERE {{cond}} {status_filter}
        ORDER BY wc.timestamp DESC LIMIT ?"""

    sql = build("json_object('session_id', json_extract(wc.context, '$.session_id'), "
                "'variables', json_object('project_id', json_extract(wc.context, '$.variables.project_id'), 'series_id', json_extract(wc.context, '$.variables.series_id')))")
    sql_plain = build("NULL")  # без JSON1: сопоставление по workflow_id
    like = f"%{like_escape(project_id)}%"
    rows_limit = max(DB_CONTEXT_ROWS, limit)  # глубокая выборка не должна упираться в окно поиска по context
    queries = [
        (sql.format(cond="(wc.workflow_id LIKE ? ESCAPE '\\' OR (json_valid(wc.context) "
                         "AND json_extract(wc.context, '$.variables.project_id') = ?))"),
         (like, project_id, rows_limit)),
        (sql_plain.format(cond="wc.workflow_id LIKE ? ESCAPE '\\'"), (like, rows_limit)),  # без JSON1
    ]
    rows: list[Any] = []
    try:
        with sqlite3.connect(db, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            for query, args in queries:
                try:
                    rows = conn.execute(query, args).fetchall()
                    break
                except sqlite3.OperationalError:
                    continue
    except sqlite3.Error as exc:
        logger.warning("Поиск прогонов проекта %s не удался: %s", project_id, exc)
        return []
    result: list[dict] = []
    for row in rows:
        if not workflow_id_matches_project(row["workflow_id"], project_id, row["context"]):
            continue
        from custom_tools.storybook.project_paths import current_series_scope
        active = current_series_scope()
        if active and active[0] == project_id:
            try:
                context = json.loads(row["context"] or "{}")
                if context.get("variables", {}).get("series_id") != active[1]:
                    continue
            except (TypeError, ValueError, AttributeError):
                continue
        try:
            completed = json.loads(row["completed_steps"] or "[]")
        except ValueError:
            completed = []
        result.append({
            "workflow_id": row["workflow_id"], "status": str(row["status"] or "").lower(),
            "current_step": row["current_step"], "completed_steps": completed if isinstance(completed, list) else [],
            "timestamp": row["timestamp"], "resumable": bool(row["resumable"]),
        })
        if len(result) >= limit:
            break
    return result


def run_coro(coro):
    """Выполнить корутину из синхронного кода (в том числе из потока с живым циклом)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def load_checkpoint(engine, workflow_id: str):
    """Последний чекпоинт по workflow_id или None."""
    try:
        return run_coro(engine.state_manager.store.get_latest_checkpoint(workflow_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Чекпоинт %s не прочитан: %s", workflow_id, exc)
        return None


# ------------------------------------------------------------------ определение пайплайна

def load_definition():
    from workflow.models import WorkflowDefinition
    return WorkflowDefinition.from_yaml(PIPELINE_PATH)


def pipeline_hash() -> str:
    try:
        return hashlib.sha256(PIPELINE_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


_ENGINE_FAIL: dict = {}
ENGINE_RETRY_S = 30.0


def cached_engine():
    """Уже созданный движок или None (опросы статуса не должны поднимать движок)."""
    try:
        return _studio("run_exec")._ENGINE.get("engine")
    except Exception:  # noqa: BLE001
        return None


def engine_status() -> dict:
    """{available, error}: движок поднимается лениво; неудача не перепроверяется чаще раза в 30 с."""
    import time
    if cached_engine() is None:
        failed = _ENGINE_FAIL.get("at")
        if failed is not None and time.monotonic() - failed < ENGINE_RETRY_S:
            return {"available": False, "error": _ENGINE_FAIL.get("error")}
    try:
        _studio("run_exec").get_engine()
        _ENGINE_FAIL.clear()
        return {"available": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        _ENGINE_FAIL.update(at=time.monotonic(), error=f"{type(exc).__name__}: {exc}")
        return {"available": False, "error": _ENGINE_FAIL["error"]}


def steps_catalog() -> list[dict]:
    """Каталог шагов для UI: id, зависимости, признак опционального гейта, артефакты."""
    status = _studio("status")
    pipeline = status._load_pipeline()
    labels = _studio("labels")
    artifacts = step_output_artifacts()
    return [{
        "id": s["id"], "title": labels.step_title(s["id"]), "depends_on": list(s["depends_on"]),
        "optional_gate": s["enable"] or s["condition"],
        "output_artifacts": list(s["output_artifacts"]) or artifacts.get(s["id"], []),
    } for s in pipeline["steps"]]


# ------------------------------------------------------------------ сборка плана

def _problem(level: str, code: str, message: str, step_id: str | None = None, artifact: str | None = None) -> dict:
    return {"level": level, "code": code, "message": message, "step_id": step_id, "artifact": artifact}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def plan_digest(plan: dict) -> str:
    """sha256 канонического JSON плана без workflow_id, digest, оценки и изменчивых problems (итог виден в can_start)."""
    body = {k: v for k, v in plan.items() if k not in ("workflow_id", "plan_digest", "estimate", "problems")}
    raw = json.dumps(_jsonable(body), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


DISCARDED_REL = ".studio/discarded_workflows.json"
_DISCARDED_KEEP = 100


def read_discarded(root: Path) -> list[str]:
    try:
        data = json.loads((Path(root) / DISCARDED_REL).read_text(encoding="utf-8"))
        items = data.get("workflows") if isinstance(data, dict) else None
    except (OSError, UnicodeError, ValueError):
        return []
    return [w for w in items if isinstance(w, str)] if isinstance(items, list) else []


def add_discarded(root: Path, workflow_ids: list[str]) -> None:
    """«Начать сначала»: запоминаем отброшенные прогоны, чтобы resume не брал их готовые шаги."""
    if not workflow_ids:
        return
    merged = [w for w in read_discarded(root) if w not in workflow_ids] + list(workflow_ids)
    (Path(root) / DISCARDED_REL).parent.mkdir(parents=True, exist_ok=True)
    _studio("project_store").write_json_atomic(Path(root) / DISCARDED_REL,
                                               {"version": 1, "workflows": merged[-_DISCARDED_KEEP:]})


def is_full_workflow_id(workflow_id: str, project_id: str) -> bool:
    """Полный прогон: `sbm_<проект>_<hex8>` (у проекта `partial_x` id начинается так же, как у частичного)."""
    return re.fullmatch(rf"sbm_{re.escape(project_id)}_[0-9a-f]{{8}}", workflow_id) is not None


def new_workflow_id(mode: str, project_id: str, step_id: str | None = None) -> str:
    tail = uuid.uuid4().hex[:8]
    if mode == "full":
        return f"sbm_{project_id}_{tail}"
    if mode == "single_step":
        return f"sbm_single_{project_id}_{step_id}_{tail}"
    return f"sbm_partial_{project_id}_{tail}"


def _validate_shot_keys(shot_keys: Any) -> list[str]:
    if shot_keys is None:
        return []
    if not isinstance(shot_keys, list) or any(not isinstance(k, str) or not _SAFE_KEY.match(k) for k in shot_keys):
        raise StudioError("invalid_argument", "shot_keys: нужен список ключей шотов")
    return list(dict.fromkeys(shot_keys))


def _gate_state(step: dict, flags: dict, enable_only: bool = False) -> bool:
    """False, если шаг выключен гейтом (enable/condition явно ложны)."""
    status = _studio("status")
    for expr in ((step.get("enable"),) if enable_only else (step.get("enable"), step.get("condition"))):
        if isinstance(expr, str) and expr.strip():
            try:
                value = status._eval_condition(expr, flags)
            except Exception:  # noqa: BLE001
                value = None
            if value is False:
                return False
    return True


def _inputs_layers(project_id: str, yaml_inputs: dict, brief: dict, checkpoint_vars: dict,
                   explicit: dict, problems: list[dict]) -> tuple[dict, dict]:
    inputs = dict(yaml_inputs)
    sources = {k: "default" for k in inputs}

    def put(values: dict, source: str) -> None:
        for key, value in values.items():
            inputs[key] = value
            sources[key] = source

    put({k: v for k, v in (checkpoint_vars or {}).items() if k not in ("project_id", "task")}, "checkpoint")
    put({k: brief[k] for k in BRIEF_INPUTS if k in brief}, "brief")
    # слой болванки (Ф11): значения из брифа; хук появится позже
    try:
        blockout = _studio("blockout")
    except ImportError:
        blockout = None
    if blockout is not None and hasattr(blockout, "settings_to_inputs"):
        try:
            put(dict(blockout.settings_to_inputs(project_id) or {}), "brief")
        except StudioError as exc:
            problems.append(_problem("warning", "settings_invalid", exc.message))
        except Exception as exc:  # noqa: BLE001
            logger.warning("blockout.settings_to_inputs: %s", exc)
    run_settings = _studio("run_settings")
    settings_keys: set[str] = set()
    try:
        values = run_settings.settings_to_inputs(project_id)
        settings_keys = set(values)
        put(values, "run_settings")
    except StudioError as exc:
        problems.append(_problem("error", "settings_invalid", exc.message))
    put(explicit, "explicit")
    return inputs, {"sources": sources, "settings_keys": settings_keys}


def build_plan(project_id: str, mode: str, *, start_step: str | None = None, step_id: str | None = None,
               shot_keys: Any = None, kinds: Any = None, rebuild_film: bool = False,
               inputs: Any = None, task: str | None = None, strict: bool = False,
               engine=None) -> dict:
    """План прогона. `strict=True` - для `run_start`: лишние ключи inputs отклоняются."""
    if mode not in MODES:
        raise StudioError("invalid_argument", "mode: full, from_step, single_step, resume или regenerate_shots")
    if inputs is not None and not isinstance(inputs, dict):
        raise StudioError("invalid_argument", "inputs: нужен объект")
    explicit = dict(inputs or {})
    root = project_paths.safe_storybook_project_dir(project_id, must_exist=True)
    status = _studio("status")
    pipeline = status._load_pipeline()
    all_ids = [s["id"] for s in pipeline["steps"]]
    problems: list[dict] = []
    keys = _validate_shot_keys(shot_keys)

    if strict:
        _check_strict_inputs(explicit, pipeline["inputs"], project_id)

    eng_state = {"available": engine is not None, "error": None}
    if engine is None:
        eng_state = engine_status()
        if eng_state["available"]:
            engine = _studio("run_exec").get_engine()
    if not eng_state["available"]:
        problems.append(_problem("error", "engine_unavailable",
                                 f"Движок прогона недоступен: {eng_state['error']}"))

    brief = _read_brief(root)
    source_wid: str | None = None
    base_ckpt = None
    reuse_wid: str | None = None
    skip: list[str] = []
    start: str | None = None

    def need_step(sid: str | None, label: str) -> str | None:
        if not sid or sid not in all_ids:
            problems.append(_problem("error", "step_unknown", f"{label}: шаг «{sid}» не найден в пайплайне", sid))
            return None
        return sid

    if mode == "full":
        run_ids = list(all_ids)
        if brief:
            problems.append(_problem("info", "brief_exists",
                                     "Бриф уже есть и не будет пересоздан; текст задания влияет лишь на шаги, "
                                     "читающие task"))
        elif not (task or "").strip():
            problems.append(_problem("error", "invalid_task", "Нет брифа: укажите описание сказки (task)"))
    elif mode == "from_step":
        start = need_step(start_step or step_id, "Начальный шаг")
        run_ids = all_ids[all_ids.index(start):] if start else []
        skip = all_ids[:all_ids.index(start)] if start else []
    elif mode == "single_step":
        start = need_step(step_id or start_step, "Шаг")
        run_ids = [start] if start else []
        skip = [i for i in all_ids if i != start]
    elif mode == "resume":
        run_ids, skip, source_wid, reuse_wid, base_ckpt = _resume_scope(
            engine, project_id, root, all_ids, problems)
    else:  # regenerate_shots
        run_ids = _regen_steps(kinds, rebuild_film, keys, problems)
        skip = [i for i in all_ids if i not in run_ids]

    run_ids_set = set(run_ids)
    if mode == "single_step" and start and engine is not None:
        base_ckpt = _latest_project_checkpoint(engine, project_id)
        source_wid = getattr(getattr(base_ckpt, "context", None), "workflow_id", None) if base_ckpt else None
        if base_ckpt is None or getattr(base_ckpt, "context", None) is None:
            problems.append(_problem("error", "no_checkpoint",
                                     f"Для шага «{start}» нужен сохранённый контекст предыдущего прогона", start))
    if mode == "from_step" and start and engine is not None:
        base_ckpt = _latest_project_checkpoint(engine, project_id)
        source_wid = getattr(getattr(base_ckpt, "context", None), "workflow_id", None) if base_ckpt else None
    if mode == "regenerate_shots" and engine is not None:
        base_ckpt = _latest_project_checkpoint(engine, project_id)
        source_wid = getattr(getattr(base_ckpt, "context", None), "workflow_id", None) if base_ckpt else None

    ckpt_vars = dict(getattr(getattr(base_ckpt, "context", None), "variables", None) or {}) if base_ckpt else {}
    merged, meta = _inputs_layers(project_id, pipeline["inputs"], brief, ckpt_vars, explicit, problems)
    merged["project_id"] = project_id
    meta["sources"]["project_id"] = "explicit"
    if task is not None:
        merged["task"] = task
        meta["sources"]["task"] = "explicit"

    # гейты (enable/condition) по итоговым входам
    flag_names = status._flag_names(pipeline)
    resolved = status._resolve_flags(flag_names, pipeline["inputs"], brief, {"inputs": merged}, {})
    flags = {name: value for name, (value, _src) in resolved.items()}
    step_by_id = {s["id"]: s for s in pipeline["steps"]}
    # шаг с выключенным гейтом остаётся в плане: пропуск делает движок/инструмент
    only_enable = mode in ("single_step", "regenerate_shots")  # condition при этом снимается
    gated_off = [i for i in run_ids if not _gate_state(step_by_id[i], flags, only_enable)]
    for sid in gated_off:
        problems.append(_problem("info", "blockout_gated",
                                 f"Шаг «{sid}» выключен настройкой и будет пропущен", sid))

    definition = None
    if mode in ("from_step", "single_step") and start and engine is not None:
        try:
            definition = load_definition()
        except Exception as exc:  # noqa: BLE001
            problems.append(_problem("error", "engine_unavailable", f"Не прочитан пайплайн: {exc}"))
    if mode == "from_step" and definition is not None and start:
        _check_artifacts(definition, run_ids, root, problems)
        _check_decoupled(pipeline, run_ids, problems)

    env: dict[str, str] = {}
    step_params: dict[str, dict] = {}
    if mode == "regenerate_shots":
        _plan_regen(keys, run_ids_set, merged, step_params, problems)
    if mode == "single_step" and start == _VIDEO_STEP and merged.get("video_provider"):
        env["STORYBOOK_VIDEO_PROVIDER"] = str(merged["video_provider"])

    _check_busy_and_jobs(root, problems)

    if mode == "resume":
        workflow_id = reuse_wid or new_workflow_id("partial", project_id)
    else:
        workflow_id = new_workflow_id(mode, project_id, start)
    from custom_tools.storybook.project_paths import current_series_scope
    active_scope = current_series_scope()
    series_id = active_scope[1] if active_scope and active_scope[0] == project_id else None
    plan = {
        "version": PLAN_VERSION, "mode": mode, "project_id": project_id, "series_id": series_id, "pipeline_hash": pipeline_hash(),
        "steps_to_run": list(run_ids),
        "skip_steps": skip, "gated_off": gated_off, "start_step": start,
        "source_workflow_id": source_wid, "workflow_id": workflow_id,
        "inputs": merged, "input_sources": meta["sources"], "step_params": step_params,
        "scope": {"shot_keys": keys, "kinds": list(kinds or []) if mode == "regenerate_shots" else []},
        "prepare": {"hook": "review.prepare_regeneration" if mode == "regenerate_shots" else None},
        "env": env, "problems": problems, "can_start": False, "plan_digest": "", "estimate": None,
        "rebuild_film": bool(rebuild_film) if mode == "regenerate_shots" else False,
        "reuse_workflow_id": bool(reuse_wid),
    }
    plan.update(stale_results_for_plan(project_id, list(run_ids), inputs=merged,
                                       explicit_step=start if mode == "single_step" else None))
    required_steps = set()
    def collect_inputs(step_id):
        for dependency in step_by_id.get(step_id, {}).get("depends_on", []):
            if dependency not in required_steps:
                required_steps.add(dependency)
                collect_inputs(dependency)
    for stale_step in plan["stale_steps"]:
        collect_inputs(stale_step)
    output_map = step_output_artifacts()
    plan["required_inputs"] = list(dict.fromkeys(
        rel for dependency in all_ids if dependency in required_steps and dependency not in run_ids_set
        for rel in output_map.get(dependency, []) if (root / rel).exists()))
    plan["can_start"] = not any(p["level"] == "error" for p in plan["problems"])
    plan["plan_digest"] = plan_digest(plan)
    plan["estimate"] = _estimate(project_id, plan)
    return plan


def _check_strict_inputs(explicit: dict, yaml_inputs: dict, project_id: str) -> None:
    run_settings = _studio("run_settings")
    try:
        owned = set(run_settings.settings_to_inputs(project_id))
    except StudioError:
        owned = set()
    unknown = [k for k in explicit if k not in yaml_inputs]
    owned_bad = [k for k in explicit if k in owned]
    if unknown:
        raise StudioError("invalid_argument", "Неизвестные входы пайплайна: " + ", ".join(sorted(unknown)))
    if owned_bad:
        raise StudioError("invalid_argument", "Эти значения задаются в настройках прогона: "
                          + ", ".join(sorted(owned_bad)))


def _latest_project_checkpoint(engine, project_id: str):
    runs = find_runs(engine, project_id, limit=1)
    return load_checkpoint(engine, runs[0]["workflow_id"]) if runs else None


def _run_history(engine, project_id: str) -> list[dict]:
    """Последние прогоны проекта. Пока выборка целиком состоит из завершённых одиночных шагов, берём больше:
    иначе упавший полный прогон под ними не виден и resume отвечает «продолжать нечего»."""
    history = find_runs(engine, project_id, limit=20)
    if len(history) == 20 and all(r["status"] == "completed" and _is_single_workflow_id(r["workflow_id"], project_id)
                                  for r in history):
        history = find_runs(engine, project_id, limit=_HISTORY_MAX, slim_singles=True)  # один дозапрос вместо постепенного роста
    return history


def _is_single_workflow_id(workflow_id: str, project_id: str) -> bool:
    """Одиночный шаг: `sbm_single_...`; полный прогон проекта `single_x` по префиксу похож на него, но им не является."""
    return workflow_id.startswith("sbm_single_") and not is_full_workflow_id(workflow_id, project_id)


def _resume_source(history: list[dict], project_id: str, discarded: set[str]) -> tuple[list[dict], list[dict]]:
    """Источник resume и пропущенные прогоны. Завершённые одиночные шаги поверх незавершённого полного
    прогона не считаются «последним прогоном»: иначе после сбоя и успешного одиночного шага продолжать «нечего».
    Частичные прогоны (`sbm_partial_`) не пропускаем: это может быть само продолжение, доведшее цепочку до конца.
    Упавший одиночный после последнего завершённого - обычная незавершённая работа."""
    if not history:
        return [], []
    idx = 0
    while idx < len(history) and history[idx]["status"] == "completed" \
            and _is_single_workflow_id(history[idx]["workflow_id"], project_id):
        idx += 1
    if idx == 0 or idx >= len(history):
        return history[:1], []
    run = history[idx]
    if run["status"] == "completed" or run["workflow_id"] in discarded:
        return history[:1], []
    for older in history[idx:]:
        if (older["status"] == "completed" and not _is_single_workflow_id(older["workflow_id"], project_id)) \
                or older["workflow_id"] in discarded:
            break  # цепочку оборвал завершённый (не одиночный) или отброшенный прогон
        if is_full_workflow_id(older["workflow_id"], project_id):
            return [run], history[:idx]
    return history[:1], []


def _resume_scope(engine, project_id: str, root: Path, all_ids: list[str], problems: list[dict]):
    """(run_ids, skip, source_wid, reuse_wid, checkpoint) для режима resume."""
    history = _run_history(engine, project_id) if engine is not None else []
    runs, side_runs = _resume_source(history, project_id, set(read_discarded(root)))
    ckpt = None
    completed: list[str] = []
    source = reuse = None
    if runs:
        source = runs[0]["workflow_id"]
        ckpt = load_checkpoint(engine, source)
        completed = list(runs[0]["completed_steps"])
        state = runs[0]["status"]
        if state == "completed":
            problems.append(_problem("error", "nothing_to_resume", "Последний прогон завершён, продолжать нечего"))
        elif state == "cancelled":
            pass  # новый workflow_id: чекпоинт CANCELLED остановил бы новый прогон
        else:
            reuse = source
        # Чекпоинт повторного прогона знает лишь шаги обрезанного определения: готовые шаги первого
        # прогона берём из журнала шагов того же workflow, иначе второе продолжение оплатит их заново.
        # Цепочка: отмена -> продолжение -> сбой -> продолжение; берём все прогоны новее последнего завершённого.
        chain = {source} | {r["workflow_id"] for r in side_runs}  # шаги готовых одиночных прогонов тоже готовы
        for side in side_runs:  # журнал шагов мог потеряться: готовность берём и из чекпоинта одиночного прогона
            completed.extend(s for s in side["completed_steps"] if s not in completed)
        discarded = set(read_discarded(root))
        for older in history[[r["workflow_id"] for r in history].index(source):]:
            if (older["status"] == "completed" and not _is_single_workflow_id(older["workflow_id"], project_id)) \
                    or (older["workflow_id"] in discarded and older["workflow_id"] != source):
                break  # завершённый (не одиночный) или отброшенный («Начать сначала») прогон не входит в цепочку;
                # готовые одиночные шаги внутри цепочки её не рвут: их шаги уже оплачены
            chain.add(older["workflow_id"])
            completed.extend(s for s in older["completed_steps"] if s not in completed)
            if is_full_workflow_id(older["workflow_id"], project_id):
                break  # полный прогон (id без префикса, в том числе «Начать сначала») - начало цепочки
        logged = _studio("step_runs").read_step_runs(root)["steps"]
        for sid in all_ids:
            rec = logged.get(sid) or {}
            if rec.get("status") == "completed" and rec.get("workflow_id") in chain and sid not in completed:
                completed.append(sid)
    else:
        step_runs = _studio("step_runs").read_step_runs(root)["steps"]
        completed = [i for i in all_ids if (step_runs.get(i) or {}).get("status") == "completed"]
        if completed:
            problems.append(_problem("info", "no_checkpoint", "Чекпоинт не найден: продолжение по журналу шагов"))
    done = set(completed)
    run_ids = [i for i in all_ids if i not in done]
    if not run_ids and not any(p["code"] == "nothing_to_resume" for p in problems):
        problems.append(_problem("error", "nothing_to_resume", "Все шаги уже выполнены, продолжать нечего"))
    if not runs and not completed:
        problems.append(_problem("error", "nothing_to_resume", "Нет прогона, который можно продолжить"))
    return run_ids, [i for i in all_ids if i in done], source, reuse, ckpt


def _regen_steps(kinds: Any, rebuild_film: bool, keys: list[str], problems: list[dict]) -> list[str]:
    kinds_list = list(kinds or [])
    if not kinds_list or any(k not in KINDS for k in kinds_list):
        raise StudioError("invalid_argument", "kinds: images и/или video")
    if not keys:
        raise StudioError("invalid_argument", "shot_keys: выберите хотя бы один шот")
    steps = [KIND_STEPS[k] for k in KINDS if k in kinds_list]
    if rebuild_film:
        steps.append(MONTAGE_STEP)
    return steps


def _plan_regen(keys: list[str], run_ids: set[str], inputs: dict, step_params: dict, problems: list[dict]) -> None:
    if _VIDEO_STEP in run_ids:
        provider = str(inputs.get("video_provider") or DEFAULT_PROVIDER).lower()
        if provider not in SHOT_SCOPE_PROVIDERS:
            problems.append(_problem("error", "provider_no_shot_scope",
                                     f"Провайдер «{provider}» не умеет генерировать отдельные шоты", _VIDEO_STEP))
        step_params[_VIDEO_STEP] = {"shot_keys": list(keys)}
    review = _try_studio("review")
    if review is None or not hasattr(review, "prepare_regeneration"):
        problems.append(_problem("error", "unavailable", "Перегенерация шотов пока недоступна (нет модуля проверки)"))


def _try_studio(name: str):
    try:
        return _studio(name)
    except ImportError:
        return None


def _check_artifacts(definition, run_ids: list[str], root: Path, problems: list[dict]) -> None:
    artifact_map = step_output_artifacts(definition)
    owners: dict[str, list[str]] = {}
    for sid, rels in artifact_map.items():
        for rel in rels:
            owners.setdefault(rel, []).append(sid)
    first = run_ids[0] if run_ids else None
    for rel in collect_required_for_steps(definition, run_ids, root):
        target = root / rel
        ok = target.exists() and (not target.is_dir() or any(target.iterdir()))
        if not ok:
            by = ", ".join(owners.get(rel, [])) or "предыдущих шагов"
            problems.append(_problem("error", "missing_artifact",
                                     f"Для запуска нужны результаты шагов: {by}; запустите их раньше",
                                     first, rel))


def _check_decoupled(pipeline: dict, run_ids: list[str], problems: list[dict]) -> None:
    inside = set(run_ids)
    for step in pipeline["steps"]:
        if step["id"] not in inside:
            continue
        cut = [d for d in step["depends_on"] if d not in inside]
        if cut:
            problems.append(_problem("info", "dep_decoupled",
                                     f"Шаг «{step['id']}» не ждёт пропущенные: {', '.join(cut)}", step["id"]))


def _check_busy_and_jobs(root: Path, problems: list[dict]) -> None:
    control = _studio("run_control")
    try:
        if control.probe_lock(root):
            problems.append(_problem("error", "project_busy", "Проект уже выполняется"))
    except Exception:  # noqa: BLE001
        pass
    inflight = [j for j in _studio("step_runs").read_jobs(root) if str(j.get("status")).lower() == "submitting"]
    if inflight:
        problems.append(_problem("warning", "video_jobs_inflight",
                                 f"Видео-задач в статусе «отправляется»: {len(inflight)}. "
                                 "Повторный запуск может оплатить их дважды"))


def _estimate(project_id: str, plan: dict) -> Optional[dict]:
    try:
        run_settings = _studio("run_settings")
        result = run_settings.cost_estimate_get(
            project_id, step_ids=plan["steps_to_run"], shot_keys=plan["scope"]["shot_keys"] or None)
    except Exception as exc:  # noqa: BLE001
        logger.info("Оценка стоимости недоступна: %s", exc)
        return None
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k != "status"}
    return None


# ------------------------------------------------------------------ определение по плану

def build_definition(plan: dict):
    """WorkflowDefinition под план: обрезка шагов, развязка висячих depends_on, params."""
    definition = load_definition()
    wanted = list(plan["steps_to_run"])
    by_id = {s.id: s for s in definition.steps}
    mode = plan["mode"]
    steps = []
    for sid in [s.id for s in definition.steps if s.id in set(wanted)]:
        step = copy.deepcopy(by_id[sid])
        if mode in ("single_step", "regenerate_shots"):
            step.depends_on = []
            step.condition = None
        else:
            step.depends_on = [d for d in step.depends_on if d in set(wanted)]
        for key, value in (plan.get("step_params") or {}).get(sid, {}).items():
            step.tool_params = {**(step.tool_params or {}), key: value}
        steps.append(step)
    definition.steps = steps
    return definition
