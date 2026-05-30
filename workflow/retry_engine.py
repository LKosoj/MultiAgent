"""
Движок повторных попыток для Workflow Engine
==========================================

RetryEngine обеспечивает надежность выполнения шагов workflow
через политики повторных попыток, экспоненциальные задержки и
интеллектуальную обработку ошибок.
"""

import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any, Callable, Optional

from .models import RetryPolicy, StepResult, StepStatus, WorkflowStepError

logger = logging.getLogger(__name__)


def _redact_retry_error(error: Any) -> str:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        return str(redact_pii_in_payload(_redact_payload(str(error))))
    except Exception:
        return "<redacted>"


class RetryEngine:
    """Движок повторных попыток с поддержкой различных стратегий"""
    
    def __init__(self):
        self.default_policy = RetryPolicy()
        self.active_retries = {}  # Отслеживание активных retry
        
    async def execute_with_retry(self,
                               step_id: str,
                               step_func: Callable,
                               context: Dict[str, Any],
                               retry_policy: Optional[RetryPolicy] = None,
                               timeout: Optional[int] = None) -> StepResult:
        """
        Выполнение функции с политикой повторных попыток
        
        Args:
            step_id: Идентификатор шага
            step_func: Async функция для выполнения
            context: Контекст выполнения
            retry_policy: Политика повторов (по умолчанию используется default_policy)
            
        Returns:
            StepResult с результатом выполнения
        """
        policy = retry_policy or self.default_policy
        start_time = datetime.now()
        
        # Инициализируем результат
        result = StepResult(
            step_id=step_id,
            status=StepStatus.RUNNING,
            start_time=start_time,
            attempt_number=1
        )
        
        self.active_retries[step_id] = {
            'start_time': start_time,
            'current_attempt': 0,
            'policy': policy
        }
        
        try:
            for attempt in range(policy.max_retries + 1):
                self.active_retries[step_id]['current_attempt'] = attempt + 1
                result.attempt_number = attempt + 1
                
                try:
                    logger.info(f"🔄 {step_id}: Попытка {attempt + 1}/{policy.max_retries + 1}")
                    
                    # Выполняем функцию
                    output = await self._execute_with_timeout(step_func, context, timeout=timeout)
                    
                    # Успешное выполнение
                    result.status = StepStatus.COMPLETED
                    result.output = output
                    result.end_time = datetime.now()
                    result.duration_seconds = (result.end_time - result.start_time).total_seconds()
                    
                    logger.info(f"✅ {step_id}: Выполнен успешно с попытки {attempt + 1}")
                    return result
                    
                except Exception as e:
                    error_type = self._classify_error(e)
                    safe_error = _redact_retry_error(e)
                    logger.warning(
                        "⚠️ %s: Ошибка на попытке %s: %s - %s",
                        step_id,
                        attempt + 1,
                        error_type,
                        safe_error,
                    )
                    
                    result.error = safe_error
                    
                    # Проверяем, нужно ли повторять
                    if attempt < policy.max_retries and self._should_retry(error_type, policy):
                        result.status = StepStatus.RETRYING
                        
                        # Вычисляем задержку
                        delay = self._calculate_delay(attempt, policy)
                        logger.info(f"⏳ {step_id}: Повтор через {delay:.1f} секунд")
                        
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Исчерпаны попытки или ошибка не подлежит retry
                        result.status = StepStatus.FAILED
                        result.end_time = datetime.now()
                        result.duration_seconds = (result.end_time - result.start_time).total_seconds()
                        
                        logger.error(f"❌ {step_id}: Шаг провален после {attempt + 1} попыток")
                        raise WorkflowStepError(
                            f"Step {step_id} failed after {attempt + 1} attempts: {safe_error}"
                        )
                        
        finally:
            # Очищаем отслеживание retry
            self.active_retries.pop(step_id, None)
            
        return result
    
    async def _execute_with_timeout(self, step_func: Callable, context: Dict[str, Any], 
                                  timeout: Optional[int] = None) -> Any:
        """Выполнение функции с таймаутом"""
        timeout = timeout or 300  # 5 минут по умолчанию
        
        try:
            return await asyncio.wait_for(step_func(context), timeout=timeout)
        except asyncio.TimeoutError:
            raise WorkflowStepError(f"Step timed out after {timeout} seconds")
    
    def _classify_error(self, error: Exception) -> str:
        """Классификация типа ошибки для принятия решения о retry"""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()
        
        # Сетевые ошибки
        if any(keyword in error_str for keyword in ['connection', 'network', 'timeout', 'dns']):
            return "network_error"
            
        # Rate limiting
        if any(keyword in error_str for keyword in ['rate limit', 'too many requests', '429']):
            return "rate_limit"
            
        # Временные ошибки API
        if any(keyword in error_str for keyword in ['502', '503', '504', 'service unavailable']):
            return "temporary_failure"
            
        # Ошибки модели
        if any(keyword in error_str for keyword in ['model', 'inference', 'cuda', 'memory']):
            return "model_error"
            
        # Ошибки валидации - не retry
        if any(keyword in error_str for keyword in ['validation', 'invalid', 'bad request', '400']):
            return "validation_error"
            
        # Ошибки авторизации - не retry  
        if any(keyword in error_str for keyword in ['unauthorized', '401', '403', 'forbidden']):
            return "auth_error"
            
        # По умолчанию - неизвестная ошибка
        return "unknown_error"
    
    def _should_retry(self, error_type: str, policy: RetryPolicy) -> bool:
        """Определяет, нужно ли повторять выполнение при данном типе ошибки"""
        return error_type in policy.retry_on_errors
    
    def _calculate_delay(self, attempt: int, policy: RetryPolicy) -> float:
        """Вычисляет задержку перед следующей попыткой"""
        if policy.backoff_strategy == "exponential":
            delay = policy.base_delay * (2 ** attempt)
        elif policy.backoff_strategy == "linear":
            delay = policy.base_delay * (attempt + 1)
        else:  # fixed
            delay = policy.base_delay
            
        # Ограничиваем максимальной задержкой
        return min(delay, policy.max_delay)
    
    def get_retry_statistics(self) -> Dict[str, Any]:
        """Получение статистики по активным retry"""
        stats = {
            'active_retries': len(self.active_retries),
            'retry_details': {}
        }
        
        for step_id, retry_info in self.active_retries.items():
            duration = (datetime.now() - retry_info['start_time']).total_seconds()
            stats['retry_details'][step_id] = {
                'current_attempt': retry_info['current_attempt'],
                'max_retries': retry_info['policy'].max_retries,
                'duration_seconds': duration,
                'policy': retry_info['policy'].__dict__
            }
            
        return stats
    
    def cancel_retry(self, step_id: str) -> bool:
        """Отмена retry для конкретного шага"""
        if step_id in self.active_retries:
            del self.active_retries[step_id]
            logger.info(f"🚫 Retry для шага {step_id} отменен")
            return True
        return False


class CircuitBreaker:
    """Circuit breaker для предотвращения каскадных сбоев"""
    
    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        
    async def call(self, func: Callable, *args, **kwargs):
        """Выполнение функции через circuit breaker"""
        
        if self.state == "OPEN":
            if self._should_attempt_reset():
                self.state = "HALF_OPEN"
                logger.info("🔄 Circuit breaker переведен в HALF_OPEN")
            else:
                raise WorkflowStepError("Circuit breaker is OPEN - too many failures")
        
        try:
            result = await func(*args, **kwargs)
            
            # Успешное выполнение
            if self.state == "HALF_OPEN":
                self._reset()
                logger.info("✅ Circuit breaker RESET - сервис восстановлен")
                
            return result
            
        except Exception as e:
            self._record_failure()
            raise e
    
    def _should_attempt_reset(self) -> bool:
        """Проверка, можно ли попробовать reset circuit breaker"""
        if not self.last_failure_time:
            return True
            
        time_since_failure = (datetime.now() - self.last_failure_time).total_seconds()
        return time_since_failure >= self.reset_timeout
    
    def _record_failure(self):
        """Запись сбоя в circuit breaker"""
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(f"🚨 Circuit breaker ОТКРЫТ - {self.failure_count} сбоев")
    
    def _reset(self):
        """Сброс circuit breaker в нормальное состояние"""
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"
