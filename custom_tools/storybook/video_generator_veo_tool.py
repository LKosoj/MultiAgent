import os
import json
import logging
import time
import base64
import mimetypes
import requests
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
                    os.environ[key] = value
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
        print("DEBUG: project_id is missing")
        return {"status": "error", "message": "project_id обязателен", "results": []}
    
    print(f"DEBUG: Starting video generation for project: {project_id}, enable: {enable}")

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

    # Принудительно перезагружаем env variables для отладки
    load_env_file()

    # Логика инициализации клиента:
    # 1. Проверяем PROJECT_ID для Vertex AI (приоритет для last_frame)
    # 2. Иначе проверяем GEMINI_API_KEY
    
    # Используем дефолт из оригинального veo.py если в env нет
    project_id_vertex = os.getenv('GOOGLE_CLOUD_PROJECT', 'gen-lang-client-0611452273')
    location_vertex = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')
    api_key = os.getenv("GEMINI_API_KEY")
    
    # Флаг использования Vertex
    use_vertex = False
    
    # Если хотим last_frame (а мы хотим), то предпочтительно Vertex.
    # Проверяем, есть ли у нас PROJECT_ID.
    if project_id_vertex:
        print(f"DEBUG: Using Vertex AI with project: {project_id_vertex}")
        use_vertex = True
    elif api_key:
        print(f"DEBUG: Using Gemini API Key (Warning: last_frame may not work)")
    else:
        print("DEBUG: No credentials found (GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT)")
        return {"status": "error", "message": "Credentials not found", "results": []}

    # Подготовка задач
    video_items = []
    seen_shots = set()
    
    print(f"DEBUG: Processing {len(items_list)} items from shots.json")

    for item in items_list:
        if item.get("shot_type") != "start": 
            continue
        
        scene = item.get("scene_number")
        shot = item.get("shot_number")
        video_path = item.get("video_path")
        
        if not video_path or os.path.exists(video_path):
            print(f"DEBUG: Skip existing or invalid video path: {video_path}")
            continue
            
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
            print(f"DEBUG: Start image not found for {scene}-{shot}: {start_img}")
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
                executor.submit(_generate_single_video_veo, item, api_key, language, use_vertex, project_id_vertex, location_vertex): item 
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

    # Синхронизируем изменения обратно в items в памяти
    sync_items_to_memory(items, items_list)

    return {
        "status": "success",
        "message": f"Сгенерировано {successful} видео Veo",
        "results": results
    }

def _generate_single_video_veo(item: Dict[str, Any], api_key: Optional[str], language: str, use_vertex: bool, project_id: str, location: str) -> Dict[str, Any]:
    scene = item.get("scene_number")
    shot = item.get("shot_number")
    video_path = item.get("video_path")
    start_path = item.get("start_image")
    end_path = item.get("end_image")
    
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
        if use_vertex:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location
            )
        else:
            client = genai.Client(api_key=api_key)
        
        # Читаем файлы
        with open(start_path, "rb") as f:
            start_bytes = f.read()
            
        end_bytes = None
        if end_path and os.path.exists(end_path):
            with open(end_path, "rb") as f:
                end_bytes = f.read()

        # Конфигурация для Veo
        # Используем модель veo-3.1-fast-generate-preview по требованию
        
        model_name = 'veo-3.1-generate-preview'
        #model_name = 'veo-3.1-fast-generate-preview'
        
        config_params = {
            "number_of_videos": 1,
            "aspect_ratio": "16:9",
            "resolution": "720p"
        }
        
        if end_bytes:
            config_params["last_frame"] = types.Image(image_bytes=end_bytes, mime_type="image/png")
            
        # Запуск генерации
        operation = client.models.generate_videos(
            model=model_name,
            prompt=prompt,
            image=types.Image(image_bytes=start_bytes, mime_type="image/png"),
            config=types.GenerateVideosConfig(**config_params)
        )
        
        logger.info(f"⏳ Veo started: {operation.name} ({scene}-{shot})")
        
        # Поллинг
        while not operation.done:
            time.sleep(5)
            operation = client.operations.get(operation)
            
        if operation.error:
            return {
                "success": False,
                "error": str(operation.error),
                "scene": scene,
                "shot": shot
            }
            
        # Получение видео
        logger.info(f"DEBUG: Operation Response for {scene}-{shot}: {operation.response}")

        if hasattr(operation.response, 'rai_media_filtered_count') and operation.response.rai_media_filtered_count and operation.response.rai_media_filtered_count > 0:
             logger.warning(f"⚠️ Video generation filtered by safety settings. Reasons: {operation.response.rai_media_filtered_reasons}")
             return {"success": False, "error": "Video filtered by safety settings", "scene": scene, "shot": shot}

        if not hasattr(operation.response, 'generated_videos') or not operation.response.generated_videos:
             return {"success": False, "error": f"No videos in response: {operation.response}", "scene": scene, "shot": shot}

        video_obj = operation.response.generated_videos[0].video
        video_uri = video_obj.uri
        video_bytes = video_obj.video_bytes

        if not video_uri and not video_bytes:
             return {"success": False, "error": f"No video URI or bytes. Response item: {operation.response.generated_videos[0]}", "scene": scene, "shot": shot}
        
        os.makedirs(os.path.dirname(video_path), exist_ok=True)

        if video_bytes:
            logger.info(f"💾 Saving Veo video from bytes to {video_path}...")
            with open(video_path, 'wb') as f:
                f.write(video_bytes)
        else:
            logger.info(f"⬇️ Downloading Veo video from {video_uri[:50]}...")
            
            download_url = video_uri
            if not use_vertex and api_key:
                download_url = f"{video_uri}&key={api_key}"
                
            resp = requests.get(download_url, stream=True)
            if resp.status_code != 200:
                 return {"success": False, "error": f"Download failed: {resp.status_code} {resp.text}", "scene": scene, "shot": shot}
                 
            with open(video_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            return {"success": True, "video_path": video_path, "scene": scene, "shot": shot}
        else:
            return {"success": False, "error": "Empty file after download/save", "scene": scene, "shot": shot}

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "scene": scene,
            "shot": shot
        }

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
