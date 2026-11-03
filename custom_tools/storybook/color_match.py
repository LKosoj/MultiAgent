"""Подгонка цвета клипов сцены к эталонному кадру перед монтажом и общее плёночное зерно."""
import hashlib
import logging
import os
import subprocess
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_VERSION = 1
# Мягкие пределы: сцена может быть нарочно темнее или теплее, правим только скачки между планами.
_GAIN_LIMITS = (0.8, 1.25)
_SHIFT_LIMIT = 24.0
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
FILM_GRAIN_FILTER = "noise=alls=5:allf=t"


def _frame_stats(ffmpeg_bin: str, path: str, at: float) -> List[tuple]:
    """Среднее и разброс каждого канала RGB для уменьшенного кадра клипа."""
    completed = subprocess.run(
        [ffmpeg_bin, "-v", "error", "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1",
         "-vf", "scale=96:54,format=rgb24", "-f", "rawvideo", "pipe:1"],
        capture_output=True, timeout=120,
    )
    data = completed.stdout
    if completed.returncode or not data or len(data) % 3:
        raise RuntimeError(completed.stderr.decode(errors="replace")[-300:] or "no frame decoded")
    return [(fmean(channel), pstdev(channel)) for channel in (data[0::3], data[1::3], data[2::3])]


def _channel_correction(stats: tuple, reference: tuple) -> tuple:
    (mean, spread), (ref_mean, ref_spread) = stats, reference
    gain = min(max(ref_spread / max(spread, 1.0), _GAIN_LIMITS[0]), _GAIN_LIMITS[1])
    target = mean + min(max(ref_mean - mean, -_SHIFT_LIMIT), _SHIFT_LIMIT)
    return gain, target - gain * mean


def color_match_filter(stats: List[tuple], reference: List[tuple]) -> Optional[str]:
    """Фильтр ffmpeg, переводящий цвет кадра к эталону; None, если правка не нужна."""
    corrections = [_channel_correction(s, r) for s, r in zip(stats, reference)]
    if all(abs(gain - 1) < 0.01 and abs(offset) < 0.5 for gain, offset in corrections):
        return None
    channels = ":".join(f"{name}='clip(val*{gain:.4f}+{offset:.2f},0,255)'"
                        for name, (gain, offset) in zip("rgb", corrections))
    return f"lutrgb={channels}"


def _sample_time(clip: Dict[str, Any]) -> float:
    duration = clip.get("actual_duration_seconds") or clip.get("planned_duration_seconds") or 1.0
    return max(0.0, float(duration) / 2)


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_matched(ffmpeg_bin: str, source: str, video_filter: str, out_dir: Path) -> str:
    key = hashlib.sha256(f"{_VERSION}|{_file_digest(source)}|{video_filter}".encode()).hexdigest()[:20]
    target = out_dir / f"{key}{Path(source).suffix.lower() or '.mp4'}"
    if target.is_file() and target.stat().st_size > 0:
        return str(target)
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.tmp-{os.getpid()}{target.suffix}")
    completed = subprocess.run(
        [ffmpeg_bin, "-v", "error", "-y", "-i", source, "-map", "0:v:0", "-map", "0:a?",
         "-vf", f"{video_filter},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-c:a", "copy", str(temporary)],
        capture_output=True, timeout=3600,
    )
    if completed.returncode or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise RuntimeError(completed.stderr.decode(errors="replace")[-300:])
    os.replace(temporary, target)
    return str(target)


def match_scene_colors(ffmpeg_bin: str, clips: List[Dict[str, Any]], out_dir: Path,
                       warnings: Optional[List[str]] = None) -> Dict[str, str]:
    """Подгоняет цвет каждого клипа к середине первого клипа его сцены.

    Возвращает {исходный путь: путь подогнанной копии}; клипы без правки не попадают в словарь.
    """
    mapping: Dict[str, str] = {}
    references: Dict[Any, List[tuple]] = {}
    for clip in clips:
        path = str(clip.get("path") or "")
        if not clip.get("exists", True) or Path(path).suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        try:
            stats = _frame_stats(ffmpeg_bin, path, _sample_time(clip))
            scene = clip.get("scene_number")
            if scene not in references:
                references[scene] = stats
                continue
            video_filter = color_match_filter(stats, references[scene])
            if video_filter:
                mapping[str(Path(path).resolve())] = _render_matched(ffmpeg_bin, path, video_filter, out_dir)
        except Exception as exc:
            message = f"color_match_skipped:{Path(path).name}:{str(exc).strip()[:200]}"
            logger.warning(message)
            if warnings is not None:
                warnings.append(message)
    return mapping
