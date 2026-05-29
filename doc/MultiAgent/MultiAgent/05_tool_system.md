# Глава 5: Система инструментов (Tool System)

Инструменты — это «руки» агентов: функции/классы для работы с файлами, вебом, БД, ML и т. д. Система инструментов централизует:
- декларативные описания (YAML) и динамическую загрузку;
- единый каталог `tool_mapping` для фабрики;
- телеметрию и логи через `ToolManager`.

## Как устроено
- Описания лежат в `tool_definitions/*.yaml`.
- При старте выполняется `load_tools()` → формируется словарь имя → объект инструмента.
- `AgentFactory` при сборке читает `tools` из профиля и «выдаёт» готовые объекты.

## Пример YAML-описания
```yaml
# tool_definitions/duckduckgosearch.yaml
name: DuckDuckGoSearchTool
description: "Поиск информации в интернете"
category: "Веб"
source_type: class_instance
implementation_source: smolagents.DuckDuckGoSearchTool
parameters: []
```
Ключевые поля: `name`, `description`, `implementation_source`, `parameters`.

## Загрузка инструментов (упрощённо)
```python
def load_tools():
    tool_mapping = {}
    for filename in os.listdir('tool_definitions'):
        if filename.endswith('.yaml'):
            cfg = yaml.safe_load(open(os.path.join('tool_definitions', filename)))
            module_path, obj_name = cfg['implementation_source'].rsplit('.', 1)
            module = importlib.import_module(module_path)
            obj = getattr(module, obj_name)
            tool_mapping[cfg['name']] = obj() if cfg.get('source_type') == 'class_instance' else obj
    # интеграция внешних (MCP) инструментов при наличии
    return tool_mapping
```

## Наблюдаемость: связка с ToolManager
- Вызовы инструментов оборачиваются `ToolManager.run_tool(...)`.
- Декоратор `@with_telemetry(name, description)` делает функцию «инструментом» с трассировкой и логами.

## Добавление нового инструмента (пример)
1) Код:
```python
# custom_tools/greeting.py
def say_hello() -> str:
    return "Hello, World!"
```
2) YAML:
```yaml
# tool_definitions/say_hello.yaml
name: say_hello
source_type: custom_function
implementation_source: custom_tools.greeting.say_hello
```
3) Профиль агента:
```yaml
# agent_profiles/researcher.yaml
tools:
  - DuckDuckGoSearchTool
  - say_hello
```
Перезапуск — и инструмент доступен всем агентам с этим профилем.

## Вывод
- YAML-определения + динамическая загрузка дают расширяемость.
- Единый каталог инструментария упрощает выдачу навыков агентам.
- Интеграция с `ToolManager` обеспечивает телеметрию и контроль.
