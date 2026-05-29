"""
Pydantic модели для валидации данных системы памяти
==================================================

Содержит модели для:
- TacticalMemoryItem: Тактическая память агентов
- StrategicGoal: Стратегические цели
- SystemContext: Системный контекст
"""

from typing import Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator


class TacticalMemoryItem(BaseModel):
    """Модель для тактической памяти агентов"""
    session_id: str = Field(..., description="ID сессии")
    agent_name: str = Field(..., description="Имя агента")
    data: Dict[str, Any] = Field(..., description="Данные памяти")
    
    @field_validator('data')
    @classmethod
    def validate_data_not_empty(cls, v):
        if not v or v == {}:
            raise ValueError("Данные не могут быть пустыми")
        
        # Проверяем, что есть содержательная информация
        has_content = False
        for key, value in v.items():
            if value is not None and value != "" and value != []:
                has_content = True
                break
        
        if not has_content:
            raise ValueError("Данные должны содержать полезную информацию")
        
        return v
    
    @field_validator('agent_name')
    @classmethod
    def validate_agent_name(cls, v):
        if not v or v.strip() == "":
            raise ValueError("Имя агента не может быть пустым")
        return v.strip()


class StrategicGoal(BaseModel):
    """Модель для стратегических целей"""
    session_id: str = Field(..., description="ID сессии")
    description: str = Field(..., description="Описание цели")
    status: Literal["pending", "in_progress", "completed", "cancelled"] = Field(
        default="pending", description="Статус цели"
    )
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        if not v or v.strip() == "":
            raise ValueError("Описание цели не может быть пустым")
        if len(v.strip()) < 5:
            raise ValueError("Описание цели должно содержать минимум 5 символов")
        return v.strip()


class SystemContext(BaseModel):
    """Модель для системного контекста"""
    session_id: str = Field(..., description="ID сессии")
    context: str = Field(..., description="Контекст системы")
    
    @field_validator('context')
    @classmethod
    def validate_context(cls, v):
        if not v or v.strip() == "":
            raise ValueError("Контекст не может быть пустым")
        if len(v.strip()) < 10:
            raise ValueError("Контекст должен содержать минимум 10 символов")
        return v.strip()
