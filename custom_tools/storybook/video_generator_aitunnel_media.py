"""Media and model-size helpers for AITunnel storybook video generation."""

from __future__ import annotations

import base64
import mimetypes
from math import gcd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image


_ASPECT_RATIO_BY_REDUCED_PAIR = {
    (1, 1): "1:1",
    (3, 4): "3:4",
    (4, 3): "4:3",
    (9, 16): "9:16",
    (16, 9): "16:9",
    (9, 21): "9:21",
    (21, 9): "21:9",
}
_RESOLUTION_BY_SHORT_EDGE = {
    480: "480p",
    720: "720p",
    1080: "1080p",
    2160: "4K",
}


def _resolve_frame_dimensions(item: Dict[str, Any], start_image: str) -> Tuple[int, int]:
    try:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0

    if width > 0 and height > 0:
        return width, height

    if start_image and not _is_url_or_data_url(start_image):
        with Image.open(start_image) as image:
            return image.size

    raise ValueError("Не удалось определить размеры кадра: отсутствуют width/height и локальный start_image")


def _resolve_model_and_size(
    model_catalog: Dict[str, Dict[str, Any]],
    configured_model: str,
    width: int,
    height: int,
    duration: int,
    requires_last_frame: bool,
    seed: Optional[int],
) -> Tuple[str, Dict[str, str], int]:
    config = model_catalog.get(configured_model)
    if not isinstance(config, dict):
        raise ValueError(f"Модель AITUNNEL не найдена: {configured_model}")

    supported_frame_images = set(config.get("supported_frame_images") or [])
    if "first_frame" not in supported_frame_images:
        raise ValueError(f"Модель {configured_model} не поддерживает first_frame")
    if requires_last_frame and "last_frame" not in supported_frame_images:
        raise ValueError(f"Модель {configured_model} не поддерживает last_frame")
    if seed is not None and not config.get("supports_seed"):
        raise ValueError(f"Модель {configured_model} не поддерживает seed")

    resolved_duration = _select_best_supported_duration(duration, config.get("supported_durations") or [])
    size_params = _resolve_size_params(config, width, height)
    if size_params is None:
        raise ValueError(
            f"Модель {configured_model} не поддерживает подходящее разрешение для {width}x{height}"
        )

    return configured_model, size_params, resolved_duration


def _resolve_size_params(
    model_config: Dict[str, Any],
    width: int,
    height: int,
) -> Optional[Dict[str, str]]:
    size = f"{width}x{height}"
    resolution = _infer_resolution(width, height)
    aspect_ratio = _infer_aspect_ratio(width, height)

    supported_sizes = set(model_config.get("supported_sizes") or [])
    if size in supported_sizes:
        return {"size": size}

    best_size = _select_best_supported_size(width, height, supported_sizes)
    if best_size:
        return {"size": best_size}

    supported_resolutions = set(model_config.get("supported_resolutions") or [])
    supported_aspect_ratios = set(model_config.get("supported_aspect_ratios") or [])
    if aspect_ratio in supported_aspect_ratios:
        best_resolution = _select_best_supported_resolution(resolution, supported_resolutions)
        if best_resolution:
            return {"resolution": best_resolution, "aspect_ratio": aspect_ratio}

    return None


def _infer_resolution(width: int, height: int) -> Optional[str]:
    return _RESOLUTION_BY_SHORT_EDGE.get(min(width, height))


def _infer_aspect_ratio(width: int, height: int) -> Optional[str]:
    divisor = gcd(width, height)
    reduced = (width // divisor, height // divisor)
    return _ASPECT_RATIO_BY_REDUCED_PAIR.get(reduced)


def _select_best_supported_duration(requested_duration: int, supported_durations: List[int]) -> int:
    if not supported_durations:
        raise ValueError("Для выбранной модели не опубликован список supported_durations")

    candidates = sorted({int(value) for value in supported_durations})
    return min(
        candidates,
        key=lambda value: (abs(value - requested_duration), value < requested_duration, value),
    )


def _select_best_supported_size(
    requested_width: int,
    requested_height: int,
    supported_sizes: set,
) -> Optional[str]:
    requested_aspect_ratio = _infer_aspect_ratio(requested_width, requested_height)
    if not requested_aspect_ratio or not supported_sizes:
        return None

    requested_area = requested_width * requested_height
    candidates: List[Tuple[str, int]] = []
    for size in supported_sizes:
        parsed = _parse_size(size)
        if not parsed:
            continue
        width, height = parsed
        if _infer_aspect_ratio(width, height) != requested_aspect_ratio:
            continue
        area = width * height
        candidates.append((size, area))

    if not candidates:
        return None

    best_size, _ = min(
        candidates,
        key=lambda candidate: (
            abs(candidate[1] - requested_area),
            candidate[1] < requested_area,
            -candidate[1],
        ),
    )
    return best_size


def _select_best_supported_resolution(
    requested_resolution: Optional[str],
    supported_resolutions: set,
) -> Optional[str]:
    normalized_supported = [value for value in supported_resolutions if value in _RESOLUTION_BY_SHORT_EDGE.values()]
    if not normalized_supported:
        return None

    if requested_resolution is None:
        return max(normalized_supported, key=lambda value: _resolution_rank(value))

    return min(
        normalized_supported,
        key=lambda value: (
            abs(_resolution_rank(value) - _resolution_rank(requested_resolution)),
            _resolution_rank(value) < _resolution_rank(requested_resolution),
            -_resolution_rank(value),
        ),
    )


def _parse_size(size: str) -> Optional[Tuple[int, int]]:
    if "x" not in size:
        return None
    try:
        width_str, height_str = size.lower().split("x", 1)
        return int(width_str), int(height_str)
    except (TypeError, ValueError):
        return None


def _resolution_rank(value: str) -> int:
    for short_edge, label in _RESOLUTION_BY_SHORT_EDGE.items():
        if label == value:
            return short_edge
    return 0


def _build_frame_image_payload(image_ref: str, frame_type: str) -> Dict[str, Any]:
    image_url = _normalize_image_reference(image_ref)
    return {
        "type": "image_url",
        "image_url": {"url": image_url},
        "frame_type": frame_type,
    }


def _normalize_image_reference(image_ref: str) -> str:
    if not image_ref:
        raise ValueError("Пустой image reference")

    if _is_url_or_data_url(image_ref):
        return image_ref

    path = Path(image_ref)
    if not path.exists():
        raise FileNotFoundError(f"Файл изображения не найден: {image_ref}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        mime_type = "image/png"

    with path.open("rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def _is_url_or_data_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://") or value.startswith("data:")
