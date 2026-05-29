"""
Исправленный MCP Server для генерации изображений с использованием Stable Diffusion
Следует официальной спецификации MCP и использует правильный Python SDK
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


# Настройка логирования только для ошибок
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)


class ImageGenerator:
    """Класс для генерации изображений через Chutes AI API"""
    
    def __init__(self):
        self.api_token = os.getenv("CHUTES_API_TOKEN", "")
        # Не останавливаем сервер, если токен не установлен - будем показывать ошибку при вызове
        
        # Настройки по умолчанию
        self.default_settings = {
            "model": os.getenv("SD_MODEL", "JuggernautXL"),
            "width": self._validate_range(int(os.getenv("SD_WIDTH", "1024")), 128, 2048),
            "height": self._validate_range(int(os.getenv("SD_HEIGHT", "1024")), 128, 2048),
            "guidance_scale": self._validate_range(float(os.getenv("SD_GUIDANCE_SCALE", "7.5")), 1.0, 20.0),
            "negative_prompt": os.getenv("SD_NEGATIVE_PROMPT", ""),
            "num_inference_steps": self._validate_range(int(os.getenv("SD_NUM_INFERENCE_STEPS", "25")), 1, 50),
            "seed": max(0, int(os.getenv("SD_SEED", "0")))
        }
    
    def _validate_range(self, value, min_val, max_val):
        """Валидация значений в заданном диапазоне"""
        return max(min_val, min(value, max_val))
    
    async def generate_image(self, prompt: str, **kwargs) -> str:
        """
        Генерирует изображение по текстовому промпту
        
        Args:
            prompt: Текстовый промпт для генерации
            **kwargs: Дополнительные параметры генерации
            
        Returns:
            Base64 encoded image data URI
            
        Raises:
            ValueError: При ошибке API или отсутствии токена
        """
        if not self.api_token:
            raise ValueError("API токен не настроен. Установите переменную окружения CHUTES_API_TOKEN")
        
        if not prompt.strip():
            raise ValueError("Пустой промпт недопустим")
        
        # Объединяем настройки по умолчанию с переданными параметрами
        settings = {**self.default_settings, **kwargs}
        
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        
        body = {
            "model": settings["model"],
            "prompt": prompt.strip(),
            "width": settings["width"],
            "height": settings["height"],
            "guidance_scale": settings["guidance_scale"],
            "negative_prompt": settings["negative_prompt"],
            "num_inference_steps": settings["num_inference_steps"],
            "seed": settings["seed"] if settings["seed"] != 0 else None
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://image.chutes.ai/generate",
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_body = await response.text()
                        logger.error(f"API Error {response.status}: {error_body}")
                        raise ValueError(f"API Error {response.status}: {error_body}")
                    
                    image_data = await response.read()
                    if not image_data or len(image_data) < 2:
                        logger.error("API вернул пустые данные изображения")
                        raise ValueError("Пустые или некорректные данные изображения")
                    
                    # Проверяем JPEG signature
                    if image_data[:2] != b'\xff\xd8':
                        logger.warning("Данные могут не быть JPEG")
                        print("Предупреждение: Данные могут не быть JPEG", file=sys.stderr)
                    
                    base64_image = base64.b64encode(image_data).decode('utf-8')
                    return f"data:image/jpeg;base64,{base64_image}"
                    
        except aiohttp.ClientError as e:
            logger.error(f"Сетевая ошибка при обращении к API: {e}")
            raise ValueError(f"Ошибка сетевого соединения: {e}")
        except asyncio.TimeoutError:
            logger.error("Таймаут при обращении к API генерации")
            raise ValueError("Таймаут при генерации изображения")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при генерации: {e}")
            raise ValueError(f"Неожиданная ошибка при генерации: {e}")


# Создаем экземпляр сервера
app = Server("image-generator")
image_gen = ImageGenerator()


@app.list_tools()
async def list_tools() -> List[Tool]:
    """Список доступных инструментов сервера"""
    return [
        Tool(
            name="generate_image",
            description="Генерирует изображения.ВАЖНО: промпт и негативный промпт должны быть СТРОГО на английском языке!",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Текстовый промпт для генерации изображения НА АНГЛИЙСКОМ ЯЗЫКЕ",
                        "minLength": 1
                    },
                    "width": {
                        "type": "integer",
                        "description": "Ширина изображения (128-2048)",
                        "minimum": 128,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "height": {
                        "type": "integer", 
                        "description": "Высота изображения (128-2048)",
                        "minimum": 128,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "guidance_scale": {
                        "type": "number",
                        "description": "Сила следования промпту (1.0-20.0)",
                        "minimum": 1.0,
                        "maximum": 20.0,
                        "default": 7.5
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Негативный промпт (что НЕ включать в изображение)",
                        "default": ""
                    },
                    "num_inference_steps": {
                        "type": "integer",
                        "description": "Количество шагов генерации (1-50)",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 25
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Seed для воспроизводимости (0 = случайный)",
                        "minimum": 0,
                        "default": 0
                    }
                },
                "required": ["prompt", "negative_prompt"]
            }
        ),
        Tool(
            name="generate_image_to_file",
            description="Генерирует изображение и сохраняет его в указанный файл. ВАЖНО: промпт и негативный промпт должны быть СТРОГО на английском языке!",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Текстовый промпт для генерации изображения НА АНГЛИЙСКОМ ЯЗЫКЕ",
                        "minLength": 1
                    },
                    "directory": {
                        "type": "string",
                        "description": "Каталог для сохранения изображения",
                        "minLength": 1
                    },
                    "filename": {
                        "type": "string",
                        "description": "Имя файла для сохранения (с расширением)",
                        "minLength": 1
                    },
                    "width": {
                        "type": "integer",
                        "description": "Ширина изображения (128-2048)",
                        "minimum": 128,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "height": {
                        "type": "integer", 
                        "description": "Высота изображения (128-2048)",
                        "minimum": 128,
                        "maximum": 2048,
                        "default": 1024
                    },
                    "guidance_scale": {
                        "type": "number",
                        "description": "Сила следования промпту (1.0-20.0)",
                        "minimum": 1.0,
                        "maximum": 20.0,
                        "default": 7.5
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Негативный промпт (что НЕ включать в изображение)",
                        "default": ""
                    },
                    "num_inference_steps": {
                        "type": "integer",
                        "description": "Количество шагов генерации (1-50)",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 25
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Seed для воспроизводимости (0 = случайный)",
                        "minimum": 0,
                        "default": 0
                    }
                },
                "required": ["prompt", "directory", "filename", "negative_prompt"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка вызовов инструментов"""
    try:
        if name == "generate_image":
            return await _handle_generate_image(arguments)
        elif name == "generate_image_to_file":
            return await _handle_generate_image_to_file(arguments)
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

async def _handle_generate_image(arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка генерации изображения в base64"""
    try:
        # Извлекаем параметры
        prompt = arguments.get("prompt", "")
        if not prompt:
            raise ValueError("Параметр 'prompt' обязателен")
        
        # Дополнительные параметры (необязательные)
        generation_params = {
            k: v for k, v in arguments.items() 
            if k in ["width", "height", "guidance_scale", "negative_prompt", "num_inference_steps", "seed"]
        }
        
        # Генерируем изображение
        image_data_uri = await image_gen.generate_image(prompt, **generation_params)
        
        # Извлекаем только base64 данные без префикса
        if image_data_uri.startswith("data:image/jpeg;base64,"):
            base64_data = image_data_uri[len("data:image/jpeg;base64,"):]
        else:
            base64_data = image_data_uri
        
        # Возвращаем результат
        return [
            TextContent(
                type="text", 
                text=f"✅ Изображение успешно сгенерировано по промпту: '{prompt}'"
            ),
            ImageContent(
                type="image",
                data=base64_data,
                mimeType="image/jpeg"
            )
        ]
        
    except Exception as e:
        # Возвращаем ошибку как текстовый контент
        return [
            TextContent(
                type="text",
                text=f"❌ Ошибка при генерации изображения: {str(e)}"
            )
        ]

async def _handle_generate_image_to_file(arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Обработка генерации изображения в файл"""
    try:
        # Извлекаем параметры
        prompt = arguments.get("prompt", "")
        directory = arguments.get("directory", "")
        filename = arguments.get("filename", "")
        
        if not prompt:
            raise ValueError("Параметр 'prompt' обязателен")
        if not directory:
            raise ValueError("Параметр 'directory' обязателен")
        if not filename:
            raise ValueError("Параметр 'filename' обязателен")
        
        # Дополнительные параметры (необязательные)
        generation_params = {
            k: v for k, v in arguments.items() 
            if k in ["width", "height", "guidance_scale", "negative_prompt", "num_inference_steps", "seed"]
        }
        
        # Генерируем изображение
        image_data_uri = await image_gen.generate_image(prompt, **generation_params)
        
        # Извлекаем только base64 данные без префикса
        if image_data_uri.startswith("data:image/jpeg;base64,"):
            base64_data = image_data_uri[len("data:image/jpeg;base64,"):]
        else:
            base64_data = image_data_uri
        
        # Создаем полный путь к файлу
        import base64
        import os
        
        # Проверяем базовый каталог из переменной окружения
        base_save_directory = os.getenv("IMG_SAVE_BASE_DIR", "")
        if base_save_directory:
            # Если задан базовый каталог, используем его как корень
            full_directory = os.path.normpath(os.path.join(base_save_directory, directory))
        else:
            # Иначе используем относительный путь от текущей директории
            full_directory = os.path.normpath(directory)
        
        # Создаем каталог если не существует
        if not os.path.exists(full_directory):
            os.makedirs(full_directory, exist_ok=True)
        
        # Формируем полный путь к файлу
        output_path = os.path.join(full_directory, filename)
        
        # Сохраняем изображение
        image_data = base64.b64decode(base64_data)
        with open(output_path, 'wb') as f:
            f.write(image_data)
        
        # Создаем file URI
        import urllib.parse
        file_uri = urllib.parse.urljoin('file://', urllib.parse.quote(os.path.abspath(output_path)))
        
        # Возвращаем результат
        return [
            TextContent(
                type="text", 
                text=f"✅ Изображение успешно сгенерировано и сохранено в '{output_path}' (URI: {file_uri})"
            )
        ]
        
    except Exception as e:
        # Возвращаем ошибку как текстовый контент
        return [
            TextContent(
                type="text",
                text=f"❌ Ошибка при генерации изображения в файл: {str(e)}"
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
