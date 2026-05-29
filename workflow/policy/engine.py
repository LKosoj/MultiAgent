"""
Policy Engine для применения политик качества и управления
"""
import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from ..models import Policy, StepResult, WorkflowContext, WorkflowStep
from .registry import PolicyRegistry
from .models import QualityGate, RetryStrategy, Budget

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Движок для применения политик качества и решений"""
    
    def __init__(self):
        self.registry = PolicyRegistry()
        
    def get_quality_gate(self, step: WorkflowStep, context: WorkflowContext) -> QualityGate:
        """Получить ворота качества для шага"""
        policy = self.registry.get_policy()
        
        # Проверяем step-specific настройки
        step_settings = getattr(step, 'enhanced_settings', {})
        if 'quality_gate' in step_settings:
            # Merge step-specific с дефолтными
            default_gate = policy.quality_gates.get("default", {})
            step_gate = step_settings['quality_gate']
            
            return QualityGate(
                min_quality_score=step_gate.get('min_quality_score', default_gate.get('min_quality_score', 0.7)),
                soft_fail_threshold=step_gate.get('soft_fail_threshold', default_gate.get('soft_fail_threshold', 0.5)),
                hard_fail_threshold=step_gate.get('hard_fail_threshold', default_gate.get('hard_fail_threshold', 0.3)),
                required_validators=step_gate.get('required_validators', default_gate.get('required_validators', [])),
                custom_rules=step_gate.get('custom_rules', default_gate.get('custom_rules', {}))
            )
        
        # Используем дефолтные настройки
        default_gate = policy.quality_gates.get("default", {})
        return QualityGate(**default_gate)
    
    def get_retry_strategy(self, error_class: str, step: WorkflowStep) -> RetryStrategy:
        """Получить стратегию повторов для типа ошибки"""
        policy = self.registry.get_policy()
        
        # Проверяем step-specific настройки
        step_settings = getattr(step, 'enhanced_settings', {})
        if 'retry_strategy' in step_settings:
            step_retry = step_settings['retry_strategy']
            return RetryStrategy(**step_retry)
        
        # Проверяем error-specific стратегии
        retry_policies = policy.retry_policies.get("default", {})
        error_specific = retry_policies.get("error_specific", {})
        
        if error_class in error_specific:
            return RetryStrategy(**error_specific[error_class])
        
        # Используем дефолтную стратегию
        return RetryStrategy(**retry_policies)
    
    def get_budget(self, budget_type: str, step: WorkflowStep = None) -> Budget:
        """Получить бюджет ресурсов"""
        policy = self.registry.get_policy()
        
        # Проверяем step-specific настройки
        if step:
            step_settings = getattr(step, 'enhanced_settings', {})
            if 'budget' in step_settings:
                return Budget(**step_settings['budget'])
        
        # Используем политику
        budgets = policy.budgets
        budget_config = budgets.get(budget_type, budgets.get("per_step", {}))
        return Budget(**budget_config)
    
    def should_escalate(self, step_result: StepResult, step: WorkflowStep, 
                       context: WorkflowContext) -> Tuple[bool, str]:
        """Определить нужна ли эскалация"""
        policy = self.registry.get_policy()
        escalation_config = policy.escalation
        
        triggers = escalation_config.get("triggers", [])
        
        # Проверяем триггеры эскалации
        for trigger in triggers:
            if trigger == "retry_budget_exhausted" and step_result.retry_count >= 3:
                return True, "Превышен лимит повторных попыток"
            elif trigger == "quality_persistently_low" and step_result.quality_score < 0.3:
                return True, "Устойчиво низкое качество результата"
            elif trigger == "security_violation" and step_result.error_class == "security_violation":
                return True, "Нарушение безопасности"
        
        return False, ""
    
    def evaluate_quality_gate(self, step_result: StepResult, 
                             quality_gate: QualityGate) -> Dict[str, Any]:
        """Оценить прохождение ворот качества"""
        
        quality_score = step_result.quality_score
        
        # Определяем уровень прохождения
        if quality_score >= quality_gate.min_quality_score:
            gate_status = "passed"
            severity = "info"
        elif quality_score >= quality_gate.soft_fail_threshold:
            gate_status = "soft_fail"
            severity = "warning"
        else:
            gate_status = "hard_fail"
            severity = "error"
        
        # Проверяем required validators
        missing_validators = []
        for validator in quality_gate.required_validators:
            validator_found = any(
                v.get("validator_name") == validator 
                for v in step_result.validator_results
            )
            if not validator_found:
                missing_validators.append(validator)
        
        return {
            "gate_status": gate_status,
            "severity": severity,
            "quality_score": quality_score,
            "threshold_met": quality_score >= quality_gate.min_quality_score,
            "missing_validators": missing_validators,
            "evaluation_time": datetime.now().isoformat(),
            "gate_config": {
                "min_score": quality_gate.min_quality_score,
                "soft_threshold": quality_gate.soft_fail_threshold,
                "hard_threshold": quality_gate.hard_fail_threshold
            }
        }
    
    def get_validation_config(self, step: WorkflowStep) -> Dict[str, Any]:
        """Получить конфигурацию валидации для шага"""
        policy = self.registry.get_policy()
        
        # Базовая конфигурация из политики
        base_config = policy.validation_rules
        
        # Step-specific настройки
        step_settings = getattr(step, 'enhanced_settings', {})
        step_validation = step_settings.get('validation', {})
        
        # Merge конфигураций
        merged_config = dict(base_config)
        for key, value in step_validation.items():
            if isinstance(value, dict) and key in merged_config:
                merged_config[key].update(value)
            else:
                merged_config[key] = value
        
        return merged_config
    
    def record_policy_decision(self, decision: str, reason: str, 
                              context: Dict[str, Any]):
        """Записать решение политики для аудита"""
        audit_record = {
            "timestamp": datetime.now().isoformat(),
            "policy_version": self.registry.active_version,
            "decision": decision,
            "reason": reason,
            "context_hash": str(hash(str(context))),
            "workflow_id": context.get("workflow_id"),
            "step_id": context.get("step_id")
        }
        
        logger.info(f"📋 Policy decision: {decision} - {reason}")
        # Здесь можно добавить запись в audit log
        
    def get_policy_stats(self) -> Dict[str, Any]:
        """Получить статистику использования политик"""
        return self.registry.get_usage_stats()
