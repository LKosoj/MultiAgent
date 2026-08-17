"""Э4: writes ``93_blockout/{chain_id}/state/shot_{NN}_out.json`` -- the
"state-transfer" level of continuity (docs/tz-blockout-reference-pipeline.md,
section 15.1, 15.2), executed inside Blender at the end of each shot's
render window.

Not launched directly with ``blender -P``: this module is imported by
``render_shot.py`` (the actual entry point, section 13.4), which calls
``export_shot_state()`` once per rendered shot, right after
``_render_shot_window()`` leaves the scene on that shot's last frame.

The file is **write-only** (раздел 15.2, "файл пишется только на запись"):
nothing in the pipeline reads it back -- continuity is guaranteed by the
chain's single Blender timeline (Р2), not by this snapshot. It exists for
humans and tests diagnosing a junction tear (Приложение Б).

The module MUST import without bpy: ``resolve_clip_duration_s()``,
``resolve_object_animation_phase()``, ``build_object_state()``,
``build_shot_state()``, ``state_output_path()`` and ``write_state_json()``
are pure/IO-only functions (no bpy) and the tested surface of this file.
``extract_camera_state()``, ``extract_object_raw_state()`` and
``export_shot_state()`` touch bpy and raise ``RuntimeError`` outside
Blender.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    import bpy  # type: ignore

    BPY_AVAILABLE = True
except ImportError:
    bpy = None
    BPY_AVAILABLE = False


def _ensure_repo_root_on_syspath() -> Path:
    """Make the repo root importable regardless of how this script is
    launched (as ``blender -P render_shot.py``, both files run outside the
    ``custom_tools.storybook`` package)."""
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "custom_tools").is_dir() and (p / "agent_command.py").exists():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            return p
    return Path.cwd()


_REPO_ROOT = _ensure_repo_root_on_syspath()

from custom_tools.storybook import blockout_assets  # noqa: E402
from custom_tools.storybook import blockout_common  # noqa: E402


# =============================================================================
# Pure functions (раздел 15.2)
# =============================================================================


def resolve_clip_duration_s(
    asset_id: Optional[str], clip_name: Optional[str], assets_root: Optional[Path] = None
) -> Optional[float]:
    """Looks up ``clip_name``'s ``duration_s`` in the library object's
    ``meta.json``/``index.json`` entry (раздел 9.2, ``animations`` list).
    Returns ``None`` when unresolvable (no asset_id, asset not found, clip
    not found, or a non-positive duration) -- the caller degrades to
    omitting the ``animation`` field rather than raising."""
    if not asset_id or not clip_name:
        return None
    index = blockout_assets.read_index(assets_root)
    entry = blockout_assets.find_asset_by_id(index, asset_id)
    if entry is None:
        return None
    for anim in entry.get("animations") or []:
        if anim.get("name") != clip_name:
            continue
        duration = anim.get("duration_s")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            return float(duration)
        return None
    return None


def resolve_object_animation_phase(
    animation_spec: Optional[Dict[str, Any]],
    t_in_chain: float,
    *,
    asset_id: Optional[str] = None,
    assets_root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """раздел 15.2: ``{"clip": ..., "phase": ...}`` for one object's state
    snapshot, reusing ``blockout_common.animation_phase()`` (Р3, no
    duplicate formula) -- ``t0`` is always ``0`` (chain start, раздел
    15.2). Returns ``None`` when the clip or its duration can't be
    resolved (build_scene.apply_animation() already records the matching
    P01 warning elsewhere; this file is diagnostic-only, so it degrades
    silently rather than raising, раздел 15.2)."""
    if not animation_spec:
        return None
    clip = animation_spec.get("clip")
    if not clip:
        return None
    duration_s = resolve_clip_duration_s(asset_id, clip, assets_root)
    if duration_s is None:
        return None
    phase_at_t0 = float(animation_spec.get("phase_at_t0", 0.0))
    speed = float(animation_spec.get("speed", 1.0)) or 1.0
    loop = bool(animation_spec.get("loop", True))
    phase = blockout_common.animation_phase(
        phase_at_t0=phase_at_t0, t_in_chain=t_in_chain, t0=0.0, speed=speed,
        clip_duration_s=duration_s, loop=loop,
    )
    return {"clip": clip, "phase": phase}


def build_object_state(
    obj_spec: Dict[str, Any],
    location: Sequence[float],
    rotation_deg: Sequence[float],
    scale: Sequence[float],
    visible: bool,
    t_in_chain: float,
    *,
    assets_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Assembles one object's entry of the ``objects`` list (раздел 15.2
    example shape). ``location``/``rotation_deg``/``scale``/``visible`` are
    values already read off the Blender object by the caller (раздел
    15.2, "Реализация обязана брать значения у Blender после установки
    кадра")."""
    state: Dict[str, Any] = {
        "instance_id": obj_spec.get("instance_id"),
        "location": [float(v) for v in location],
        "rotation_deg": [float(v) for v in rotation_deg],
        "scale": [float(v) for v in scale],
        "visible": bool(visible),
    }
    phase_entry = resolve_object_animation_phase(
        obj_spec.get("animation"), t_in_chain, asset_id=obj_spec.get("asset_id"), assets_root=assets_root
    )
    if phase_entry:
        state["animation"] = phase_entry
    return state


def build_shot_state(
    *,
    chain_id: str,
    shot_number: int,
    t_in_chain: float,
    blender_version: Optional[str],
    camera: Dict[str, Any],
    objects: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Верхний уровень раздел 15.2's example shape."""
    return {
        "chain_id": chain_id,
        "shot_number": shot_number,
        "t_in_chain": t_in_chain,
        "blender_version": blender_version,
        "camera": camera,
        "objects": objects,
    }


def state_output_path(chain_dir: Path, shot_number: int) -> Path:
    """раздел 8: ``93_blockout/{chain_id}/state/shot_{NN}_out.json``,
    sibling of ``chain.blend`` -- lives under the CHAIN directory, not the
    per-shot ``scene_NN_shot_MM/`` directory."""
    return Path(chain_dir) / "state" / f"shot_{int(shot_number):02d}_out.json"


def write_state_json(path: Path, state: Dict[str, Any]) -> None:
    """раздел 15.2: "файл пишется только на запись" -- diagnostic-only, no
    reader ever needs read-your-writes consistency and no two writers ever
    target the same shot's state file, so a plain atomic write (no
    sidecar lock) is enough."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


# =============================================================================
# bpy-touching functions (раздел 15.2)
# =============================================================================


def _require_bpy() -> None:
    if not BPY_AVAILABLE:
        raise RuntimeError("this function requires the Blender 'bpy' module")


def extract_camera_state(camera_obj: Any) -> Dict[str, Any]:
    """раздел 15.2: the camera's rotation MUST come from ``matrix_world``
    (the Track-To constraint drives it, раздел 12) -- ``rotation_euler``
    stays zero and would be wrong."""
    _require_bpy()
    matrix = camera_obj.matrix_world
    location = tuple(matrix.translation)
    rotation_deg = tuple(math.degrees(a) for a in matrix.to_euler())
    return {
        "location": [float(v) for v in location],
        "rotation_deg": [float(v) for v in rotation_deg],
        "lens_mm": float(camera_obj.data.lens),
    }


def extract_object_raw_state(obj: Any) -> Dict[str, Any]:
    """раздел 15.2: an ordinary object's rotation does not need
    ``matrix_world`` (only the camera does) -- ``.location`` /
    ``.rotation_euler`` / ``.scale`` are read directly."""
    _require_bpy()
    return {
        "location": [float(v) for v in obj.location],
        "rotation_deg": [math.degrees(a) for a in obj.rotation_euler],
        "scale": [float(v) for v in obj.scale],
        "visible": not obj.hide_render,
    }


def export_shot_state(
    *,
    chain_id: str,
    shot_number: int,
    t_in_chain: float,
    blender_version: Optional[str],
    camera_obj: Any,
    object_specs: Sequence[Dict[str, Any]],
    resolve_object: Callable[[str], Any],
    assets_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Orchestrates one shot's state snapshot (раздел 15.2). ``resolve_object``
    maps an ``instance_id`` to its bpy object (``bpy.data.objects.get`` in
    practice) -- kept as a caller-supplied lookup so this module doesn't
    need to know how ``build_scene.py`` names scene objects."""
    _require_bpy()
    camera_state = extract_camera_state(camera_obj)
    objects_state: List[Dict[str, Any]] = []
    for obj_spec in object_specs:
        instance_id = obj_spec.get("instance_id")
        bpy_obj = resolve_object(instance_id) if instance_id else None
        if bpy_obj is None:
            continue
        raw = extract_object_raw_state(bpy_obj)
        objects_state.append(
            build_object_state(
                obj_spec, raw["location"], raw["rotation_deg"], raw["scale"], raw["visible"],
                t_in_chain, assets_root=assets_root,
            )
        )
    return build_shot_state(
        chain_id=chain_id, shot_number=shot_number, t_in_chain=t_in_chain,
        blender_version=blender_version, camera=camera_state, objects=objects_state,
    )
