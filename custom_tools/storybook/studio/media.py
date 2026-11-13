"""Медиа проекта: список файлов, кэш длительности, субтитры и таймлайн (общий слой для React и Tk).

Все пути в ответах относительные (POSIX, от папки проекта). Длительность видео и звука
хранится в `.studio/media_index.json` и пересчитывается, только если файл изменился.
Таймлайн фильма собирается в `media_timeline.py`, здесь лежат его «розетки» (@capability).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .capabilities import capability
from .errors import StudioError, success
from .labels import MEDIA_TYPE_LABELS
from .paths import MEDIA_EXTENSIONS, project_dir

logger = logging.getLogger(__name__)

# --- подписи (держим здесь, labels.py в этой волне не трогаем) -------------------------

MEDIA_TYPE_TITLES: dict[str, str] = {
    **MEDIA_TYPE_LABELS,
    "photo_video": "Фотомонтаж",
    "narration": "Озвучка",
}

MEDIA_CATEGORY_LABELS: dict[str, str] = {
    "book_page": "Страница книги",
    "character": "Герой",
    "landmark": "Ориентир",
    "location": "Локация",
    "background": "Фон (лист пространства)",
    "start_frame": "Кадр начала",
    "end_frame": "Кадр конца",
    "shot_other": "Другое изображение шота",
    "shot_clip": "Видео шота",
    "video_unit": "Видео-блок (несколько шотов)",
    "video_unit_variant": "Вариант видео-блока",
    "shot_video_other": "Другое видео шота",
    "final": "Итоговый фильм",
    "photo": "Из картинок",
    "planned": "Черновые титры",
    "final_subs": "Титры фильма",
    "narration": "Озвучка",
    "leitmotif": "Лейтмотив",
    "neutral": "Нейтральная музыка",
    "scene_sfx": "Эффект сцены",
    "audio_other": "Другой звук",
    "blockout_ref": "Болванка: референс",
    "blockout_frame": "Болванка: кадр",
    "blockout_sheet": "Болванка: лист",
    "blockout_video": "Болванка: видео",
    "blockout_preview": "Болванка: превью",
}


def media_type_title(value: str) -> str:
    return MEDIA_TYPE_TITLES.get(value, value)


def media_category_title(value: str) -> str:
    return MEDIA_CATEGORY_LABELS.get(value, value)


def media_labels() -> dict:
    return {"types": dict(MEDIA_TYPE_TITLES), "categories": dict(MEDIA_CATEGORY_LABELS)}


KINDS = ("image", "video", "audio", "subtitles")

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}
_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
_SUBTITLE_EXT = {".srt", ".vtt"}

# порядок типов при сортировке «по сцене и шоту»
_TYPE_ORDER = ("illustration", "reference", "shot", "shot_video", "blockout_ref", "blockout_frame",
               "blockout_sheet", "blockout_video", "blockout_preview", "narration", "music", "sfx",
               "subtitles", "final_video", "photo_video", "audio")
_SORT_FIELDS = ("scene_shot", "name", "modified", "size", "type", "duration")
_MAX_DEPTH = 8


def _kind_of(suffix: str) -> str | None:
    suffix = suffix.lower()
    if suffix in _IMAGE_EXT:
        return "image"
    if suffix in _VIDEO_EXT:
        return "video"
    if suffix in _AUDIO_EXT:
        return "audio"
    if suffix in _SUBTITLE_EXT:
        return "subtitles"
    return None


# --- чтение json с кэшем в памяти -------------------------------------------------------

_JSON_CACHE: "OrderedDict[str, tuple[int, int, object]]" = OrderedDict()
_JSON_CACHE_MAX = 16
_JSON_LOCK = threading.Lock()


def inside_root(root: Path, path: Path) -> bool:
    """Это обычный файл, и после раскрытия симлинков он лежит внутри проекта."""
    try:
        path.resolve().relative_to(Path(root).resolve())
        return path.is_file()
    except (OSError, ValueError):
        return False


def read_json_cached(path: Path, root: Path) -> tuple[str, object]:
    """('ok', данные) | ('missing', None) | ('bad', None). Ключ кэша: путь, размер, mtime_ns.

    Файл, который через симлинк выходит за пределы проекта root, считается отсутствующим."""
    if not inside_root(root, path):
        return "missing", None
    try:
        st = path.stat()
    except OSError:
        return "missing", None
    key = str(path)
    with _JSON_LOCK:
        hit = _JSON_CACHE.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            _JSON_CACHE.move_to_end(key)
            return "ok", hit[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "bad", None
    with _JSON_LOCK:
        _JSON_CACHE[key] = (st.st_size, st.st_mtime_ns, data)
        _JSON_CACHE.move_to_end(key)
        while len(_JSON_CACHE) > _JSON_CACHE_MAX:
            _JSON_CACHE.popitem(last=False)
    return "ok", data


def norm_key(raw: object) -> str | None:
    """'2_3', '2-3', '02-03' -> '2-3'."""
    match = re.match(r"^\s*(\d+)\D+(\d+)\s*$", str(raw))
    return f"{int(match.group(1))}-{int(match.group(2))}" if match else None


# --- запись инвентаря и сканер -----------------------------------------------------------


@dataclass(frozen=True)
class MediaEntry:
    path: str
    name: str
    kind: str
    type: str
    category: str
    key: str | None
    keys: tuple[str, ...]
    scene: int | None
    shot: int | None
    page: int | None
    phase: str | None
    music_id: str | None
    size: int
    mtime_ns: int
    servable: bool


_SCENE_SHOT_RE = re.compile(r"^scene_(\d+)_shot_(\d+)$")
_SCENE_ONLY_RE = re.compile(r"^scene_(\d+)$")
_UNIT_RE = re.compile(r"^(\d+)-(\d+)_(\d+)-(\d+)(.*)$")
_PAGE_RE = re.compile(r"^page_(\d+)$")
_PHASE_RE = re.compile(r"^img_final_(start|end)_")


class _Hints:
    """Подсказки из манифестов: ключи видео-блоков, сцены эффектов, id музыки."""

    def __init__(self, root: Path):
        self.root = root
        self._units: dict[str, list[str]] | None = None
        self._sfx: dict[str, int] | None = None
        self._music: dict[str, str] | None = None

    def unit_keys(self, name: str) -> list[str] | None:
        if self._units is None:
            self._units = {}
            state, data = read_json_cached(self.root / "97_shots" / "video_plan.json", self.root)
            units = data.get("units") if state == "ok" and isinstance(data, dict) else None
            for unit in units if isinstance(units, list) else []:
                if not isinstance(unit, dict) or not isinstance(unit.get("video_path"), str):
                    continue
                keys = [k for k in (norm_key(x) for x in unit.get("shot_keys") or []) if k]
                if keys:
                    self._units[unit["video_path"].replace("\\", "/").rsplit("/", 1)[-1]] = keys
        return self._units.get(name)

    def sfx_scene(self, name: str) -> int | None:
        if self._sfx is None:
            self._sfx = {}
            for fname in ("sfx_manifest.json", "audio_manifest.json"):
                state, data = read_json_cached(self.root / "98_audio" / fname, self.root)
                tracks = data.get("audio_tracks") if state == "ok" and isinstance(data, dict) else None
                for track in tracks if isinstance(tracks, list) else []:
                    if not (isinstance(track, dict) and track.get("role") == "sfx"):
                        continue
                    scene, path = track.get("scene_number"), track.get("path")
                    if isinstance(path, str) and isinstance(scene, int) and not isinstance(scene, bool):
                        self._sfx.setdefault(path.replace("\\", "/").rsplit("/", 1)[-1], scene)
        return self._sfx.get(name)

    def music_id(self, name: str) -> str | None:
        if self._music is None:
            self._music = {}
            state, data = read_json_cached(self.root / "98_audio" / "music_manifest.json", self.root)
            for mid, value in (data.items() if state == "ok" and isinstance(data, dict) else []):
                path = value.get("path") if isinstance(value, dict) else value
                if isinstance(path, str) and _kind_of(Path(path).suffix) == "audio":
                    self._music.setdefault(path.replace("\\", "/").rsplit("/", 1)[-1], str(mid))
        return self._music.get(name)


def _dir_items(directory: Path, root: Path) -> list[tuple[Path, bool]]:
    """Содержимое папки: (путь, это_папка). Скрытое и симлинки наружу пропускаются."""
    try:
        with os.scandir(directory) as it:
            raw = sorted(it, key=lambda e: e.name)
    except OSError:
        return []
    out: list[tuple[Path, bool]] = []
    for entry in raw:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                target = Path(entry.path).resolve()
                target.relative_to(root)
                if not target.is_file():
                    continue  # симлинк на папку не раскрываем (защита от петель)
                out.append((Path(entry.path), False))
            elif entry.is_dir(follow_symlinks=False):
                out.append((Path(entry.path), True))
            elif entry.is_file():
                out.append((Path(entry.path), False))
        except (OSError, ValueError):
            continue
    return out


def _walk(base: Path, root: Path, depth: int = _MAX_DEPTH, _prefix: tuple[str, ...] = ()):
    """Файлы под base рекурсивно: (путь, части пути от base)."""
    if depth < 0:
        return
    for path, is_dir in _dir_items(base, root):
        if is_dir:
            yield from _walk(path, root, depth - 1, _prefix + (path.name,))
        else:
            yield path, _prefix + (path.name,)


def _unit_keys_from_name(match: "re.Match[str]") -> list[str]:
    a, b, c, d = (int(match.group(i)) for i in range(1, 5))
    if a == c and 0 <= d - b <= 200:
        return [f"{a}-{x}" for x in range(b, d + 1)]
    return [f"{a}-{b}", f"{c}-{d}"]


def scan_media(root: Path, *, include_blockout_frames: bool = False) -> list[MediaEntry]:
    """Все медиафайлы проекта с типом, категорией и ключом шота. Диск не пишется."""
    root = Path(root).resolve()
    hints = _Hints(root)
    out: list[MediaEntry] = []

    def add(path: Path, type_: str, category: str, *, kind: str | None = None, key: str | None = None,
            keys: tuple[str, ...] = (), scene: int | None = None, shot: int | None = None,
            page: int | None = None, phase: str | None = None, music_id: str | None = None,
            base: Path | None = None) -> None:
        kind = kind or _kind_of(path.suffix)
        try:
            st = path.stat()
            rel = path.relative_to(base or root).as_posix()
        except (OSError, ValueError):
            return
        out.append(MediaEntry(
            path=rel, name=path.name, kind=kind or "image", type=type_, category=category,
            key=key, keys=keys or ((key,) if key else ()), scene=scene, shot=shot, page=page,
            phase=phase, music_id=music_id, size=st.st_size, mtime_ns=st.st_mtime_ns,
            servable=path.suffix.lower() in MEDIA_EXTENSIONS,
        ))

    # обложки героев, ориентиры, локации
    for path, parts in _walk(root / "20_bible" / "references", root):
        kind = _kind_of(path.suffix)
        if kind != "image":
            continue
        folder = parts[0] if len(parts) > 1 else ""
        category = {"characters": "character", "landmarks": "landmark",
                    "background_references": "background"}.get(folder, "location")
        add(path, "reference", category)

    # референсы из общего каталога проекта, привязанные к этой серии
    from custom_tools.storybook.reference_catalog import bound_references
    project_root = root.parent.parent if root.parent.name == "series" else root
    try:
        bound = bound_references(root, None)
    except (OSError, ValueError):
        bound = []
    for record in bound:
        # серийный роут отдаёт только shared/references/, остальное дало бы битую миниатюру
        if not str(record["path"]).startswith("shared/references/"):
            continue
        path = project_root / record["path"]
        if not path.resolve().is_relative_to(project_root) or _kind_of(path.suffix) != "image":
            continue
        kinds = set(record.get("kinds") or ())
        category = ("character" if kinds & {"identity", "character", "appearance"}
                    else "landmark" if "landmark" in kinds
                    else "background" if "background" in kinds else "location")
        add(path, "reference", category, base=project_root)

    # иллюстрации книги
    for path, parts in _walk(root / "50_images", root):
        if _kind_of(path.suffix) != "image" or len(parts) < 2:
            continue
        match = _PAGE_RE.match(parts[0])
        add(path, "illustration", "book_page", page=int(match.group(1)) if match else None)

    # шоты
    shots_dir = root / "97_shots"
    for item, is_dir in _dir_items(shots_dir, root):
        name = item.name
        if not is_dir:
            kind = _kind_of(item.suffix)
            if kind == "image":
                add(item, "shot", "shot_other")
            elif kind == "video":
                add(item, "shot_video", "shot_video_other")
        elif name == "background_references":
            for path, _parts in _walk(item, root):
                if _kind_of(path.suffix) == "image":
                    add(path, "reference", "background")
        elif name == "video_units":
            for path, _parts in _walk(item, root, 0):
                if _kind_of(path.suffix) != "video":
                    continue
                match = _UNIT_RE.match(path.stem)
                if not match:
                    add(path, "shot_video", "shot_video_other")
                    continue
                keys = hints.unit_keys(path.name) or _unit_keys_from_name(match)
                first = re.match(r"^(\d+)-(\d+)$", keys[0])
                add(path, "shot_video", "video_unit_variant" if match.group(5) else "video_unit",
                    key=keys[0], keys=tuple(keys),
                    scene=int(first.group(1)) if first else None, shot=int(first.group(2)) if first else None)
        elif name.startswith("scene_"):
            both = _SCENE_SHOT_RE.match(name)
            only = _SCENE_ONLY_RE.match(name)
            scene = int(both.group(1)) if both else int(only.group(1)) if only else None
            shot = int(both.group(2)) if both else None
            key = f"{scene}-{shot}" if both else None
            for path, _parts in _walk(item, root, 0):
                kind = _kind_of(path.suffix)
                if kind == "image":
                    phase_match = _PHASE_RE.match(path.name)
                    phase = phase_match.group(1) if phase_match else None
                    category = {"start": "start_frame", "end": "end_frame"}.get(phase or "", "shot_other")
                    add(path, "shot", category, key=key, scene=scene, shot=shot, phase=phase)
                elif kind == "video":
                    category = "shot_clip" if path.name.startswith("video_final_") else "shot_video_other"
                    add(path, "shot_video", category, key=key, scene=scene, shot=shot)

    # болванка: заходим только в scene_* на один уровень, frames/ без флажка не листаем
    blockout = root / "93_blockout"
    for item, is_dir in _dir_items(blockout, root):
        if not is_dir:
            continue
        if item.name == "preview":
            for path, _parts in _walk(item, root, 0):
                kind = _kind_of(path.suffix)
                if kind == "image" and path.name.startswith("contact_sheet"):
                    add(path, "blockout_sheet", "blockout_sheet")
                elif kind == "video":
                    add(path, "blockout_preview", "blockout_preview")
        elif item.name.startswith("scene_"):
            both = _SCENE_SHOT_RE.match(item.name)
            only = _SCENE_ONLY_RE.match(item.name)
            scene = int(both.group(1)) if both else int(only.group(1)) if only else None
            shot = int(both.group(2)) if both else None
            key = f"{scene}-{shot}" if both else None
            for path, sub_is_dir in _dir_items(item, root):
                if sub_is_dir:
                    if path.name == "frames" and include_blockout_frames:
                        for frame, _parts in _walk(path, root, 0):
                            if frame.suffix.lower() == ".png":
                                add(frame, "blockout_frame", "blockout_frame", key=key, scene=scene, shot=shot)
                    continue
                kind = _kind_of(path.suffix)
                if kind == "image" and path.stem in ("ref_start", "ref_end"):
                    add(path, "blockout_ref", "blockout_ref", key=key, scene=scene, shot=shot,
                        phase="start" if path.stem == "ref_start" else "end")
                elif kind == "video" and path.stem == "blockout_ref":
                    add(path, "blockout_video", "blockout_video", key=key, scene=scene, shot=shot)

    # звук
    audio_dir = root / "98_audio"
    for item, is_dir in _dir_items(audio_dir, root):
        if is_dir and item.name in ("music", "sfx"):
            for path, _parts in _walk(item, root, 0):
                if _kind_of(path.suffix) != "audio":
                    continue
                if item.name == "music":
                    mid = hints.music_id(path.name) or path.stem
                    add(path, "music", "neutral" if mid == "neutral" else "leitmotif", music_id=mid)
                else:
                    add(path, "sfx", "scene_sfx", scene=hints.sfx_scene(path.name))
        elif not is_dir:
            kind = _kind_of(item.suffix)
            if kind == "subtitles":
                add(item, "subtitles", "planned")
            elif kind == "audio":
                if item.stem == "narration":
                    add(item, "narration", "narration")
                else:
                    add(item, "audio", "audio_other")

    # итоговые файлы
    for item, is_dir in _dir_items(root / "99_final", root):
        if is_dir:
            continue
        kind = _kind_of(item.suffix)
        if kind == "subtitles" and item.stem == "subtitles":
            add(item, "subtitles", "final_subs")
        elif kind == "video":
            if item.stem == "photo_video":
                add(item, "photo_video", "photo")
            else:
                add(item, "final_video", "final")
    return out


# --- кэш длительности --------------------------------------------------------------------

_INDEX_NAME = "media_index.json"
_INDEX_LOCK = threading.Lock()
_WARNED_WRITE: set[str] = set()


def _studio_dir_ok(root: Path) -> bool:
    """.studio должна лежать внутри проекта (не симлинк наружу)."""
    try:
        (root / ".studio").resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_index_key(key: str) -> bool:
    return not (key.startswith("/") or "\\" in key or ".." in key.split("/"))


def _load_index(root: Path) -> dict[str, dict]:
    if not _studio_dir_ok(root):
        return {}
    try:
        data = json.loads((root / ".studio" / _INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), dict):
        return {}
    return {k: v for k, v in data["entries"].items() if isinstance(k, str) and _safe_index_key(k) and isinstance(v, dict)}


def _write_index(root: Path, new: dict[str, dict]) -> None:
    tmp: Path | None = None
    if not _studio_dir_ok(root):
        return
    with _INDEX_LOCK:
        try:
            merged = _load_index(root)
            merged.update(new)
            merged = {k: v for k, v in merged.items() if (root / k).is_file()}
            folder = root / ".studio"
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f"{_INDEX_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps({"version": 1, "entries": merged}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, folder / _INDEX_NAME)
        except OSError as exc:
            if str(root) not in _WARNED_WRITE:
                _WARNED_WRITE.add(str(root))
                logger.warning("Не удалось записать индекс длительности медиа в %s: %s", root, exc)
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass


_NO_TOOL = object()  # ни один инструмент не нашёлся: это не провал файла, кэшировать нельзя


def _probe_cv2(path: Path):
    try:
        import cv2
    except ImportError:
        return _NO_TOOL
    try:
        # Ограничение: у cv2.VideoCapture нет таймаута, на зависшем сетевом диске вызов может блокироваться
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        finally:
            cap.release()
        if fps and fps > 0 and frames and frames > 0:
            return round(frames / fps, 2)
    except Exception:  # битый файл не должен ронять запрос
        return None
    return None


def _probe_ffprobe(path: Path):
    exe = shutil.which("ffprobe")
    if not exe:
        return _NO_TOOL
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        value = float(proc.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return round(value, 2) if value > 0 else None


def _probe_duration(path: Path, kind: str) -> float | None:
    """Единственное место, где открываются медиафайлы (тесты подменяют эту функцию)."""
    if kind == "video":
        first = _probe_cv2(path)
        if first is not _NO_TOOL and first:
            return first
        second = _probe_ffprobe(path)
        return _NO_TOOL if first is _NO_TOOL and second is _NO_TOOL else (None if second is _NO_TOOL else second)
    if path.suffix.lower() == ".wav":
        import wave
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                if rate > 0:
                    return round(handle.getnframes() / rate, 2) or None
        except (OSError, EOFError, wave.Error):
            pass
    return _probe_ffprobe(path)


class DurationCache:
    """Длительности через индекс проекта; не больше probe_limit новых открытий файлов."""

    def __init__(self, root: Path, probe_limit: int = 40):
        self.root = Path(root)
        self.limit = max(0, int(probe_limit))
        self.probed = 0
        self.pending = 0
        self._entries = _load_index(self.root)
        self._new: dict[str, dict] = {}

    def get(self, rel: str, kind: str) -> float | None:
        try:
            st = (self.root / rel).stat()
        except OSError:
            return None
        rec = self._entries.get(rel)
        if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns:
            value = rec.get("duration_s")
            return float(value) if isinstance(value, (int, float)) and not rec.get("failed") else None
        if self.probed >= self.limit:
            self.pending += 1
            return None
        self.probed += 1
        value = _probe_duration(self.root / rel, kind)
        if value is _NO_TOOL:
            return None
        rec = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "duration_s": value, "failed": value is None}
        self._entries[rel] = rec
        self._new[rel] = rec
        return value

    def flush(self) -> None:
        if self._new:
            _write_index(self.root, self._new)
            self._new = {}


def video_duration(root: Path, rel_path: str, *, probe: bool = True) -> float | None:
    """Длительность одного файла через общий индекс (probe=False: только кэш)."""
    root = Path(root).resolve()
    suffix = Path(rel_path).suffix
    kind = _kind_of(suffix) or "video"
    cache = DurationCache(root, probe_limit=1_000_000 if probe else 0)
    try:
        return cache.get(rel_path, kind)
    finally:
        cache.flush()


# --- разбор SRT --------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*$")


def srt_timecode(seconds: float) -> str:
    total = max(0, int(round(seconds * 1000)))
    h, rest = divmod(total, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_seconds(h: str, m: str, s: str, frac: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def parse_srt(text: str) -> tuple[list[dict], int]:
    """(реплики по времени, число пропущенных блоков). Текст реплик не меняем."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[dict] = []
    skipped = 0
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.strip("\n").split("\n")
        if not "".join(lines).strip():
            continue
        source_index: int | None = None
        pos = 0
        if re.fullmatch(r"\d+", lines[0].strip()) and len(lines) > 1:
            source_index = int(lines[0].strip())
            pos = 1
        match = _TIME_RE.match(lines[pos])
        if not match:
            skipped += 1
            continue
        g = match.groups()
        start, end = _srt_seconds(*g[0:4]), _srt_seconds(*g[4:8])
        if end <= start:
            skipped += 1
            continue
        cues.append({"source_index": source_index, "start_s": start, "end_s": end,
                     "text": "\n".join(lines[pos + 1:]).strip("\n")})
    cues.sort(key=lambda c: (c["start_s"], c["end_s"]))
    for number, cue in enumerate(cues, 1):
        cue["index"] = number
        cue["start_tc"] = srt_timecode(cue["start_s"])
        cue["end_tc"] = srt_timecode(cue["end_s"])
    return cues, skipped


# --- возможности ------------------------------------------------------------------------


def _iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).isoformat()


def _item(entry: MediaEntry, duration: float | None) -> dict:
    return {
        "path": entry.path, "name": entry.name, "kind": entry.kind,
        "type": entry.type, "type_title": media_type_title(entry.type),
        "category": entry.category, "category_title": media_category_title(entry.category),
        "key": entry.key, "keys": list(entry.keys), "scene": entry.scene, "shot": entry.shot,
        "page": entry.page, "phase": entry.phase, "music_id": entry.music_id,
        "size": entry.size, "modified": _iso(entry.mtime_ns), "modified_ts": entry.mtime_ns / 1e9,
        "duration_s": duration, "servable": entry.servable,
    }


def _scene_shot_sort_key(e: MediaEntry):
    inf = float("inf")
    order = _TYPE_ORDER.index(e.type) if e.type in _TYPE_ORDER else len(_TYPE_ORDER)
    return (e.scene if e.scene is not None else inf, e.shot if e.shot is not None else inf,
            order, e.phase or "", e.name, e.path)


def _check_list(value: object, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise StudioError("invalid_argument", f"Параметр {name} должен быть списком строк")
    return list(value)


@capability("media_inventory", title="Список медиафайлов", access="read",
            category="media", supersedes=("media_inventory",))
def media_inventory(
    project_id: str,
    types: list | None = None,
    categories: list | None = None,
    kinds: list | None = None,
    scene: int | None = None,
    key: str | None = None,
    search: str = "",
    include_blockout_frames: bool = False,
    sort_by: str = "scene_shot",
    descending: bool = False,
    offset: int = 0,
    limit: int = 500,
    probe_limit: int = 40,
) -> dict:
    types_f, cats_f, kinds_f = _check_list(types, "types"), _check_list(categories, "categories"), _check_list(kinds, "kinds")
    for value in types_f or []:
        if value not in MEDIA_TYPE_TITLES:
            raise StudioError("invalid_argument", f"Неизвестный тип медиа: {value}")
    for value in kinds_f or []:
        if value not in KINDS:
            raise StudioError("invalid_argument", f"Неизвестный вид файла: {value}")
    if sort_by not in _SORT_FIELDS:
        raise StudioError("invalid_argument", f"Неизвестная сортировка: {sort_by}")
    for name, value in (("limit", limit), ("offset", offset), ("probe_limit", probe_limit)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise StudioError("invalid_argument", f"Параметр {name} должен быть целым числом")
    if not 1 <= limit <= 2000:
        raise StudioError("invalid_argument", "limit должен быть от 1 до 2000")
    if offset < 0 or probe_limit < 0:
        raise StudioError("invalid_argument", "offset и probe_limit не могут быть отрицательными")
    if not isinstance(search, str):
        raise StudioError("invalid_argument", "search должен быть строкой")

    root = project_dir(project_id).resolve()
    entries = scan_media(root, include_blockout_frames=bool(include_blockout_frames))
    counts = {"types": {}, "categories": {}, "kinds": {}}
    for e in entries:
        for field, value in (("types", e.type), ("categories", e.category), ("kinds", e.kind)):
            counts[field][value] = counts[field].get(value, 0) + 1

    needle = search.strip().lower()
    matched = [
        e for e in entries
        if (not types_f or e.type in types_f) and (not cats_f or e.category in cats_f)
        and (not kinds_f or e.kind in kinds_f) and (scene is None or e.scene == scene)
        and (key is None or e.key == key or key in e.keys)
        and (not needle or needle in e.path.lower())
    ]

    cache = DurationCache(root, probe_limit)
    try:
        durations: dict[str, float | None] = {}

        def duration(e: MediaEntry) -> float | None:
            if e.kind not in ("video", "audio"):
                return None
            if e.path not in durations:
                durations[e.path] = cache.get(e.path, e.kind)
            return durations[e.path]

        if sort_by == "scene_shot":
            matched.sort(key=_scene_shot_sort_key, reverse=descending)
        elif sort_by == "duration":
            known = [e for e in matched if duration(e) is not None]
            unknown = [e for e in matched if durations.get(e.path) is None]
            known.sort(key=lambda e: (durations[e.path], e.path), reverse=descending)
            matched = known + sorted(unknown, key=lambda e: e.path)
        else:
            sort_key = {
                "name": lambda e: (e.name.lower(), e.path),
                "modified": lambda e: (e.mtime_ns, e.path),
                "size": lambda e: (e.size, e.path),
                "type": lambda e: (e.type, e.category, e.path),
            }[sort_by]
            matched.sort(key=sort_key, reverse=descending)

        page = matched[offset:offset + limit]
        items = [_item(e, duration(e)) for e in page]
        return success(
            project_id=project_id, total=len(entries), matched=len(matched), offset=offset, limit=limit,
            durations_pending=cache.pending, counts=counts, labels=media_labels(), items=items,
        )
    finally:
        cache.flush()


@capability("media_timeline", title="Таймлайн фильма", access="read", category="media")
def media_timeline(project_id: str, probe_limit: int = 20) -> dict:
    if isinstance(probe_limit, bool) or not isinstance(probe_limit, int) or probe_limit < 0:
        raise StudioError("invalid_argument", "probe_limit должен быть целым числом от 0")
    from . import media_timeline as impl  # отложенный импорт: модуль зависит от этого

    return success(project_id=project_id, **impl.build_timeline(project_dir(project_id).resolve(), project_id, probe_limit))


@capability("media_subtitles", title="Субтитры фильма", access="read", category="media")
def media_subtitles(project_id: str, source: str = "auto") -> dict:
    if source not in ("auto", "final", "planned"):
        raise StudioError("invalid_argument", "source: auto, final или planned")
    from . import media_timeline as impl

    return success(project_id=project_id, **impl.build_subtitles(project_dir(project_id).resolve(), source))
