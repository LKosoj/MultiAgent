# Workflow Engine

Надежная система выполнения рабочих процессов на основе мультиагентной архитектуры MultiAgent.

## 🚀 Быстрый старт

```python
from workflow import WorkflowEngine

# Создаем workflow engine (все возможности DynamicAgentSystem сохраняются!)
engine = WorkflowEngine()

# Определяем простой workflow
workflow_def = {
    "name": "data_analysis",
    "steps": [
        {
            "id": "collect",
            "agent_type": "researcher", 
            "task": "Собери данные о трендах ИИ"
        },
        {
            "id": "analyze",
            "agent_type": "analyst",
            "task": "Проанализируй тренды",
            "depends_on": ["collect"]
        }
    ]
}

# Выполняем workflow
result = await engine.execute_workflow(workflow_def)
print(f"Статус: {result.status}")
```

## 📁 Структура

- `__init__.py` - Экспорт основных компонентов
- `models.py` - Модели данных (WorkflowDefinition, StepResult, etc.)
- `engine.py` - Главный WorkflowEngine класс
- `state_manager.py` - Управление состоянием и checkpoint'ы
- `retry_engine.py` - Механизмы повторных попыток
- `resource_manager.py` - Управление ресурсами и квотами

## 🔗 Ключевые особенности

- ✅ **Полная совместимость** с DynamicAgentSystem
- ✅ **Персистентность** - восстановление после сбоев
- ✅ **Retry логика** - автоматические повторы
- ✅ **Изоляция ресурсов** - предотвращение "шумного соседа"
- ✅ **Checkpoint'инг** - сохранение прогресса
- ✅ **Мониторинг** - полная наблюдаемость

## 📖 Документация

Полная документация: [WORKFLOW_ENGINE.md](../doc/WORKFLOW_ENGINE.md)

## 🧪 Примеры

Примеры использования: [workflow_examples.py](../examples/workflow_examples.py)

## 🏗️ Архитектура

```
WorkflowEngine (наследует DynamicAgentSystem)
├── StateManager      # SQLite + RAG память
├── RetryEngine       # Exponential backoff, circuit breaker
├── ResourceManager   # Квоты, изоляция, fair sharing
└── Models           # Типизированные данные
```

Workflow Engine реализует концепцию **"рабочие процессы как базы данных"** для enterprise-grade ИИ приложений.
