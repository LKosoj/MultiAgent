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
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from agent_command import model_hard

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

# Импорт общих функций из модуля common
# Только те функции, которые реально используются в этом модуле
from custom_tools.storybook.video_generator_common import (
    parse_duration_seconds_from_timing,
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

# Импорт для API
try:
    from utils import translate_prompts_in_items
except ImportError:
    translate_prompts_in_items = None
    logger.warning("⚠️ Модуль utils.translate_prompts_in_items не найден")

# MiniMax-Hailuo-02 поддерживает только фиксированные длительности видео
_MM_SUPPORTED_DURATIONS = (6, 10)
_MM_PROVIDER_NAME = "minimax"
_MM_MODEL_NAME = "MiniMax-Hailuo-02"
_MM_RESOLUTION = "768P"


class _MMPollTimeoutError(Exception):
    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


class _MMSubmitUnknownError(Exception):
    pass


class _MMSubmitFailedError(Exception):
    pass


class _MMLedgerUpdateError(Exception):
    pass


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


def _snap_to_supported_duration_mm(duration: int) -> int:
    """Приводит длительность к ближайшему поддерживаемому MiniMax-Hailuo-02 значению."""
    return min(_MM_SUPPORTED_DURATIONS, key=lambda allowed: (abs(allowed - duration), allowed))


def video_generator_mm_tool(
    session_id: str,
    project_id: str,
    items: Any = None,
    max_concurrency: int = 2,
    enable: bool = False,
    seed: Optional[int] = None,
    language: str = 'en',
    force_update_prompts: bool = False,
    skip_prompt_enhancement: bool = False
) -> Dict[str, Any]:
    """
    Генерирует видео с использованием MiniMax API.
    Читает данные из файла /plots/storybooks/{project_id}/97_shots/shots.json
    
    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        items: Параметр игнорируется, данные читаются из shots.json
        project_id: Идентификатор проекта (обязателен для поиска shots.json)
        max_concurrency: Максимальное количество параллельных запросов
        enable: Если True, выполняет генерацию видео, иначе пропускает
        seed: Сид для генерации видео
        language: Язык генерации для корректного перевода промптов (по умолчанию 'en')
        force_update_prompts: Если True, принудительно обновляет video_prompt независимо от timestamp
        skip_prompt_enhancement: Если True, пропускает улучшение промпта LLM (только перевод без галлюцинаций)
    Returns:
        Словарь с результатами генерации видео для каждого кадра.
    """
    
    # Читаем данные из файла shots.json
    if not project_id:
        logger.error("❌ project_id обязателен для чтения shots.json")
        return {"status": "error", "message": "project_id обязателен", "results": []}
    
    shots_file_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
    
    # Проверяем, существует ли файл shots.json
    if not os.path.exists(shots_file_path):
        logger.warning(f"⚠️ Файл shots.json не найден: {shots_file_path}")
        return {"status": "success", "message": f"Файл shots.json не найден: {shots_file_path}", "results": []}
    
    # Читаем и парсим данные из shots.json
    try:
        with open(shots_file_path, 'r', encoding='utf-8') as f:
            shots_data = json.load(f)
        logger.info(f"📖 Загружены данные из {shots_file_path}")
    except Exception as e:
        logger.error(f"❌ Ошибка чтения shots.json: {e}")
        return {"status": "error", "message": f"Ошибка чтения shots.json: {e}", "results": []}
    
    # Извлекаем список кадров из shots.json
    if isinstance(shots_data, dict) and "items" in shots_data:
        items_list = shots_data.get("items", [])
    elif isinstance(shots_data, list):
        items_list = shots_data
    else:
        logger.error("❌ Неверная структура данных в shots.json")
        return {"status": "error", "message": "Неверная структура данных в shots.json", "results": []}
    
    if not items_list:
        logger.warning("⚠️ Список items в shots.json пуст")
        return {"status": "success", "message": "Список items в shots.json пуст", "results": []}
    
    # ЭТАП 1: Обновляем описания изображений
    logger.info("📝 Этап 1: Анализ и обновление описаний изображений")
    descriptions_updated = update_shots_with_descriptions(shots_file_path, items_list, force_update_prompts, skip_prompt_enhancement)
    
    if not enable:
        logger.info("🎬 Генерация видео MiniMax отключена (enable=False). Анализ изображений завершен.")
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
    
    # Получаем API ключ MiniMax для генерации видео
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        logger.error("❌ MINIMAX_API_KEY не найден в переменных окружения")
        return {"status": "error", "message": "MINIMAX_API_KEY не найден", "results": []}

    try:
        job_store = _ProviderJobStore(
            f"plots/storybooks/{project_id}/97_shots/provider_jobs.json",
            provider_name=_MM_PROVIDER_NAME,
        )
    except Exception as e:
        logger.error(f"❌ Ошибка чтения provider_jobs.json: {e}")
        return _error_result(f"Ошибка чтения provider_jobs.json: {e}")
    
    # ЭТАП 2: Фильтруем кадры, которые нужно конвертировать в видео
    logger.info("🎬 Этап 2: Подготовка START кадров для генерации видео")
    video_items = []
    seen_shots = set()  # Для дедупликации по scene_number + shot_number
    for item in items_list:
        shot_type = item.get("shot_type")
        video_prompt = item.get("video_prompt", "").strip()
        video_path = item.get("video_path")
        scene_number = item.get("scene_number", "?")
        shot_number = item.get("shot_number", "?")
        
        # Берем только START кадры для генерации видео
        if shot_type != "start":
            logger.debug(f"⏭️ Пропускаем не-start кадр {shot_type}: {scene_number}-{shot_number}")
            continue
        
        # Проверяем обязательные поля
        if not video_prompt or not video_path:
            logger.debug(f"⏭️ Пропускаем кадр без video_prompt или video_path: {scene_number}-{shot_number}")
            continue
        
        # Проверяем, существует ли уже непустое видео. При включенном ledger
        # пропускаем его через single-step, чтобы отметить output как downloaded.
        output_exists = _is_non_empty_file(video_path)
        if output_exists:
            logger.info(f"✅ Видео уже существует: {video_path}")

        video_dir = os.path.dirname(video_path)
        
        # 1) Пробуем взять явные пути изображений из item
        explicit_start = item.get("start_image")
        explicit_end = item.get("end_image")
        
        start_image = None
        end_image = None
        
        if explicit_start:
            if os.path.exists(explicit_start):
                start_image = explicit_start
            else:
                logger.debug(f"⏭️ Указанный start_image не найден: {explicit_start}")
                if not output_exists:
                    continue
        
        if explicit_end:
            if os.path.exists(explicit_end):
                end_image = explicit_end
            else:
                logger.debug(f"ℹ️ Указанный end_image не найден: {explicit_end}")
                end_image = None
        
        # 2) Если start не задан явно — ищем по шаблону в директории видео
        if not start_image:
            if not os.path.exists(video_dir):
                logger.debug(f"⏭️ Директория видео не существует: {video_dir}")
                if not output_exists:
                    continue
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
            if os.path.exists(start_path):
                start_image = start_path
                logger.debug(f"🖼️ Найдено start изображение: {start_pattern}")
            else:
                logger.debug(f"⏭️ Start изображение не найдено: {start_pattern}, пропускаем")
                if not output_exists:
                    continue
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
        logger.info(f"ℹ️ Нет START кадров для генерации видео MiniMax (проект: {project_id})")
        return {"status": "success", "message": "Нет START кадров для обработки", "results": []}
    
    logger.info(f"🎬 Начинаем генерацию видео MiniMax для {len(video_items)} уникальных START кадров (проект: {project_id})")
    
    # Логируем список кадров для отладки
    if video_items:
        shot_list = [f"{item.get('scene_number', '?')}-{item.get('shot_number', '?')}" for item in video_items]
        logger.info(f"📋 START кадры для генерации: {', '.join(shot_list)}")
    
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
                executor.submit(_generate_single_video_mm, item, session_id, seed, language, job_store, api_key): item
                for item in batch_items
            }
            
            # Обрабатываем результаты пакета
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                    results.append(result)
                    
                    if result.get("success"):
                        logger.info(f"✅ Видео MiniMax создано: {result.get('video_path', '')}")
                    else:
                        logger.error(f"❌ Ошибка создания видео MiniMax: {result.get('error', '')}")
                        
                except Exception as e:
                    logger.error(f"❌ Исключение при генерации видео MiniMax: {e}")
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
    
    logger.info(f"📊 Генерация MiniMax завершена: {successful}/{total} успешно")
    
    # Используем переданный язык генерации из пайплайна
    
    # Локализуем video_prompt в результатах если нужно
    if language != 'en':
        from utils import translate_prompts_in_items
        logger.info(f"🌍 Локализуем video_prompt на язык: {language}")
        for result in results:
            if result.get("success") and "item" in result:
                # Переводим только video_prompt
                result["item"] = translate_prompts_in_items(result["item"], language)

    # Синхронизируем изменения обратно в items в памяти
    sync_items_to_memory(items, items_list)

    stats = {
        "total": total,
        "successful": successful,
        "failed": total - successful
    }
    message = f"Сгенерировано {successful} из {total} видео MiniMax"
    if successful == total:
        return {
            "status": "success",
            "message": message,
            "results": results,
            "stats": stats,
        }
    return _error_result(message, results=results, stats=stats)


def _generate_single_video_mm(
    item: Dict[str, Any],
    session_id: str,
    seed: Optional[int],
    language: str = 'en',
    job_store: Optional[_ProviderJobStore] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Генерирует одно видео из изображений (start и опционально end) с использованием MiniMax API.
    """
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    video_path = item.get("video_path")
    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    shot_key = f"{scene_number}-{shot_number}"
    timing = item.get("timing", "00:00 - 00:06")
    input_hash: Optional[str] = None

    if language != 'en' and translate_prompts_in_items is not None:
        translated_item = translate_prompts_in_items(item, 'en')
        video_prompt = translated_item.get('video_prompt', item.get('video_prompt', ''))
    else:
        video_prompt = item.get('video_prompt', '')

    # Определяем длительность видео из timing и приводим к поддерживаемым MiniMax-Hailuo-02 значениям
    duration = _snap_to_supported_duration_mm(parse_duration_seconds_from_timing(timing))

    logger.info(f"🎥 Генерируем видео MiniMax для сцены {scene_number}, кадр {shot_number} длительность {duration}")
    if end_image:
        logger.info(f"📹 Используем start + end изображения для анимации MiniMax")
    else:
        logger.info(f"📹 Используем только start изображение MiniMax")

    try:
        if not video_path:
            raise ValueError("video_path is required")

        prompt_hash = _hash_text(str(video_prompt or ""))
        source_image_hashes = {
            "start_image": _hash_source_image(start_image),
            "end_image": _hash_source_image(end_image),
        }
        output_exists = _is_non_empty_file(str(video_path))
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
            and trusted_output_job.get("model") == _MM_MODEL_NAME
            and trusted_output_job.get("resolved_duration") == duration
            and (trusted_output_job.get("seed") == seed if trusted_output_job.get("seed") is not None or seed is not None else True)
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
                "file_id": trusted_output_job.get("file_id"),
                "error": None,
            }
        frame_types = ["first_frame"] + (["last_frame"] if end_image else [])
        input_hash = _build_input_hash(
            model_name=_MM_MODEL_NAME,
            prompt_hash=prompt_hash,
            source_image_hashes=source_image_hashes,
            requested_duration=duration,
            requested_width=0,
            requested_height=0,
            seed=seed,
            frame_types=frame_types,
            provider_name=_MM_PROVIDER_NAME,
        )

        if job_store:
            job_store.mark_stale_for_changed_input(shot_key, input_hash)
            existing_job = job_store.find_current_job(shot_key, input_hash)
            has_prior_job_for_shot = job_store.has_job_for_shot(shot_key)
            job_data = _new_provider_job(
                shot_key=shot_key,
                model=_MM_MODEL_NAME,
                prompt_hash=prompt_hash,
                source_image_hashes=source_image_hashes,
                input_hash=input_hash,
                output_path=str(video_path),
                resolved_size_params={"resolution": _MM_RESOLUTION},
                resolved_duration=duration,
                provider_name=_MM_PROVIDER_NAME,
            )
            job_data["seed"] = seed
            job_store.ensure_job(job_data)
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
                        "model": _MM_MODEL_NAME,
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
                "file_id": existing_job.get("file_id") if existing_job else None,
                "error": None,
            }

        if not start_image or not os.path.exists(start_image):
            raise ValueError(f"Start image not found: {start_image}")

        # Создаем директорию для видео
        os.makedirs(os.path.dirname(video_path), exist_ok=True)

        # Кодируем start изображение в base64 и формируем корректный data URI
        with open(start_image, "rb") as img_file:
            start_bytes = img_file.read()
            start_b64 = base64.b64encode(start_bytes).decode('utf-8')
        start_mime, _ = mimetypes.guess_type(start_image)
        if not start_mime:
            start_mime = "image/png"
        first_frame_data_uri = f"data:{start_mime};base64,{start_b64}"
        
        # Кодируем end изображение в base64 (если есть)
        last_frame_data_uri = None
        if end_image:
            with open(end_image, "rb") as img_file:
                end_bytes = img_file.read()
                end_b64 = base64.b64encode(end_bytes).decode('utf-8')
            end_mime, _ = mimetypes.guess_type(end_image)
            if not end_mime:
                end_mime = "image/png"
            last_frame_data_uri = f"data:{end_mime};base64,{end_b64}"
        
        # Длительность уже рассчитана выше
        
        api_key = api_key or os.getenv("MINIMAX_API_KEY")
        resume_job = (
            job_store.find_resumable_job(shot_key, input_hash, prompt_hash, source_image_hashes)
            if job_store
            else None
        )
        task_id = None
        file_id = None

        if resume_job and resume_job.get("task_id"):
            task_id = resume_job.get("task_id")
            file_id = resume_job.get("file_id")
            logger.info(f"↩️ MiniMax resume task_id={task_id} для сцены {scene_number}-{shot_number}")
        elif resume_job and resume_job.get("status") in {"submitting", "submitted", "pending", "in_progress", "poll_timeout"}:
            raise RuntimeError(
                "Найдена неоднозначная MiniMax provider job без task_id; "
                "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
            )
        else:
            old_task_id = (
                job_store.find_task_id_for_resubmit(shot_key, input_hash, prompt_hash, source_image_hashes)
                if job_store
                else None
            )
            old_payload = _query_video_generation_mm(old_task_id, api_key) if old_task_id else None
            old_status = old_payload.get("status") if old_payload else None
            if old_status == "Success" and old_payload.get("file_id"):
                task_id = old_task_id
                file_id = old_payload.get("file_id")
            else:
                if job_store:
                    claim = job_store.claim_submitting_job(
                        shot_key,
                        input_hash,
                        {
                            "model": _MM_MODEL_NAME,
                            "prompt_hash": prompt_hash,
                            "source_image_hashes": source_image_hashes,
                            "output_path": str(video_path),
                            "error": None,
                        },
                    )
                    if not claim.get("claimed"):
                        raise RuntimeError(
                            "Найдена неоднозначная MiniMax provider job без task_id; "
                            "повторная отправка заблокирована, чтобы не создать дубликат платной задачи"
                        )

                logger.debug(f"📤 Отправляем запрос в MiniMax для сцены {scene_number}-{shot_number}")
                task_id = _invoke_video_generation_mm(
                    video_prompt,
                    first_frame_data_uri,
                    last_frame_data_uri,
                    duration,
                    api_key,
                    seed,
                )
                if not task_id:
                    raise RuntimeError("Не удалось получить task_id от MiniMax API")

                if job_store:
                    job_store.update_job(
                        shot_key,
                        input_hash,
                        {
                            "task_id": task_id,
                            "status": "submitted",
                            "provider_status": "submitted",
                            "model": _MM_MODEL_NAME,
                            "prompt_hash": prompt_hash,
                            "source_image_hashes": source_image_hashes,
                            "output_path": str(video_path),
                            "error": None,
                        },
                        "submitted_at",
                    )

        logger.info(f"📋 MiniMax task_id: {task_id} для сцены {scene_number}-{shot_number}")

        if not file_id:
            file_id = _wait_for_video_completion_mm(
                task_id,
                session_id,
                api_key,
                on_poll=(
                    (lambda payload: _record_poll_update_mm(job_store, shot_key, input_hash, payload))
                    if job_store
                    else None
                ),
                raise_on_timeout=job_store is not None,
            )

        if not file_id:
            raise RuntimeError("Не удалось получить file_id видео от MiniMax")

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "task_id": task_id,
                    "file_id": file_id,
                    "status": "completed",
                    "provider_status": "Success",
                    "error": None,
                },
                "completed_at",
            )
            job_store.update_job(shot_key, input_hash, {"status": "downloading", "error": None}, None)

        try:
            download_url = _retrieve_download_url_mm(file_id, api_key)
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"video_url": download_url, "video_url_requires_auth": False},
                    None,
                )

            success = _download_url_mm(download_url, video_path)
            if not success:
                raise RuntimeError("Ошибка скачивания видео MiniMax")
        except Exception as download_error:
            if job_store:
                job_store.update_job(
                    shot_key,
                    input_hash,
                    {"status": "download_failed", "error": str(download_error)},
                    "failed_at",
                )
            raise

        if job_store:
            job_store.update_job(
                shot_key,
                input_hash,
                {
                    "status": "downloaded",
                    "task_id": task_id,
                    "file_id": file_id,
                    "video_url": download_url,
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
            "file_id": file_id,
            "error": None,
        }

    except Exception as e:
        if job_store and input_hash:
            if isinstance(e, _MMSubmitUnknownError):
                status = "submitting"
            elif isinstance(e, (_MMPollTimeoutError, _MMLedgerUpdateError)):
                status = "poll_timeout"
            else:
                status = "failed"
            current_job = job_store.find_current_job(shot_key, input_hash)
            if current_job and current_job.get("status") == "download_failed":
                status = "download_failed"
            job_store.update_job(shot_key, input_hash, {"status": status, "error": str(e)}, "failed_at")
        logger.error(f"❌ Исключение при генерации видео MiniMax сцена {scene_number}-{shot_number}: {e}")
        return {
            "success": False,
            "error": str(e),
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path
        }


def _invoke_video_generation_mm(prompt: str, first_frame_data_uri: str, last_frame_data_uri: Optional[str], duration: int, api_key: str, seed: Optional[int]) -> Optional[str]:
    """
    Отправляет запрос на генерацию видео в MiniMax API.
    
    Args:
        prompt: Текстовый промпт для видео
        start_image_data: Base64 данные первого кадра
        end_image_data: Base64 данные последнего кадра (опционально)
        duration: Длительность видео в секундах
        api_key: API ключ MiniMax
        seed: Сид для генерации видео
    Returns:
        task_id или None в случае ошибки
    """
    try:
        url = "https://api.minimax.io/v1/video_generation"
        
        # Подготавливаем payload согласно документации MiniMax
        payload_data = {
            "model": _MM_MODEL_NAME,
            "prompt": prompt,
            "first_frame_image": first_frame_data_uri,
            "duration": duration,
            "resolution": _MM_RESOLUTION,
            "seed": seed
        }
        
        # Добавляем последний кадр если есть
        if last_frame_data_uri:
            payload_data["last_frame_image"] = last_frame_data_uri
        
        payload = json.dumps(payload_data)
        headers = {
            'authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }

        response = requests.request("POST", url, headers=headers, data=payload, timeout=60)
        
        if response.status_code != 200:
            raise _MMSubmitFailedError(f"Ошибка MiniMax API: {response.status_code} - {response.text}")
        
        result = response.json()
        task_id = result.get('task_id')
        
        if task_id:
            logger.info(f"📤 Задача генерации видео MiniMax отправлена успешно, task ID: {task_id}")
        if not task_id:
            raise _MMSubmitFailedError(f"Не получен task_id от MiniMax API: {result}")
        
        return task_id

    except _MMSubmitFailedError:
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке запроса в MiniMax: {e}")
        raise _MMSubmitUnknownError(f"Неизвестный исход MiniMax submit: {e}") from e


def _record_poll_update_mm(
    job_store: _ProviderJobStore,
    shot_key: str,
    input_hash: str,
    payload: Dict[str, Any],
) -> None:
    job_store.update_job(
        shot_key,
        input_hash,
        {
            "provider_status": payload.get("status") or "unknown",
            "error": payload.get("error"),
        },
        "polled_at",
    )


def _query_video_generation_mm(task_id: Optional[str], api_key: str) -> Optional[Dict[str, Any]]:
    if not task_id:
        return None
    url = f"https://api.minimax.io/v1/query/video_generation?task_id={task_id}"
    headers = {
        'authorization': 'Bearer ' + api_key
    }
    response = requests.request("GET", url, headers=headers, timeout=60)
    if response.status_code != 200:
        logger.error(f"❌ Ошибка проверки статуса MiniMax task {task_id}: {response.status_code} - {response.text}")
        return None
    return response.json()


def _wait_for_video_completion_mm(
    task_id: str,
    session_id: str,
    api_key: str,
    max_wait_time: int = 900,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
    raise_on_timeout: bool = False,
) -> Optional[str]:
    """
    Ожидает завершения генерации видео в MiniMax и возвращает file_id.
    
    Args:
        task_id: ID задачи в MiniMax
        session_id: ID сессии для логирования
        api_key: API ключ MiniMax
        max_wait_time: Максимальное время ожидания в секундах (15 минут)
        
    Returns:
        file_id видео или None в случае ошибки
    """
    start_time = time.time()
    check_interval = 10  # Проверяем каждые 10 секунд
    
    logger.info(f"⏳ Ожидаем генерацию видео MiniMax для task {task_id}")
    
    while time.time() - start_time < max_wait_time:
        try:
            result = _query_video_generation_mm(task_id, api_key)
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке статуса MiniMax task {task_id}: {e}")
            time.sleep(check_interval)
            continue

        if result is None:
            time.sleep(check_interval)
            continue
        if on_poll:
            try:
                on_poll(result)
            except Exception as exc:
                raise _MMLedgerUpdateError(str(exc)) from exc
        status = result.get('status')

        logger.debug(f"🔄 Статус MiniMax task {task_id}: {status}")

        if status == 'Success':
            file_id = result.get('file_id')
            if file_id:
                logger.info(f"✅ Видео MiniMax готово: {task_id}, file_id: {file_id}")
                return file_id
            logger.error(f"❌ Видео готово, но file_id не найден в ответе MiniMax: {result}")
            return None

        if status == 'Fail':
            logger.error(f"❌ Генерация видео MiniMax провалилась: {task_id}")
            return None

        if status in ['Preparing', 'Queueing', 'Processing']:
            if status == 'Preparing':
                logger.debug("...Подготовка MiniMax...")
            elif status == 'Queueing':
                logger.debug("...В очереди MiniMax...")
            elif status == 'Processing':
                logger.debug("...Генерация MiniMax...")
            time.sleep(check_interval)
            continue

        logger.warning(f"⚠️ Неизвестный статус MiniMax: {status} для task {task_id}")
        time.sleep(check_interval)
        continue
    
    logger.error(f"⏰ Превышено время ожидания для MiniMax task {task_id}")
    if raise_on_timeout:
        raise _MMPollTimeoutError(f"Превышено время ожидания для MiniMax task {task_id}", task_id)
    return None


def _retrieve_download_url_mm(file_id: str, api_key: str) -> str:
    """Получает свежий download_url для готового MiniMax file_id."""
    logger.info(f"⬇️ Получаем URL видео MiniMax: {file_id}")
    url = f"https://api.minimax.io/v1/files/retrieve?file_id={file_id}"
    headers = {
        'authorization': 'Bearer ' + api_key,
    }

    response = requests.request("GET", url, headers=headers, timeout=60)

    if response.status_code != 200:
        raise RuntimeError(f"Ошибка получения URL скачивания MiniMax: {response.status_code} - {response.text}")

    result = response.json()
    download_url = result.get('file', {}).get('download_url')

    if not download_url:
        raise RuntimeError(f"Не получен download_url от MiniMax API: {result}")

    logger.info(f"🔗 URL скачивания видео MiniMax: {_sanitize_url_for_log(download_url)}")
    return download_url


def _download_url_mm(download_url: str, output_path: str) -> bool:
    """Скачивает готовое видео MiniMax по download_url."""
    try:
        logger.info(f"⬇️ Скачиваем видео MiniMax: {os.path.basename(output_path)}")

        video_response = requests.get(download_url, timeout=120, stream=True)
        video_response.raise_for_status()

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output.with_name(f".{output.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
        try:
            with tmp_path.open('wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
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
            logger.info(f"✅ Видео MiniMax скачано: {output_path}")
            return True
        logger.error(f"❌ Файл не создался или пустой: {output_path}")
        return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка скачивания видео MiniMax {_sanitize_url_for_log(download_url)}: {e}")
        return False


def _fetch_video_result_mm(file_id: str, output_path: str, api_key: str) -> bool:
    """
    Скачивает готовое видео из MiniMax по file_id.

    Returns:
        True если скачивание успешно, False в противном случае
    """
    try:
        return _download_url_mm(_retrieve_download_url_mm(file_id, api_key), output_path)
    except Exception as e:
        logger.error(f"❌ Ошибка скачивания видео MiniMax {file_id}: {e}")
        return False
