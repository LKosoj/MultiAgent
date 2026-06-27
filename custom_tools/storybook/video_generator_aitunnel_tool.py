import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from custom_tools.storybook.video_generator_aitunnel_jobs import (
    _PROVIDER_NAME,
    _ProviderJobStore,
    _build_input_hash,
    _hash_source_image,
    _hash_text,
    _new_provider_job,
)
from custom_tools.storybook.video_generator_aitunnel_media import (
    _build_frame_image_payload,
    _is_url_or_data_url,
    _resolve_frame_dimensions,
    _resolve_model_and_size,
)
from custom_tools.storybook.video_generator_common import (
    sync_items_to_memory,
    update_shots_with_descriptions,
)

logger = logging.getLogger(__name__)

_AITUNNEL_MODELS_URL = "https://api.aitunnel.ru/public/aitunnel/models/videos"
_DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_WAIT_SECONDS = 900
_DEFAULT_POLL_INTERVAL_SECONDS = 15
_AITUNNEL_MODELS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_AITUNNEL_MODELS_CACHE_LOCK: threading.Lock = threading.Lock()


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
                os.environ.setdefault(key, value)


load_env_file()

try:
    from utils import translate_prompts_in_items
except Exception:
    translate_prompts_in_items = None


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
    sample_before_batch: bool = False,
    sample_shot_key: Optional[str] = None,
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
        sample_before_batch: Если True, обрабатывает только один выбранный/ожидающий шот.
        sample_shot_key: Опциональный ключ шота в формате "scene-shot" для sample режима.

    Returns:
        Словарь со статусом, сообщением и списком results по шотам.
    """
    enable = _as_bool(enable)
    force_update_prompts = _as_bool(force_update_prompts)
    skip_prompt_enhancement = _as_bool(skip_prompt_enhancement)
    sample_before_batch = _as_bool(sample_before_batch)
    if not enable and not project_id and items is None:
        logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
        return {
            "status": "skipped",
            "message": "Генерация видео отключена",
            "results": [],
        }

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
                    return _error_result("Неверная структура данных в shots.json")
            except Exception as exc:
                logger.error("❌ Ошибка чтения shots.json: %s", exc)
                return _error_result(f"Ошибка чтения shots.json: {exc}")
        elif items is None:
            if not enable:
                logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
                return {
                    "status": "skipped",
                    "message": "Генерация видео отключена",
                    "results": [],
                }
            logger.warning("⚠️ Файл shots.json не найден: %s", shots_file_path)
            return _error_result(f"Файл shots.json не найден: {shots_file_path}")

    if not items_list and items is not None:
        parsed_items, parse_error = _parse_items_payload(items)
        if parse_error:
            logger.error("❌ Ошибка парсинга items: %s", parse_error)
            return _error_result(parse_error)
        items_list = parsed_items

    if not items_list:
        logger.warning("⚠️ Список items пуст")
        return _error_result("Список items пуст")

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
                return _error_result(f"Ошибка перезагрузки shots.json: {exc}")

    if not enable:
        logger.info("🎬 Генерация видео AITUNNEL отключена (enable=False).")
        sync_items_to_memory(items, items_list)
        return {
            "status": "skipped",
            "message": "Генерация видео отключена",
            "results": [],
        }

    api_key = os.getenv("AITUNNEL_API_KEY")
    if not api_key:
        logger.error("❌ AITUNNEL_API_KEY не найден в переменных окружения")
        return _error_result("AITUNNEL_API_KEY не найден")
    configured_model = (os.getenv("AITUNNEL_VIDEO_MODEL") or "").strip()
    if not configured_model:
        logger.error("❌ AITUNNEL_VIDEO_MODEL не задан в переменных окружения")
        return _error_result("AITUNNEL_VIDEO_MODEL не задан")

    try:
        model_catalog = _get_aitunnel_video_models()
    except Exception as exc:
        logger.error("❌ Не удалось получить capabilities AITUNNEL: %s", exc)
        return _error_result(f"Не удалось получить список моделей AITUNNEL: {exc}")

    try:
        job_store = _ProviderJobStore(f"plots/storybooks/{project_id}/97_shots/provider_jobs.json") if project_id else None
    except Exception as exc:
        logger.error("❌ Ошибка чтения provider_jobs.json: %s", exc)
        return _error_result(f"Ошибка чтения provider_jobs.json: {exc}")
    video_items = _collect_video_items(items_list, include_existing=job_store is not None)
    if not video_items:
        logger.info("ℹ️ Нет кадров для генерации видео AITUNNEL")
        return _error_result("Нет кадров для обработки")

    remaining = 0
    if sample_before_batch:
        video_items, remaining, sample_error = _select_sample_video_items(video_items, sample_shot_key)
        if sample_error:
            return _error_result(sample_error)
        if not video_items:
            logger.info("ℹ️ Нет ожидающих кадров для sample генерации AITUNNEL")
            return {
                "status": "success",
                "message": "Нет ожидающих кадров для sample генерации AITUNNEL",
                "results": [],
                "stats": {
                    "total": 0,
                    "successful": 0,
                    "failed": 0,
                    "remaining": 0,
                },
            }

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
                    job_store,
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

    stats = {
        "total": total,
        "successful": successful,
        "failed": total - successful,
    }
    if sample_before_batch:
        stats["remaining"] = remaining

    message = f"Сгенерировано {successful} из {total} видео AITUNNEL"
    if successful == total:
        return {
            "status": "success",
            "message": message,
            "results": results,
            "stats": stats,
        }
    return _error_result(message, results=results, stats=stats)


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


def _error_result(message: str, **extra: Any) -> Dict[str, Any]:
    result = {
        "status": "error",
        "message": message,
        "error": message,
        "results": [],
    }
    result.update(extra)
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _collect_video_items(items_list: List[Dict[str, Any]], include_existing: bool = False) -> List[Dict[str, Any]]:
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

        output_exists = _is_non_empty_file(video_path)
        if output_exists and not include_existing:
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

        if not start_image and not output_exists:
            logger.debug("⏭️ Стартовый кадр не найден для %s-%s", scene_number, shot_number)
            continue

        shot_key = _shot_key(item)
        if shot_key in seen_keys:
            logger.debug("⏭️ Пропускаем дубликат кадра: %s", shot_key)
            continue

        item_copy = item.copy()
        item_copy["start_image"] = start_image
        item_copy["end_image"] = end_image
        seen_keys.add(shot_key)
        video_items.append(item_copy)

    return video_items


def _select_sample_video_items(
    video_items: List[Dict[str, Any]],
    sample_shot_key: Optional[str],
) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    pending_items = [item for item in video_items if not _is_non_empty_file(str(item.get("video_path") or ""))]

    normalized_key = str(sample_shot_key or "").strip()
    if normalized_key:
        selected = [item for item in video_items if _shot_key(item) == normalized_key]
        if not selected:
            return [], 0, f"sample_shot_key не найден: {normalized_key}"
        selected_item = selected[0]
        remaining = len([item for item in pending_items if _shot_key(item) != normalized_key])
        return [selected_item], remaining, None

    if not pending_items:
        return [], 0, None

    selected_item = pending_items[0]
    remaining = max(0, len(pending_items) - 1)
    return [selected_item], remaining, None


def _shot_key(item: Dict[str, Any]) -> str:
    return f"{item.get('scene_number', '?')}-{item.get('shot_number', '?')}"


def _is_non_empty_file(path: str) -> bool:
    try:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


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


def _extract_cost_and_currency(status_payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    usage = status_payload.get("usage") or {}
    if "cost" in usage:
        return usage.get("cost"), usage.get("currency")
    if "cost_rub" in usage:
        return usage.get("cost_rub"), "RUB"
    return None, None


def _generate_single_video_aitunnel(
    item: Dict[str, Any],
    session_id: str,
    api_key: str,
    model_catalog: Dict[str, Dict[str, Any]],
    configured_model: str,
    seed: Optional[int],
    language: str,
    job_store: Optional[_ProviderJobStore] = None,
) -> Dict[str, Any]:
    del session_id

    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    shot_key = _shot_key(item)
    video_path = item.get("video_path")
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    input_hash: Optional[str] = None

    try:
        prompt = _resolve_video_prompt(item, language)
        if not prompt:
            raise ValueError("Пустой video_prompt")

        prompt_hash = _hash_text(prompt)
        source_image_hashes = {
            "start_image": _hash_source_image(start_image),
            "end_image": _hash_source_image(end_image),
        }
        output_exists = _is_non_empty_file(str(video_path or ""))
        trusted_output_job = (
            job_store.find_latest_output_job_for_shot(shot_key, str(video_path))
            if job_store and output_exists
            else None
        )
        trusted_source_hashes = trusted_output_job.get("source_image_hashes") if trusted_output_job else {}
        missing_trusted_source_hash = any(
            source_image_hashes.get(name) is None and bool((trusted_source_hashes or {}).get(name))
            for name in source_image_hashes
        )
        if trusted_output_job and missing_trusted_source_hash:
            job_store.update_job(
                trusted_output_job["shot_key"],
                trusted_output_job["input_hash"],
                {
                    "status": "downloaded",
                    "output_path": str(video_path),
                    "error": None,
                },
                "downloaded_at",
            )
            return {
                "success": True,
                "video_path": video_path,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "task_id": trusted_output_job.get("task_id"),
                "video_url": trusted_output_job.get("video_url"),
                "error": None,
                "model": trusted_output_job.get("model") or configured_model,
                "cost_rub": trusted_output_job.get("cost") if trusted_output_job.get("currency") == "RUB" else None,
                "cost": trusted_output_job.get("cost"),
                "currency": trusted_output_job.get("currency"),
            }
        requested_duration = _parse_duration_from_timing(item.get("timing", "00:00 - 00:06"))
        frame_types = ["first_frame"] + (["last_frame"] if end_image else [])
        model_name = configured_model
        size_params: Dict[str, str] = {}
        duration = requested_duration

        try:
            width, height = _resolve_frame_dimensions(item, start_image)
            model_name, size_params, duration = _resolve_model_and_size(
                model_catalog=model_catalog,
                configured_model=configured_model,
                width=width,
                height=height,
                duration=requested_duration,
                requires_last_frame=bool(end_image),
                seed=seed,
            )
        except Exception:
            if not output_exists:
                raise

        input_hash = _build_input_hash(
            model_name=model_name,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            duration=duration,
            size_params=size_params,
            seed=seed,
            frame_types=frame_types,
        )

        if job_store:
            job_store.mark_stale_for_changed_input(shot_key, input_hash)
            existing_job = job_store.find_current_job(shot_key, input_hash)
            has_prior_job_for_shot = job_store.has_job_for_shot(shot_key)
            job_store.ensure_job(
                _new_provider_job(
                    shot_key=shot_key,
                    model=model_name,
                    prompt_hash=prompt_hash,
                    source_image_hashes=source_image_hashes,
                    input_hash=input_hash,
                    output_path=str(video_path),
                )
            )
        else:
            existing_job = None
            has_prior_job_for_shot = False

        if output_exists and (not job_store or existing_job or not has_prior_job_for_shot):
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "status": "downloaded",
                        "model": model_name,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                    "downloaded_at",
                )
            return {
                "success": True,
                "video_path": video_path,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "task_id": None,
                "video_url": None,
                "error": None,
                "model": model_name,
                "cost_rub": None,
                "cost": None,
                "currency": None,
            }

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
        poll_callback = (
            (lambda payload: _record_poll_update(job_store, shot_key, input_hash, payload))
            if job_store
            else None
        )

        resume_job = job_store.find_resumable_job(shot_key, input_hash) if job_store else None
        status_payload: Dict[str, Any] = {}
        task_id = None
        video_url = None
        video_url_requires_auth = False

        if resume_job and (resume_job.get("task_id") or resume_job.get("video_url")):
            task_id = resume_job.get("task_id")
            video_url = resume_job.get("video_url")
            video_url_requires_auth = _stored_video_url_requires_auth(resume_job, str(video_url or ""), base_url)
            status = resume_job.get("status")
            if video_url and status in {"completed", "download_failed", "downloaded"}:
                status_payload = {"usage": {}}
            elif task_id:
                status_payload = _wait_for_video_completion_aitunnel(
                    task_id=task_id,
                    headers=headers,
                    base_url=base_url,
                    on_poll=poll_callback,
                )
                unsigned_urls = status_payload.get("unsigned_urls") or []
                video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"
                video_url_requires_auth = not bool(unsigned_urls)
            else:
                raise RuntimeError("В provider_jobs.json нет task_id или video_url для resume")
        else:
            if resume_job and resume_job.get("status") in {"submitting", "submitted", "pending", "in_progress"}:
                raise RuntimeError(
                    "Найдена неоднозначная provider job без task_id/video_url; "
                    "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
                )
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "status": "submitting",
                        "model": model_name,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                    "submitting_at",
                )
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

            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "task_id": task_id,
                        "status": submit_payload.get("status") or "submitted",
                        "model": model_name,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                    "submitted_at",
                )

            status_payload = _wait_for_video_completion_aitunnel(
                task_id=task_id,
                headers=headers,
                base_url=base_url,
                on_poll=poll_callback,
            )
            unsigned_urls = status_payload.get("unsigned_urls") or []
            video_url = unsigned_urls[0] if unsigned_urls else f"{base_url}/videos/{task_id}/content?index=0"
            video_url_requires_auth = not bool(unsigned_urls)

        cost, currency = _extract_cost_and_currency(status_payload)
        if cost is None and resume_job:
            cost = resume_job.get("cost")
            currency = resume_job.get("currency")
        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "cost": cost,
                    "currency": currency,
                    "video_url": video_url,
                    "video_url_requires_auth": video_url_requires_auth,
                    "error": None,
                },
                "completed_at",
            )

        if job_store:
            job_store.update_job(shot_key, input_hash, {"status": "downloading", "error": None}, None)
        try:
            _download_video_aitunnel(
                video_url,
                video_path,
                _download_headers(video_url, headers, base_url, video_url_requires_auth),
            )
        except Exception as exc:
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"status": "download_failed", "error": str(exc), "video_url": video_url},
                    "failed_at",
                )
            raise

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "status": "downloaded",
                    "cost": cost,
                    "currency": currency,
                    "video_url": video_url,
                    "video_url_requires_auth": video_url_requires_auth,
                    "output_path": str(video_path),
                    "error": None,
                },
                "downloaded_at",
            )

        return {
            "success": True,
            "video_path": video_path,
            "scene_number": scene_number,
            "shot_number": shot_number,
            "task_id": task_id,
            "video_url": video_url,
            "error": None,
            "model": model_name,
            "cost_rub": cost if currency == "RUB" else ((status_payload.get("usage") or {}).get("cost_rub")),
            "cost": cost,
            "currency": currency,
        }
    except Exception as exc:
        if job_store and input_hash:
            current_job = job_store.find_current_job(shot_key, input_hash)
            status = "download_failed" if current_job and current_job.get("status") == "download_failed" else "failed"
            job_store.update_job(shot_key, input_hash, {"status": status, "error": str(exc)}, "failed_at")
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


def _record_poll_update(
    job_store: _ProviderJobStore,
    shot_key: str,
    input_hash: str,
    payload: Dict[str, Any],
) -> None:
    status = payload.get("status") or "unknown"
    updates: Dict[str, Any] = {
        "status": status,
        "error": payload.get("error"),
    }
    cost, currency = _extract_cost_and_currency(payload)
    if cost is not None:
        updates["cost"] = cost
        updates["currency"] = currency
    job_store.update_job(
        shot_key,
        input_hash,
        updates,
        "failed_at" if status == "failed" else "polled_at",
    )


def _wait_for_video_completion_aitunnel(
    task_id: str,
    headers: Dict[str, str],
    base_url: str,
    max_wait_time: int = _DEFAULT_MAX_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL_SECONDS,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
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
        if on_poll:
            on_poll(payload)
        if status == "completed":
            return payload
        if status == "failed":
            raise RuntimeError(f"AITUNNEL generation failed: {payload.get('error')}")
        if status not in {"pending", "in_progress"}:
            raise RuntimeError(f"Неизвестный статус AITUNNEL: {status}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Превышено время ожидания AITUNNEL task {task_id}")


def _download_video_aitunnel(video_url: str, output_path: str, headers: Dict[str, str]) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    response = requests.get(
        video_url,
        headers=headers,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        stream=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"AITUNNEL download failed: {response.status_code} - {response.text}")

    try:
        with tmp_path.open("wb") as output_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())

        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise RuntimeError(f"Пустой файл после скачивания видео: {output_path}")
        os.replace(tmp_path, output)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError(f"Пустой файл после скачивания видео: {output_path}")


def _download_headers(
    video_url: str,
    api_headers: Dict[str, str],
    base_url: str,
    requires_auth: bool,
) -> Dict[str, str]:
    if not requires_auth:
        return {}
    if not str(video_url or "").startswith(base_url.rstrip("/") + "/"):
        return {}
    return {"Authorization": api_headers.get("Authorization", "")}


def _stored_video_url_requires_auth(job: Dict[str, Any], video_url: str, base_url: str) -> bool:
    stored = job.get("video_url_requires_auth")
    if stored is not None:
        return bool(stored)
    return str(video_url or "").startswith(base_url.rstrip("/") + "/")


def _get_aitunnel_video_models(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    global _AITUNNEL_MODELS_CACHE

    # Fast-path: avoid lock acquisition on cache-hit (double-checked locking).
    if _AITUNNEL_MODELS_CACHE is not None and not force_refresh:
        return _AITUNNEL_MODELS_CACHE

    with _AITUNNEL_MODELS_CACHE_LOCK:
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
