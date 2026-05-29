# Глава 18: Система плагинов для БД (DBPluginManager)

Универсальный адаптер для разных СУБД (SQLite/Postgres/…): единый интерфейс, независимый от диалектов SQL и драйверов.

## Зачем
- Единообразный доступ к БД из агентов/пайплайнов.
- Расширяемость через плагины (по схеме DSN).
- Интроспекция схем, тест соединения, безопасное выполнение запросов.

## Как устроено
Реестр плагинов:
```python
# db_plugins/manager.py
_PLUGINS = {
  "sqlite": SQLitePlugin(),
  "postgres": PostgresPlugin(),
  "postgresql": PostgresPlugin(),
  "duckdb": DuckDBPlugin(),
}
```
Выбор плагина по DSN:
```python
from urllib.parse import urlparse

def get_plugin(dsn: str):
    scheme = urlparse(dsn).scheme.lower()
    plugin = _PLUGINS.get(scheme)
    if not plugin:
        raise ValueError(f"Нет плагина для схемы: {scheme}")
    return plugin
```

Интерфейс плагина (идея):
- connect(dsn)
- introspect_schema(conn)
- execute_select(conn, sql, …)
- explain(conn, sql)

## API для UI
```python
# db_plugins/streamlit_api.py
mgr = get_db_plugin_manager()
plugins = mgr.list_plugins()
valid = mgr.validate_dsn("postgresql://user:pass@host:5432/db.schema")
probe = mgr.test_connection("sqlite:///local.db")
```
- list_plugins: какие диалекты доступны.
- validate_dsn: проверка схемы/формата.
- test_connection: реальный коннект + базовая информация о схеме.

## Интроспекция (пример)
```python
# SQLitePlugin.introspect_schema(conn)
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
# ... парсинг и нормализация в стандартный формат ...
return standardized_schema
```
Плагин Postgres делает то же, но через information_schema.*

## Диаграмма
```mermaid
sequenceDiagram
  participant Client
  participant Manager as get_plugin()
  participant Registry as _PLUGINS
  participant Plugin

  Client->>Manager: get_plugin(dsn)
  Manager->>Registry: схема из dsn → плагин
  Registry-->>Manager: Plugin
  Manager-->>Client: Plugin
```

## Вывод
DBPluginManager скрывает различия СУБД и даёт единый интерфейс подключения, интроспекции и безопасного выполнения запросов, упрощая интеграцию в пайплайны.
