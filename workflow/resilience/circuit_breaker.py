"""
Circuit Breaker Pattern для защиты от cascade failures
"""
import logging
from typing import Dict, Any, Callable, Optional
from datetime import datetime, timedelta
from enum import Enum
import asyncio

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Состояния Circuit Breaker"""
    CLOSED = "closed"      # Нормальная работа
    OPEN = "open"          # Блокировка вызовов
    HALF_OPEN = "half_open"  # Тестирование восстановления


class CircuitBreakerOpenError(Exception):
    """Ошибка при попытке вызова когда circuit breaker открыт"""
    pass


class AgentCircuitBreaker:
    """Circuit Breaker для отдельного агента"""
    
    def __init__(self, agent_name: str, failure_threshold: int = 5, 
                 recovery_timeout: int = 60, success_threshold: int = 3):
        self.agent_name = agent_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        
        # Статистика
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.state_changes = []
        
    async def call(self, agent_func: Callable, *args, **kwargs) -> Any:
        """Выполнить вызов агента через circuit breaker"""
        
        self.total_calls += 1
        
        # Проверяем состояние circuit breaker
        await self._update_state()
        
        if self.state == CircuitState.OPEN:
            logger.warning(f"🚫 Circuit breaker OPEN for agent '{self.agent_name}' - blocking call")
            raise CircuitBreakerOpenError(f"Circuit breaker is OPEN for agent {self.agent_name}")
        
        try:
            # Выполняем вызов
            result = await agent_func(*args, **kwargs)
            
            # Фиксируем успех
            await self._record_success()
            
            return result
            
        except Exception as e:
            # Фиксируем неудачу
            await self._record_failure(e)
            raise
    
    async def _update_state(self):
        """Обновить состояние circuit breaker"""
        
        current_time = datetime.now()
        
        if self.state == CircuitState.OPEN:
            # Проверяем можно ли перейти в HALF_OPEN
            if (self.last_failure_time and 
                current_time - self.last_failure_time >= timedelta(seconds=self.recovery_timeout)):
                await self._change_state(CircuitState.HALF_OPEN)
                logger.info(f"🔄 Circuit breaker for '{self.agent_name}' changed to HALF_OPEN")
        
        elif self.state == CircuitState.HALF_OPEN:
            # В HALF_OPEN состоянии проверяем успехи
            if self.success_count >= self.success_threshold:
                await self._change_state(CircuitState.CLOSED)
                logger.info(f"✅ Circuit breaker for '{self.agent_name}' RECOVERED (CLOSED)")
    
    async def _record_success(self):
        """Зафиксировать успешный вызов"""
        self.total_successes += 1
        self.last_success_time = datetime.now()
        
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
        elif self.state == CircuitState.CLOSED:
            # Сбрасываем счетчик ошибок при успехе
            self.failure_count = 0
    
    async def _record_failure(self, error: Exception):
        """Зафиксировать неудачный вызов"""
        self.total_failures += 1
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        logger.warning(f"⚠️ Agent '{self.agent_name}' failure #{self.failure_count}: {error}")
        
        # Проверяем нужно ли открыть circuit breaker
        if (self.state == CircuitState.CLOSED and 
            self.failure_count >= self.failure_threshold):
            await self._change_state(CircuitState.OPEN)
            logger.error(f"🚨 Circuit breaker for '{self.agent_name}' OPENED due to {self.failure_count} failures")
        
        elif self.state == CircuitState.HALF_OPEN:
            # В HALF_OPEN любая ошибка возвращает в OPEN
            await self._change_state(CircuitState.OPEN)
            logger.error(f"🚨 Circuit breaker for '{self.agent_name}' returned to OPEN from HALF_OPEN")
    
    async def _change_state(self, new_state: CircuitState):
        """Изменить состояние circuit breaker"""
        old_state = self.state
        self.state = new_state
        
        # Сбрасываем счетчики при смене состояния
        if new_state == CircuitState.CLOSED:
            self.failure_count = 0
            self.success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self.success_count = 0
        
        # Записываем изменение состояния
        self.state_changes.append({
            "timestamp": datetime.now().isoformat(),
            "from_state": old_state.value,
            "to_state": new_state.value,
            "failure_count": self.failure_count,
            "total_calls": self.total_calls
        })
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику circuit breaker"""
        uptime = 1.0
        if self.total_calls > 0:
            uptime = (self.total_calls - self.total_failures) / self.total_calls
        
        return {
            "agent_name": self.agent_name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "uptime_percentage": uptime * 100,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
            "state_changes_count": len(self.state_changes),
            "recovery_timeout": self.recovery_timeout,
            "failure_threshold": self.failure_threshold
        }
    
    def is_available(self) -> bool:
        """Проверить доступен ли агент для вызова"""
        return self.state != CircuitState.OPEN
    
    async def manual_reset(self):
        """Ручной сброс circuit breaker в CLOSED состояние"""
        logger.info(f"🔧 Manual reset of circuit breaker for '{self.agent_name}'")
        await self._change_state(CircuitState.CLOSED)


class CircuitBreakerManager:
    """Менеджер circuit breaker'ов для всех агентов"""
    
    def __init__(self):
        self.breakers: Dict[str, AgentCircuitBreaker] = {}
        self.global_config = {
            "failure_threshold": 5,
            "recovery_timeout": 60,
            "success_threshold": 3,
            "enabled": True
        }
        
    def get_or_create_breaker(self, agent_name: str) -> AgentCircuitBreaker:
        """Получить или создать circuit breaker для агента"""
        if agent_name not in self.breakers:
            self.breakers[agent_name] = AgentCircuitBreaker(
                agent_name=agent_name,
                failure_threshold=self.global_config["failure_threshold"],
                recovery_timeout=self.global_config["recovery_timeout"],
                success_threshold=self.global_config["success_threshold"]
            )
            logger.info(f"🔧 Created circuit breaker for agent '{agent_name}'")
        
        return self.breakers[agent_name]
    
    async def call_agent_safely(self, agent_name: str, agent_func: Callable, 
                               *args, **kwargs) -> Any:
        """Безопасный вызов агента через circuit breaker"""
        
        if not self.global_config["enabled"]:
            # Circuit breaker отключен - прямой вызов
            return await agent_func(*args, **kwargs)
        
        breaker = self.get_or_create_breaker(agent_name)
        return await breaker.call(agent_func, *args, **kwargs)
    
    def is_agent_available(self, agent_name: str) -> bool:
        """Проверить доступен ли агент"""
        if agent_name not in self.breakers:
            return True  # Агент еще не использовался
        
        return self.breakers[agent_name].is_available()
    
    def get_all_stats(self) -> Dict[str, Any]:
        """Получить статистику всех circuit breaker'ов"""
        
        stats = {
            "global_config": self.global_config,
            "total_agents": len(self.breakers),
            "agents": {}
        }
        
        # Подсчитываем общую статистику
        total_calls = 0
        total_failures = 0
        open_breakers = 0
        
        for agent_name, breaker in self.breakers.items():
            agent_stats = breaker.get_stats()
            stats["agents"][agent_name] = agent_stats
            
            total_calls += agent_stats["total_calls"]
            total_failures += agent_stats["total_failures"]
            
            if agent_stats["state"] == "open":
                open_breakers += 1
        
        stats["summary"] = {
            "total_calls": total_calls,
            "total_failures": total_failures,
            "overall_uptime": ((total_calls - total_failures) / total_calls * 100) if total_calls > 0 else 100,
            "open_breakers": open_breakers,
            "healthy_agents": len(self.breakers) - open_breakers
        }
        
        return stats
    
    def get_unhealthy_agents(self) -> list[str]:
        """Получить список нездоровых агентов"""
        unhealthy = []
        
        for agent_name, breaker in self.breakers.items():
            if breaker.state == CircuitState.OPEN:
                unhealthy.append(agent_name)
        
        return unhealthy
    
    async def reset_agent_breaker(self, agent_name: str):
        """Сбросить circuit breaker для конкретного агента"""
        if agent_name in self.breakers:
            await self.breakers[agent_name].manual_reset()
            logger.info(f"🔧 Reset circuit breaker for agent '{agent_name}'")
        else:
            logger.warning(f"⚠️ No circuit breaker found for agent '{agent_name}'")
    
    async def reset_all_breakers(self):
        """Сбросить все circuit breaker'ы"""
        for agent_name, breaker in self.breakers.items():
            await breaker.manual_reset()
        
        logger.info(f"🔧 Reset all {len(self.breakers)} circuit breakers")
    
    def configure(self, config: Dict[str, Any]):
        """Обновить глобальную конфигурацию"""
        self.global_config.update(config)
        logger.info(f"🔧 Updated circuit breaker configuration: {config}")
    
    def disable_circuit_breakers(self):
        """Отключить все circuit breaker'ы"""
        self.global_config["enabled"] = False
        logger.info("🔧 Circuit breakers globally disabled")
    
    def enable_circuit_breakers(self):
        """Включить все circuit breaker'ы"""
        self.global_config["enabled"] = True
        logger.info("🔧 Circuit breakers globally enabled")
