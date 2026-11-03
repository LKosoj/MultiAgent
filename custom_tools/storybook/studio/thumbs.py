"""Миниатюры картинок и видео для медиа-маршрута."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .errors import StudioError
from .paths import resolve_in_project, thumbs_dir

logger = logging.getLogger(__name__)

THUMB_SIZES = (128, 256, 512)
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_EXT = {".mp4", ".webm", ".mov"}

try:  # cv2 необязателен: без него падаем на ffmpeg
    import cv2  # type: ignore
except Exception:  # pragma: no cover - зависит от окружения
    cv2 = None  # type: ignore


def _to_jpeg_bytes(image, size: int) -> bytes:
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image)
    image.thumbnail((size, size))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def placeholder_jpeg(label: str, size: int = 256) -> bytes:
    """Серый квадрат с подписью расширения; на диск не кэшируется."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size, size), (200, 200, 200))
    ImageDraw.Draw(image).text((size // 8, size // 2 - 5), (label or "file").upper(), fill=(90, 90, 90))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def _image_thumb(path: Path, size: int) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            return _to_jpeg_bytes(image, size)
    except Exception:
        return None


def _video_frame_cv2(path: Path):
    if cv2 is None:
        return None
    try:
        from PIL import Image

        capture = cv2.VideoCapture(str(path))
        try:
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total > 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.1))
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            return None
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    except Exception:
        return None


def _video_frame_ffmpeg(path: Path, size: int) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", "0.5", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={size}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except Exception:
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def _video_thumb(path: Path, size: int) -> bytes | None:
    frame = _video_frame_cv2(path)
    if frame is not None:
        try:
            return _to_jpeg_bytes(frame, size)
        except Exception:
            pass
    return _video_frame_ffmpeg(path, size)


def make_thumb(project_id: str, rel_path: str, size: int = 256) -> tuple[Path | bytes, bool]:
    """Вернуть (путь к jpeg в кэше, False) либо (байты заглушки, True)."""
    if size not in THUMB_SIZES:
        raise StudioError("invalid_argument", "Недопустимый размер миниатюры")
    source = resolve_in_project(project_id, rel_path)
    ext = source.suffix.lower()
    if ext not in _IMAGE_EXT and ext not in _VIDEO_EXT:
        return placeholder_jpeg(ext.lstrip("."), size), True

    prefix = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]
    cache_dir = thumbs_dir(project_id)
    cached = cache_dir / f"{prefix}_{size}_{source.stat().st_mtime_ns}.jpg"
    if cached.is_file():
        return cached, False

    data = _image_thumb(source, size) if ext in _IMAGE_EXT else _video_thumb(source, size)
    if data is None:
        return placeholder_jpeg(ext.lstrip("."), size), True

    cache_dir = thumbs_dir(project_id, create=True)
    # uuid: параллельные запросы одного процесса не должны делить временный файл
    tmp = cache_dir / f".{cached.name}.{uuid.uuid4().hex}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, cached)
    # старые версии этого файла с тем же размером больше не нужны
    for old in cache_dir.glob(f"{prefix}_{size}_*.jpg"):
        if old != cached:
            try:
                old.unlink()
            except OSError:
                pass
    return cached, False
