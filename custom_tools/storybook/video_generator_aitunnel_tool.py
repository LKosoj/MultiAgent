import base64
import json
import logging
import mimetypes
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import gcd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image

from custom_tools.storybook.video_generator_common import (
    sync_items_to_memory,
    update_shots_with_descriptions,
)

try:
    from utils import translate_prompts_in_items
except ImportError:
    translate_prompts_in_items = None


logger = logging.getLogger(__name__)

_AITUNNEL_MODELS_URL = "https://api.aitunnel.ru/public/aitunnel/models/videos"
_DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_WAIT_SECONDS = 900
_DEFAULT_POLL_INTERVAL_SECONDS = 15
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
_AITUNNEL_MODELS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def load_env_file() -> None:
    """Загружает переменные окружения из корневого .env файла проекта."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value


load_env_file()


def video_generator_aitunnel_tool(
    session_id: str,
    project_id: Optional[str] = None,
    items: Any = None,
    max_concurrency: int = 2,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = "en",
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False,
) -> Dict[str, Any]:
    """
    Генерирует видео через AITUNNEL, сохраняя контракт storybook video tools.

    Совместимость по входу:
    - При наличии project_id читает plots/storybooks/{project_id}/97_shots/shots.json
    - При отсутствии project_id может работать напрямую от items/items JSON string
    - Поддерживает start/end кадры через frame_images[first_frame/last_frame]

    Args:
        session_id: Идентификатор сессии для трассировки выполнения и синхронизации с памятью.
        project_id: Идентификатор проекта storybook; при задании читается shots.json из каталога проекта.
        items: Список шотов или JSON-строка с items, если нет shots.json или нужна явная передача данных.
        max_concurrency: Максимальное число параллельных запросов к API.
        enable: Если True, выполняет генерацию; иначе пропускает фактические вызовы API.
        seed: Опциональный seed для воспроизводимости генерации.
        language: Язык промптов/перевода (например, en).
        force_update_prompts: Принудительно обновить video_prompt независимо от timestamp.
        skip_prompt_enhancement: Пропустить улучшение промпта (только перевод и т.п.).

    Returns:
        Словарь со статусом, сообщением и списком results по шотам.
    """
    api_key = os.getenv("AITUNNEL_API_KEY")
    if not api_key:
        logger.error("❌ AITUNNEL_API_KEY не найден в переменных окружения")
        return {"status": "error", "message": "AITUNNEL_API_KEY не найден", "results": []}
    configured_model = (os.getenv("AITUNNEL_VIDEO_MODEL") or "").strip()
    if not configured_model:
        logger.error("❌ AITUNNEL_VIDEO_MODEL не задан в переменных окружения")
        return {"status": "error", "message": "AITUNNEL_VIDEO_MODEL не задан", "results": []}

    shots_file_path: Optional[str] = None
    items_list: List[Dict[str, Any]] = []

    if project_id:
        shots_file_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
        if os.path.exists(shots_file_path):
            try:
                with open(shots_file_path, "r", encoding="utf-8") as shots_file:
                    shots_data = json.load(shots_file)
                if isinstance(shots_data, dict):
                    items_list = shots_data.get("items", [])
                elif isinstance(shots_data, list):
                    items_list = shots_data
                else:
                    logger.error("❌ Неверная структура данных в shots.json")
                    return {"status": "error", "message": "Неверная структура данных в shots.json", "results": []}
            except Exception as exc:
                logger.error("❌ Ошибка чтения shots.json: %s", exc)
                return {"status": "error", "message": f"Ошибка чтения shots.json: {exc}", "results": []}
        elif items is None:
            logger.warning("⚠️ Файл shots.json не найден: %s", shots_file_path)
            return {
                "status": "error",
                "message": f"Файл shots.json не найден: {shots_file_path}",
                "results": [],
            }

    if not items_list and items is not None:
        parsed_items, parse_error = _parse_items_payload(items)
        if parse_error:
            logger.error("❌ Ошибка парсинга items: %s", parse_error)
            return {"status": "error", "message": parse_error, "results": []}
        items_list = parsed_items

    if not items_list:
        logger.warning("⚠️ Список items пуст")
        return {"status": "error", "message": "Список items пуст", "results": []}

    if shots_file_path and project_id:
        logger.info("📝 Этап 1: Анализ и обновление описаний изображений")
        descriptions_updated = update_shots_with_descriptions(
            shots_file_path,
            items_list,
            force_update=force_update_prompts,
            skip_prompt_enhancement=skip_prompt_enhancement,
        )
        if descriptions_updated:
            logger.info("🔄 Описания обновлены, перезагружаем данные из shots.json")
            try:
                with open(shots_file_path, "r", encoding="utf-8") as shots_file:
                    shots_data = json.load(shots_file)
                items_list = shots_data.get("items", []) if isinstance(shots_data, dict) else shots_data
            except Exception as exc:
                logger.error("❌ Ошибка перезагрузки shots.json после обновления описаний: %s", exc)
                return {
                    "status": "error",
                    "message": f"Ошибка перезагрузки shots.json: {exc}",
                    "results": [],
                }

    if not enable:
        logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
        return {
            "status": "skipped",
            "message": "Генерация видео отключена, анализ изображений выполнен",
            "results": [],
        }

    try:
        model_catalog = _get_aitunnel_video_models()
    except Exception as exc:
        logger.error("❌ Не удалось получить capabilities AITUNNEL: %s", exc)
        return {
            "status": "error",
            "message": f"Не удалось получить список моделей AITUNNEL: {exc}",
            "results": [],
        }

    video_items = _collect_video_items(items_list)
    if not video_items:
        logger.info("ℹ️ Нет кадров для генерации видео AITUNNEL")
        return {"status": "error", "message": "Нет кадров для обработки", "results": []}

    results: List[Dict[str, Any]] = []
    worker_count = max(1, int(max_concurrency or 1))

    for batch_start in range(0, len(video_items), worker_count):
        batch_items = video_items[batch_start:batch_start + worker_count]
        logger.info(
            "🎬 AITUNNEL: пакет %s, видео %s",
            batch_start // worker_count + 1,
            len(batch_items),
        )
        with ThreadPoolExecutor(max_workers=len(batch_items)) as executor:
            future_to_item = {
                executor.submit(
                    _generate_single_video_aitunnel,
                    item,
                    session_id,
                    api_key,
                    model_catalog,
                    configured_model,
                    seed,
                    language,
                ): item
                for item in batch_items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error("❌ Исключение при генерации видео AITUNNEL: %s", exc)
                    result = {
                        "success": False,
                        "error": str(exc),
                        "scene_number": item.get("scene_number"),
                        "shot_number": item.get("shot_number"),
                        "video_path": item.get("video_path"),
                    }
                results.append(result)

    successful = len([result for result in results if result.get("success")])
    total = len(results)

    sync_items_to_memory(items, items_list)

    return {
        "status": "success" if successful == total else "error",
        "message": f"Сгенерировано {successful} из {total} видео AITUNNEL",
        "results": results,
        "stats": {
            "total": total,
            "successful": successful,
            "failed": total - successful,
        },
    }


def _parse_items_payload(items: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if isinstance(items, str):
        try:
            items_obj = json.loads(items)
        except Exception as exc:
            return [], f"Невалидный JSON: {exc}"
    else:
        items_obj = items

    if isinstance(items_obj, dict) and isinstance(items_obj.get("items"), list):
        return items_obj["items"], None
    if isinstance(items_obj, list):
        return items_obj, None
    return [], "Неверная структура данных items"


def _collect_video_items(items_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    video_items: List[Dict[str, Any]] = []
    seen_keys = set()

    for item in items_list:
        shot_type = item.get("shot_type")
        if shot_type and shot_type != "start":
            continue

        video_prompt = str(item.get("video_prompt", "") or "").strip()
        video_path = item.get("video_path")
        scene_number = item.get("scene_number", "?")
        shot_number = item.get("shot_number", "?")

        if not video_prompt or not video_path:
            logger.debug("⏭️ Пропускаем кадр без video_prompt/video_path: %s-%s", scene_number, shot_number)
            continue

        if os.path.exists(video_path):
            logger.info("✅ Видео уже существует: %s", video_path)
            continue

        start_image = item.get("start_image")
        end_image = item.get("end_image")
        video_dir = os.path.dirname(video_path)

        if start_image and not _is_url_or_data_url(start_image) and not os.path.exists(start_image):
            start_image = None
        if end_image and not _is_url_or_data_url(end_image) and not os.path.exists(end_image):
            end_image = None

        if not start_image:
            start_image = _discover_shot_image(video_dir, scene_number, shot_number, "start")
        if not end_image:
            end_image = _discover_shot_image(video_dir, scene_number, shot_number, "end")

        if not start_image:
            logger.debug("⏭️ Стартовый кадр не найден для %s-%s", scene_number, shot_number)
            continue

        shot_key = f"{scene_number}-{shot_number}"
        if shot_key in seen_keys:
            logger.debug("⏭️ Пропускаем дубликат кадра: %s", shot_key)
            continue

        item_copy = item.copy()
        item_copy["start_image"] = start_image
        item_copy["end_image"] = end_image
        seen_keys.add(shot_key)
        video_items.append(item_copy)

    return video_items


def _discover_shot_image(
    video_dir: str,
    scene_number: Any,
    shot_number: Any,
    shot_type: str,
) -> Optional[str]:
    if not video_dir or not os.path.exists(video_dir):
        return None

    try:
        scene_num = int(scene_number) if scene_number != "?" else 1
        shot_num = int(shot_number) if shot_number != "?" else 1
    except (TypeError, ValueError):
        scene_num = 1
        shot_num = 1

    filename = f"img_final_{shot_type}_{scene_num:02d}_{shot_num:02d}.png"
    candidate = os.path.join(video_dir, filename)
    return candidate if os.path.exists(candidate) else None


def _generate_single_video_aitunnel(
    item: Dict[str, Any],
    session_id: str,
    api_key: str,
    model_catalog: Dict[str, Dict[str, Any]],
    configured_model: str,
    seed: Optional[int],
    language: str,
) -> Dict[str, Any]:
    del session_id

    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    video_path = item.get("video_path")
    start_image = item.get("start_image")
    end_image = item.get("end_image")

    try:
        prompt = _resolve_video_prompt(item, language)
        if not prompt:
            raise ValueError("Пустой video_prompt")

        width, height = _resolve_frame_dimensions(item, start_image)
        requested_duration = _parse_duration_from_timing(item.get("timing", "00:00 - 00:06"))
        model_name, size_params, duration = _resolve_model_and_size(
            model_catalog=model_catalog,
            configured_model=configured_model,
            width=width,
            height=height,
            duration=requested_duration,
            requires_last_frame=bool(end_image),
            seed=seed,
        )

        frame_images = [_build_frame_image_payload(start_image, "first_frame")]
        if end_image:
            frame_images.append(_build_frame_image_payload(end_image, "last_frame"))

        payload: Dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "duration": duration,
            "frame_images": frame_images,
            "generate_audio": False,
        }
        payload.update(size_params)
        if seed is not None:
            payload["seed"] = seed

        video_dir = os.path.dirname(video_path)
        if video_dir:
            os.makedirs(video_dir, exist_ok=True)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        base_url = os.getenv("AITUNNEL_API_BASE", _DEFAULT_BASE_URL).rstrip("/")

        submit_response = requests.post(
            f"{base_url}/videos",
            headers=headers,
            json=payload,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        if submit_response.status_code not in (200, 202):
            raise RuntimeError(
                f"AITUNNEL submit failed: {submit_response.status_code} - {submit_response.text}"
            )

        submit_payload = submit_response.json()
        task_id = submit_payload.get("id")
        if not task_id:
            raise RuntimeError(f"Не получен id задачи от AITUNNEL: {submit_payload}")

        status_payload = _wait_for_video_completion_aitunnel(
            task_id=task_id,
            headers=headers,
            base_url=base_url,
        )
        unsigned_urls = status_payload.get("unsigned_urls") or []
        video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"

        _download_video_aitunnel(video_url, video_path, headers)

        return {
            "success": True,
            "video_path": video_path,
            "scene_number": scene_number,
            "shot_number": shot_number,
            "task_id": task_id,
            "video_url": video_url,
            "error": None,
            "model": model_name,
            "cost_rub": ((status_payload.get("usage") or {}).get("cost_rub")),
        }
    except Exception as exc:
        logger.error("❌ AITUNNEL: ошибка генерации %s-%s: %s", scene_number, shot_number, exc)
        return {
            "success": False,
            "error": str(exc),
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path,
        }


def _resolve_video_prompt(item: Dict[str, Any], language: str) -> str:
    if language == "en" or translate_prompts_in_items is None:
        return str(item.get("video_prompt", "") or "").strip()

    translated_item = translate_prompts_in_items(item, "en")
    return str(translated_item.get("video_prompt", item.get("video_prompt", "")) or "").strip()


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


def _wait_for_video_completion_aitunnel(
    task_id: str,
    headers: Dict[str, str],
    base_url: str,
    max_wait_time: int = _DEFAULT_MAX_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    start_time = time.time()

    while time.time() - start_time < max_wait_time:
        response = requests.get(
            f"{base_url}/videos/{task_id}",
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"AITUNNEL polling failed: {response.status_code} - {response.text}"
            )

        payload = response.json()
        status = payload.get("status")
        if status == "completed":
            return payload
        if status == "failed":
            raise RuntimeError(f"AITUNNEL generation failed: {payload.get('error')}")
        if status not in {"pending", "in_progress"}:
            raise RuntimeError(f"Неизвестный статус AITUNNEL: {status}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Превышено время ожидания AITUNNEL task {task_id}")


def _download_video_aitunnel(video_url: str, output_path: str, headers: Dict[str, str]) -> None:
    response = requests.get(
        video_url,
        headers=headers,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        stream=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"AITUNNEL download failed: {response.status_code} - {response.text}")

    with open(output_path, "wb") as output_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                output_file.write(chunk)

    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
        raise RuntimeError(f"Пустой файл после скачивания видео: {output_path}")


def _get_aitunnel_video_models(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    global _AITUNNEL_MODELS_CACHE

    if _AITUNNEL_MODELS_CACHE is not None and not force_refresh:
        return _AITUNNEL_MODELS_CACHE

    response = requests.get(_AITUNNEL_MODELS_URL, timeout=_DEFAULT_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise RuntimeError(
            f"AITUNNEL models endpoint failed: {response.status_code} - {response.text}"
        )

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Неверный ответ публичного models endpoint: {payload}")

    _AITUNNEL_MODELS_CACHE = payload
    return payload


def _parse_duration_from_timing(timing: str) -> int:
    try:
        if " - " in timing:
            start_str, end_str = timing.split(" - ", 1)
            duration = _time_str_to_seconds(end_str.strip()) - _time_str_to_seconds(start_str.strip())
        elif str(timing).endswith("s"):
            duration = int(str(timing)[:-1])
        else:
            duration = int(timing)
    except Exception:
        duration = 6

    if duration < 1:
        return 1
    return duration


def _time_str_to_seconds(time_str: str) -> int:
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0
    return 0
