"""Чистое ядро болванки (Ф11): хеш, состояние шота, P10, защищённое чтение файлов.

Без Tk, Blender и импорта `blockout_renderer`: правила перенесены из
удалённой старой Tk-панели болванки.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

BLOCKOUT_DIR = "93_blockout"

STATE_MISSING = "missing"
STATE_STALE = "stale"
STATE_JUNCTION_ERROR = "junction_error"
STATE_RENDERED = "rendered"

STATE_TITLES = {
    STATE_RENDERED: "готова",
    STATE_STALE: "устарела",
    STATE_MISSING: "отсутствует",
    STATE_JUNCTION_ERROR: "ошибка",
}


def shot_dir_name(scene_number: int, shot_number: int) -> str:
    return f"scene_{int(scene_number):02d}_shot_{int(shot_number):02d}"


def make_key(scene_number: int, shot_number: int) -> str:
    return f"{int(scene_number)}-{int(shot_number)}"


def parse_key(value: Any) -> Optional[tuple[int, int]]:
    """`S-N` (нули допустимы) -> (сцена, шот); иначе None."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{1,6})-(\d{1,6})", value.strip(), re.ASCII)
    return (int(m.group(1)), int(m.group(2))) if m else None


_CHAIN_ID = re.compile(r"[\w.-]+", re.ASCII)


def safe_chain_id(value: Any) -> bool:
    """Имя цепочки идёт в путь и в blockout_scope: только буквы/цифры/_ . - и без `..`."""
    return isinstance(value, str) and bool(_CHAIN_ID.fullmatch(value)) and ".." not in value


def compute_spec_hash(chain_spec: dict) -> str:
    """Тот же хеш, что `blockout_renderer.compute_spec_hash` (проверяет тест паритета)."""
    canonical = json.dumps(chain_spec, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_resolution(value: Any) -> Optional[list[int]]:
    """`"WxH"` -> [W, H]; не разобрано -> None (трактуется как несовпадение)."""
    if not value or not isinstance(value, str):
        return None
    parts = value.lower().split("x")
    if len(parts) != 2:
        return None
    try:
        return [int(parts[0]), int(parts[1])]
    except ValueError:
        return None


def duration_mismatch(shots_duration_s: Any, scene_spec_duration_s: Any) -> bool:
    if scene_spec_duration_s is None or shots_duration_s is None:
        return False
    return shots_duration_s != scene_spec_duration_s


def manifest_current(manifest: Optional[dict], *, duration_s: Any, fps: Any, spec_hash: Any,
                     resolution: Optional[list[int]] = None) -> bool:
    """Манифест совпадает с текущими параметрами. blender_version не сравниваем (как в Tk)."""
    if not manifest:
        return False
    mres = manifest.get("resolution")
    resolution_ok = resolution is not None and mres is not None and list(mres) == list(resolution)
    return (manifest.get("duration_s") == duration_s and manifest.get("fps") == fps
            and manifest.get("spec_hash") == spec_hash and resolution_ok)


def compute_state(manifest: Optional[dict], *, shots_duration_s: Any = None, scene_spec_duration_s: Any = None,
                  fps: Any = None, spec_hash: Optional[str] = None,
                  resolution: Optional[list[int]] = None) -> str:
    if manifest is None:
        return STATE_MISSING
    if duration_mismatch(shots_duration_s, scene_spec_duration_s):
        return STATE_STALE
    if not manifest_current(manifest, duration_s=scene_spec_duration_s, fps=fps,
                            spec_hash=spec_hash, resolution=resolution):
        return STATE_STALE
    for name in ("junction_with_prev", "junction_with_next"):
        junction = manifest.get(name)
        if isinstance(junction, dict) and junction.get("status") == "failed":
            return STATE_JUNCTION_ERROR
    return STATE_RENDERED


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rendered_before_images(manifest: Optional[dict], image_mtimes: Sequence[float]) -> bool:
    """Картинки кадра старше болванки (без учёта согласия человека)."""
    if not manifest:
        return False
    rendered_at = parse_iso_utc(manifest.get("rendered_at"))
    if rendered_at is None or not image_mtimes:
        return False
    return any(datetime.fromtimestamp(mt, tz=timezone.utc) < rendered_at for mt in image_mtimes)


def is_p10(manifest: Optional[dict], image_mtimes: Sequence[float]) -> bool:
    """P10: картинки старше болванки, и человек с этим ещё не согласился."""
    if not manifest or manifest.get("p10_acknowledged"):
        return False
    return rendered_before_images(manifest, image_mtimes)


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def final_image_mtimes(root: Path, scene_number: int, shot_number: int) -> list[float]:
    """mtime самых свежих готовых картинок start/end шота (по именам img_final_*)."""
    folder = root / "97_shots" / shot_dir_name(scene_number, shot_number)
    best: dict[str, float] = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        name = entry.name.lower()
        for part in ("start", "end"):
            if name.startswith(f"img_final_{part}_") and name.endswith(_IMAGE_EXTS):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if entry.is_file() and st.st_size > 0:
                    best[part] = max(best.get(part, 0.0), st.st_mtime)
    return list(best.values())


# --- защищённое чтение JSON с кэшем по (mtime_ns, size, inode) ---------------------------------

_CACHE: "OrderedDict[str, tuple[tuple, Any, str]]" = OrderedDict()
_CACHE_GUARD = threading.Lock()
_CACHE_SIZE = 4096
_FRESH_S = 2.0


def clear_cache() -> None:
    with _CACHE_GUARD:
        _CACHE.clear()


def read_json(path: Path) -> tuple[Any, str]:
    """(данные, статус): `ok` | `missing` | `broken`. Не бросает; данные не менять."""
    try:
        st = path.stat()
    except OSError:
        return None, "missing"
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    name = str(path)
    with _CACHE_GUARD:
        hit = _CACHE.get(name)
        if hit and hit[0] == sig:
            _CACHE.move_to_end(name)
            return hit[1], hit[2]
    try:
        data, status = json.loads(path.read_text(encoding="utf-8")), "ok"
    except (OSError, ValueError):
        data, status = None, "broken"
    # свежий файл (моложе ~2 с) не кэшируем: перезапись того же размера в ту же долю времени не отличить
    if time.time() - st.st_mtime >= _FRESH_S:
        with _CACHE_GUARD:
            _CACHE[name] = (sig, data, status)
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.popitem(last=False)
    return data, status
