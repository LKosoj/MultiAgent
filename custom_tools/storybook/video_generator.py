import os
import json
import logging
import time
import base64
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import jwt

# Загружаем переменные окружения из .env файла
def load_env_file():
    """Загружает переменные окружения из .env файла"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key, value)

load_env_file()

logger = logging.getLogger(__name__)

# Импорт общего функционала для генераторов видео
from custom_tools.storybook.video_generator_common import (
    update_shots_with_descriptions,
    sync_items_to_memory
)
from custom_tools.storybook.video_generator_aitunnel_jobs import (
    _ProviderJobStore,
    _build_input_hash,
    _hash_source_image,
    _hash_text,
    _new_provider_job,
)

# Импорты из проекта
try:
    from utils import call_openai_api, extract_json_from_markdown
except ImportError:
    call_openai_api = None
    extract_json_from_markdown = None
    logger.warning("⚠️ Модуль utils не найден, некоторые функции могут быть недоступны")

ak = os.getenv("KLING_API_KEY")
sk = os.getenv("KLING_API_SECRET_KEY")
_KLING_PROVIDER_NAME = "kling"
_KLING_MODEL_NAME = "kling-v2-1"
_KLING_MODE = "pro"
_KLING_ASPECT_RATIO = "16:9"


class _KlingPollTimeoutError(Exception):
    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


class _KlingSubmitFailedError(Exception):
    pass


class _KlingLedgerUpdateError(Exception):
    pass

# --- Вспомогательные функции для Kling ---

def encode_jwt_token(ak, sk):
    headers = {
        "alg": "HS256",
        "typ": "JWT"
    }
    payload = {
        "iss": ak,
        "exp": int(time.time()) + 1800, # The valid time, in this example, represents the current time+1800s(30min)
        "nbf": int(time.time()) - 5 # The time when it starts to take effect, in this example, represents the current time minus 5s
    }
    token = jwt.encode(payload, sk, headers=headers)
    return token


def _error_result(message: str, **extra: Any) -> Dict[str, Any]:
    result = {
        "status": "error",
        "message": message,
        "error": message,
        "results": [],
    }
    result.update(extra)
    return result


def _is_non_empty_file(path: str) -> bool:
    try:
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _sanitize_url_for_log(url: str) -> str:
    """Возвращает netloc + усечённый путь без query для безопасного логирования подписанных URL."""
    try:
        parsed = urlparse(str(url or ""))
        path = parsed.path or ""
        if len(path) > 32:
            path = path[:32] + "..."
        return f"{parsed.netloc}{path}" if parsed.netloc else "<url>"
    except Exception:
        return "<url>"


def video_generator_tool(
    session_id: str,
    items: Any,
    project_id: Optional[str] = None,
    max_concurrency: int = 3,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = 'en',
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False
) -> Dict[str, Any]:
    """
    Генерирует видео из изображений с использованием Kling AI API.
    
    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        items: Данные items или JSON-строка. Ожидаемая структура:
            {
                "items": [список кадров для конвертации в видео],
                "consistency_rules": [правила проекта]
            }
        project_id: Идентификатор проекта (для логирования и контекста)
        max_concurrency: Максимальное количество параллельных запросов
        enable: Если True, выполняет генерацию видео, иначе пропускает
        seed: Сид для генерации видео
        language: Язык генерации для корректного перевода промптов (по умолчанию 'en')
        force_update_prompts: Если True, принудительно обновляет video_prompt независимо от timestamp
        skip_prompt_enhancement: Если True, пропускает улучшение промпта LLM (только перевод без галлюцинаций)
        
    Returns:
        Словарь с результатами генерации видео для каждого кадра.
    """
    
    # Парсим входные данные
    if isinstance(items, str):
        try:
            items_obj = json.loads(items)
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга JSON: {e}")
            return {"status": "error", "message": f"Невалидный JSON: {e}", "results": []}
    else:
        items_obj = items
    
    # Извлекаем список кадров
    if isinstance(items_obj, dict) and "items" in items_obj:
        items_list = items_obj.get("items", [])
    elif isinstance(items_obj, list):
        items_list = items_obj
    else:
        logger.error("❌ Неверная структура данных items")
        return {"status": "error", "message": "Неверная структура данных", "results": []}
    
    if not items_list:
        logger.warning("⚠️ Список items пуст")
        return {"status": "success", "message": "Список items пуст", "results": []}

    # ЭТАП 1: Обновляем описания изображений и промпты (единая логика из common)
    if project_id:
        shots_file_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
        if os.path.exists(shots_file_path):
            logger.info("📝 Этап 1: Анализ и обновление описаний изображений")
            descriptions_updated = update_shots_with_descriptions(shots_file_path, items_list, force_update_prompts, skip_prompt_enhancement)
            
            if descriptions_updated:
                logger.info("🔄 Описания обновлены, перезагружаем данные из shots.json")
                # Перезагружаем данные после обновления описаний
                try:
                    with open(shots_file_path, 'r', encoding='utf-8') as f:
                        loaded_data = json.load(f)
                        if isinstance(loaded_data, dict):
                            items_list = loaded_data.get("items", [])
                except Exception as e:
                    logger.error(f"❌ Ошибка перезагрузки shots.json после обновления описаний: {e}")
                    return {"status": "error", "message": f"Ошибка перезагрузки shots.json: {e}", "results": []}

    if not enable:
        logger.info("🎬 Генерация видео Kling отключена (enable=False). Анализ изображений завершен.")
        return {"status": "skipped", "message": "Генерация видео отключена, анализ изображений выполнен", "results": []}

    api_key = os.getenv("KLING_API_KEY")
    api_secret = os.getenv("KLING_API_SECRET_KEY")
    if not api_key or not api_secret:
        logger.error("❌ KLING_API_KEY или KLING_API_SECRET_KEY не найдены в переменных окружения")
        return {"status": "error", "message": "KLING_API_KEY или KLING_API_SECRET_KEY не найдены", "results": []}

    try:
        job_store = (
            _ProviderJobStore(
                f"plots/storybooks/{project_id}/97_shots/provider_jobs.json",
                provider_name=_KLING_PROVIDER_NAME,
            )
            if project_id
            else None
        )
    except Exception as e:
        logger.error(f"❌ Ошибка чтения provider_jobs.json: {e}")
        return _error_result(f"Ошибка чтения provider_jobs.json: {e}")
    
    # Фильтруем кадры, которые нужно конвертировать в видео
    video_items = []
    seen_shots = set()  # Для дедупликации по scene_number + shot_number
    for item in items_list:
        # Берем только START кадры для генерации видео (паритет с mm/veo)
        if item.get("shot_type") != "start":
            continue

        video_prompt = item.get("video_prompt", "").strip()
        video_path = item.get("video_path")
        scene_number = item.get("scene_number", "?")
        shot_number = item.get("shot_number", "?")

        # Проверяем обязательные поля
        if not video_prompt or not video_path:
            logger.debug(f"⏭️ Пропускаем кадр без video_prompt или video_path: {scene_number}-{shot_number}")
            continue

        # Проверяем, существует ли уже непустое видео. В project_id-режиме
        # пропускаем его через single-step, чтобы отметить downloaded в ledger.
        output_exists = _is_non_empty_file(video_path)
        if output_exists:
            logger.info(f"✅ Видео уже существует: {video_path}")
            if job_store is None:
                continue
        
        # Анализируем директорию, где должно быть видео, для поиска изображений
        video_dir = os.path.dirname(video_path)
        if not os.path.exists(video_dir):
            logger.debug(f"⏭️ Директория видео не существует: {video_dir}")
            if not output_exists:
                continue
        
        # Ищем start и end изображения в директории
        start_image = None
        end_image = None
        
        # Паттерны для поиска файлов
        try:
            scene_num = int(scene_number) if scene_number != "?" else 1
            shot_num = int(shot_number) if shot_number != "?" else 1
        except (ValueError, TypeError):
            scene_num = 1
            shot_num = 1
            
        start_pattern = f"img_final_start_{scene_num:02d}_{shot_num:02d}.png"
        end_pattern = f"img_final_end_{scene_num:02d}_{shot_num:02d}.png"
        
        start_path = os.path.join(video_dir, start_pattern)
        end_path = os.path.join(video_dir, end_pattern)
        
        # Проверяем наличие start изображения (обязательно)
        if os.path.exists(start_path):
            start_image = start_path
            logger.debug(f"🖼️ Найдено start изображение: {start_pattern}")
        else:
            logger.debug(f"⏭️ Start изображение не найдено: {start_pattern}, пропускаем")
            if not output_exists:
                continue
        
        # Проверяем наличие end изображения (опционально)
        if os.path.exists(end_path):
            end_image = end_path
            logger.debug(f"🖼️ Найдено end изображение: {end_pattern}")
        
        # Проверяем дедупликацию по scene_number + shot_number
        shot_key = f"{scene_number}-{shot_number}"
        if shot_key in seen_shots:
            logger.debug(f"⏭️ Пропускаем дубликат кадра: {shot_key}")
            continue

        # Добавляем найденные изображения в item
        item_copy = item.copy()
        item_copy["start_image"] = start_image
        item_copy["end_image"] = end_image

        seen_shots.add(shot_key)
        video_items.append(item_copy)
    
    if not video_items:
        logger.info(f"ℹ️ Нет кадров для генерации видео (проект: {project_id})")
        return {"status": "success", "message": "Нет кадров для обработки", "results": []}
    
    logger.info(f"🎬 Начинаем генерацию видео для {len(video_items)} кадров (проект: {project_id})")
    
    # Генерируем видео пакетами с ожиданием завершения каждого пакета
    results = []
    
    # Разбиваем items на пакеты по max_concurrency
    for batch_start in range(0, len(video_items), max_concurrency):
        batch_end = min(batch_start + max_concurrency, len(video_items))
        batch_items = video_items[batch_start:batch_end]
        
        logger.info(f"🎬 Обрабатываем пакет {batch_start//max_concurrency + 1}: {len(batch_items)} видео")
        
        # Обрабатываем текущий пакет и дожидаемся завершения ВСЕХ задач в пакете
        with ThreadPoolExecutor(max_workers=len(batch_items)) as executor:
            # Создаем задачи для текущего пакета
            future_to_item = {
                executor.submit(_generate_single_video, item, session_id, job_store, api_key, api_secret): item
                for item in batch_items
            }
            
            # Обрабатываем результаты пакета
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                    results.append(result)
                    
                    if result.get("success"):
                        logger.info(f"✅ Видео создано: {result.get('video_path', '')}")
                    else:
                        logger.error(f"❌ Ошибка создания видео: {result.get('error', '')}")
                        
                except Exception as e:
                    logger.error(f"❌ Исключение при генерации видео: {e}")
                    results.append({
                        "success": False,
                        "error": str(e),
                        "scene_number": item.get("scene_number"),
                        "shot_number": item.get("shot_number")
                    })
        
        logger.info(f"📦 Пакет {batch_start//max_concurrency + 1} завершен. Переходим к следующему...")
    
    # Статистика
    successful = len([r for r in results if r.get("success")])
    total = len(results)
    
    logger.info(f"📊 Генерация завершена: {successful}/{total} успешно")

    # Синхронизируем изменения обратно в items в памяти
    sync_items_to_memory(items, items_list)

    stats = {
        "total": total,
        "successful": successful,
        "failed": total - successful
    }
    message = f"Сгенерировано {successful} из {total} видео"
    if successful == total:
        return {
            "status": "success",
            "message": message,
            "results": results,
            "stats": stats,
        }
    return _error_result(message, results=results, stats=stats)


def _generate_single_video(
    item: Dict[str, Any],
    session_id: str,
    job_store: Optional[_ProviderJobStore] = None,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Генерирует одно видео из изображений (start и опционально end) с использованием Kling AI API.
    """
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    video_prompt = item.get("video_prompt", "")
    video_path = item.get("video_path")
    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    shot_key = f"{scene_number}-{shot_number}"
    timing = item.get("timing", "00:00 - 00:05")
    input_hash: Optional[str] = None
    
    logger.info(f"🎥 Генерируем видео для сцены {scene_number}, кадр {shot_number}")
    if end_image:
        logger.info(f"📹 Используем start + end изображения для анимации")
    else:
        logger.info(f"📹 Используем только start изображение")

    try:
        duration = _parse_duration_from_timing(timing)
        prompt_hash = _hash_text(str(video_prompt or ""))
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
        trusted_metadata_matches = (
            trusted_output_job
            and trusted_output_job.get("prompt_hash") == prompt_hash
            and trusted_output_job.get("model") == _KLING_MODEL_NAME
            and trusted_output_job.get("resolved_duration") == duration
            and (trusted_output_job.get("resolved_size_params") or {}) == {
                "mode": _KLING_MODE,
                "aspect_ratio": _KLING_ASPECT_RATIO,
            }
        )
        if trusted_output_job and missing_trusted_source_hash and trusted_metadata_matches:
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
            }
        frame_types = ["first_frame"] + (["last_frame"] if end_image else [])
        input_hash = _build_input_hash(
            model_name=f"{_KLING_MODEL_NAME}|{_KLING_MODE}|{_KLING_ASPECT_RATIO}",
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=duration,
            requested_width=0,
            requested_height=0,
            seed=None,
            frame_types=frame_types,
            provider_name=_KLING_PROVIDER_NAME,
        )

        if job_store:
            job_store.mark_stale_for_changed_input(shot_key, input_hash)
            existing_job = job_store.find_current_job(shot_key, input_hash)
            has_prior_job_for_shot = job_store.has_job_for_shot(shot_key)
            job_store.ensure_job(
                _new_provider_job(
                    shot_key=shot_key,
                    model=_KLING_MODEL_NAME,
                    prompt_hash=prompt_hash,
                    source_image_hashes=source_image_hashes,
                    input_hash=input_hash,
                    output_path=str(video_path),
                    resolved_size_params={"mode": _KLING_MODE, "aspect_ratio": _KLING_ASPECT_RATIO},
                    resolved_duration=duration,
                    provider_name=_KLING_PROVIDER_NAME,
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
                        "model": _KLING_MODEL_NAME,
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
                "task_id": existing_job.get("task_id") if existing_job else None,
                "video_url": existing_job.get("video_url") if existing_job else None,
                "error": None,
            }

        api_key = api_key or ak
        api_secret = api_secret or sk
        if not api_key or not api_secret:
            raise RuntimeError("KLING_API_KEY или KLING_API_SECRET_KEY не заданы")

        resume_job = (
            job_store.find_resumable_job(shot_key, input_hash, prompt_hash, source_image_hashes)
            if job_store
            else None
        )
        task_id = None

        if resume_job and resume_job.get("task_id"):
            task_id = resume_job.get("task_id")
            logger.info(f"↩️ Kling resume task_id={task_id} для сцены {scene_number}-{shot_number}")
        elif resume_job and resume_job.get("status") in {"submitting", "submitted", "pending", "in_progress", "poll_timeout"}:
            raise RuntimeError(
                "Найдена неоднозначная Kling provider job без task_id; "
                "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
            )
        else:
            if not start_image or not os.path.exists(start_image):
                raise ValueError(f"Start image not found: {start_image}")

            os.makedirs(os.path.dirname(video_path), exist_ok=True)

            with open(start_image, "rb") as img_file:
                image_data = base64.b64encode(img_file.read()).decode('utf-8')

            image_tail_data = None
            if end_image:
                with open(end_image, "rb") as img_file:
                    image_tail_data = base64.b64encode(img_file.read()).decode('utf-8')

            token = encode_jwt_token(api_key, api_secret)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "model_name": _KLING_MODEL_NAME,
                "image": image_data,
                "image_tail": image_tail_data,
                "prompt": video_prompt,
                "negative_prompt": "blurry, distorted, low quality, artifacts, watermark",
                "cfg_scale": 0.5,
                "mode": _KLING_MODE,
                "duration": duration,
                "aspect_ratio": _KLING_ASPECT_RATIO
            }

            if job_store:
                claim = job_store.claim_submitting_job(
                    shot_key,
                    input_hash,
                    {
                        "model": _KLING_MODEL_NAME,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                )
                if not claim.get("claimed"):
                    raise RuntimeError(
                        "Найдена неоднозначная Kling provider job без task_id; "
                        "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
                    )

            logger.debug(f"📤 Отправляем запрос в Kling AI для сцены {scene_number}-{shot_number}")
            response = requests.post(
                "https://api-singapore.klingai.com/v1/videos/image2video",
                headers=headers,
                json=payload,
                timeout=(30, 600)
            )

            if response.status_code != 200:
                raise _KlingSubmitFailedError(f"Ошибка API Kling: {response.status_code} - {response.text}")

            result = response.json()
            task_id = result.get("data", {}).get("task_id")

            if not task_id:
                raise _KlingSubmitFailedError(f"Не получен task_id от API: {result}")

            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "task_id": task_id,
                        "status": "submitted",
                        "provider_status": "submitted",
                        "error": None,
                    },
                    "submitted_at",
                )

        logger.info(f"📋 Kling task_id: {task_id} для сцены {scene_number}-{shot_number}")

        video_url = _wait_for_video_completion(
            task_id,
            session_id,
            api_key,
            api_secret,
            on_poll=(
                (lambda payload: _record_kling_poll_update(job_store, shot_key, input_hash, payload))
                if job_store
                else None
            ),
            raise_on_timeout=job_store is not None,
        )

        if not video_url:
            raise RuntimeError("Не удалось получить URL видео")

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "provider_status": "succeed",
                    "video_url": video_url,
                    "video_url_requires_auth": False,
                    "error": None,
                },
                "completed_at",
            )
            job_store.update_job(shot_key, input_hash, {"status": "downloading", "error": None}, None)

        success = _download_video(video_url, video_path)
        if not success:
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"status": "download_failed", "error": "Ошибка скачивания видео", "video_url": video_url},
                    "failed_at",
                )
            raise RuntimeError("Ошибка скачивания видео")

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "status": "downloaded",
                    "task_id": task_id,
                    "video_url": video_url,
                    "video_url_requires_auth": False,
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
            "error": None
        }

    except Exception as e:
        if job_store and input_hash:
            current_job = job_store.find_current_job(shot_key, input_hash)
            if current_job and current_job.get("status") == "download_failed":
                status = "download_failed"
            elif (
                current_job
                and current_job.get("status") == "submitting"
                and not current_job.get("task_id")
                and not isinstance(e, _KlingSubmitFailedError)
                and not isinstance(e, _KlingLedgerUpdateError)
            ):
                status = "submitting"
            elif isinstance(e, (_KlingPollTimeoutError, _KlingLedgerUpdateError)):
                status = "poll_timeout"
            else:
                status = "failed"
            job_store.update_job(shot_key, input_hash, {"status": status, "error": str(e)}, "failed_at")
        logger.error(f"❌ Исключение при генерации видео сцена {scene_number}-{shot_number}: {e}")
        return {
            "success": False,
            "error": str(e),
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path
        }


def _record_kling_poll_update(
    job_store: _ProviderJobStore,
    shot_key: str,
    input_hash: str,
    payload: Dict[str, Any],
) -> None:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    job_store.update_job(
        shot_key,
        input_hash,
        {
            "provider_status": data.get("task_status") or "unknown",
            "error": data.get("task_status_msg") or payload.get("message"),
        },
        "polled_at",
    )


def _wait_for_video_completion(
    task_id: str,
    session_id: str,
    api_key: str,
    api_secret: str,
    max_wait_time: int = 600,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
    raise_on_timeout: bool = False,
) -> Optional[str]:
    """
    Ожидает завершения генерации видео и возвращает URL.
    
    Args:
        task_id: ID задачи в Kling AI
        session_id: ID сессии для логирования
        max_wait_time: Максимальное время ожидания в секундах
        
    Returns:
        URL видео или None в случае ошибки
    """
    
    start_time = time.time()
    check_interval = 10  # Проверяем каждые 10 секунд
    first_check = True

    while time.time() - start_time < max_wait_time:
        if first_check:
            time.sleep(50)
            first_check = False
        try:
            token = encode_jwt_token(api_key, api_secret)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            response = requests.get(
                f"https://api-singapore.klingai.com/v1/videos/image2video/{task_id}",
                headers=headers,
                timeout=(30, 600)
            )

            # Обрабатываем не-200 ответ: логируем и повторяем попытку
            if response.status_code != 200:
                logger.error(f"❌ Ошибка проверки статуса task {task_id}: {response.status_code} - {response.text}")
                time.sleep(check_interval)
                continue

            result = response.json()
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке статуса task {task_id}: {e}")
            time.sleep(check_interval)
            continue

        if on_poll:
            try:
                on_poll(result)
            except Exception as exc:
                raise _KlingLedgerUpdateError(str(exc)) from exc
        data = result.get("data", {})
        status = data.get("task_status")

        logger.debug(f"🔄 Статус task {task_id}: {status}")

        if status == "succeed":
            task_result = data.get("task_result", {})
            videos = task_result.get("videos", [])
            if videos and len(videos) > 0:
                video_url = videos[0].get("url")
                if video_url:
                    logger.info(f"✅ Видео готово: {task_id}")
                    return video_url
            logger.error(f"❌ Видео готово, но URL не найден в ответе: {result}")
            return None

        if status == "failed":
            logger.error(f"❌ Генерация видео провалилась: {task_id}")
            return None

        if status in ["submitted", "processing"]:
            time.sleep(check_interval)
            continue

        logger.warning(f"⚠️ Неизвестный статус: {status} для task {task_id}")
        time.sleep(check_interval)
        continue
    
    logger.error(f"⏰ Превышено время ожидания для task {task_id}")
    if raise_on_timeout:
        raise _KlingPollTimeoutError(f"Превышено время ожидания для task {task_id}", task_id)
    return None


def _download_video(video_url: str, output_path: str) -> bool:
    """
    Скачивает видео по URL и сохраняет в указанный путь.
    
    Returns:
        True если скачивание успешно, False в противном случае
    """
    try:
        logger.info(f"⬇️ Скачиваем видео: {os.path.basename(output_path)}")
        
        response = requests.get(video_url, timeout=(30, 600), stream=True)
        response.raise_for_status()

        # Скачиваем атомарно: во временный файл + os.replace
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        try:
            with tmp_path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
                logger.error(f"❌ Файл не создался или пустой: {output_path}")
                return False
            os.replace(tmp_path, output)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        # Проверяем, что файл создался и не пустой
        if _is_non_empty_file(output_path):
            logger.info(f"✅ Видео скачано: {output_path}")
            return True
        logger.error(f"❌ Файл не создался или пустой: {output_path}")
        return False

    except Exception as e:
        logger.error(f"❌ Ошибка скачивания видео {_sanitize_url_for_log(video_url)}: {e}")
        return False


def _parse_duration_from_timing(timing: str) -> int:
    """
    Парсит строку timing и возвращает длительность в секундах.
    Kling AI поддерживает только 5 или 10 секунд.
    
    Args:
        timing: Строка вида "00:00 - 00:05" или "5s"
        
    Returns:
        5 или 10 секунд
    """
    try:
        if " - " in timing:
            start_str, end_str = timing.split(" - ")
            start_seconds = _time_str_to_seconds(start_str.strip())
            end_seconds = _time_str_to_seconds(end_str.strip())
            duration = end_seconds - start_seconds
        elif timing.endswith("s"):
            duration = int(timing[:-1])
        else:
            duration = 5  # По умолчанию
        
        # Kling AI поддерживает только 5 или 10 секунд
        if duration <= 5:
            return 5
        else:
            return 10
            
    except Exception:
        return 5  # По умолчанию 5 секунд


def _time_str_to_seconds(time_str: str) -> int:
    """
    Конвертирует время в формате MM:SS в секунды.
    """
    try:
        parts = time_str.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes * 60 + seconds
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        else:
            return 0
    except Exception:
        return 0

if __name__ == "__main__":
    ak = os.getenv("KLING_API_KEY")
    sk = os.getenv("KLING_API_SECRET_KEY")
    if ak and sk:
        print(encode_jwt_token(ak, sk))
    else:
        print("❌ KLING_API_KEY или KLING_API_SECRET_KEY не установлены")
