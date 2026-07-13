"""
Система памяти для мультиагентной системы
========================================

Пакет предоставляет современную систему памяти с темпоральной моделью данных,
автоматическим разрешением конфликтов и поддержкой исторических запросов.

Основные компоненты:
- models: Pydantic модели для валидации данных
- database: Низкоуровневая работа с SQLite и ChromaDB  
- manager: Основная логика управления памятью
- tools: Интерфейс инструментов для агентов
- rebuild: Утилиты для пересборки векторной БД
"""

from .models import TacticalMemoryItem, StrategicGoal, SystemContext
_TOOL_EXPORTS = frozenset(
    {
        "save_memory",
        "get_memory",
        "get_memory_summary",
        "save_goal",
        "get_goals",
        "update_goal_status",
        "save_context",
        "get_context",
        "extract_keywords",
        "summary_agent_memory_step",
        "agent_list",
    }
)


def __getattr__(name):
    if name not in _TOOL_EXPORTS:
        raise AttributeError(name)
    from . import tools

    value = getattr(tools, name)
    globals()[name] = value
    return value

__version__ = "2.0.0"
__all__ = [
    # Модели данных
    "TacticalMemoryItem",
    "StrategicGoal", 
    "SystemContext",
    
    # Инструменты для агентов
    "save_memory",
    "get_memory",
    "get_memory_summary", 
    "save_goal",
    "get_goals",
    "update_goal_status",
    "save_context",
    "get_context",
    "extract_keywords",
    "summary_agent_memory_step",
    "agent_list"
]
