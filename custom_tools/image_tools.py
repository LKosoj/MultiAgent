import requests
import random
import base64
import os
import asyncio
import time
from io import BytesIO
from pathlib import Path
from PIL import Image
from typing import Dict, List, Any, Optional
from agent_command import model_vision
from utils import extract_json_from_markdown
import logging
logger = logging.getLogger(__name__)

# Импорт функции для вызова OpenAI API
try:
    from utils import call_openai_api
except ImportError:
    call_openai_api = None

# Централизованная конфигурация типов анализа изображений
ANALYSIS_TYPES_CONFIG = {
    "Распознавание объектов": {
        "json_field": "objects_detected",
        "description": "Список обнаруженных объектов с координатами и уровнем уверенности",
        "prompt_instruction": "определи все объекты на изображении с их расположением и уровнем уверенности",
        "icon": "🎯"
    },
    "Анализ композиции": {
        "json_field": "composition_analysis",
        "description": "Анализ композиции изображения: правило третей, баланс, визуальный вес, линии",
        "prompt_instruction": "проанализируй композицию изображения, включая правило третей, баланс и визуальные линии",
        "icon": "🎨"
    },
        "Анализ ракурса": {
            "json_field": "perspective_analysis",
            "description": "Анализ ракурса, перспективы и точки съемки изображения",
            "prompt_instruction": "проанализируй ракурс и перспективу изображения (например, вид сверху, съемка на уровне глаз, макро)",
            "icon": "📐"
        },
        "Анализ фона": {
            "json_field": "background_analysis",
            "description": "Детальный анализ фона: окружение, объекты, его влияние на общую картину",
            "prompt_instruction": "детально проанализируй фон изображения, его окружение и объекты на нем",
            "icon": "🏞️"
        },
    "Определение цветов": {
        "json_field": "color_analysis", 
        "description": "Анализ цветовой палитры, доминирующих цветов, их сочетаний и гармонии",
        "prompt_instruction": "определи цветовую палитру, доминирующие цвета и их гармоничные сочетания",
        "icon": "🌈"
    },
    "Оценка качества": {
        "json_field": "quality_assessment",
        "description": "Оценка технического качества изображения: резкость, освещение, экспозиция",
        "prompt_instruction": "оцени техническое качество изображения включая резкость, освещение и экспозицию",
        "icon": "⭐"
    },
    "Анализ содержимого": {
        "json_field": "content_analysis",
        "description": "Детальный анализ содержимого и контекста изображения",
        "prompt_instruction": "проанализируй содержимое и контекст изображения в деталях",
        "icon": "📋"
    },
    "Анализ лиц": {
        "json_field": "face_analysis",
        "description": "Анализ лиц людей: количество, возраст, эмоции, позы",
        "prompt_instruction": "проанализируй лица людей на изображении включая эмоции и позы",
        "icon": "👤"
    },
    "Извлечение текста (OCR)": {
        "json_field": "text_extraction",
        "description": "Извлечение и распознавание текста на изображении",
        "prompt_instruction": "найди и извлеки весь видимый текст на изображении",
        "icon": "📝"
    },
    "Определение настроения": {
        "json_field": "mood_analysis",
        "description": "Определение общего настроения и атмосферы изображения",
        "prompt_instruction": "определи общее настроение и эмоциональную атмосферу изображения",
        "icon": "😊"
    }
}


def get_available_image_analysis_types() -> List[str]:
    """Возвращает список доступных типов анализа изображений для агентов"""
    return list(ANALYSIS_TYPES_CONFIG.keys())


def get_analysis_type_config(analysis_type: str) -> Dict[str, str]:
    """Возвращает конфигурацию для конкретного типа анализа"""
    return ANALYSIS_TYPES_CONFIG.get(analysis_type, {})


def build_dynamic_json_schema(selected_analysis_types: List[str]) -> Dict[str, str]:
    """Строит динамический JSON schema на основе выбранных типов анализа"""
    schema_fields = {
        "general_description": "Общее описание изображения"
    }
    
    for analysis_type in selected_analysis_types:
        config = get_analysis_type_config(analysis_type)
        if config and "json_field" in config:
            schema_fields[config["json_field"]] = config["description"]
    
    return schema_fields


def build_dynamic_system_prompt(selected_analysis_types: List[str]) -> str:
    """Строит динамический системный промпт на основе выбранных типов анализа"""
    schema_fields = build_dynamic_json_schema(selected_analysis_types)
    
    fields_description = ",\n    ".join([
        f'"{field}": "{desc}"' 
        for field, desc in schema_fields.items()
    ])
    
    return f"""Ты эксперт по анализу изображений. Анализируй изображения детально только по запрошенным аспектам.
Всегда отвечай на русском языке.

КРИТИЧЕСКИ ВАЖНО: Твой ответ должен быть ТОЛЬКО чистым валидным JSON без каких-либо markdown-блоков, тегов ```json или дополнительного текста.
Просто верни JSON-объект с этими полями:
{{
    {fields_description}
}}"""


def build_dynamic_analysis_prompt(selected_analysis_types: List[str], custom_prompt: str = None) -> str:
    """Строит динамический промпт для анализа на основе выбранных типов"""
    if custom_prompt:
        return custom_prompt
    
    if not selected_analysis_types:
        return "Проанализируй это изображение подробно по всем аспектам."
    
    # Создаем специфический промпт только для выбранных типов
    analysis_instructions = []
    for analysis_type in selected_analysis_types:
        config = get_analysis_type_config(analysis_type)
        if config and "prompt_instruction" in config:
            analysis_instructions.append(config["prompt_instruction"])
    
    if analysis_instructions:
        return f"Проанализируй это изображение: {', '.join(analysis_instructions)}. Дай подробный анализ на русском языке."
    else:
        return "Проанализируй это изображение подробно."


def extract_direct_statistics(analysis_data: dict, analysis_types: List[str]) -> dict:
    """
    Извлекает простую статистику напрямую из данных анализа без использования LLM
    
    Args:
        analysis_data: Распарсенные результаты анализа изображения
        analysis_types: Список выполненных типов анализа
    
    Returns:
        dict: Словарь с прямыми метриками
    """
    stats = {
        "completed_analyses": len(analysis_types),
        "analysis_completeness": f"{len(analysis_types)}/{len(ANALYSIS_TYPES_CONFIG)} типов"
    }
    
    # Считаем объекты напрямую
    if "objects_detected" in analysis_data:
        objects = analysis_data["objects_detected"]
        if isinstance(objects, list):
            stats["total_objects_found"] = len(objects)
        elif isinstance(objects, str) and objects.strip():
            # Если это строка, пытаемся посчитать упоминания объектов
            # Простая эвристика: считаем количество запятых + 1
            object_count = len([x.strip() for x in objects.split(',') if x.strip()])
            stats["total_objects_found"] = object_count if object_count > 1 else 1
        else:
            stats["total_objects_found"] = "не определено"
    
    # Анализируем цвета из текста
    if "color_analysis" in analysis_data:
        color_data = analysis_data["color_analysis"]
        # Преобразуем в строку если это словарь
        if isinstance(color_data, dict):
            color_text = str(color_data).lower()
        elif isinstance(color_data, str):
            color_text = color_data.lower()
        else:
            color_text = ""
        # Список основных цветов для поиска
        colors = [
            "красн", "син", "зелен", "желт", "оранж", 
            "фиолет", "розов", "сер", "черн", "бел",
            "коричнев", "золот", "серебр", "бирюзов"
        ]
        found_colors = sum(1 for color in colors if color in color_text)
        stats["main_colors_identified"] = found_colors if found_colors > 0 else "не определено"
    
    # Анализируем качество напрямую
    if "quality_assessment" in analysis_data:
        quality_data = analysis_data["quality_assessment"]
        # Преобразуем в строку если это словарь
        if isinstance(quality_data, dict):
            quality_text = str(quality_data).lower()
        elif isinstance(quality_data, str):
            quality_text = quality_data.lower()
        else:
            quality_text = ""
        # Простая эвристика для оценки качества
        positive_words = ["хорош", "отличн", "высок", "качеств", "четк", "ярк"]
        negative_words = ["плох", "низк", "размыт", "темн", "нечетк", "слаб"]
        
        positive_count = sum(1 for word in positive_words if word in quality_text)
        negative_count = sum(1 for word in negative_words if word in quality_text)
        
        if positive_count > negative_count:
            stats["quality_indicator"] = "высокое"
        elif negative_count > positive_count:
            stats["quality_indicator"] = "требует улучшения"
        else:
            stats["quality_indicator"] = "среднее"
    
    return stats


def generate_smart_summary(analysis_results_json: str, analysis_types: List[str]) -> str:
    """
    Генерирует оптимизированную умную сводку: прямые метрики + LLM анализ
    
    Args:
        analysis_results_json: JSON строка с результатами анализа
        analysis_types: Список выполненных типов анализа
    
    Returns:
        str: JSON строка с умной сводкой (даже в случае ошибки)
    """
    import json

    # --- Начальная структура для ответа ---
    direct_stats = {}
    llm_data = {
        "quality_assessment": {
            "overall_score": "N/A",
            "main_strengths": [],
            "areas_for_improvement": ["LLM анализ не удался"]
        },
        "key_insights": ["Анализ LLM не был выполнен"],
        "practical_recommendations": ["Повторите попытку позже"]
    }

    try:

        analysis_results_json = extract_json_from_markdown(analysis_results_json)
        # Логируем входные данные для отладки
        logger.info(f"Входные данные analysis_results_json: {repr(analysis_results_json[:200] if analysis_results_json else 'None/Empty')}")
        logger.info(f"Длина входных данных: {len(analysis_results_json) if analysis_results_json else 0}")
        
        # Проверяем, что данные не пустые
        if not analysis_results_json or not analysis_results_json.strip():
            logger.error("analysis_results_json пустой или содержит только пробелы")
            direct_stats = {"error": "Нет данных для анализа"}
            return json.dumps({"statistics": direct_stats, **llm_data}, ensure_ascii=False, indent=2)
        
        # Парсим исходные данные
        analysis_data = json.loads(analysis_results_json)
        
        # Шаг 1: Извлекаем простую статистику напрямую (без LLM)
        direct_stats = extract_direct_statistics(analysis_data, analysis_types)
        
        # --- Попытка выполнить LLM анализ ---
        if call_openai_api:
            try:
                from agent_command import model_code
                
                # Оптимизированный промпт - только для сложного анализа
                llm_prompt = f"""Проанализируй результаты анализа изображения и дай качественную оценку.
Результаты анализа: {analysis_results_json}
Сосредоточься только на качественном анализе. Верни JSON с этими полями:
{{
    "quality_assessment": {{
        "overall_score": "оценка от 1 до 10 на основе всех аспектов анализа",
        "main_strengths": ["2-3 главные сильные стороны изображения"],
        "areas_for_improvement": ["1-2 конкретные области для улучшения"]
    }},
    "key_insights": ["2-3 самые интересные и важные находки из анализа"],
    "practical_recommendations": ["2-3 практических совета по улучшению изображения"]
}}
Основывайся только на реальных данных анализа. Будь конкретным и полезным."""

                # Системный промпт для качественного анализа
                system_prompt = """Ты эксперт по качественному анализу изображений. 
Твоя задача - дать субъективную оценку и практические рекомендации на основе данных анализа.
Будь конкретным, полезным и основывайся только на предоставленных данных.
Отвечай на русском языке в указанном JSON формате."""

                # Вызываем LLM только для сложного анализа
                llm_response = call_openai_api(
                    prompt=llm_prompt,
                    system_prompt=system_prompt,
                    model=model_code,
                    max_tokens=8000,
                    temperature=0.2,
                    response_format={"type": "json_object"}
                )
                
                logger.info(f"LLM ответ: {llm_response}")
                # Если ответ получен и не пустой, пытаемся его распарсить
                if llm_response and llm_response.strip():
                    # Сначала проверяем, является ли ответ JSON объектом с метаданными API
                    try:
                        api_response = json.loads(llm_response)
                        if isinstance(api_response, dict) and "content" in api_response:
                            # Извлекаем content из ответа API
                            content = api_response["content"]
                            logger.info(f"Извлечен content из API ответа: {content[:100]}...")
                            # Теперь применяем extract_json_from_markdown к содержимому
                            json_content = extract_json_from_markdown(content)
                        else:
                            # Если это обычный JSON без "content", используем как есть
                            json_content = llm_response
                    except json.JSONDecodeError:
                        # Если это не JSON, применяем extract_json_from_markdown как есть
                        json_content = extract_json_from_markdown(llm_response)

                    try:
                        parsed_llm_data = json.loads(json_content)
                    except json.JSONDecodeError as parse_err:
                        logger.error(f"Не удалось разобрать JSON из ответа LLM: {parse_err}; ответ: {json_content[:300]!r}")
                        llm_data["areas_for_improvement"] = [f"Ответ LLM не является валидным JSON: {parse_err}"]
                        parsed_llm_data = None

                    # Обновляем llm_data только теми полями, которые есть в ответе LLM
                    # Сохраняем fallback значения для отсутствующих полей
                    if isinstance(parsed_llm_data, dict):
                        llm_data.update(parsed_llm_data)
                    elif parsed_llm_data is not None:
                        # Если LLM вернул не dict, оставляем fallback значения
                        llm_data["areas_for_improvement"] = ["Некорректный формат ответа LLM"]
                else:
                    logger.error("call_openai_api вернул None или пустой ответ для умной сводки")
                    logger.error(f"Параметры запроса: model={model_code}, max_tokens=8000, temperature=0.2")
                    logger.error(f"Тип модели: {type(model_code)}")
                    logger.error(f"Полученный ответ: {repr(llm_response)}")

            except Exception as llm_error:
                 import traceback
                 logger.error(f"Ошибка при генерации LLM сводки: {llm_error}")
                 logger.error(f"Полная трассировка ошибки: {traceback.format_exc()}")
                 logger.error(f"Ответ от call_openai_api: {repr(llm_response)}")
                 # llm_data уже содержит fallback значения
        else:
            # Если call_openai_api недоступен
            llm_data["areas_for_improvement"] = ["Функция вызова LLM недоступна"]

    except json.JSONDecodeError as e:
        direct_stats = {"error": f"Ошибка парсинга исходного JSON: {str(e)}"}
        logger.error(f"Ошибка парсинга исходного JSON: {str(e)}")
    except Exception as e:
        direct_stats = {"error": f"Неизвестная ошибка: {str(e)}"}
        logger.error(f"Неизвестная ошибка: {str(e)}")

    # --- Финальная сборка ответа ---
    # Гарантированно объединяем и возвращаем валидный JSON
    final_summary = {
        "statistics": direct_stats,
        **llm_data
    }
    
    return json.dumps(final_summary, ensure_ascii=False, indent=2)


def _resolve_openai_images_api_base() -> str:
    """Базовый URL OpenAI-совместимого API (ожидается суффикс /v1). Из окружения, без хардкода хоста."""
    return (os.getenv("OPENAI_API_BASE_DB") or "").strip().rstrip("/")


def _resolve_openai_images_api_key() -> str:
    """Ключ Bearer: OPENAI_API_KEY_DB; при отсутствии — VSEGPT_API_KEY (миграция со старых деплоев)."""
    return (os.getenv("OPENAI_API_KEY_DB") or os.getenv("VSEGPT_API_KEY") or "").strip()


def _images_generations_url() -> Optional[str]:
    base = _resolve_openai_images_api_base()
    if not base:
        return None
    return f"{base}/images/generations"


def _images_edits_url() -> Optional[str]:
    base = _resolve_openai_images_api_base()
    if not base:
        return None
    return f"{base}/images/edits"


def _guess_image_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def _b64_from_images_api_item(item: Dict[str, Any]) -> Optional[str]:
    """Достаёт base64 из элемента data[] ответа POST .../images/generations или .../images/edits.

    Некоторые шлюзы возвращают b64_json, другие — url вида data:image/...;base64,...
    при том же request с response_format=b64_json.
    """
    if not isinstance(item, dict):
        return None
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        return b64
    url_val = item.get("url")
    if isinstance(url_val, str) and "base64," in url_val:
        return url_val.split("base64,", 1)[1]
    return None


def generate_image_tool(
    prompt: str,
    session_id: str,
    number: int,
    negative_prompt: str = "",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Генерация изображения (txt2img): только TEXT2IMAGE_MODEL, POST .../images/generations.

    Требуются: OPENAI_API_BASE_DB, OPENAI_API_KEY_DB (или VSEGPT_API_KEY), TEXT2IMAGE_MODEL.

    Args:
        prompt: Строка запроса для генерации изображения СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ
        session_id: Идентификатор сессии
        number: Номер изображения
        negative_prompt: Негативный промпт (что НЕ должно быть в изображении) СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ
        width: Ширина изображения (по умолчанию 1920)
        height: Высота изображения (по умолчанию 1080)
    Returns:
        str: Имя файла, в который сохранено изображение или сообщение об ошибке
    """
    url = _images_generations_url()
    if not url:
        return (
            "Ошибка: не задан OPENAI_API_BASE_DB (базовый URL OpenAI-совместимого API, например http://host:port/v1)"
        )

    api_key = _resolve_openai_images_api_key()
    if not api_key:
        return (
            "Ошибка: не найден ключ API для изображений. Установите OPENAI_API_KEY_DB или VSEGPT_API_KEY"
        )

    model = (os.getenv("TEXT2IMAGE_MODEL") or "").strip()
    if not model:
        return "Ошибка: не задана модель TEXT2IMAGE_MODEL"

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "response_format": "b64_json",
        "size": f"{int(width)}x{int(height)}",
    }
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt

    session = requests.Session()
    try:
        session.headers.update(
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        )

        try:
            response = session.post(url, json=payload, timeout=120)
        except requests.exceptions.RequestException as e:
            return f"Ошибка сетевого запроса API изображений: {str(e)}"

        if response.status_code != 200:
            return f"Ошибка API изображений: {response.status_code}: {response.text}"

        try:
            response_json = response.json()
            item = response_json["data"][0]
            b64_data = _b64_from_images_api_item(item)
            if not b64_data:
                return f"Ошибка API изображений: неожиданный формат ответа: {item!r}"
        except (KeyError, IndexError, ValueError, TypeError) as e:
            return f"Ошибка API изображений: неожиданный формат ответа: {str(e)}"

        image_bytes = base64.b64decode(b64_data)
        image = Image.open(BytesIO(image_bytes))

        os.makedirs("plots", exist_ok=True)
        filename = f"plots/image{number}_{random.randint(100, 999)}_{session_id}.png"
        image.save(filename, format="PNG")

        return filename
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def edit_image_tool(prompt: str, image_path: str, session_id: str, number: int, negative_prompt: str = "", width: int = 1920, height: int = 1080, true_cfg_scale: float = 4.0, num_inference_steps: int = 50, seed: int = None, output_path: str = None) -> str:
    """Редактирует изображение по заданному prompt.
    
    Использует MCP сервер для редактирования изображений через ИИ
    
    Args:
        prompt: Текстовое описание желаемых изменений СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ
        image_path: Путь к исходному файлу изображения
        session_id: Идентификатор сессии
        number: Номер изображения
        negative_prompt: Негативный промпт (что НЕ должно быть в результате) СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ
        width: Ширина результата (512-2048)
        height: Высота результата (512-2048)
        true_cfg_scale: Сила следования промпту (1.0-10.0)
        num_inference_steps: Количество шагов обработки (10-100)
        seed: Seed для воспроизводимости (None для случайного)
        output_path: Путь для сохранения результата (опционально)
    Returns:
        str: Путь к файлу с отредактированным изображением или сообщение об ошибке
    """
    
    # Пробуем MCP сервер для редактирования файлов
    try:
        if output_path:
            # Если указан выходной путь, используем edit_image_file
            return _edit_file_via_direct_mcp_with_output(prompt, image_path, output_path, negative_prompt, width, height, true_cfg_scale, num_inference_steps, seed)
        else:
            # Иначе используем стандартную логику (генерируем имя файла)
            return _edit_file_via_direct_mcp(prompt, image_path, session_id, number, negative_prompt, width, height, true_cfg_scale, num_inference_steps, seed)
    except Exception as mcp_error:
        return f"Ошибка редактирования через MCP: {mcp_error}"

def _edit_file_via_direct_mcp(prompt: str, image_path: str, session_id: str, number: int, negative_prompt: str, width: int, height: int, true_cfg_scale: float, num_inference_steps: int, seed: int = None, output_path: str = None) -> str:
    """Прямое подключение к MCP серверу image-editor для работы с файлами.

    Если output_path не указан, генерирует имя файла из session_id и number.
    """
    async def call_mcp_edit_file():
        from mcp import StdioServerParameters
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client
        import json

        # Загружаем настройки из mcp_servers.json
        with open("mcp_servers.json", "r", encoding="utf-8") as f:
            config = json.load(f)

        # Находим настройки image-editor сервера
        edit_server_config = config["mcpServers"]["image-editor"]

        # Создаем параметры MCP сервера из конфигурации
        server_params = StdioServerParameters(
            command=edit_server_config["command"],
            args=edit_server_config["args"],
            env={
                **os.environ,
                **edit_server_config["env"]
            }
        )

        # Определяем путь для выходного файла
        resolved_output_path = output_path if output_path is not None else f"plots/edited_image{number}_{random.randint(100, 999)}_{session_id}.png"

        # Подключаемся к MCP серверу
        async with stdio_client(server_params) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                await session.initialize()

                call_params = {
                    "prompt": prompt,
                    "image_path": image_path,
                    "output_path": resolved_output_path,
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height
                }

                # Добавляем опциональные параметры только если они не None
                if true_cfg_scale is not None:
                    call_params["true_cfg_scale"] = true_cfg_scale
                if num_inference_steps is not None:
                    call_params["num_inference_steps"] = num_inference_steps
                if seed is not None:
                    call_params["seed"] = seed

                result = await session.call_tool("edit_image_file", call_params)

                # Проверяем результат
                for item in result.content:
                    if hasattr(item, 'type') and item.type == 'text':
                        if "✅" in item.text:
                            if output_path is not None:
                                # Для явно указанного пути — пробуем извлечь итоговый путь из ответа
                                if "(URI: " in item.text:
                                    uri_start = item.text.find("(URI: ") + 6
                                    uri_end = item.text.find(")", uri_start)
                                    if uri_end > uri_start:
                                        file_uri = item.text[uri_start:uri_end]
                                        import urllib.parse
                                        path = urllib.parse.urlparse(file_uri).path
                                        return urllib.parse.unquote(path)
                                if "'->" in item.text:
                                    out = item.text.split("'->")[-1].strip().strip("'")
                                    if "(URI:" in out:
                                        out = out.split("(URI:")[0].strip()
                                    return out
                                try:
                                    import json as _json
                                    with open("mcp_servers.json", "r", encoding="utf-8") as f:
                                        cfg = _json.load(f)
                                    base_dir = cfg["mcpServers"]["image-editor"]["env"].get("IMG_SAVE_BASE_DIR", "")
                                    if base_dir and not os.path.isabs(resolved_output_path):
                                        return os.path.normpath(os.path.join(base_dir, resolved_output_path))
                                except Exception:
                                    pass
                            return resolved_output_path
                        else:
                            raise Exception(f"MCP ошибка: {item.text}")

                raise Exception("Неожиданный результат от MCP сервера")

    # Выполняем асинхронный вызов с учетом существующего event loop
    try:
        # Проверяем, есть ли уже запущенный event loop
        loop = asyncio.get_running_loop()
        # Если есть, создаем задачу и ждем ее выполнения
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, call_mcp_edit_file())
            try:
                output_file_path = future.result(timeout=120)
            except Exception as e:
                # Извлекаем оригинальную ошибку из ThreadPoolExecutor
                if hasattr(e, '__cause__') and e.__cause__:
                    raise e.__cause__
                else:
                    raise e
    except RuntimeError:
        # Если нет event loop, создаем новый
        output_file_path = asyncio.run(call_mcp_edit_file())

    return output_file_path


def _edit_file_via_direct_mcp_with_output(prompt: str, image_path: str, output_path: str, negative_prompt: str, width: int, height: int, true_cfg_scale: float, num_inference_steps: int, seed: int = None) -> str:
    """Обратносовместимая обёртка над _edit_file_via_direct_mcp с явным output_path."""
    if not output_path:
        raise ValueError("output_path must be a non-empty string")
    return _edit_file_via_direct_mcp(
        prompt, image_path, "", 0, negative_prompt, width, height,
        true_cfg_scale, num_inference_steps, seed, output_path=output_path
    )

def _edit_via_direct_mcp(prompt: str, image_b64: str, session_id: str, number: int, width: int, height: int) -> str:
    """Прямое подключение к MCP серверу image-editor"""
    async def call_mcp_edit():
        from mcp import StdioServerParameters
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client
        import json
        
        # Загружаем настройки из mcp_servers.json
        with open("mcp_servers.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        
        # Находим настройки image-editor сервера
        edit_server_config = config["mcpServers"]["image-editor"]
        
        # Создаем параметры MCP сервера из конфигурации
        server_params = StdioServerParameters(
            command=edit_server_config["command"],
            args=edit_server_config["args"],
            env={
                **os.environ,
                **edit_server_config["env"]
            }
        )
        
        # Подключаемся к MCP серверу
        async with stdio_client(server_params) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Вызываем edit_image
                result = await session.call_tool("edit_image", {
                    "prompt": prompt,
                    "image_b64": image_b64,
                    "width": width,
                    "height": height
                })
                
                # Ищем ImageContent в результате
                for item in result.content:
                    if hasattr(item, 'type') and item.type == 'image':
                        return item.data  # Возвращаем base64 данные
                        
                raise Exception("Отредактированное изображение не найдено в MCP результате")
    
    # Выполняем асинхронный вызов с учетом существующего event loop
    try:
        # Проверяем, есть ли уже запущенный event loop
        loop = asyncio.get_running_loop()
        # Если есть, создаем задачу и ждем ее выполнения
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, call_mcp_edit())
            edited_image_base64 = future.result(timeout=120)  # Редактирование может занимать больше времени
    except RuntimeError:
        # Если нет event loop, создаем новый
        edited_image_base64 = asyncio.run(call_mcp_edit())
    
    # Декодируем и сохраняем отредактированное изображение
    image_bytes = base64.b64decode(edited_image_base64)
    image = Image.open(BytesIO(image_bytes))
    
    os.makedirs("plots", exist_ok=True)
    filename = f"plots/edited_image{number}_{random.randint(100, 999)}_{session_id}.png"
    image.save(filename, format="PNG")
    
    return filename


def analyze_image_tool(image_input: str, analysis_prompt: str = None, analysis_types: List[str] = None, input_type: str = "auto") -> str:
    """Анализирует изображение с помощью vision модели через OpenAI API.
    
    Args:
        image_input: Путь к файлу изображения ИЛИ URL изображения ИЛИ base64 строка изображения
        analysis_prompt: Кастомный промпт для анализа (опционально)
        analysis_types: Список типов анализа для выполнения (опционально)
        input_type: Тип входных данных - "path", "url", "base64" или "auto" (автоопределение)
    
    Returns:
        str: JSON строка с результатами анализа или сообщение об ошибке
    """
    
    if not call_openai_api:
        return "Ошибка: Функция call_openai_api недоступна"
    
    try:
        # Определяем тип входных данных
        if input_type == "auto":
            if image_input.startswith(("http://", "https://")):
                input_type = "url"
            elif os.path.exists(image_input):
                input_type = "path"
            elif len(image_input) > 100 and not "/" in image_input and not "\\" in image_input and not "." in image_input:
                input_type = "base64"
            else:
                return f"Ошибка: Не удалось определить тип входных данных. Укажите input_type явно."
        
        # Получаем base64 и MIME тип в зависимости от типа входных данных
        if input_type == "path":
            if not os.path.exists(image_input):
                return f"Ошибка: Файл изображения не найден: {image_input}"
            
            # Читаем и кодируем изображение в base64
            with open(image_input, 'rb') as image_file:
                image_data = image_file.read()
                image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Определяем MIME тип по расширению
            image_extension = os.path.splitext(image_input)[1].lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg', 
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            mime_type = mime_types.get(image_extension, 'image/jpeg')
            
        elif input_type == "url":
            # Для URL не нужно скачивать - передаем напрямую
            image_base64 = None  # Не используется для URL
            mime_type = None     # Не используется для URL
            
        elif input_type == "base64":
            image_base64 = image_input
            mime_type = "image/jpeg"  # По умолчанию
        else:
            return f"Ошибка: Неизвестный тип входных данных: {input_type}"
        
        # Формируем URL для изображения
        if input_type == "url":
            # Для URL используем прямую ссылку
            image_url = image_input
        else:
            # Для path и base64 формируем data URI
            image_url = f"data:{mime_type};base64,{image_base64}"
        
        # Добавляем обратную совместимость и умные дефолты
        if analysis_types is None:
            # Если не указано - анализируем все доступные типы
            analysis_types = get_available_image_analysis_types()
        else:
            # Фильтруем неизвестные типы с предупреждением
            valid_types = []
            for atype in analysis_types:
                if atype in ANALYSIS_TYPES_CONFIG:
                    valid_types.append(atype)
                else:
                    print(f"Warning: Unknown analysis type '{atype}' ignored")
            analysis_types = valid_types
        
        # Если после фильтрации не осталось типов - используем дефолтные
        if not analysis_types:
            analysis_types = ["Распознавание объектов", "Анализ композиции", "Анализ содержимого"]
        
        # Создаем динамический промпт для анализа
        final_analysis_prompt = build_dynamic_analysis_prompt(analysis_types, analysis_prompt)
        
        # Создаем динамический системный промпт
        system_prompt = build_dynamic_system_prompt(analysis_types)
        
        # Используем расширенную функцию call_openai_api с поддержкой изображений
        response = call_openai_api(
            prompt=final_analysis_prompt,
            system_prompt=system_prompt,
            model=model_vision,
            max_tokens=2000,
            temperature=0.1,
            image_url=image_url,
            response_format={"type": "json_object"}
        )
        
        if response:
            return response
        else:
            return "Ошибка: Не удалось получить ответ от API"
            
    except Exception as e:
        return f"Ошибка анализа изображения: {str(e)}"


def _edit_image_vse_post_edits_multipart(
    url_edits: str,
    api_key: str,
    model: str,
    prompt: str,
    image_paths: List[str],
    negative_prompt: str,
    width: int,
    height: int,
    seed: Optional[int],
) -> Any:
    """POST .../images/edits (OpenAI-совместимый multipart: image, image[], ...)."""
    files: List[Any] = []
    for idx, path in enumerate(image_paths):
        with open(path, "rb") as image_file:
            raw = image_file.read()
        name = os.path.basename(path) or f"image_{idx}.png"
        mime = _guess_image_mime(path)
        field_name = "image" if idx == 0 else "image[]"
        files.append((field_name, (name, raw, mime)))

    data: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": f"{int(width)}x{int(height)}",
    }
    if negative_prompt:
        data["negative_prompt"] = negative_prompt
    if seed is not None:
        data["seed"] = str(seed)

    return requests.post(
        url_edits,
        headers={"Authorization": f"Bearer {api_key}", "X-Title": "MultiAgents Image Edit"},
        files=files,
        data=data,
        timeout=(30, 600),
    )


def edit_image_vse_tool(prompt: str, image_paths: List[str], session_id: str, output_path: str = None, seed: int = None, negative_prompt: str = "", width: int = 1920, height: int = 1080) -> str:
    """Редактирование изображения (img2img): только IMG2IMG_MODEL (или VSEGPT_IMG2IMG_MODEL).

    Сначала POST .../images/edits (multipart); если шлюз отвечает 404 «No image edit route» для этой
    модели — повтор POST .../images/generations с image_url (тот же IMG2IMG_MODEL). Генерация с нуля —
    только через generate_image_tool и TEXT2IMAGE_MODEL.

    Args:
        prompt: Текстовое описание желаемых изменений СТРОГО НА АНГЛИЙСКОМ ЯЗЫКЕ
        image_paths: Список путей к исходным файлам изображений (до 10 изображений)
        session_id: Идентификатор сессии для именования файла
        output_path: Путь для сохранения отредактированного изображения (опционально)
        seed: Seed для воспроизводимости (оставьте пустым для случайного)
        negative_prompt: Негативный промпт (что НЕ должно быть в изображении)
        width: Ширина изображения
        height: Высота изображения
    
    Returns:
        str: Путь к файлу с отредактированным изображением или сообщение об ошибке
    """
    
    try:
        url_gen = _images_generations_url()
        url_edits = _images_edits_url()
        if not url_gen or not url_edits:
            return (
                "Ошибка: не задан OPENAI_API_BASE_DB (базовый URL OpenAI-совместимого API, например http://host:port/v1)"
            )

        api_key = _resolve_openai_images_api_key()
        if not api_key:
            return (
                "Ошибка: не найден ключ API для изображений. Установите OPENAI_API_KEY_DB или VSEGPT_API_KEY"
            )

        model = (os.getenv("IMG2IMG_MODEL") or os.getenv("VSEGPT_IMG2IMG_MODEL") or "").strip()
        if not model:
            return (
                "Ошибка: не задана модель img2img. Установите IMG2IMG_MODEL или VSEGPT_IMG2IMG_MODEL"
            )
        
        # Проверяем входной массив изображений
        if not image_paths:
            return "Ошибка: Список изображений пуст"
        
        # multi-image: поддерживаем до 10 входных изображений
        if len(image_paths) > 10:
            return f"Ошибка: Поддерживается максимум 10 изображений, передано: {len(image_paths)}"
        
        # Проверяем существование всех файлов изображений
        for image_path in image_paths:
            if not os.path.exists(image_path):
                return f"Ошибка: Файл изображения не найден: {image_path}"
        
        def _post_generations_json():
            def encode_image(image_path: str) -> str:
                with open(image_path, "rb") as image_file:
                    return base64.b64encode(image_file.read()).decode("utf-8")

            encoded_images = [encode_image(path) for path in image_paths]

            payload: Dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "response_format": "b64_json",
                "size": f"{int(width)}x{int(height)}",
                "seed": seed,
                "image_url": f"data:image/jpeg;base64,{encoded_images[0]}",
            }

            if negative_prompt:
                payload["negative_prompt"] = negative_prompt

            for idx in range(2, min(len(encoded_images), 10) + 1):
                payload[f"image{idx}_url"] = f"data:image/jpeg;base64,{encoded_images[idx - 1]}"

            session = requests.Session()
            try:
                session.headers.update({
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "X-Title": "MultiAgents Image Edit",
                })
                return session.post(url_gen, json=payload, timeout=180)
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    close()

        response = _edit_image_vse_post_edits_multipart(
            url_edits,
            api_key,
            model,
            prompt,
            image_paths,
            negative_prompt,
            width,
            height,
            seed,
        )
        if (
            response.status_code == 404
            and "No image edit route" in (response.text or "")
        ):
            response = _post_generations_json()

        # Проверяем успешность запроса
        if response.status_code == 200:
            # Получаем base64 строку из ответа
            response_json = response.json()
            item = response_json["data"][0]
            b64_data = _b64_from_images_api_item(item)
            if not b64_data:
                return f"Ошибка API изображений: неожиданный формат ответа: {item!r}"

            # Определяем путь для сохранения
            if output_path:
                # Проверяем, указан ли конкретный файл или только директория
                output_path_obj = Path(output_path)
                
                # Определяем: директория или файл
                is_directory = (
                    output_path.endswith('/') or 
                    output_path.endswith('\\') or 
                    output_path_obj.is_dir() or
                    not output_path_obj.suffix  # Нет расширения файла
                )
                
                if is_directory:
                    # Это директория - генерируем имя файла
                    timestamp = int(time.time())
                    session_suffix = f"_{session_id}" if session_id else ""
                    images_count = len(image_paths)
                    filename = f"vse_edited_{images_count}imgs_{timestamp}{session_suffix}.png"
                    filepath = output_path_obj / filename
                else:
                    # Это полный путь к файлу
                    filepath = output_path_obj
                
                # Создаем директории если нужно
                filepath.parent.mkdir(parents=True, exist_ok=True)
            else:
                # Создаем временную директорию в папке скрипта
                script_dir = os.path.dirname(os.path.abspath(__file__))
                tmp_dir = Path(script_dir) / "tmp_images"
                tmp_dir.mkdir(exist_ok=True)
                
                # Генерируем имя файла с timestamp и количеством изображений
                timestamp = int(time.time())
                session_suffix = f"_{session_id}" if session_id else ""
                images_count = len(image_paths)
                filename = f"vse_edited_{images_count}imgs_{timestamp}{session_suffix}.png"
                filepath = tmp_dir / filename
            
            # Декодируем base64 и сохраняем в файл
            with open(filepath, "wb") as img_file:
                img_file.write(base64.b64decode(b64_data))
            
            # Возвращаем абсолютный путь к файлу
            return str(filepath.absolute())
        else:
            return f"Ошибка API изображений: {response.status_code}: {response.text}"
            
    except requests.exceptions.RequestException as e:
        return f"Ошибка сетевого запроса: {str(e)}"
    except Exception as e:
        return f"Ошибка редактирования изображения через API изображений: {str(e)}"
