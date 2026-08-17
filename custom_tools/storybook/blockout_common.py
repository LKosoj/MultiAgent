"""Shared pure functions and Blender-launch plumbing for the blockout layer.

Used by ``blockout_scene_builder.py`` (Э2) and, later, ``blockout_renderer.py``
(Э3) so both sides of the pipeline compute timelines, chains, camera geometry
and the Blender launch contract identically — a divergence here would break
checks B09/B10/B16 (TZ docs/tz-blockout-reference-pipeline.md, разделы 6-7,
10.1, 13.4, Приложение А).

No project-file I/O beyond the small atomic-write helpers below; no LLM
calls; no network. Safe to import from anywhere without cycles.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# === Timeline / frame formulas (Приложение А, раздел 6.4, 6.3) =============

FRAME_GRID_TOLERANCE = 1e-6
MIN_BLENDER_VERSION = (4, 2)


def n_render(duration_s: float, fps: int) -> int:
    """Number of frames rendered for a shot: duration_s * fps + 1 (раздел 6.4)."""
    return round(duration_s * fps) + 1


def n_video(duration_s: float, fps: int) -> int:
    """Number of frames in the video reference clip: duration_s * fps (раздел 6.4)."""
    return round(duration_s * fps)


def frame_local_time(t_start: float, i: int, fps: int) -> float:
    """t(i) = t_start + (i - 1) / fps — момент времени локального фрейма i."""
    return t_start + (i - 1) / fps


def next_chain_t_start(t_start: float, duration_s: float) -> float:
    """t_start(N+1) = t_start(N) + duration_s(N)."""
    return t_start + duration_s


def frame_blender(t: float, fps: int) -> int:
    """frame_blender(t) = round(t * fps) + 1 — номер фрейма в chain.blend."""
    return round(t * fps) + 1


def shot_window_frames(t_start: float, duration_s: float, fps: int) -> Tuple[int, int]:
    """[frame_blender(t_start), frame_blender(t_start + duration_s)]."""
    return frame_blender(t_start, fps), frame_blender(t_start + duration_s, fps)


def is_on_frame_grid(t: float, fps: int, tolerance: float = FRAME_GRID_TOLERANCE) -> bool:
    """|t * fps - round(t * fps)| <= tolerance (проверка B09, раздел 20.1)."""
    scaled = t * fps
    return abs(scaled - round(scaled)) <= tolerance


def snap_to_frame_grid(t: float, fps: int) -> float:
    """t = round(t * fps) / fps — притягивание к сетке фреймов (раздел 10.1)."""
    return round(t * fps) / fps


# === Continuity chains (раздел 7) ===========================================

_KNOWN_LINK_TYPES = {"full_copy", "independent", "reference"}


def effective_link_type(shot_elements: Sequence[Dict[str, Any]]) -> Tuple[str, bool]:
    """Reads ``link_type`` from the ``shot_type: "start"`` element of one shot.

    Returns ``(link_type, used_fallback)``. Missing/empty/unrecognized values,
    or a shot with no ``start`` element at all, fall back to ``"independent"``
    with ``used_fallback=True`` (раздел 7.1, «Неполные и неожиданные данные»).
    """
    start = next((el for el in shot_elements if el.get("shot_type") == "start"), None)
    if start is None:
        return "independent", True
    raw = str(start.get("link_type") or "").strip().lower()
    if raw not in _KNOWN_LINK_TYPES:
        return "independent", True
    return raw, False


def _shot_duration_s(shot_elements: Sequence[Dict[str, Any]]) -> Optional[int]:
    for el in shot_elements:
        value = el.get("duration_s")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


@dataclass
class ChainShot:
    shot_number: int
    duration_s: int
    t_start: float

    def to_dict(self) -> Dict[str, Any]:
        return {"shot_number": self.shot_number, "duration_s": self.duration_s, "t_start": self.t_start}


@dataclass
class Chain:
    chain_id: str
    scene_number: int
    shots: List[ChainShot] = field(default_factory=list)

    @property
    def total_duration_s(self) -> int:
        return sum(shot.duration_s for shot in self.shots)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "scene_number": self.scene_number,
            "shots": [shot.to_dict() for shot in self.shots],
            "total_duration_s": self.total_duration_s,
        }


def compute_chains(
    scene_number: int, shot_elements: Sequence[Dict[str, Any]]
) -> Tuple[List[Chain], List[str]]:
    """Splits one scene's flat ``shots.json`` elements into continuity chains
    (раздел 7). ``shot_elements`` is every element (``start`` and ``end``) for
    that scene, in any order; shots are grouped by ``shot_number`` and
    processed in ascending order — gaps in numbering do not affect the split.

    Every shot must already carry a resolved ``duration_s`` (B01 is the
    caller's responsibility, раздел 10.1 пункт 2); a shot missing it is
    skipped with a warning rather than raising, since chain computation
    itself never blocks the run.

    Returns ``(chains, warnings)`` — free-text warnings for report section
    "blockout_scene_builder" (раздел 7.1, без кодов раздела 20.2).
    """
    by_shot: Dict[int, List[Dict[str, Any]]] = {}
    for el in shot_elements:
        try:
            shot_number = int(el.get("shot_number"))
        except (TypeError, ValueError):
            continue
        by_shot.setdefault(shot_number, []).append(el)

    warnings: List[str] = []
    chains: List[Chain] = []
    current: Optional[Chain] = None
    t_start = 0.0
    chain_seq = 0
    is_first_shot = True

    for shot_number in sorted(by_shot.keys()):
        elements = by_shot[shot_number]
        duration = _shot_duration_s(elements)
        if duration is None:
            warnings.append(
                f"scene {scene_number} shot {shot_number}: no duration_s, skipped from chain computation"
            )
            continue

        link_type, used_fallback = effective_link_type(elements)
        if used_fallback:
            warnings.append(
                f"scene {scene_number} shot {shot_number}: link_type missing or unrecognized, "
                "treated as independent (new chain)"
            )
        if is_first_shot and link_type == "full_copy":
            warnings.append(
                f"scene {scene_number} shot {shot_number}: first shot of scene has link_type "
                "full_copy, starting a new chain anyway (no cross-scene continuity, раздел 7.1)"
            )

        starts_new_chain = is_first_shot or link_type != "full_copy"
        if starts_new_chain:
            chain_seq += 1
            current = Chain(chain_id=f"sc{scene_number:02d}_ch{chain_seq:02d}", scene_number=scene_number)
            chains.append(current)
            t_start = 0.0

        assert current is not None
        current.shots.append(ChainShot(shot_number=shot_number, duration_s=duration, t_start=t_start))
        t_start = next_chain_t_start(t_start, duration)
        is_first_shot = False

    return chains, warnings


def write_chains_json(path: Path, chains: Sequence[Chain]) -> None:
    """Atomic write of ``93_blockout/chains.json``. Write-only artifact — no
    pipeline step reads it back (раздел 7.2), so no sidecar lock is needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"chains": [chain.to_dict() for chain in chains]}
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


# === Camera geometry (раздел 10.1, 15.2, Приложение А) ======================


def sensor_h_mm(sensor_mm: float, h_render: int, w_render: int) -> float:
    """h_sensor_mm = sensor_mm * h_render / w_render — вертикальный размер сенсора."""
    return sensor_mm * h_render / w_render


def camera_distance_m(lens_mm: float, s_m: float, fraction: float, h_sensor_mm_value: float) -> float:
    """d = lens_mm * S_m / (доля * h_sensor_mm) (раздел 10.1, Приложение А)."""
    return lens_mm * s_m / (fraction * h_sensor_mm_value)


def camera_rot_deg(location: Sequence[float], look_at: Sequence[float]) -> Tuple[float, float]:
    """rot_x/rot_z (градусы) of a camera at ``location`` pointed at ``look_at``
    (раздел 15.2, Приложение А)::

        d = look_at - location
        rot_x = 90 + atan2(d_z, hypot(d_x, d_y))
        rot_z = atan2(d_y, d_x) - 90
    """
    d_x = look_at[0] - location[0]
    d_y = look_at[1] - location[1]
    d_z = look_at[2] - location[2]
    rot_x = 90.0 + math.degrees(math.atan2(d_z, math.hypot(d_x, d_y)))
    rot_z = math.degrees(math.atan2(d_y, d_x)) - 90.0
    return rot_x, rot_z


def animation_phase(
    phase_at_t0: float, t_in_chain: float, t0: float, speed: float, clip_duration_s: float, loop: bool
) -> float:
    """x = phase_at_t0 + (t_in_chain - t0) * speed / clip.duration_s;
    phase = frac(x) при loop else min(x, 1.0) (раздел 15.2, Приложение А)."""
    x = phase_at_t0 + (t_in_chain - t0) * speed / clip_duration_s
    if loop:
        return x - math.floor(x)
    return min(x, 1.0)


# === Resolution matching (Приложение А) =====================================


def resolution_match(w: int, h: int, a_num: int, a_den: int) -> Tuple[int, int]:
    """Пересчёт размера болванки под соотношение сторон видеомодели::

        k        = max(1, round(sqrt(w * h / (4 * a_num * a_den))))
        w_render = 2 * k * a_num
        h_render = 2 * k * a_den

    Площадь сохраняется, стороны чётны (для -pix_fmt yuv420p), соотношение
    точное.
    """
    k = max(1, round(math.sqrt((w * h) / (4 * a_num * a_den))))
    return 2 * k * a_num, 2 * k * a_den


# === Track evaluation (раздел 12, 15.4) =====================================


def evaluate_track_scalar(keys: Sequence[Dict[str, Any]], t: float) -> float:
    """Evaluates a scalar track (e.g. ``lens_mm``) at time ``t``.

    ``keys`` — sorted by ``t``, each ``{"t": float, "v": float, "interp": str}``.
    A single-key track with ``interp: "constant"`` is constant everywhere
    (раздел 12). ``interp`` of a key describes the segment *after* that key;
    ``bezier`` is approximated as Blender's ``AUTO_CLAMPED`` smoothstep
    ``3s^2 - 2s^3`` (раздел 12, «Откуда четыре ключа»​).
    """
    if not keys:
        raise ValueError("empty track")
    if len(keys) == 1:
        return float(keys[0]["v"])
    if t <= keys[0]["t"]:
        return float(keys[0]["v"])
    if t >= keys[-1]["t"]:
        return float(keys[-1]["v"])
    for left, right in zip(keys, keys[1:]):
        if left["t"] <= t <= right["t"]:
            span = right["t"] - left["t"]
            s = 0.0 if span <= 0 else (t - left["t"]) / span
            interp = left.get("interp", "linear")
            if interp == "constant":
                return float(left["v"])
            if interp == "bezier":
                s = 3 * s * s - 2 * s * s * s
            return float(left["v"]) + (float(right["v"]) - float(left["v"])) * s
    return float(keys[-1]["v"])


def evaluate_track_vec3(keys: Sequence[Dict[str, Any]], t: float) -> Tuple[float, float, float]:
    """Same as ``evaluate_track_scalar`` but for 3-component ``v`` values."""
    if not keys:
        raise ValueError("empty track")
    if len(keys) == 1:
        v = keys[0]["v"]
        return float(v[0]), float(v[1]), float(v[2])
    if t <= keys[0]["t"]:
        v = keys[0]["v"]
        return float(v[0]), float(v[1]), float(v[2])
    if t >= keys[-1]["t"]:
        v = keys[-1]["v"]
        return float(v[0]), float(v[1]), float(v[2])
    for left, right in zip(keys, keys[1:]):
        if left["t"] <= t <= right["t"]:
            span = right["t"] - left["t"]
            s = 0.0 if span <= 0 else (t - left["t"]) / span
            interp = left.get("interp", "linear")
            if interp == "constant":
                v = left["v"]
                return float(v[0]), float(v[1]), float(v[2])
            if interp == "bezier":
                s = 3 * s * s - 2 * s * s * s
            lv, rv = left["v"], right["v"]
            return tuple(float(lv[i]) + (float(rv[i]) - float(lv[i])) * s for i in range(3))  # type: ignore[return-value]
    v = keys[-1]["v"]
    return float(v[0]), float(v[1]), float(v[2])


def track_covers_range(keys: Sequence[Dict[str, Any]], total_duration_s: float) -> bool:
    """B08: track covers [0, total_duration_s], or is the single-key
    ``constant`` shorthand for "constant everywhere" (раздел 12)."""
    if not keys:
        return False
    if len(keys) == 1:
        return keys[0].get("interp") == "constant" and abs(keys[0]["t"]) <= FRAME_GRID_TOLERANCE
    return (
        abs(keys[0]["t"]) <= FRAME_GRID_TOLERANCE
        and abs(keys[-1]["t"] - total_duration_s) <= FRAME_GRID_TOLERANCE
    )


def track_keys_on_frame_grid(keys: Sequence[Dict[str, Any]], fps: int) -> bool:
    """B09: every key's ``t`` lands on a whole frame."""
    return all(is_on_frame_grid(key["t"], fps) for key in keys)


def camera_continuity_ok(
    location_keys: Sequence[Dict[str, Any]],
    look_at_keys: Sequence[Dict[str, Any]],
    lens_keys: Sequence[Dict[str, Any]],
    t_junction: float,
    fps: int,
) -> bool:
    """B10: no derivative discontinuity at an internal chain junction (раздел
    15.4). Finite differences at ``t_junction`` for ``location``/``look_at``
    (threshold 2 m/s) and ``lens_mm`` (threshold 60 mm/s). A track that is a
    single constant key is exempt (раздел 15.4)."""
    dt = 1.0 / fps

    def _vec_ok(keys: Sequence[Dict[str, Any]], threshold: float) -> bool:
        # Раздел 15.4: исключение — трек из единственного ключа с
        # interp: "constant". Отдельно проверять interp тут не нужно:
        # evaluate_track_vec3() для len(keys) == 1 всегда возвращает
        # keys[0]["v"] независимо от interp (см. её докстринг выше), так
        # что такой трек и так неотличим от константного.
        if len(keys) <= 1:
            return True
        before = evaluate_track_vec3(keys, t_junction - dt)
        at = evaluate_track_vec3(keys, t_junction)
        after = evaluate_track_vec3(keys, t_junction + dt)
        v_before = tuple((at[i] - before[i]) * fps for i in range(3))
        v_after = tuple((after[i] - at[i]) * fps for i in range(3))
        delta = math.sqrt(sum((v_after[i] - v_before[i]) ** 2 for i in range(3)))
        return delta <= threshold

    def _scalar_ok(keys: Sequence[Dict[str, Any]], threshold: float) -> bool:
        # Раздел 15.4: та же эквивалентность, что в _vec_ok() выше —
        # evaluate_track_scalar() для len(keys) == 1 игнорирует interp.
        if len(keys) <= 1:
            return True
        before = evaluate_track_scalar(keys, t_junction - dt)
        at = evaluate_track_scalar(keys, t_junction)
        after = evaluate_track_scalar(keys, t_junction + dt)
        v_before = (at - before) * fps
        v_after = (after - at) * fps
        return abs(v_after - v_before) <= threshold

    return (
        _vec_ok(location_keys, 2.0)
        and _vec_ok(look_at_keys, 2.0)
        and _scalar_ok(lens_keys, 60.0)
    )


# === Cache hashing (раздел 10.1, «кеш») =====================================


def chain_input_hash(payload: Dict[str, Any]) -> str:
    """sha256 of the LLM-spatial-parse input for one chain, excluding fps and
    camera_aspect (раздел 10.1, «кеш»: they are compared separately as their
    own ``scene_spec.json`` fields, not folded into the hash)."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# === Blender availability (B12, раздел 13.4) ================================


def check_blender_available(min_version: Tuple[int, int] = MIN_BLENDER_VERSION) -> Tuple[bool, Optional[str], str]:
    """Checks the Blender binary/module is present and >= ``min_version``
    (раздел 13.4, B12). Returns ``(available, version_string_or_None, message)``.
    Never raises.
    """
    mode = os.getenv("BLOCKOUT_BLENDER_MODE", "binary").strip().lower()
    if mode == "module":
        try:
            import bpy  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            return False, None, f"bpy module not importable: {exc}"
        version = tuple(bpy.app.version[:2])
        version_str = f"{version[0]}.{version[1]}"
        if version < min_version:
            return False, version_str, f"blender module version {version_str} is below minimum {min_version[0]}.{min_version[1]}"
        return True, version_str, "ok"

    blender_bin = os.getenv("BLOCKOUT_BLENDER_BIN") or shutil.which("blender")
    if not blender_bin:
        return False, None, "blender binary not found in PATH (set BLOCKOUT_BLENDER_BIN)"
    try:
        proc = subprocess.run(
            [blender_bin, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except Exception as exc:  # noqa: BLE001
        return False, None, f"failed to run '{blender_bin} --version': {exc}"
    if proc.returncode != 0:
        return False, None, f"'{blender_bin} --version' exited with code {proc.returncode}"
    match = re.search(r"Blender\s+(\d+)\.(\d+)", proc.stdout)
    if not match:
        return False, None, f"could not parse blender version from output: {proc.stdout[:200]!r}"
    version = (int(match.group(1)), int(match.group(2)))
    version_str = f"{version[0]}.{version[1]}"
    if version < min_version:
        return False, version_str, f"blender version {version_str} is below minimum {min_version[0]}.{min_version[1]}"
    return True, version_str, "ok"


# === Blender launch contract (раздел 13.4) ==================================


def run_blender_script(
    script_path: Path,
    payload: Dict[str, Any],
    output_json_path: Path,
    *,
    timeout: float,
    module_callable: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Launches Blender per the section 13.4 contract: JSON-in via a temp
    file passed after ``--``, JSON-out via ``output_json_path`` (which the
    caller must also set as ``payload["output_path"]``). One process per
    call. Never raises; returns ``{"ok": False, "error": ...}`` on any
    failure (non-zero exit, missing output file, exception, ...).

    ``module_callable`` is required for ``BLOCKOUT_BLENDER_MODE=module``
    (import indirection stays with the caller so this function has no
    hard dependency on any specific Blender script module).
    """
    mode = os.getenv("BLOCKOUT_BLENDER_MODE", "binary").strip().lower()
    try:
        if mode == "module":
            if module_callable is None:
                return {"ok": False, "error": "module_callable not provided for BLOCKOUT_BLENDER_MODE=module"}
            result = module_callable(payload)
        else:
            work_dir = output_json_path.parent
            work_dir.mkdir(parents=True, exist_ok=True)
            input_json_path = work_dir / f"input.{os.getpid()}.json"
            input_json_path.write_text(json.dumps(payload), encoding="utf-8")
            blender_bin = os.getenv("BLOCKOUT_BLENDER_BIN", "blender")
            proc = subprocess.run(
                [blender_bin, "-b", "-P", str(script_path), "--", str(input_json_path)],
                capture_output=True,
                timeout=timeout,
                text=True,
                check=False,
            )
            parsed: Optional[Dict[str, Any]] = None
            not_object = False
            if output_json_path.is_file():
                try:
                    loaded = json.loads(output_json_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    loaded = None
                if isinstance(loaded, dict):
                    parsed = loaded
                elif loaded is not None:
                    not_object = True
            if parsed is None:
                # render_shot.py writes a clean error JSON on caught exceptions even
                # when it exits non-zero; only fall back to the raw stderr blob when
                # that JSON is absent or corrupt, so CAM_INSIDE/SUBJ_HIDDEN survive.
                # WHY: a valid-but-non-dict output (e.g. a JSON list) must not reach
                # dict(result) below (TypeError swallowed by the outer except, hiding
                # proc.stderr) -- treated the same as a missing/corrupt file.
                detail = "output json was not an object; " if not_object else ""
                return {
                    "ok": False,
                    "error": f"blender exited with code {proc.returncode}: {detail}{proc.stderr}",
                }
            result = parsed
        return dict(result)
    except Exception as exc:  # noqa: BLE001 - launch must never raise into the caller
        return {"ok": False, "error": str(exc)}
