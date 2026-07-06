import os
import json
import logging
import time
import base64
import mimetypes
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from google import genai
from google.genai import types
from PIL import Image
import io

# Настройка логгера
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из .env файла
def load_env_file():
    """Загружает переменные окружения из .env файла"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    # print(f"DEBUG: load_env_file called. CWD: {cwd}, File Dir: {current_dir}")
    
    # Пробуем разные варианты расположения .env
    possible_paths = [
        os.path.join(current_dir, '.env'),
        os.path.join(current_dir, '..', '.env'),
        os.path.join(current_dir, '..', '..', '.env'), # Должно сработать, если файл в custom_tools/storybook
        os.path.join(cwd, '.env')
    ]
    
    env_path = None
    for path in possible_paths:
        if os.path.exists(path):
            env_path = path
            # print(f"DEBUG: Found .env at {env_path}")
            break
            
    if env_path:
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key, value)
    else:
        logger.warning(f"⚠️ .env не найден. Searched in: {possible_paths}")

load_env_file()

# Импорты из проекта
try:
    from utils import translate_prompts_in_items
except ImportError:
    translate_prompts_in_items = None
    logger.warning("⚠️ Модуль utils не найден, некоторые функции могут быть недоступны")

# Импорт общего функционала для генераторов видео
# Только те функции, которые реально используются в этом модуле
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


_VEO_PROVIDER_NAME = "veo"
_VEO_MODEL_NAME = 'veo-3.1-generate-preview'
_VEO_ASPECT_RATIO = "16:9"
_VEO_RESOLUTION = "720p"


class _VeoPollTimeoutError(Exception):
    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


class _VeoSubmitUnknownError(Exception):
    pass


class _VeoLedgerUpdateError(Exception):
    pass


class _VeoOperationRef:
    def __init__(self, name: str):
        self.name = name
        self.done = False
        self.error = None
        self.response = None


def _veo_operation_ref_from_name(name: str) -> Any:
    operation_type = getattr(types, "GenerateVideosOperation", None)
    if operation_type is not None:
        return operation_type(name=name, done=False)
    return _VeoOperationRef(name)


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


# --- Основная логика генерации Veo ---

def video_generator_veo_tool(
    session_id: str,
    project_id: str,
    items: Any = None,
    max_concurrency: int = 1, # Veo может иметь лимиты, лучше по одному или 2
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = 'en',
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False
) -> Dict[str, Any]:
    """
    Генерирует видео с использованием Google Veo (Gemini) API.
    Замена для video_generator_mm_tool.
    
    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        items: Параметр игнорируется, данные читаются из shots.json
        project_id: Идентификатор проекта (обязателен для поиска shots.json)
        max_concurrency: Максимальное количество параллельных запросов
        enable: Если True, выполняет генерацию видео, иначе пропускает
        seed: Сид для генерации видео
        language: Язык генерации для корректного перевода промптов (по умолчанию 'en')
        force_update_prompts: Если True, принудительно обновляет video_prompt независимо от timestamp
        skip_prompt_enhancement: Если True, пропускает улучшение промпта (только перевод, без галлюцинаций)
    Returns:
        Словарь с результатами генерации видео для каждого кадра.
    """
    if not project_id:
        logger.error("project_id is missing")
        return {"status": "error", "message": "project_id обязателен", "results": []}

    logger.debug(f"Starting video generation for project: {project_id}, enable: {enable}")

    shots_file_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
    
    if not os.path.exists(shots_file_path):
        return {"status": "error", "message": f"Файл не найден: {shots_file_path}", "results": []}
        
    try:
        with open(shots_file_path, 'r', encoding='utf-8') as f:
            shots_data = json.load(f)
        items_list = shots_data.get("items", []) if isinstance(shots_data, dict) else shots_data
    except Exception as e:
        return {"status": "error", "message": f"Ошибка чтения: {e}", "results": []}

    # ЭТАП 1: Обновляем описания изображений и промпты (единая логика из common)
    logger.info("📝 Этап 1: Анализ и обновление описаний изображений")
    descriptions_updated = update_shots_with_descriptions(
        shots_file_path, 
        items_list, 
        force_update=force_update_prompts,
        skip_prompt_enhancement=skip_prompt_enhancement
    )
    
    if not enable:
        logger.info("🎬 Генерация видео Veo отключена (enable=False). Анализ изображений завершен.")
        return {"status": "skipped", "message": "Генерация видео отключена, анализ изображений выполнен", "results": []}
    
    if descriptions_updated:
        logger.info("🔄 Описания обновлены, перезагружаем данные из shots.json")
        # Перезагружаем данные после обновления описаний
        try:
            with open(shots_file_path, 'r', encoding='utf-8') as f:
                shots_data = json.load(f)
            items_list = shots_data.get("items", []) if isinstance(shots_data, dict) else shots_data
        except Exception as e:
            logger.error(f"❌ Ошибка перезагрузки shots.json после обновления описаний: {e}")
            return {"status": "error", "message": f"Ошибка перезагрузки shots.json: {e}", "results": []}

    # Логика инициализации клиента:
    # 1. Проверяем PROJECT_ID для Vertex AI (приоритет для last_frame)
    # 2. Иначе проверяем GEMINI_API_KEY
    project_id_vertex = os.getenv('GOOGLE_CLOUD_PROJECT')
    location_vertex = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')
    api_key = os.getenv("GEMINI_API_KEY")

    if not project_id_vertex and not api_key:
        logger.error("No credentials found: set GOOGLE_CLOUD_PROJECT (Vertex AI) or GEMINI_API_KEY")
        return {"status": "error", "message": "Credentials not found: set GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY", "results": []}

    # Флаг использования Vertex
    use_vertex = False

    # Если хотим last_frame (а мы хотим), то предпочтительно Vertex.
    # Проверяем, есть ли у нас PROJECT_ID.
    if project_id_vertex:
        logger.debug(f"Using Vertex AI with project: {project_id_vertex}")
        use_vertex = True
    else:
        logger.debug("Using Gemini API Key (Warning: last_frame may not work)")

    try:
        job_store = _ProviderJobStore(
            f"plots/storybooks/{project_id}/97_shots/provider_jobs.json",
            provider_name=_VEO_PROVIDER_NAME,
        )
    except Exception as e:
        logger.error(f"❌ Ошибка чтения provider_jobs.json: {e}")
        return _error_result(f"Ошибка чтения provider_jobs.json: {e}")

    # Подготовка задач
    video_items = []
    seen_shots = set()
    
    logger.debug(f"Processing {len(items_list)} items from shots.json")

    for item in items_list:
        if item.get("shot_type") != "start": 
            continue
        
        scene = item.get("scene_number")
        shot = item.get("shot_number")
        video_path = item.get("video_path")
        
        output_exists = _is_non_empty_file(str(video_path or ""))
        if not video_path:
            logger.debug(f"Skip existing or invalid video path: {video_path}")
            continue
        if output_exists:
            logger.debug(f"Existing video path will be recorded in ledger: {video_path}")
            
        # Поиск изображений (аналогично mm_tool)
        start_img = item.get("start_image")
        if not start_img:
            video_dir = os.path.dirname(video_path)
            try:
                s_num = int(scene) if scene != "?" else 1
                sh_num = int(shot) if shot != "?" else 1
            except: s_num, sh_num = 1, 1
            start_pattern = f"img_final_start_{s_num:02d}_{sh_num:02d}.png"
            potential_start = os.path.join(video_dir, start_pattern)
            if os.path.exists(potential_start):
                start_img = potential_start

        if not start_img or not os.path.exists(start_img):
            logger.debug(f"Start image not found for {scene}-{shot}: {start_img}")
            if not output_exists:
                continue
            
        # End image
        end_img = item.get("end_image")
        if not end_img:
            video_dir = os.path.dirname(video_path)
            try:
                s_num = int(scene) if scene != "?" else 1
                sh_num = int(shot) if shot != "?" else 1
            except: s_num, sh_num = 1, 1
            end_pattern = f"img_final_end_{s_num:02d}_{sh_num:02d}.png"
            potential_end = os.path.join(video_dir, end_pattern)
            if os.path.exists(potential_end):
                end_img = potential_end
        
        item_copy = item.copy()
        item_copy["start_image"] = start_img
        item_copy["end_image"] = end_img
        
        key = f"{scene}-{shot}"
        if key not in seen_shots:
            seen_shots.add(key)
            video_items.append(item_copy)

    logger.info(f"🎬 Начинаем генерацию Veo для {len(video_items)} кадров")
    
    results = []
    # Обработка пакетами
    for batch_start in range(0, len(video_items), max_concurrency):
        batch = video_items[batch_start:batch_start + max_concurrency]
        
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(
                    _generate_single_video_veo,
                    item,
                    api_key,
                    language,
                    use_vertex,
                    project_id_vertex,
                    location_vertex,
                    job_store,
                ): item
                for item in batch
            }
            
            for future in as_completed(futures):
                item = futures[future]
                try:
                    res = future.result()
                    results.append(res)
                    if res.get("success"):
                        logger.info(f"✅ Veo видео создано: {res.get('video_path')}")
                    else:
                        logger.error(f"❌ Ошибка Veo: {res.get('error')}")
                except Exception as e:
                    logger.error(f"❌ Исключение: {e}")
                    results.append({"success": False, "error": str(e)})

    successful = len([r for r in results if r.get("success")])
    total = len(results)

    # Синхронизируем изменения обратно в items в памяти
    sync_items_to_memory(items, items_list)

    stats = {
        "total": total,
        "successful": successful,
        "failed": total - successful
    }
    message = f"Сгенерировано {successful} из {total} видео Veo"
    if successful == total:
        return {
            "status": "success",
            "message": message,
            "results": results,
            "stats": stats,
        }
    return _error_result(message, results=results, stats=stats)

def _generate_single_video_veo(
    item: Dict[str, Any],
    api_key: Optional[str],
    language: str,
    use_vertex: bool,
    project_id: str,
    location: str,
    job_store: Optional[_ProviderJobStore] = None,
) -> Dict[str, Any]:
    scene = item.get("scene_number")
    shot = item.get("shot_number")
    shot_key = f"{scene}-{shot}"
    video_path = item.get("video_path")
    start_path = item.get("start_image")
    end_path = item.get("end_image")
    input_hash: Optional[str] = None

    # Локализация промпта
    if translate_prompts_in_items and language != 'en':
        tr_item = translate_prompts_in_items(item, 'en')
        prompt = tr_item.get("video_prompt", item.get("video_prompt"))
    else:
        prompt = item.get("video_prompt", "")
        
    if not prompt:
        prompt = "Cinematic shot" # Fallback

    logger.info(f"🎥 Veo: Сцена {scene}-{shot}, Prompt: {prompt[:50]}...")

    try:
        backend_identity = f"vertex:{project_id}:{location}" if use_vertex else "gemini"
        prompt_hash = _hash_text(str(prompt or ""))
        source_image_hashes = {
            "start_image": _hash_source_image(start_path),
            "end_image": _hash_source_image(end_path),
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
            and trusted_output_job.get("model") == _VEO_MODEL_NAME
            and (trusted_output_job.get("resolved_size_params") or {}) == {
                "aspect_ratio": _VEO_ASPECT_RATIO,
                "resolution": _VEO_RESOLUTION,
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
                "scene": scene,
                "shot": shot,
                "task_id": trusted_output_job.get("task_id"),
            }
        frame_types = ["first_frame"] + (["last_frame"] if end_path else [])
        input_hash = _build_input_hash(
            model_name=f"{_VEO_MODEL_NAME}|{backend_identity}|{_VEO_ASPECT_RATIO}|{_VEO_RESOLUTION}|1",
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=0,
            requested_width=0,
            requested_height=0,
            seed=None,
            frame_types=frame_types,
            provider_name=_VEO_PROVIDER_NAME,
        )

        if job_store:
            job_store.mark_stale_for_changed_input(shot_key, input_hash)
            existing_job = job_store.find_current_job(shot_key, input_hash)
            has_prior_job_for_shot = job_store.has_job_for_shot(shot_key)
            job_store.ensure_job(
                _new_provider_job(
                    shot_key=shot_key,
                    model=_VEO_MODEL_NAME,
                    prompt_hash=prompt_hash,
                    source_image_hashes=source_image_hashes,
                    input_hash=input_hash,
                    output_path=str(video_path),
                    resolved_size_params={"aspect_ratio": _VEO_ASPECT_RATIO, "resolution": _VEO_RESOLUTION},
                    resolved_duration=None,
                    provider_name=_VEO_PROVIDER_NAME,
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
                        "model": _VEO_MODEL_NAME,
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
                "scene": scene,
                "shot": shot,
                "task_id": existing_job.get("task_id") if existing_job else None,
            }

        if use_vertex:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location
            )
        else:
            client = genai.Client(api_key=api_key)

        resume_job = (
            job_store.find_resumable_job(shot_key, input_hash, prompt_hash, source_image_hashes)
            if job_store
            else None
        )
        if resume_job and resume_job.get("task_id"):
            operation = _veo_operation_ref_from_name(str(resume_job["task_id"]))
            logger.info(f"↩️ Veo resume operation={operation.name} ({scene}-{shot})")
        elif resume_job and resume_job.get("status") in {"submitting", "submitted", "pending", "in_progress", "poll_timeout"}:
            raise RuntimeError(
                "Найдена неоднозначная Veo provider job без task_id; "
                "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
            )
        else:
            if not start_path or not os.path.exists(start_path):
                raise ValueError(f"Start image not found: {start_path}")

            with open(start_path, "rb") as f:
                start_bytes = f.read()

            end_bytes = None
            if end_path and os.path.exists(end_path):
                with open(end_path, "rb") as f:
                    end_bytes = f.read()

            config_params = {
                "number_of_videos": 1,
                "aspect_ratio": _VEO_ASPECT_RATIO,
                "resolution": _VEO_RESOLUTION,
            }

            if end_bytes:
                config_params["last_frame"] = types.Image(image_bytes=end_bytes, mime_type="image/png")

            if job_store:
                claim = job_store.claim_submitting_job(
                    shot_key,
                    input_hash,
                    {
                        "model": _VEO_MODEL_NAME,
                        "prompt_hash": prompt_hash,
                        "source_image_hashes": source_image_hashes,
                        "output_path": str(video_path),
                        "error": None,
                    },
                )
                if not claim.get("claimed"):
                    raise RuntimeError(
                        "Найдена неоднозначная Veo provider job без task_id; "
                        "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
                    )

            try:
                operation = client.models.generate_videos(
                    model=_VEO_MODEL_NAME,
                    prompt=prompt,
                    image=types.Image(image_bytes=start_bytes, mime_type="image/png"),
                    config=types.GenerateVideosConfig(**config_params)
                )
            except Exception as submit_error:
                raise _VeoSubmitUnknownError(str(submit_error)) from submit_error

            logger.info(f"⏳ Veo started: {operation.name} ({scene}-{shot})")
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "task_id": operation.name,
                        "status": "submitted",
                        "provider_status": "submitted",
                        "error": None,
                    },
                    "submitted_at",
                )

        operation = _wait_for_veo_operation(
            client,
            operation,
            on_poll=(
                (lambda op: _record_veo_poll_update(job_store, shot_key, input_hash, op))
                if job_store
                else None
            ),
        )
        video_uri, video_bytes = _extract_veo_video(operation, scene, shot)

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "task_id": operation.name,
                    "status": "completed",
                    "provider_status": "done",
                    "video_url": video_uri,
                    "video_url_requires_auth": bool(video_uri and not use_vertex and api_key),
                    "error": None,
                },
                "completed_at",
            )
            job_store.update_job(shot_key, input_hash, {"status": "downloading", "error": None}, None)

        try:
            _save_or_download_veo_video(
                video_path=video_path,
                video_uri=video_uri,
                video_bytes=video_bytes,
                api_key=api_key,
                use_vertex=use_vertex,
            )
        except Exception as download_error:
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"status": "download_failed", "error": str(download_error), "video_url": video_uri},
                    "failed_at",
                )
            raise

        if _is_non_empty_file(video_path):
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {
                        "status": "downloaded",
                        "task_id": operation.name,
                        "video_url": video_uri,
                        "video_url_requires_auth": bool(video_uri and not use_vertex and api_key),
                        "output_path": str(video_path),
                        "error": None,
                    },
                    "downloaded_at",
                )
            return {
                "success": True,
                "video_path": video_path,
                "scene": scene,
                "shot": shot,
                "task_id": operation.name,
            }
        raise RuntimeError("Empty file after download/save")

    except Exception as e:
        if job_store and input_hash:
            current_job = job_store.find_current_job(shot_key, input_hash)
            if current_job and current_job.get("status") == "download_failed":
                status = "download_failed"
            elif (
                current_job
                and current_job.get("status") == "submitting"
                and not current_job.get("task_id")
                and not isinstance(e, _VeoLedgerUpdateError)
            ):
                status = "submitting"
            elif isinstance(e, (_VeoPollTimeoutError, _VeoLedgerUpdateError)):
                status = "poll_timeout"
            else:
                status = "failed"
            job_store.update_job(shot_key, input_hash, {"status": status, "error": str(e)}, "failed_at")
        return {
            "success": False,
            "error": str(e),
            "scene": scene,
            "shot": shot
        }


def _record_veo_poll_update(
    job_store: _ProviderJobStore,
    shot_key: str,
    input_hash: str,
    operation: Any,
) -> None:
    provider_status = "done" if getattr(operation, "done", False) else "running"
    job_store.update_job(
        shot_key,
        input_hash,
        {"provider_status": provider_status, "error": str(getattr(operation, "error", "") or "") or None},
        "polled_at",
    )


def _wait_for_veo_operation(
    client: Any,
    operation: Any,
    max_wait_time: int = 600,
    on_poll: Optional[Any] = None,
) -> Any:
    start_time = time.time()
    while not getattr(operation, "done", False):
        if time.time() - start_time >= max_wait_time:
            task_id = str(getattr(operation, "name", ""))
            logger.error(f"⏰ Превышено время ожидания Veo ({max_wait_time}s) для {task_id}")
            raise _VeoPollTimeoutError(f"Превышено время ожидания Veo ({max_wait_time}s)", task_id)
        time.sleep(5)
        try:
            operation = client.operations.get(operation)
        except Exception as e:
            logger.warning(f"⚠️ Veo: сбой опроса операции {getattr(operation, 'name', '')}, повтор: {e}")
            continue
        if on_poll:
            try:
                on_poll(operation)
            except Exception as exc:
                raise _VeoLedgerUpdateError(str(exc)) from exc

    if on_poll:
        try:
            on_poll(operation)
        except Exception as exc:
            raise _VeoLedgerUpdateError(str(exc)) from exc
    if getattr(operation, "error", None):
        raise RuntimeError(str(operation.error))
    return operation


def _extract_veo_video(operation: Any, scene: Any, shot: Any) -> tuple[Optional[str], Optional[bytes]]:
    logger.debug(f"Operation Response for {scene}-{shot}: {operation.response}")

    if (
        hasattr(operation.response, 'rai_media_filtered_count')
        and operation.response.rai_media_filtered_count
        and operation.response.rai_media_filtered_count > 0
    ):
        logger.warning(
            f"⚠️ Video generation filtered by safety settings. "
            f"Reasons: {operation.response.rai_media_filtered_reasons}"
        )
        raise RuntimeError("Video filtered by safety settings")

    if not hasattr(operation.response, 'generated_videos') or not operation.response.generated_videos:
        raise RuntimeError(f"No videos in response: {operation.response}")

    video_obj = operation.response.generated_videos[0].video
    video_uri = video_obj.uri
    video_bytes = video_obj.video_bytes

    if not video_uri and not video_bytes:
        raise RuntimeError(f"No video URI or bytes. Response item: {operation.response.generated_videos[0]}")
    return video_uri, video_bytes


def _save_or_download_veo_video(
    video_path: str,
    video_uri: Optional[str],
    video_bytes: Optional[bytes],
    api_key: Optional[str],
    use_vertex: bool,
) -> None:
    output = Path(video_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")

    try:
        if video_bytes:
            logger.info(f"💾 Saving Veo video from bytes to {video_path}...")
            with open(tmp_path, 'wb') as f:
                f.write(video_bytes)
                f.flush()
                os.fsync(f.fileno())
        else:
            logger.info(f"⬇️ Downloading Veo video from {_sanitize_url_for_log(str(video_uri or ''))}...")

            download_headers = {}
            if not use_vertex and api_key:
                download_headers["x-goog-api-key"] = api_key

            resp = requests.get(video_uri, headers=download_headers, stream=True, timeout=(30, 600))
            if resp.status_code != 200:
                raise RuntimeError(f"Download failed: {resp.status_code} {_sanitize_url_for_log(str(video_uri or ''))}")

            with open(tmp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())

        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise RuntimeError("Empty file after download/save")
        os.replace(tmp_path, output)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

if __name__ == "__main__":
    # Тест
    print("🧪 Тест video_generator_veo_tool")
    if not os.getenv("GEMINI_API_KEY"):
        print("❌ GEMINI_API_KEY required")
    else:
        # Можно добавить тестовый вызов
        # res = video_generator_veo_tool(session_id="test", project_id="bubr", enable=True)
        # print(json.dumps(res, indent=2, ensure_ascii=False))
        print("ℹ️ Запустите с реальным project_id для теста")
