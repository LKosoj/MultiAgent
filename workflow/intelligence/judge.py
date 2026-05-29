"""
Post-Step Judge для валидации результатов и оценки качества
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models import ValidationResult, StepResult, StepPlan, WorkflowStep
from ..policy.engine import PolicyEngine
from ..contracts.registry import ContractRegistry

logger = logging.getLogger(__name__)


class PostStepJudge:
    """Судья для валидации результатов шагов"""
    
    def __init__(self):
        self.policy_engine = PolicyEngine()
        self.contract_registry = ContractRegistry()
        
    async def validate_result(self, step_result: StepResult, plan: StepPlan,
                             step: WorkflowStep) -> ValidationResult:
        """Валидировать результат выполнения шага"""
        
        try:
            logger.info(f"⚖️ Validating result for step '{step.id}'")
            
            # Получаем контракт для типа шага
            contract = self.contract_registry.get_contract_for_step(step.id, step.agent_type)
            
            # Получаем конфигурацию валидации
            validation_config = self.policy_engine.get_validation_config(step)
            
            # Определяем какие валидаторы запускать
            enabled_validators = await self._get_enabled_validators(step, plan, validation_config)
            
            # Запускаем валидацию артефакта
            validation_result = await self.contract_registry.validate_artifact(
                artifact=step_result.output,
                contract=contract,
                enabled_validators=enabled_validators
            )
            
            # Анализируем результаты валидации
            analysis = await self._analyze_validation_results(
                validation_result, plan, step_result
            )
            
            # Генерируем предложения по улучшению
            improvements = await self._generate_improvement_suggestions(
                validation_result, step_result, step
            )
            
            # Классифицируем ошибку если есть
            error_class = await self._classify_error(step_result, validation_result)
            
            result = ValidationResult(
                step_id=step.id,
                overall_score=validation_result["overall_score"],
                validation_passed=validation_result["validation_passed"],
                validator_results=validation_result["validator_results"],
                error_class=error_class,
                improvement_suggestions=improvements,
                contract_compliance=analysis
            )
            
            logger.info(f"✅ Validation complete for step '{step.id}': "
                       f"score={result.overall_score:.2f}, passed={result.validation_passed}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Failed to validate step '{step.id}': {e}")
            # Возвращаем минимальный результат валидации
            return ValidationResult(
                step_id=step.id,
                overall_score=0.0,
                validation_passed=False,
                validator_results=[{
                    "validator_name": "system",
                    "passed": False,
                    "score": 0.0,
                    "message": f"Validation system error: {e}",
                    "timestamp": datetime.now().isoformat()
                }],
                error_class="system_error"
            )
    
    async def _get_enabled_validators(self, step: WorkflowStep, plan: StepPlan,
                                     validation_config: Dict[str, Any]) -> List[str]:
        """Определить какие валидаторы запускать"""
        
        enabled_validators = []
        
        # Проверяем каждый доступный валидатор
        available_validators = self.contract_registry.get_available_validators()
        
        for validator_name in available_validators:
            validator_config = validation_config.get(validator_name, {})
            
            # Проверяем глобальное включение
            if not validator_config.get("enabled", True):
                continue
                
            # Проверяем step-specific настройки
            step_settings = getattr(step, 'enhanced_settings', {})
            step_validation = step_settings.get('validation', {})
            
            if validator_name in step_validation:
                if not step_validation[validator_name].get("enabled", True):
                    continue
            
            # Проверяем требования плана
            if plan.quality_criteria.get("required_validators"):
                if validator_name not in plan.quality_criteria["required_validators"]:
                    continue
            
            enabled_validators.append(validator_name)
        
        # Если ничего не включено, используем базовые валидаторы
        if not enabled_validators:
            enabled_validators = ["structural", "completeness"]
        
        logger.debug(f"🔍 Enabled validators for step '{step.id}': {enabled_validators}")
        return enabled_validators
    
    async def _analyze_validation_results(self, validation_result: Dict[str, Any],
                                         plan: StepPlan, step_result: StepResult) -> Dict[str, Any]:
        """Анализ результатов валидации"""
        
        analysis = {
            "contract_compliance": validation_result["threshold_met"],
            "quality_gate_status": "unknown",
            "critical_issues": [],
            "warnings": [],
            "compliance_details": {}
        }
        
        overall_score = validation_result["overall_score"]
        min_threshold = plan.quality_criteria.get("min_score", 0.7)
        soft_threshold = plan.quality_criteria.get("soft_threshold", 0.5)
        hard_threshold = plan.quality_criteria.get("hard_threshold", 0.3)
        
        # Определяем статус качественных ворот
        if overall_score >= min_threshold:
            analysis["quality_gate_status"] = "passed"
        elif overall_score >= soft_threshold:
            analysis["quality_gate_status"] = "soft_fail"
            analysis["warnings"].append(f"Quality score {overall_score:.2f} below threshold {min_threshold}")
        else:
            analysis["quality_gate_status"] = "hard_fail"
            analysis["critical_issues"].append(f"Quality score {overall_score:.2f} critically low")
        
        # Анализируем результаты отдельных валидаторов
        for validator_result in validation_result["validator_results"]:
            validator_name = validator_result["validator_name"]
            
            if not validator_result["passed"]:
                issue_info = {
                    "validator": validator_name,
                    "score": validator_result["score"],
                    "message": validator_result["message"]
                }
                
                if validator_result["score"] < 0.3:
                    analysis["critical_issues"].append(issue_info)
                else:
                    analysis["warnings"].append(issue_info)
            
            # Детали соответствия контракту
            analysis["compliance_details"][validator_name] = {
                "passed": validator_result["passed"],
                "score": validator_result["score"],
                "details": validator_result.get("details", {})
            }
        
        return analysis
    
    async def _generate_improvement_suggestions(self, validation_result: Dict[str, Any],
                                               step_result: StepResult, step: WorkflowStep) -> List[str]:
        """Генерировать предложения по улучшению"""
        
        suggestions = []
        
        # Анализируем результаты валидаторов для генерации предложений
        for validator_result in validation_result["validator_results"]:
            validator_name = validator_result["validator_name"]
            
            if not validator_result["passed"]:
                if validator_name == "structural":
                    suggestions.extend(self._get_structural_improvements(validator_result))
                elif validator_name == "completeness":
                    suggestions.extend(self._get_completeness_improvements(validator_result))
                elif validator_name == "security":
                    suggestions.extend(self._get_security_improvements(validator_result))
                elif validator_name == "semantic":
                    suggestions.extend(self._get_semantic_improvements(validator_result))
        
        # Общие предложения на основе типа агента
        agent_suggestions = self._get_agent_specific_suggestions(step.agent_type, validation_result)
        suggestions.extend(agent_suggestions)
        
        # Удаляем дубликаты
        suggestions = list(set(suggestions))
        
        return suggestions
    
    def _get_structural_improvements(self, validator_result: Dict[str, Any]) -> List[str]:
        """Предложения по улучшению структуры"""
        suggestions = []
        
        details = validator_result.get("details", {})
        issues = details.get("issues", [])
        
        for issue in issues:
            if "missing required field" in issue.lower():
                field_name = issue.split(":")[-1].strip()
                suggestions.append(f"Добавьте обязательное поле: {field_name}")
            elif "empty required field" in issue.lower():
                field_name = issue.split(":")[-1].strip()
                suggestions.append(f"Заполните поле: {field_name}")
            elif "too short" in issue.lower():
                suggestions.append("Расширьте содержимое - текущая длина недостаточна")
            elif "invalid json" in issue.lower():
                suggestions.append("Исправьте формат JSON - проверьте синтаксис")
        
        return suggestions
    
    def _get_completeness_improvements(self, validator_result: Dict[str, Any]) -> List[str]:
        """Предложения по улучшению полноты"""
        suggestions = []
        
        details = validator_result.get("details", {})
        issues = details.get("issues", [])
        
        for issue in issues:
            if "low keyword coverage" in issue.lower():
                suggestions.append("Включите больше ключевых терминов по теме")
            elif "missing sections" in issue.lower():
                sections = issue.split(":")[-1].strip()
                suggestions.append(f"Добавьте недостающие разделы: {sections}")
            elif "too few sentences" in issue.lower():
                suggestions.append("Расширьте объяснение - добавьте больше деталей")
        
        return suggestions
    
    def _get_security_improvements(self, validator_result: Dict[str, Any]) -> List[str]:
        """Предложения по улучшению безопасности"""
        suggestions = []
        
        details = validator_result.get("details", {})
        threats = details.get("threats", [])
        
        for threat in threats:
            if "sql injection" in threat.lower():
                suggestions.append("КРИТИЧНО: Удалите потенциально опасный SQL код")
            elif "xss" in threat.lower():
                suggestions.append("КРИТИЧНО: Удалите потенциально опасный JavaScript код")
            elif "dangerous command" in threat.lower():
                suggestions.append("КРИТИЧНО: Удалите опасные системные команды")
            elif "data leak" in threat.lower():
                suggestions.append("КРИТИЧНО: Удалите чувствительные данные")
        
        return suggestions
    
    def _get_semantic_improvements(self, validator_result: Dict[str, Any]) -> List[str]:
        """Предложения по улучшению семантики"""
        # TODO: Implement when semantic validator is ready
        return ["Проверьте логическую согласованность содержания"]
    
    def _get_agent_specific_suggestions(self, agent_type: str, 
                                       validation_result: Dict[str, Any]) -> List[str]:
        """Предложения специфичные для типа агента"""
        suggestions = []
        
        if agent_type == "sql_generator_agent":
            suggestions.extend([
                "Убедитесь, что SQL запрос оптимизирован",
                "Добавьте комментарии к сложным частям запроса",
                "Проверьте корректность JOIN условий"
            ])
        elif agent_type == "analyst":
            suggestions.extend([
                "Структурируйте анализ по разделам",
                "Подкрепите выводы конкретными данными",
                "Добавьте рекомендации по результатам анализа"
            ])
        elif agent_type == "researcher":
            suggestions.extend([
                "Добавьте больше актуальных источников",
                "Проверьте достоверность информации",
                "Укажите даты и контекст найденной информации"
            ])
        
        return suggestions
    
    async def _classify_error(self, step_result: StepResult, 
                             validation_result: Dict[str, Any]) -> str:
        """Классифицировать тип ошибки"""
        
        # Если шаг вообще не выполнился
        if step_result.status.value == "failed":
            if step_result.error:
                error_text = step_result.error.lower()
                if "timeout" in error_text:
                    return "timeout"
                elif "rate limit" in error_text:
                    return "rate_limit"
                elif "network" in error_text:
                    return "network_error"
                elif any(keyword in error_text for keyword in ["unterminated string", "json", "parsing", "invalid json"]):
                    return "parsing_error"
                elif any(keyword in error_text for keyword in ["file not found", "no such file", "отсутствуют"]):
                    return "file_not_found"
                else:
                    return "execution_error"
            return "unknown_error"
        
        # Если шаг выполнился, но не прошел валидацию
        if not validation_result["validation_passed"]:
            overall_score = validation_result["overall_score"]
            
            if overall_score < 0.3:
                return "low_quality"
            elif overall_score < 0.7:
                return "validation_failed"
            
            # Проверяем специфичные проблемы
            for validator_result in validation_result["validator_results"]:
                if validator_result["validator_name"] == "security" and not validator_result["passed"]:
                    return "security_violation"
                elif validator_result["validator_name"] == "structural" and not validator_result["passed"]:
                    return "format_error"
        
        return "none"
