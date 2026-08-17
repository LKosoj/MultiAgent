"""Э2: ``blockout_scene_builder`` — chains.json / scene_spec.json / asset_map.json.

Spec: docs/tz-blockout-reference-pipeline.md, разделы 7, 9.4, 10.0-10.4, 12,
20.1-20.3, Приложение А.

Contract (раздел 10.3.1): this tool always returns a DICT, never a JSON
string, and never a key ``status: "error"``/``error``/``exception`` on a
normal return. Blocking checks (B01, B02, B12, B14, B15) write a
``level: "error"`` record to their own ``report.json`` section FIRST, then
raise an exception — the engine turns that into a failed step. Everything
else degrades to a ``warning`` entry in the report and a non-blocking
``status: "warning"`` in the return value.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent_command import model_hard
from utils import call_openai_api, parse_llm_json

from custom_tools.storybook import blockout_assets
from custom_tools.storybook import blockout_common
from custom_tools.storybook.blockout_asset_fetch import fetch_and_register_asset
from custom_tools.storybook.project_paths import safe_storybook_project_dir
from custom_tools.storybook.video_generator_aitunnel_media import _infer_aspect_ratio, _resolve_size_params
from custom_tools.storybook.video_generator_common import resolve_effective_durations

logger = logging.getLogger(__name__)

DEFAULT_SENSOR_MM = 36.0
SPEC_VERSION = 1

# === camera_plan table (раздел 10.1) ========================================

CAMERA_PLAN_TABLE: Dict[str, Dict[str, Any]] = {
    "EXTREME LONG SHOT": {"lens_mm": 24.0, "fraction": 0.06, "dist_range_m": (25.0, 45.0)},
    "LONG SHOT": {"lens_mm": 28.0, "fraction": 0.15, "dist_range_m": (12.0, 20.0)},
    "WIDE SHOT": {"lens_mm": 28.0, "fraction": 0.25, "dist_range_m": (7.0, 12.0)},
    "TRACKING SHOT": {"lens_mm": 35.0, "fraction": 0.40, "dist_range_m": (6.0, 12.0), "moves_with_subject": True},
    "MEDIUM SHOT": {"lens_mm": 50.0, "fraction": 0.55, "dist_range_m": (4.0, 7.0), "s_m_override": 0.90},
    "MEDIUM CLOSE-UP": {"lens_mm": 65.0, "fraction": 0.70, "dist_range_m": (2.5, 4.0), "s_m_override": 0.55},
    "CLOSE-UP": {"lens_mm": 85.0, "fraction": 0.85, "dist_range_m": (1.2, 2.0), "s_m_override": 0.30},
    "EXTREME CLOSE-UP": {"lens_mm": 100.0, "fraction": 1.10, "dist_range_m": (0.6, 1.0), "s_m_override": 0.15},
    "LOW ANGLE": {"lens_mm": 35.0, "fraction": 0.60, "dist_range_m": (3.0, 6.0), "camera_z_m": 0.3, "tilt_up": True},
    "LOW SHOT": {"lens_mm": 35.0, "fraction": 0.60, "dist_range_m": (3.0, 6.0), "camera_z_m": 0.1},
    "POV": {"lens_mm": 35.0, "fraction": None, "dist_range_m": None, "pov": True},
    # "as MEDIUM CLOSE-UP" (раздел 10.1)
    "SPLIT-FRAME": {"lens_mm": 65.0, "fraction": 0.70, "dist_range_m": (2.5, 4.0), "s_m_override": 0.55},
}

_SEPARATOR_RE = re.compile(r"[—/]")
_POSITION_MODIFIER_RE = re.compile(r"\b(POV|ВНУТРИ|FROM)\b", re.IGNORECASE)
_SNAP_ZOOM_RE = re.compile(r"SNAP\s*ZOOM", re.IGNORECASE)
_SLOW_MOTION_RE = re.compile(r"SLOW\s*MOTION", re.IGNORECASE)

# Fallback camera direction used when neither the LLM spatial parse nor a
# cached track supplies one (deep-fallback path, section 10.3.1 "B08/B09/B10
# provalились -> объекты в начале координат, камера по таблице camera_plan").
# Matches the worked example's south-west-facing unit vector (раздел 15.2)
# so numbers stay sane rather than arbitrary.
_FALLBACK_CAMERA_DIRECTION = (0.70709, 0.70709, -0.0248)


# =============================================================================
# camera_plan parsing (раздел 10.1, "Разбор camera_plan" + "Правило отката")
# =============================================================================


def _match_table_base(text: str) -> Optional[str]:
    normalized = " ".join(text.split()).upper()
    return normalized if normalized in CAMERA_PLAN_TABLE else None


def parse_camera_plan(camera_plan: Any) -> Dict[str, Any]:
    """Splits ``camera_plan`` into base + modifier and resolves lens/distance
    parameters per the table and the four-branch fallback rule (раздел
    10.1). Never raises — an unparseable value degrades to MEDIUM SHOT with
    a P11 warning (criterion A20).
    """
    raw = str(camera_plan or "").strip()
    warnings: List[Dict[str, Any]] = []
    derived_from: List[str] = []

    match = _SEPARATOR_RE.search(raw)
    if match:
        base_part, modifier_part = raw[: match.start()].strip(), raw[match.end():].strip()
    else:
        base_part, modifier_part = raw, ""

    base_key = _match_table_base(base_part)
    if base_key is None:
        warnings.append({
            "code": "P11", "level": "warning",
            "message": f"unrecognized camera_plan base {base_part!r}; applied MEDIUM SHOT fallback",
            "details": {"camera_plan": raw},
        })
        derived_from.append("camera_plan:base_unrecognized")
        result = _plan_result("MEDIUM SHOT", False, modifier_part, CAMERA_PLAN_TABLE["MEDIUM SHOT"])
        result["llm_text"] = raw
        result["warnings"] = warnings
        result["derived_from"] = derived_from
        return result

    entry = CAMERA_PLAN_TABLE[base_key]
    result = _plan_result(base_key, True, modifier_part, entry)
    result["warnings"] = warnings
    result["derived_from"] = derived_from

    if not modifier_part:
        return result

    if _POSITION_MODIFIER_RE.search(modifier_part):
        result["position_override"] = modifier_part
        if re.search(r"\bPOV\b", modifier_part, re.IGNORECASE) or result["is_pov"]:
            result["is_pov"] = True
        result["fraction"] = None
        result["dist_range_m"] = None
        result["s_m_override"] = None
        warnings.append({
            "code": "note", "level": "info",
            "message": "camera_plan_conflict: position modifier overrides base distance",
            "details": {"camera_plan": raw},
        })
        derived_from.append("camera_plan_conflict")
        return result

    if result["is_pov"]:
        # base is POV: the modifier is the named subject, not noise.
        result["pov_subject_name"] = modifier_part
        derived_from.append("camera_plan:pov_subject")
        return result

    if _SNAP_ZOOM_RE.search(modifier_part):
        result["snap_zoom"] = True
        derived_from.append("camera_plan:snap_zoom")
        return result

    if _SLOW_MOTION_RE.search(modifier_part):
        result["slow_motion"] = True
        warnings.append({
            "code": "note", "level": "info", "message": "camera_plan:slow_motion_ignored",
            "details": {"camera_plan": raw},
        })
        derived_from.append("camera_plan:slow_motion_ignored")
        return result

    warnings.append({
        "code": "P11", "level": "warning",
        "message": f"unrecognized camera_plan modifier {modifier_part!r}; applied base only",
        "details": {"camera_plan": raw},
    })
    derived_from.append("camera_plan:modifier_unrecognized")
    result["llm_text"] = modifier_part
    return result


def _plan_result(base_key: str, base_recognized: bool, modifier: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "base": base_key,
        "base_recognized": base_recognized,
        "modifier": modifier,
        "lens_mm": entry["lens_mm"],
        "fraction": entry.get("fraction"),
        "dist_range_m": entry.get("dist_range_m"),
        "s_m_override": entry.get("s_m_override"),
        "position_override": None,
        "pov_subject_name": None,
        "is_pov": bool(entry.get("pov")),
        "camera_z_m": entry.get("camera_z_m"),
        "tilt_up": bool(entry.get("tilt_up")),
        "moves_with_subject": bool(entry.get("moves_with_subject")),
        "snap_zoom": False,
        "slow_motion": False,
        "llm_text": None,
    }


# =============================================================================
# report.json — sidecar-locked read-merge-write (раздел 20.3)
# =============================================================================


def merge_write_report(
    report_path: Path, section: str, update_fn: Callable[[Dict[str, Any]], Dict[str, Any]]
) -> Dict[str, Any]:
    """Read-merge-write one top-level section of ``93_blockout/report.json``.

    Mirrors ``screenplay_shots_generator._merge_write_shots()``: exclusive
    ``fcntl.flock`` on a SIDECAR file ``{report_path}.lock`` (never the
    target — ``os.replace()`` changes the inode, so flocking the target
    itself would let a second writer lock the stale inode), read-under-lock,
    merge, write to a UNIQUE temp file ``{path}.{pid}.tmp``, ``os.replace``,
    unlock. Other sections (other steps' data) are preserved untouched —
    this is NOT the broken pattern in ``shots_prompt_qa.py`` (flocks the
    target file itself, non-unique temp name).
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = report_path.with_name(report_path.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            report: Dict[str, Any] = {}
            if report_path.is_file():
                try:
                    loaded = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        report = loaded
                except (OSError, json.JSONDecodeError):
                    report = {}
            current_section = report.get(section)
            if not isinstance(current_section, dict):
                current_section = {}
            report[section] = update_fn(current_section)

            tmp_path = report_path.with_name(f"{report_path.name}.{os.getpid()}.tmp")
            try:
                tmp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_path, report_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            return report
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _create_empty_report(report_path: Path) -> None:
    """Creates ``93_blockout/report.json`` as ``{}`` — the very first thing
    this step does after the ``enable`` branch, before any blocking check
    (раздел 10.3.1)."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.is_file():
        return
    tmp_path = report_path.with_name(f"{report_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text("{}", encoding="utf-8")
    os.replace(tmp_path, report_path)


def _report_append_checks(report_path: Path, entries: Sequence[Dict[str, Any]]) -> None:
    if not entries:
        return

    def _update(section: Dict[str, Any]) -> Dict[str, Any]:
        section = dict(section)
        checks = list(section.get("checks") or [])
        checks.extend(entries)
        section["checks"] = checks
        return section

    merge_write_report(report_path, "blockout_scene_builder", _update)


# =============================================================================
# Zero contract (раздел 10.3.1)
# =============================================================================


def _zero_contract(project_id: str) -> Dict[str, Any]:
    # раздел 10.2 "Форма пути": the absolute path safe_storybook_project_dir()
    # resolves -- never the hardcoded 'plots/storybooks/...' literal, which
    # is wrong whenever STORYBOOK_PROJECTS_DIR is set.
    project_dir = safe_storybook_project_dir(project_id)
    return {
        "status": "skipped",
        "chains_total": 0,
        "shots_total": 0,
        "unmapped_assets": [],
        "artifact_path": str(project_dir / "93_blockout" / "chains.json"),
    }


# =============================================================================
# B02 / B12 — environment checks (раздел 6.3, 13.4)
# =============================================================================


def _validate_fps_is_integer(fps: Any) -> int:
    if isinstance(fps, bool):
        raise ValueError(f"blockout_fps must be an integer, got {fps!r}")
    if isinstance(fps, int):
        return fps
    if isinstance(fps, (float, str)):
        try:
            as_float = float(fps)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"blockout_fps must be an integer, got {fps!r}") from exc
        if as_float.is_integer():
            return int(as_float)
        raise ValueError(f"blockout_fps must be an integer, got {fps!r}")
    raise ValueError(f"blockout_fps must be an integer, got {fps!r}")


def _check_b02(report_path: Path, fps: Any) -> int:
    try:
        return _validate_fps_is_integer(fps)
    except ValueError as exc:
        _report_append_checks(report_path, [{"code": "B02", "level": "error", "message": str(exc)}])
        raise


def _check_b12(report_path: Path) -> None:
    available, version, message = blockout_common.check_blender_available()
    if not available:
        detail = f"B12: Blender not available: {message}"
        _report_append_checks(report_path, [{"code": "B12", "level": "error", "message": detail, "details": {"version": version}}])
        raise RuntimeError(detail)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _detect_duplicate_shot_numbers(scene_number: int, elements: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """B01a (раздел 7): the usual root cause of the B01 duration-mismatch
    symptom is two logically distinct shots sharing one ``shot_number`` in
    the same scene -- ``compute_chains`` groups by ``shot_number`` via a
    dict, so the second one's data is silently absorbed into the first
    instead of raising. This walks the scene's ``start`` elements in the
    same ascending order and applies the same new-chain rule
    ``compute_chains`` uses, but WITHOUT collapsing by shot_number first, so
    a collision still yields two distinct chain labels instead of vanishing.
    """
    starts = []
    for el in elements:
        if el.get("shot_type") != "start":
            continue
        try:
            shot_no = int(el.get("shot_number"))
        except (TypeError, ValueError):
            continue
        starts.append((shot_no, el))
    starts.sort(key=lambda pair: pair[0])

    labels_by_shot: Dict[int, List[str]] = {}
    chain_seq = 0
    is_first = True
    for shot_no, el in starts:
        link_type = str(el.get("link_type") or "").strip().lower()
        if is_first or link_type != "full_copy":
            chain_seq += 1
        is_first = False
        labels_by_shot.setdefault(shot_no, []).append(f"sc{scene_number:02d}_ch{chain_seq:02d}")

    return [
        {"scene_number": scene_number, "shot_number": shot_no, "chains": labels}
        for shot_no, labels in sorted(labels_by_shot.items())
        if len(labels) > 1
    ]


def _check_chain_integrity(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """B14: ``link_type`` must agree with ``source_end_path``/
    ``copy_from_previous_end`` on every shot's ``start`` element (раздел
    20.3: "целостность цепочек" is discovered by scene_builder before any
    render happens)."""
    violations: List[Dict[str, Any]] = []
    for item in items:
        if item.get("shot_type") != "start":
            continue
        link_type = str(item.get("link_type") or "").strip().lower()
        has_source = bool(item.get("source_end_path"))
        copy_flag = item.get("copy_from_previous_end")
        is_full_copy = link_type == "full_copy"
        if is_full_copy and not (has_source and copy_flag is True):
            violations.append({"scene_number": item.get("scene_number"), "shot_number": item.get("shot_number")})
        elif not is_full_copy and (has_source or copy_flag is True):
            violations.append({"scene_number": item.get("scene_number"), "shot_number": item.get("shot_number")})
    return violations


def _resolve_paths(project_id: str) -> Dict[str, Path]:
    project_dir = safe_storybook_project_dir(project_id)
    blockout_dir = project_dir / "93_blockout"
    return {
        "project_dir": project_dir,
        "shots": project_dir / "97_shots" / "shots.json",
        "caps": project_dir / "97_shots" / "video_model_caps.json",
        "screenplay": project_dir / "91_screenplay" / "screenplay.json",
        "characters": project_dir / "20_bible" / "characters.json",
        "locations": project_dir / "20_bible" / "locations.json",
        "chains": blockout_dir / "chains.json",
        "scene_spec": blockout_dir / "scene_spec.json",
        "asset_map": blockout_dir / "asset_map.json",
        "report": blockout_dir / "report.json",
    }


def _report_finalize(report_path: Path, summary: Dict[str, Any]) -> None:
    """Replaces the whole ``blockout_scene_builder`` section with a fresh
    summary (this step has no ``scope`` parameter, so a successful run
    always rebuilds its entire section, discarding stale ``checks`` from a
    previous failed attempt) — other steps' sections are untouched."""
    merge_write_report(report_path, "blockout_scene_builder", lambda _section: dict(summary))


# =============================================================================
# Main entrypoint (раздел 10.0-10.4)
# =============================================================================


def blockout_scene_builder_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    fps: Any = 24,
    allowed_durations: Optional[List[int]] = None,
    resolution: str = "1280x720",
    enable: bool = True,
) -> Dict[str, Any]:
    """Э2 main entrypoint. Always returns a dict, never a JSON string, and
    never ``status: "error"``/``error``/``exception`` (раздел 10.3.1).
    Blocking checks (B01, B02, B12, B14, B15) write an error record to their
    own ``report.json`` section, then raise — everything else degrades to a
    ``status: "warning"`` entry.
    """
    del session_id  # standard tool signature; not otherwise used here

    if not _as_bool(enable):
        try:
            return _zero_contract(project_id)
        except Exception:  # noqa: BLE001
            # раздел 10.3.1 is unconditional for the disabled layer: an
            # invalid project_id must still degrade to status="skipped",
            # never raise -- unlike the B* blocking checks below, which are
            # allowed to raise but only run once the layer is enabled.
            return {
                "status": "skipped",
                "chains_total": 0,
                "shots_total": 0,
                "unmapped_assets": [],
                "artifact_path": "",
            }

    paths = _resolve_paths(project_id)
    _create_empty_report(paths["report"])

    fps_int = _check_b02(paths["report"], fps)
    _check_b12(paths["report"])

    shots_data = _read_json(paths["shots"], default={"items": []})
    caps_data = _read_json(paths["caps"], default={})
    screenplay_data = _read_json(paths["screenplay"], default={"screenplay": []})
    characters = _read_json(paths["characters"], default=[])
    locations = _read_json(paths["locations"], default=[])

    checks: List[Dict[str, Any]] = []
    items = shots_data.get("items") or []

    supported_durations = list(caps_data.get("supported_durations") or [])
    if not supported_durations:
        entry = {
            "code": "B15", "level": "error",
            "message": "video_model_caps.json has no supported_durations; rerun screenplay_shots_generator",
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B15: " + entry["message"])

    effective_durations, duration_warnings = resolve_effective_durations(supported_durations, allowed_durations)
    checks.extend(duration_warnings)

    shots_by_key: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for item in items:
        try:
            key = (int(item.get("scene_number")), int(item.get("shot_number")))
        except (TypeError, ValueError):
            continue
        shots_by_key.setdefault(key, []).append(item)

    scenes_by_number: Dict[int, List[Dict[str, Any]]] = {}
    for item in items:
        try:
            scene_no = int(item.get("scene_number"))
        except (TypeError, ValueError):
            continue
        scenes_by_number.setdefault(scene_no, []).append(item)

    b01a_duplicates: List[Dict[str, Any]] = []
    for scene_no, elements in scenes_by_number.items():
        b01a_duplicates.extend(_detect_duplicate_shot_numbers(scene_no, elements))
    if b01a_duplicates:
        first = b01a_duplicates[0]
        chains_str = ", ".join(first["chains"])
        message = (
            f"duplicate shot_number in scene={first['scene_number']}: shot={first['shot_number']} "
            f"in chains [{chains_str}]; each shot_number must be unique within a scene. "
            "Use shot_offset when splitting a scene across chains."
        )
        entry = {"code": "B01a", "level": "error", "message": message, "details": {"duplicates": b01a_duplicates}}
        _report_append_checks(paths["report"], [entry])
        raise ValueError("B01a: " + message)

    b01_missing: List[Dict[str, Any]] = []
    b01_invalid: List[Dict[str, Any]] = []
    effective_set = {int(v) for v in effective_durations}
    for (scene_no, shot_no), elements in sorted(shots_by_key.items()):
        duration = next((e.get("duration_s") for e in elements if e.get("duration_s") is not None), None)
        if duration is None:
            b01_missing.append({"scene_number": scene_no, "shot_number": shot_no})
            continue
        try:
            in_set = int(duration) in effective_set
        except (TypeError, ValueError):
            in_set = False
        if not in_set:
            b01_invalid.append({"scene_number": scene_no, "shot_number": shot_no, "duration_s": duration})

    if b01_missing or b01_invalid:
        entry = {
            "code": "B01", "level": "error",
            "message": "duration_s missing or not within the allowed set for one or more shots; rerun screenplay_shots_generator",
            "details": {"missing": b01_missing, "invalid": b01_invalid},
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B01: " + entry["message"])

    b14_violations = _check_chain_integrity(items)
    if b14_violations:
        entry = {
            "code": "B14", "level": "error",
            "message": "link_type inconsistent with source_end_path/copy_from_previous_end",
            "details": {"shots": b14_violations},
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B14: " + entry["message"])

    all_chains: List[blockout_common.Chain] = []
    for scene_no in sorted(scenes_by_number):
        scene_chains, chain_warnings = blockout_common.compute_chains(scene_no, scenes_by_number[scene_no])
        all_chains.extend(scene_chains)
        for message in chain_warnings:
            checks.append({"code": "note", "level": "warning", "message": message})

    blockout_common.write_chains_json(paths["chains"], all_chains)

    existing_asset_map = _read_json(paths["asset_map"], default=None)
    asset_map, asset_warnings = build_asset_map(existing_asset_map, characters, locations, language)
    checks.extend(asset_warnings)
    _write_json_atomic(paths["asset_map"], asset_map)
    unmapped_assets = list(asset_map.get("unmapped") or [])

    existing_scene_spec = _read_json(paths["scene_spec"], default=None)
    screenplay_text = json.dumps(screenplay_data.get("screenplay") or [], ensure_ascii=False)[:4000]
    try:
        scene_spec = build_scene_spec(
            all_chains, shots_by_key, asset_map, fps_int, caps_data, resolution, screenplay_text,
            existing_scene_spec, checks, project_id=project_id,
        )
    except ValueError as exc:
        # subject_focus/visible-track/asset-resolution validation (SUBJ_INVALID,
        # VIS_INVALID, ASSET_MISSING) raises deep inside build_scene_spec with no
        # access to paths["report"] -- caught here once so it still lands in
        # report.json before propagating, same contract as the B* checks above.
        _report_append_checks(paths["report"], [{"code": "SPEC_INVALID", "level": "error", "message": str(exc)}])
        raise
    _write_json_atomic(paths["scene_spec"], scene_spec)

    # video_model_caps.json warnings are read only here, at finalization
    # (раздел 20.3) — never at report creation, never folded back into
    # video_model_caps.json itself.
    for w in caps_data.get("warnings") or []:
        if isinstance(w, dict):
            checks.append(dict(w))

    chains_total = len(all_chains)
    shots_total = len(shots_by_key)
    # B01/B02/B12/B14/B15 already raised above if triggered, so every entry
    # remaining in ``checks`` here is non-blocking (раздел 10.3.1).
    status = "warning" if (checks or unmapped_assets) else "success"

    summary = {
        "status": status,
        "chains_total": chains_total,
        "shots_total": shots_total,
        "unmapped_assets": unmapped_assets,
        "checks": checks,
    }
    _report_finalize(paths["report"], summary)

    return {
        "status": status,
        "chains_total": chains_total,
        "shots_total": shots_total,
        "unmapped_assets": unmapped_assets,
        "artifact_path": f"plots/storybooks/{project_id}/93_blockout/chains.json",
    }


# =============================================================================
# Asset matching (раздел 9.4)
# =============================================================================


def llm_resolve_asset(display_name: str, description_context: str, language: str) -> Optional[Dict[str, Any]]:
    """One LLM call matching a character/location key_object name to a
    library object (раздел 9.4, пункт 2). Never raises: returns ``None`` on
    any failure (network, non-JSON, missing required fields). Module-level
    so tests can monkeypatch it directly."""
    system_prompt = (
        "You match a fairy-tale character or location object description to a 3D "
        "blockout asset library. Respond with ONLY a JSON object with exactly these "
        "keys: asset_id (string matching an existing library id, or null), "
        "name (short human-readable English name for meta.json), "
        "search_query (1-3 English words for an external asset search, used only "
        "when asset_id is null), "
        f"category (one of {list(blockout_assets.CATEGORIES)}), "
        "height_m (number: height in meters for a biped/quadruped, or size in "
        "meters otherwise), "
        "body_plan (one of \"biped\", \"quadruped\", \"none\")."
    )
    user_prompt = json.dumps(
        {"name": display_name, "description": description_context, "language": language},
        ensure_ascii=False,
    )
    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=400,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        parsed = parse_llm_json(response)
    except Exception:  # noqa: BLE001 - asset matching must never raise into the caller
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if "category" not in parsed or "body_plan" not in parsed:
        return None
    return parsed


def _normalize_body_plan(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("biped", "quadruped", "none") else "none"


def _proxy_id_for_body_plan(body_plan: str) -> str:
    return {
        "biped": blockout_assets.PROXY_BIPED,
        "quadruped": blockout_assets.PROXY_QUADRUPED,
    }.get(body_plan, blockout_assets.PROXY_BOX)


def _match_new_entry(
    *,
    display_name: str,
    description_context: str,
    default_category: str,
    default_body_plan: str,
    index: Dict[str, Any],
    language: str,
    assets_root: Optional[Path],
    warnings: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """Steps 1-4 of раздел 9.4 for one name with no existing asset_map record.
    Returns (entry, is_unmapped)."""
    # step 1: exact normalized-name match
    hit = blockout_assets.find_asset_by_name(index, display_name)
    if hit is not None:
        return {
            "name": display_name,
            "asset_id": hit["id"],
            "scale": 1.0,
            "height_m": float(hit["dimensions_m"][2]),
        }, False

    # step 2: one LLM call
    llm_result = llm_resolve_asset(display_name, description_context, language)
    body_plan = default_body_plan
    category = default_category
    height_m: Optional[float] = None
    search_query: Optional[str] = None
    friendly_name = display_name

    if llm_result is None:
        warnings.append({
            "code": "P01", "level": "warning",
            "message": f"LLM asset match call failed for {display_name!r}",
            "details": {"name": display_name},
        })
    else:
        body_plan = _normalize_body_plan(llm_result.get("body_plan"))
        category = llm_result.get("category") or default_category
        if category not in blockout_assets.CATEGORIES:
            category = default_category
        raw_height = llm_result.get("height_m")
        if isinstance(raw_height, (int, float)) and not isinstance(raw_height, bool) and raw_height > 0:
            height_m = float(raw_height)
        search_query = llm_result.get("search_query") or None
        friendly_name = llm_result.get("name") or display_name

        asset_id = llm_result.get("asset_id")
        if asset_id:
            hit2 = blockout_assets.find_asset_by_id(index, asset_id)
            if hit2 is not None:
                return {
                    "name": display_name,
                    "asset_id": hit2["id"],
                    "scale": 1.0,
                    "height_m": float(hit2["dimensions_m"][2]),
                    "body_plan": body_plan,
                }, False

    # step 3: automatic search + download
    if search_query:
        fetch_result = fetch_and_register_asset(
            search_query=search_query,
            category=category,
            display_name=friendly_name,
            body_plan=body_plan,
            assets_root=assets_root,
        )
        if fetch_result.ok and fetch_result.asset_id:
            refreshed_index = blockout_assets.read_index(assets_root)
            hit3 = blockout_assets.find_asset_by_id(refreshed_index, fetch_result.asset_id)
            if hit3 is not None:
                return {
                    "name": display_name,
                    "asset_id": hit3["id"],
                    "scale": 1.0,
                    "height_m": float(hit3["dimensions_m"][2]),
                    "body_plan": body_plan,
                }, False
        elif fetch_result.reason:
            warnings.append({
                "code": "P01", "level": "warning",
                "message": f"asset search/download failed for {display_name!r}: {fetch_result.reason}",
                "details": {"name": display_name, "reason": fetch_result.reason},
            })

    # step 4: proxy placeholder
    if height_m is None:
        height_m = blockout_assets.typical_height_m(category)
    proxy_id = _proxy_id_for_body_plan(body_plan)
    warnings.append({
        "code": "P01", "level": "warning",
        "message": f"no library object matched for {display_name!r}; substituted proxy {proxy_id}",
        "details": {"name": display_name, "asset_id": proxy_id},
    })
    return {
        "name": display_name,
        "asset_id": proxy_id,
        "scale": 1.0,
        "height_m": float(height_m),
        "body_plan": body_plan,
    }, True


def _reconcile_existing(
    entry: Dict[str, Any],
    index: Dict[str, Any],
    warnings: List[Dict[str, Any]],
    *,
    label: str,
    is_character: bool,
) -> Dict[str, Any]:
    """Read-time reconciliation of one pre-existing asset_map record (раздел
    9.4, "Правило дочитывания"): unchanged unless (1) asset_id vanished from
    the index (replaced by a proxy) or (2/3) the height<->scale recompute
    applies to a record with a real (non-proxy) asset_id."""
    entry = dict(entry)
    asset_id = entry.get("asset_id")
    if blockout_assets.is_proxy_asset_id(asset_id):
        return entry

    hit = blockout_assets.find_asset_by_id(index, asset_id)
    if hit is None:
        body_plan_raw = entry.get("body_plan")
        body_plan = _normalize_body_plan(body_plan_raw) if body_plan_raw else ("biped" if is_character else "none")
        height_m = entry.get("height_m")
        if not (isinstance(height_m, (int, float)) and height_m > 0):
            height_m = blockout_assets.typical_height_m("character" if is_character else "prop")
        proxy_id = _proxy_id_for_body_plan(body_plan)
        warnings.append({
            "code": "P01", "level": "warning",
            "message": f"asset_id {asset_id!r} for {label!r} no longer in index; substituted proxy {proxy_id}",
            "details": {"name": label, "asset_id": proxy_id},
        })
        entry["asset_id"] = proxy_id
        entry["scale"] = 1.0
        entry["height_m"] = float(height_m)
        if body_plan_raw:
            entry["body_plan"] = body_plan
        return entry

    if is_character:
        # exception 1: scale recomputed from height_m
        height_m = entry.get("height_m")
        if isinstance(height_m, (int, float)) and height_m > 0:
            try:
                entry["scale"] = blockout_assets.compute_scale(hit, float(height_m))
            except ValueError:
                entry["scale"] = 1.0
    else:
        # exception 2: height_m recomputed from dimensions_m[2], scale stays 1.0
        entry["height_m"] = float(hit["dimensions_m"][2])
        entry["scale"] = 1.0
    return entry


def build_asset_map(
    existing: Optional[Dict[str, Any]],
    characters: Sequence[Dict[str, Any]],
    locations: Sequence[Dict[str, Any]],
    language: str,
    assets_root: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Builds/updates ``asset_map.json`` (раздел 9.4). Returns
    ``(asset_map, warnings)``. Existing records are never modified except
    the three documented exceptions inside ``_reconcile_existing``."""
    existing = existing or {}
    warnings: List[Dict[str, Any]] = []
    index = blockout_assets.read_index(assets_root)

    existing_chars = {e.get("name"): e for e in (existing.get("characters") or []) if isinstance(e, dict)}
    existing_locs = {
        (e.get("location"), e.get("key_object")): e
        for e in (existing.get("locations") or [])
        if isinstance(e, dict)
    }

    out_chars: List[Dict[str, Any]] = []
    unmapped: List[str] = []
    for char in characters:
        name = char.get("name")
        if not name:
            continue
        if name in existing_chars:
            entry = _reconcile_existing(existing_chars[name], index, warnings, label=name, is_character=True)
        else:
            entry, is_unmapped = _match_new_entry(
                display_name=name,
                description_context=_character_description(char),
                default_category="character",
                default_body_plan="biped",
                index=index,
                language=language,
                assets_root=assets_root,
                warnings=warnings,
            )
            if is_unmapped:
                unmapped.append(name)
        out_chars.append(entry)

    out_locs: List[Dict[str, Any]] = []
    for loc in locations:
        loc_name = loc.get("name")
        for key_object in loc.get("key_objects") or []:
            key = (loc_name, key_object)
            if key in existing_locs:
                entry = _reconcile_existing(
                    existing_locs[key], index, warnings, label=f"{loc_name} / {key_object}", is_character=False
                )
            else:
                entry, is_unmapped = _match_new_entry(
                    display_name=key_object,
                    description_context=f"{loc_name}: {key_object}. {loc.get('description', '')}",
                    default_category="prop",
                    default_body_plan="none",
                    index=index,
                    language=language,
                    assets_root=assets_root,
                    warnings=warnings,
                )
                entry["location"] = loc_name
                entry["key_object"] = key_object
                if is_unmapped:
                    unmapped.append(f"{loc_name} / {key_object}")
            out_locs.append(entry)

    asset_map = {"characters": out_chars, "locations": out_locs, "unmapped": unmapped}
    return asset_map, warnings


def _character_description(char: Dict[str, Any]) -> str:
    parts = [
        str(char.get("role") or ""),
        json.dumps(char.get("immutable_attributes") or {}, ensure_ascii=False),
        json.dumps(char.get("variable_attributes") or {}, ensure_ascii=False),
    ]
    return " ".join(p for p in parts if p and p != "{}")


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# =============================================================================
# scene_spec.json assembly (раздел 12)
# =============================================================================

_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate(text: str) -> str:
    out = []
    for ch in text.lower():
        out.append(_TRANSLIT_MAP.get(ch, ch))
    return "".join(out)


def _slug_instance_id(text: str, used: Dict[str, int]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", _transliterate(str(text or "").strip())).strip("_") or "object"
    if slug not in used:
        used[slug] = 1
        return slug
    used[slug] += 1
    return f"{slug}_{used[slug]}"


def resolve_camera_aspect(
    first_start_element: Optional[Dict[str, Any]], video_caps: Dict[str, Any], blockout_resolution: str
) -> Tuple[str, List[Dict[str, Any]]]:
    """Chain camera aspect ratio (раздел 10.1, "Ведущий столбец"). Returns
    ``(aspect_ratio_string, warnings)`` — warnings are only the
    ``shot_size_missing`` form of P13, which THIS step owns (раздел 20.3);
    the other P13 forms belong to ``blockout_renderer`` and are not raised
    here."""
    width = (first_start_element or {}).get("width")
    height = (first_start_element or {}).get("height")
    valid = (
        isinstance(width, (int, float)) and not isinstance(width, bool)
        and isinstance(height, (int, float)) and not isinstance(height, bool)
        and width > 0 and height > 0
    )
    if not valid:
        return _aspect_from_resolution_string(blockout_resolution), [{
            "code": "P13", "level": "warning", "reason": "shot_size_missing",
            "message": "first shot of chain has no width/height; camera aspect taken from blockout_resolution",
        }]

    result = _resolve_size_params(video_caps, int(width), int(height))
    if result is None:
        return _aspect_from_resolution_string(blockout_resolution), []
    if "aspect_ratio" in result:
        return result["aspect_ratio"], []
    inferred = _infer_aspect_ratio(int(width), int(height))
    return inferred or _aspect_from_resolution_string(blockout_resolution), []


def _aspect_from_resolution_string(resolution: str) -> str:
    match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(resolution or ""))
    if not match:
        return "16:9"
    w, h = int(match.group(1)), int(match.group(2))
    if w <= 0 or h <= 0:
        return "16:9"
    from math import gcd

    divisor = gcd(w, h)
    return f"{w // divisor}:{h // divisor}"


def _resolve_main_subject(
    shot_elements: Sequence[Dict[str, Any]],
    name_to_instance: Dict[str, str],
    chain_character_instance_ids: Sequence[str],
    chain_object_instance_ids: Sequence[str],
) -> Optional[str]:
    """раздел 10.1, "Главный объект шота"."""
    raw = ""
    for el in shot_elements:
        raw = str(el.get("main_subject") or "").strip()
        if raw:
            break
    if raw:
        matched = name_to_instance.get(blockout_assets.normalize_match_name(raw))
        if matched:
            return matched

    shot_character_names: List[str] = []
    for el in shot_elements:
        for name in el.get("characters") or []:
            if name not in shot_character_names:
                shot_character_names.append(name)
    matched_chars = [
        name_to_instance[blockout_assets.normalize_match_name(n)]
        for n in shot_character_names
        if blockout_assets.normalize_match_name(n) in name_to_instance
    ]
    if len(matched_chars) == 1:
        return matched_chars[0]

    if chain_character_instance_ids:
        return chain_character_instance_ids[0]
    if chain_object_instance_ids:
        return chain_object_instance_ids[0]
    return None


def llm_parse_chain_spatial(chain_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One LLM call resolving object/camera tracks for one chain (раздел
    10.1, "Контракт LLM-разбора", источник 3). Returns
    ``{"objects": [...], "camera": {"tracks": {...}}}`` or ``None`` on
    failure/malformed JSON. Retry-once orchestration lives in the caller;
    this makes exactly one HTTP call. Module-level so tests can monkeypatch
    it directly."""
    system_prompt = (
        "You are a 3D layout assistant for a Blender blockout previz. Given the "
        "shots of one continuous camera take (a 'chain'), its objects and their "
        "sizes, and the screenplay text, output ONLY a JSON object: "
        "{\"objects\": [{\"instance_id\", \"tracks\": {\"location\": "
        "[{\"t\", \"v\": [x,y,z], \"interp\"}], \"rotation_deg\": [...]}, "
        "\"animation\": {\"clip\", \"phase_at_t0\", \"speed\", \"loop\"}}], "
        "\"camera\": {\"tracks\": {\"lens_mm\": [{\"t\", \"v\", \"interp\"}], "
        "\"location\": [{\"t\", \"v\": [x,y,z], \"interp\"}], "
        "\"look_at\": [{\"t\", \"v\": [x,y,z], \"interp\"}]}}}. "
        "interp is one of linear/constant/bezier/ease_in/ease_out/ease_in_out. "
        "All positions are meters; all t values are seconds from the start of the "
        "chain; every track must cover [0, total_duration_s] of the chain."
    )
    user_prompt = json.dumps(chain_payload, ensure_ascii=False, default=str)
    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=2000,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        parsed = parse_llm_json(response)
    except Exception:  # noqa: BLE001 - spatial parse must never raise into the caller
        return None
    if not isinstance(parsed, dict) or "objects" not in parsed or "camera" not in parsed:
        return None
    return parsed


def _snap_track_times(keys: Any, fps: int) -> List[Dict[str, Any]]:
    """Snaps every key's ``t`` to the frame grid (раздел 10.1, "валидация"),
    then collapses keys that landed on the same frame (last by original ``t``
    wins)."""
    if not isinstance(keys, list):
        return []
    snapped = []
    for key in keys:
        if not isinstance(key, dict) or "t" not in key or "v" not in key:
            continue
        try:
            t = blockout_common.snap_to_frame_grid(float(key["t"]), fps)
        except (TypeError, ValueError):
            continue
        snapped.append({"t": t, "v": key["v"], "interp": key.get("interp", "linear")})
    snapped.sort(key=lambda k: k["t"])
    deduped: Dict[float, Dict[str, Any]] = {}
    for key in snapped:
        deduped[key["t"]] = key  # last wins
    return [deduped[t] for t in sorted(deduped)]


def _validate_chain_tracks(objects: Any, camera: Any, total_duration_s: float, fps: int) -> Optional[str]:
    """Returns ``None`` if the parsed LLM chain response is valid, else the
    report code of the first check it fails: ``"B08"`` (track_covers_range),
    ``"B09"`` (track_keys_on_frame_grid), or the catch-all ``"note"`` for
    structurally malformed track data (missing/wrong-typed fields, which
    isn't a B08/B09 failure of its own — раздел 20.3)."""
    if not isinstance(objects, list) or not isinstance(camera, dict):
        return "note"
    camera_tracks = camera.get("tracks") or {}
    for track_name in ("location", "look_at", "lens_mm"):
        keys = camera_tracks.get(track_name)
        if not isinstance(keys, list) or not keys:
            return "note"
        if not blockout_common.track_covers_range(keys, total_duration_s):
            return "B08"
        if not blockout_common.track_keys_on_frame_grid(keys, fps):
            return "B09"
    for obj in objects:
        if not isinstance(obj, dict):
            return "note"
        loc_keys = (obj.get("tracks") or {}).get("location")
        if not isinstance(loc_keys, list) or not loc_keys:
            return "note"
        if not blockout_common.track_covers_range(loc_keys, total_duration_s):
            return "B08"
        if not blockout_common.track_keys_on_frame_grid(loc_keys, fps):
            return "B09"
    return None


def _ensure_track_coverage(keys: List[Dict[str, Any]], total_duration_s: float) -> List[Dict[str, Any]]:
    total_duration_s = float(total_duration_s)
    if not keys:
        return [{"t": 0.0, "v": 0.0, "interp": "constant"}]
    keys = sorted(keys, key=lambda k: k["t"])
    if keys[0]["t"] > blockout_common.FRAME_GRID_TOLERANCE:
        keys.insert(0, {"t": 0.0, "v": keys[0]["v"], "interp": keys[0].get("interp", "linear")})
    if abs(keys[-1]["t"] - total_duration_s) > blockout_common.FRAME_GRID_TOLERANCE:
        keys.append({"t": total_duration_s, "v": keys[-1]["v"], "interp": keys[-1].get("interp", "linear")})
    return keys


def _static_fallback_tracks(total_duration_s: float, instance_ids: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """B08/B09/B10 provalились (or the LLM never answered): objects placed
    motionless at the origin, camera assembled purely from the camera_plan
    table (раздел 10.3.1)."""
    objects = [
        {
            "instance_id": instance_id,
            "tracks": {
                "location": [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}],
                "rotation_deg": [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}],
            },
        }
        for instance_id in instance_ids
    ]
    camera = {
        "tracks": {
            "lens_mm": [{"t": 0.0, "v": 50.0, "interp": "constant"}],
            "location": [{"t": 0.0, "v": list(_FALLBACK_CAMERA_DIRECTION), "interp": "constant"}],
            "look_at": [{"t": 0.0, "v": [0.0, 0.0, 1.5], "interp": "constant"}],
        }
    }
    return objects, camera


def _apply_camera_boundary_overrides(
    camera_tracks: Dict[str, List[Dict[str, Any]]],
    boundary_points: Sequence[Tuple[float, Dict[str, Any], Optional[float], Optional[Tuple[float, float, float]]]],
    h_sensor_mm: float,
    ground: Dict[str, Any],
) -> None:
    """Overrides ``location``/``lens_mm`` keys at chain-boundary points per
    the table rule (раздел 10.1, "camera_plan задаёт цель, а не мгновенное
    состояние"). ``boundary_points`` items are
    ``(t, plan, main_subject_height_m, main_subject_location)``. POV and
    position-conflict shots are excluded from this recompute (their location
    is set separately by the caller for POV, or left as authored for
    position-conflict, раздел 10.1).

    ``ground`` (the chain's ``world.ground`` block) supplies the single hard
    height boundary (раздел 10.1, "Единственная жёсткая граница по высоте"):
    a computed ``z`` below ``ground.z`` while ``ground.enabled`` is raised to
    ``ground.z``, and the horizontal component of the location vector is
    lengthened so ``|location - look_at|`` still equals the computed
    distance.
    """
    location_keys = list(camera_tracks.get("location") or [])
    lens_keys = list(camera_tracks.get("lens_mm") or [])
    look_at_keys = camera_tracks.get("look_at") or [{"t": 0.0, "v": [0.0, 0.0, 1.5], "interp": "constant"}]
    ground_enabled = bool((ground or {}).get("enabled", True))
    ground_z = float((ground or {}).get("z", 0.0))

    for t, plan, subject_height_m, subject_location in boundary_points:
        if plan.get("is_pov") or plan.get("position_override"):
            continue
        fraction = plan.get("fraction")
        if not fraction:
            continue
        s_m = plan.get("s_m_override")
        if s_m is None:
            s_m = subject_height_m if subject_height_m else 1.70

        look_at_point = blockout_common.evaluate_track_vec3(look_at_keys, t)
        try:
            prior_location = blockout_common.evaluate_track_vec3(location_keys, t) if location_keys else _FALLBACK_CAMERA_DIRECTION
        except Exception:  # noqa: BLE001 - malformed model track, use canonical default
            prior_location = _FALLBACK_CAMERA_DIRECTION
        direction = tuple(prior_location[i] - look_at_point[i] for i in range(3))
        norm = sum(c * c for c in direction) ** 0.5
        unit = direction if norm <= 1e-9 else tuple(c / norm for c in direction)
        if unit == (0.0, 0.0, 0.0):
            unit = _FALLBACK_CAMERA_DIRECTION

        distance = blockout_common.camera_distance_m(float(plan["lens_mm"]), float(s_m), float(fraction), h_sensor_mm)
        new_location = [look_at_point[i] + unit[i] * distance for i in range(3)]

        if ground_enabled and new_location[2] < ground_z:
            dz = ground_z - look_at_point[2]
            horiz_len_sq = distance * distance - dz * dz
            horiz_len = math.sqrt(horiz_len_sq) if horiz_len_sq > 0.0 else 0.0
            horiz_norm = math.hypot(unit[0], unit[1])
            hx, hy = (unit[0] / horiz_norm, unit[1] / horiz_norm) if horiz_norm > 1e-9 else (1.0, 0.0)
            new_location = [
                look_at_point[0] + hx * horiz_len,
                look_at_point[1] + hy * horiz_len,
                ground_z,
            ]

        location_keys = [k for k in location_keys if abs(k["t"] - t) > blockout_common.FRAME_GRID_TOLERANCE]
        location_keys.append({"t": t, "v": new_location, "interp": "bezier"})
        lens_keys = [k for k in lens_keys if abs(k["t"] - t) > blockout_common.FRAME_GRID_TOLERANCE]
        lens_keys.append({"t": t, "v": float(plan["lens_mm"]), "interp": "linear"})

    location_keys.sort(key=lambda k: k["t"])
    lens_keys.sort(key=lambda k: k["t"])
    camera_tracks["location"] = location_keys
    camera_tracks["lens_mm"] = lens_keys


def build_world_block(object_heights_m: Sequence[float]) -> Dict[str, Any]:
    """раздел 12 / 10.1 "Откуда берётся world": one ``world`` block per
    chain — ``sun`` is the constant project-wide direction (45.0/225.0
    degrees), ``ground`` is a plane at ``z: 0.0``. ``ground.size_m``
    approximates "4x the largest horizontal dimension across scene objects,
    minimum 400" using ``height_m`` as a stand-in for horizontal extent —
    the true ``dimensions_m[0]``/``[1]`` values are not threaded through to
    this step for proxy placeholders (documented deviation, see final
    report)."""
    largest = max(object_heights_m, default=0.0)
    return {
        "ground": {"enabled": True, "size_m": max(400.0, 4.0 * largest), "z": 0.0},
        "sun": {"elevation_deg": 45.0, "azimuth_deg": 225.0},
    }


def _parse_resolution_wh(resolution: str) -> List[int]:
    """``"1280x720"`` -> ``[1280, 720]`` for the ``resolution`` root field
    (раздел 12: "запрошенное разрешение"). Falls back to ``[1280, 720]``,
    same default used elsewhere for an unparsable ``blockout_resolution``."""
    match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(resolution or ""))
    if not match:
        return [1280, 720]
    w, h = int(match.group(1)), int(match.group(2))
    if w <= 0 or h <= 0:
        return [1280, 720]
    return [w, h]


def _proxy_bounding_box_m(body_plan: str, height_m: float) -> List[float]:
    """Coarse ``[w, d, h]`` bounding box for a single-primitive proxy render
    (P1.9). ``blockout_assets.proxy_part_specs`` builds the detailed
    multi-part figure for the renderer's own placeholder path; this is a
    cheaper box-only estimate so ``render_shot`` doesn't have to re-derive a
    bounding box from ``body_plan`` itself."""
    h = float(height_m)
    if body_plan == "biped":
        footprint = h * (0.5 / 1.8)  # width:height ratio of a typical registered character asset
        return [round(footprint, 3), round(footprint, 3), round(h, 3)]
    if body_plan == "quadruped":
        return [round(0.5 * h, 3), round(1.4 * h, 3), round(h, 3)]
    return [round(h, 3), round(h, 3), round(h, 3)]


def _resolve_object_asset(
    obj: Dict[str, Any],
    *,
    asset_id: Optional[str],
    category_default: str,
    body_plan: str,
    height_m: Optional[float],
    library_index: Dict[str, Any],
    chain_id: str,
) -> None:
    """P1.9: attaches ``asset_id``/``category`` to a scene_spec object. A
    reserved proxy asset_id (раздел 9.4) never has a backing file by design
    — it always degrades to an explicit ``proxy_placeholder`` marker
    (dimensions_m + a WARNING, not an error) so render_shot can draw a
    colored primitive without re-deriving a bounding box from ``body_plan``
    itself. A real, non-proxy asset_id is attached as-is without touching
    the filesystem: ``blockout_assets.resolve_asset_file_path`` is the
    actual load-time existence gate, and it belongs to the renderer/asset
    layer, not to spec assembly here.
    """
    if not asset_id:
        logger.warning(
            "chain %s instance %s: no asset_id resolved (unmapped instance or hallucinated id); "
            "object left without asset_id/category/proxy marker",
            chain_id, obj.get("instance_id"),
        )
        return
    if blockout_assets.is_proxy_asset_id(asset_id):
        dims = _proxy_bounding_box_m(body_plan, height_m or blockout_assets.typical_height_m(category_default))
        obj["asset_id"] = asset_id
        obj["category"] = category_default
        obj["proxy"] = True
        obj["placeholder"] = True
        obj["dimensions_m"] = dims
        logger.warning(
            "chain %s instance %s: proxy asset %s has no backing model file; placeholder dimensions_m=%s",
            chain_id, obj.get("instance_id"), asset_id, dims,
        )
        return

    hit = blockout_assets.find_asset_by_id(library_index, asset_id)
    if hit is None:
        logger.warning(
            "chain %s instance %s: asset_id %s not found in library_index; falling back to category_default=%s",
            chain_id, obj.get("instance_id"), asset_id, category_default,
        )
    obj["asset_id"] = asset_id
    obj["category"] = (hit.get("category") if hit else None) or category_default


def _validate_visible_track(keys: Any, total_duration_s: float, chain_id: str, instance_id: Any) -> None:
    """Optional bool ``visible`` track (раздел 12, "Требования к трекам").
    Unlike ``location``/``rotation_deg`` it is never auto-extended to cover
    the full range — a single key at ``t=0`` with ``interp: "constant"``
    already means "visible the whole chain" per spec — so this only
    validates, it never rewrites the track."""
    if not isinstance(keys, list):
        raise ValueError(f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t=?: track must be a list")
    tolerance = blockout_common.FRAME_GRID_TOLERANCE
    for key in keys:
        if not isinstance(key, dict):
            raise ValueError(f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t=?: keyframe must be an object")
        t = key.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            raise ValueError(f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t={t!r}: t must be a number")
        if not (-tolerance <= t <= total_duration_s + tolerance):
            raise ValueError(
                f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t={t}: "
                f"outside [0, {total_duration_s}]"
            )
        v = key.get("v")
        if not isinstance(v, bool):
            raise ValueError(f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t={t}: v must be a bool, got {v!r}")
        interp = key.get("interp", "constant")
        if interp != "constant":
            raise ValueError(
                f"VIS_INVALID: chain={chain_id} instance={instance_id} visible keyframe t={t}: "
                f"interp must be 'constant' for a bool track, got {interp!r}"
            )


def build_scene_spec(
    chains: Sequence["blockout_common.Chain"],
    shots_by_key: Dict[Tuple[int, int], List[Dict[str, Any]]],
    asset_map: Dict[str, Any],
    fps: int,
    video_caps: Dict[str, Any],
    blockout_resolution: str,
    screenplay_text: str,
    existing_scene_spec: Optional[Dict[str, Any]],
    warnings_out: List[Dict[str, Any]],
    project_id: str,
    assets_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Assembles the full ``scene_spec.json`` (раздел 12) from already
    computed chains and an already built/reconciled ``asset_map``. ``world``
    is per-chain (раздел 12); the root only carries ``project_id``/``fps``/
    ``resolution``/``generated_at``/``spec_version``/``chains``."""
    used_slugs: Dict[str, int] = {}
    name_to_instance: Dict[str, str] = {}
    height_by_instance: Dict[str, float] = {}
    asset_id_by_instance: Dict[str, str] = {}
    body_plan_by_instance: Dict[str, str] = {}
    category_by_instance: Dict[str, str] = {}
    instance_ids_by_location: Dict[str, List[str]] = {}

    for char in asset_map.get("characters") or []:
        name = char.get("name")
        if not name:
            continue
        instance_id = _slug_instance_id(name, used_slugs)
        name_to_instance[blockout_assets.normalize_match_name(name)] = instance_id
        height_by_instance[instance_id] = float(char.get("height_m") or 1.70)
        asset_id_by_instance[instance_id] = char.get("asset_id")
        body_plan_by_instance[instance_id] = char.get("body_plan") or "biped"
        category_by_instance[instance_id] = "character"

    for loc in asset_map.get("locations") or []:
        key_object = loc.get("key_object")
        if not key_object:
            continue
        instance_id = _slug_instance_id(key_object, used_slugs)
        name_to_instance[blockout_assets.normalize_match_name(key_object)] = instance_id
        height_by_instance[instance_id] = float(loc.get("height_m") or 0.50)
        asset_id_by_instance[instance_id] = loc.get("asset_id")
        body_plan_by_instance[instance_id] = loc.get("body_plan") or "none"
        category_by_instance[instance_id] = "prop"
        instance_ids_by_location.setdefault(loc.get("location"), []).append(instance_id)

    library_index = blockout_assets.read_index(assets_root)

    existing_chains_by_id = {
        c.get("chain_id"): c for c in (existing_scene_spec or {}).get("chains") or [] if isinstance(c, dict)
    }

    chain_specs: List[Dict[str, Any]] = []
    for chain in chains:
        if not chain.shots:
            continue
        first_shot = chain.shots[0]
        first_elements = shots_by_key.get((chain.scene_number, first_shot.shot_number), [])
        first_start = next(
            (e for e in first_elements if e.get("shot_type") == "start"),
            first_elements[0] if first_elements else None,
        )
        camera_aspect, aspect_warnings = resolve_camera_aspect(first_start, video_caps, blockout_resolution)
        warnings_out.extend(aspect_warnings)

        chain_spec = build_chain_scene_spec(
            chain=chain,
            shot_elements_by_shot={
                shot.shot_number: shots_by_key.get((chain.scene_number, shot.shot_number), [])
                for shot in chain.shots
            },
            asset_index=height_by_instance,
            name_to_instance=name_to_instance,
            fps=fps,
            camera_aspect=camera_aspect,
            screenplay_text=screenplay_text,
            cached_chain=existing_chains_by_id.get(chain.chain_id),
            warnings_out=warnings_out,
            asset_id_by_instance=asset_id_by_instance,
            body_plan_by_instance=body_plan_by_instance,
            category_by_instance=category_by_instance,
            library_index=library_index,
            instance_ids_by_location=instance_ids_by_location,
        )
        chain_spec["scene_number"] = chain.scene_number
        chain_specs.append(chain_spec)

    return {
        "project_id": project_id,
        "fps": fps,
        "resolution": _parse_resolution_wh(blockout_resolution),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spec_version": SPEC_VERSION,
        "chains": chain_specs,
    }


def _cache_hit(cached_chain: Optional[Dict[str, Any]], input_hash: str) -> bool:
    """раздел 10.1, «кеш»: ``fps``/``camera_aspect`` are deliberately absent
    from ``input_hash`` (``blockout_common.chain_input_hash``) — a mismatch
    on either is the cheap re-snap-without-LLM path, not a cache miss, so
    only ``input_hash`` gates reuse here."""
    if not cached_chain:
        return False
    return cached_chain.get("input_hash") == input_hash


def build_chain_scene_spec(
    chain: "blockout_common.Chain",
    shot_elements_by_shot: Dict[int, List[Dict[str, Any]]],
    asset_index: Dict[str, float],
    name_to_instance: Dict[str, str],
    fps: int,
    camera_aspect: str,
    screenplay_text: str,
    cached_chain: Optional[Dict[str, Any]],
    warnings_out: List[Dict[str, Any]],
    asset_id_by_instance: Optional[Dict[str, str]] = None,
    body_plan_by_instance: Optional[Dict[str, str]] = None,
    category_by_instance: Optional[Dict[str, str]] = None,
    library_index: Optional[Dict[str, Any]] = None,
    instance_ids_by_location: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Assembles one chain entry of ``scene_spec.json`` (раздел 12): builds
    this chain's own ``world`` block plus ``objects``/``camera``/tracks, and
    applies the camera-plan boundary-override rule.
    """
    h_sensor_mm = blockout_common.sensor_h_mm(DEFAULT_SENSOR_MM, camera_aspect_to_render_hw(camera_aspect)[1], camera_aspect_to_render_hw(camera_aspect)[0])

    # objects present across the chain: characters mentioned in any shot +
    # the key_objects of every location visited by this chain's shots
    # (раздел 12, "Состав объектов цепочки") -- not every location in the
    # project, which would leak other chains' props into this one's
    # subject_focus validation set. A chain can legitimately span more than
    # one location (e.g. a "full_copy" link continuing into a new
    # location's shot), so the FIRST shot's location alone is not enough --
    # union across all of this chain's own shot locations.
    chain_character_names: List[str] = []
    chain_locations: List[str] = []
    for shot in chain.shots:
        elements = shot_elements_by_shot.get(shot.shot_number, [])
        for el in elements:
            for name in el.get("characters") or []:
                if name not in chain_character_names:
                    chain_character_names.append(name)
        shot_start = next(
            (e for e in elements if e.get("shot_type") == "start"),
            elements[0] if elements else {},
        )
        shot_location = shot_start.get("location")
        if shot_location and shot_location not in chain_locations:
            chain_locations.append(shot_location)
    chain_character_instance_ids = [
        name_to_instance[blockout_assets.normalize_match_name(n)]
        for n in chain_character_names
        if blockout_assets.normalize_match_name(n) in name_to_instance
    ]
    chain_object_instance_ids: List[str] = []
    for loc in chain_locations:
        for iid in (instance_ids_by_location or {}).get(loc, []):
            if iid not in chain_object_instance_ids:
                chain_object_instance_ids.append(iid)

    # world is per-chain (раздел 12) and built before the camera track below
    # so the ground-clamp rule (раздел 10.1) has ground.z/enabled to clamp
    # against.
    world = build_world_block(
        [asset_index.get(iid, 0.0) for iid in (chain_character_instance_ids + chain_object_instance_ids)]
    )

    plans_by_shot: Dict[int, Dict[str, Any]] = {}
    subjects_by_shot: Dict[int, Optional[str]] = {}
    llm_fields_by_shot: Dict[int, Dict[str, Any]] = {}
    subject_focus_by_shot: Dict[int, List[str]] = {}
    chain_instance_ids = chain_character_instance_ids + chain_object_instance_ids
    for shot in chain.shots:
        elements = shot_elements_by_shot.get(shot.shot_number, [])
        start_el = next((e for e in elements if e.get("shot_type") == "start"), elements[0] if elements else {})
        end_el = next((e for e in elements if e.get("shot_type") == "end"), None)
        plan = parse_camera_plan(start_el.get("camera_plan"))
        for w in plan.get("warnings") or []:
            warnings_out.append(dict(w))
        plans_by_shot[shot.shot_number] = plan
        subjects_by_shot[shot.shot_number] = _resolve_main_subject(
            elements, name_to_instance, chain_character_instance_ids, chain_object_instance_ids
        )
        # раздел 10.1, "Контракт LLM-разбора" — вход: raw camera_plan text
        # (not just the parsed base — a position modifier like "FROM WALL"
        # is meaningless to the model as just "CLOSE-UP") plus
        # english_prompt/video_prompt/spatial_changes_from_start.
        llm_fields_by_shot[shot.shot_number] = {
            "camera_plan": start_el.get("camera_plan"),
            "english_prompt": start_el.get("english_prompt"),
            "video_prompt": start_el.get("video_prompt"),
            "spatial_changes_from_start": (end_el or {}).get("spatial_changes_from_start"),
        }

        # P2.14: subject_focus is validated against this chain's own
        # instance_ids -- it is authored per-shot in shots.json, so a typo
        # or a name from a different chain must fail loudly, not silently
        # point render_shot at nothing.
        raw_subject_focus = start_el.get("subject_focus")
        if raw_subject_focus:
            for focus_id in raw_subject_focus:
                if focus_id not in chain_instance_ids:
                    raise ValueError(
                        f"SUBJ_INVALID: shot={shot.shot_number} in chain={chain.chain_id}: subject_focus "
                        f"{focus_id!r} does not match any instance_id in this chain "
                        f"(available: {chain_instance_ids})"
                    )
            subject_focus_by_shot[shot.shot_number] = list(dict.fromkeys(raw_subject_focus))

    input_hash = chain_input_hash_for_chain(chain, plans_by_shot, chain_character_instance_ids)

    objects: Optional[List[Dict[str, Any]]] = None
    camera: Optional[Dict[str, Any]] = None
    source = "static_fallback"

    if _cache_hit(cached_chain, input_hash):
        objects = cached_chain.get("objects")
        camera = cached_chain.get("camera")
        source = "cache"

    if objects is None or camera is None:
        chain_payload = {
            "chain_id": chain.chain_id,
            "total_duration_s": chain.total_duration_s,
            "fps": fps,
            "screenplay_excerpt": screenplay_text[:2000],
            "objects": [
                {"instance_id": iid, "height_m": asset_index.get(iid)}
                for iid in (chain_character_instance_ids + chain_object_instance_ids)
            ],
            "shots": [
                {
                    "shot_number": shot.shot_number,
                    "t_start": shot.t_start,
                    "duration_s": shot.duration_s,
                    "camera_plan": llm_fields_by_shot[shot.shot_number]["camera_plan"],
                    "english_prompt": llm_fields_by_shot[shot.shot_number]["english_prompt"],
                    "video_prompt": llm_fields_by_shot[shot.shot_number]["video_prompt"],
                    "spatial_changes_from_start": llm_fields_by_shot[shot.shot_number]["spatial_changes_from_start"],
                    "main_subject": subjects_by_shot[shot.shot_number],
                }
                for shot in chain.shots
            ],
        }
        parsed = llm_parse_chain_spatial(chain_payload)
        if parsed is not None:
            parsed = {
                "objects": parsed.get("objects") if isinstance(parsed.get("objects"), list) else [],
                "camera": parsed.get("camera") if isinstance(parsed.get("camera"), dict) else {},
            }
        track_failure = _validate_chain_tracks(
            parsed.get("objects"), parsed.get("camera"), chain.total_duration_s, fps
        ) if parsed is not None else "note"
        if parsed is None or track_failure:
            # one retry
            parsed = llm_parse_chain_spatial(chain_payload)
            if parsed is not None:
                parsed = {
                    "objects": parsed.get("objects") if isinstance(parsed.get("objects"), list) else [],
                    "camera": parsed.get("camera") if isinstance(parsed.get("camera"), dict) else {},
                }
            track_failure = _validate_chain_tracks(
                parsed.get("objects"), parsed.get("camera"), chain.total_duration_s, fps
            ) if parsed is not None else "note"
            if parsed is None or track_failure:
                warnings_out.append({
                    "code": track_failure, "level": "warning",
                    "message": f"chain {chain.chain_id}: LLM spatial parse unavailable/invalid; static fallback applied",
                })
                objects, camera = _static_fallback_tracks(
                    chain.total_duration_s, chain_character_instance_ids + chain_object_instance_ids
                )
                source = "static_fallback"
            else:
                objects, camera = parsed["objects"], parsed["camera"]
                source = "llm"
        else:
            objects, camera = parsed["objects"], parsed["camera"]
            source = "llm"

    # snap every track to the frame grid, regardless of source — раздел
    # 10.1, "валидация": "то же притягивание делается и при чтении из
    # кеша", потому что fps не входит в input_hash и мог смениться с
    # прошлого запуска.
    for obj in objects:
        tracks = obj.get("tracks") or {}
        for key_name in ("location", "rotation_deg"):
            if key_name in tracks:
                tracks[key_name] = _ensure_track_coverage(
                    _snap_track_times(tracks[key_name], fps), chain.total_duration_s
                )
        visible_keys = tracks.get("visible")
        if visible_keys is not None:
            _validate_visible_track(visible_keys, chain.total_duration_s, chain.chain_id, obj.get("instance_id"))
        instance_id = obj.get("instance_id")
        _resolve_object_asset(
            obj,
            asset_id=(asset_id_by_instance or {}).get(instance_id),
            category_default=(category_by_instance or {}).get(instance_id, "prop"),
            body_plan=(body_plan_by_instance or {}).get(instance_id, "none"),
            height_m=asset_index.get(instance_id),
            library_index=library_index or {},
            chain_id=chain.chain_id,
        )
    camera_tracks = camera.setdefault("tracks", {})
    for key_name in ("location", "look_at", "lens_mm"):
        if key_name in camera_tracks:
            camera_tracks[key_name] = _snap_track_times(camera_tracks[key_name], fps)

    camera_tracks = camera.setdefault("tracks", {})
    for key_name in ("location", "look_at", "lens_mm"):
        camera_tracks.setdefault(key_name, [{"t": 0.0, "v": _default_track_value(key_name), "interp": "constant"}])

    # boundary override points: t=0 (first shot's own plan) + every shot's window end
    boundary_points: List[Tuple[float, Dict[str, Any], Optional[float], Optional[Tuple[float, float, float]]]] = []
    for idx, shot in enumerate(chain.shots):
        plan = plans_by_shot[shot.shot_number]
        subject_iid = subjects_by_shot[shot.shot_number]
        subject_height = asset_index.get(subject_iid) if subject_iid else None
        window_end = shot.t_start + shot.duration_s
        if idx == 0:
            subject_loc0 = _lookup_object_location(objects, subject_iid, 0.0)
            boundary_points.append((0.0, plan, subject_height, subject_loc0))
        subject_loc_end = _lookup_object_location(objects, subject_iid, window_end)
        boundary_points.append((window_end, plan, subject_height, subject_loc_end))

    _apply_camera_boundary_overrides(camera_tracks, boundary_points, h_sensor_mm, world["ground"])
    _apply_pov_overrides(camera_tracks, boundary_points)
    _apply_position_modifier_lens_overrides(camera_tracks, boundary_points)
    camera_tracks["location"] = _ensure_track_coverage(camera_tracks["location"], chain.total_duration_s)
    camera_tracks["look_at"] = _ensure_track_coverage(camera_tracks["look_at"], chain.total_duration_s)
    camera_tracks["lens_mm"] = _ensure_track_coverage(camera_tracks["lens_mm"], chain.total_duration_s)

    # B10 (раздел 15.4, 20.1): no camera derivative discontinuity at any
    # internal chain junction. Checked here, on the final tracks, because
    # the boundary overrides just applied above are what can actually
    # introduce a jump between two shots with very different camera_plan
    # targets. Per раздел 10.3.1, B08/B09/B10 never raise — a failure
    # degrades to a warning in the report, same as B08/B09 above.
    for shot, next_shot in zip(chain.shots, chain.shots[1:]):
        t_junction = shot.t_start + shot.duration_s
        if not blockout_common.camera_continuity_ok(
            camera_tracks["location"], camera_tracks["look_at"], camera_tracks["lens_mm"], t_junction, fps
        ):
            warnings_out.append({
                "code": "B10", "level": "warning",
                "message": (
                    f"chain {chain.chain_id}: camera derivative discontinuity at t={t_junction:.3f}s "
                    f"(shot {shot.shot_number} -> {next_shot.shot_number} junction)"
                ),
            })

    return {
        "chain_id": chain.chain_id,
        "input_hash": input_hash,
        "fps": fps,
        "camera_aspect": camera_aspect,
        "source": source,
        "total_duration_s": chain.total_duration_s,
        "world": world,
        "objects": objects,
        "camera": {"sensor_mm": DEFAULT_SENSOR_MM, "tracks": camera_tracks},
        "shots": [
            {
                "shot_number": shot.shot_number,
                "t_start": shot.t_start,
                "duration_s": shot.duration_s,
                "subject_focus": subject_focus_by_shot.get(shot.shot_number, []),
            }
            for shot in chain.shots
        ],
    }


def _default_track_value(key_name: str) -> Any:
    if key_name == "lens_mm":
        return 50.0
    if key_name == "look_at":
        return [0.0, 0.0, 1.5]
    return list(_FALLBACK_CAMERA_DIRECTION)


def _lookup_object_location(
    objects: Optional[List[Dict[str, Any]]], instance_id: Optional[str], t: float
) -> Optional[Tuple[float, float, float]]:
    if not instance_id or not objects:
        return None
    for obj in objects:
        if obj.get("instance_id") == instance_id:
            keys = (obj.get("tracks") or {}).get("location")
            if keys:
                try:
                    return blockout_common.evaluate_track_vec3(keys, t)
                except Exception:  # noqa: BLE001 - malformed key, treat as unknown
                    return None
    return None


def camera_aspect_to_render_hw(camera_aspect: str) -> Tuple[int, int]:
    """``(a_num, a_den) -> (w_render, h_render)`` via the resolution-matching
    k-formula (Приложение А), used only to derive a physically consistent
    sensor height for the aspect ratio — the actual render is not produced
    by this step."""
    match = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", str(camera_aspect or ""))
    if not match:
        return 1280, 720
    a_num, a_den = int(match.group(1)), int(match.group(2))
    if a_num <= 0 or a_den <= 0:
        return 1280, 720
    return blockout_common.resolution_match(1280, 720, a_num, a_den)


def chain_input_hash_for_chain(
    chain: "blockout_common.Chain", plans_by_shot: Dict[int, Dict[str, Any]], character_instance_ids: Sequence[str]
) -> str:
    payload = {
        "chain_id": chain.chain_id,
        "shots": [
            {
                "shot_number": s.shot_number,
                "duration_s": s.duration_s,
                "camera_plan_base": plans_by_shot[s.shot_number].get("base"),
                "camera_plan_modifier": plans_by_shot[s.shot_number].get("modifier"),
            }
            for s in chain.shots
        ],
        "character_instance_ids": sorted(character_instance_ids),
    }
    return blockout_common.chain_input_hash(payload)


def _apply_pov_overrides(
    camera_tracks: Dict[str, List[Dict[str, Any]]],
    boundary_points: Sequence[Tuple[float, Dict[str, Any], Optional[float], Optional[Tuple[float, float, float]]]],
) -> None:
    """POV: camera placed at the subject's location + 0.94*height_m on Z
    (раздел 10.1); lens_mm follows the POV row (35mm)."""
    location_keys = list(camera_tracks.get("location") or [])
    lens_keys = list(camera_tracks.get("lens_mm") or [])
    for t, plan, subject_height_m, subject_location in boundary_points:
        if not plan.get("is_pov"):
            continue
        base_location = subject_location or (0.0, 0.0, 0.0)
        height = subject_height_m or 1.70
        eye_location = [base_location[0], base_location[1], base_location[2] + 0.94 * height]
        location_keys = [k for k in location_keys if abs(k["t"] - t) > blockout_common.FRAME_GRID_TOLERANCE]
        location_keys.append({"t": t, "v": eye_location, "interp": "bezier"})
        lens_keys = [k for k in lens_keys if abs(k["t"] - t) > blockout_common.FRAME_GRID_TOLERANCE]
        lens_keys.append({"t": t, "v": float(plan["lens_mm"]), "interp": "linear"})
    location_keys.sort(key=lambda k: k["t"])
    lens_keys.sort(key=lambda k: k["t"])
    camera_tracks["location"] = location_keys
    camera_tracks["lens_mm"] = lens_keys


def _apply_position_modifier_lens_overrides(
    camera_tracks: Dict[str, List[Dict[str, Any]]],
    boundary_points: Sequence[Tuple[float, Dict[str, Any], Optional[float], Optional[Tuple[float, float, float]]]],
) -> None:
    """Non-POV position modifiers (``FROM WALL``, ``ВНУТРИ …``, ``FROM
    TRAP``) — раздел 10.1, "Правило отката": "положение берётся из
    модификатора, от базы остаётся только объектив". The LLM already placed
    ``location``/``look_at`` for these shots (raw ``camera_plan`` text is
    part of its input); this only forces ``lens_mm`` to the base's value at
    each boundary point, same as the table-driven recompute does for
    ordinary shots. POV shots are excluded — ``_apply_pov_overrides`` owns
    their lens.
    """
    lens_keys = list(camera_tracks.get("lens_mm") or [])
    for t, plan, _subject_height_m, _subject_location in boundary_points:
        if not plan.get("position_override") or plan.get("is_pov"):
            continue
        lens_keys = [k for k in lens_keys if abs(k["t"] - t) > blockout_common.FRAME_GRID_TOLERANCE]
        lens_keys.append({"t": t, "v": float(plan["lens_mm"]), "interp": "linear"})
    lens_keys.sort(key=lambda k: k["t"])
    camera_tracks["lens_mm"] = lens_keys
