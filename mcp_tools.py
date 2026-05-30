import os
import json
import base64
import mcp
from mcp import StdioServerParameters
from smolagents.mcp_client import MCPClient
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Метаданные загруженных MCP-серверов
mcp_server_metadata: dict = {}


def load_mcp_servers_from_json(json_path: str):
    """
    Загружает MCP серверы из JSON конфигурации.
    Поддерживает новый стандартный формат конфигурации с расширенными возможностями.
    
    Поддерживаемые поля:
    - isActive: включен/выключен сервер
    - name: имя сервера  
    - type: тип сервера (stdio, http, sse, inMemory)
    - description: описание сервера
    - longRunning: флаг долгоработающего процесса
    - command: команда для stdio серверов
    - args: аргументы команды
    - env: переменные окружения
    - baseUrl: URL для http/sse серверов
    - timeout: таймаут соединения
    - disabledTools: список отключенных инструментов
    - autoApprove: список автоодобряемых инструментов
    - registryUrl: URL реестра
    - provider: провайдер сервера
    """
    with open(json_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    servers = []
    server_metadata = {}  # Для хранения дополнительной информации о серверах
    mcp_servers = config.get("mcpServers", {})
    
    for server_id, server in mcp_servers.items():
        # Пропускаем неактивные серверы
        if not server.get("isActive", True):
            print(f"Пропущен неактивный сервер: {server.get('name', server_id)}")
            continue
            
        server_type = server.get("type", "stdio")
        server_name = server.get("name", server_id)
        description = server.get("description", "")
        long_running = server.get("longRunning", False)
        timeout = server.get("timeout", 30)
        
        # Сохраняем метаданные сервера
        server_metadata[server_id] = {
            "name": server_name,
            "description": description,
            "longRunning": long_running,
            "timeout": timeout,
            "disabledTools": server.get("disabledTools", []),
            "autoApprove": server.get("autoApprove", []),
            "provider": server.get("provider", ""),
            "registryUrl": server.get("registryUrl", "")
        }
        
        if server_type == "stdio":
            env = server.get("env", {})
            # Объединяем переменные окружения системы и сервера
            env = {**os.environ, **env}
            
            if not server.get("command"):
                print(f"Предупреждение: Пропущен stdio сервер {server_name} - отсутствует команда")
                continue
                
            params = StdioServerParameters(
                command=server["command"],
                args=server.get("args", []),
                env=env
            )
            servers.append(params)
            print(f"Загружен stdio сервер: {server_name}")
            
        elif server_type == "http" or server_type == "sse":
            # Обработка HTTP и SSE серверов
            url = server.get("baseUrl", server.get("url", ""))
            if not url:
                print(f"Предупреждение: Пропущен {server_type} сервер {server_name} - отсутствует baseUrl/url")
                continue
                
            env = server.get("env", {})
            smithery_api_key = env.get("API_KEY")
            
            if smithery_api_key:
                config_dict = {
                    "smitheryApiKey": smithery_api_key,
                    "dynamic": False
                }
                config_b64 = base64.urlsafe_b64encode(json.dumps(config_dict).encode()).decode()
                parsed = urlparse(url)
                query = parse_qs(parsed.query)
                query["config"] = [config_b64]
                query["api_key"] = [smithery_api_key]
                new_query = urlencode(query, doseq=True)
                url = urlunparse(parsed._replace(query=new_query))
                
            params = {"url": url, "transport": "sse"}
            servers.append(params)
            print(f"Загружен {server_type} сервер: {server_name}")
            
        elif server_type == "inMemory":
            # Для in-memory серверов (например, Cherry AI)
            print(f"Пропущен in-memory сервер: {server_name} (не поддерживается в текущей реализации)")
            continue
            
        else:
            print(f"Предупреждение: Неизвестный тип MCP-сервера: {server_type} для {server_name}")
    
    # Сохраняем метаданные для возможного использования
    global mcp_server_metadata
    mcp_server_metadata = server_metadata

    return servers

def get_server_info(server_id: str = None):
    """
    Получает информацию о MCP серверах.
    
    Args:
        server_id: ID конкретного сервера. Если None, возвращает информацию обо всех серверах.
        
    Returns:
        dict: Информация о сервере(ах)
    """
    if server_id:
        return mcp_server_metadata.get(server_id, {})
    return mcp_server_metadata


def list_active_servers():
    """Выводит список активных MCP серверов."""
    print("\n=== Активные MCP серверы ===")
    for server_id, info in mcp_server_metadata.items():
        print(f"• {info['name']} ({server_id})")
        if info['description']:
            print(f"  Описание: {info['description']}")
        print(f"  Тип: {info.get('type', 'stdio')}")
        if info['provider']:
            print(f"  Провайдер: {info['provider']}")
        if info['disabledTools']:
            print(f"  Отключенные инструменты: {', '.join(info['disabledTools'])}")
        print()


mcp_tools = []
mcp_clients = {}

# Загружаем серверы из JSON конфигурации
_mcp_config_path = os.environ.get("MCP_SERVERS_CONFIG", "mcp_servers.json")
try:
    servers = load_mcp_servers_from_json(_mcp_config_path)
except Exception as e:
    print(f"Предупреждение: не удалось загрузить конфигурацию MCP-серверов ({_mcp_config_path}): {e}")
    servers = []

try:
    server_ids = list(mcp_server_metadata.keys())
    for index, server in enumerate(servers):
        mcp_client = MCPClient(server, structured_output=False)
        server_id = server_ids[index] if index < len(server_ids) else f"server_{index}"
        mcp_clients[server_id] = mcp_client
        server_name = mcp_server_metadata.get(server_id, {}).get("name")
        if server_name:
            mcp_clients[server_name] = mcp_client
        mcp_tools.extend(mcp_client.get_tools())

    #print("Тулы успешно загружены:", [getattr(t, 'name', str(t)) for t in mcp_tools])
except Exception as e:
    print(f"Ошибка при загрузке инструментов: {e}")
finally:
    print("Инструменты успешно загружены:", [getattr(t, 'name', str(t)) for t in mcp_tools])
#     for mcp_client in mcp_clients:
#         mcp_client.disconnect()
