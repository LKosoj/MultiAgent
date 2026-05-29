"""
Pre-Step Planner для анализа контекста и планирования выполнения шагов
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models import StepPlan, WorkflowStep, WorkflowContext, StepResult
from ..policy.engine import PolicyEngine
from ..contracts.registry import ContractRegistry

logger = logging.getLogger(__name__)


class PreStepPlanner:
    """Планировщик шагов workflow"""
    
    def __init__(self):
        self.policy_engine = PolicyEngine()
        self.contract_registry = ContractRegistry()
        
    async def plan_step(self, step: WorkflowStep, context: WorkflowContext,
                       previous_results: Dict[str, StepResult] = None) -> StepPlan:
        """Создать план выполнения шага"""
        
        try:
            logger.info(f"🧠 Planning step '{step.id}' with agent '{step.agent_type}'")
            
            # Анализируем контекст и предыдущие результаты
            context_analysis = await self._analyze_context(step, context, previous_results)
            
            # Уточняем задачу на основе контекста
            refined_task = await self._refine_task(step, context_analysis)
            
            # Определяем ожидаемый формат результата
            output_format = await self._determine_output_format(step, context)
            
            # Получаем критерии качества
            quality_criteria = await self._get_quality_criteria(step)
            
            # Определяем бюджет ресурсов
            resource_budget = await self._calculate_resource_budget(step, context_analysis)
            
            # Планируем fallback стратегии
            fallback_strategies = await self._plan_fallback_strategies(step, context_analysis)
            
            plan = StepPlan(
                step_id=step.id,
                refined_task=refined_task,
                expected_output_format=output_format,
                quality_criteria=quality_criteria,
                resource_budget=resource_budget,
                timeout_seconds=resource_budget.get("timeout_seconds", 300),
                retry_budget=resource_budget.get("retry_budget", 3),
                context_hints=context_analysis.get("hints", []),
                fallback_strategies=fallback_strategies
            )
            
            logger.info(f"✅ Created plan for step '{step.id}': {len(plan.context_hints)} hints, "
                       f"{len(plan.fallback_strategies)} fallback strategies")
            
            return plan
            
        except Exception as e:
            logger.error(f"❌ Failed to plan step '{step.id}': {e}")
            # Возвращаем минимальный план
            return StepPlan(
                step_id=step.id,
                refined_task=step.task,
                expected_output_format="text",
                quality_criteria={"min_score": 0.5},
                resource_budget={"timeout_seconds": 300, "retry_budget": 3},
                timeout_seconds=300,
                retry_budget=3
            )
    
    async def _analyze_context(self, step: WorkflowStep, context: WorkflowContext,
                              previous_results: Dict[str, StepResult] = None) -> Dict[str, Any]:
        """Анализ контекста выполнения"""
        
        analysis = {
            "step_position": "initial",
            "available_data": [],
            "complexity_factors": [],
            "hints": [],
            "dependencies_met": True
        }
        
        # Анализируем позицию шага
        if previous_results:
            analysis["step_position"] = "intermediate" if len(previous_results) > 0 else "initial"
            
            # Собираем доступные данные из предыдущих шагов
            for step_id, result in previous_results.items():
                if result.status.value == "completed" and result.output:
                    analysis["available_data"].append({
                        "step_id": step_id,
                        "output_type": type(result.output).__name__,
                        "quality_score": result.quality_score,
                        "size": len(str(result.output))
                    })
        
        # Проверяем зависимости
        for dep_step in step.depends_on:
            if not previous_results or dep_step not in previous_results:
                analysis["dependencies_met"] = False
                analysis["complexity_factors"].append(f"Missing dependency: {dep_step}")
            elif previous_results[dep_step].status.value != "completed":
                analysis["dependencies_met"] = False
                analysis["complexity_factors"].append(f"Failed dependency: {dep_step}")
        
        # Анализируем сложность задачи
        task_complexity = await self._assess_task_complexity(step.task)
        analysis["complexity_factors"].extend(task_complexity)
        
        # Генерируем подсказки для выполнения
        hints = await self._generate_context_hints(step, context, previous_results)
        analysis["hints"] = hints
        
        return analysis
    
    async def _assess_task_complexity(self, task: str) -> List[str]:
        """Оценить сложность задачи"""
        complexity_factors = []
        
        task_lower = task.lower()
        
        # Проверяем ключевые слова, указывающие на сложность
        if any(word in task_lower for word in ["analyze", "compare", "evaluate"]):
            complexity_factors.append("analytical_task")
            
        if any(word in task_lower for word in ["create", "generate", "build"]):
            complexity_factors.append("creative_task")
            
        if any(word in task_lower for word in ["sql", "query", "database"]):
            complexity_factors.append("technical_task")
            
        if len(task) > 200:
            complexity_factors.append("detailed_instructions")
            
        if "{" in task and "}" in task:
            complexity_factors.append("template_variables")
        
        return complexity_factors
    
    async def _refine_task(self, step: WorkflowStep, context_analysis: Dict[str, Any]) -> str:
        """Уточнить задачу на основе анализа контекста"""
        
        refined_task = step.task
        
        # Добавляем контекстную информацию
        if context_analysis["available_data"]:
            data_summary = []
            for data in context_analysis["available_data"]:
                data_summary.append(f"- Результат от {data['step_id']} (качество: {data['quality_score']:.2f})")
            
            refined_task += f"\n\nДоступные данные из предыдущих шагов:\n" + "\n".join(data_summary)
        
        # Добавляем подсказки по сложности
        if "analytical_task" in context_analysis["complexity_factors"]:
            refined_task += "\n\nОбратите внимание: эта задача требует аналитического подхода и обоснования выводов."
            
        if "technical_task" in context_analysis["complexity_factors"]:
            refined_task += "\n\nВажно: соблюдайте технические требования и проверьте корректность решения."
        
        # Добавляем контекстные подсказки
        if context_analysis["hints"]:
            refined_task += f"\n\nДополнительные указания:\n" + "\n".join(f"- {hint}" for hint in context_analysis["hints"])
        
        return refined_task
    
    async def _generate_context_hints(self, step: WorkflowStep, context: WorkflowContext,
                                     previous_results: Dict[str, StepResult] = None) -> List[str]:
        """Генерировать подсказки на основе контекста"""
        hints = []
        
        # Подсказки на основе типа агента
        if step.agent_type == "sql_generator_agent":
            hints.append("Убедитесь, что SQL запрос оптимизирован и безопасен")
            hints.append("Включите объяснение логики запроса")
            
        elif step.agent_type == "analyst":
            hints.append("Структурируйте анализ с четкими выводами")
            hints.append("Подкрепите выводы данными и примерами")
            
        elif step.agent_type == "researcher":
            hints.append("Используйте актуальные и надежные источники")
            hints.append("Проверьте фактическую точность информации")
        
        # Подсказки на основе предыдущих результатов
        if previous_results:
            low_quality_steps = [
                step_id for step_id, result in previous_results.items()
                if result.quality_score < 0.7
            ]
            
            if low_quality_steps:
                hints.append(f"Учтите потенциальные проблемы с качеством из шагов: {', '.join(low_quality_steps)}")
        
        # Подсказки на основе переменных контекста
        if context.variables:
            if "urgency" in context.variables:
                hints.append("Задача помечена как срочная - сосредоточьтесь на ключевых аспектах")
            if "accuracy_required" in context.variables:
                hints.append("Требуется высокая точность - тщательно проверьте результат")
        
        return hints
    
    async def _determine_output_format(self, step: WorkflowStep, context: WorkflowContext) -> str:
        """Определить ожидаемый формат результата"""
        
        # Получаем контракт для типа агента
        contract = self.contract_registry.get_contract_for_step(step.id, step.agent_type)
        
        if contract:
            if "object" in str(contract.schema.get("type", "")):
                return "structured_json"
            elif step.agent_type == "sql_generator_agent":
                return "sql_with_explanation"
            elif step.agent_type == "analyst":
                return "structured_analysis"
            elif step.agent_type == "researcher":
                return "research_report"
        
        return "text"
    
    async def _get_quality_criteria(self, step: WorkflowStep) -> Dict[str, Any]:
        """Получить критерии качества для шага"""
        
        quality_gate = self.policy_engine.get_quality_gate(step, None)
        
        return {
            "min_score": quality_gate.min_quality_score,
            "soft_threshold": quality_gate.soft_fail_threshold,
            "hard_threshold": quality_gate.hard_fail_threshold,
            "required_validators": quality_gate.required_validators,
            "custom_rules": quality_gate.custom_rules
        }
    
    async def _calculate_resource_budget(self, step: WorkflowStep, 
                                        context_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Вычислить бюджет ресурсов для шага"""
        
        base_budget = self.policy_engine.get_budget("per_step", step)
        
        # Корректируем бюджет на основе сложности
        complexity_multiplier = 1.0
        
        if "analytical_task" in context_analysis["complexity_factors"]:
            complexity_multiplier *= 1.5
        if "creative_task" in context_analysis["complexity_factors"]:
            complexity_multiplier *= 1.3
        if "detailed_instructions" in context_analysis["complexity_factors"]:
            complexity_multiplier *= 1.2
        
        return {
            "max_tokens": int((base_budget.max_tokens or 32768) * complexity_multiplier),
            "timeout_seconds": int((base_budget.max_duration_seconds or 300) * complexity_multiplier),
            "max_cost_usd": (base_budget.max_cost_usd or 5.0) * complexity_multiplier,
            "retry_budget": base_budget.max_retries or 3,
            "complexity_multiplier": complexity_multiplier
        }
    
    async def _plan_fallback_strategies(self, step: WorkflowStep,
                                       context_analysis: Dict[str, Any]) -> List[str]:
        """Планировать fallback стратегии"""
        
        strategies = []
        
        # Базовые стратегии на основе типа задачи
        if "analytical_task" in context_analysis["complexity_factors"]:
            strategies.extend([
                "simplify_analysis_scope",
                "request_additional_context",
                "use_alternative_methodology"
            ])
            
        if "technical_task" in context_analysis["complexity_factors"]:
            strategies.extend([
                "verify_technical_requirements",
                "consult_documentation",
                "use_simpler_approach"
            ])
            
        if "creative_task" in context_analysis["complexity_factors"]:
            strategies.extend([
                "provide_multiple_options",
                "use_structured_framework",
                "iterate_on_feedback"
            ])
        
        # Стратегии на основе зависимостей
        if not context_analysis["dependencies_met"]:
            strategies.extend([
                "proceed_with_partial_data",
                "request_dependency_clarification",
                "use_alternative_data_source"
            ])
        
        return strategies
