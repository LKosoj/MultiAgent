"""Э3: ``render_shot.py`` -- the actual Blender entry point for one chain
(docs/tz-blockout-reference-pipeline.md, section 13.2-13.4, 14).

Invocation contract (section 13.4, same shape as ``normalize_asset.py``):
``blender -b -P render_shot.py -- <input.json>``. Blender launches **once
per chain**: this script first calls ``build_scene.build_chain_scene()``
to assemble the whole chain (world/objects/camera, optionally saving
``chain.blend``), then renders every shot of the chain as a window of the
chain's single continuous frame timeline (section 13.4, "Нумерация файлов
- локальная").

Input JSON: ``{"chain": <build_scene payload>, "shots": [{"shot_key",
"chain_frame_start", "chain_frame_end", "duration_s", "shot_dir"}, ...],
"resolution": [w, h], "fps": int}``. Output JSON: ``{"ok": true,
"chain_warnings": [...], "shots": {shot_key: {"frames_written": int,
"resolution": [w, h], "frame_0001_sha256": str, "frame_last_sha256": str,
"ref_start_sha256": str, "ref_end_sha256": str}}}`` or ``{"ok": false,
"error": str}``.

The module MUST import without bpy: ``local_frame_filename()``,
``ref_copy_plan()`` and ``chain_scene_payload()`` are pure functions and
the tested surface of this file's frame-naming/payload logic. ``run()``/
``main()`` are the only bpy-touching entry points; outside Blender
``run()`` raises ``RuntimeError``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import bpy  # type: ignore
    import bpy_extras.object_utils  # type: ignore
    import mathutils  # type: ignore

    BPY_AVAILABLE = True
except ImportError:
    bpy = None
    bpy_extras = None
    mathutils = None
    BPY_AVAILABLE = False


def _ensure_repo_root_on_syspath() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "custom_tools").is_dir() and (p / "agent_command.py").exists():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            return p
    return Path.cwd()


_REPO_ROOT = _ensure_repo_root_on_syspath()

from custom_tools.storybook.blockout_blender import build_scene  # noqa: E402
from custom_tools.storybook.blockout_blender import export_state  # noqa: E402


# =============================================================================
# Pure functions (раздел 13.4 "Нумерация файлов - локальная", раздел 14)
# =============================================================================


def local_frame_filename(i: int) -> str:
    return f"frame_{i:04d}.png"


def frame_filenames_for_window(n_render: int) -> List[str]:
    return [local_frame_filename(i) for i in range(1, n_render + 1)]


def ref_copy_plan(n_render: int) -> Dict[str, str]:
    """раздел 14: ``ref_start.png`` <- ``frame_0001.png``, ``ref_end.png``
    <- ``frame_{N_render}.png`` (bit-identical copies, not separate renders)."""
    return {"ref_start.png": local_frame_filename(1), "ref_end.png": local_frame_filename(n_render)}


def chain_scene_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """раздел 8/13.4: ``build_scene.build_chain_scene()`` only saves
    ``chain.blend`` when the payload it receives carries ``blend_path``
    (its own docstring). This module's caller (``blockout_renderer.py``)
    places that key at the TOP level of the render JSON, sibling of
    ``"chain"`` -- the same place ``_write_shot_state()`` reads it from --
    so it must be copied into the nested chain payload before calling
    ``build_chain_scene()``, or ``chain.blend`` is silently never written."""
    chain_payload = payload["chain"]
    if "blend_path" in payload:
        chain_payload = dict(chain_payload, blend_path=payload["blend_path"])
    return chain_payload


# =============================================================================
# bpy-touching functions (раздел 13.2, 13.4)
# =============================================================================

# P0.2: fallback material tint per top-level assets/blockout/ category, used
# when a chain object has no per-asset "render" override.
CATEGORY_COLORS: Dict[str, Tuple[float, float, float]] = {
    "character": (0.85, 0.72, 0.60),
    "building": (0.72, 0.68, 0.60),
    "prop": (0.55, 0.38, 0.24),
    "nature": (0.28, 0.44, 0.22),
    "vehicle": (0.35, 0.35, 0.40),
}

_CAMERA_MARGIN_M = 0.05


def _configure_workbench(scene: Any, resolution: Sequence[int], fps: int, light_direction: Sequence[float]) -> None:
    """Verbatim раздел 13.2 settings block."""
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = int(resolution[0]), int(resolution[1])
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.render.film_transparent = False

    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.studio_light = "Default"
    sh.color_type = "SINGLE"
    sh.single_color = (0.55, 0.55, 0.55)
    sh.show_shadows = True
    sh.shadow_intensity = 0.5
    sh.show_cavity = True
    sh.cavity_type = "BOTH"
    sh.show_object_outline = False
    sh.show_specular_highlight = False

    scene.display.render_aa = "8"
    scene.display.light_direction = tuple(light_direction)

    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"


def _configure_eevee(scene: Any, resolution: Sequence[int], fps: int, samples: int = 8) -> None:
    """P0.1: EEVEE default renderer -- shadows, real materials, unlike
    ``_configure_workbench``'s flat studio-light preview."""
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x, scene.render.resolution_y = int(resolution[0]), int(resolution[1])
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.render.film_transparent = False

    scene.eevee.taa_render_samples = samples

    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"


def _setup_eevee_lighting(payload: Dict[str, Any]) -> None:
    """P0.3: workbench's ``light_direction`` (``build_scene.apply_world()``)
    only drives viewport/solid shading; EEVEE's Principled BSDF needs actual
    light objects plus a background so materials aren't rendered black."""
    sun_spec = ((payload.get("chain") or {}).get("world") or {}).get("sun") or {}
    elevation_deg = float(sun_spec.get("elevation_deg", 30.0))
    azimuth_deg = float(sun_spec.get("azimuth_deg", 100.0))

    sun_data = bpy.data.lights.new("blockout_sun", type="SUN")
    sun_data.energy = 3.0
    sun_obj = bpy.data.objects.new("blockout_sun", sun_data)
    bpy.context.collection.objects.link(sun_obj)
    sun_obj.rotation_euler = (math.radians(90.0 - elevation_deg), 0.0, math.radians(azimuth_deg))

    fill_data = bpy.data.lights.new("blockout_fill", type="AREA")
    fill_data.energy = 200.0
    fill_data.size = 5.0
    fill_obj = bpy.data.objects.new("blockout_fill", fill_data)
    bpy.context.collection.objects.link(fill_obj)
    fill_obj.location = (0.0, -6.0, 4.0)
    fill_obj.rotation_euler = (math.radians(55.0), 0.0, 0.0)

    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("blockout_world")
        bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.45, 0.55, 0.65, 1.0)
        bg.inputs["Strength"].default_value = 0.35


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _maybe_quiet(quiet: bool):
    """P2.13: only silences ``bpy.ops.render.render()``'s own progress
    output -- callers' status/error prints happen outside this context."""
    if not quiet:
        yield
        return
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        yield


def _render_shot_window(
    scene: Any, chain_frame_start: int, n_render: int, frames_dir: Path, quiet: bool = False
) -> List[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    with _maybe_quiet(quiet):
        for i in range(1, n_render + 1):
            scene.frame_set(chain_frame_start + i - 1)
            out_path = frames_dir / local_frame_filename(i)
            scene.render.filepath = str(out_path)
            bpy.ops.render.render(write_still=True)
            written.append(out_path)
    return written


# =============================================================================
# P0.2 -- category materials (EEVEE only, workbench ignores material data)
# =============================================================================


def _scan_asset_categories(assets_root: Optional[str] = None) -> Dict[str, str]:
    """``{asset_id: category}`` from the first-level directory layout under
    ``assets/blockout/`` (character/building/prop/nature/vehicle/...)."""
    if assets_root:
        root = Path(assets_root)
    else:
        from custom_tools.storybook import blockout_assets  # local: avoid import cycles at module load

        root = blockout_assets.blockout_assets_root()
    mapping: Dict[str, str] = {}
    if not root.is_dir():
        return mapping
    for category_dir in root.iterdir():
        if not category_dir.is_dir():
            continue
        for asset_dir in category_dir.iterdir():
            if asset_dir.is_dir():
                mapping[asset_dir.name] = category_dir.name
    return mapping


def _find_ancestor_instance_id(obj: Any, known_ids: Dict[str, Any]) -> Optional[str]:
    """Imported multi-mesh assets only rename the glTF root to
    ``instance_id`` (``build_scene.import_asset_object()``); mesh children
    keep their importer-given names, so materials must resolve via the
    parent chain."""
    node = obj
    while node is not None:
        if node.name in known_ids:
            return node.name
        node = node.parent
    return None


def _ensure_category_material(cat: str, tint: Optional[Sequence[float]] = None, roughness: Optional[float] = None) -> Any:
    color = tuple(float(c) for c in tint) if tint else CATEGORY_COLORS.get(cat, CATEGORY_COLORS["prop"])
    rough = float(roughness) if roughness is not None else 0.7
    name = f"blockout_mat_{cat}_{color[0]:.3f}_{color[1]:.3f}_{color[2]:.3f}_{rough:.3f}"
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
        bsdf.inputs["Roughness"].default_value = rough
    return mat


def _category_for_instance(id_to_category: Dict[str, str], instance_id: Optional[str]) -> Optional[str]:
    """``None`` means ``instance_id`` itself is unresolved (e.g.
    ``build_scene``'s ``blockout_ground``, which has no chain object entry at
    all) -- callers must leave those objects' materials untouched rather than
    defaulting to "prop". A resolved instance_id with no category match still
    defaults to "prop"."""
    if instance_id is None:
        return None
    return id_to_category.get(instance_id, "prop")


def _assign_category_materials(chain_payload: Dict[str, Any], assets_root: Optional[str], engine: str) -> None:
    """``_configure_workbench``'s ``sh.color_type = "SINGLE"`` ignores
    material data entirely, so this is a no-op outside EEVEE."""
    if engine != "eevee":
        return
    category_by_asset = _scan_asset_categories(assets_root)
    id_to_category: Dict[str, str] = {}
    id_to_render: Dict[str, Dict[str, Any]] = {}
    for obj_spec in chain_payload.get("objects") or []:
        instance_id = obj_spec.get("instance_id")
        if not instance_id:
            continue
        id_to_category[instance_id] = category_by_asset.get(obj_spec.get("asset_id"), "prop")
        render_override = obj_spec.get("render")
        if render_override:
            id_to_render[instance_id] = render_override

    for obj in list(bpy.data.objects):
        if obj.type != "MESH":
            continue
        instance_id = _find_ancestor_instance_id(obj, id_to_category)
        cat = _category_for_instance(id_to_category, instance_id)
        if cat is None:
            continue
        override = id_to_render.get(instance_id) or {}
        mat = _ensure_category_material(cat, tint=override.get("tint"), roughness=override.get("roughness"))
        obj.data.materials.clear()
        obj.data.materials.append(mat)


# =============================================================================
# P1.8 -- visibility keyframes (hide_render/hide_viewport, constant step)
# =============================================================================


def _force_constant_interpolation(obj: Any, data_path: str) -> None:
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return
    for fcurve in action.fcurves:
        if fcurve.data_path != data_path:
            continue
        for kf in fcurve.keyframe_points:
            kf.interpolation = "CONSTANT"


def _apply_visibility_keyframes(chain_payload: Dict[str, Any], fps: int) -> None:
    """``build_scene.apply_visibility_track()`` already keyframes
    hide_render/hide_viewport from a ``tracks["visible"]`` track (раздел 12)
    but leaves Blender's default keyframe interpolation, turning the
    intended on/off step into a fade. Re-applied here with explicit
    CONSTANT interpolation; must run after ``build_chain_scene()`` so the
    objects it creates already exist."""
    for obj_spec in chain_payload.get("objects") or []:
        visible_keys = (obj_spec.get("tracks") or {}).get("visible")
        if not visible_keys:
            continue
        obj = bpy.data.objects.get(obj_spec.get("instance_id"))
        if obj is None:
            continue
        for key in visible_keys:
            frame = int(round(float(key["t"]) * fps)) + 1
            hidden = not bool(key["v"])
            obj.hide_render = hidden
            obj.keyframe_insert(data_path="hide_render", frame=frame)
            obj.hide_viewport = hidden
            obj.keyframe_insert(data_path="hide_viewport", frame=frame)
        _force_constant_interpolation(obj, "hide_render")
        _force_constant_interpolation(obj, "hide_viewport")


# =============================================================================
# P1.4/P1.5 -- camera-inside-object and subject-frustum hard-error guards
# =============================================================================


def _world_aabb(obj: Any) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _aabb_contains(
    point: Tuple[float, float, float],
    aabb_min: Tuple[float, float, float],
    aabb_max: Tuple[float, float, float],
    margin: float,
) -> bool:
    """Pure margin-shrunk AABB containment check, factored out of
    ``_camera_inside_object()`` so it's testable without bpy/mathutils."""
    px, py, pz = point
    min_x, min_y, min_z = aabb_min
    max_x, max_y, max_z = aabb_max
    return (
        min_x + margin < px < max_x - margin
        and min_y + margin < py < max_y - margin
        and min_z + margin < pz < max_z - margin
    )


def _camera_inside_object(cam_loc: Any, obj: Any, margin: float = _CAMERA_MARGIN_M) -> bool:
    aabb_min, aabb_max = _world_aabb(obj)
    return _aabb_contains((cam_loc.x, cam_loc.y, cam_loc.z), aabb_min, aabb_max, margin)


def _sampling_frames(start: int, end: int) -> List[int]:
    """Start/mid/end frames of a window, deduped (start == end collapses to
    one frame; a set is used so the pair is order-agnostic)."""
    mid = (start + end) // 2
    return sorted({start, mid, end})


def _assert_camera_not_inside_any_object(
    scene: Any, camera_obj: Any, shot_key: str, chain_frame_start: int, chain_frame_end: int
) -> None:
    """P1.4: a camera origin inside a mesh's (margin-shrunk) AABB is never a
    directable shot -- hard-fails the whole run rather than rendering it.
    Sampled at start/mid/end of the shot window, same set as
    ``_assert_subjects_visible()``, so clipping that only occurs at the
    window's edges (not its midpoint) is still caught."""
    for frame in _sampling_frames(chain_frame_start, chain_frame_end):
        scene.frame_set(frame)
        cam_loc = camera_obj.matrix_world.translation
        for obj in bpy.data.objects:
            if obj.type != "MESH":
                continue
            if _camera_inside_object(cam_loc, obj):
                raise RuntimeError(
                    f"CAM_INSIDE: shot={shot_key} camera={tuple(cam_loc)} inside object={obj.name} at frame={frame}"
                )


def _subject_world_center(obj: Any) -> Any:
    (min_x, min_y, min_z), (max_x, max_y, max_z) = _world_aabb(obj)
    return mathutils.Vector(((min_x + max_x) / 2.0, (min_y + max_y) / 2.0, (min_z + max_z) / 2.0))


def _assert_subjects_visible(
    scene: Any, camera_obj: Any, shot: Dict[str, Any], chain_frame_start: int, chain_frame_end: int
) -> None:
    """P1.5: samples start/mid/end frames of the shot window. A subject
    hidden (P1.8 ``hide_render``) at every sampled frame is an authoring
    choice, not a framing bug, so those frames are skipped rather than
    counted against it."""
    subject_focus = shot.get("subject_focus") or []
    if not subject_focus:
        return
    frames = _sampling_frames(chain_frame_start, chain_frame_end)
    for subject_id in subject_focus:
        obj = bpy.data.objects.get(subject_id)
        if obj is None:
            continue
        seen = False
        checked_any = False
        for frame in frames:
            scene.frame_set(frame)
            if obj.hide_render:
                continue
            checked_any = True
            co = bpy_extras.object_utils.world_to_camera_view(scene, camera_obj, _subject_world_center(obj))
            if 0.0 <= co.x <= 1.0 and 0.0 <= co.y <= 1.0 and co.z > 0.0:
                seen = True
                break
        if checked_any and not seen:
            raise RuntimeError(f"SUBJ_HIDDEN: shot={shot['shot_key']} subject={subject_id} never in frustum")


def _clean_shot_dir(shot_dir: Path) -> None:
    """раздел 8: "Перед рендером директория шота очищается" -- old
    frames/, blockout_ref.mp4, ref_start.png, ref_end.png, manifest.json."""
    frames_dir = shot_dir / "frames"
    if frames_dir.is_dir():
        shutil.rmtree(frames_dir)
    for name in ("blockout_ref.mp4", "ref_start.png", "ref_end.png", "manifest.json"):
        stale = shot_dir / name
        if stale.is_file():
            stale.unlink()


def _write_shot_state(
    payload: Dict[str, Any], chain_payload: Dict[str, Any], shot: Dict[str, Any], assets_root: Optional[str]
) -> None:
    """раздел 15.2: writes ``93_blockout/{chain_id}/state/shot_{NN}_out.json``
    at the end of this shot's render window (the scene is left on the
    shot's last frame by ``_render_shot_window()``). Diagnostic-only
    (раздел 15.2, "файл пишется только на запись") -- Э4's export_state.py
    owns the actual field extraction/formula (Р3)."""
    chain_id = payload["chain_id"]
    chain_dir = Path(payload["blend_path"]).parent
    shot_number = int(shot["shot_number"])
    t_in_chain = float(shot["t_start"]) + float(shot["duration_s"])
    camera_obj = bpy.data.objects.get(build_scene.CAMERA_OBJECT_NAME)
    state = export_state.export_shot_state(
        chain_id=chain_id,
        shot_number=shot_number,
        t_in_chain=t_in_chain,
        blender_version=".".join(str(c) for c in bpy.app.version),
        camera_obj=camera_obj,
        object_specs=chain_payload.get("objects") or [],
        resolve_object=bpy.data.objects.get,
        assets_root=Path(assets_root) if assets_root else None,
    )
    export_state.write_state_json(export_state.state_output_path(chain_dir, shot_number), state)


def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The only bpy-touching entry point besides the lower-level helpers
    above. Raises ``RuntimeError`` outside Blender."""
    if not BPY_AVAILABLE:
        raise RuntimeError("render_shot.run() requires the Blender 'bpy' module")

    chain_payload = chain_scene_payload(payload)
    resolution = payload["resolution"]
    fps = int(payload["fps"])
    engine = str(payload.get("engine") or "eevee").lower()
    quiet = bool(payload.get("quiet", False))

    assets_root = payload.get("assets_root")
    scene_result = build_scene.build_chain_scene(
        chain_payload, Path(assets_root) if assets_root else None
    )
    chain_warnings = list(scene_result.get("warnings") or [])

    scene = bpy.context.scene
    if engine == "workbench":
        light_direction = scene.display.light_direction
        _configure_workbench(scene, resolution, fps, light_direction)
    else:
        _configure_eevee(scene, resolution, fps)
        _setup_eevee_lighting(payload)

    _apply_visibility_keyframes(chain_payload, fps)
    _assign_category_materials(chain_payload, assets_root, engine)

    camera_obj = bpy.data.objects.get(build_scene.CAMERA_OBJECT_NAME)

    shots_out: Dict[str, Any] = {}
    for shot in payload.get("shots") or []:
        shot_key = shot["shot_key"]
        shot_dir = Path(shot["shot_dir"])
        _clean_shot_dir(shot_dir)
        frames_dir = shot_dir / "frames"

        chain_frame_start = int(shot["chain_frame_start"])
        n_render = int(shot["n_render"])
        chain_frame_end = int(shot.get("chain_frame_end", chain_frame_start + n_render - 1))

        if camera_obj is not None:
            _assert_camera_not_inside_any_object(scene, camera_obj, shot_key, chain_frame_start, chain_frame_end)
            _assert_subjects_visible(scene, camera_obj, shot, chain_frame_start, chain_frame_end)

        written = _render_shot_window(scene, chain_frame_start, n_render, frames_dir, quiet=quiet)

        plan = ref_copy_plan(n_render)
        ref_hashes: Dict[str, str] = {}
        for ref_name, source_name in plan.items():
            src = frames_dir / source_name
            dst = shot_dir / ref_name
            shutil.copyfile(src, dst)
            ref_hashes[ref_name] = _sha256_file(dst)

        shots_out[shot_key] = {
            "frames_written": len(written),
            "frame_files": [p.name for p in written],
            "resolution": [int(resolution[0]), int(resolution[1])],
            "frame_0001_sha256": _sha256_file(written[0]) if written else None,
            "frame_last_sha256": _sha256_file(written[-1]) if written else None,
            "ref_start_sha256": ref_hashes.get("ref_start.png"),
            "ref_end_sha256": ref_hashes.get("ref_end.png"),
        }

        _write_shot_state(payload, chain_payload, shot, assets_root)

    return {"ok": True, "chain_warnings": chain_warnings, "shots": shots_out}


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
        print("render_shot: missing '--' separator before the input JSON path", file=sys.stderr)
        return 1
    after = raw_argv[raw_argv.index("--") + 1 :]
    if not after:
        print("render_shot: missing input JSON path after '--'", file=sys.stderr)
        return 1
    input_json_path = after[0]

    try:
        payload = json.loads(Path(input_json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"render_shot: failed to read input JSON: {exc}", file=sys.stderr)
        return 1

    try:
        output_path = Path(payload["output_path"])
    except KeyError as exc:
        print(f"render_shot: input JSON is missing {exc}", file=sys.stderr)
        return 1

    try:
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 - always report failure as JSON, never crash Blender silently
        result = {"ok": False, "error": str(exc)}

    _write_json_atomic(output_path, result)

    if not result.get("ok"):
        print(f"render_shot: FAILED: {result.get('error')}")
        return 1
    print(f"render_shot: OK shots={list(result.get('shots', {}).keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
