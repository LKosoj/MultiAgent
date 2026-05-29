"""
Adaptive Retry Engine с ML-powered стратегиями восстановления
"""
import logging
import asyncio
from typing import Dict, Any, List, Callable, Optional, Union
from datetime import datetime, timedelta
from enum import Enum
import random

from ..models import StepResult, StepStatus, ErrorClass
from ..policy.models import RetryStrategyType

logger = logging.getLogger(__name__)


def _redact_retry_error(error: Any) -> str:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        return str(redact_pii_in_payload(_redact_payload(str(error))))
    except Exception:
        return "<redacted>"


class RetryOutcome(Enum):
    """Результаты retry попыток"""
    SUCCESS = "success"
    FAILED_PERMANENTLY = "failed_permanently"
    RETRY_LIMIT_EXCEEDED = "retry_limit_exceeded"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"


class RetryStrategy:
    """Стратегия повторных попыток"""
    
    def __init__(self, strategy_type: RetryStrategyType, config: Dict[str, Any] = None):
        self.strategy_type = strategy_type
        self.config = config or {}
        
    async def apply(self, original_task: str, error_context: Dict[str, Any]) -> str:
        """Применить стратегию к задаче"""
        
        if self.strategy_type == RetryStrategyType.REFINE_PROMPT:
            return await self._refine_prompt(original_task, error_context)
        elif self.strategy_type == RetryStrategyType.ADD_CONTEXT:
            return await self._add_context(original_task, error_context)
        elif self.strategy_type == RetryStrategyType.REDUCE_COMPLEXITY:
            return await self._reduce_complexity(original_task, error_context)
        else:
            return original_task
    
    async def _refine_prompt(self, task: str, error_context: Dict[str, Any]) -> str:
        """Уточнить промпт на основе ошибки"""
        
        refinements = []
        
        error_class = error_context.get("error_class", "")
        validation_issues = error_context.get("validation_issues", [])
        
        if error_class == "format_error":
            refinements.append("ВАЖНО: Следуйте точно указанному формату ответа.")
            refinements.append("Проверьте структуру вашего ответа перед отправкой.")
        
        elif error_class == "low_quality":
            refinements.append("ТРЕБОВАНИЕ: Предоставьте более детальный и обоснованный ответ.")
            refinements.append("Включите конкретные примеры и данные.")
        
        elif error_class == "validation_failed":
            if validation_issues:
                refinements.append(f"ИСПРАВЬТЕ: {'; '.join(validation_issues)}")
        
        # Общие улучшения
        refinements.extend([
            "Убедитесь в полноте и точности ответа.",
            "Проверьте соответствие всем требованиям задачи."
        ])
        
        if refinements:
            refined_task = task + "\n\n" + "\n".join(f"• {r}" for r in refinements)
            return refined_task
        
        return task
    
    async def _add_context(self, task: str, error_context: Dict[str, Any]) -> str:
        """Добавить контекст к задаче"""
        
        context_additions = []
        
        # Добавляем информацию о предыдущих попытках
        attempt_number = error_context.get("attempt_number", 1)
        if attempt_number > 1:
            context_additions.append(f"Это попытка #{attempt_number}. Предыдущие попытки не увенчались успехом.")
        
        # Добавляем информацию о качественных требованиях
        min_quality = error_context.get("min_quality_score", 0.7)
        context_additions.append(f"Требуемый минимальный уровень качества: {min_quality:.0%}")
        
        # Добавляем примеры хорошего результата
        if error_context.get("error_class") == "low_quality":
            context_additions.extend([
                "Пример качественного ответа должен включать:",
                "- Четкую структуру и логику",
                "- Конкретные данные и примеры",
                "- Обоснованные выводы",
                "- Практические рекомендации"
            ])
        
        if context_additions:
            enhanced_task = task + "\n\nДополнительный контекст:\n" + "\n".join(context_additions)
            return enhanced_task
        
        return task
    
    async def _reduce_complexity(self, task: str, error_context: Dict[str, Any]) -> str:
        """Упростить задачу"""
        
        simplifications = [
            "Сосредоточьтесь на основных аспектах задачи.",
            "Если задача сложная, разбейте ее на более простые части.",
            "Предоставьте базовое решение, которое можно развить позже."
        ]
        
        simplified_task = task + "\n\nУпрощенный подход:\n" + "\n".join(simplifications)
        return simplified_task


class AdaptiveRetryEngine:
    """Adaptive Retry Engine с умным выбором стратегий"""
    
    def __init__(self):
        self.retry_history: Dict[str, List[Dict[str, Any]]] = {}
        self.strategy_effectiveness: Dict[str, Dict[str, float]] = {}
        
        # Базовые стратегии для разных типов ошибок
        self.error_strategies = {
            ErrorClass.TIMEOUT: [
                RetryStrategyType.REDUCE_COMPLEXITY,
                RetryStrategyType.REFINE_PROMPT
            ],
            ErrorClass.EMPTY_RESPONSE: [
                RetryStrategyType.REFINE_PROMPT,
                RetryStrategyType.ADD_CONTEXT
            ],
            ErrorClass.LOW_QUALITY: [
                RetryStrategyType.ADD_CONTEXT,
                RetryStrategyType.REFINE_PROMPT
            ],
            ErrorClass.VALIDATION_FAILED: [
                RetryStrategyType.REFINE_PROMPT,
                RetryStrategyType.ADD_CONTEXT
            ],
            ErrorClass.SECURITY_VIOLATION: [
                # Для security violations retry обычно не помогает
            ],
            ErrorClass.UNKNOWN: [
                RetryStrategyType.REFINE_PROMPT
            ]
        }
    
    async def execute_with_retry(self, step_id: str, step_func: Callable,
                                context: Dict[str, Any],
                                max_retries: int = 3,
                                base_delay: float = 1.0,
                                max_delay: float = 60.0,
                                backoff_multiplier: float = 2.0) -> StepResult:
        """Выполнить функцию с адаптивными повторами"""
        
        attempt = 0
        last_error = None
        retry_context = {
            "step_id": step_id,
            "start_time": datetime.now(),
            "attempts": []
        }
        
        while attempt <= max_retries:
            attempt += 1
            attempt_start = datetime.now()
            
            try:
                logger.info(f"🔄 Retry attempt {attempt}/{max_retries + 1} for step '{step_id}'")
                
                # Выполняем функцию
                result = await step_func(context)
                
                # Проверяем результат
                if self._is_result_acceptable(result):
                    # Успех!
                    await self._record_success(step_id, attempt, retry_context)
                    logger.info(f"✅ Step '{step_id}' succeeded on attempt {attempt}")
                    return result
                else:
                    # Результат неприемлемый, но не критичная ошибка
                    logger.warning(f"⚠️ Step '{step_id}' returned poor quality result on attempt {attempt}")
                    last_error = "Poor quality result"
                    
            except Exception as e:
                safe_error = _redact_retry_error(e)
                last_error = safe_error
                logger.warning("❌ Step '%s' failed on attempt %s: %s", step_id, attempt, safe_error)
                
                # Записываем попытку
                attempt_info = {
                    "attempt_number": attempt,
                    "error": safe_error,
                    "error_class": self._classify_error(e),
                    "duration": (datetime.now() - attempt_start).total_seconds(),
                    "timestamp": attempt_start.isoformat()
                }
                retry_context["attempts"].append(attempt_info)
            
            # Если это не последняя попытка, применяем стратегию
            if attempt <= max_retries:
                await self._apply_retry_strategy(step_id, attempt, retry_context, context)
                
                # Вычисляем задержку
                delay = min(base_delay * (backoff_multiplier ** (attempt - 1)), max_delay)
                # Добавляем jitter
                jitter = random.uniform(0.1, 0.3) * delay
                total_delay = delay + jitter
                
                logger.info(f"⏱️ Waiting {total_delay:.1f}s before retry #{attempt + 1}")
                await asyncio.sleep(total_delay)
        
        # Все попытки исчерпаны
        await self._record_failure(step_id, max_retries + 1, retry_context)
        
        return StepResult(
            step_id=step_id,
            status=StepStatus.FAILED,
            error=f"Failed after {max_retries + 1} attempts. Last error: {last_error}",
            start_time=retry_context["start_time"],
            end_time=datetime.now(),
            retry_count=max_retries + 1,
            metadata={"retry_context": retry_context}
        )
    
    def _is_result_acceptable(self, result: Any) -> bool:
        """Проверить приемлемость результата"""
        
        if isinstance(result, StepResult):
            # Если это StepResult, проверяем статус и качество
            if result.status == StepStatus.FAILED:
                return False
            
            if hasattr(result, 'quality_score') and result.quality_score < 0.3:
                return False
            
            return True
        
        # Для других типов результата - базовые проверки
        if result is None:
            return False
        
        if isinstance(result, str) and len(result.strip()) < 10:
            return False
        
        return True
    
    def _classify_error(self, error: Exception) -> str:
        """Классифицировать ошибку для выбора стратегии"""
        
        error_str = str(error).lower()
        
        if "timeout" in error_str:
            return ErrorClass.TIMEOUT.value
        elif "empty" in error_str or "no response" in error_str:
            return ErrorClass.EMPTY_RESPONSE.value
        elif "quality" in error_str or "validation" in error_str:
            return ErrorClass.LOW_QUALITY.value
        elif "security" in error_str or "violation" in error_str:
            return ErrorClass.SECURITY_VIOLATION.value
        elif "format" in error_str or "structure" in error_str:
            return ErrorClass.VALIDATION_FAILED.value
        else:
            return ErrorClass.UNKNOWN.value
    
    async def _apply_retry_strategy(self, step_id: str, attempt: int,
                                   retry_context: Dict[str, Any],
                                   context: Dict[str, Any]):
        """Применить стратегию retry"""
        
        last_attempt = retry_context["attempts"][-1] if retry_context["attempts"] else {}
        error_class = last_attempt.get("error_class", ErrorClass.UNKNOWN.value)
        
        # Выбираем стратегию
        strategy_type = self._select_strategy(error_class, attempt, step_id)
        
        if strategy_type:
            strategy = RetryStrategy(strategy_type)
            
            # Подготавливаем контекст ошибки
            error_context = {
                "step_id": step_id,
                "attempt_number": attempt,
                "error_class": error_class,
                "validation_issues": last_attempt.get("validation_issues", []),
                "min_quality_score": context.get("min_quality_score", 0.7)
            }
            
            # Применяем стратегию к задаче
            if "task" in context:
                original_task = context["task"]
                modified_task = await strategy.apply(original_task, error_context)
                context["task"] = modified_task
                
                logger.info(f"🎯 Applied {strategy_type.value} strategy for retry #{attempt + 1}")
    
    def _select_strategy(self, error_class: str, attempt: int, step_id: str) -> Optional[RetryStrategyType]:
        """Выбрать наилучшую стратегию для ошибки"""
        
        try:
            error_enum = ErrorClass(error_class)
        except ValueError:
            error_enum = ErrorClass.UNKNOWN
        
        available_strategies = self.error_strategies.get(error_enum, [])
        
        if not available_strategies:
            return None
        
        # Выбираем стратегию на основе номера попытки и эффективности
        if attempt <= len(available_strategies):
            selected_strategy = available_strategies[attempt - 1]
        else:
            # Если попыток больше чем стратегий, используем последнюю
            selected_strategy = available_strategies[-1]
        
        return selected_strategy
    
    async def _record_success(self, step_id: str, attempts: int, retry_context: Dict[str, Any]):
        """Записать успешное выполнение"""
        
        if step_id not in self.retry_history:
            self.retry_history[step_id] = []
        
        success_record = {
            "timestamp": datetime.now().isoformat(),
            "outcome": RetryOutcome.SUCCESS.value,
            "attempts_used": attempts,
            "total_duration": (datetime.now() - retry_context["start_time"]).total_seconds(),
            "retry_context": retry_context
        }
        
        self.retry_history[step_id].append(success_record)
        
        # Обновляем статистику эффективности стратегий
        await self._update_strategy_effectiveness(retry_context, True)
    
    async def _record_failure(self, step_id: str, attempts: int, retry_context: Dict[str, Any]):
        """Записать неуспешное выполнение"""
        
        if step_id not in self.retry_history:
            self.retry_history[step_id] = []
        
        failure_record = {
            "timestamp": datetime.now().isoformat(),
            "outcome": RetryOutcome.RETRY_LIMIT_EXCEEDED.value,
            "attempts_used": attempts,
            "total_duration": (datetime.now() - retry_context["start_time"]).total_seconds(),
            "retry_context": retry_context
        }
        
        self.retry_history[step_id].append(failure_record)
        
        # Обновляем статистику эффективности стратегий
        await self._update_strategy_effectiveness(retry_context, False)
    
    async def _update_strategy_effectiveness(self, retry_context: Dict[str, Any], success: bool):
        """Обновить статистику эффективности стратегий"""
        
        # TODO: Implement ML-based strategy effectiveness tracking
        # Пока простая статистика
        pass
    
    def get_retry_statistics(self) -> Dict[str, Any]:
        """Получить статистику retry операций"""
        
        if not self.retry_history:
            return {"message": "No retry history available"}
        
        total_operations = sum(len(history) for history in self.retry_history.values())
        successful_operations = 0
        total_attempts = 0
        
        for step_history in self.retry_history.values():
            for operation in step_history:
                total_attempts += operation["attempts_used"]
                if operation["outcome"] == RetryOutcome.SUCCESS.value:
                    successful_operations += 1
        
        avg_attempts = total_attempts / total_operations if total_operations > 0 else 0
        success_rate = successful_operations / total_operations if total_operations > 0 else 0
        
        return {
            "total_operations": total_operations,
            "successful_operations": successful_operations,
            "success_rate": success_rate,
            "average_attempts_per_operation": avg_attempts,
            "steps_with_retries": len(self.retry_history),
            "strategy_effectiveness": self.strategy_effectiveness
        }
    
    def get_step_retry_history(self, step_id: str) -> List[Dict[str, Any]]:
        """Получить историю retry для конкретного шага"""
        return self.retry_history.get(step_id, [])
    
    def clear_history(self, older_than_days: int = 7):
        """Очистить старую историю retry"""
        cutoff_date = datetime.now() - timedelta(days=older_than_days)
        
        for step_id in list(self.retry_history.keys()):
            filtered_history = []
            for record in self.retry_history[step_id]:
                record_date = datetime.fromisoformat(record["timestamp"])
                if record_date >= cutoff_date:
                    filtered_history.append(record)
            
            if filtered_history:
                self.retry_history[step_id] = filtered_history
            else:
                del self.retry_history[step_id]
        
        logger.info(f"🧹 Cleared retry history older than {older_than_days} days")
