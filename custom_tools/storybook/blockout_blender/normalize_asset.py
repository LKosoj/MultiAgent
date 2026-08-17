"""Normalizes a downloaded/uploaded 3D asset to the library conventions
(docs/tz-blockout-reference-pipeline.md, section 9.3), executed inside
Blender.

Invocation contract (section 13.4): ``blender -b -P normalize_asset.py --
<input.json>``. Input JSON: {"source_path", "category", "output_path",
"output_glb_path"}. Output, written atomically to "output_path": success
{"ok": true, "dimensions_m", "has_armature", "animations", "forward_axis",
"up_axis", "pivot", "blender_version", "warnings"}; failure {"ok": false,
"error": str} with a non-zero process exit code. Stdout/stderr are a log,
not parsed by callers.

The module MUST import without bpy: canonicalize_clip_name(),
resolve_animation_names(), compute_dimensions(), compute_pivot_offset(),
compute_out_of_range_correction(), detect_named_forward_xy() and
yaw_delta_to_plus_y() are pure functions and the tested surface of this
module. run() is the only place that touches bpy; outside Blender it
raises RuntimeError.
"""

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import bpy  # type: ignore
    import mathutils  # type: ignore

    BPY_AVAILABLE = True
except ImportError:
    bpy = None
    mathutils = None
    BPY_AVAILABLE = False


def _ensure_repo_root_on_syspath() -> Path:
    """Make the repo root importable regardless of how this script is
    launched (as `blender -P normalize_asset.py`, this file runs as
    __main__, outside the custom_tools.storybook package)."""
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "custom_tools").is_dir() and (p / "agent_command.py").exists():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            return p
    return Path.cwd()


_REPO_ROOT = _ensure_repo_root_on_syspath()

from custom_tools.storybook.blockout_assets import typical_height_m  # noqa: E402


_KNOWN_CLIP_NAMES = ("walk", "run", "idle")


def canonicalize_clip_name(name: str) -> str:
    """Lowercase match against walk/run/idle as a substring; unrecognized
    names are kept as-is (section 9.5)."""
    lowered = (name or "").lower()
    for known in _KNOWN_CLIP_NAMES:
        if known in lowered:
            return known
    return name


def resolve_animation_names(raw_names: Sequence[str]) -> List[str]:
    """canonicalize_clip_name() each clip; if none of several clips were
    recognized, they keep their own names, but a single unrecognized clip
    becomes "idle" (most likely a rest pose)."""
    canonical = [canonicalize_clip_name(name) for name in raw_names]
    if len(raw_names) == 1 and canonical[0] not in _KNOWN_CLIP_NAMES:
        return ["idle"]
    return canonical


def compute_dimensions(bbox_min: Sequence[float], bbox_max: Sequence[float]) -> Tuple[float, float, float]:
    return (
        bbox_max[0] - bbox_min[0],
        bbox_max[1] - bbox_min[1],
        bbox_max[2] - bbox_min[2],
    )


def compute_pivot_offset(bbox_min: Sequence[float], bbox_max: Sequence[float]) -> Tuple[float, float, float]:
    """Offset to move the pivot to the base-center (x/y centered, z at the
    bottom face) of the bounding box."""
    minx, miny, minz = bbox_min[0], bbox_min[1], bbox_min[2]
    maxx, maxy = bbox_max[0], bbox_max[1]
    return (-(minx + maxx) / 2.0, -(miny + maxy) / 2.0, -minz)


def compute_out_of_range_correction(height_m: float, category: str) -> Optional[float]:
    """None if 0.01 <= height_m <= 100; otherwise the scale factor that
    brings the object to the category's typical height (section 9.5)."""
    if 0.01 <= height_m <= 100:
        return None
    return typical_height_m(category) / height_m


_FRONT_TOKENS = (
    "wheels-front", "wheel-front", "front-wheel", "cowcatcher", "headlight", "nose",
    "smokebox", "body_front", "detail_front", "underside_detail_front",
)
_BACK_TOKENS = (
    "wheels-back", "wheel-back", "back-wheel", "cab", "tender-hitch",
    "body_back", "back_detail", "underside_detail_back",
)


def _name_hits(name: str, tokens: Sequence[str]) -> bool:
    lowered = (name or "").lower()
    return any(token in lowered for token in tokens)


def detect_named_forward_xy(parts: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """Infer a horizontal forward vector from child-part names.

    Kenney (and many kitbash) vehicles ship ``wheels-front`` / ``wheels-back``
    (or a lone ``front`` mesh). Section 9.3 requires the *normalized* asset
    to face ``+Y``; the TZ also says a download has no reliable facing, so
    this only fires when names give a clear cue. No cue → ``None`` and the
    mesh is left as imported (``forward_axis`` is still recorded as ``+Y``).
    """
    fronts: List[Tuple[float, float]] = []
    backs: List[Tuple[float, float]] = []
    for part in parts:
        name = str(part.get("name") or "")
        loc = part.get("location") or part.get("loc") or (0.0, 0.0, 0.0)
        if not isinstance(loc, (list, tuple)) or len(loc) < 2:
            continue
        xy = (float(loc[0]), float(loc[1]))
        # Prefer the explicit wheel tokens over a bare "front" substring so
        # "front-porch" furniture does not get treated as a vehicle nose.
        if _name_hits(name, _FRONT_TOKENS) or (
            "front" in name.lower() and "back" not in name.lower() and _name_hits(name, ("wheel", "wheels"))
        ):
            fronts.append(xy)
        elif _name_hits(name, _BACK_TOKENS) or (
            "back" in name.lower() and "front" not in name.lower() and _name_hits(name, ("wheel", "wheels"))
        ):
            backs.append(xy)
    if not fronts:
        return None
    fx = sum(p[0] for p in fronts) / len(fronts)
    fy = sum(p[1] for p in fronts) / len(fronts)
    if backs:
        bx = sum(p[0] for p in backs) / len(backs)
        by = sum(p[1] for p in backs) / len(backs)
        dx, dy = fx - bx, fy - by
    else:
        dx, dy = fx, fy
    if abs(dx) < 1e-8 and abs(dy) < 1e-8:
        return None
    return dx, dy


def yaw_delta_to_plus_y(dx: float, dy: float) -> float:
    """Radians around +Z that rotate vector ``(dx, dy)`` onto ``+Y``."""
    return (math.pi / 2.0) - math.atan2(dy, dx)


def _compute_scene_bbox(objects) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    xs: List[float] = []
    ys: List[float] = []
    zs: List[float] = []
    for obj in objects:
        bound_box = getattr(obj, "bound_box", None)
        if not bound_box:
            continue
        for corner in bound_box:
            world_corner = obj.matrix_world @ mathutils.Vector(corner)
            xs.append(world_corner.x)
            ys.append(world_corner.y)
            zs.append(world_corner.z)
    if not xs:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The only bpy-touching function: imports the glTF, measures the bbox,
    shifts the pivot to base_center, detects the armature/clips, corrects an
    out-of-range scale and exports the normalized .glb. Raises RuntimeError
    outside Blender."""
    if not BPY_AVAILABLE:
        raise RuntimeError("normalize_asset.run() requires the Blender 'bpy' module")

    source_path = payload["source_path"]
    category = payload["category"]
    output_glb_path = payload["output_glb_path"]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=source_path)

    imported_objects = list(bpy.context.selected_objects)
    scene_objects = list(bpy.context.scene.objects)
    named_parts = [
        {"name": obj.name, "location": list(obj.matrix_world.translation)}
        for obj in scene_objects
    ]
    forward_xy = detect_named_forward_xy(named_parts)
    warnings: List[str] = []
    if forward_xy is not None:
        delta = yaw_delta_to_plus_y(forward_xy[0], forward_xy[1])
        # Bake the yaw into the mesh. Leaving it on the root rotation would
        # be undone the first time the renderer keyframes rotation_euler
        # (identity = "face +Y" in scene_spec).
        if abs(delta) > 1e-4:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in imported_objects:
                if obj.parent is None:
                    if getattr(obj, "rotation_mode", None) != "XYZ":
                        obj.rotation_mode = "XYZ"
                    obj.rotation_euler[2] = float(obj.rotation_euler[2]) + delta
                    obj.select_set(True)
                    bpy.context.view_layer.objects.active = obj
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
            warnings.append(
                f"rotated {math.degrees(delta):.1f}° around +Z so named front faces +Y"
            )

    bbox_min, bbox_max = _compute_scene_bbox(imported_objects)
    dimensions = compute_dimensions(bbox_min, bbox_max)
    offset = compute_pivot_offset(bbox_min, bbox_max)

    for obj in imported_objects:
        if obj.parent is None:
            obj.location.x += offset[0]
            obj.location.y += offset[1]
            obj.location.z += offset[2]

    armature = next((obj for obj in imported_objects if obj.type == "ARMATURE"), None)
    has_armature = armature is not None

    fps = bpy.context.scene.render.fps or 24
    raw_clip_names: List[str] = []
    durations: Dict[str, float] = {}
    for action in bpy.data.actions:
        raw_clip_names.append(action.name)
        frame_range = action.frame_range
        durations[action.name] = (frame_range[1] - frame_range[0]) / float(fps)

    resolved_names = resolve_animation_names(raw_clip_names)
    animations = [
        {"name": resolved, "duration_s": round(durations.get(raw, 0.0), 3), "loop": True}
        for raw, resolved in zip(raw_clip_names, resolved_names)
    ]

    height_m = dimensions[2]
    correction = compute_out_of_range_correction(height_m, category)
    if correction is not None:
        for obj in imported_objects:
            if obj.parent is None:
                obj.scale = tuple(s * correction for s in obj.scale)
        dimensions = tuple(d * correction for d in dimensions)
        warnings.append(
            f"height {height_m:.4f}m out of range, rescaled by {correction:.4f} to match typical {category} height"
        )

    Path(output_glb_path).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported_objects:
        obj.select_set(True)
    bpy.ops.export_scene.gltf(filepath=str(output_glb_path), export_format="GLB", use_selection=True)

    return {
        "ok": True,
        "dimensions_m": list(dimensions),
        "has_armature": has_armature,
        "animations": animations,
        "forward_axis": "+Y",
        "up_axis": "+Z",
        "pivot": "base_center",
        "blender_version": ".".join(str(v) for v in bpy.app.version),
        "warnings": warnings,
    }


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv if argv is None else argv)
    if "--" not in raw_argv:
        print("normalize_asset: missing '--' separator before the input JSON path", file=sys.stderr)
        return 1
    after = raw_argv[raw_argv.index("--") + 1 :]
    if not after:
        print("normalize_asset: missing input JSON path after '--'", file=sys.stderr)
        return 1
    input_json_path = after[0]

    try:
        payload = json.loads(Path(input_json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"normalize_asset: failed to read input JSON: {exc}", file=sys.stderr)
        return 1

    try:
        output_path = Path(payload["output_path"])
    except KeyError as exc:
        print(f"normalize_asset: input JSON is missing {exc}", file=sys.stderr)
        return 1

    try:
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 - always report failure as JSON, never crash Blender silently
        result = {"ok": False, "error": str(exc)}

    _write_json_atomic(output_path, result)

    if not result.get("ok"):
        print(f"normalize_asset: FAILED: {result.get('error')}")
        return 1
    print(f"normalize_asset: OK dimensions_m={result.get('dimensions_m')} has_armature={result.get('has_armature')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
