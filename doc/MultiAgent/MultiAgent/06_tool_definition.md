# Глава 6: Определение инструмента (Tool Definition)

Определение инструмента — это YAML-«паспорт», описывающий имя, назначение и расположение кода. Такой подход позволяет добавлять новые навыки без правок ядра.

## Схема YAML (пример)
```yaml
# tool_definitions/file_write.yaml
name: file_write
description: "Записывает содержимое в файл. Для append=True — дописывает."
category: "Файлы"
source_type: custom_function
implementation_source: custom_tools.file_system_tools.file_write
parameters:
  - name: filename
    type: str
    description: Путь к файлу
    required: true
  - name: content
    type: str
    description: Содержимое для записи
    required: true
```
Ключевые поля: `name`, `description`, `implementation_source`, `parameters`.

## Как YAML оживает
Запуском `load_tools()` все `.yaml` сканируются, код импортируется по `implementation_source`, и формируется словарь имя → функция/объект.

```python
def load_tools():
    tool_mapping = {}
    for fn in os.listdir('tool_definitions'):
        if fn.endswith('.yaml'):
            cfg = yaml.safe_load(open(os.path.join('tool_definitions', fn)))
            module_path, obj_name = cfg['implementation_source'].rsplit('.', 1)
            module = importlib.import_module(module_path)
            obj = getattr(module, obj_name)
            tool_mapping[cfg['name']] = obj
    return tool_mapping
```

## Выдача инструментов агентам
`AgentFactory` читает список `tools` из профиля и поднимает объекты из каталога `tool_mapping`, подключая их к агенту.

## Вывод
YAML-определения делают инструменты самостоятельными и легко расширяемыми; динамическая загрузка убирает жёсткие зависимости из кода.
