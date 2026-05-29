"""
Модели для Policy Engine
"""
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from enum import Enum


class RetryStrategyType(Enum):
    """Типы стратегий retry"""
    REFINE_PROMPT = "refine_prompt"
    ADD_CONTEXT = "add_context"
    ALTERNATE_AGENT = "alternate_agent"
    HUMAN_ESCALATION = "human_escalation"
    REDUCE_COMPLEXITY = "reduce_complexity"


@dataclass
class QualityGate:
    """Настройки ворот качества"""
    min_quality_score: float = 0.7
    soft_fail_threshold: float = 0.5  # Warning but continue
    hard_fail_threshold: float = 0.3  # Must retry
    required_validators: List[str] = field(default_factory=list)
    custom_rules: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryStrategy:
    """Стратегия повторных попыток"""
    max_retries: int = 3
    strategies: Dict[int, RetryStrategyType] = field(default_factory=dict)
    backoff_multiplier: float = 1.5
    max_backoff_seconds: int = 60
    error_specific: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass 
class Budget:
    """Бюджеты ресурсов"""
    max_tokens: Optional[int] = None
    max_duration_seconds: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_retries: int = 3
    
    
@dataclass
class EscalationPolicy:
    """Политика эскалации"""
    triggers: List[str] = field(default_factory=list)
    notification_channels: List[str] = field(default_factory=list)
    timeout_seconds: int = 3600
    auto_actions: Dict[str, str] = field(default_factory=dict)


@dataclass
class ValidationConfig:
    """Конфигурация валидации"""
    structural: Dict[str, Any] = field(default_factory=dict)
    completeness: Dict[str, Any] = field(default_factory=dict)
    semantic: Dict[str, Any] = field(default_factory=dict)
    security: Dict[str, Any] = field(default_factory=dict)
    business_rules: List[str] = field(default_factory=list)
