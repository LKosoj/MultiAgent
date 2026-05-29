import random
import base64
import os
import asyncio
import urllib.parse
from mcp_tools import mcp_tools

def generate_image_to_file_tool(prompt: str, directory: str, filename: str, width: int = 1920, height: int = 1080) -> str:
    """Генерирует изображение и сохраняет его в указанный каталог с заданным именем файла.
    
    Args:
        prompt: Строка запроса для генерации изображения
        directory: Каталог для сохранения изображения
        filename: Имя файла для сохранения (с расширением)
        width: Ширина изображения (128-2048)
        height: Высота изображения (128-2048)
    Returns:
        str: Полный путь к созданному файлу или сообщение об ошибке
    """
    
    # Пробуем MCP сервер для генерации в файл
    try:
        return _generate_to_file_via_direct_mcp(prompt, directory, filename, width, height)
    except Exception as mcp_error:
        return f"Ошибка генерации через MCP: {mcp_error}"

def _generate_to_file_via_direct_mcp(prompt: str, directory: str, filename: str, width: int, height: int) -> str:
    """Прямое подключение к MCP серверу image-generator для сохранения в файл"""
    async def call_mcp_generate_to_file():
        from mcp import StdioServerParameters
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client
        import json
        
        # Загружаем настройки из mcp_servers.json
        with open("mcp_servers.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        
        # Находим настройки image-generator сервера
        gen_server_config = config["mcpServers"]["image-generator"]
        
        # Создаем параметры MCP сервера из конфигурации
        server_params = StdioServerParameters(
            command=gen_server_config["command"],
            args=gen_server_config["args"],
            env={
                **os.environ,
                **gen_server_config["env"]
            }
        )
        
        # Подключаемся к MCP серверу
        async with stdio_client(server_params) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Вызываем generate_image_to_file
                result = await session.call_tool("generate_image_to_file", {
                    "prompt": prompt,
                    "directory": directory,
                    "filename": filename,
                    "width": width,
                    "height": height
                })
                
                # Проверяем результат (возвращает только текст)
                for item in result.content:
                    if hasattr(item, 'type') and item.type == 'text':
                        if "✅" in item.text:
                            # Извлекаем URI из текста, если есть
                            if "(URI: " in item.text:
                                # Извлекаем file URI из сообщения
                                uri_start = item.text.find("(URI: ") + 6
                                uri_end = item.text.find(")", uri_start)
                                if uri_end > uri_start:
                                    file_uri = item.text[uri_start:uri_end]
                                    # Конвертируем file URI обратно в путь
                                    path = urllib.parse.urlparse(file_uri).path
                                    return urllib.parse.unquote(path)
                            
                            # Если URI нет, извлекаем путь из кавычек
                            if "сохранено в '" in item.text:
                                path_start = item.text.find("сохранено в '") + 13
                                path_end = item.text.find("'", path_start)
                                if path_end > path_start:
                                    return item.text[path_start:path_end]
                            
                            # Fallback: возвращаем составленный путь
                            return os.path.join(directory, filename)
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
            future = executor.submit(asyncio.run, call_mcp_generate_to_file())
            result_path = future.result(timeout=60)
    except RuntimeError:
        # Если нет event loop, создаем новый
        result_path = asyncio.run(call_mcp_generate_to_file())
    
    return result_path
