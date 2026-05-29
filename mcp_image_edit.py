"""
MCP Server для редактирования изображений с использованием Chutes AI
Основан на официальном MCP Python SDK
"""

import asyncio
import base64
import json
import os
import sys
import traceback
import logging
from typing import Any, Dict, List, Optional, Sequence

# Импорт официального MCP SDK
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
    import mcp.server.models as models
except ImportError:
    print("Ошибка: Установите официальный MCP SDK: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Дополнительные импорты для работы с API
try:
    import aiohttp
except ImportError:
    print("Ошибка: Установите aiohttp: pip install aiohttp", file=sys.stderr)
    sys.exit(1)


class ImageEditor:
    """Класс для редактирования изображений через Chutes AI API"""
    
    def __init__(self):
        self.api_token = os.getenv("CHUTES_API_TOKEN", "")
        # Не останавливаем сервер, если токен не установлен - будем показывать ошибку при вызове
        
        # Настройки по умолчанию
        self.default_settings = {
            "width": int(os.getenv("EDIT_WIDTH", "1024")),
            "height": int(os.getenv("EDIT_HEIGHT", "1024")),
            "true_cfg_scale": float(os.getenv("EDIT_CFG_SCALE", "4")),
            "negative_prompt": os.getenv("EDIT_NEGATIVE_PROMPT", ""),
            "num_inference_steps": int(os.getenv("EDIT_INFERENCE_STEPS", "50")),
            "seed": None
        }
    
    def _validate_range(self, value, min_val, max_val):
        """Валидация значений в заданном диапазоне"""
        return max(min_val, min(value, max_val))
    
    async def edit_image(self, prompt: str, image_b64: str, **kwargs) -> str:
        """
        Редактирует изображение по текстовому промпту
        
        Args:
            prompt: Текстовый промпт с описанием желаемых изменений
            image_b64: Исходное изображение в формате base64
            **kwargs: Дополнительные параметры редактирования
            
        Returns:
            Base64 encoded image data (без data URI префикса)
            
        Raises:
            ValueError: При ошибке API или отсутствии токена
        """
        if not self.api_token:
            raise ValueError("API токен не настроен. Установите переменную окружения CHUTES_API_TOKEN")
        
        if not prompt.strip():
            raise ValueError("Пустой промпт недопустим")
            
        if not image_b64.strip():
            raise ValueError("Изображение для редактирования не предоставлено")
        
        # Объединяем настройки по умолчанию с переданными параметрами
        settings = {**self.default_settings, **kwargs}
        
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        
        body = {
            "seed": settings.get("seed"),
            "width": settings["width"],
            "height": settings["height"],
            "prompt": prompt.strip(),
            "image_b64": image_b64,
            "true_cfg_scale": settings["true_cfg_scale"],
            "negative_prompt": settings["negative_prompt"],
            "num_inference_steps": settings["num_inference_steps"]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://chutes-qwen-image-edit.chutes.ai/generate",
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=120)  # Редактирование может занимать больше времени
                ) as response:
                    if response.status != 200:
                        error_body = await response.text()
                        logger.error(f"API Error {response.status}: {error_body}")
                        raise ValueError(f"API Error {response.status}: {error_body}")
                    
                    # Проверяем content-type ответа
                    content_type = response.headers.get('content-type', '')
                    
                    if 'application/json' in content_type:
                        # Если JSON ответ
                        result = await response.json()
                        
                        # Извлекаем отредактированное изображение из ответа
                        if 'image' in result:
                            edited_image_b64 = result['image']
                        elif 'data' in result:
                            edited_image_b64 = result['data']
                        else:
                            logger.error(f"Неожиданный формат ответа API: {result}")
                            raise ValueError(f"Неожиданный формат ответа API: {result}")
                        
                        return edited_image_b64
                        
                    elif 'image/' in content_type:
                        # Если изображение напрямую
                        image_data = await response.read()
                        if not image_data or len(image_data) < 2:
                            logger.error("API вернул пустые данные изображения")
                            raise ValueError("Пустые или некорректные данные изображения")
                        
                        # Конвертируем в base64
                        edited_image_b64 = base64.b64encode(image_data).decode('utf-8')
                        return edited_image_b64
                        
                    else:
                        logger.error(f"API вернул неожиданный content-type: {content_type}")
                        raise ValueError(f"Неожиданный content-type: {content_type}")
                    
        except aiohttp.ClientError as e:
            logger.error(f"Сетевая ошибка при обращении к API: {e}")
            raise ValueError(f"Ошибка сетевого соединения: {e}")
        except asyncio.TimeoutError:
            logger.error("Таймаут при обращении к API редактирования")
            raise ValueError("Таймаут при редактировании изображения")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при редактировании: {e}")
            raise ValueError(f"Неожиданная ошибка при редактировании: {e}")


# Настройка логирования только для ошибок
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)

# Создаем экземпляр сервера
app = Server("image-editor")
image_editor = ImageEditor()


@app.list_tools()
async def list_tools() -> List[Tool]:
    """Список доступных инструментов сервера"""
    return [
        Tool(
            name="edit_image",
            description="Редактирует изображение на основе текстового описания желаемых изменений. ВАЖНО: промпт и негативный промпт должны быть СТРОГО на английском языке!",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Текстовое описание желаемых изменений к изображению НА АНГЛИЙСКОМ ЯЗЫКЕ",
                        "minLength": 1
                    },
                    "image_b64": {
                        "type": "string",
                        "description": "Исходное изображение в формате base64 (без data URI префикса)",
                        "minLength": 1
                    },
                    "width": {
                        "type": "integer",
                        "description": "Ширина результата (512-2048)",
                        "minimum": 512,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "height": {
                        "type": "integer", 
                        "description": "Высота результата (512-2048)",
                        "minimum": 512,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "true_cfg_scale": {
                        "type": "number",
                        "description": "Сила следования промпту (1.0-10.0)",
                        "minimum": 1.0,
                        "maximum": 10.0,
                        "default": 4.0
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Негативный промпт (что НЕ должно быть в результате)",
                        "default": ""
                    },
                    "num_inference_steps": {
                        "type": "integer",
                        "description": "Количество шагов обработки (10-100)",
                        "minimum": 10,
                        "maximum": 100,
                        "default": 50
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Seed для воспроизводимости (оставьте пустым для случайного)",
                        "minimum": 0,
                        "default": None
                    }
                },
                "required": ["prompt", "image_b64", "negative_prompt"]
            }
        ),
        Tool(
            name="edit_image_file",
            description="Редактирует изображение из файла на основе текстового описания желаемых изменений. ВАЖНО: промпт и негативный промпт должны быть СТРОГО на английском языке!",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Текстовое описание желаемых изменений к изображению НА АНГЛИЙСКОМ ЯЗЫКЕ",
                        "minLength": 1
                    },
                    "image_path": {
                        "type": "string",
                        "description": "Путь к исходному файлу изображения",
                        "minLength": 1
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Путь для сохранения отредактированного изображения",
                        "minLength": 1
                    },
                    "width": {
                        "type": "integer",
                        "description": "Ширина результата (512-2048)",
                        "minimum": 512,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "height": {
                        "type": "integer", 
                        "description": "Высота результата (512-2048)",
                        "minimum": 512,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "true_cfg_scale": {
                        "type": "number",
                        "description": "Сила следования промпту (1.0-10.0)",
                        "minimum": 1.0,
                        "maximum": 10.0,
                        "default": 4.0
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Негативный промпт (что НЕ должно быть в результате)",
                        "default": ""
                    },
                    "num_inference_steps": {
                        "type": "integer",
                        "description": "Количество шагов обработки (10-100)",
                        "minimum": 10,
                        "maximum": 100,
                        "default": 50
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Seed для воспроизводимости (оставьте пустым для случайного)",
                        "minimum": 0,
                        "default": None
                    }
                },
                "required": ["prompt", "image_path", "output_path", "negative_prompt"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка вызовов инструментов"""
    try:
        if name == "edit_image":
            return await _handle_edit_image(arguments)
        elif name == "edit_image_file":
            return await _handle_edit_image_file(arguments)
        else:
            raise ValueError(f"Неизвестный инструмент: {name}")
            
    except Exception as e:
        logger.error(f"Ошибка при вызове инструмента {name}: {e}")
        
        # Возвращаем ошибку как текстовый контент
        return [
            TextContent(
                type="text",
                text=f"❌ Ошибка при вызове инструмента {name}: {str(e)}"
            )
        ]

async def _handle_edit_image(arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка редактирования изображения из base64"""
    try:
        # Извлекаем параметры
        prompt = arguments.get("prompt", "")
        image_b64 = arguments.get("image_b64", "")
        
        if not prompt:
            raise ValueError("Параметр 'prompt' обязателен")
        if not image_b64:
            raise ValueError("Параметр 'image_b64' обязателен")
        
        # Дополнительные параметры (необязательные)
        edit_params = {
            k: v for k, v in arguments.items() 
            if k in ["width", "height", "true_cfg_scale", "negative_prompt", "num_inference_steps", "seed"]
        }
        
        # Редактируем изображение
        edited_image_b64 = await image_editor.edit_image(prompt, image_b64, **edit_params)
        
        # Возвращаем результат
        return [
            TextContent(
                type="text", 
                text=f"✅ Изображение успешно отредактировано по промпту: '{prompt}'"
            ),
            ImageContent(
                type="image",
                data=edited_image_b64,
                mimeType="image/jpeg"
            )
        ]
        
    except Exception as e:
        # Возвращаем ошибку как текстовый контент
        return [
            TextContent(
                type="text",
                text=f"❌ Ошибка при редактировании изображения: {str(e)}"
            )
        ]

async def _handle_edit_image_file(arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка редактирования изображения из файла"""
    try:
        # Извлекаем параметры
        prompt = arguments.get("prompt", "")
        image_path = arguments.get("image_path", "")
        output_path = arguments.get("output_path", "")
        
        if not prompt:
            raise ValueError("Параметр 'prompt' обязателен")
        if not image_path:
            raise ValueError("Параметр 'image_path' обязателен")
        if not output_path:
            raise ValueError("Параметр 'output_path' обязателен")
        
        # Обрабатываем путь к исходному файлу с учетом базового каталога
        base_save_directory = os.getenv("IMG_SAVE_BASE_DIR", "")
        if base_save_directory and not os.path.isabs(image_path):
            # Если задан базовый каталог и путь относительный, ищем в базовом каталоге
            full_image_path = os.path.normpath(os.path.join(base_save_directory, image_path))
        else:
            # Иначе используем путь как есть
            full_image_path = os.path.normpath(image_path)
        
        # Проверяем существование входного файла
        if not os.path.exists(full_image_path):
            raise ValueError(f"Файл изображения не найден: {full_image_path}")
        
        # Загружаем изображение и конвертируем в base64
        try:
            with open(full_image_path, 'rb') as f:
                image_data = f.read()
                image_b64 = base64.b64encode(image_data).decode('utf-8')
        except Exception as e:
            raise ValueError(f"Ошибка при чтении файла изображения: {e}")
        
        # Дополнительные параметры (необязательные)
        edit_params = {
            k: v for k, v in arguments.items() 
            if k in ["width", "height", "true_cfg_scale", "negative_prompt", "num_inference_steps", "seed"]
        }
        
        # Редактируем изображение через base64 метод
        edited_image_b64 = await image_editor.edit_image(prompt, image_b64, **edit_params)
        
        # Декодируем и сохраняем результат
        try:
            edited_image_data = base64.b64decode(edited_image_b64)
        except Exception as e:
            raise ValueError(f"Ошибка при декодировании отредактированного изображения: {e}")
        
        # Обрабатываем путь к выходному файлу с учетом базового каталога
        if base_save_directory and not os.path.isabs(output_path):
            # Если задан базовый каталог и путь относительный, сохраняем в базовом каталоге
            full_output_path = os.path.normpath(os.path.join(base_save_directory, output_path))
        else:
            # Иначе используем путь как есть
            full_output_path = os.path.normpath(output_path)
        
        # Создаем папку для выходного файла если нужно
        output_dir = os.path.dirname(full_output_path)
        if output_dir and not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception as e:
                raise ValueError(f"Ошибка при создании директории {output_dir}: {e}")
        
        # Сохраняем файл
        try:
            with open(full_output_path, 'wb') as f:
                f.write(edited_image_data)
        except Exception as e:
            raise ValueError(f"Ошибка при сохранении файла {full_output_path}: {e}")
        
        # Создаем file URI для выходного файла
        try:
            import urllib.parse
            output_file_uri = urllib.parse.urljoin('file://', urllib.parse.quote(os.path.abspath(full_output_path)))
        except Exception:
            output_file_uri = full_output_path
        
        # Возвращаем результат
        return [
            TextContent(
                type="text", 
                text=f"✅ Изображение успешно отредактировано: '{full_image_path}' -> '{full_output_path}' (URI: {output_file_uri})"
            )
        ]
        
    except Exception as e:
        # Возвращаем ошибку как текстовый контент
        return [
            TextContent(
                type="text",
                text=f"❌ Ошибка при редактировании файла изображения: {str(e)}"
            )
        ]


async def main():
    """Главная функция для запуска сервера"""
    try:
        # Запускаем сервер через stdio transport
        async with stdio_server() as streams:
            await app.run(
                streams[0], streams[1],
                app.create_initialization_options()
            )
    except KeyboardInterrupt:
        print("Сервер остановлен пользователем", file=sys.stderr)
    except Exception as e:
        logger.error(f"Критическая ошибка сервера: {e}")
        print(f"Критическая ошибка сервера: {e}", file=sys.stderr)
        sys.exit(1)


def setup_exception_handler():
    """Настройка глобального обработчика исключений"""
    def exception_handler(loop, context):
        """Обработчик исключений для asyncio loop"""
        if 'exception' in context:
            exc = context['exception']
            logger.error(f"Необработанное исключение: {type(exc).__name__}: {exc}")
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Если нет активного loop, создаем новый
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.set_exception_handler(exception_handler)

if __name__ == "__main__":
    # Настраиваем обработчик исключений
    setup_exception_handler()
    
    # Запускаем сервер
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Фатальная ошибка при запуске: {e}")
        print(f"Фатальная ошибка при запуске: {e}", file=sys.stderr)
        sys.exit(1)
