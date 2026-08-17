"""Shared library of 3D blockout objects (assets/blockout/).

Schema and rules: docs/tz-blockout-reference-pipeline.md, section 9.

Path resolution mirrors ``custom_tools/storybook/project_paths.py``: an env
variable wins, otherwise the path is CWD-relative (not repo-root-relative).
"""

import json
import logging
import math
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CATEGORIES = ("character", "prop", "vehicle", "building", "nature")

# Typical height per category (meters), section 9.1: used whenever an actual
# height cannot be determined (LLM call failed / returned nothing / a
# fetched object's height is out of range).
TYPICAL_HEIGHT_M: Dict[str, float] = {
    "character": 1.70,
    "prop": 0.50,
    "vehicle": 1.60,
    "building": 6.00,
    "nature": 4.00,
}

# Fallback tint per category (0..1 RGB), used by render_shot when a meta.json
# has no render.tint of its own.
CATEGORY_DEFAULT_TINT: Dict[str, Tuple[float, float, float]] = {
    "character": (0.85, 0.72, 0.60),
    "building": (0.72, 0.68, 0.60),
    "prop": (0.55, 0.38, 0.24),
    "nature": (0.28, 0.44, 0.22),
    "vehicle": (0.35, 0.35, 0.40),
}

PROXY_BIPED = "__proxy_biped__"
PROXY_QUADRUPED = "__proxy_quadruped__"
PROXY_BOX = "__proxy_box__"
RESERVED_PROXY_IDS = (PROXY_BIPED, PROXY_QUADRUPED, PROXY_BOX)

DEFAULT_ASSET_FILENAME = "model.glb"
INDEX_FILENAME = "index.json"
META_FILENAME = "meta.json"


def blockout_assets_root() -> Path:
    """Return the blockout asset library root used by pipeline tools."""
    env_root = _env_assets_root()
    if env_root is not None:
        return env_root
    return _cwd_assets_root()


def _env_assets_root() -> Optional[Path]:
    configured = os.getenv("BLOCKOUT_ASSETS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return None


def _cwd_assets_root() -> Path:
    return (Path("assets") / "blockout").resolve()


def is_proxy_asset_id(asset_id: Any) -> bool:
    return asset_id in RESERVED_PROXY_IDS


def typical_height_m(category: str) -> float:
    try:
        return TYPICAL_HEIGHT_M[category]
    except KeyError as exc:
        raise ValueError(f"unknown category: {category!r}") from exc


def category_default_tint(cat: str) -> Tuple[float, float, float]:
    try:
        return CATEGORY_DEFAULT_TINT[cat]
    except KeyError as exc:
        raise ValueError(f"unknown category: {cat!r}") from exc


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _validate_render_section(meta: Dict[str, Any], rel_path: str, warnings: List[str]) -> None:
    """Validate meta["render"] in place, if present (tint/roughness in
    [0, 1]); out-of-range components are clamped and warned about, not
    rejected, since a bad render hint should not drop an otherwise-valid
    asset from the index. Each field is validated independently, so a
    malformed field (wrong type/length, non-numeric component) only drops
    that field -- not a validly-clamped sibling field -- and the warning
    names which field was dropped. The whole render section is removed only
    once no valid field remains in it."""
    render = meta.get("render")
    if not isinstance(render, dict):
        return
    tint = render.get("tint")
    if tint is not None:
        try:
            if not isinstance(tint, (list, tuple)) or len(tint) != 3:
                raise ValueError(f"render.tint must be a 3-element list, got {tint!r}")
            components = [float(c) for c in tint]
            # WHY: NaN < 0.0 and NaN > 1.0 are both False, so the range check
            # above silently clamps NaN to 0.0 via _clamp01 with no warning --
            # caught here explicitly and routed through the same malformed path.
            if any(math.isnan(c) for c in components):
                raise ValueError(f"render.tint contains NaN: {tint!r}")
            if any(c < 0.0 or c > 1.0 for c in components):
                warnings.append(f"render.tint out of [0,1] in {rel_path}, clamped")
            render["tint"] = [_clamp01(c) for c in components]
        except (ValueError, TypeError, KeyError) as exc:
            warnings.append(f"asset {rel_path}: render.tint malformed ({type(exc).__name__}: {exc}), ignoring")
            render.pop("tint", None)
    roughness = render.get("roughness")
    if roughness is not None:
        try:
            value = float(roughness)
            if math.isnan(value):
                raise ValueError(f"render.roughness is NaN: {roughness!r}")
            if value < 0.0 or value > 1.0:
                warnings.append(f"render.roughness out of [0,1] in {rel_path}, clamped")
            render["roughness"] = _clamp01(value)
        except (ValueError, TypeError, KeyError) as exc:
            warnings.append(f"asset {rel_path}: render.roughness malformed ({type(exc).__name__}: {exc}), ignoring")
            render.pop("roughness", None)
    if not render:
        meta.pop("render", None)


def _require_safe_asset_id(value: Any, field_name: str = "asset_id") -> str:
    """Local safety check, modeled after
    ``project_paths.require_safe_storybook_project_id`` (no cross-import:
    the two id spaces — storybook projects and library assets — are
    unrelated)."""
    if value is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} is required")
    if stripped in {".", ".."}:
        raise ValueError(f"{field_name} must be a safe path segment")
    if (
        "/" in stripped
        or "\\" in stripped
        or PurePosixPath(stripped).is_absolute()
        or PureWindowsPath(stripped).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(stripped).parts)
        or any(part in {"", ".", ".."} for part in PureWindowsPath(stripped).parts)
    ):
        raise ValueError(f"{field_name} must be a safe path segment")
    return stripped


@dataclass
class RebuildResult:
    objects: List[Dict[str, Any]]
    warnings: List[str]
    generated_at: str


def rebuild_index(assets_root: Optional[Path] = None) -> RebuildResult:
    """Rescan ``<root>/*/*/meta.json`` and rewrite ``index.json``.

    Does NOT log or write to any report itself — warnings are returned so the
    caller picks the channel (CLI stdout+logger, or a pipeline report.json;
    section 9.2). Not a read-modify-write: this is a full rescan, so no flock
    is taken, only an atomic tmp+os.replace of index.json.
    """
    root = assets_root or blockout_assets_root()
    warnings: List[str] = []
    objects: List[Dict[str, Any]] = []
    seen_ids: Dict[str, str] = {}

    if root.is_dir():
        for category_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for asset_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
                rel_path = str(PurePosixPath(category_dir.name, asset_dir.name))
                meta_path = asset_dir / META_FILENAME
                if not meta_path.is_file():
                    warnings.append(f"no {META_FILENAME} in {rel_path}, skipped")
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    warnings.append(f"invalid {META_FILENAME} in {rel_path}: {exc}, skipped")
                    continue
                asset_id = meta.get("id")
                if asset_id in seen_ids:
                    warnings.append(
                        f"duplicate id {asset_id!r} in {rel_path} (already used by "
                        f"{seen_ids[asset_id]}), skipped"
                    )
                    continue
                seen_ids[asset_id] = rel_path
                _validate_render_section(meta, rel_path, warnings)
                entry = dict(meta)
                entry["path"] = rel_path
                objects.append(entry)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_payload = {"version": 1, "generated_at": generated_at, "objects": objects}

    root.mkdir(parents=True, exist_ok=True)
    index_path = root / INDEX_FILENAME
    tmp_path = f"{index_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(index_payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, index_path)

    return RebuildResult(objects=objects, warnings=warnings, generated_at=generated_at)


def read_index(assets_root: Optional[Path] = None) -> Dict[str, Any]:
    """Read index.json. A missing (or unreadable) file yields an empty index."""
    root = assets_root or blockout_assets_root()
    index_path = root / INDEX_FILENAME
    if not index_path.is_file():
        return {"version": 1, "generated_at": None, "objects": []}
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "generated_at": None, "objects": []}


def normalize_match_name(text: Any) -> str:
    """Lowercase, collapse whitespace, fold ё -> е (section 9.4)."""
    if text is None:
        return ""
    collapsed = " ".join(str(text).split()).lower()
    return collapsed.replace("ё", "е")


def find_asset_by_id(index: Dict[str, Any], asset_id: str) -> Optional[Dict[str, Any]]:
    for entry in index.get("objects") or []:
        if entry.get("id") == asset_id:
            return entry
    return None


def find_asset_by_name(index: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    target = normalize_match_name(name)
    if not target:
        return None
    for entry in index.get("objects") or []:
        if normalize_match_name(entry.get("name")) == target:
            return entry
    return None


def find_asset_by_source_url(index: Dict[str, Any], source_url: Optional[str]) -> Optional[Dict[str, Any]]:
    if not source_url:
        return None
    for entry in index.get("objects") or []:
        if entry.get("source_url") == source_url:
            return entry
    return None


def resolve_asset_file_path(entry: Dict[str, Any], assets_root: Optional[Path] = None) -> Path:
    """Resolve ``{root}/{path}/{file}`` (section 9.3). Never guessed from
    category+id: ``path`` comes from the index entry, since a hand-copied
    directory's name may not match its meta.json ``id``."""
    root = assets_root or blockout_assets_root()
    path = entry.get("path")
    file_name = entry.get("file")
    if not path or not file_name:
        raise FileNotFoundError(f"asset entry is missing path/file: {entry!r}")
    candidate = root / path / file_name
    if not candidate.is_file():
        raise FileNotFoundError(f"asset file not found: {candidate}")
    return candidate


def compute_scale(entry: Dict[str, Any], height_m: float) -> float:
    """``scale = height_m / dimensions_m[2]`` (section 9.4).

    Raises ValueError for a proxy placeholder (no meta.json => no
    dimensions_m => scale is always 1.0 for those) or when the object's
    height dimension is not positive.
    """
    if is_proxy_asset_id((entry or {}).get("id")):
        raise ValueError("compute_scale is not applicable to proxy placeholders (scale is always 1.0)")
    dimensions = (entry or {}).get("dimensions_m")
    if not dimensions or len(dimensions) < 3:
        raise ValueError("entry is missing dimensions_m")
    height_dim = dimensions[2]
    if not height_dim or height_dim <= 0:
        raise ValueError("dimensions_m[2] must be positive")
    return height_m / height_dim


def register_asset(
    *,
    asset_id: str,
    name: str,
    category: str,
    source_file: Any,
    dimensions_m: List[float],
    tags: Optional[List[str]] = None,
    forward_axis: str = "+Y",
    has_armature: bool = False,
    animations: Optional[List[Dict[str, Any]]] = None,
    source: str = "manual",
    source_url: Optional[str] = None,
    assets_root: Optional[Path] = None,
    overwrite: bool = False,
) -> Path:
    """Register (or overwrite) one library object. Copies ``source_file`` to
    ``<root>/<category>/<id>/model.glb`` and writes ``meta.json`` atomically.

    Does NOT call rebuild_index() itself — three different callers (section
    9.2) each decide when to rebuild.

    Returns the asset's directory (``<root>/<category>/<id>``).
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")
    safe_id = _require_safe_asset_id(asset_id)
    if not dimensions_m or len(dimensions_m) != 3 or any(d is None or d <= 0 for d in dimensions_m):
        raise ValueError("dimensions_m must be three positive numbers")

    root = assets_root or blockout_assets_root()
    asset_dir = root / category / safe_id
    pre_existing = asset_dir.exists()

    if pre_existing and not overwrite:
        raise FileExistsError(f"asset already registered: {asset_dir}")

    asset_version = 1
    existing_meta_path = asset_dir / META_FILENAME
    if existing_meta_path.is_file():
        try:
            existing_meta = json.loads(existing_meta_path.read_text(encoding="utf-8"))
            asset_version = int(existing_meta.get("asset_version", 1)) + 1
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            asset_version = 1

    try:
        asset_dir.mkdir(parents=True, exist_ok=True)
        dest_file = asset_dir / DEFAULT_ASSET_FILENAME
        shutil.copyfile(str(source_file), str(dest_file))
        file_size_bytes = dest_file.stat().st_size
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        meta = {
            "id": safe_id,
            "name": name,
            "category": category,
            "tags": list(tags or []),
            "file": DEFAULT_ASSET_FILENAME,
            "dimensions_m": [float(d) for d in dimensions_m],
            "pivot": "base_center",
            "forward_axis": forward_axis,
            "up_axis": "+Z",
            "has_armature": bool(has_armature),
            "animations": list(animations or []),
            "source": source,
            "source_url": source_url,
            "file_size_bytes": file_size_bytes,
            "fetched_at": fetched_at,
            "asset_version": asset_version,
        }

        meta_path = asset_dir / META_FILENAME
        tmp_path = f"{meta_path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, meta_path)
    except Exception:
        # Roll back only a directory THIS call created: on a fresh
        # registration (pre_existing=False), a copy/write failure must not
        # leave an orphan dir (truncated model.glb, no meta.json) that would
        # permanently block re-registration with FileExistsError. An
        # overwrite of a pre-existing asset that fails mid-way must NOT
        # delete the prior (still valid) registration.
        if not pre_existing:
            shutil.rmtree(asset_dir, ignore_errors=True)
        raise

    return asset_dir


@dataclass
class ProxyPart:
    """One primitive of a placeholder figure (section 9.4).

    ``size`` holds kind-specific keys: sphere -> {"diameter"}, capsule ->
    {"length", "radius"}, box -> {"side"}. ``position`` is the part's center
    in the asset's local space (pivot at base_center, z=0 is the ground).
    ``axis`` is the capsule's long axis ("+Z" vertical, "+Y" horizontal); not
    meaningful for sphere/box (None).

    This is a SPECIFICATION only: actual geometry is built by the renderer
    in Э3 (Blender primitives), so the formulas below live in exactly one
    place and are not duplicated there.
    """

    kind: str
    size: Dict[str, float]
    position: tuple
    axis: Optional[str] = None


def proxy_part_specs(body_plan: str, height_m: float) -> List[ProxyPart]:
    """Primitive breakdown of a placeholder figure for ``body_plan``, per the
    table in section 9.4. ``height_m`` is assumed already resolved by the
    caller (LLM answer, or typical category height as a fallback)."""
    h = float(height_m)
    if body_plan == "biped":
        return _biped_proxy_parts(h)
    if body_plan == "quadruped":
        return _quadruped_proxy_parts(h)
    if body_plan == "none":
        return _box_proxy_parts(h)
    raise ValueError(f"unknown body_plan: {body_plan!r}")


def _biped_proxy_parts(h: float) -> List[ProxyPart]:
    leg_length = 0.50 * h
    leg_radius = h / 22
    torso_length = 0.357 * h
    torso_radius = h / 9
    arm_length = 0.32 * h
    arm_radius = h / 28
    head_diameter = h / 7

    torso_bottom = leg_length
    torso_center_z = torso_bottom + torso_length / 2
    torso_top = torso_bottom + torso_length
    head_center_z = torso_top + head_diameter / 2

    leg_offset_x = torso_radius / 2
    arm_offset_x = torso_radius + arm_radius

    return [
        ProxyPart("sphere", {"diameter": head_diameter}, (0.0, 0.0, head_center_z), None),
        ProxyPart("capsule", {"length": torso_length, "radius": torso_radius}, (0.0, 0.0, torso_center_z), "+Z"),
        ProxyPart("capsule", {"length": arm_length, "radius": arm_radius}, (-arm_offset_x, 0.0, torso_center_z), "+Z"),
        ProxyPart("capsule", {"length": arm_length, "radius": arm_radius}, (arm_offset_x, 0.0, torso_center_z), "+Z"),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (-leg_offset_x, 0.0, leg_length / 2), "+Z"),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (leg_offset_x, 0.0, leg_length / 2), "+Z"),
    ]


def _quadruped_proxy_parts(h: float) -> List[ProxyPart]:
    # H is withers height for the quadruped: body top sits at z=H.
    body_length = 1.4 * h
    body_radius = h / 4
    head_diameter = h / 3
    leg_length = 0.5 * h
    leg_radius = h / 12

    body_center_z = h - body_radius
    leg_center_z = leg_length / 2
    head_center_z = body_center_z
    head_offset_y = body_length / 2 + head_diameter / 2
    leg_offset_x = body_radius / 2
    leg_offset_y = body_length / 2 - leg_radius

    return [
        ProxyPart("capsule", {"length": body_length, "radius": body_radius}, (0.0, 0.0, body_center_z), "+Y"),
        ProxyPart("sphere", {"diameter": head_diameter}, (0.0, head_offset_y, head_center_z), None),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (-leg_offset_x, leg_offset_y, leg_center_z), "+Z"),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (leg_offset_x, leg_offset_y, leg_center_z), "+Z"),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (-leg_offset_x, -leg_offset_y, leg_center_z), "+Z"),
        ProxyPart("capsule", {"length": leg_length, "radius": leg_radius}, (leg_offset_x, -leg_offset_y, leg_center_z), "+Z"),
    ]


def _box_proxy_parts(h: float) -> List[ProxyPart]:
    return [ProxyPart("box", {"side": h}, (0.0, 0.0, h / 2), None)]


def _cmd_rebuild_index(args) -> int:
    assets_root = Path(args.assets_dir).expanduser().resolve() if args.assets_dir else None
    result = rebuild_index(assets_root=assets_root)
    for warning in result.warnings:
        logger.warning(warning)
        print(f"WARNING: {warning}")
    print(f"Rebuilt index with {len(result.objects)} object(s).")
    return 0


def _cmd_add(args) -> int:
    import sys

    normalized_path = Path(args.normalized_json)
    try:
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"failed to read --normalized-json: {exc}", file=sys.stderr)
        return 1
    if not normalized.get("ok"):
        print(f"normalize result is not ok: {normalized.get('error')}", file=sys.stderr)
        return 1
    source_file = normalized.get("output_glb_path")
    if not source_file:
        print("--normalized-json is missing output_glb_path", file=sys.stderr)
        return 1

    assets_root = Path(args.assets_dir).expanduser().resolve() if args.assets_dir else None
    try:
        asset_dir = register_asset(
            asset_id=args.id,
            name=args.name,
            category=args.category,
            tags=list(normalized.get("tags") or []),
            source_file=source_file,
            dimensions_m=normalized["dimensions_m"],
            forward_axis=normalized.get("forward_axis", "+Y"),
            has_armature=bool(normalized.get("has_armature", False)),
            animations=normalized.get("animations") or [],
            source=args.source,
            source_url=args.source_url,
            assets_root=assets_root,
        )
    except (ValueError, FileExistsError, FileNotFoundError, KeyError, OSError) as exc:
        print(f"add failed: {exc}", file=sys.stderr)
        return 1
    print(f"Registered asset at {asset_dir}")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Blockout asset library maintenance CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rebuild = sub.add_parser("rebuild-index", help="Rescan assets/blockout and rewrite index.json")
    p_rebuild.add_argument("--assets-dir", default=None)

    p_add = sub.add_parser("add", help="Register an asset normalized by blockout_blender/normalize_asset.py")
    p_add.add_argument("--category", required=True, choices=CATEGORIES)
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--source", required=True)
    p_add.add_argument("--source-url", default=None)
    p_add.add_argument("--normalized-json", required=True, help="Output JSON produced by normalize_asset.py")
    p_add.add_argument("--assets-dir", default=None)

    args = parser.parse_args()

    if args.command == "rebuild-index":
        return _cmd_rebuild_index(args)
    if args.command == "add":
        return _cmd_add(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
