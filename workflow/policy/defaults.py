"""
Дефолтные политики для enhanced workflow
"""
from .models import QualityGate, RetryStrategy, Budget, EscalationPolicy, ValidationConfig, RetryStrategyType
from ..models import Policy


def get_default_quality_gate() -> QualityGate:
    """Дефолтные ворота качества"""
    return QualityGate(
        min_quality_score=0.7,
        soft_fail_threshold=0.5,
        hard_fail_threshold=0.3,
        required_validators=["structural", "completeness"],
        custom_rules={}
    )


def get_default_retry_strategy() -> RetryStrategy:
    """Дефолтная стратегия повторов"""
    return RetryStrategy(
        max_retries=3,
        strategies={
            1: RetryStrategyType.REFINE_PROMPT,
            2: RetryStrategyType.ADD_CONTEXT,
            3: RetryStrategyType.ALTERNATE_AGENT
        },
        backoff_multiplier=1.5,
        max_backoff_seconds=60,
        error_specific={
            "timeout": {
                "max_retries": 2,
                "strategies": ["extend_timeout", "alternate_agent"]
            },
            "empty_response": {
                "max_retries": 2, 
                "strategies": ["refine_prompt", "add_examples"]
            },
            "low_quality": {
                "max_retries": 3,
                "strategies": ["refine_prompt", "add_context", "peer_review"]
            }
        }
    )


def get_default_budget() -> Budget:
    """Дефолтный бюджет ресурсов"""
    return Budget(
        max_tokens=32768,
        max_duration_seconds=300,
        max_cost_usd=5.0,
        max_retries=3
    )


def get_default_escalation_policy() -> EscalationPolicy:
    """Дефолтная политика эскалации"""
    return EscalationPolicy(
        triggers=[
            "retry_budget_exhausted",
            "quality_persistently_low", 
            "security_violation"
        ],
        notification_channels=[
            "log:error"
        ],
        timeout_seconds=3600,
        auto_actions={
            "retry_budget_exhausted": "human_escalation",
            "security_violation": "stop_immediately"
        }
    )


def get_default_validation_config() -> ValidationConfig:
    """Дефолтная конфигурация валидации"""
    return ValidationConfig(
        structural={
            "enabled": True,
            "required_fields": ["output"],
            "min_length": 10
        },
        completeness={
            "enabled": True,
            "coverage_threshold": 0.8
        },
        semantic={
            "enabled": False,  # Expensive, enable selectively
            "fact_check": False,
            "hallucination_check": False
        },
        security={
            "enabled": True,
            "sql_injection_check": True,
            "xss_check": True
        },
        business_rules=[]
    )


def get_default_policy() -> Policy:
    """Получить дефолтную политику"""
    quality_gate = get_default_quality_gate()
    retry_strategy = get_default_retry_strategy()
    budget = get_default_budget()
    escalation = get_default_escalation_policy()
    validation = get_default_validation_config()
    
    return Policy(
        name="default",
        version="1.0",
        quality_gates={
            "default": quality_gate.__dict__
        },
        validation_rules=validation.__dict__,
        retry_policies={
            "default": retry_strategy.__dict__
        },
        budgets={
            "per_step": budget.__dict__,
            "per_workflow": Budget(
                max_tokens=500000,
                max_duration_seconds=1800,
                max_cost_usd=50.0,
                max_retries=10
            ).__dict__
        },
        escalation=escalation.__dict__
    )
