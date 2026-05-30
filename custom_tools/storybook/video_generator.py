import os
import json
import logging
import time
import base64
import requests
from typing import Any, Dict, List, Optional
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

# Импорты из проекта
try:
    from utils import call_openai_api, extract_json_from_markdown
except ImportError:
    call_openai_api = None
    extract_json_from_markdown = None
    logger.warning("⚠️ Модуль utils не найден, некоторые функции могут быть недоступны")

ak = os.getenv("KLING_API_KEY")
sk = os.getenv("KLING_API_SECRET_KEY")

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
    
    # Получаем API ключ
    api_key = os.getenv("KLING_API_KEY")
    if not api_key:
        logger.error("❌ KLING_API_KEY не найден в переменных окружения")
        return {"status": "error", "message": "KLING_API_KEY не найден", "results": []}
    
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
    
    # Фильтруем кадры, которые нужно конвертировать в видео
    video_items = []
    for item in items_list:
        video_prompt = item.get("video_prompt", "").strip()
        video_path = item.get("video_path")
        scene_number = item.get("scene_number", "?")
        shot_number = item.get("shot_number", "?")
        
        # Проверяем обязательные поля
        if not video_prompt or not video_path:
            logger.debug(f"⏭️ Пропускаем кадр без video_prompt или video_path: {scene_number}-{shot_number}")
            continue
        
        # Проверяем, существует ли уже видео
        if os.path.exists(video_path):
            logger.info(f"✅ Видео уже существует: {video_path}")
            continue
        
        # Анализируем директорию, где должно быть видео, для поиска изображений
        video_dir = os.path.dirname(video_path)
        if not os.path.exists(video_dir):
            logger.debug(f"⏭️ Директория видео не существует: {video_dir}")
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
            continue
        
        # Проверяем наличие end изображения (опционально)
        if os.path.exists(end_path):
            end_image = end_path
            logger.debug(f"🖼️ Найдено end изображение: {end_pattern}")
        
        # Добавляем найденные изображения в item
        item_copy = item.copy()
        item_copy["start_image"] = start_image
        item_copy["end_image"] = end_image
        
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
                executor.submit(_generate_single_video, item, session_id): item 
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
    
    return {
        "status": "success",
        "message": f"Сгенерировано {successful} из {total} видео",
        "results": results,
        "stats": {
            "total": total,
            "successful": successful,
            "failed": total - successful
        }
    }


def _generate_single_video(item: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """
    Генерирует одно видео из изображений (start и опционально end) с использованием Kling AI API.
    """
    start_image = item.get("start_image")
    end_image = item.get("end_image")
    video_prompt = item.get("video_prompt", "")
    video_path = item.get("video_path")
    scene_number = item.get("scene_number", "?")
    shot_number = item.get("shot_number", "?")
    timing = item.get("timing", "00:00 - 00:05")
    
    logger.info(f"🎥 Генерируем видео для сцены {scene_number}, кадр {shot_number}")
    if end_image:
        logger.info(f"📹 Используем start + end изображения для анимации")
    else:
        logger.info(f"📹 Используем только start изображение")

    if not ak or not sk:
        return {
            "success": False,
            "error": "KLING_API_KEY или KLING_API_SECRET_KEY не заданы",
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path,
        }

    try:
        # Создаем директорию для видео
        os.makedirs(os.path.dirname(video_path), exist_ok=True)

        # Кодируем start изображение в base64
        with open(start_image, "rb") as img_file:
            image_data = base64.b64encode(img_file.read()).decode('utf-8')

        # Кодируем end изображение в base64 (если есть)
        image_tail_data = None
        if end_image:
            with open(end_image, "rb") as img_file:
                image_tail_data = base64.b64encode(img_file.read()).decode('utf-8')

        # Определяем длительность видео из timing
        duration = _parse_duration_from_timing(timing)

        token = encode_jwt_token(ak, sk)
        # Подготавливаем запрос к Kling AI API
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "model_name": "kling-v2-1",
            "image": image_data,
            "image_tail": image_tail_data,  # Конечное изображение (если есть)
            "prompt": video_prompt,
            "negative_prompt": "blurry, distorted, low quality, artifacts, watermark",
            "cfg_scale": 0.5,
            "mode": "pro",  # standard или pro
            "duration": duration,  # 5 или 10 секунд
            "aspect_ratio": "16:9"  # 16:9, 9:16, 1:1
        }
        
        # Отправляем запрос на создание видео
        logger.debug(f"📤 Отправляем запрос в Kling AI для сцены {scene_number}-{shot_number}")

        response = requests.post(
            "https://api-singapore.klingai.com/v1/videos/image2video",
            headers=headers,
            json=payload,
            timeout=(30, 600)
        )
        
        if response.status_code != 200:
            error_msg = f"Ошибка API Kling: {response.status_code} - {response.text}"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "video_path": video_path
            }
        
        result = response.json()
        task_id = result.get("data", {}).get("task_id")
        
        if not task_id:
            error_msg = f"Не получен task_id от API: {result}"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "scene_number": scene_number,
                "shot_number": shot_number,
                "video_path": video_path
            }
        
        logger.info(f"📋 Получен task_id: {task_id} для сцены {scene_number}-{shot_number}")
        
        # Ожидаем завершения генерации
        video_url = _wait_for_video_completion(task_id, session_id, token)
        
        if not video_url:
            return {
                "success": False,
                "error": "Не удалось получить URL видео",
                "scene_number": scene_number,
                "shot_number": shot_number,
                "video_path": video_path
            }
        
        # Скачиваем видео
        success = _download_video(video_url, video_path)
        
        return {
            "success": success,
            "video_path": video_path if success else None,
            "scene_number": scene_number,
            "shot_number": shot_number,
            "task_id": task_id,
            "video_url": video_url if success else None,
            "error": None if success else "Ошибка скачивания видео"
        }
        
    except Exception as e:
        logger.error(f"❌ Исключение при генерации видео сцена {scene_number}-{shot_number}: {e}")
        return {
            "success": False,
            "error": str(e),
            "scene_number": scene_number,
            "shot_number": shot_number,
            "video_path": video_path
        }


def _wait_for_video_completion(task_id: str, session_id: str, token: str, max_wait_time: int = 600) -> Optional[str]:
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
            token = encode_jwt_token(ak, sk)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            response = requests.get(
                f"https://api-singapore.klingai.com/v1/videos/image2video/{task_id}",
                headers=headers,
                timeout=(30, 600)
            )
            
            # Если прошло больше 13 минут и статус не succeed, то возвращаем None
            if time.time() - start_time > 900 and response.status_code != 200:
                logger.error(f"❌ Ошибка проверки статуса task {task_id}: {str(response)}")
                return None
            

            result = response.json()
            #logger.info(f"🔄 Статус task {task_id}: {str(result)}")
            data = result.get("data", {})
            status = data.get("task_status")
            
            logger.debug(f"🔄 Статус task {task_id}: {status}")
            
            if status == "succeed":
                # Видео готово
                task_result = data.get("task_result", {})
                videos = task_result.get("videos", [])
                if videos and len(videos) > 0:
                    video_url = videos[0].get("url")
                    if video_url:
                        logger.info(f"✅ Видео готово: {task_id}")
                        return video_url
                
                logger.error(f"❌ Видео готово, но URL не найден в ответе: {result}")
                return None
                
            elif status == "failed":
                logger.error(f"❌ Генерация видео провалилась: {task_id}")
                return None
                
            elif status in ["submitted", "processing"]:
                # Видео еще генерируется, ждем
                time.sleep(check_interval)
                continue
                
            else:
                logger.warning(f"⚠️ Неизвестный статус: {status} для task {task_id}")
                time.sleep(check_interval)
                continue
                
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке статуса task {task_id}: {e}")
            time.sleep(check_interval)
            continue
    
    logger.error(f"⏰ Превышено время ожидания для task {task_id}")
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
        
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Проверяем, что файл создался и не пустой
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"✅ Видео скачано: {output_path}")
            return True
        else:
            logger.error(f"❌ Файл не создался или пустой: {output_path}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка скачивания видео {video_url}: {e}")
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
