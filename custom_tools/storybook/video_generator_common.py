"""
Общий функционал для всех инструментов генерации видео.
Содержит утилиты для работы с промптами, описаниями изображений и синхронизацией состояния.
"""

import json
import os
import logging
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_AITUNNEL_MODELS_URL = "https://api.aitunnel.ru/public/aitunnel/models/videos"
_AITUNNEL_MODELS_TIMEOUT_SECONDS = 60
_AITUNNEL_MODELS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_AITUNNEL_MODELS_CACHE_LOCK: threading.Lock = threading.Lock()


def _get_aitunnel_video_models(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    global _AITUNNEL_MODELS_CACHE

    # Fast-path: avoid lock acquisition on cache-hit (double-checked locking).
    if _AITUNNEL_MODELS_CACHE is not None and not force_refresh:
        return _AITUNNEL_MODELS_CACHE

    with _AITUNNEL_MODELS_CACHE_LOCK:
        if _AITUNNEL_MODELS_CACHE is not None and not force_refresh:
            return _AITUNNEL_MODELS_CACHE

        response = requests.get(_AITUNNEL_MODELS_URL, timeout=_AITUNNEL_MODELS_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise RuntimeError(
                f"AITUNNEL models endpoint failed: {response.status_code} - {response.text}"
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Неверный ответ публичного models endpoint: {payload}")

        _AITUNNEL_MODELS_CACHE = payload
        return payload


def _get_active_aitunnel_model_record(
    model_catalog: Dict[str, Dict[str, Any]], configured_model: str
) -> Optional[Dict[str, Any]]:
    config = model_catalog.get(configured_model)
    return config if isinstance(config, dict) else None


def safe_parse_json(data: Any) -> Any:
    """
    Безопасный парсинг JSON-данных с поддержкой markdown-блоков.
    
    Args:
        data: Данные для парсинга (строка или объект)
        
    Returns:
        Распарсенный объект или исходные данные
    """
    if isinstance(data, str):
        try:
            # Сначала пробуем напрямую парсить
            return json.loads(data)
        except json.JSONDecodeError:
            # Если не получилось, пробуем извлечь JSON из markdown
            try:
                from utils import extract_json_from_markdown
                json_str = extract_json_from_markdown(data)
                return json.loads(json_str)
            except Exception as e:
                logger.warning(f"⚠️ Не удалось распарсить JSON: {e}")
                return data
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при парсинге JSON: {e}")
            return data
    return data


def get_file_modification_time(file_path: str) -> Optional[float]:
    """
    Получает время модификации файла.
    
    Args:
        file_path: Путь к файлу
        
    Returns:
        Timestamp модификации или None если файл не существует
    """
    try:
        if os.path.exists(file_path):
            return os.path.getmtime(file_path)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить время модификации {file_path}: {e}")
    return None


def set_file_modification_time(file_path: str, timestamp: float) -> bool:
    """
    Устанавливает время модификации файла.
    
    Args:
        file_path: Путь к файлу
        timestamp: Новый timestamp
        
    Returns:
        True если успешно, False иначе
    """
    try:
        if os.path.exists(file_path):
            os.utime(file_path, (timestamp, timestamp))
            return True
    except Exception as e:
        logger.warning(f"⚠️ Не удалось установить время модификации {file_path}: {e}")
    return False


def safe_timestamp_compare(ts1: Optional[float], ts2: Optional[float]) -> bool:
    """
    Безопасное сравнение timestamp'ов.
    
    Args:
        ts1: Первый timestamp
        ts2: Второй timestamp
        
    Returns:
        True если ts1 < ts2, False иначе
    """
    if ts1 is None or ts2 is None:
        return True
    return ts1 < ts2


def generate_image_description(image_path: str) -> Optional[str]:
    """
    Генерирует описание изображения с помощью vision API.
    Использует расширенный набор типов анализа для получения детального описания.
    
    Args:
        image_path: Путь к изображению
        
    Returns:
        JSON-строка с описанием изображения или None в случае ошибки
    """
    try:
        from custom_tools.image_tools import analyze_image_tool
        from utils import extract_json_from_markdown
    except ImportError:
        logger.warning("⚠️ Анализ изображений недоступен - не найдены необходимые модули")
        return None
    
    try:
        logger.info(f"🔍 Комплексный анализ изображения: {os.path.basename(image_path)}")
        
        # Используем расширенный набор типов анализа для получения детального описания
        result = analyze_image_tool(
            image_input=image_path,
            analysis_types=[
                "Распознавание объектов", 
                "Анализ композиции", 
                "Анализ ракурса", 
                "Анализ фона", 
                "Определение цветов", 
                "Анализ содержимого", 
                "Анализ лиц", 
                "Определение настроения"
            ],
            input_type="path"
        )

        if result and not result.startswith("Ошибка:"):
            try:
                # Логируем исходный результат для диагностики
                logger.debug(f"🔍 Исходный результат анализа для {os.path.basename(image_path)}: {result[:200]}...")
                
                # Используем безопасную функцию парсинга JSON (с автоматическим извлечением из markdown)
                parsed_result = safe_parse_json(result)
                if parsed_result and isinstance(parsed_result, dict):
                    logger.info(f"✅ Структурированное описание получено для {os.path.basename(image_path)}")
                    # Возвращаем строковое представление для совместимости с остальным кодом
                    return json.dumps(parsed_result, ensure_ascii=False)
                else:
                    logger.warning(f"⚠️ Не удалось извлечь валидный JSON из результата анализа для {os.path.basename(image_path)}")
                    return None
                    
            except Exception as e:
                logger.error(f"❌ Ошибка при извлечении JSON для {os.path.basename(image_path)}: {e}")
                logger.debug(f"Результат анализа: {result[:300]}...")
                return None
        else:
            logger.error(f"❌ Ошибка анализа изображения: {result}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Исключение при анализе изображения {image_path}: {e}")
        return None


def enhance_video_prompt(
    original_prompt: str,
    start_description: str,
    end_description: Optional[str] = None,
    item_context: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Корректирует video_prompt под ФАКТИЧЕСКОЕ сгенерированное изображение.
    
    Проблема: изображение может отличаться от оригинального описания
    (другие цвета, освещение, фон). Video-генератор получает изображение + промпт,
    и промпт должен описывать то, что РЕАЛЬНО на изображении.
    
    Логика:
    1. Заменить визуальные описания на фактические (из анализа изображения)
    2. Сохранить оригинальное действие/движение камеры
    3. Добавить важные визуальные детали для video-генератора
    
    Args:
        original_prompt: Исходный video_prompt с задуманным действием
        start_description: JSON анализ ФАКТИЧЕСКОГО стартового изображения
        end_description: JSON анализ конечного изображения (опционально)
        item_context: Дополнительный контекст из item
    
    Returns:
        Скорректированный video_prompt или None при ошибке
    """
    import re
    
    try:
        from utils import call_openai_api
        from agent_command import model_hard
    except ImportError:
        logger.warning("⚠️ OpenAI API недоступен для улучшения video_prompt")
        return None
    
    # Валидация входных данных
    if not original_prompt or not original_prompt.strip():
        logger.warning("⚠️ Пустой original_prompt, пропускаем улучшение")
        return None
    
    try:
        # Парсим описание стартового изображения
        start_data = safe_parse_json(start_description)
        if not start_data or not isinstance(start_data, dict):
            logger.warning(f"⚠️ Не удалось разобрать JSON описания стартового изображения")
            return None
        
        # Парсим описание конечного изображения (ТОЛЬКО для деталей окружения)
        end_data = None
        if end_description:
            end_data = safe_parse_json(end_description)
            if end_data and isinstance(end_data, dict):
                logger.info("✅ END изображение будет использовано ТОЛЬКО для деталей окружения")
            else:
                end_data = None
        
        # ============================================================
        # ЭТАП 1: Извлекаем КЛЮЧЕВЫЕ ЭЛЕМЕНТЫ из оригинального промпта
        # ============================================================
        
        # Паттерны для извлечения ключевых элементов оригинала
        camera_patterns = [
            r'\b(static|stationary|locked off|locks off)\b',
            r'\b(dolly|pan|tilt|zoom|tracking|crane|orbit|push|pull)\s*(in|out|left|right|up|down)?\b',
            r'\b(slowly|quickly|steadily|gently|sharply|gradually)\b',
            r'\b(camera\s+\w+)\b',
        ]
        
        # Извлекаем движения камеры и темп из оригинала
        original_camera_elements = []
        original_lower = original_prompt.lower()
        for pattern in camera_patterns:
            matches = re.findall(pattern, original_lower, re.IGNORECASE)
            original_camera_elements.extend([m if isinstance(m, str) else m[0] for m in matches])
        
        # Определяем тип кадра: статичный или динамичный
        is_static = any(word in original_lower for word in ['static', 'stationary', 'locked', 'locks off', 'неподвижн', 'в покое', 'остаётся', 'сохраняет'])
        has_subject_action = any(word in original_lower for word in ['moves', 'moving', 'walks', 'running', 'extends', 'rises', 'falls', 'transforms'])
        
        logger.info(f"🔍 Анализ оригинала: static={is_static}, has_action={has_subject_action}, camera_elements={original_camera_elements[:3]}")
        
        # ============================================================
        # ЭТАП 2: Извлекаем ФАКТИЧЕСКОЕ ОПИСАНИЕ из анализа изображения
        # ============================================================
        
        # Собираем ключевые факты о том, что РЕАЛЬНО на изображении
        actual_image_facts = []
        
        # Объекты и содержимое — что реально видно
        if "content_analysis" in start_data:
            content = start_data["content_analysis"]
            if content:
                actual_image_facts.append(f"CONTENT: {str(content)}")
        
        if "object_recognition" in start_data:
            objects = start_data["object_recognition"]
            if objects:
                actual_image_facts.append(f"OBJECTS: {str(objects)}")
        
        # Люди и лица — одежда, позы, выражения
        if "face_analysis" in start_data:
            faces = start_data["face_analysis"]
            if faces:
                actual_image_facts.append(f"FACES/PEOPLE: {str(faces)}")
        
        # Освещение и цвета
        if "color_analysis" in start_data:
            colors = start_data["color_analysis"]
            if colors:
                actual_image_facts.append(f"LIGHTING/COLORS: {str(colors)}")
        
        # Фон и окружение
        if "background_analysis" in start_data:
            bg = start_data["background_analysis"]
            if bg:
                actual_image_facts.append(f"BACKGROUND: {str(bg)}")
        
        # Композиция и ракурс
        if "composition_analysis" in start_data:
            comp = start_data["composition_analysis"]
            if comp:
                actual_image_facts.append(f"COMPOSITION: {str(comp)}")
        
        if "angle_analysis" in start_data:
            angle = start_data["angle_analysis"]
            if angle:
                actual_image_facts.append(f"CAMERA ANGLE: {str(angle)}")
        
        # ============================================================
        # ЭТАП 3: Генерируем скорректированный промпт через LLM
        # ============================================================
        
        # Системный промпт для КОРРЕКТИРОВКИ video_prompt под фактическое изображение
        system_prompt = """You are a video prompt corrector. Video generator receives IMAGE + your PROMPT. Make the prompt match the ACTUAL image.

ALGORITHM (follow in order):

1. CAMERA: Copy camera clause from original prompt EXACTLY (type + direction + position + tempo).
   - DIRECTION LOCK: "descends" stays "descends" (NEVER → "ascends"). "pans right" stays "pans right".
   - TIMING LOCK: "touches/contacts" stays contact moment (NEVER → "approaching"). "above/hovering" stays pre-contact.

2. SUBJECT: Keep original action/movement verb. CORRECT visual appearance to match actual image:
   - Clothes/colors: replace with what image actually shows
   - Pose details: adjust to match actual pose in image
   - Keep quality keyword from original (natural/energetic/graceful/etc.)

3. ENVIRONMENT: Replace background description with what is ACTUALLY in the image.
   - Actual lighting direction, quality, color temperature
   - Actual background elements and atmosphere
   - Add micro-dynamics: mist drifts / reflections shimmer / leaves sway (NEVER "static background")

4. TEMPO: Keep from original prompt (slowly/quickly/steadily/etc.)

RULES:
- Output = single line, English only, no markdown
- KEEP all action/movement verbs from original — only correct VISUAL descriptions
- Do NOT invent new actions, objects, or characters not in original prompt or image
- Do NOT add readable text/logos/years unless visible in image
- Format: Camera clause → Subject (appearance from image + action from original) → Environment (from image) → Tempo

SELF-CHECK before output: every noun phrase either from original prompt OR from image analysis. If neither → delete."""

        # Формируем user_prompt
        image_analysis = "\n".join(actual_image_facts) if actual_image_facts else "No detailed analysis available"
        original_word_count = len(original_prompt.split())
        
        user_prompt = f"""ORIGINAL VIDEO PROMPT (intended description):
"{original_prompt}"

ACTUAL IMAGE ANALYSIS (what is REALLY in the generated image):
{image_analysis}

TASK:
1. Correct the prompt to match what is ACTUALLY in the image
2. Keep the original action/movement (camera movement, subject action)
3. Replace any visual descriptions that don't match the actual image
4. Output: single corrected prompt in English"""

        # Вызываем LLM с низкой temperature для детерминированности
        enhanced_prompt = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=500,  # Ограничиваем длину ответа
            temperature=0.1  # Минимальная креативность
        )
        
        # ============================================================
        # ЭТАП 4: Валидация и постобработка результата
        # ============================================================
        
        if not enhanced_prompt or not str(enhanced_prompt).strip():
            logger.warning("⚠️ LLM вернул пустой результат")
            return None
        
        cleaned = str(enhanced_prompt).strip()
        
        # Удаляем markdown и теги
        if cleaned.startswith("```"):
            cleaned = re.sub(r"```\w*\n?", "", cleaned).strip("`")
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = cleaned.strip('"\'')
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        
        # ============================================================
        # ЭТАП 5: Проверка на галлюцинации
        # ============================================================
        
        # Паттерны запрещенных слов (признаки галлюцинации)
        hallucination_patterns = [
            r'\btransforms?\s+into\b',
            r'\bevolves?\s+(from|into|to)\b',
            r'\btransitions?\s+to\b',
            r'\bbegins?\s+to\s+(rise|move|transform|change)\b',
            r'\bstarts?\s+to\s+(rise|move|transform|change)\b',
            r'\bslowly\s+(rises?|transforms?|changes?|evolves?)\b',
            r'\bgradually\s+(transforms?|changes?|evolves?)\b',
        ]
        
        cleaned_lower = cleaned.lower()
        
        # Если в оригинале НЕ было действия, но в результате появилось — это галлюцинация
        if is_static and not has_subject_action:
            for pattern in hallucination_patterns:
                if re.search(pattern, cleaned_lower):
                    logger.warning(f"⚠️ ГАЛЛЮЦИНАЦИЯ ОБНАРУЖЕНА: паттерн '{pattern}' в статичном кадре")
                    logger.warning(f"   Оригинал: {original_prompt}...")
                    logger.warning(f"   Результат: {cleaned}...")
                    # Возвращаем простой перевод оригинала без "улучшений"
                    return _simple_translate_prompt(original_prompt)
        
        # Проверка на чрезмерное увеличение размера
        result_word_count = len(cleaned.split())
        # original_word_count уже вычислен выше при формировании user_prompt
        
        if result_word_count > original_word_count * 5:
            logger.warning(f"⚠️ Промпт увеличился в {result_word_count/original_word_count:.1f}x раз ({original_word_count} → {result_word_count} слов)")
            # Возвращаем простой перевод
            return _simple_translate_prompt(original_prompt)
        
        logger.info(f"✅ Video prompt скорректирован под фактическое изображение: {original_word_count} → {result_word_count} слов")
        return cleaned
        
    except Exception as e:
        logger.error(f"❌ Ошибка при улучшении video_prompt: {e}")
        return None


def _simple_translate_prompt(prompt: str) -> Optional[str]:
    """
    Простой перевод промпта на английский без добавления деталей.
    Используется как fallback при обнаружении галлюцинаций.
    """
    # Если кириллицы нет — считаем, что промпт уже на английском (или в основном на английском),
    # и возвращаем его как есть (нормализуя пробелы) без обращения к API.
    import re
    if isinstance(prompt, str) and prompt.strip():
        if not re.search(r"[А-Яа-яЁё]", prompt):
            cleaned = prompt.strip().strip('"\'')
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return cleaned

    try:
        from utils import call_openai_api
        from agent_command import model_hard
    except ImportError:
        return None
    
    try:
        result = call_openai_api(
            prompt=f"Translate to English. Keep structure exactly. One line output:\n\n{prompt}",
            system_prompt="You are a translator. Translate the video prompt to English. Do NOT add any new actions or details. Keep the exact same structure and meaning.",
            model=model_hard,
            max_tokens=300,
            temperature=0.0
        )
        
        if result:
            import re
            cleaned = str(result).strip()
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
            cleaned = cleaned.strip('"\'')
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            logger.info("✅ Использован простой перевод (fallback)")
            return cleaned
    except Exception as e:
        logger.error(f"❌ Ошибка простого перевода: {e}")
    
    return None


# Fields this function ever writes onto a shot item (see the two phases below).
# Used to merge the in-memory item back onto the freshly re-read on-disk item:
# only these are overlaid, so a field written concurrently by another writer
# (blockout paths, duration_s/duration_source/duration_requested_s, a manual
# edit from the "Editor" tab, etc.) between this function's caller reading
# items_list and this function's write is not clobbered.
_UPDATE_SHOTS_WRITABLE_FIELDS = (
    "image_description",
    "image_description_timestamp",
    "original_video_prompt",
    "video_prompt",
    "video_prompt_updated_timestamp",
)


def update_shots_with_descriptions(
    shots_file_path: str,
    items_list: List[Dict[str, Any]],
    force_update: bool = False,
    skip_prompt_enhancement: bool = False
) -> int:
    """
    Обновляет описания изображений и video_prompt в shots.json для start и end кадров.
    Использует расширенную логику с поддержкой синхронизации времени файлов.
    
    Args:
        shots_file_path: Путь к файлу shots.json
        items_list: Список элементов кадров
        force_update: Если True, принудительно обновляет video_prompt независимо от timestamp
        skip_prompt_enhancement: Если True, пропускает улучшение video_prompt (только перевод)
        
    Returns:
        Количество обновленных элементов; 0 — обновлять было нечего;
        -1 — изменения посчитаны (уже применены к items_list в памяти), но
        запись shots.json на диск не удалась (см. журнал) — на диске
        остаётся прежняя версия файла.
    """
    if not os.path.exists(shots_file_path):
        logger.warning(f"⚠️ Файл {shots_file_path} не существует")
        return 0
    
    logger.info("📝 Проверяем и обновляем описания изображений...")

    changes_made = False
    updated_count = 0
    _changes_lock = threading.Lock()

    # ------------------------------------------------------------------
    # ФАЗА 1 (параллельная): Генерация описаний изображений через vision API
    # ------------------------------------------------------------------
    items_needing_description = []
    for item in items_list:
        shot_type = item.get("shot_type")
        if shot_type not in ["start", "end"]:
            continue
        output_path = item.get("output_path")
        if not output_path or not os.path.exists(output_path):
            continue
        image_mod_time = get_file_modification_time(output_path)
        if not image_mod_time:
            continue
        current_description = item.get("image_description")
        description_timestamp = item.get("image_description_timestamp")
        description_needs_update = (
            not current_description or
            not description_timestamp or
            safe_timestamp_compare(description_timestamp, image_mod_time)
        )
        if description_needs_update:
            items_needing_description.append((item, output_path, image_mod_time))
        else:
            logger.debug(f"✓ Описание актуально для {os.path.basename(output_path)}")

    def _generate_description_for_item(args):
        item, output_path, image_mod_time = args
        logger.info(f"🔄 Обновляем описание для {os.path.basename(output_path)}")
        new_description = generate_image_description(output_path)
        if new_description:
            with _changes_lock:
                item["image_description"] = new_description
                item["image_description_timestamp"] = image_mod_time
            logger.info(f"✅ Структурированное описание обновлено для {os.path.basename(output_path)}")
            return True
        else:
            logger.warning(f"⚠️ Не удалось получить описание для {os.path.basename(output_path)}")
            return False

    if items_needing_description:
        desc_workers = min(4, len(items_needing_description))
        logger.info(f"📝 Генерируем описания для {len(items_needing_description)} изображений (workers={desc_workers})")
        if desc_workers <= 1:
            for args in items_needing_description:
                if _generate_description_for_item(args):
                    changes_made = True
                    updated_count += 1
        else:
            with ThreadPoolExecutor(max_workers=desc_workers) as executor:
                futures = {executor.submit(_generate_description_for_item, args): args for args in items_needing_description}
                for future in as_completed(futures):
                    try:
                        if future.result():
                            with _changes_lock:
                                changes_made = True
                                updated_count += 1
                    except Exception as e:
                        item_args = futures[future]
                        logger.error(f"❌ Ошибка описания {os.path.basename(item_args[1])}: {e}")

    # ------------------------------------------------------------------
    # ФАЗА 2 (параллельная): Enhance video_prompt для start-кадров
    # ------------------------------------------------------------------
    # Собираем end_items lookup для быстрого поиска
    end_items_lookup: Dict[tuple, Dict[str, Any]] = {}
    for item in items_list:
        if item.get("shot_type") == "end":
            key = (item.get("scene_number"), item.get("shot_number"))
            end_items_lookup[key] = item

    start_items_for_enhance = []
    for item in items_list:
        if item.get("shot_type") != "start":
            continue
        output_path = item.get("output_path")
        if not output_path or not os.path.exists(output_path):
            continue

        current_video_prompt = item.get("video_prompt")
        video_prompt_timestamp = item.get("video_prompt_updated_timestamp")
        original_video_prompt = item.get("original_video_prompt")

        # Сохраняем original_video_prompt если его еще нет
        if not original_video_prompt and current_video_prompt:
            item["original_video_prompt"] = current_video_prompt
            original_video_prompt = current_video_prompt
            changes_made = True
            logger.info(f"💾 Сохранен original_video_prompt для {os.path.basename(output_path)}")

        scene_number = item.get("scene_number")
        shot_number = item.get("shot_number")
        end_item = end_items_lookup.get((scene_number, shot_number))

        # Вычисляем максимальный timestamp из start и end изображений
        try:
            max_image_timestamp = float(item.get("image_description_timestamp", 0)) if item.get("image_description_timestamp") else 0.0
            if end_item and end_item.get("image_description_timestamp"):
                end_timestamp = float(end_item.get("image_description_timestamp", 0)) if end_item["image_description_timestamp"] else 0.0
                max_image_timestamp = max(max_image_timestamp, end_timestamp)
        except (ValueError, TypeError):
            max_image_timestamp = 0.0

        # Синхронизация времени файлов
        files_timestamp_mismatch = False
        current_image_mod_time = get_file_modification_time(output_path)

        if current_image_mod_time and max_image_timestamp > 0:
            if safe_timestamp_compare(current_image_mod_time, max_image_timestamp):
                set_file_modification_time(output_path, max_image_timestamp)
                files_timestamp_mismatch = True
            if end_item:
                end_output_path = end_item.get("output_path")
                if end_output_path and os.path.exists(end_output_path):
                    end_image_mod_time = get_file_modification_time(end_output_path)
                    if end_image_mod_time and safe_timestamp_compare(end_image_mod_time, max_image_timestamp):
                        set_file_modification_time(end_output_path, max_image_timestamp)
                        files_timestamp_mismatch = True

        prompt_needs_update = (
            original_video_prompt and
            item.get("image_description") and
            item.get("image_description_timestamp") and
            (force_update or files_timestamp_mismatch or not video_prompt_timestamp or
             safe_timestamp_compare(video_prompt_timestamp, max_image_timestamp))
        )

        if prompt_needs_update:
            start_items_for_enhance.append((item, end_item, output_path, original_video_prompt, max_image_timestamp))

    def _enhance_item_prompt(args):
        item, end_item, output_path, original_video_prompt, max_image_timestamp = args
        scene_number = item.get("scene_number")
        shot_number = item.get("shot_number")
        logger.warning(f"🎬 Обновляем video_prompt для {os.path.basename(output_path)}")

        end_description = None
        if end_item and end_item.get("image_description"):
            end_description = end_item["image_description"]
            logger.info(f"🎭 Используем описание end кадра для {scene_number}-{shot_number}")

        item_context = {
            "camera_plan": item.get("camera_plan"),
            "timing": item.get("timing"),
            "scene_pacing": item.get("scene_pacing"),
            "spatial_changes_from_start": item.get("spatial_changes_from_start")
        }

        if skip_prompt_enhancement:
            logger.info(f"⏭️ skip_enhancement=True, используем только перевод для {os.path.basename(output_path)}")
            enhanced_prompt = _simple_translate_prompt(original_video_prompt)
        else:
            enhanced_prompt = enhance_video_prompt(
                original_video_prompt,
                item["image_description"],
                end_description,
                item_context
            )

        if enhanced_prompt:
            with _changes_lock:
                item["video_prompt"] = enhanced_prompt
                if max_image_timestamp > 0:
                    item["video_prompt_updated_timestamp"] = max_image_timestamp
                else:
                    import time as _time
                    item["video_prompt_updated_timestamp"] = _time.time()
            logger.info(f"✅ Video_prompt улучшен для {os.path.basename(output_path)}")
            return True
        else:
            logger.warning(f"⚠️ Не удалось улучшить video_prompt для {os.path.basename(output_path)}")
            return False

    if start_items_for_enhance:
        enh_workers = min(4, len(start_items_for_enhance))
        logger.info(f"🎬 Обновляем video_prompt для {len(start_items_for_enhance)} start-кадров (workers={enh_workers})")
        if enh_workers <= 1:
            for args in start_items_for_enhance:
                if _enhance_item_prompt(args):
                    changes_made = True
                    updated_count += 1
        else:
            with ThreadPoolExecutor(max_workers=enh_workers) as executor:
                futures = {executor.submit(_enhance_item_prompt, args): args for args in start_items_for_enhance}
                for future in as_completed(futures):
                    try:
                        if future.result():
                            with _changes_lock:
                                changes_made = True
                                updated_count += 1
                    except Exception as e:
                        item_args = futures[future]
                        logger.error(f"❌ Ошибка enhance {os.path.basename(item_args[2])}: {e}")
    
    # Сохраняем изменения в файл, если они были
    if changes_made:
        try:
            logger.info(f"💾 Сохраняем обновленные данные в {shots_file_path}")

            # M-7: атомарная кросс-процессная запись (flock + tmp + os.replace),
            # чтобы не биться с чекпойнтерами shots.json из других шагов пайплайна.
            # flock на sidecar `{path}.lock` (НЕ подменяется): флок на самом
            # shots_file_path бесполезен — os.replace меняет inode, второй процесс
            # флокнул бы новый inode (окно потерянного апдейта).
            os.makedirs(os.path.dirname(shots_file_path) or ".", exist_ok=True)
            lock_path = f"{shots_file_path}.lock"
            with open(lock_path, 'a+', encoding='utf-8') as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    # Загружаем существующие верхнеуровневые поля под блокировкой
                    existing_data = {}
                    try:
                        with open(shots_file_path, 'r', encoding='utf-8') as rf:
                            raw = rf.read()
                        if raw.strip():
                            loaded = json.loads(raw)
                            if isinstance(loaded, dict):
                                existing_data = loaded
                    except FileNotFoundError:
                        existing_data = {}
                    except Exception as read_err:
                        logger.warning(f"⚠️ Не удалось перечитать существующий shots.json: {read_err}")

                    # Обновляем items поэлементным слиянием (не заменой целиком),
                    # сохраняя остальные ключи верхнего уровня: между чтением
                    # items_list вызывающим кодом и этой записью другой писатель
                    # (blockout_renderer, ручная правка длительности/редактор)
                    # мог дописать в диск-версию элемента поля, которых нет в
                    # items_list, — полная замена items их бы стёрла.
                    if isinstance(existing_data, dict):
                        disk_items = existing_data.get("items") or []
                        if disk_items:
                            fresh_by_key: Dict[tuple, Dict[str, Any]] = {}
                            for it in items_list:
                                fresh_by_key[
                                    (it.get("scene_number"), it.get("shot_number"), it.get("shot_type"))
                                ] = it
                            merged_items = []
                            seen_keys = set()
                            for disk_item in disk_items:
                                key = (
                                    disk_item.get("scene_number"),
                                    disk_item.get("shot_number"),
                                    disk_item.get("shot_type"),
                                )
                                seen_keys.add(key)
                                fresh_item = fresh_by_key.get(key)
                                if fresh_item is None:
                                    merged_items.append(disk_item)
                                    continue
                                merged = dict(disk_item)
                                for field in _UPDATE_SHOTS_WRITABLE_FIELDS:
                                    if field in fresh_item:
                                        merged[field] = fresh_item[field]
                                merged_items.append(merged)
                            # Items present only in items_list (not yet on disk under
                            # this key) are appended as-is: nothing to merge onto.
                            for key, fresh_item in fresh_by_key.items():
                                if key not in seen_keys:
                                    merged_items.append(fresh_item)
                            existing_data["items"] = merged_items
                        else:
                            existing_data["items"] = items_list
                        shots_data = existing_data
                    else:
                        shots_data = {"items": items_list}

                    tmp = f"{shots_file_path}.{os.getpid()}.tmp"
                    try:
                        with open(tmp, 'w', encoding='utf-8') as wf:
                            json.dump(shots_data, wf, ensure_ascii=False, indent=2)
                        os.replace(tmp, shots_file_path)
                    except Exception:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        raise
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)

            logger.info(f"✅ Успешно обновлено {updated_count} элементов в {shots_file_path}")
            return updated_count
        except Exception as write_err:
            logger.error(f"❌ Не удалось записать обновленные данные в {shots_file_path}: {write_err}")
            # -1, а не 0: отличает "запись не удалась" от "обновлять было
            # нечего" (см. docstring) — вызывающие проверяют
            # `descriptions_updated < 0`, чтобы не перечитывать с диска
            # устаревшую версию поверх уже обновлённых в памяти items_list.
            return -1
    else:
        logger.info("ℹ️ Нет изменений для сохранения")
        return 0


def sync_items_to_memory(
    items: Any,
    items_list: List[Dict[str, Any]],
    keys_to_sync: Optional[List[str]] = None
) -> int:
    """
    Синхронизирует изменения из items_list обратно в объект items в памяти.
    
    Args:
        items: Исходный объект items (может быть списком или словарем с ключом "items")
        items_list: Список обновленных элементов
        keys_to_sync: Список ключей для синхронизации (по умолчанию стандартный набор)
        
    Returns:
        Количество синхронизированных элементов
    """
    if keys_to_sync is None:
        keys_to_sync = [
            "video_prompt",
            "video_prompt_updated_timestamp",
            "image_description",
            "image_description_timestamp",
            "original_video_prompt"
        ]
    
    try:
        items_to_update = None
        if isinstance(items, list):
            items_to_update = items
        elif isinstance(items, dict) and "items" in items and isinstance(items["items"], list):
            items_to_update = items["items"]
        
        if items_to_update is None:
            return 0
        
        # Создаем карту для быстрого поиска
        updated_map = {
            f"{it.get('scene_number')}-{it.get('shot_number')}": it
            for it in items_list
            if it.get("scene_number") is not None and it.get("shot_number") is not None
        }
        
        count_synced = 0
        for item in items_to_update:
            key = f"{item.get('scene_number')}-{item.get('shot_number')}"
            if key in updated_map:
                fresh = updated_map[key]
                for k in keys_to_sync:
                    if k in fresh:
                        item[k] = fresh[k]
                count_synced += 1
        
        if count_synced > 0:
            logger.info(f"🔄 Синхронизировано {count_synced} элементов обратно в память")
        
        return count_synced
        
    except Exception as e:
        logger.warning(f"⚠️ Не удалось синхронизировать элементы с памятью: {e}")
        return 0


def parse_duration_seconds_from_timing(timing: str, default: int = 5, minimum: int = 1) -> int:
    # Границ у длительности нет, кроме нижней (раздел 6.2 ТЗ): подгонка под набор
    # допустимых значений модели выполняется отдельно, общей функцией выбора
    # длительности (раздел 6.1 ТЗ), а не отсечением здесь.
    try:
        if " - " in timing:
            start_str, end_str = timing.split(" - ")
            start_seconds = _time_str_to_seconds(start_str.strip(), 0)
            end_seconds = _time_str_to_seconds(end_str.strip(), 0)
            duration = end_seconds - start_seconds
        elif timing.endswith("s"):
            duration = int(timing[:-1])
        elif ":" in timing:
            # Одиночная метка "MM:SS"/"HH:MM:SS" (не диапазон) — разбираем как длительность
            # напрямую, а не проваливаемся в default (дефект раздела 2.5 ТЗ).
            parsed = _time_str_to_seconds(timing.strip(), None)
            if parsed is None:
                logger.warning(
                    "⚠️ Не удалось разобрать timing %r как MM:SS/HH:MM:SS, используем default=%s",
                    timing, default,
                )
            duration = parsed if parsed is not None else default
        else:
            duration = int(timing)

        if duration < minimum:
            return minimum
        return duration
    except Exception as e:
        logger.warning(
            "⚠️ Не удалось разобрать timing %r (%s), используем default=%s",
            timing, e, default,
        )
        return default


def _time_str_to_seconds(time_str: str, default: Optional[int] = None) -> Optional[int]:
    try:
        parts = time_str.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        return default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Централизованный реестр возможностей видеомоделей (раздел 6.1 ТЗ).
#
# Источники значений — константы, которые раньше жили в самих инструментах:
#   video_generator_mm_tool.py (MiniMax): _MM_MODEL_NAME (строка 57),
#       _MM_SUPPORTED_DURATIONS (строка 55), _MM_RESOLUTION (строка 58);
#   video_generator.py (Kling): _KLING_MODEL_NAME (строка 55), _KLING_MODE
#       (строка 56), _KLING_ASPECT_RATIO (строка 57); supported_durations
#       [5, 10] выведен из правила «≤5 с → 5, иначе → 10» в
#       _parse_duration_from_timing() (строки 878-882);
#   video_generator_veo_tool.py (VEO): _VEO_MODEL_NAME (строка 80),
#       _VEO_ASPECT_RATIO (строка 81), _VEO_RESOLUTION (строка 82);
#       supported_durations [4, 6, 8] в коде не заданы — источник:
#       документация Google для veo-3.1-generate-preview при resolution=720p
#       без extend/reference images.
# Записи для video_generator_aitunnel_tool здесь нет: его возможности
# целиком приходят из сетевого каталога (_get_aitunnel_video_models()).
# ---------------------------------------------------------------------------
_VIDEO_MODEL_CAPABILITIES_REGISTRY: Dict[str, Dict[str, Any]] = {
    "video_generator_mm_tool": {
        "model": "MiniMax-Hailuo-02",
        "supported_durations": [6, 10],
        "supported_sizes": [],
        "supported_resolutions": ["768P"],
        "supported_aspect_ratios": [],
        "supported_modes": [],
        "resolution": "768P",
        "aspect_ratio": None,
        "mode": None,
    },
    "video_generator_tool": {
        "model": "kling-v2-1",
        "supported_durations": [5, 10],
        "supported_sizes": [],
        "supported_resolutions": [],
        "supported_aspect_ratios": ["16:9"],
        "supported_modes": ["pro"],
        "resolution": None,
        "aspect_ratio": "16:9",
        "mode": "pro",
    },
    "video_generator_veo_tool": {
        "model": "veo-3.1-generate-preview",
        "supported_durations": [4, 6, 8],
        "supported_sizes": [],
        "supported_resolutions": ["720p"],
        "supported_aspect_ratios": ["16:9"],
        "supported_modes": [],
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "mode": None,
    },
}

# Публикуемые поля возможностей (без resolution/aspect_ratio/mode — см. раздел 6.1 ТЗ:
# они нужны инструменту для сборки запроса, а не рендереру болванки).
_CAPABILITY_FIELDS = (
    "supported_durations",
    "supported_sizes",
    "supported_resolutions",
    "supported_aspect_ratios",
    "supported_modes",
)


def video_model_caps_warning(
    code: str,
    message: str,
    level: str = "warning",
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Формирует запись для списка `warnings` файла video_model_caps.json.

    Формат зафиксирован (раздел 6.1 ТЗ) и используется другими модулями,
    которые дописывают в этот же список: {"code", "level", "message", "details"}.
    """
    return {
        "code": code,
        "level": level,
        "message": message,
        "details": details if details is not None else {},
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_previous_video_caps(caps_file_path: str) -> Optional[Dict[str, Any]]:
    """Читает video_model_caps.json прежнего прогона для отката (source=cache)."""
    try:
        if not caps_file_path or not os.path.exists(caps_file_path):
            return None
        with open(caps_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _write_video_model_caps(caps_file_path: str, payload: Dict[str, Any]) -> None:
    """Атомарная запись video_model_caps.json: уникальный tmp-файл + os.replace."""
    try:
        directory = os.path.dirname(caps_file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{caps_file_path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, caps_file_path)
    except Exception as e:
        logger.error(f"❌ Не удалось записать {caps_file_path}: {e}")


def _resolve_aitunnel_capabilities(caps_file_path: str) -> Dict[str, Any]:
    """Ветка video_generator_aitunnel_tool: каталог по сети, иначе откат на кэш."""
    warnings: List[Dict[str, Any]] = []
    configured_model = (os.getenv("AITUNNEL_VIDEO_MODEL") or "").strip()
    record: Optional[Dict[str, Any]] = None
    # Причина неудачи — три разных сценария, тексты P14 не должны их путать.
    reason = "переменная окружения AITUNNEL_VIDEO_MODEL не задана"

    if configured_model:
        try:
            catalog = _get_aitunnel_video_models()
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить каталог видеомоделей AITUNNEL: {e}")
            reason = "каталог видеомоделей AITUNNEL недоступен"
        else:
            record = _get_active_aitunnel_model_record(catalog, configured_model)
            if record is None:
                reason = f"модель {configured_model!r} отсутствует в каталоге видеомоделей AITUNNEL"

    if record is not None:
        return {
            "model": configured_model,
            "supported_durations": list(record.get("supported_durations") or []),
            "supported_sizes": list(record.get("supported_sizes") or []),
            "supported_resolutions": list(record.get("supported_resolutions") or []),
            "supported_aspect_ratios": list(record.get("supported_aspect_ratios") or []),
            "supported_modes": list(record.get("supported_modes") or []),
            "source": "catalog",
            "resolved_at": _utc_now_iso(),
            "warnings": warnings,
        }

    cached = _read_previous_video_caps(caps_file_path)
    # Откат на кэш допустим только при непустом supported_durations в прежнем файле
    # (раздел 6.1 ТЗ): иначе файл, уже записанный ранее с пустым набором и
    # source=null, самоподдерживал бы себя как "cache" на каждом следующем прогоне.
    if cached is not None and cached.get("supported_durations"):
        resolved_at = cached.get("resolved_at")
        if not resolved_at:
            # Запасной вариант: в прежнем файле нет resolved_at — копировать
            # нечего, используем момент этой (неудачной) попытки.
            resolved_at = _utc_now_iso()
        warnings.append(video_model_caps_warning(
            "P14",
            f"Не удалось получить возможности видеомодели AITUNNEL из каталога ({reason}), "
            "использован набор возможностей из video_model_caps.json предыдущего прогона",
            details={"tool_name": "video_generator_aitunnel_tool", "configured_model": configured_model or None},
        ))
        return {
            "model": cached.get("model"),
            "supported_durations": list(cached.get("supported_durations") or []),
            "supported_sizes": list(cached.get("supported_sizes") or []),
            "supported_resolutions": list(cached.get("supported_resolutions") or []),
            "supported_aspect_ratios": list(cached.get("supported_aspect_ratios") or []),
            "supported_modes": list(cached.get("supported_modes") or []),
            "source": "cache",
            "resolved_at": resolved_at,
            "warnings": warnings,
        }

    warnings.append(video_model_caps_warning(
        "P14",
        f"Не удалось определить возможности видеомодели AITUNNEL ({reason}), а video_model_caps.json "
        "предыдущего прогона отсутствует или содержит пустой набор длительностей",
        details={"tool_name": "video_generator_aitunnel_tool", "configured_model": configured_model or None},
    ))
    return {
        "model": None,
        "supported_durations": [],
        "supported_sizes": [],
        "supported_resolutions": [],
        "supported_aspect_ratios": [],
        "supported_modes": [],
        "source": None,
        "resolved_at": _utc_now_iso(),
        "warnings": warnings,
    }


def resolve_effective_durations(
    supported_durations: List[int],
    allowed_durations: Optional[List[int]],
) -> "tuple[List[int], List[Dict[str, Any]]]":
    """Пересекает полный набор длительностей модели с ограничением проекта.

    Раздел 6.2 ТЗ, блок 4: значения проекта (``blockout_allowed_durations``),
    которых нет в наборе модели, отбрасываются (warning-запись со списком
    отброшенного). Если пересечение оказывается пустым — ограничение проекта
    игнорируется целиком и используется полный набор модели (error-запись).
    Пустой/не заданный ``allowed_durations`` означает «взять всё, что
    поддерживает модель» — пересечение не применяется.

    Возвращает (effective_durations, warnings) — само пересечение НЕ
    публикуется в video_model_caps.json (там всегда полный набор модели),
    warnings из этой функции подмешиваются в общий список.
    """
    full_set = list(supported_durations or [])
    if not allowed_durations:
        return full_set, []

    allowed_set = {int(v) for v in allowed_durations}
    intersection = [v for v in full_set if int(v) in allowed_set]

    if intersection:
        dropped = sorted(allowed_set - {int(v) for v in intersection})
        if dropped:
            return intersection, [video_model_caps_warning(
                "DURATION_ALLOWED_NARROWED",
                f"Значения blockout_allowed_durations {dropped} не поддерживаются моделью и отброшены",
                details={"dropped": dropped, "supported_durations": full_set},
            )]
        return intersection, []

    return full_set, [video_model_caps_warning(
        "DURATION_ALLOWED_EMPTY_INTERSECTION",
        "Пересечение blockout_allowed_durations с набором модели пусто, ограничение проекта "
        "проигнорировано целиком, использован полный набор модели",
        level="error",
        details={"allowed_durations": sorted(allowed_set), "supported_durations": full_set},
    )]


def append_video_model_caps_warnings(caps_file_path: str, new_warnings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """"Запись 2" video_model_caps.json (раздел 6.2 ТЗ, блок 2): дописывает
    предупреждения в уже существующий файл, не перезаписывая остальные поля.

    Атомарно, под тем же sidecar-локом, что и остальные писатели этого файла:
    flock на ``{caps_file_path}.lock``, чтение-изменение-запись, уникальный
    tmp-файл + os.replace (см. _merge_write_shots в screenplay_shots_generator.py).
    """
    if not new_warnings:
        return {}

    directory = os.path.dirname(caps_file_path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = f"{caps_file_path}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            payload = _read_previous_video_caps(caps_file_path)
            if payload is None:
                logger.warning(
                    "⚠️ append_video_model_caps_warnings: %s не найден, дописывать некуда",
                    caps_file_path,
                )
                return {}
            existing = payload.get("warnings")
            if not isinstance(existing, list):
                existing = []
            payload["warnings"] = existing + list(new_warnings)
            _write_video_model_caps(caps_file_path, payload)
            return payload
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def resolve_video_model_capabilities(
    tool_name: Optional[str],
    caps_file_path: str,
    allowed_durations: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Определяет возможности активной видеомодели и публикует их в caps_file_path.

    Никогда не бросает исключений; файл создаётся всегда (раздел 6.1 ТЗ).

    ``allowed_durations`` (опционально, раздел 6.2 ТЗ блок 4) — ограничение
    проекта (blockout_allowed_durations). Публикуемый ``supported_durations``
    всегда остаётся ПОЛНЫМ набором модели — пересечение не меняет схему
    возврата (проверяется тестом на точный набор ключей), а только добавляет
    предупреждения о сужении/пустом пересечении в общий список ``warnings``.
    Сам список эффективных длительностей для нормализации нужно получить
    отдельным вызовом resolve_effective_durations(...).
    """
    try:
        if tool_name == "video_generator_aitunnel_tool":
            resolved = _resolve_aitunnel_capabilities(caps_file_path)
        else:
            registry_entry = _VIDEO_MODEL_CAPABILITIES_REGISTRY.get(tool_name)
            if registry_entry is not None:
                resolved = {
                    "model": registry_entry["model"],
                    **{field: list(registry_entry[field]) for field in _CAPABILITY_FIELDS},
                    "source": "constant",
                    "resolved_at": _utc_now_iso(),
                    "warnings": [],
                }
            else:
                logger.warning(f"⚠️ Неизвестный tool_name видеогенератора: {tool_name!r}")
                resolved = {
                    "model": None,
                    "supported_durations": [],
                    "supported_sizes": [],
                    "supported_resolutions": [],
                    "supported_aspect_ratios": [],
                    "supported_modes": [],
                    "source": None,
                    "resolved_at": _utc_now_iso(),
                    "warnings": [video_model_caps_warning(
                        "P14",
                        f"Неизвестный tool_name видеогенератора: {tool_name!r}, возможности модели не определены",
                        details={"tool_name": tool_name},
                    )],
                }

        if allowed_durations:
            _, narrowing_warnings = resolve_effective_durations(
                resolved.get("supported_durations") or [], allowed_durations
            )
            if narrowing_warnings:
                resolved = {**resolved, "warnings": list(resolved.get("warnings") or []) + narrowing_warnings}

        payload = {"tool_name": tool_name, **resolved}
    except Exception as e:
        logger.error(f"❌ resolve_video_model_capabilities: непредвиденная ошибка: {e}")
        payload = {
            "tool_name": tool_name,
            "model": None,
            "supported_durations": [],
            "supported_sizes": [],
            "supported_resolutions": [],
            "supported_aspect_ratios": [],
            "supported_modes": [],
            "source": None,
            "resolved_at": _utc_now_iso(),
            "warnings": [video_model_caps_warning(
                "P14",
                f"Внутренняя ошибка при определении возможностей видеомодели: {e}",
                details={"tool_name": tool_name},
            )],
        }

    _write_video_model_caps(caps_file_path, payload)
    return payload
