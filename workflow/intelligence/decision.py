"""
Decision Engine для принятия решений о продолжении workflow
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models import Decision, ValidationResult, StepResult, WorkflowStep, WorkflowContext, DecisionType
from ..policy.engine import PolicyEngine

logger = logging.getLogger(__name__)


class DecisionEngine:
    """Движок принятия решений для enhanced workflow"""
    
    def __init__(self):
        self.policy_engine = PolicyEngine()
        self.decision_history: Dict[str, List[Dict[str, Any]]] = {}
        
    async def make_decision(self, validation_result: ValidationResult,
                           step_result: StepResult, step: WorkflowStep,
                           context: WorkflowContext,
                           step_history: List[StepResult] = None) -> Decision:
        """Принять решение о дальнейших действиях"""
        
        try:
            logger.info(f"🤔 Making decision for step '{step.id}' (score: {validation_result.overall_score:.2f})")
            
            # Анализируем текущую ситуацию
            situation_analysis = await self._analyze_situation(
                validation_result, step_result, step, context, step_history
            )
            
            # Определяем действие на основе политик
            action = await self._determine_action(situation_analysis, step)
            
            # Генерируем обоснование решения
            reason = await self._generate_reason(action, situation_analysis)
            
            # Вычисляем уверенность в решении
            confidence = await self._calculate_confidence(action, situation_analysis)
            
            # Генерируем предложения по модификации
            modifications = await self._suggest_modifications(action, situation_analysis, step)
            
            # Оценка влияния на ресурсы
            resource_impact = await self._assess_resource_impact(action, situation_analysis)
            
            # Подсказки для следующего шага
            next_step_hints = await self._generate_next_step_hints(action, validation_result)
            
            decision = Decision(
                action=action,
                reason=reason,
                confidence=confidence,
                suggested_modifications=modifications,
                resource_impact=resource_impact,
                next_step_hints=next_step_hints
            )
            
            # Записываем решение в историю
            await self._record_decision(step.id, decision, situation_analysis)
            
            logger.info(f"✅ Decision for step '{step.id}': {action} (confidence: {confidence:.2f})")
            
            return decision
            
        except Exception as e:
            logger.error(f"❌ Failed to make decision for step '{step.id}': {e}")
            # Возвращаем безопасное решение
            return Decision(
                action=DecisionType.STOP.value,
                reason=f"Decision engine error: {e}",
                confidence=0.0,
                suggested_modifications={"error": str(e)},
                resource_impact={"decision_failed": True}
            )
    
    async def _analyze_situation(self, validation_result: ValidationResult,
                                step_result: StepResult, step: WorkflowStep,
                                context: WorkflowContext,
                                step_history: List[StepResult] = None) -> Dict[str, Any]:
        """Анализ текущей ситуации"""
        
        analysis = {
            "quality_status": "unknown",
            "retry_count": step_result.retry_count,
            "error_class": validation_result.error_class,
            "improvement_potential": "unknown",
            "resource_consumption": {},
            "escalation_triggers": [],
            "historical_pattern": "none"
        }
        
        # Анализируем качество
        score = validation_result.overall_score
        if score >= 0.8:
            analysis["quality_status"] = "excellent"
        elif score >= 0.7:
            analysis["quality_status"] = "good"
        elif score >= 0.5:
            analysis["quality_status"] = "acceptable"
        elif score >= 0.3:
            analysis["quality_status"] = "poor"
        else:
            analysis["quality_status"] = "critical"
        
        # Анализируем потенциал улучшения
        if validation_result.improvement_suggestions:
            suggestion_count = len(validation_result.improvement_suggestions)
            if suggestion_count <= 2:
                analysis["improvement_potential"] = "high"
            elif suggestion_count <= 4:
                analysis["improvement_potential"] = "medium"
            else:
                analysis["improvement_potential"] = "low"
        
        # Анализируем потребление ресурсов
        if step_result.duration_seconds:
            analysis["resource_consumption"]["time"] = step_result.duration_seconds
        if hasattr(step_result, 'token_count'):
            analysis["resource_consumption"]["tokens"] = step_result.token_count
        
        # Проверяем триггеры эскалации
        should_escalate, escalation_reason = self.policy_engine.should_escalate(
            step_result, step, context
        )
        if should_escalate:
            analysis["escalation_triggers"].append(escalation_reason)
        
        # Анализируем исторический паттерн
        if step_history:
            pattern = await self._analyze_historical_pattern(step.id, step_history)
            analysis["historical_pattern"] = pattern
        
        return analysis
    
    async def _determine_action(self, situation_analysis: Dict[str, Any], 
                               step: WorkflowStep) -> str:
        """Определить действие на основе анализа ситуации"""
        
        quality_status = situation_analysis["quality_status"]
        retry_count = situation_analysis["retry_count"]
        error_class = situation_analysis["error_class"]
        escalation_triggers = situation_analysis["escalation_triggers"]
        
        # Проверяем критичные ситуации
        if escalation_triggers:
            if "security_violation" in str(escalation_triggers):
                return DecisionType.STOP.value
            elif "retry_budget_exhausted" in str(escalation_triggers):
                return DecisionType.HUMAN_REQUIRED.value
        
        # Проверяем качество
        if quality_status in ["excellent", "good"]:
            return DecisionType.PROCEED.value
        
        # Анализируем возможность retry
        if retry_count >= 3:
            if quality_status == "acceptable":
                return DecisionType.PROCEED.value  # Приемлемое качество после многих попыток
            else:
                return DecisionType.ESCALATE.value
        
        # Определяем стратегию retry на основе типа ошибки
        if error_class == "security_violation":
            return DecisionType.STOP.value
        elif error_class == "timeout":
            if retry_count < 2:
                return DecisionType.RETRY.value
            else:
                return DecisionType.ALTERNATE.value
        elif error_class in ["low_quality", "validation_failed"]:
            if situation_analysis["improvement_potential"] == "high":
                return DecisionType.RETRY.value
            elif retry_count < 2:
                return DecisionType.RETRY.value
            else:
                return DecisionType.ALTERNATE.value
        elif error_class == "format_error":
            return DecisionType.RETRY.value
        elif error_class == "parsing_error":
            # JSON parsing ошибки - retry с лимитом (LLM может исправиться)
            if retry_count < 2:
                return DecisionType.RETRY.value
            else:
                return DecisionType.STOP.value
        
        # Дефолтное поведение
        # Для критических ошибок (missing files) - останавливаем workflow сразу
        if error_class in ["file_not_found", "critical_error"] or quality_status == "critical":
            return DecisionType.STOP.value
        elif quality_status in ["excellent", "good", "acceptable"]:
            return DecisionType.PROCEED.value
        elif retry_count < 2:
            return DecisionType.RETRY.value
        else:
            return DecisionType.ESCALATE.value
    
    async def _generate_reason(self, action: str, situation_analysis: Dict[str, Any]) -> str:
        """Генерировать обоснование решения"""
        
        quality_status = situation_analysis["quality_status"]
        retry_count = situation_analysis["retry_count"]
        error_class = situation_analysis["error_class"]
        improvement_potential = situation_analysis["improvement_potential"]
        
        if action == DecisionType.PROCEED.value:
            if quality_status in ["excellent", "good"]:
                return f"Quality {quality_status} - proceeding to next step"
            else:
                return f"Acceptable quality after {retry_count} attempts - proceeding"
        
        elif action == DecisionType.RETRY.value:
            if error_class == "format_error":
                return "Format issues detected - retry with corrected structure"
            elif improvement_potential == "high":
                return f"Quality {quality_status} but high improvement potential - retry with suggestions"
            else:
                return f"Quality {quality_status} - attempting retry #{retry_count + 1}"
        
        elif action == DecisionType.ALTERNATE.value:
            return f"Multiple retries unsuccessful (quality: {quality_status}) - trying alternative approach"
        
        elif action == DecisionType.ESCALATE.value:
            if retry_count >= 3:
                return "Retry budget exhausted - escalating for review"
            else:
                return f"Quality persistently {quality_status} - escalating"
        
        elif action == DecisionType.STOP.value:
            if error_class == "security_violation":
                return "Security violation detected - stopping immediately"
            else:
                return "Critical error - stopping workflow"
        
        elif action == DecisionType.HUMAN_REQUIRED.value:
            return "Human intervention required to resolve issues"
        
        return f"Action {action} based on current situation"
    
    async def _calculate_confidence(self, action: str, situation_analysis: Dict[str, Any]) -> float:
        """Вычислить уверенность в решении"""
        
        base_confidence = 0.5
        
        quality_status = situation_analysis["quality_status"]
        retry_count = situation_analysis["retry_count"]
        improvement_potential = situation_analysis["improvement_potential"]
        
        # Корректировки на основе качества
        quality_confidence = {
            "excellent": 0.95,
            "good": 0.85,
            "acceptable": 0.7,
            "poor": 0.4,
            "critical": 0.2
        }
        base_confidence = quality_confidence.get(quality_status, 0.5)
        
        # Корректировки на основе действия
        if action == DecisionType.PROCEED.value:
            if quality_status in ["excellent", "good"]:
                confidence = 0.9
            else:
                confidence = 0.7
        elif action == DecisionType.RETRY.value:
            if improvement_potential == "high":
                confidence = 0.8
            elif retry_count == 0:
                confidence = 0.7
            else:
                confidence = 0.6 - (retry_count * 0.1)
        elif action == DecisionType.ALTERNATE.value:
            confidence = 0.6
        elif action == DecisionType.ESCALATE.value:
            confidence = 0.8  # Высокая уверенность в эскалации
        elif action == DecisionType.STOP.value:
            confidence = 0.9  # Высокая уверенность в остановке при критичных ошибках
        elif action == DecisionType.HUMAN_REQUIRED.value:
            confidence = 0.85
        else:
            confidence = base_confidence
        
        return max(0.1, min(1.0, confidence))
    
    async def _suggest_modifications(self, action: str, situation_analysis: Dict[str, Any],
                                    step: WorkflowStep) -> Dict[str, Any]:
        """Предложить модификации для следующей попытки"""
        
        modifications = {}
        
        if action == DecisionType.RETRY.value:
            error_class = situation_analysis["error_class"]
            
            if error_class == "format_error":
                modifications["format_correction"] = True
                modifications["add_format_examples"] = True
            elif error_class == "low_quality":
                modifications["enhance_prompt"] = True
                modifications["add_quality_criteria"] = True
            elif error_class == "validation_failed":
                modifications["clarify_requirements"] = True
                modifications["add_validation_context"] = True
            
            # Общие модификации для retry
            modifications["increase_detail_level"] = True
            modifications["add_step_by_step_instructions"] = True
        
        elif action == DecisionType.ALTERNATE.value:
            modifications["use_alternative_agent"] = True
            modifications["simplify_approach"] = True
            modifications["break_into_smaller_steps"] = True
        
        elif action == DecisionType.ESCALATE.value:
            modifications["prepare_human_context"] = True
            modifications["document_failure_reasons"] = True
        
        return modifications
    
    async def _assess_resource_impact(self, action: str, 
                                     situation_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Оценить влияние решения на ресурсы"""
        
        impact = {
            "additional_cost": 0.0,
            "time_delay": 0,
            "token_consumption": 0,
            "human_time_required": 0
        }
        
        if action == DecisionType.RETRY.value:
            impact["additional_cost"] = 0.5  # Примерная стоимость retry
            impact["time_delay"] = 30  # секунд
            impact["token_consumption"] = 5000  # примерно
        
        elif action == DecisionType.ALTERNATE.value:
            impact["additional_cost"] = 1.0
            impact["time_delay"] = 60
            impact["token_consumption"] = 10000
        
        elif action == DecisionType.HUMAN_REQUIRED.value:
            impact["human_time_required"] = 1800  # 30 минут
            impact["time_delay"] = 3600  # Может ждать час
        
        return impact
    
    async def _generate_next_step_hints(self, action: str, 
                                       validation_result: ValidationResult) -> List[str]:
        """Генерировать подсказки для следующего шага"""
        
        hints = []
        
        if action == DecisionType.PROCEED.value:
            hints.append("Используйте результат текущего шага как входные данные")
            if validation_result.overall_score < 0.9:
                hints.append("Учтите потенциальные неточности в данных")
        
        elif action == DecisionType.RETRY.value:
            hints.extend(validation_result.improvement_suggestions)
            hints.append("Сосредоточьтесь на устранении выявленных проблем")
        
        elif action == DecisionType.ALTERNATE.value:
            hints.append("Попробуйте другой подход к решению задачи")
            hints.append("Упростите требования если возможно")
        
        return hints
    
    async def _analyze_historical_pattern(self, step_id: str, 
                                         step_history: List[StepResult]) -> str:
        """Анализ исторических паттернов для шага"""
        
        if not step_history:
            return "none"
        
        # Считаем паттерны
        recent_results = step_history[-5:]  # Последние 5 результатов
        quality_scores = [r.quality_score for r in recent_results if r.quality_score > 0]
        
        if not quality_scores:
            return "insufficient_data"
        
        avg_quality = sum(quality_scores) / len(quality_scores)
        
        if avg_quality > 0.8:
            return "consistently_good"
        elif avg_quality > 0.6:
            return "variable_quality"
        else:
            return "consistently_poor"
    
    async def _record_decision(self, step_id: str, decision: Decision,
                              situation_analysis: Dict[str, Any]):
        """Записать решение в историю"""
        
        if step_id not in self.decision_history:
            self.decision_history[step_id] = []
        
        record = {
            "timestamp": datetime.now().isoformat(),
            "action": decision.action,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "situation": situation_analysis,
            "modifications": decision.suggested_modifications
        }
        
        self.decision_history[step_id].append(record)
        
        # Ограничиваем историю
        if len(self.decision_history[step_id]) > 10:
            self.decision_history[step_id] = self.decision_history[step_id][-10:]
    
    def get_decision_history(self, step_id: str = None) -> Dict[str, Any]:
        """Получить историю решений"""
        if step_id:
            return self.decision_history.get(step_id, [])
        return self.decision_history
