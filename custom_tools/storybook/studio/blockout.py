"""Болванка (Ф11): состояние, подробности, план/запуск действий и две прямые записи.

Всё, что меняет проект через пайплайн, идёт одним путём: `blockout_plan` (только расчёт)
-> `blockout_run` -> `runs.run_start` (Ф4). Свой запуск и замок здесь не берутся.
Без Tk, Blender и импорта `blockout_renderer`. Соседние модули studio зовём через importlib
в момент вызова: тесты пересоздают реестр.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook import project_paths

from . import _blockout_state as S
from .capabilities import capability
from .errors import StudioError, success
from .paths import MEDIA_EXTENSIONS, project_dir, project_relpath

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
BLOCKOUT_DIR = S.BLOCKOUT_DIR

ACTIONS = ("redraw_shot", "redraw_chain", "rebuild_scene", "build_preview", "regenerate_shots")
# действия, которые требуют выбранных шотов; остальные работают на проекте целиком
_CONFIRM_ACTIONS = ("redraw_chain", "rebuild_scene", "regenerate_shots")
_MAX_KEYS = 500
_PROBE_TTL_S = 10.0
_BLENDER_TTL_S = 60.0

# запасные значения совпадают с inputs в storybook_pipeline.yaml (сверяет тест)
DEFAULTS: dict[str, Any] = {
    "generate_blockout": False,
    "blockout_fps": 24,
    "blockout_resolution": "1280x720",
    "blockout_jobs": 0,
    "blockout_preview_burnin": True,
    "blockout_use_as_image_reference": False,
    "blockout_use_as_video_reference": False,
}


def _studio(name: str):
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _try_studio(name: str):
    try:
        return _studio(name)
    except ImportError:
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _warn(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


# ------------------------------------------------------------------ настройки из брифа

def _valid_setting(name: str, value: Any) -> bool:
    if name == "blockout_fps":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if name == "blockout_resolution":
        size = S.parse_resolution(value)
        return size is not None and min(size) > 0
    if name == "blockout_jobs":
        return _is_int(value) and value >= 0
    return isinstance(value, bool)


def _read_brief(root: Path) -> dict:
    data, _ = S.read_json(root / "00_brief.json")
    return data if isinstance(data, dict) else {}


def _brief_settings(brief: dict, strict: bool, warnings: Optional[list] = None) -> dict:
    """Заданные в брифе и допустимые значения. Плохое значение: strict -> ошибка, иначе предупреждение."""
    found: dict[str, Any] = {}
    for name in DEFAULTS:
        if name not in brief or brief[name] is None:
            continue
        value = brief[name]
        if _valid_setting(name, value):
            found[name] = value
        elif strict:
            raise StudioError("invalid_argument", f"В брифе неверное значение {name}")
        elif warnings is not None:
            warnings.append(_warn("brief_value_invalid", f"В брифе неверное значение {name}: взято значение по умолчанию"))
    return found


def settings_to_inputs(project_id: str) -> dict:
    """Хук для Ф4 (не возможность): входы пайплайна из брифа, только заданные там."""
    root = project_dir(project_id)
    return _brief_settings(_read_brief(root), strict=True)


# ------------------------------------------------------------------ инструменты и проба запуска

_TOOLS_CACHE: dict[str, Any] = {}
_PROBE_CACHE: dict[str, tuple] = {}
_GUARD = threading.Lock()


def reset_caches() -> None:
    with _GUARD:
        _TOOLS_CACHE.clear()
        _PROBE_CACHE.clear()
    S.clear_cache()


def _blender_info() -> dict:
    try:
        probe = _studio_video_contract()._probe_blockout_blender_readiness
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "version": None, "message": f"проба Blender недоступна: {exc}"}
    now = time.monotonic()
    with _GUARD:
        hit = _TOOLS_CACHE.get("blender")
        if hit and hit[0] is probe and now - hit[1] < _BLENDER_TTL_S:
            return dict(hit[2])
    try:
        raw = probe() or {}
        info = {"available": bool(raw.get("available")), "version": raw.get("version"),
                "message": str(raw.get("message") or "")}
    except Exception as exc:  # noqa: BLE001
        info = {"available": False, "version": None, "message": f"проба Blender не удалась: {exc}"}
    with _GUARD:
        _TOOLS_CACHE["blender"] = (probe, now, dict(info))
    return info


def _studio_video_contract():
    return importlib.import_module("custom_tools.storybook.video_contract")


def _run_probe(project_id: str) -> dict:
    """Один пробный план `single_step`: чекпоинт, движок, занятость. Кэш 10 с."""
    runs = _try_studio("runs")
    fn = getattr(runs, "run_plan_get", None)
    unknown = {"known": False, "codes": frozenset()}
    if fn is None:
        return unknown
    now = time.monotonic()
    with _GUARD:
        hit = _PROBE_CACHE.get(project_id)
        if hit and hit[0] is fn and now - hit[1] < _PROBE_TTL_S:
            return hit[2]
    try:
        result = fn(project_id, "single_step", step_id="blockout_renderer")
        problems = (result.get("plan") or {}).get("problems") or []
        value = {"known": True, "codes": frozenset(
            p.get("code") for p in problems if isinstance(p, dict) and p.get("level") == "error")}
    except Exception as exc:  # noqa: BLE001
        logger.info("blockout: проба плана не удалась: %s", exc)
        value = unknown
    with _GUARD:
        _PROBE_CACHE[project_id] = (fn, now, value)
    return value


def _run_info(project_id: str) -> dict:
    runs = _try_studio("runs")
    fn = getattr(runs, "active_run_info", None)
    if fn is None:
        return {"active": False, "current_steps": None, "known": False}
    try:
        info = fn(project_id) or {}
    except Exception:  # noqa: BLE001
        logger.exception("blockout: active_run_info")
        return {"active": False, "current_steps": None, "known": False}
    return {"active": bool(info.get("active")), "current_steps": info.get("current_steps"),
            "known": True}


def _screenplay_running(run: dict) -> bool:
    if not run.get("active"):
        return False
    steps = run.get("current_steps")
    return steps is None or "screenplay_shots_generator" in steps


# ------------------------------------------------------------------ чтение проекта

def _collect(project_id: str) -> dict:
    root = project_dir(project_id)
    warnings: list[dict] = []

    def load(rel: str, expect=dict):
        data, status = S.read_json(root / rel)
        if status == "broken" or (status == "ok" and not isinstance(data, expect)):
            warnings.append(_warn("file_broken", f"Файл {rel} повреждён и не прочитан", file=rel))
            return None
        return data if status == "ok" else None

    shots_doc = load("97_shots/shots.json")
    items = shots_doc.get("items") if isinstance(shots_doc, dict) else None
    rows: dict[tuple[int, int], dict] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        scene, shot = item.get("scene_number"), item.get("shot_number")
        if not (_is_int(scene) and _is_int(shot)):
            continue
        row = rows.setdefault((scene, shot), {"start": None})
        if row["start"] is None and str(item.get("shot_type") or "").lower() == "start":
            row["start"] = item

    chains_doc = load(f"{BLOCKOUT_DIR}/chains.json")
    spec_doc = load(f"{BLOCKOUT_DIR}/scene_spec.json")
    chain_of: dict[tuple[int, int], dict] = {}
    chain_defs: list[dict] = []
    chains_list = (chains_doc or {}).get("chains")
    if chains_list is not None and not isinstance(chains_list, list):
        warnings.append(_warn("file_broken", f"В {BLOCKOUT_DIR}/chains.json поле chains не список", file=f"{BLOCKOUT_DIR}/chains.json"))
        chains_list = []
    skipped_chains = 0
    for chain in chains_list or []:
        cid = chain.get("chain_id") if isinstance(chain, dict) else None
        if not S.safe_chain_id(cid) or not isinstance(chain.get("shots") or [], list):
            skipped_chains += 1
            continue
        keys = []
        for shot in chain.get("shots") or []:
            if isinstance(shot, dict):
                pair = (chain.get("scene_number"), shot.get("shot_number"))
                if not (_is_int(pair[0]) and _is_int(pair[1])):
                    continue
                chain_of[pair] = {"chain_id": cid, "duration_s": shot.get("duration_s")}
                keys.append(pair)
        chain_defs.append({"chain_id": cid, "scene_number": chain.get("scene_number"), "pairs": keys})
    if skipped_chains:
        warnings.append(_warn("file_broken", f"В {BLOCKOUT_DIR}/chains.json пропущено цепочек с неверной формой: {skipped_chains}",
                              file=f"{BLOCKOUT_DIR}/chains.json"))
    spec_list = (spec_doc or {}).get("chains")
    if spec_list is not None and not isinstance(spec_list, list):
        warnings.append(_warn("file_broken", f"В {BLOCKOUT_DIR}/scene_spec.json поле chains не список",
                              file=f"{BLOCKOUT_DIR}/scene_spec.json"))
    spec_chain = {c["chain_id"]: c for c in spec_list if isinstance(c, dict) and S.safe_chain_id(c.get("chain_id"))} \
        if isinstance(spec_list, list) else {}

    manifests: dict[tuple[int, int], Optional[dict]] = {}
    for scene, shot in rows:
        rel = f"{BLOCKOUT_DIR}/{S.shot_dir_name(scene, shot)}/manifest.json"
        manifests[(scene, shot)] = load(rel)

    return {
        "root": root, "warnings": warnings, "rows": rows, "chain_of": chain_of, "chain_defs": chain_defs,
        "spec_chain": spec_chain, "manifests": manifests, "shots_doc": shots_doc,
        "built": (root / BLOCKOUT_DIR / "chains.json").is_file() or (root / BLOCKOUT_DIR / "scene_spec.json").is_file(),
        "spec_present": isinstance(spec_doc, dict),
        "spec_fps": spec_doc.get("fps") if isinstance(spec_doc, dict) else None,
        "brief": load("00_brief.json") or {},
    }


def _durations(project_id: str, root: Path, brief: dict) -> dict:
    run_settings = _try_studio("run_settings")
    if run_settings is not None:
        try:
            stored, _ = run_settings.read_stored(project_id)
            allowed = run_settings.normalize(stored)["video"]["allowed_durations"]
            if isinstance(allowed, list) and allowed:
                return {"allowed": [int(v) for v in allowed], "source": "run_settings"}
        except Exception:  # noqa: BLE001
            logger.info("blockout: run_settings недоступны", exc_info=True)
    raw = brief.get("blockout_allowed_durations")
    if isinstance(raw, list) and raw and all(_is_int(v) for v in raw):
        return {"allowed": list(raw), "source": "brief"}
    caps, _ = S.read_json(root / "97_shots" / "video_model_caps.json")
    values = caps.get("supported_durations") if isinstance(caps, dict) else None
    if isinstance(values, list) and values and all(_is_int(v) for v in values):
        return {"allowed": list(values), "source": "video_model_caps"}
    return {"allowed": [], "source": "none"}


def _servable(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def _file_ref(project_id: str, path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        return {"path": project_relpath(project_id, path), "servable": _servable(path),
                "mtime": path.stat().st_mtime}
    except (OSError, StudioError):
        return None


def _preview(root: Path) -> dict:
    pdir = root / BLOCKOUT_DIR / "preview"
    burnin, plain = pdir / "blockout_all_burnin.mp4", pdir / "blockout_all.mp4"
    chosen, kind = (burnin, "burnin") if burnin.is_file() else ((plain, "plain") if plain.is_file() else (None, None))
    sheets: list[str] = []
    try:
        sheets = sorted(f"{BLOCKOUT_DIR}/preview/{p.name}" for p in pdir.glob("contact_sheet*.png") if p.is_file())
    except OSError:
        pass
    return {"path": f"{BLOCKOUT_DIR}/preview/{chosen.name}" if chosen else None, "kind": kind,
            "servable": bool(chosen and _servable(chosen)), "contact_sheets": sheets}


def _blk(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _build_state(project_id: str) -> dict:
    snap = _collect(project_id)
    root, warnings = snap["root"], snap["warnings"]
    brief = snap["brief"]
    picked = _brief_settings(brief, strict=False, warnings=warnings)
    cfg = {**DEFAULTS, **picked}
    settings = {
        "fps": cfg["blockout_fps"], "resolution": cfg["blockout_resolution"], "jobs": cfg["blockout_jobs"],
        "burnin": cfg["blockout_preview_burnin"],
        "use_as_image_reference": cfg["blockout_use_as_image_reference"],
        "use_as_video_reference": cfg["blockout_use_as_video_reference"],
        "scene_spec_fps": snap["spec_fps"], "source": "brief" if picked else "default",
    }
    resolution = S.parse_resolution(cfg["blockout_resolution"])
    durations = _durations(project_id, root, brief)

    shot_rows: list[dict] = []
    summary = {"total": 0, "rendered": 0, "stale": 0, "missing": 0, "junction_error": 0, "p10": 0}
    for (scene, shot) in sorted(snap["rows"]):
        start = snap["rows"][(scene, shot)]["start"]
        info = snap["chain_of"].get((scene, shot))
        spec_duration = info.get("duration_s") if info else None
        shots_duration = start.get("duration_s") if start else None
        spec_hash = None
        if info and info["chain_id"] in snap["spec_chain"]:
            spec_hash = S.compute_spec_hash(snap["spec_chain"][info["chain_id"]])
        manifest = snap["manifests"].get((scene, shot))
        state = S.compute_state(manifest, shots_duration_s=shots_duration, scene_spec_duration_s=spec_duration,
                                fps=cfg["blockout_fps"], spec_hash=spec_hash, resolution=resolution)
        mismatch = S.duration_mismatch(shots_duration, spec_duration)
        p10 = S.is_p10(manifest, S.final_image_mtimes(root, scene, shot))
        allowed = []
        if info and not mismatch:
            allowed += ["redraw_shot", "redraw_chain"]
        allowed.append("regenerate_shots")
        if durations["allowed"] and start is not None:
            allowed.append("set_duration")
        if p10:
            allowed.append("leave_as_is")
        shot_rows.append({
            "key": S.make_key(scene, shot), "scene_number": scene, "shot_number": shot,
            "chain_id": info["chain_id"] if info else None, "state": state, "state_title": S.STATE_TITLES[state],
            "duration_s": shots_duration, "scene_spec_duration_s": spec_duration,
            "duration_mismatch": mismatch, "p10": p10, "allowed": allowed,
        })
        summary["total"] += 1
        summary[state] += 1
        summary["p10"] += 1 if p10 else 0

    chained = set()
    scenes: dict[int, dict] = {}
    for chain in snap["chain_defs"]:
        scene = chain["scene_number"]
        if not _is_int(scene):
            continue
        keys = [S.make_key(*p) for p in sorted(chain["pairs"])]
        chained.update(keys)
        blend = f"{BLOCKOUT_DIR}/{chain['chain_id']}/chain.blend"
        scenes.setdefault(scene, {"scene_number": scene, "chains": [], "unchained_shot_keys": []})["chains"].append({
            "chain_id": chain["chain_id"], "shot_keys": keys, "blend_path": blend,
            "blend_exists": (root / blend).is_file()})
    for row in shot_rows:
        if row["key"] not in chained:
            scenes.setdefault(row["scene_number"], {"scene_number": row["scene_number"], "chains": [],
                                                     "unchained_shot_keys": []})["unchained_shot_keys"].append(row["key"])

    tools = {"blender": _blender_info(), "ffmpeg": {"available": bool(shutil.which("ffmpeg"))}}
    run = _run_info(project_id)
    built = snap["built"]
    probe = _run_probe(project_id) if shot_rows or built else {"known": False, "codes": frozenset()}
    checkpoint = {"known": probe["known"], "present": ("no_checkpoint" not in probe["codes"]) if probe["known"] else None}
    review = _try_studio("review")
    regen_ok = review is not None and hasattr(review, "prepare_regeneration")

    state = {
        "project_id": project_id, "built": built,
        "empty_reason": "no_shots" if not shot_rows else (None if built else "not_built"),
        "enabled": bool(cfg["generate_blockout"]), "settings": settings, "durations": durations,
        "tools": tools, "scenes": [scenes[k] for k in sorted(scenes)], "shots": shot_rows, "summary": summary,
        "preview": _preview(root), "run": {k: run[k] for k in ("active", "current_steps", "known")},
        "checkpoint": checkpoint, "warnings": warnings,
    }
    state["actions"] = _actions(state, snap, probe, regen_ok)
    body = {k: v for k, v in state.items() if k not in ("run", "digest")}
    state["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
                                     .encode("utf-8")).hexdigest()
    return state


def _actions(state: dict, snap: dict, probe: dict, regen_ok: bool) -> dict:
    run, tools, summary = state["run"], state["tools"], state["summary"]
    codes = probe["codes"]
    out: dict[str, dict] = {}

    def common(extra_checkpoint: bool) -> list[dict]:
        blockers = []
        if run["active"]:
            blockers.append(_blk("run_active", "Выполняется прогон — дождитесь завершения."))
        if not probe["known"]:
            blockers.append(_blk("checkpoint_unknown", "Не удалось проверить состояние прогона."))
        else:
            if "engine_unavailable" in codes:
                blockers.append(_blk("engine_unavailable", "Движок прогона недоступен."))
            if extra_checkpoint and "no_checkpoint" in codes:
                blockers.append(_blk("no_checkpoint", "Проекту нужен полный прогон — сохранённый контекст не найден."))
        return blockers

    def blender() -> list[dict]:
        if tools["blender"]["available"]:
            return []
        text = "Blender не найден или не запускается. Задайте BLOCKOUT_BLENDER_BIN или установите Blender."
        detail = tools["blender"].get("message")
        return [_blk("blender_unavailable", f"{text} ({detail})" if detail else text)]

    not_built = [] if state["built"] else [_blk("not_built", "Болванка ещё не построена: нажмите «Пересобрать сцену».")]
    no_spec = [] if snap["spec_present"] else [_blk("no_scene_spec", "Нет файла scene_spec.json: сначала пересоберите сцену.")]
    fps_changed = []
    spec_fps = snap["spec_fps"]
    if spec_fps is not None and spec_fps != state["settings"]["fps"]:
        fps_changed = [_blk("fps_changed", "Частота кадров в настройках изменена: сначала пересоберите сцену.")]

    for name in ("redraw_shot", "redraw_chain"):
        out[name] = {"blockers": not_built + (no_spec if state["built"] else []) + blender() + fps_changed + common(True)}
    no_shots = [] if state["shots"] else [_blk("no_shots", "Нет шотов: сначала нужна раскадровка.")]
    out["rebuild_scene"] = {"blockers": no_shots + blender() + common(True)}
    preview_blockers = list(not_built)
    if state["built"] and not summary["rendered"]:
        preview_blockers.append(_blk("nothing_rendered", "Нет ни одной готовой болванки: собирать превью не из чего."))
    if not tools["ffmpeg"]["available"]:
        preview_blockers.append(_blk("ffmpeg_unavailable", "ffmpeg не найден: превью собрать нельзя."))
    out["build_preview"] = {"blockers": preview_blockers + common(True)}
    regen = [] if regen_ok else [_blk("regeneration_unavailable", "Перегенерация шотов появится после реализации версий.")]
    out["regenerate_shots"] = {"blockers": no_shots + regen + common(False)}

    edit = []
    if not state["durations"]["allowed"]:
        edit.append(_blk("no_durations", "Допустимые длительности неизвестны."))
    if _screenplay_running(run):
        edit.append(_blk("screenplay_running", "Идёт шаг «Раскадровка»: дождитесь его конца."))
    if not probe["known"]:
        edit.append(_blk("checkpoint_unknown", "Не удалось проверить состояние прогона."))
    elif "no_checkpoint" in codes:
        edit.append(_blk("no_checkpoint", "Проекту нужен полный прогон — сохранённый контекст не найден."))
    out["set_duration"] = {"blockers": edit}
    out["leave_as_is"] = {"blockers": [_blk("run_active", "Выполняется прогон — дождитесь завершения.")]
                          if run["active"] else []}
    for item in out.values():
        item["enabled"] = not item["blockers"]
    return out


@capability("blockout_state", title="Состояние болванки", access="read", category="blockout")
def blockout_state(project_id: str) -> dict:
    return success(**_build_state(project_id))


# ------------------------------------------------------------------ подробности

def _shot_row(state: dict, key: str) -> dict:
    for row in state["shots"]:
        if row["key"] == key:
            return row
    raise StudioError("not_found", f"Шот {key} не найден")


def _require_key(value: Any) -> tuple[str, tuple[int, int]]:
    pair = S.parse_key(value)
    if pair is None:
        raise StudioError("invalid_argument", "Не удалось разобрать ключ шота (ожидается вид 2-3)")
    return S.make_key(*pair), pair


def _notes_for(project_id: str, keys: set[str], chain_id: Optional[str], extra: list[dict]) -> list[dict]:
    notes: list[dict] = list(extra)
    reports = _try_studio("reports")
    if reports is None:
        return notes
    try:
        if chain_id is None and len(keys) == 1:
            result = reports.reports_findings(project_id, shot_key=next(iter(keys)), sources=["blockout"],
                                              levels=["error", "warning", "info"], limit=1000)
            found = result.get("findings") or []
        else:
            result = reports.reports_findings(project_id, sources=["blockout"],
                                              levels=["error", "warning", "info"], limit=1000)
            found = [f for f in result.get("findings") or []
                     if (f.get("details") or {}).get("chain_id") == chain_id
                     or keys & ({f.get("shot_key")} | set(f.get("related_shot_keys") or []))]
    except Exception:  # noqa: BLE001
        logger.exception("blockout: замечания недоступны")
        return notes
    for f in found:
        notes.append({"level": f.get("level"), "code": f.get("code"), "message": f.get("message"),
                      "shot_key": f.get("shot_key"), "source": "report"})
    return notes


@capability("blockout_detail", title="Подробности болванки", access="read", category="blockout")
def blockout_detail(project_id: str, shot_key: str = "", chain_id: str = "") -> dict:
    if bool(shot_key) == bool(chain_id):
        raise StudioError("invalid_argument", "Нужно ровно одно: shot_key или chain_id")
    state = _build_state(project_id)
    root = project_dir(project_id)
    if chain_id:
        for scene in state["scenes"]:
            for chain in scene["chains"]:
                if chain["chain_id"] == chain_id:
                    keys = set(chain["shot_keys"])
                    return success(chain_id=chain_id, shot_keys=chain["shot_keys"],
                                   notes=_notes_for(project_id, keys, chain_id, []),
                                   blend_exists=chain["blend_exists"], blend_path=chain["blend_path"])
        raise StudioError("not_found", f"Цепочка {chain_id} не найдена")

    key, (scene, shot) = _require_key(shot_key)
    row = _shot_row(state, key)
    folder = f"{BLOCKOUT_DIR}/{S.shot_dir_name(scene, shot)}"
    manifest, _ = S.read_json(root / folder / "manifest.json")
    manifest = manifest if isinstance(manifest, dict) else None
    extra = [{"level": "warning", "code": "manifest_warning", "message": str(w), "shot_key": key, "source": "manifest"}
             for w in (manifest or {}).get("warnings") or []]

    shots = _try_studio("shots")
    final: dict[str, Optional[Path]] = {"start": None, "end": None, "video": None}
    digest = None
    if shots is not None:
        try:
            final.update(shots.shot_files(project_id, key))
        except Exception:  # noqa: BLE001
            logger.info("blockout: файлы шота недоступны", exc_info=True)
        try:
            digest = shots.shot_get(project_id, key)["shot"]["digest"]
        except Exception:  # noqa: BLE001
            logger.info("blockout: digest шота недоступен", exc_info=True)

    def mine(name: str) -> Optional[dict]:
        return _file_ref(project_id, root / folder / name)

    start_item = _start_item(root, scene, shot)
    edit = state["actions"]["set_duration"]
    editable, reason = edit["enabled"], (edit["blockers"][0]["code"] if edit["blockers"] else None)
    if editable and "set_duration" not in row["allowed"]:
        editable, reason = False, "no_start_item"
    duration = {"current_s": row["duration_s"], "requested_s": (start_item or {}).get("duration_requested_s"),
                "source": (start_item or {}).get("duration_source"), "allowed": state["durations"]["allowed"],
                "editable": editable}
    if reason:
        duration["reason"] = reason
    keep = ("rendered_at", "fps", "resolution", "duration_s", "blender_version", "warnings",
            "junction_with_prev", "junction_with_next", "p10_acknowledged")
    return success(
        key=key, state=row["state"], state_title=row["state_title"], chain_id=row["chain_id"],
        notes=_notes_for(project_id, {key}, None, extra),
        manifest={k: manifest.get(k) for k in keep} if manifest else None,
        images={"blockout_start": mine("ref_start.png"), "image_start": _file_ref(project_id, final["start"]),
                "image_end": _file_ref(project_id, final["end"]), "blockout_end": mine("ref_end.png")},
        videos={"blockout_ref": mine("blockout_ref.mp4"), "final": _file_ref(project_id, final["video"])},
        duration=duration, p10=row["p10"], digest=digest)


def _start_item(root: Path, scene: int, shot: int) -> Optional[dict]:
    doc, _ = S.read_json(root / "97_shots" / "shots.json")
    items = doc.get("items") if isinstance(doc, dict) else None
    for item in items if isinstance(items, list) else []:
        if (isinstance(item, dict) and item.get("scene_number") == scene and item.get("shot_number") == shot
                and str(item.get("shot_type") or "").lower() == "start"):
            return item
    return None


# ------------------------------------------------------------------ план и запуск

_NOTES = {
    "redraw_shot": "Считается вся цепочка шота, но сохраняются файлы только этого шота.",
    "redraw_chain": "Будут пересчитаны все шоты цепочки.",
    "rebuild_scene": "Пересборка изменит chains.json, scene_spec.json и asset_map.json.",
    "build_preview": "Превью собирается из уже готовых болванок.",
    "regenerate_shots": "Старые картинки не удаляются, они сохранятся как версии шота.",
}
# проблемы этих кодов считает сам Ф11; остальные (занятость, чекпоинт, движок) отдаёт план Ф4
_OWN_CODES = frozenset({"not_built", "no_scene_spec", "blender_unavailable", "ffmpeg_unavailable", "fps_changed",
                        "nothing_rendered", "no_shots", "regeneration_unavailable"})


def _problem(level: str, code: str, message: str, step_id: Optional[str] = None) -> dict:
    return {"level": level, "code": code, "message": message, "step_id": step_id, "artifact": None,
            "source": "blockout"}


def _keys_list(shot_keys: Any) -> list[str]:
    if shot_keys is None:
        return []
    if not isinstance(shot_keys, list) or len(shot_keys) > _MAX_KEYS:
        raise StudioError("invalid_argument", "shot_keys: нужен список ключей шотов")
    return list(dict.fromkeys(_require_key(k)[0] for k in shot_keys))


def _chain_of_request(state: dict, keys: list[str], chain_id: Optional[str]) -> dict:
    chains = {c["chain_id"]: c for s in state["scenes"] for c in s["chains"]}
    if chain_id:
        if chain_id not in chains:
            raise StudioError("not_found", f"Цепочка {chain_id} не найдена")
        return chains[chain_id]
    if len(keys) != 1:
        raise StudioError("invalid_argument", "redraw_chain: нужна цепочка (chain_id) или один шот")
    cid = _shot_row(state, keys[0])["chain_id"]
    if cid is None:
        raise StudioError("invalid_argument", f"У шота {keys[0]} нет цепочки: пересоберите сцену")
    return chains[cid]


def _build_request(project_id: str, action: str, shot_keys: Any, chain_id: Any) -> dict:
    if action not in ACTIONS:
        raise StudioError("invalid_argument", "action: " + ", ".join(ACTIONS))
    if chain_id is not None and not isinstance(chain_id, str):
        raise StudioError("invalid_argument", "chain_id: нужна строка")
    keys = _keys_list(shot_keys)
    state = _build_state(project_id)
    cfg = state["settings"]
    problems: list[dict] = []
    for blocker in state["actions"][action]["blockers"]:
        if blocker["code"] in _OWN_CODES:
            problems.append(_problem("error", blocker["code"], blocker["message"]))
    common = {"generate_blockout": True, "blockout_fps": cfg["fps"], "blockout_resolution": cfg["resolution"]}
    req: dict[str, Any] = {"state": state, "mode": "single_step", "step_id": None, "shot_keys": None,
                           "kinds": None, "rebuild_film": False, "inputs": None, "chain": None}
    if action in ("redraw_shot", "redraw_chain") and not state["enabled"]:
        problems.append(_problem("info", "blockout_disabled",
                                 "Слой болванки выключен в настройках: на время этого запуска он включён."))

    if action == "redraw_shot":
        if len(keys) != 1:
            raise StudioError("invalid_argument", "redraw_shot: нужен ровно один шот")
        row = _shot_row(state, keys[0])
        if not row["chain_id"]:
            problems.append(_problem("error", "no_chain", "У шота нет цепочки: сначала пересоберите сцену."))
        if row["duration_mismatch"]:
            problems.append(_problem("error", "duration_mismatch",
                                     "Сначала пересоберите сцену: длительность шота изменена вручную."))
        scene, shot = row["scene_number"], row["shot_number"]
        req.update(step_id="blockout_renderer", inputs={
            **common, "blockout_scope": S.shot_dir_name(scene, shot), "blockout_jobs": cfg["jobs"]})
    elif action == "redraw_chain":
        chain = _chain_of_request(state, keys, chain_id)
        req["chain"] = chain
        rows = {r["key"]: r for r in state["shots"]}
        if any(rows[k]["duration_mismatch"] for k in chain["shot_keys"] if k in rows):
            problems.append(_problem("error", "duration_mismatch",
                                     "Сначала пересоберите сцену: длительность шота изменена вручную."))
        req.update(step_id="blockout_renderer", inputs={
            **common, "blockout_scope": f"chain_{chain['chain_id']}", "blockout_jobs": cfg["jobs"]})
    elif action == "rebuild_scene":
        problems.append(_problem("warning", "rebuild_makes_stale",
                                 "После пересборки все шоты станут «устарела»: перерисуйте их."))
        problems.append(_problem("warning", "rebuild_overwrites",
                                 "Пересборка изменит chains.json, scene_spec.json, asset_map.json."))
        req.update(step_id="blockout_scene_builder", inputs=dict(common))
    elif action == "build_preview":
        req.update(step_id="blockout_preview", inputs={"generate_blockout": True,
                                                      "blockout_preview_burnin": cfg["burnin"]})
    else:  # regenerate_shots
        if not keys:
            raise StudioError("invalid_argument", "shot_keys: выберите хотя бы один шот")
        rows = {r["key"]: r for r in state["shots"]}
        for key in keys:
            if key not in rows:
                raise StudioError("not_found", f"Шот {key} не найден")
        if any(rows[k]["state"] != S.STATE_RENDERED for k in keys):
            problems.append(_problem("warning", "blockout_not_current",
                                     "У части выбранных шотов болванка не актуальна: картинки опираются на устаревшую."))
        if not cfg["use_as_image_reference"]:
            problems.append(_problem("warning", "no_image_reference",
                                     "Картинки не будут опираться на болванку (выключено в настройках)."))
        req.update(mode="regenerate_shots", shot_keys=keys, kinds=["images"], rebuild_film=False)
    req["problems"] = problems
    return req


def _call_runs(name: str):
    fn = getattr(_try_studio("runs"), name, None)
    if fn is None:
        raise StudioError("unavailable", "Управление прогоном недоступно")
    return fn


def _run_kwargs(req: dict) -> dict:
    return {"mode": req["mode"], "step_id": req["step_id"], "shot_keys": req["shot_keys"], "kinds": req["kinds"],
            "rebuild_film": req["rebuild_film"], "inputs": req["inputs"]}


def _confirm(action: str, req: dict, plan: dict) -> dict:
    if action not in _CONFIRM_ACTIONS:
        return {"required": False, "title": None, "text": None, "cost_rub": None}
    if action == "redraw_chain":
        chain = req["chain"]
        return {"required": True, "title": "Перерисовать цепочку?", "cost_rub": None,
                "text": f"Будут пересчитаны все шоты цепочки {chain['chain_id']}: {len(chain['shot_keys'])}."}
    if action == "rebuild_scene":
        return {"required": True, "title": "Пересобрать сцену?", "cost_rub": None,
                "text": "После пересборки все шоты станут «устарела»: их нужно перерисовать. "
                        "Будут изменены chains.json, scene_spec.json и asset_map.json."}
    est = plan.get("estimate") if isinstance(plan, dict) else None
    cost = est.get("total_known_rub") if isinstance(est, dict) else None
    if cost is None:
        price = "Цена неизвестна."
    elif est.get("complete"):
        price = f"Оценка стоимости: {cost:.2f} ₽."
    else:
        price = f"Оценка стоимости: не менее {cost:.2f} ₽ (цена известна не для всех частей)."
    return {"required": True, "title": "Перегенерировать картинки шотов?", "cost_rub": cost,
            "text": f"Шотов: {len(req['shot_keys'])}. {price} "
                    "Старые картинки не удаляются, они сохранятся как версии шота."}


@capability("blockout_plan", title="План действия с болванкой", access="read", category="blockout")
def blockout_plan(project_id: str, action: str, shot_keys: Optional[list] = None,
                  chain_id: Optional[str] = None) -> dict:
    req = _build_request(project_id, action, shot_keys, chain_id)
    result = _call_runs("run_plan_get")(project_id, **_run_kwargs(req))
    plan = result.get("plan") if isinstance(result, dict) else None
    if not isinstance(plan, dict):
        raise StudioError("unavailable", "План прогона недоступен")
    problems = list(plan.get("problems") or []) + req["problems"]
    can_start = bool(plan.get("can_start")) and not any(p["level"] == "error" for p in req["problems"])
    return success(action=action, plan=plan, plan_digest=plan.get("plan_digest"), can_start=can_start,
                   problems=problems, confirm=_confirm(action, req, plan), notes=[_NOTES[action]])


@capability("blockout_run", title="Запуск действия с болванкой", access="run", category="blockout")
def blockout_run(project_id: str, action: str, shot_keys: Optional[list] = None, chain_id: Optional[str] = None,
                 launcher: str = "backend", expected_plan_digest: Optional[str] = None,
                 discard_unfinished: bool = False) -> dict:
    if action in _CONFIRM_ACTIONS and not (isinstance(expected_plan_digest, str) and expected_plan_digest.strip()):
        raise StudioError("invalid_argument", "Для этого действия нужен expected_plan_digest из blockout_plan (подтверждение)")
    req = _build_request(project_id, action, shot_keys, chain_id)
    errors = [p for p in req["problems"] if p["level"] == "error"]
    if errors:
        raise StudioError("conflict", errors[0]["message"], reason="plan_blocked", problems=errors)
    result = _call_runs("run_start")(project_id, launcher=launcher, expected_plan_digest=expected_plan_digest,
                                     discard_unfinished=discard_unfinished, **_run_kwargs(req))
    reset_caches()
    return result


# ------------------------------------------------------------------ прямые записи

@capability("blockout_set_duration", title="Длительность шота (болванка)", access="write", category="blockout")
def blockout_set_duration(project_id: str, shot_key: str, seconds: int, base_digest: str) -> dict:
    key, _ = _require_key(shot_key)
    if not _is_int(seconds):
        raise StudioError("invalid_argument", "seconds: нужно целое число секунд")
    if not isinstance(base_digest, str) or not base_digest.strip():
        raise StudioError("invalid_argument", "base_digest обязателен: возьмите его из blockout_detail")
    state = _build_state(project_id)
    row = _shot_row(state, key)
    allowed = state["durations"]["allowed"]
    if allowed and seconds not in allowed:
        raise StudioError("invalid_argument", f"Длительность {seconds} с не входит в допустимые: "
                          + ", ".join(map(str, allowed)), allowed=allowed)
    blockers = state["actions"]["set_duration"]["blockers"]
    if blockers:
        raise StudioError("locked", blockers[0]["message"], reason=blockers[0]["code"])
    if "set_duration" not in row["allowed"]:
        raise StudioError("locked", "У шота нет start-элемента: длительность править нельзя", reason="no_start_item")
    shots = _try_studio("shots")
    if shots is None or not hasattr(shots, "set_duration"):
        raise StudioError("unavailable", "Правка шотов недоступна")
    done = shots.set_duration(project_id, key, seconds, base_digest)
    reset_caches()
    fresh = _shot_row(_build_state(project_id), key)
    spec = row["scene_spec_duration_s"]
    return success(shot_key=key, duration_s=seconds, digest=done.get("digest"),
                   rebuild_needed=spec is not None and spec != seconds, state=fresh["state"], shot=fresh)


def _lock_error(root: Path) -> StudioError:
    control = _try_studio("run_control")
    if control is not None and hasattr(control, "locked_error"):
        try:
            return control.locked_error(root)
        except Exception:  # noqa: BLE001
            pass
    return StudioError("locked", "Идёт прогон — дождитесь конца")


def _acquire_lock(project_id: str):
    control = _try_studio("run_control")
    if control is not None and hasattr(control, "acquire_lock_with_retries"):
        return control.acquire_lock_with_retries(project_id)
    return project_paths.acquire_storybook_project_lock(project_id)


@capability("blockout_leave_as_is", title="Оставить картинки как есть", access="write", category="blockout")
def blockout_leave_as_is(project_id: str, shot_keys: list) -> dict:
    if not isinstance(shot_keys, list) or not shot_keys:
        raise StudioError("invalid_argument", "shot_keys: выберите хотя бы один шот")
    keys = _keys_list(shot_keys)
    root = project_dir(project_id)
    store = _studio("project_store")
    handle = _acquire_lock(project_id)
    if handle is None:
        raise _lock_error(root)
    changed: list[str] = []
    skipped: list[dict] = []
    try:
        for key in keys:
            scene, shot = S.parse_key(key)
            path = root / BLOCKOUT_DIR / S.shot_dir_name(scene, shot) / "manifest.json"
            try:  # под замком читаем заново, мимо кэша
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = None
            if not isinstance(manifest, dict):
                skipped.append({"key": key, "reason": "manifest_missing"})
            elif manifest.get("p10_acknowledged"):
                skipped.append({"key": key, "reason": "already_acknowledged"})
            elif not S.rendered_before_images(manifest, S.final_image_mtimes(root, scene, shot)):
                skipped.append({"key": key, "reason": "no_p10"})
            else:
                manifest["p10_acknowledged"] = S.utc_now_iso()
                store.write_json_atomic(path, manifest)
                changed.append(key)
    finally:
        project_paths.release_storybook_project_lock(handle)
        reset_caches()
    return success(changed=changed, skipped=skipped)
