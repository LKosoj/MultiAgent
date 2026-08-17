"""Э3: assembles one continuity chain's Blender scene from its
``scene_spec.json`` chain entry (docs/tz-blockout-reference-pipeline.md,
section 12), executed inside Blender.

Not launched directly with ``blender -P``: ``render_shot.py`` is the actual
entry point (one Blender process per chain, section 13.4) and calls
``build_chain_scene()`` in-process before rendering any shot window.

The module MUST import without bpy: ``sun_direction()``,
``blender_interp_for()``, ``is_proxy_object()``, ``proxy_body_plan_for_asset_id()``
and ``resolve_object_geometry()`` are pure functions and the tested surface
of this module. Everything else touches bpy and raises ``RuntimeError``
outside Blender.

Deviation from ``scene_spec.json`` as documented in section 12 (flagged in
the Э3 final report): the *actual* output of ``blockout_scene_builder``
(Э2) never populates ``asset_id``/``proxy``/``height_m``/``source`` on a
chain object — only ``instance_id`` and ``tracks`` (optionally
``animation``) are ever present. ``resolve_object_geometry()`` treats any
object without a usable ``asset_id`` as an implicit generic box proxy
(section 9.4, ``__proxy_box__`` body plan ``"none"``) and returns a
warning for the caller to fold into ``report.json``/``manifest.json`` as
P01. This keeps the renderer spec-compliant ("does not read
``asset_map.json``") while staying usable against Э2's real output.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

CAMERA_TARGET_NAME = "blockout_camera_target"
CAMERA_OBJECT_NAME = "blockout_camera"


# =============================================================================
# Pure functions (раздел 12, 13.2)
# =============================================================================


def sun_direction(elevation_deg: float, azimuth_deg: float) -> Tuple[float, float, float]:
    """``x = cos(el)*cos(az); y = cos(el)*sin(az); z = sin(el)`` (раздел 12,
    "world.sun не создаёт источник света") -> ``scene.display.light_direction``."""
    el = math.radians(elevation_deg)
    az = math.radians(azimuth_deg)
    return math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)


# interp -> (fcurve.keyframe_point.interpolation, .easing); раздел 12,
# "Требования к трекам": linear->LINEAR, constant->CONSTANT, bezier->BEZIER
# with both handles AUTO_CLAMPED (handled by the caller, not encoded here),
# ease_* -> SINE with the matching easing.
_INTERP_MAP: Dict[str, Tuple[str, str]] = {
    "linear": ("LINEAR", "AUTO"),
    "constant": ("CONSTANT", "AUTO"),
    "bezier": ("BEZIER", "AUTO"),
    "ease_in": ("SINE", "EASE_IN"),
    "ease_out": ("SINE", "EASE_OUT"),
    "ease_in_out": ("SINE", "EASE_IN_OUT"),
}


def blender_interp_for(interp: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Returns ``(interpolation, easing, warning)``. An unrecognized value
    (not spec'd — defensive only, in case the LLM answers outside the
    documented enum) falls back to ``linear`` with a warning."""
    key = (interp or "linear").strip().lower()
    if key in _INTERP_MAP:
        interpolation, easing = _INTERP_MAP[key]
        return interpolation, easing, None
    interpolation, easing = _INTERP_MAP["linear"]
    return interpolation, easing, f"unrecognized interp {interp!r}, treated as linear"


def is_proxy_object(obj: Dict[str, Any]) -> bool:
    if obj.get("proxy") is True:
        return True
    return blockout_assets.is_proxy_asset_id(obj.get("asset_id"))


def proxy_body_plan_for_asset_id(asset_id: Optional[str]) -> str:
    if asset_id == blockout_assets.PROXY_BIPED:
        return "biped"
    if asset_id == blockout_assets.PROXY_QUADRUPED:
        return "quadruped"
    if asset_id == blockout_assets.PROXY_BOX:
        return "none"
    raise ValueError(f"not a reserved proxy asset_id: {asset_id!r}")


def resolve_object_geometry(obj: Dict[str, Any], assets_root: Optional[Path] = None) -> Dict[str, Any]:
    """Decides how to build one ``scene_spec.json`` chain object (раздел 9.4
    "Поля заглушки" for the documented shape; see module docstring for the
    defensive fallback covering Э2's actual, poorer shape).

    Returns one of:
      ``{"mode": "proxy", "body_plan": ..., "height_m": ..., "warning": Optional[str]}``
      ``{"mode": "asset", "asset_path": Path, "scale": float, "warning": Optional[str]}``
    Never raises: an unresolvable real ``asset_id`` degrades to a box proxy
    with a warning, exactly like a genuinely proxy-less object.
    """
    instance_id = obj.get("instance_id", "?")
    asset_id = obj.get("asset_id")

    if is_proxy_object(obj):
        try:
            body_plan = proxy_body_plan_for_asset_id(asset_id)
        except ValueError:
            body_plan = "none"
        height_m = obj.get("height_m")
        warning = None
        if not (isinstance(height_m, (int, float)) and not isinstance(height_m, bool) and height_m > 0):
            height_m = blockout_assets.typical_height_m("character" if body_plan != "none" else "prop")
            warning = f"P01: object {instance_id!r} is a proxy with no usable height_m; substituted typical height"
        return {"mode": "proxy", "body_plan": body_plan, "height_m": float(height_m), "warning": warning}

    if asset_id:
        index = blockout_assets.read_index(assets_root)
        entry = blockout_assets.find_asset_by_id(index, asset_id)
        if entry is not None:
            try:
                asset_path = blockout_assets.resolve_asset_file_path(entry, assets_root)
                scale = obj.get("scale")
                scale = float(scale) if isinstance(scale, (int, float)) and not isinstance(scale, bool) else 1.0
                return {"mode": "asset", "asset_path": asset_path, "scale": scale, "warning": None}
            except FileNotFoundError:
                pass
        return {
            "mode": "proxy",
            "body_plan": "none",
            "height_m": blockout_assets.typical_height_m("prop"),
            "warning": f"P01: asset_id {asset_id!r} for object {instance_id!r} not found in library; substituted box proxy",
        }

    # Э2's actual output: neither proxy nor asset_id present (documented
    # schema gap, see module docstring).
    height_m = obj.get("height_m")
    if not (isinstance(height_m, (int, float)) and not isinstance(height_m, bool) and height_m > 0):
        height_m = blockout_assets.typical_height_m("character")
    return {
        "mode": "proxy",
        "body_plan": "none",
        "height_m": float(height_m),
        "warning": f"P01: object {instance_id!r} has no asset_id in scene_spec.json; rendered as generic box proxy",
    }


# =============================================================================
# bpy-touching functions (раздел 9.4, 12, 13.2)
# =============================================================================


def _require_bpy() -> None:
    if not BPY_AVAILABLE:
        raise RuntimeError("this function requires the Blender 'bpy' module")


def _build_sphere_part(part: "blockout_assets.ProxyPart") -> Any:
    _require_bpy()
    bpy.ops.mesh.primitive_uv_sphere_add(radius=part.size["diameter"] / 2.0, location=part.position)
    return bpy.context.active_object


def _build_box_part(part: "blockout_assets.ProxyPart") -> Any:
    _require_bpy()
    bpy.ops.mesh.primitive_cube_add(size=part.size["side"], location=part.position)
    return bpy.context.active_object


def _build_capsule_part(part: "blockout_assets.ProxyPart") -> Any:
    """A cylinder plus two end-caps spheres, joined into one object. Built
    from ``bpy.ops`` primitives rather than raw bmesh vertex surgery: this
    deviates slightly from a literal "собери через bmesh" reading of the
    spec, but standard operators are far more likely to produce correct,
    manifold geometry when actually run in Blender -- neither approach is
    executable in this sandbox, so this is a deliberate reliability
    tradeoff, flagged for Э12 manual acceptance."""
    _require_bpy()
    radius = part.size["radius"]
    length = part.size["length"]
    cyl_length = max(length - 2.0 * radius, 0.0)
    rotation = (math.radians(90.0), 0.0, 0.0) if part.axis == "+Y" else (0.0, 0.0, 0.0)
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=cyl_length, location=part.position, rotation=rotation)
    cylinder = bpy.context.active_object

    if part.axis == "+Y":
        offsets = [(0.0, cyl_length / 2.0, 0.0), (0.0, -cyl_length / 2.0, 0.0)]
    else:
        offsets = [(0.0, 0.0, cyl_length / 2.0), (0.0, 0.0, -cyl_length / 2.0)]

    caps = []
    for dx, dy, dz in offsets:
        loc = (part.position[0] + dx, part.position[1] + dy, part.position[2] + dz)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=loc)
        caps.append(bpy.context.active_object)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in caps:
        obj.select_set(True)
    cylinder.select_set(True)
    bpy.context.view_layer.objects.active = cylinder
    bpy.ops.object.join()
    return cylinder


PROXY_PART_BUILDERS: Dict[str, Callable[["blockout_assets.ProxyPart"], Any]] = {
    "sphere": _build_sphere_part,
    "box": _build_box_part,
    "capsule": _build_capsule_part,
}


def build_proxy_object(instance_id: str, body_plan: str, height_m: float) -> Any:
    _require_bpy()
    parts = blockout_assets.proxy_part_specs(body_plan, height_m)
    built = [PROXY_PART_BUILDERS[part.kind](part) for part in parts]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in built:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = built[0]
    if len(built) > 1:
        bpy.ops.object.join()
    root = bpy.context.view_layer.objects.active
    root.name = instance_id
    return root


def import_asset_object(asset_path: Path, instance_id: str, scale: float) -> Any:
    _require_bpy()
    bpy.ops.import_scene.gltf(filepath=str(asset_path))
    imported = list(bpy.context.selected_objects)
    if not imported:
        raise RuntimeError(f"glTF import produced no objects: {asset_path}")
    roots = [obj for obj in imported if obj.parent is None]
    if not roots:
        roots = [imported[0]]
    # Blender's glTF importer leaves a Y-up→Z-up correction on the root
    # (quaternion / euler). scene_spec rotation_deg then overwrites that
    # rest pose, so people/trees/towers fall onto their backs. Bake the
    # importer rotation into mesh data first; tracks only add scene yaw.
    bpy.ops.object.select_all(action="DESELECT")
    for obj in roots:
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    for obj in roots:
        obj.scale = tuple(s * scale for s in obj.scale)
        if getattr(obj, "rotation_mode", None) != "XYZ":
            obj.rotation_mode = "XYZ"
    root = roots[0]
    root.name = instance_id
    return root


def apply_world(world: Dict[str, Any]) -> None:
    _require_bpy()
    scene = bpy.context.scene
    ground = world.get("ground") or {}
    if ground.get("enabled", True):
        size_m = float(ground.get("size_m", 400.0))
        z = float(ground.get("z", 0.0))
        bpy.ops.mesh.primitive_plane_add(size=size_m, location=(0.0, 0.0, z))
        bpy.context.active_object.name = "blockout_ground"

    sun = world.get("sun") or {}
    elevation_deg = float(sun.get("elevation_deg", 45.0))
    azimuth_deg = float(sun.get("azimuth_deg", 225.0))
    scene.display.light_direction = sun_direction(elevation_deg, azimuth_deg)


def _set_last_keyframe_interp(obj: Any, data_path: str, frame: int, interp: Optional[str]) -> Optional[str]:
    interpolation, easing, warning = blender_interp_for(interp)
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return warning
    for fcurve in action.fcurves:
        if fcurve.data_path != data_path:
            continue
        for kp in fcurve.keyframe_points:
            if abs(kp.co[0] - frame) < 1e-6:
                kp.interpolation = interpolation
                kp.easing = easing
                if interpolation == "BEZIER":
                    kp.handle_left_type = "AUTO_CLAMPED"
                    kp.handle_right_type = "AUTO_CLAMPED"
    return warning


def apply_object_track(
    obj: Any, data_path: str, keys: List[Dict[str, Any]], fps: int, *, is_angle: bool = False
) -> List[str]:
    """Keyframes a vec3 property (``location`` / ``rotation_euler``) of
    ``obj`` from a ``scene_spec.json`` track (раздел 12)."""
    _require_bpy()
    warnings: List[str] = []
    # glTF import leaves objects in QUATERNION mode. Writing rotation_euler
    # then does not change the visible pose, so a scene_spec yaw is silently
    # dropped (train stays native +Y while the author offset cars along X).
    if is_angle and data_path == "rotation_euler" and getattr(obj, "rotation_mode", None) != "XYZ":
        obj.rotation_mode = "XYZ"
    for key in keys:
        frame = blockout_common.frame_blender(float(key["t"]), fps)
        value = key["v"]
        if is_angle:
            value = tuple(math.radians(float(v)) for v in value)
        else:
            value = tuple(float(v) for v in value)
        setattr(obj, data_path, value)
        obj.keyframe_insert(data_path=data_path, frame=frame)
        warning = _set_last_keyframe_interp(obj, data_path, frame, key.get("interp"))
        if warning:
            warnings.append(warning)
    return warnings


def apply_scalar_track(data_block: Any, data_path: str, keys: List[Dict[str, Any]], fps: int) -> List[str]:
    _require_bpy()
    warnings: List[str] = []
    for key in keys:
        frame = blockout_common.frame_blender(float(key["t"]), fps)
        setattr(data_block, data_path, float(key["v"]))
        data_block.keyframe_insert(data_path=data_path, frame=frame)
        warning = _set_last_keyframe_interp(data_block, data_path, frame, key.get("interp"))
        if warning:
            warnings.append(warning)
    return warnings


def apply_visibility_track(obj: Any, keys: List[Dict[str, Any]], fps: int) -> None:
    """Optional ``visible`` track (раздел 12): step function, always
    ``interp: "constant"``. Keyframes ``hide_render``/``hide_viewport``."""
    _require_bpy()
    for key in keys:
        frame = blockout_common.frame_blender(float(key["t"]), fps)
        hidden = not bool(key["v"])
        obj.hide_render = hidden
        obj.keyframe_insert(data_path="hide_render", frame=frame)
        obj.hide_viewport = hidden
        obj.keyframe_insert(data_path="hide_viewport", frame=frame)


def apply_animation(obj: Any, animation_spec: Dict[str, Any]) -> Optional[str]:
    """Best-effort NLA playback of a clip on ``obj``'s armature child
    (раздел 15.2 phase formula owns the *meaning*; this only has to make
    Blender play the clip back roughly in sync). Not verifiable without
    Blender -- flagged for Э12 manual QA (compare a rendered walk-cycle
    phase against ``blockout_common.animation_phase()``)."""
    _require_bpy()
    clip = animation_spec.get("clip")
    if not clip:
        return None
    armature = next((child for child in obj.children if child.type == "ARMATURE"), None)
    if armature is None:
        return f"animation clip {clip!r} requested but object has no armature; ignored"
    action = bpy.data.actions.get(clip)
    if action is None:
        return f"animation clip {clip!r} not found among imported actions; object kept in rest pose"

    if armature.animation_data is None:
        armature.animation_data_create()
    track = armature.animation_data.nla_tracks.new()
    strip = track.strips.new(clip, 1, action)
    speed = float(animation_spec.get("speed", 1.0)) or 1.0
    strip.scale = 1.0 / speed
    strip.repeat = 1.0 if not animation_spec.get("loop", True) else 100.0
    return None


def build_camera(camera_spec: Dict[str, Any], fps: int) -> Any:
    """раздел 12, "Как look_at попадает в Blender": ``Track To`` on an
    empty (``blockout_camera_target``), whose location track is
    ``look_at``. ``camera.rotation_euler`` stays zero."""
    _require_bpy()
    cam_data = bpy.data.cameras.new("blockout_camera")
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.sensor_width = float(camera_spec.get("sensor_mm", 36.0))
    cam_obj = bpy.data.objects.new(CAMERA_OBJECT_NAME, cam_data)
    bpy.context.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    target = bpy.data.objects.new(CAMERA_TARGET_NAME, None)
    bpy.context.collection.objects.link(target)

    constraint = cam_obj.constraints.new(type="TRACK_TO")
    constraint.target = target
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"

    tracks = camera_spec.get("tracks") or {}
    warnings: List[str] = []
    warnings += apply_object_track(cam_obj, "location", tracks.get("location") or [], fps)
    warnings += apply_object_track(target, "location", tracks.get("look_at") or [], fps)
    warnings += apply_scalar_track(cam_data, "lens", tracks.get("lens_mm") or [], fps)
    return cam_obj, warnings


def build_chain_scene(payload: Dict[str, Any], assets_root: Optional[Path] = None) -> Dict[str, Any]:
    """Assembles the whole chain scene: world, every object (proxy or
    imported asset) with its tracks/animation, and the camera. Optionally
    saves ``chain.blend`` when ``payload["blend_path"]`` is set."""
    _require_bpy()
    bpy.ops.wm.read_factory_settings(use_empty=True)

    warnings: List[str] = []
    apply_world(payload.get("world") or {})

    fps = int(payload["fps"])
    for obj_spec in payload.get("objects") or []:
        geometry = resolve_object_geometry(obj_spec, assets_root)
        if geometry.get("warning"):
            warnings.append(geometry["warning"])
        if geometry["mode"] == "proxy":
            obj = build_proxy_object(obj_spec["instance_id"], geometry["body_plan"], geometry["height_m"])
        else:
            obj = import_asset_object(geometry["asset_path"], obj_spec["instance_id"], geometry["scale"])

        tracks = obj_spec.get("tracks") or {}
        warnings += apply_object_track(obj, "location", tracks.get("location") or [], fps)
        warnings += apply_object_track(obj, "rotation_euler", tracks.get("rotation_deg") or [], fps, is_angle=True)
        if "visible" in tracks:
            apply_visibility_track(obj, tracks["visible"], fps)

        animation_spec = obj_spec.get("animation")
        if animation_spec:
            warning = apply_animation(obj, animation_spec)
            if warning:
                warnings.append(warning)

    _, camera_warnings = build_camera(payload.get("camera") or {}, fps)
    warnings += camera_warnings

    blend_path = payload.get("blend_path")
    if blend_path:
        Path(blend_path).parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

    return {"ok": True, "warnings": warnings, "object_count": len(payload.get("objects") or [])}
