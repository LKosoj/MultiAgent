"""Э3: ``blockout_renderer`` -- Blender Workbench render, ``blockout_ref.mp4``,
``ref_start.png`` / ``ref_end.png``.

Spec: docs/tz-blockout-reference-pipeline.md, разделы 10.2, 13, 14, 16, 20.1-20.3.

Contract (раздел 10.3.1), identical in spirit to ``blockout_scene_builder``:
this tool always returns a DICT, never a JSON string, and never a key
``status: "error"``/``error``/``exception`` on a normal return. Blocking
checks write a ``level: "error"`` record to the ``blockout_renderer``
section of ``report.json`` FIRST, then raise -- the engine turns that into
a failed step -- **except** B06 (owned by Э4, not implemented here), B16
and B17's second form, which mark one chain ``partial`` without raising
(раздел 10.3.1, "Исключений из этого правила три").

Э3/Э4 boundary (this tool's scope): scene assembly, Workbench render,
``blockout_ref.mp4``, duplication of the edge frames into
``ref_start.png``/``ref_end.png``, ``manifest.json`` per shot with
**placeholder** junction fields (``{"status": "not_checked"}`` for internal
junctions, ``null`` at chain boundaries -- real comparison is Э4's B06).
``state/shot_NN_out.json`` is not written here.

Everything in this file is plain Python -- no ``bpy`` import. Blender
itself only runs via ``custom_tools.storybook.blockout_common.run_blender_script()``,
launching ``blockout_blender/render_shot.py`` once per chain.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from PIL import Image

try:
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:
    np = None
    _NUMPY_AVAILABLE = False

from custom_tools.storybook import blockout_assets, blockout_common
from custom_tools.storybook.blockout_blender import render_shot as blockout_render_shot
from custom_tools.storybook.blockout_scene_builder import merge_write_report
from custom_tools.storybook.project_paths import safe_storybook_project_dir
from custom_tools.storybook.video_generator_aitunnel_media import _infer_aspect_ratio, _resolve_size_params

logger = logging.getLogger(__name__)

REPORT_SECTION = "blockout_renderer"
_RENDER_SHOT_SCRIPT = Path(__file__).resolve().parent / "blockout_blender" / "render_shot.py"


# =============================================================================
# Zero contract / small helpers (раздел 10.3.1)
# =============================================================================


def _zero_contract(project_id: str) -> Dict[str, Any]:
    # раздел 10.2 "Форма пути": the absolute path safe_storybook_project_dir()
    # resolves -- never the hardcoded 'plots/storybooks/...' literal, which
    # is wrong whenever STORYBOOK_PROJECTS_DIR is set. Resolving the path is
    # not "reading project files" (раздел 10.3.1): safe_storybook_project_dir()
    # neither creates directories nor requires the project to exist.
    project_dir = safe_storybook_project_dir(project_id)
    return {
        "status": "skipped",
        "shots_rendered": 0,
        "frames_total": 0,
        "junction_checks": [],
        "artifact_path": str(project_dir / "93_blockout"),
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _env_shortcut(value: Any, default: Any, env_name: str) -> Any:
    """P0.7/P2.13/engine forwarding: an explicit call-site value always
    wins; a value left at its parameter default falls back to the
    matching ``BLOCKOUT_*`` env var, so ad-hoc single-shot/quiet/engine
    runs don't need every call site edited.
    """
    if value != default:
        return value
    return os.getenv(env_name, default)


def _parse_shots_filter(shots: str) -> Optional[Set[int]]:
    """P0.7: comma-separated ``shot_number`` allowlist, e.g. ``"1,2,3"``.
    ``None`` means "no filter" (empty string). A non-empty string with no
    valid ints (e.g. a typo) raises rather than silently disabling the
    filter."""
    value = str(shots or "").strip()
    if not value:
        return None
    numbers: Set[int] = set()
    bad_tokens: List[str] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            numbers.add(int(part))
        except ValueError:
            bad_tokens.append(part)
    if bad_tokens:
        raise ValueError(f"B21b: invalid shots filter tokens: {bad_tokens} (from {value!r})")
    if not numbers:
        raise ValueError(f"B21b: invalid shots filter: {value!r}")
    return numbers


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_paths(project_id: str) -> Dict[str, Path]:
    project_dir = safe_storybook_project_dir(project_id)
    return {
        "project_dir": project_dir,
        "shots": project_dir / "97_shots" / "shots.json",
        "caps": project_dir / "97_shots" / "video_model_caps.json",
        "scene_spec": project_dir / "93_blockout" / "scene_spec.json",
        "report": project_dir / "93_blockout" / "report.json",
        "blockout_dir": project_dir / "93_blockout",
    }


# =============================================================================
# report.json section (раздел 20.3) -- own section, reuses the generic
# read-merge-write from blockout_scene_builder
# =============================================================================


def _report_append_checks(report_path: Path, entries: Sequence[Dict[str, Any]]) -> None:
    if not entries:
        return

    def _update(section: Dict[str, Any]) -> Dict[str, Any]:
        section = dict(section)
        checks = list(section.get("checks") or [])
        checks.extend(entries)
        section["checks"] = checks
        return section

    merge_write_report(report_path, REPORT_SECTION, _update)


def _report_clear_scope_checks(report_path: Path, chain_ids: Sequence[str]) -> None:
    """раздел 20.3, "слияние по ключу шота": before a run starts appending
    new checks, drop this section's OWN stale entries for the chains about
    to be reprocessed (identified by their ``chain_id`` field) -- otherwise
    a chain that failed once (e.g. B16) and is fixed and rerun with a
    narrower ``scope`` keeps its old error in ``report.json`` forever.
    Entries for chains outside ``chain_ids``, and entries with no
    ``chain_id`` (project-wide preconditions such as B12/B02), are left
    untouched -- partial runs must not erase diagnostics for the rest of
    the project.
    """
    if not chain_ids:
        return
    chain_id_set = set(chain_ids)

    def _update(section: Dict[str, Any]) -> Dict[str, Any]:
        section = dict(section)
        checks = list(section.get("checks") or [])
        section["checks"] = [c for c in checks if c.get("chain_id") not in chain_id_set]
        return section

    merge_write_report(report_path, REPORT_SECTION, _update)


def _report_finalize_summary(report_path: Path, summary: Dict[str, Any]) -> None:
    """Sets the terminal ``status``/counters without touching ``checks``
    (already accumulated via incremental ``_report_append_checks`` calls
    made throughout the run -- a batch B03-B11 failure must abort the run
    without losing earlier chains' diagnostics, раздел 10.3.1)."""

    def _update(section: Dict[str, Any]) -> Dict[str, Any]:
        section = dict(section)
        section.update(summary)
        return section

    merge_write_report(report_path, REPORT_SECTION, _update)


def _check_b12(report_path: Path) -> None:
    available, version, message = blockout_common.check_blender_available()
    if not available:
        detail = f"B12: Blender not available: {message}"
        _report_append_checks(
            report_path, [{"code": "B12", "level": "error", "message": detail, "details": {"version": version}}]
        )
        raise RuntimeError(detail)


# =============================================================================
# Resolution negotiation (раздел 16.2) + B16
# =============================================================================


def _parse_resolution_string(resolution: str) -> Tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(resolution or ""))
    if not match:
        return 1280, 720
    w, h = int(match.group(1)), int(match.group(2))
    return (w, h) if w > 0 and h > 0 else (1280, 720)


def _aspect_from_resolution_string(resolution: str) -> str:
    from math import gcd

    w, h = _parse_resolution_string(resolution)
    divisor = gcd(w, h)
    return f"{w // divisor}:{h // divisor}"


def resolve_render_resolution(
    video_caps: Dict[str, Any],
    width: Optional[Any],
    height: Optional[Any],
    blockout_resolution: str,
) -> Tuple[int, int, str, List[Dict[str, Any]]]:
    """раздел 16.2: negotiates the actual render size for one chain.

    Returns ``(w_render, h_render, aspect_ratio_string, warnings)``.
    ``warnings`` are P13 entries (raw dicts, code/reason/message) -- the
    caller decides whether/where to persist them.
    """
    base_w, base_h = _parse_resolution_string(blockout_resolution)
    valid_size = (
        isinstance(width, (int, float)) and not isinstance(width, bool)
        and isinstance(height, (int, float)) and not isinstance(height, bool)
        and width > 0 and height > 0
    )
    if not valid_size:
        aspect = _aspect_from_resolution_string(blockout_resolution)
        warning = {
            "code": "P13", "level": "warning", "reason": "shot_size_missing",
            "message": "first shot of chain has no width/height; camera aspect taken from blockout_resolution",
        }
        a_num, a_den = (int(x) for x in aspect.split(":"))
        w, h = blockout_common.resolution_match(base_w, base_h, a_num, a_den)
        return w, h, aspect, [warning]

    has_size_fields = bool(
        video_caps.get("supported_sizes") or video_caps.get("supported_resolutions")
        or video_caps.get("supported_aspect_ratios")
    )
    if not has_size_fields:
        aspect = _aspect_from_resolution_string(blockout_resolution)
        warning = {
            "code": "P13", "level": "warning", "reason": "no_size_fields",
            "message": "video_model_caps.json has no size fields; rendering by blockout_resolution as-is",
        }
        a_num, a_den = (int(x) for x in aspect.split(":"))
        w, h = blockout_common.resolution_match(base_w, base_h, a_num, a_den)
        return w, h, aspect, [warning]

    result = _resolve_size_params(video_caps, int(width), int(height))
    if result is None:
        aspect = _aspect_from_resolution_string(blockout_resolution)
        warning = {
            "code": "P13", "level": "warning", "reason": "aspect_mismatch",
            "message": "video model did not confirm an aspect ratio for the shot's size; rendering by blockout_resolution",
        }
        a_num, a_den = (int(x) for x in aspect.split(":"))
        w, h = blockout_common.resolution_match(base_w, base_h, a_num, a_den)
        return w, h, aspect, [warning]

    if "aspect_ratio" in result:
        aspect = result["aspect_ratio"]
    else:
        size_w, size_h = _parse_resolution_string(result["size"])
        aspect = _infer_aspect_ratio(size_w, size_h) or _aspect_from_resolution_string(blockout_resolution)

    a_num, a_den = (int(x) for x in aspect.split(":"))
    w, h = blockout_common.resolution_match(base_w, base_h, a_num, a_den)
    return w, h, aspect, []


def check_chain_size_mismatch(
    chain_shots: Sequence[Dict[str, Any]],
    items: Sequence[Dict[str, Any]],
    scene_number: int,
    first_width: Any,
    first_height: Any,
) -> bool:
    """P13, reason ``chain_size_mismatch`` (раздел 20.2/22 A40): True when a
    shot of the chain other than the first has a ``shots.json`` size that
    differs from the first shot's -- rendering still goes by the first
    shot's size (раздел 16.2), this only decides whether to warn about it.
    """

    def _valid(w: Any, h: Any) -> bool:
        return (
            isinstance(w, (int, float)) and not isinstance(w, bool)
            and isinstance(h, (int, float)) and not isinstance(h, bool)
            and w > 0 and h > 0
        )

    if not _valid(first_width, first_height):
        return False
    for shot in chain_shots[1:]:
        item = find_shot_item(items, scene_number, shot.get("shot_number"), "start")
        if item is None:
            continue
        w, h = item.get("width"), item.get("height")
        if _valid(w, h) and (w, h) != (first_width, first_height):
            return True
    return False


def check_b16(scene_spec_camera_aspect: Optional[str], computed_aspect: str) -> bool:
    return str(scene_spec_camera_aspect or "") == str(computed_aspect or "")


def check_b17_chain_present(scene_spec_chains: Sequence[Dict[str, Any]], chain_id: str) -> bool:
    return any(c.get("chain_id") == chain_id for c in scene_spec_chains)


# =============================================================================
# Precondition scan: B01 third form / B02 second form (раздел 12, 20.1)
# =============================================================================


def find_shot_item(
    items: Sequence[Dict[str, Any]], scene_number: int, shot_number: int, shot_type: str = "start"
) -> Optional[Dict[str, Any]]:
    for item in items:
        if (
            item.get("scene_number") == scene_number
            and item.get("shot_number") == shot_number
            and item.get("shot_type") == shot_type
        ):
            return item
    return None


def check_duration_mismatches(
    chains: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]], shots_filter: Optional[Set[int]] = None
) -> List[Dict[str, Any]]:
    """B01, third form: every shot's ``duration_s`` in ``scene_spec.json``
    must match ``shots.json`` (раздел 12). Returns mismatch records.
    ``shots_filter`` (раздел 18.4, partial reruns), when given, skips shots
    not in scope -- a stale duration on an untouched shot must not block a
    rerun of a different one."""
    mismatches: List[Dict[str, Any]] = []
    for chain in chains:
        scene_number = chain.get("scene_number")
        for shot in chain.get("shots") or []:
            shot_number = shot.get("shot_number")
            if shots_filter is not None and shot_number not in shots_filter:
                continue
            spec_duration = shot.get("duration_s")
            item = find_shot_item(items, scene_number, shot_number, "start")
            actual_duration = item.get("duration_s") if item else None
            if actual_duration is None or actual_duration != spec_duration:
                mismatches.append({
                    "scene_number": scene_number, "shot_number": shot_number,
                    "scene_spec_duration_s": spec_duration, "shots_json_duration_s": actual_duration,
                })
    return mismatches


def check_fps_mismatch(requested_fps: int, scene_spec_fps: Any) -> bool:
    """B02, second form: ``blockout_fps`` must match ``scene_spec.json``'s
    ``fps``. Returns True on a MATCH (no mismatch)."""
    return int(requested_fps) == scene_spec_fps


# =============================================================================
# Post-render checks: B03-B05, B07, B11 (раздел 20.1) -- raise on failure,
# same footing as preconditions (раздел 10.3.1), but only for shots
# actually rendered this run.
# =============================================================================


def check_b03(frame_files: Sequence[str], n_render: int) -> bool:
    return len(frame_files) == n_render


def check_b04(frame_files: Sequence[str], n_render: int) -> bool:
    expected = set(blockout_render_shot.frame_filenames_for_window(n_render))
    return set(frame_files) == expected


def check_b05(sizes: Sequence[Tuple[int, int]]) -> bool:
    if not sizes:
        return False
    first = sizes[0]
    return all(s == first for s in sizes)


def _read_frame_sizes(frames_dir: Path, frame_files: Sequence[str]) -> List[Optional[Tuple[int, int]]]:
    """Reads each rendered frame's actual pixel size off disk for B05 --
    trusts the file, not just the size Blender self-reports for the shot."""
    sizes: List[Optional[Tuple[int, int]]] = []
    for name in frame_files:
        try:
            with Image.open(frames_dir / name) as image:
                sizes.append(tuple(image.size))
        except (OSError, ValueError):
            sizes.append(None)
    return sizes


def check_b07(ref_hash: Optional[str], frame_hash: Optional[str]) -> bool:
    return bool(ref_hash) and ref_hash == frame_hash


def check_b11(actual_video_frame_count: Optional[int], n_video: int) -> bool:
    return actual_video_frame_count == n_video


# =============================================================================
# scope (раздел 10.2 "Параметр scope"): "all" | "scene_NN" |
# "scene_NN_shot_MM" | "chain_<chain_id>". Any other value falls back to
# "all" with a warning rather than silently rendering nothing.
#
# Э3 simplification (documented in the final report): for
# "scene_NN_shot_MM" the spec re-renders the whole chain but persists only
# the requested shot's files, so that the neighbouring shots' freshly
# rendered frames exist in memory for Э4's B06 junction diff without being
# written to disk. Э3 does not implement B06, so this resolver only
# decides *which chains* get processed -- every shot of a selected chain
# is rendered AND persisted (manifest.json/blockout_ref.mp4/shots.json),
# not just the one named by the scope string.
#
# Э4 code-review, "Предупреждение 2": a "scene_NN_shot_MM" run therefore
# also lets `blockout_junction_failed` be set/cleared on shots.json
# elements outside the requested shot. Left as-is on purpose, not a new
# Э4 defect: раздел 15.3 defines the flag protocol scope-agnostically --
# "признак снимается тем же рендерером. У каждого шота, чей стык он в
# этом прогоне проверял и принял..., поле удаляется... если стык в
# прогоне не проверялся -- сосед не рендерился -- прежнее значение
# сохраняется" -- and since the WHOLE chain is re-rendered in memory for
# this scope form (above), every internal junction of the chain genuinely
# IS checked this run, so every shot whose junction was checked is
# correctly in scope for the flag update, not just the requested one.
# What раздел 10.2 restricts to "only the requested shot" is disk
# persistence of per-shot artifact FILES (manifest.json, frames/*.png,
# blockout_ref.mp4) -- the pre-existing Э3 gap noted above, out of this
# review's scope to close.
# =============================================================================


def resolve_scope(scope: str, chains: Sequence[Dict[str, Any]]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    value = str(scope or "all").strip()
    all_ids = [c.get("chain_id") for c in chains if isinstance(c, dict) and c.get("chain_id")]

    if value == "all" or not value:
        return all_ids, None

    match = re.match(r"^scene_(\d+)_shot_(\d+)$", value)
    if match:
        scene_number, shot_number = int(match.group(1)), int(match.group(2))
        for chain in chains:
            if chain.get("scene_number") != scene_number:
                continue
            if any(s.get("shot_number") == shot_number for s in chain.get("shots") or []):
                return [chain.get("chain_id")], None
        return [], {"code": "note", "level": "warning", "message": f"scope {value!r} matched no chain"}

    match = re.match(r"^scene_(\d+)$", value)
    if match:
        scene_number = int(match.group(1))
        ids = [c.get("chain_id") for c in chains if c.get("scene_number") == scene_number]
        if ids:
            return ids, None
        return [], {"code": "note", "level": "warning", "message": f"scope {value!r} matched no chain"}

    match = re.match(r"^chain_(.+)$", value)
    if match:
        chain_id = match.group(1)
        if chain_id in all_ids:
            return [chain_id], None
        return [], {"code": "note", "level": "warning", "message": f"scope {value!r} matched no chain"}

    return all_ids, {
        "code": "note", "level": "warning",
        "message": f"scope {value!r} not recognized; rendering the full project (scope: all)",
    }


# =============================================================================
# manifest.json (раздел 10.2 item 7, 15.1)
# =============================================================================


def compute_spec_hash(chain_spec: Dict[str, Any]) -> str:
    canonical = json.dumps(chain_spec, ensure_ascii=False, sort_keys=True, default=str)
    import hashlib

    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Э4 code-review, "Предупреждение 3" (раздел 10.2, "Повторный запуск не
# перерисовывает то, что уже актуально"): implemented and unit-tested,
# but never called from ``blockout_renderer_tool`` -- ``scope: "all"``
# always re-renders every chain, so a rerun never skips an already-current
# shot the way раздел 10.2 requires, and A17's minute budget (раздел 22)
# is spent on nothing-changed shots too. Neither раздел 21's Э3 row
# ("B03, B04, B07, B11 проходят на одном шоте") nor its Э4 row ("B06
# проходит на цепочке из трёх шотов") names this skip-logic as part of
# either stage's deliverable, so ownership is left unresolved by раздел
# 21 the same way A04's soft-junction ratio was before this pass. Not
# wired up here: doing so touches the render-vs-skip branch of the main
# loop, a larger change than this review's continuity/B06-joint scope,
# and the task explicitly asked to only flag it, not fix it.
def is_manifest_current(
    manifest: Optional[Dict[str, Any]],
    *, duration_s: int, fps: int, resolution: Sequence[int], blender_version: Optional[str], spec_hash: str,
) -> bool:
    if not manifest:
        return False
    return (
        manifest.get("duration_s") == duration_s
        and manifest.get("fps") == fps
        and list(manifest.get("resolution") or []) == list(resolution)
        and manifest.get("blender_version") == blender_version
        and manifest.get("spec_hash") == spec_hash
    )


def build_manifest(
    *,
    scene_number: int,
    shot_number: int,
    chain_id: str,
    duration_s: int,
    fps: int,
    frames_rendered: int,
    spec_hash: str,
    resolution: Sequence[int],
    t_start_in_chain: float,
    junction_with_prev: Optional[Dict[str, Any]],
    junction_with_next: Optional[Dict[str, Any]],
    assets_used: Sequence[Dict[str, Any]],
    warnings: Sequence[str],
    blender_version: Optional[str],
) -> Dict[str, Any]:
    return {
        "scene_number": scene_number,
        "shot_number": shot_number,
        "chain_id": chain_id,
        "duration_s": duration_s,
        "fps": fps,
        "frames_rendered": frames_rendered,
        "spec_hash": spec_hash,
        "resolution": list(resolution),
        "t_start_in_chain": t_start_in_chain,
        "junction_with_prev": junction_with_prev,
        "junction_with_next": junction_with_next,
        "assets_used": list(assets_used),
        "warnings": list(warnings),
        "rendered_at": _utc_now_iso(),
        "blender_version": blender_version,
        "view_transform": "Standard",
        "render_aa": "8",
        "p10_acknowledged": None,
    }


# =============================================================================
# Junction check -- Э4, раздел 15.3, B06/P07. Never raises: B06 is one of
# the three checks that must not (раздел 10.3.1).
# =============================================================================

_JUNCTION_MAX_CHANNEL_DIFF = 1  # раздел 15.3: "максимальная разница по каналу <= 1 уровень из 256"
_JUNCTION_DIFF_PIXELS_RATIO = 0.001  # раздел 15.3: "доля отличающихся пикселей <= 0.1%"


def compare_junction_frames(
    hash_a: Optional[str], hash_b: Optional[str], path_a: Path, path_b: Path
) -> Dict[str, Any]:
    """раздел 15.3: two-level junction check between shot N's last
    rendered frame (``path_a``/``hash_a`` = its ``frame_last_sha256``) and
    shot N+1's first frame (``path_b``/``hash_b`` = its
    ``frame_0001_sha256``). Never raises.

    Returns ``{"status": "exact"}`` (sha256 fast path); ``{"status":
    "soft"|"failed", "max_channel_diff": int, "diff_pixels_ratio": float}``
    (pixel diff against the раздел 15.3 thresholds -- a zero diff via the
    pixel path is also "soft", but carries an extra ``"warning":
    "static_cut"`` since the cut then shows no visible transition at
    all); or ``{"status": "failed", "reason":
    "missing_file"|"size_mismatch"|"numpy_unavailable"}`` when the
    pixel-level comparison itself can't be performed.
    """
    if hash_a and hash_b and hash_a == hash_b:
        return {"status": "exact"}
    if not _NUMPY_AVAILABLE:
        return {"status": "failed", "reason": "numpy_unavailable"}
    try:
        with Image.open(path_a) as img_a, Image.open(path_b) as img_b:
            if img_a.size != img_b.size:
                return {"status": "failed", "reason": "size_mismatch"}
            arr_a = np.asarray(img_a.convert("RGB"), dtype=np.int16)
            arr_b = np.asarray(img_b.convert("RGB"), dtype=np.int16)
    except (OSError, ValueError):
        return {"status": "failed", "reason": "missing_file"}

    diff = np.abs(arr_a - arr_b)
    max_channel_diff = int(diff.max()) if diff.size else 0
    total_pixels = arr_a.shape[0] * arr_a.shape[1]
    differing_pixels = int(np.any(diff > 0, axis=-1).sum()) if total_pixels else 0
    diff_pixels_ratio = (differing_pixels / total_pixels) if total_pixels else 0.0

    # P2.10: zero diff via the pixel path is still a threshold PASS
    # ("soft", раздел 15.3's enum has no separate status for it), but
    # flagged via "warning" -- the cut shows literally no visible
    # transition, worth surfacing separately from an ordinary soft pass.
    if max_channel_diff == 0 and diff_pixels_ratio == 0.0:
        return {
            "status": "soft", "max_channel_diff": max_channel_diff, "diff_pixels_ratio": diff_pixels_ratio,
            "warning": "static_cut",
        }

    accepted = max_channel_diff <= _JUNCTION_MAX_CHANNEL_DIFF and diff_pixels_ratio <= _JUNCTION_DIFF_PIXELS_RATIO
    return {
        "status": "soft" if accepted else "failed",
        "max_channel_diff": max_channel_diff,
        "diff_pixels_ratio": diff_pixels_ratio,
    }


def blender_version_mismatch(version_a: Optional[str], version_b: Optional[str]) -> bool:
    """раздел 13.4: "Несовпадение blender_version у соседних шотов даёт
    предупреждение" (Приложение Б, п.8). Missing/empty on either side
    counts as no mismatch -- nothing to compare."""
    return bool(version_a) and bool(version_b) and version_a != version_b


# =============================================================================
# ffmpeg (раздел 16.1)
# =============================================================================


def build_ffmpeg_args(frames_dir: Path, fps: int, n_video: int, output_path: Path) -> List[str]:
    """Verbatim раздел 16.1 command as an argv list (no shell)."""
    return [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-start_number", "1",
        "-i", str(frames_dir / "frame_%04d.png"),
        "-frames:v", str(n_video),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "16",
        "-preset", "slow",
        "-movflags", "+faststart",
        str(output_path),
    ]


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _run_ffmpeg(ffmpeg_bin: str, args: List[str], *, timeout: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.run([ffmpeg_bin] + args[1:], capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr[-2000:]
    return True, ""


# =============================================================================
# shots.json field patch (раздел 10.2 item 6, "Дисциплина записи в shots.json")
# =============================================================================

_SHOTS_LOCK_PATH_SUFFIX = ".lock"

# раздел 15.3: "признак снимается" -- a patch field mapped to this sentinel
# means "remove this field from the on-disk element" rather than "set it",
# so ``_merge_write_shots_blockout_fields`` can clear ``blockout_junction_failed``
# the same way it sets it (one merge protocol, no separate deletion path).
_REMOVE_FIELD = object()


def _merge_write_shots_blockout_fields(shots_path: Path, patches: Dict[Tuple[int, int, str], Dict[str, Any]]) -> None:
    """Per-field merge (not whole-element replace) of the blockout fields
    onto ``97_shots/shots.json`` elements, keyed by ``(scene_number,
    shot_number, shot_type)``. Sidecar-locked, matching
    ``screenplay_shots_generator._merge_write_shots()`` (раздел 10.2). A
    patch value of ``_REMOVE_FIELD`` deletes that field from the element
    instead of setting it (раздел 15.3).
    """
    if not patches:
        return
    import fcntl

    lock_path = shots_path.with_name(shots_path.name + _SHOTS_LOCK_PATH_SUFFIX)
    shots_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            on_disk: Dict[str, Any] = {"items": []}
            if shots_path.is_file():
                try:
                    loaded = json.loads(shots_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        on_disk = loaded
                except (OSError, json.JSONDecodeError):
                    pass

            items = list(on_disk.get("items") or [])
            for item in items:
                try:
                    key = (int(item.get("scene_number")), int(item.get("shot_number")), item.get("shot_type"))
                except (TypeError, ValueError):
                    continue
                patch = patches.get(key)
                if patch:
                    for field_name, value in patch.items():
                        if value is _REMOVE_FIELD:
                            item.pop(field_name, None)
                        else:
                            item[field_name] = value
            on_disk["items"] = items

            tmp_path = shots_path.with_name(f"{shots_path.name}.{os.getpid()}.tmp")
            try:
                tmp_path.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_path, shots_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


# =============================================================================
# Chain payload for Blender (раздел 12, 13.4)
# =============================================================================


def build_chain_payload(chain_spec: Dict[str, Any], scene_spec_world: Dict[str, Any], fps: int) -> Dict[str, Any]:
    return {
        "world": scene_spec_world,
        "fps": fps,
        "objects": chain_spec.get("objects") or [],
        "camera": chain_spec.get("camera") or {},
    }


# =============================================================================
# assets_used (раздел 10.2 п.7, Приложение Б п.8)
# =============================================================================


def collect_chain_assets_used(
    chain_objects: Sequence[Dict[str, Any]], assets_root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Library objects actually used by one chain, deduplicated by
    ``asset_id``, each carrying its current ``asset_version`` from
    ``meta.json`` -- reuses Э2's own asset-library lookup
    (``blockout_assets.read_index``/``find_asset_by_id``) rather than
    re-deriving asset resolution here. Proxies (no real library
    ``asset_id``) are excluded -- they are not library objects.
    """
    index: Optional[Dict[str, Any]] = None
    seen: Dict[str, Dict[str, Any]] = {}
    for obj in chain_objects:
        asset_id = obj.get("asset_id")
        if not asset_id or obj.get("proxy") is True or blockout_assets.is_proxy_asset_id(asset_id):
            continue
        if asset_id in seen:
            continue
        if index is None:
            index = blockout_assets.read_index(assets_root)
        entry = blockout_assets.find_asset_by_id(index, asset_id)
        if entry is None:
            continue
        seen[asset_id] = {"asset_id": asset_id, "asset_version": entry.get("asset_version")}
    return list(seen.values())


# =============================================================================
# Blender subprocess timeout (раздел 13.4, 13.5)
# =============================================================================

_CHAIN_TIMEOUT_MIN_S = 900.0
_CHAIN_TIMEOUT_PER_FRAME_WORST_CASE_S = 0.4  # раздел 13.5, "Фрейм Workbench" upper bound
_CHAIN_TIMEOUT_MARGIN = 1.5  # +50% safety margin
_CHAIN_TIMEOUT_OVERHEAD_S = 60.0  # scene assembly + Blender process startup


def compute_chain_render_timeout(total_n_render: int) -> float:
    """раздел 13.5: one Blender process renders every shot of a chain in
    sequence, so the subprocess timeout must scale with the chain's total
    frame count -- a flat 900 s lets a long chain (e.g. ten 10 s shots,
    ~96 s/shot worst case, well over 900 s total) time out mid-render and
    lose already-finished shots to a spurious B12. Simple linear estimate
    from the frame count, not a detailed model; ``_CHAIN_TIMEOUT_MIN_S`` is
    a floor so short chains keep the previous generous timeout.
    """
    estimated = total_n_render * _CHAIN_TIMEOUT_PER_FRAME_WORST_CASE_S * _CHAIN_TIMEOUT_MARGIN + _CHAIN_TIMEOUT_OVERHEAD_S
    return max(_CHAIN_TIMEOUT_MIN_S, estimated)


# =============================================================================
# Main entrypoint (раздел 10.0-10.4)
# =============================================================================


def blockout_renderer_tool(
    session_id: str,
    project_id: str,
    fps: Any = 24,
    resolution: str = "1280x720",
    jobs: int = 0,
    scope: str = "all",
    enable: bool = True,
    shots: str = "",
    quiet: bool = False,
    engine: str = "eevee",
) -> Dict[str, Any]:
    """Э3 main entrypoint. Always returns a dict, never a JSON string, and
    never ``status: "error"``/``error``/``exception`` (раздел 10.3.1).

    ``jobs`` (parallelism across independent chains, раздел 13.5) is
    accepted for signature/contract compatibility but **not implemented**
    in Э3: chains are rendered sequentially. Э3's acceptance bar (раздел
    21) only requires B03/B04/B07/B11 to pass on one shot, which a
    sequential implementation already satisfies; documented as a scope
    reduction in the final report rather than gold-plated here.

    P02-P06 (раздел 20.2) are **not implemented** here either, for the
    same reason: раздел 20.3's ownership table lists ``blockout_renderer``
    as the eventual owner, but раздел 21's Э3 stage row (line ~2497)
    scopes this step's deliverable to "B03, B04, B07, B11 проходят на
    одном шоте", and none of раздел 22's acceptance criteria (A01-A42)
    exercise P02-P06. Implementing them now (P02 in particular would need
    a geometric occlusion/coverage estimate from ``scene_spec.json``,
    since ``color_type='SINGLE'`` rules out a pixel-based check) is
    speculative work beyond this step's accepted scope; left for a later
    pass when an acceptance criterion actually requires them.
    """
    del session_id, jobs

    if not _as_bool(enable):
        return _zero_contract(project_id)

    # P0.7/P2.13/engine forwarding: resolve BLOCKOUT_SHOTS/BLOCKOUT_QUIET/
    # BLOCKOUT_ENGINE shortcuts before any project I/O.
    # WHY: the B21b (shots filter) and invalid-engine raises below happen
    # before paths/report.json are resolved, so neither writes a report.json
    # entry -- unlike раздел 10.3.1's blocking checks, these are pre-flight
    # arg validation (bad tool input, not project state) and are not covered
    # by that "write to report first" contract.
    shots_filter = _parse_shots_filter(_env_shortcut(shots, "", "BLOCKOUT_SHOTS"))
    quiet_effective = _as_bool(_env_shortcut(quiet, False, "BLOCKOUT_QUIET"))
    engine_effective = _env_shortcut(engine, "eevee", "BLOCKOUT_ENGINE")
    if str(engine_effective).strip().lower() not in ("eevee", "workbench"):
        raise ValueError(f"invalid engine: {engine_effective!r}")

    paths = _resolve_paths(project_id)

    if not paths["scene_spec"].is_file():
        entry = {
            "code": "B17", "level": "error",
            "message": "scene_spec.json does not exist; run blockout_scene_builder first",
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B17: " + entry["message"])

    _check_b12(paths["report"])
    _, blender_version, _ = blockout_common.check_blender_available()

    scene_spec = _read_json(paths["scene_spec"], default=None)
    if not isinstance(scene_spec, dict):
        entry = {"code": "B17", "level": "error", "message": "scene_spec.json is not valid JSON"}
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B17: " + entry["message"])

    fps_int = int(fps)
    if not check_fps_mismatch(fps_int, scene_spec.get("fps")):
        entry = {
            "code": "B02", "level": "error",
            "message": "blockout_fps does not match scene_spec.json's fps; rerun blockout_scene_builder",
            "details": {"requested": fps_int, "scene_spec": scene_spec.get("fps")},
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B02: " + entry["message"])

    shots_data = _read_json(paths["shots"], default={"items": []})
    items = shots_data.get("items") or []
    caps_data = _read_json(paths["caps"], default={})

    all_chains = [c for c in (scene_spec.get("chains") or []) if isinstance(c, dict)]

    # раздел 12 (сверка), 18.4 (частичные запуски): scope is resolved BEFORE
    # B01 is checked -- rerendering one chain must not fail on a duration
    # mismatch in some unrelated chain that isn't part of this run.
    selected_chain_ids, scope_warning = resolve_scope(scope, all_chains)
    # раздел 20.3 "слияние по ключу шота": drop this section's own stale
    # entries for the chains about to be reprocessed before appending new
    # ones, so a fixed-and-rerun chain doesn't keep its old error forever.
    _report_clear_scope_checks(paths["report"], selected_chain_ids)
    if scope_warning:
        _report_append_checks(paths["report"], [scope_warning])

    selected_chains = [c for c in all_chains if c.get("chain_id") in selected_chain_ids]

    if shots_filter is not None:
        any_shots_match = any(
            any(s.get("shot_number") in shots_filter for s in (c.get("shots") or []))
            for c in selected_chains
        )
        if not any_shots_match:
            entry = {
                "code": "SHOTS_FILTER_ZERO", "level": "error",
                "message": f"shots filter matched 0 shots (filter={sorted(shots_filter)})",
            }
            _report_append_checks(paths["report"], [entry])
            raise RuntimeError(f"B21: shots filter matched 0 shots (filter={sorted(shots_filter)})")

    duration_mismatches = check_duration_mismatches(selected_chains, items, shots_filter)
    if duration_mismatches:
        entry = {
            "code": "B01", "level": "error",
            "message": "shot duration_s in shots.json does not match scene_spec.json; rerun blockout_scene_builder",
            "details": {"shots": duration_mismatches},
        }
        _report_append_checks(paths["report"], [entry])
        raise RuntimeError("B01: " + entry["message"])

    ffmpeg_bin = _find_ffmpeg()
    mode = os.getenv("BLOCKOUT_BLENDER_MODE", "binary").strip().lower()

    shots_rendered = 0
    frames_total = 0
    any_chain_failed = False
    junction_checks: List[Dict[str, Any]] = []

    for chain_id in selected_chain_ids:
        chain_spec = next((c for c in all_chains if c.get("chain_id") == chain_id), None)
        if chain_spec is None:
            any_chain_failed = True
            _report_append_checks(paths["report"], [{
                "code": "B17", "level": "error", "chain_id": chain_id,
                "message": f"chain {chain_id!r} has no record in scene_spec.json", "details": {"chain_id": chain_id},
            }])
            continue

        chain_shots = chain_spec.get("shots") or []
        if not chain_shots:
            continue

        if shots_filter is not None:
            chain_shots = [s for s in chain_shots if s.get("shot_number") in shots_filter]
            if not chain_shots:
                continue

        # раздел 12: world (ground/sun) is defined PER CHAIN, not at the
        # scene_spec.json root -- the root never has a "world" key.
        world = chain_spec.get("world") or {}

        first_shot = chain_shots[0]
        scene_number = chain_spec.get("scene_number")
        first_item = find_shot_item(items, scene_number, first_shot.get("shot_number"), "start")
        width = first_item.get("width") if first_item else None
        height = first_item.get("height") if first_item else None
        w_render, h_render, computed_aspect, resolution_warnings = resolve_render_resolution(
            caps_data, width, height, resolution
        )
        for w in resolution_warnings:
            w["chain_id"] = chain_id
        if resolution_warnings:
            _report_append_checks(paths["report"], resolution_warnings)

        if check_chain_size_mismatch(chain_shots, items, scene_number, width, height):
            _report_append_checks(paths["report"], [{
                "code": "P13", "level": "warning", "reason": "chain_size_mismatch", "chain_id": chain_id,
                "message": f"chain {chain_id}: shots have differing width/height; rendering by the first shot's size",
            }])

        if not check_b16(chain_spec.get("camera_aspect"), computed_aspect):
            any_chain_failed = True
            _report_append_checks(paths["report"], [{
                "code": "B16", "level": "error", "chain_id": chain_id,
                "message": f"chain {chain_id}: camera_aspect changed; rerun blockout_scene_builder first",
                "details": {"scene_spec": chain_spec.get("camera_aspect"), "computed": computed_aspect},
            }])
            continue

        chain_dir = paths["blockout_dir"] / chain_id
        spec_hash = compute_spec_hash(chain_spec)
        chain_assets_used = collect_chain_assets_used(chain_spec.get("objects") or [])

        shots_meta = []
        for shot in chain_shots:
            duration_s = int(shot["duration_s"])
            n_render = blockout_common.n_render(duration_s, fps_int)
            chain_frame_start = blockout_common.frame_blender(float(shot["t_start"]), fps_int)
            shot_key = f"scene_{chain_spec.get('scene_number'):02d}_shot_{shot.get('shot_number'):02d}"
            shot_dir = paths["blockout_dir"] / f"scene_{chain_spec.get('scene_number'):02d}_shot_{shot.get('shot_number'):02d}"
            shots_meta.append({
                "shot_key": shot_key,
                "shot_dir": str(shot_dir),
                "chain_frame_start": chain_frame_start,
                "chain_frame_end": chain_frame_start + n_render - 1,
                "n_render": n_render,
                # Э4, раздел 15.2: render_shot.py needs these to write
                # state/shot_NN_out.json -- not derivable inside Blender
                # without duplicating the chain timeline math (Р3).
                "shot_number": shot.get("shot_number"),
                "t_start": float(shot["t_start"]),
                "duration_s": duration_s,
                "subject_focus": shot.get("subject_focus") or [],
            })

        chain_payload = build_chain_payload(chain_spec, world, fps_int)
        output_json_path = chain_dir / "render_result.json"
        blend_path = chain_dir / "chain.blend"
        blender_payload = {
            "chain": chain_payload,
            "shots": shots_meta,
            "resolution": [w_render, h_render],
            "fps": fps_int,
            "output_path": str(output_json_path),
            "blend_path": str(blend_path),
            # Э4, раздел 15.2: render_shot.py needs chain_id to write
            # state/shot_NN_out.json's own "chain_id" field.
            "chain_id": chain_id,
            "quiet": quiet_effective,
            "engine": engine_effective,
        }

        chain_timeout = compute_chain_render_timeout(sum(m["n_render"] for m in shots_meta))
        result = blockout_common.run_blender_script(
            _RENDER_SHOT_SCRIPT, blender_payload, output_json_path,
            timeout=chain_timeout, module_callable=blockout_render_shot.run if mode == "module" else None,
        )

        if not result.get("ok"):
            error_text = str(result.get("error") or "")
            hard_prefix, _, hard_detail = error_text.partition(":")
            # CAM_INSIDE/SUBJ_HIDDEN are scene-geometry defects, not launch
            # failures (раздел hard-error propagation): unlike B12, they
            # must abort the whole run by raising, not just mark this one
            # chain "partial" and continue.
            if hard_prefix in ("CAM_INSIDE", "SUBJ_HIDDEN"):
                hard_detail = hard_detail.strip()
                if hard_prefix == "CAM_INSIDE":
                    code, message = "B18", f"camera inside object — scene needs rework: {hard_detail}"
                else:
                    code, message = "B19", f"subject never in frustum — {hard_detail}"
                _report_append_checks(paths["report"], [{
                    "code": code, "level": "error", "chain_id": chain_id, "message": message,
                }])
                raise RuntimeError(f"{code}: {message}")

            any_chain_failed = True
            _report_append_checks(paths["report"], [{
                "code": "B12", "level": "error", "chain_id": chain_id,
                "message": f"chain {chain_id}: Blender render launch failed: {result.get('error')}",
            }])
            continue

        chain_warnings = list(result.get("chain_warnings") or [])
        if chain_warnings:
            _report_append_checks(
                paths["report"],
                [{"code": "P01", "level": "warning", "chain_id": chain_id, "message": w} for w in chain_warnings],
            )

        results_by_key = result.get("shots") or {}

        # Pass 1: per-shot validation (B03-B05, B07, B11) -- raises on
        # failure, same as before (раздел 10.3.1). Junction computation
        # (раздел 15.3) needs every shot of the chain already validated
        # (it compares a shot against its *neighbour*), so manifest.json
        # and shots.json patches are deferred to pass 2 below rather than
        # written per-shot here.
        shot_records: List[Dict[str, Any]] = []
        for idx, shot in enumerate(chain_shots):
            shot_number = shot.get("shot_number")
            meta = shots_meta[idx]
            shot_result = results_by_key.get(meta["shot_key"])
            duration_s = int(shot["duration_s"])
            n_render = meta["n_render"]
            n_video = blockout_common.n_video(duration_s, fps_int)

            if not shot_result:
                any_chain_failed = True
                entry = {
                    "code": "B03", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: no render result returned",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B03: " + entry["message"])

            frame_files = shot_result.get("frame_files") or []
            if not check_b03(frame_files, n_render):
                entry = {
                    "code": "B03", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: expected {n_render} frames, got {len(frame_files)}",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B03: " + entry["message"])
            if not check_b04(frame_files, n_render):
                entry = {
                    "code": "B04", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: frame numbering has gaps",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B04: " + entry["message"])

            shot_dir = Path(meta["shot_dir"])
            frames_dir = shot_dir / "frames"
            frame_sizes = _read_frame_sizes(frames_dir, frame_files)
            if not check_b05(frame_sizes):
                entry = {
                    "code": "B05", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: rendered frames have inconsistent sizes",
                    "details": {"sizes": [list(s) if s else None for s in frame_sizes]},
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B05: " + entry["message"])

            if not check_b07(shot_result.get("ref_start_sha256"), shot_result.get("frame_0001_sha256")):
                entry = {
                    "code": "B07", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: ref_start.png does not match frame_0001.png",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B07: " + entry["message"])
            if not check_b07(shot_result.get("ref_end_sha256"), shot_result.get("frame_last_sha256")):
                entry = {
                    "code": "B07", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: ref_end.png does not match the last frame",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B07: " + entry["message"])

            video_path = shot_dir / "blockout_ref.mp4"
            if ffmpeg_bin:
                args = build_ffmpeg_args(frames_dir, fps_int, n_video, video_path)
                ok, error_detail = _run_ffmpeg(ffmpeg_bin, args, timeout=600.0)
                if not ok:
                    entry = {
                        "code": "B11", "level": "error", "chain_id": chain_id,
                        "message": f"chain {chain_id} shot {shot_number}: ffmpeg failed: {error_detail}",
                    }
                    _report_append_checks(paths["report"], [entry])
                    raise RuntimeError("B11: " + entry["message"])
            else:
                entry = {
                    "code": "B11", "level": "error", "chain_id": chain_id,
                    "message": f"chain {chain_id} shot {shot_number}: ffmpeg not found in PATH",
                }
                _report_append_checks(paths["report"], [entry])
                raise RuntimeError("B11: " + entry["message"])

            shot_records.append({
                "shot_number": shot_number,
                "duration_s": duration_s,
                "t_start": float(shot["t_start"]),
                "frame_files": frame_files,
                "shot_dir": shot_dir,
                "video_path": video_path,
                "shot_result": shot_result,
                "first_frame_path": frames_dir / frame_files[0],
                "last_frame_path": frames_dir / frame_files[-1],
            })

            shots_rendered += 1
            frames_total += len(frame_files)

        # Pass 2a: junction check (раздел 15.3, B06/P07) -- one comparison
        # per internal gap, attributed to the *receiving* shot (the one
        # further along shot_number, раздел 15.3: its own first frame is
        # what disagreed with the copied starting image). Never raises
        # (B06 is one of the three exceptions, раздел 10.3.1).
        scene_number = chain_spec.get("scene_number")
        junction_results: Dict[int, Dict[str, Any]] = {}
        junction_flag_patches: Dict[Tuple[int, int, str], Any] = {}
        for i in range(len(shot_records) - 1):
            prev_record = shot_records[i]
            next_record = shot_records[i + 1]
            junction = compare_junction_frames(
                prev_record["shot_result"].get("frame_last_sha256"),
                next_record["shot_result"].get("frame_0001_sha256"),
                prev_record["last_frame_path"],
                next_record["first_frame_path"],
            )
            junction_results[i] = junction

            status = junction["status"]
            receiving_shot_number = next_record["shot_number"]
            check_entry: Dict[str, Any] = {
                "chain_id": chain_id, "scene_number": scene_number,
                "shot_number": receiving_shot_number, "prev_shot_number": prev_record["shot_number"],
                "status": status,
            }
            if "max_channel_diff" in junction:
                check_entry["max_channel_diff"] = junction["max_channel_diff"]
                check_entry["diff_pixels_ratio"] = junction["diff_pixels_ratio"]
            if "reason" in junction:
                check_entry["reason"] = junction["reason"]
            if "warning" in junction:
                check_entry["warning"] = junction["warning"]
            junction_checks.append(check_entry)

            flag_start_key = (scene_number, receiving_shot_number, "start")
            flag_end_key = (scene_number, receiving_shot_number, "end")
            if status == "failed":
                any_chain_failed = True
                junction_flag_patches[flag_start_key] = True
                junction_flag_patches[flag_end_key] = True
                _report_append_checks(paths["report"], [{
                    "code": "B06", "level": "error", "chain_id": chain_id,
                    "message": (
                        f"chain {chain_id}: junction between shot {prev_record['shot_number']} and "
                        f"shot {receiving_shot_number} failed (раздел 15.3)"
                    ),
                    "details": check_entry,
                }])
            else:
                # раздел 15.3: "признак снимается тем же рендерером" -- a
                # junction re-checked and accepted this run (exact or
                # soft) clears any stale blockout_junction_failed.
                junction_flag_patches[flag_start_key] = _REMOVE_FIELD
                junction_flag_patches[flag_end_key] = _REMOVE_FIELD
                if status == "soft":
                    _report_append_checks(paths["report"], [{
                        "code": "P07", "level": "warning", "chain_id": chain_id,
                        "message": (
                            f"chain {chain_id}: junction between shot {prev_record['shot_number']} and "
                            f"shot {receiving_shot_number} passed on the soft threshold"
                        ),
                        "details": check_entry,
                    }])

        # Pass 2b: manifest.json + shots.json patches, now that every
        # internal junction of this chain is known. shots_json_patches is
        # local to THIS chain and written out immediately below (like
        # manifest.json), not accumulated across the whole run -- a later
        # chain raising (e.g. B03) must not discard an earlier, already
        # successful chain's fields (раздел 13.4: "остальные цепочки
        # продолжают рендериться с сохранением результата").
        chain_shots_json_patches: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
        for idx, record in enumerate(shot_records):
            junction_with_prev = junction_results.get(idx - 1) if idx > 0 else None
            junction_with_next = junction_results.get(idx) if idx < len(shot_records) - 1 else None

            manifest_warnings = list(chain_warnings)
            for label, junction in (("previous", junction_with_prev), ("next", junction_with_next)):
                if junction and junction.get("status") == "soft":
                    manifest_warnings.append(
                        f"P07: junction with {label} shot passed on soft threshold "
                        f"(max_channel_diff={junction.get('max_channel_diff')}, "
                        f"diff_pixels_ratio={junction.get('diff_pixels_ratio'):.5f})"
                    )

            manifest = build_manifest(
                scene_number=scene_number,
                shot_number=record["shot_number"],
                chain_id=chain_id,
                duration_s=record["duration_s"],
                fps=fps_int,
                frames_rendered=len(record["frame_files"]),
                spec_hash=spec_hash,
                resolution=record["shot_result"].get("resolution") or [w_render, h_render],
                t_start_in_chain=record["t_start"],
                junction_with_prev=junction_with_prev,
                junction_with_next=junction_with_next,
                assets_used=chain_assets_used,
                warnings=manifest_warnings,
                blender_version=blender_version,
            )
            _write_json_atomic(record["shot_dir"] / "manifest.json", manifest)

            shot_number = record["shot_number"]
            blockout_ref_image_start = str(record["shot_dir"] / "ref_start.png")
            blockout_ref_image_end = str(record["shot_dir"] / "ref_end.png")
            rendered_at = manifest["rendered_at"]
            patch_start = {
                "blockout_ref_image": blockout_ref_image_start,
                "blockout_video": str(record["video_path"]),
                "blockout_rendered_at": rendered_at,
            }
            patch_end = {
                "blockout_ref_image": blockout_ref_image_end,
                "blockout_video": str(record["video_path"]),
                "blockout_rendered_at": rendered_at,
            }
            start_key = (scene_number, shot_number, "start")
            end_key = (scene_number, shot_number, "end")
            if start_key in junction_flag_patches:
                patch_start["blockout_junction_failed"] = junction_flag_patches[start_key]
            if end_key in junction_flag_patches:
                patch_end["blockout_junction_failed"] = junction_flag_patches[end_key]
            chain_shots_json_patches[start_key] = patch_start
            chain_shots_json_patches[end_key] = patch_end

        if chain_shots_json_patches:
            _merge_write_shots_blockout_fields(paths["shots"], chain_shots_json_patches)

    status = "partial" if any_chain_failed else ("success" if shots_rendered > 0 else "warning")
    # раздел 22, критерий A04: "доля стыков, прошедших только по мягкому
    # порогу (P07), фиксируется в отчёте" -- simple aggregate over this
    # run's junction_checks (every internal junction checked this run,
    # regardless of chain), no separate counters kept beyond what's
    # already in `checks` above.
    soft_junction_count = sum(1 for c in junction_checks if c.get("status") == "soft")
    soft_junction_ratio = (soft_junction_count / len(junction_checks)) if junction_checks else None
    _report_finalize_summary(paths["report"], {
        "status": status, "shots_rendered": shots_rendered, "frames_total": frames_total,
        "soft_junction_ratio": soft_junction_ratio,
    })

    return {
        "status": status,
        "shots_rendered": shots_rendered,
        "frames_total": frames_total,
        "junction_checks": junction_checks,
        "artifact_path": str(paths["blockout_dir"]),
    }
