import os
import json
import logging
import time
import base64
import mimetypes
import requests
from typing import Any, Dict, List, Optional
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
                    os.environ[key] = value

load_env_file()

logger = logging.getLogger(__name__)

# Импорт общих функций из модуля common
# Только те функции, которые реально используются в этом модуле
from custom_tools.storybook.video_generator_common import (
    update_shots_with_descriptions,
    sync_items_to_memory
)

# Импорт для API
try:
    from utils import translate_prompts_in_items
except ImportError:
    translate_prompts_in_items = None
    logger.warning("⚠️ Модуль utils.translate_prompts_in_items не найден")


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
        
        # Проверяем, существует ли уже видео
        if os.path.exists(video_path):
            logger.info(f"✅ Видео уже существует: {video_path}")
            continue
        
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
                executor.submit(_generate_single_video_mm, item, session_id, seed, language): item 
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
    
    return {
        "status": "success",
        "message": f"Сгенерировано {successful} из {total} видео MiniMax",
        "results": results,
        "stats": {
            "total": total,
            "successful": successful,
            "failed": total - successful
        }
    }


def _generate_single_video_mm(item: Dict[str, Any], session_id: str, seed: Optional[int], language: str = 'en') -> Dict[str, Any]:
    """
    Генерирует одно видео из изображений (start и опционально end) с использованием MiniMax API.
    """
    # Переводим video_prompt на английский перед API вызовом
    from utils import translate_prompts_in_items
    
    if language != 'en':
        translated_item = translate_prompts_in_items(item, 'en')
        video_prompt = translated_item.get('video_prompt', item.get('video_prompt', ''))
    else:
        video_prompt = item.get('video_prompt', '')
        
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    video_path = item.get("video_path")
    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    timing = item.get("timing", "00:00 - 00:06")
    
    # Определяем длительность видео из timing ДО логирования
    duration = _parse_duration_from_timing(timing)
    # Принудительно фиксируем длительность при необходимости
    duration = 6
    
    logger.info(f"🎥 Генерируем видео MiniMax для сцены {scene_number}, кадр {shot_number} длительность {duration}")
    if end_image:
        logger.info(f"📹 Используем start + end изображения для анимации MiniMax")
    else:
        logger.info(f"📹 Используем только start изображение MiniMax")
    
    try:
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
        
        # Подготавливаем запрос к MiniMax API
        api_key = os.getenv("MINIMAX_API_KEY")
        
        # Отправляем запрос на создание видео в MiniMax
        logger.debug(f"📤 Отправляем запрос в MiniMax для сцены {scene_number}-{shot_number}")
        
        task_id = _invoke_video_generation_mm(video_prompt, first_frame_data_uri, last_frame_data_uri, duration, api_key, seed)
        
        if not task_id:
            return {
                "success": False,
                "error": "Не удалось получить task_id от MiniMax API",
                "scene_number": scene_number,
                "shot_number": shot_number,
                "video_path": video_path
            }
        
        logger.info(f"📋 Получен task_id MiniMax: {task_id} для сцены {scene_number}-{shot_number}")
        
        # Ожидаем завершения генерации
        file_id = _wait_for_video_completion_mm(task_id, session_id, api_key)
        
        if not file_id:
            return {
                "success": False,
                "error": "Не удалось получить file_id видео от MiniMax",
                "scene_number": scene_number,
                "shot_number": shot_number,
                "video_path": video_path
            }
        
        # Скачиваем видео
        success = _fetch_video_result_mm(file_id, video_path, api_key)
        
        return {
            "success": success,
            "video_path": video_path if success else None,
            "scene_number": scene_number,
            "shot_number": shot_number,
            "task_id": task_id,
            "file_id": file_id if success else None,
            "error": None if success else "Ошибка скачивания видео MiniMax"
        }
        
    except Exception as e:
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
            "model": "MiniMax-Hailuo-02",
            "prompt": prompt,
            "first_frame_image": first_frame_data_uri,
            "duration": duration,
            "resolution": "768P",
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
            logger.error(f"❌ Ошибка MiniMax API: {response.status_code} - {response.text}")
            return None
        
        result = response.json()
        task_id = result.get('task_id')
        
        if task_id:
            logger.info(f"📤 Задача генерации видео MiniMax отправлена успешно, task ID: {task_id}")
        else:
            logger.error(f"❌ Не получен task_id от MiniMax API: {result}")
        
        return task_id
        
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке запроса в MiniMax: {e}")
        return None


def _wait_for_video_completion_mm(task_id: str, session_id: str, api_key: str, max_wait_time: int = 900) -> Optional[str]:
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
            url = f"https://api.minimax.io/v1/query/video_generation?task_id={task_id}"
            headers = {
                'authorization': 'Bearer ' + api_key
            }
            
            response = requests.request("GET", url, headers=headers, timeout=60)
            
            if response.status_code != 200:
                logger.error(f"❌ Ошибка проверки статуса MiniMax task {task_id}: {response.status_code} - {response.text}")
                time.sleep(check_interval)
                continue
            
            result = response.json()
            status = result.get('status')
            
            logger.debug(f"🔄 Статус MiniMax task {task_id}: {status}")
            
            if status == 'Success':
                file_id = result.get('file_id')
                if file_id:
                    logger.info(f"✅ Видео MiniMax готово: {task_id}, file_id: {file_id}")
                    return file_id
                else:
                    logger.error(f"❌ Видео готово, но file_id не найден в ответе MiniMax: {result}")
                    return None
                    
            elif status == 'Fail':
                logger.error(f"❌ Генерация видео MiniMax провалилась: {task_id}")
                return None
                
            elif status in ['Preparing', 'Queueing', 'Processing']:
                # Видео еще генерируется, ждем
                if status == 'Preparing':
                    logger.debug("...Подготовка MiniMax...")
                elif status == 'Queueing':
                    logger.debug("...В очереди MiniMax...")
                elif status == 'Processing':
                    logger.debug("...Генерация MiniMax...")
                    
                time.sleep(check_interval)
                continue
                
            else:
                logger.warning(f"⚠️ Неизвестный статус MiniMax: {status} для task {task_id}")
                time.sleep(check_interval)
                continue
                
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке статуса MiniMax task {task_id}: {e}")
            time.sleep(check_interval)
            continue
    
    logger.error(f"⏰ Превышено время ожидания для MiniMax task {task_id}")
    return None


def _fetch_video_result_mm(file_id: str, output_path: str, api_key: str) -> bool:
    """
    Скачивает готовое видео из MiniMax по file_id.
    
    Returns:
        True если скачивание успешно, False в противном случае
    """
    try:
        logger.info(f"⬇️ Скачиваем видео MiniMax: {os.path.basename(output_path)}")
        
        # Получаем URL для скачивания
        url = f"https://api.minimax.io/v1/files/retrieve?file_id={file_id}"
        headers = {
            'authorization': 'Bearer ' + api_key,
        }

        response = requests.request("GET", url, headers=headers, timeout=60)
        
        if response.status_code != 200:
            logger.error(f"❌ Ошибка получения URL скачивания MiniMax: {response.status_code} - {response.text}")
            return False
        
        result = response.json()
        download_url = result.get('file', {}).get('download_url')
        
        if not download_url:
            logger.error(f"❌ Не получен download_url от MiniMax API: {result}")
            return False
        
        logger.info(f"🔗 URL скачивания видео MiniMax: {download_url}")
        
        # Скачиваем видео по полученному URL
        video_response = requests.get(download_url, timeout=120, stream=True)
        video_response.raise_for_status()
        
        with open(output_path, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Проверяем, что файл создался и не пустой
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"✅ Видео MiniMax скачано: {output_path}")
            return True
        else:
            logger.error(f"❌ Файл не создался или пустой: {output_path}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка скачивания видео MiniMax {file_id}: {e}")
        return False


def _parse_duration_from_timing(timing: str) -> int:
    """
    Парсит строку timing и возвращает длительность в секундах.
    MiniMax поддерживает продолжительность от 1 до 10 секунд.
    
    Args:
        timing: Строка вида "00:00 - 00:06" или "6s"
        
    Returns:
        Длительность в секундах (1-10)
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
            duration = 6  # По умолчанию
        
        # MiniMax поддерживает от 1 до 10 секунд
        if duration < 1:
            return 1
        elif duration > 10:
            return 10
        else:
            return duration
            
    except Exception:
        return 6  # По умолчанию 6 секунд


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
    # Тестовый пример использования
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        print("❌ MINIMAX_API_KEY не установлен")
        exit(1)
    
    # Пример тестовых данных
    test_items = {
        "items": [
            {
                "scene_number": 1,
                "shot_number": 1,
                "video_prompt": "A beautiful sunset over the ocean with waves gently crashing on the shore",
                "video_path": "/tmp/test_video_01_01.mp4",
                "timing": "00:00 - 00:06",
                # Примечание: для реального использования нужны файлы:
                # "start_image": "/path/to/img_final_start_01_01.png",
                # "end_image": "/path/to/img_final_end_01_01.png"
            }
        ]
    }
    
    print("🧪 Тестируем MiniMax video generator...")
    result = video_generator_mm_tool(
        session_id="test_session",
        items=test_items,
        project_id="test_project",
        enable=True
    )
    
    print(f"📋 Результат: {json.dumps(result, indent=2, ensure_ascii=False)}")
