"""
Управление ресурсами и квотами для Workflow Engine
===============================================

ResourceManager обеспечивает изоляцию ресурсов между workflow,
предотвращение "шумного соседа" и fair sharing ресурсов.
"""

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import asdict

from .models import ResourceLimits, ResourceLease, ResourceQuotaExceededError

logger = logging.getLogger(__name__)


class ResourcePool:
    """Пул ресурсов с ограничениями и квотированием"""
    
    def __init__(self, max_concurrent: int = 10, max_memory_mb: int = 8192, 
                 max_api_calls_per_minute: int = 1000):
        self.max_concurrent = max_concurrent
        self.max_memory_mb = max_memory_mb
        self.max_api_calls_per_minute = max_api_calls_per_minute
        
        # Текущее использование
        self.current_concurrent = 0
        self.current_memory_mb = 0
        self.api_calls_window = []  # Список временных меток API вызовов
        
        # Активные lease'ы
        self.active_leases: Dict[str, ResourceLease] = {}
        self.lock = asyncio.Lock()
        # Отдельный threading.Lock для record_api_call, который вызывается из thread pool
        self._api_calls_lock = threading.Lock()
        
    async def acquire_resources(self, workflow_id: str, 
                              requirements: ResourceLimits) -> Optional[ResourceLease]:
        """Получение ресурсов для workflow"""
        now = datetime.now()
        # Чистим скользящее окно (60с) и снимаем счётчик API ПОД _api_calls_lock ДО
        # входа в asyncio.Lock: блокирующий threading.Lock НЕ держим внутри
        # event-loop-критической секции (self.lock), иначе пока thread-pool-поток в
        # record_api_call держит _api_calls_lock, event loop простаивает. Корректно и
        # эквивалентно: acquire_resources НЕ мутирует api_calls_window (только
        # record_api_call добавляет записи, под своим _api_calls_lock), а concurrent/
        # memory-бюджет резервируется ниже под self.lock. Проверка api-лимита носит
        # advisory-характер на момент выдачи — небольшая гонка на пару append между этим
        # снимком и резервированием допустима. self.lock сериализует конкурентные
        # acquire_resources по concurrent/memory (api-бюджет acquire не тратит).
        with self._api_calls_lock:
            self.api_calls_window = [
                call_time for call_time in self.api_calls_window
                if (now - call_time).total_seconds() < 60
            ]
            current_api_calls = len(self.api_calls_window)
        async with self.lock:
            # Проверяем доступность ресурсов
            required_concurrent = 1  # Один workflow = одно исполнение
            required_memory = requirements.max_memory_mb or 512  # По умолчанию 512MB
            required_api_calls = requirements.max_api_calls_per_minute or 10

            if (self.current_concurrent + required_concurrent > self.max_concurrent or
                self.current_memory_mb + required_memory > self.max_memory_mb or
                current_api_calls + required_api_calls > self.max_api_calls_per_minute):
                
                logger.warning(f"⚠️ Недостаточно ресурсов для workflow {workflow_id}")
                return None
            
            # Создаем lease
            lease = ResourceLease(
                lease_id=f"{workflow_id}_{now.timestamp()}",
                workflow_id=workflow_id,
                allocated_memory_mb=required_memory,
                allocated_api_calls=required_api_calls,
                start_time=now,
                expires_at=now + timedelta(hours=1)  # Lease на 1 час
            )
            
            # Резервируем ресурсы
            self.current_concurrent += required_concurrent
            self.current_memory_mb += required_memory
            self.active_leases[lease.lease_id] = lease
            
            logger.info(f"✅ Выделены ресурсы для {workflow_id}: {required_memory}MB, {required_api_calls} API calls")
            return lease
    
    async def release_resources(self, lease: ResourceLease):
        """Освобождение ресурсов"""
        async with self.lock:
            if lease.lease_id in self.active_leases:
                self.current_concurrent = max(0, self.current_concurrent - 1)
                self.current_memory_mb = max(0, self.current_memory_mb - lease.allocated_memory_mb)
                
                del self.active_leases[lease.lease_id]
                lease.active = False
                
                logger.info(f"🔓 Освобождены ресурсы для {lease.workflow_id}")
    
    def record_api_call(self):
        """Регистрация API вызова для rate limiting"""
        with self._api_calls_lock:
            self.api_calls_window.append(datetime.now())
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Статистика использования ресурсов.

        ADVISORY/best-effort: метод синхронный, а current_concurrent /
        current_memory_mb / active_leases защищены self.lock (asyncio.Lock),
        который из sync-кода захватить нельзя. Каждое поле читается GIL-атомарно
        по отдельности, но комбинированный снимок НЕ консистентен (значения могут
        относиться к разным моментам). Предназначено только для мониторинга/
        логирования, а не для принятия решений о выделении ресурсов — для этого
        используйте acquire_resources, где проверка идёт под self.lock.
        """
        with self._api_calls_lock:
            api_calls_count = len(self.api_calls_window)
        return {
            "concurrent_usage": f"{self.current_concurrent}/{self.max_concurrent}",
            "memory_usage_mb": f"{self.current_memory_mb}/{self.max_memory_mb}",
            "api_calls_per_minute": f"{api_calls_count}/{self.max_api_calls_per_minute}",
            "active_leases": len(self.active_leases),
            "memory_utilization_percent": (self.current_memory_mb / self.max_memory_mb) * 100,
            "concurrent_utilization_percent": (self.current_concurrent / self.max_concurrent) * 100
        }


class ClientQuotaManager:
    """Управление квотами по клиентам"""
    
    def __init__(self):
        self.client_quotas: Dict[str, Dict[str, Any]] = {}
        self.client_usage: Dict[str, Dict[str, Any]] = {}
        self.default_quota = {
            "max_concurrent_workflows": 5,
            "max_memory_mb_per_hour": 2048,
            "max_api_calls_per_hour": 1000,
            "max_workflow_duration_minutes": 60
        }
    
    def set_client_quota(self, client_id: str, quota: Dict[str, Any]):
        """Установка квоты для клиента"""
        self.client_quotas[client_id] = {**self.default_quota, **quota}
        logger.info(f"📊 Установлена квота для клиента {client_id}: {quota}")
    
    def check_client_quota(self, client_id: str, resource_requirements: ResourceLimits) -> bool:
        """Проверка квоты клиента"""
        quota = self.client_quotas.get(client_id, self.default_quota)
        usage = self.client_usage.get(client_id, {
            "concurrent_workflows": 0,
            "memory_used_hour": 0,
            "api_calls_hour": 0,
            "hour_reset": datetime.now()
        })
        
        # Сброс счетчиков если прошел час
        if (datetime.now() - usage.get("hour_reset", datetime.now())).total_seconds() > 3600:
            usage.update({
                "memory_used_hour": 0,
                "api_calls_hour": 0,
                "hour_reset": datetime.now()
            })
        
        # Проверяем ограничения
        required_memory = resource_requirements.max_memory_mb or 512
        required_api_calls = resource_requirements.max_api_calls_per_minute or 10
        
        if (usage["concurrent_workflows"] >= quota["max_concurrent_workflows"] or
            usage["memory_used_hour"] + required_memory > quota["max_memory_mb_per_hour"] or
            usage["api_calls_hour"] + required_api_calls > quota["max_api_calls_per_hour"]):
            
            logger.warning(f"⚠️ Клиент {client_id} превысил квоту")
            return False
        
        return True
    
    def update_client_usage(self, client_id: str, resource_delta: Dict[str, int]):
        """Обновление использования ресурсов клиентом"""
        if client_id not in self.client_usage:
            self.client_usage[client_id] = {
                "concurrent_workflows": 0,
                "memory_used_hour": 0,
                "api_calls_hour": 0,
                "hour_reset": datetime.now()
            }
        
        usage = self.client_usage[client_id]
        for resource, delta in resource_delta.items():
            usage[resource] = max(0, usage.get(resource, 0) + delta)


class ResourceManager:
    """Главный менеджер ресурсов для Workflow Engine"""
    
    def __init__(self):
        self.resource_pool = ResourcePool()
        self.quota_manager = ClientQuotaManager()
        self.active_workflows: Dict[str, Dict[str, Any]] = {}
        
    async def acquire_workflow_resources(self, workflow_id: str, client_id: Optional[str],
                                       requirements: ResourceLimits) -> ResourceLease:
        """Получение ресурсов для workflow с проверкой квот"""
        
        # Проверяем квоту клиента
        if client_id and not self.quota_manager.check_client_quota(client_id, requirements):
            raise ResourceQuotaExceededError(
                f"Клиент {client_id} превысил квоту ресурсов"
            )
        
        # Получаем ресурсы из пула
        lease = await self.resource_pool.acquire_resources(workflow_id, requirements)
        
        if not lease:
            raise ResourceQuotaExceededError(
                "Недостаточно ресурсов в системе. Попробуйте позже."
            )
        
        # Обновляем использование клиента
        if client_id:
            self.quota_manager.update_client_usage(client_id, {
                "concurrent_workflows": 1,
                "memory_used_hour": requirements.max_memory_mb or 512,
                "api_calls_hour": requirements.max_api_calls_per_minute or 10
            })
        
        # Регистрируем активный workflow
        self.active_workflows[workflow_id] = {
            "client_id": client_id,
            "lease": lease,
            "start_time": datetime.now(),
            "requirements": asdict(requirements)
        }
        
        logger.info(f"🚀 Ресурсы выделены для workflow {workflow_id} (клиент: {client_id})")
        return lease
    
    async def release_workflow_resources(self, workflow_id: str):
        """Освобождение ресурсов workflow"""
        if workflow_id not in self.active_workflows:
            logger.warning(f"⚠️ Workflow {workflow_id} не найден в активных")
            return
        
        workflow_info = self.active_workflows[workflow_id]
        lease = workflow_info["lease"]
        client_id = workflow_info["client_id"]
        
        # Освобождаем ресурсы
        await self.resource_pool.release_resources(lease)
        
        # Обновляем использование клиента
        if client_id:
            requirements = workflow_info["requirements"]
            self.quota_manager.update_client_usage(client_id, {
                "concurrent_workflows": -1,
                # memory и api_calls остаются в почасовом лимите
            })
        
        # Убираем из активных
        del self.active_workflows[workflow_id]
        
        logger.info(f"🔓 Ресурсы освобождены для workflow {workflow_id}")
    
    def record_api_call(self, workflow_id: str):
        """Регистрация API вызова для workflow"""
        self.resource_pool.record_api_call()
        
        # Обновляем метрики клиента
        if workflow_id in self.active_workflows:
            client_id = self.active_workflows[workflow_id]["client_id"]
            if client_id:
                self.quota_manager.update_client_usage(client_id, {
                    "api_calls_hour": 1
                })
    
    async def cleanup_expired_leases(self):
        """Очистка просроченных lease'ов"""
        now = datetime.now()
        expired_workflows = []
        
        for workflow_id, info in self.active_workflows.items():
            lease = info["lease"]
            if now > lease.expires_at:
                expired_workflows.append(workflow_id)
        
        for workflow_id in expired_workflows:
            logger.warning(f"⏰ Принудительно освобождаем ресурсы для просроченного workflow {workflow_id}")
            await self.release_workflow_resources(workflow_id)
    
    def get_system_stats(self) -> Dict[str, Any]:
        """Полная статистика системы ресурсов"""
        pool_stats = self.resource_pool.get_usage_stats()
        
        client_stats = {}
        for client_id, usage in self.quota_manager.client_usage.items():
            quota = self.quota_manager.client_quotas.get(client_id, self.quota_manager.default_quota)
            client_stats[client_id] = {
                "usage": usage,
                "quota": quota,
                "utilization": {
                    "concurrent": f"{usage['concurrent_workflows']}/{quota['max_concurrent_workflows']}",
                    "memory_hour": f"{usage['memory_used_hour']}/{quota['max_memory_mb_per_hour']}MB",
                    "api_calls_hour": f"{usage['api_calls_hour']}/{quota['max_api_calls_per_hour']}"
                }
            }
        
        return {
            "resource_pool": pool_stats,
            "active_workflows": len(self.active_workflows),
            "clients": client_stats,
            "total_clients": len(client_stats)
        }
