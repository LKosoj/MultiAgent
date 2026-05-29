"""
Caching система для решений и результатов workflow
"""
import hashlib
import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class CacheEntryStatus(Enum):
    """Статусы записей в кэше"""
    VALID = "valid"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


@dataclass
class CacheEntry:
    """Запись в кэше"""
    key: str
    value: Any
    created_at: datetime
    expires_at: Optional[datetime]
    access_count: int = 0
    last_accessed: Optional[datetime] = None
    metadata: Dict[str, Any] = None
    
    def is_expired(self) -> bool:
        """Проверить истек ли срок действия"""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at
    
    def get_age_seconds(self) -> float:
        """Получить возраст записи в секундах"""
        return (datetime.now() - self.created_at).total_seconds()
    
    def access(self):
        """Отметить доступ к записи"""
        self.access_count += 1
        self.last_accessed = datetime.now()


class DecisionCache:
    """Кэш для решений планировщика и судьи"""
    
    def __init__(self, max_size: int = 1000, default_ttl_seconds: int = 3600):
        self.max_size = max_size
        self.default_ttl_seconds = default_ttl_seconds
        self.cache: Dict[str, CacheEntry] = {}
        self.hit_count = 0
        self.miss_count = 0
        
    def _generate_key(self, step_id: str, task: str, context: Dict[str, Any],
                     decision_type: str) -> str:
        """Генерировать ключ кэша"""
        
        # Создаем нормализованный контекст (убираем изменчивые поля)
        normalized_context = {
            k: v for k, v in context.items()
            if k not in ['timestamp', 'workflow_id', 'session_id', 'start_time']
        }
        
        # Создаем строку для хеширования
        key_data = {
            "step_id": step_id,
            "task": task,
            "context": normalized_context,
            "decision_type": decision_type
        }
        
        key_string = json.dumps(key_data, sort_keys=True, default=str)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get_cached_decision(self, step_id: str, task: str, context: Dict[str, Any],
                           decision_type: str) -> Optional[Any]:
        """Получить кэшированное решение"""
        
        cache_key = self._generate_key(step_id, task, context, decision_type)
        
        if cache_key not in self.cache:
            self.miss_count += 1
            return None
        
        entry = self.cache[cache_key]
        
        # Проверяем срок действия
        if entry.is_expired():
            logger.debug(f"🕒 Cache entry expired for key {cache_key[:8]}...")
            del self.cache[cache_key]
            self.miss_count += 1
            return None
        
        # Отмечаем доступ и возвращаем значение
        entry.access()
        self.hit_count += 1
        
        logger.debug(f"💾 Cache hit for {decision_type} decision (key: {cache_key[:8]}...)")
        return entry.value
    
    def cache_decision(self, step_id: str, task: str, context: Dict[str, Any],
                      decision_type: str, decision: Any, ttl_seconds: Optional[int] = None):
        """Кэшировать решение"""
        
        cache_key = self._generate_key(step_id, task, context, decision_type)
        
        # Используем дефолтный TTL если не указан
        if ttl_seconds is None:
            ttl_seconds = self.default_ttl_seconds
        
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds) if ttl_seconds > 0 else None
        
        entry = CacheEntry(
            key=cache_key,
            value=decision,
            created_at=datetime.now(),
            expires_at=expires_at,
            metadata={
                "step_id": step_id,
                "decision_type": decision_type,
                "task_length": len(task),
                "context_keys": list(context.keys())
            }
        )
        
        # Проверяем размер кэша
        if len(self.cache) >= self.max_size:
            self._evict_entries()
        
        self.cache[cache_key] = entry
        logger.debug(f"💾 Cached {decision_type} decision (key: {cache_key[:8]}..., TTL: {ttl_seconds}s)")
    
    def _evict_entries(self):
        """Удалить старые записи из кэша"""
        
        # Удаляем истекшие записи
        expired_keys = [
            key for key, entry in self.cache.items()
            if entry.is_expired()
        ]
        
        for key in expired_keys:
            del self.cache[key]
        
        # Если все еще много записей, удаляем самые старые и редко используемые
        if len(self.cache) >= self.max_size:
            # Сортируем по комбинации возраста и частоты доступа
            entries_by_priority = sorted(
                self.cache.items(),
                key=lambda item: (item[1].access_count, -item[1].get_age_seconds())
            )
            
            # Удаляем четверть записей
            to_remove = len(entries_by_priority) // 4
            for key, _ in entries_by_priority[:to_remove]:
                del self.cache[key]
        
        logger.info(f"🧹 Cache eviction completed, current size: {len(self.cache)}")
    
    def invalidate_for_step(self, step_id: str):
        """Инвалидировать все записи для конкретного шага"""
        
        keys_to_remove = []
        for key, entry in self.cache.items():
            if entry.metadata and entry.metadata.get("step_id") == step_id:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.cache[key]
        
        logger.info(f"🗑️ Invalidated {len(keys_to_remove)} cache entries for step '{step_id}'")
    
    def clear_cache(self):
        """Очистить весь кэш"""
        self.cache.clear()
        self.hit_count = 0
        self.miss_count = 0
        logger.info("🗑️ Decision cache cleared")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Получить статистику кэша"""
        
        total_requests = self.hit_count + self.miss_count
        hit_rate = (self.hit_count / total_requests) * 100 if total_requests > 0 else 0
        
        # Статистика по типам решений
        decision_types = {}
        for entry in self.cache.values():
            decision_type = entry.metadata.get("decision_type", "unknown")
            if decision_type not in decision_types:
                decision_types[decision_type] = {"count": 0, "avg_age": 0}
            
            decision_types[decision_type]["count"] += 1
            decision_types[decision_type]["avg_age"] += entry.get_age_seconds()
        
        # Вычисляем средний возраст
        for stats in decision_types.values():
            if stats["count"] > 0:
                stats["avg_age"] /= stats["count"]
        
        return {
            "cache_size": len(self.cache),
            "max_size": self.max_size,
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_rate_percentage": hit_rate,
            "decision_types": decision_types,
            "default_ttl_seconds": self.default_ttl_seconds
        }


class ResultCache:
    """Кэш для результатов выполнения шагов"""
    
    def __init__(self, max_size: int = 500, default_ttl_seconds: int = 1800):
        self.max_size = max_size
        self.default_ttl_seconds = default_ttl_seconds
        self.cache: Dict[str, CacheEntry] = {}
        self.hit_count = 0
        self.miss_count = 0
        
    def _generate_result_key(self, agent_type: str, task: str, context: Dict[str, Any]) -> str:
        """Генерировать ключ для кэша результатов"""
        
        # Для результатов учитываем только стабильные части контекста
        stable_context = {
            k: v for k, v in context.items()
            if k in ['variables', 'step_outputs'] and v is not None
        }
        
        key_data = {
            "agent_type": agent_type,
            "task": task.strip(),
            "context": stable_context
        }
        
        key_string = json.dumps(key_data, sort_keys=True, default=str)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get_cached_result(self, agent_type: str, task: str, context: Dict[str, Any]) -> Optional[Any]:
        """Получить кэшированный результат"""
        
        cache_key = self._generate_result_key(agent_type, task, context)
        
        if cache_key not in self.cache:
            self.miss_count += 1
            return None
        
        entry = self.cache[cache_key]
        
        if entry.is_expired():
            logger.debug(f"🕒 Result cache entry expired for key {cache_key[:8]}...")
            del self.cache[cache_key]
            self.miss_count += 1
            return None
        
        entry.access()
        self.hit_count += 1
        
        logger.debug(f"⚡ Result cache hit for agent '{agent_type}' (key: {cache_key[:8]}...)")
        return entry.value
    
    def cache_result(self, agent_type: str, task: str, context: Dict[str, Any],
                    result: Any, quality_score: float = 0.0, ttl_seconds: Optional[int] = None):
        """Кэшировать результат выполнения"""
        
        # Кэшируем только качественные результаты
        if quality_score < 0.7:
            logger.debug(f"🚫 Not caching low quality result (score: {quality_score:.2f})")
            return
        
        cache_key = self._generate_result_key(agent_type, task, context)
        
        if ttl_seconds is None:
            # TTL зависит от качества результата
            base_ttl = self.default_ttl_seconds
            quality_multiplier = min(2.0, quality_score + 0.5)  # 0.7-1.0 -> 1.2-1.5
            ttl_seconds = int(base_ttl * quality_multiplier)
        
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds) if ttl_seconds > 0 else None
        
        entry = CacheEntry(
            key=cache_key,
            value=result,
            created_at=datetime.now(),
            expires_at=expires_at,
            metadata={
                "agent_type": agent_type,
                "task_length": len(task),
                "quality_score": quality_score,
                "result_length": len(str(result))
            }
        )
        
        # Проверяем размер кэша
        if len(self.cache) >= self.max_size:
            self._evict_entries()
        
        self.cache[cache_key] = entry
        logger.debug(f"⚡ Cached result for agent '{agent_type}' "
                    f"(key: {cache_key[:8]}..., quality: {quality_score:.2f}, TTL: {ttl_seconds}s)")
    
    def _evict_entries(self):
        """Удалить старые записи из кэша результатов"""
        
        # Удаляем истекшие записи
        expired_keys = [
            key for key, entry in self.cache.items()
            if entry.is_expired()
        ]
        
        for key in expired_keys:
            del self.cache[key]
        
        # Если все еще много записей, удаляем записи с низким качеством и редким использованием
        if len(self.cache) >= self.max_size:
            entries_by_priority = sorted(
                self.cache.items(),
                key=lambda item: (
                    item[1].metadata.get("quality_score", 0.0),
                    item[1].access_count,
                    -item[1].get_age_seconds()
                )
            )
            
            to_remove = len(entries_by_priority) // 4
            for key, _ in entries_by_priority[:to_remove]:
                del self.cache[key]
        
        logger.info(f"🧹 Result cache eviction completed, current size: {len(self.cache)}")
    
    def invalidate_for_agent(self, agent_type: str):
        """Инвалидировать записи для конкретного агента"""
        
        keys_to_remove = []
        for key, entry in self.cache.items():
            if entry.metadata and entry.metadata.get("agent_type") == agent_type:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self.cache[key]
        
        logger.info(f"🗑️ Invalidated {len(keys_to_remove)} result cache entries for agent '{agent_type}'")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Получить статистику кэша результатов"""
        
        total_requests = self.hit_count + self.miss_count
        hit_rate = (self.hit_count / total_requests) * 100 if total_requests > 0 else 0
        
        # Статистика по агентам
        agent_stats = {}
        total_quality = 0
        quality_count = 0
        
        for entry in self.cache.values():
            agent_type = entry.metadata.get("agent_type", "unknown")
            quality_score = entry.metadata.get("quality_score", 0.0)
            
            if agent_type not in agent_stats:
                agent_stats[agent_type] = {
                    "count": 0, 
                    "avg_quality": 0, 
                    "avg_access_count": 0
                }
            
            agent_stats[agent_type]["count"] += 1
            agent_stats[agent_type]["avg_quality"] += quality_score
            agent_stats[agent_type]["avg_access_count"] += entry.access_count
            
            total_quality += quality_score
            quality_count += 1
        
        # Вычисляем средние значения
        for stats in agent_stats.values():
            if stats["count"] > 0:
                stats["avg_quality"] /= stats["count"]
                stats["avg_access_count"] /= stats["count"]
        
        avg_quality = total_quality / quality_count if quality_count > 0 else 0
        
        return {
            "cache_size": len(self.cache),
            "max_size": self.max_size,
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_rate_percentage": hit_rate,
            "average_quality_score": avg_quality,
            "agent_statistics": agent_stats,
            "default_ttl_seconds": self.default_ttl_seconds
        }
    
    def get_quality_distribution(self) -> Dict[str, int]:
        """Получить распределение по качеству кэшированных результатов"""
        
        distribution = {
            "excellent (>0.9)": 0,
            "good (0.8-0.9)": 0,
            "acceptable (0.7-0.8)": 0,
            "poor (<0.7)": 0
        }
        
        for entry in self.cache.values():
            quality = entry.metadata.get("quality_score", 0.0)
            
            if quality > 0.9:
                distribution["excellent (>0.9)"] += 1
            elif quality > 0.8:
                distribution["good (0.8-0.9)"] += 1
            elif quality > 0.7:
                distribution["acceptable (0.7-0.8)"] += 1
            else:
                distribution["poor (<0.7)"] += 1
        
        return distribution
    
    def clear_cache(self):
        """Очистить кэш результатов"""
        self.cache.clear()
        self.hit_count = 0
        self.miss_count = 0
        logger.info("🗑️ Result cache cleared")


class CacheManager:
    """Менеджер всех кэшей"""
    
    def __init__(self):
        self.decision_cache = DecisionCache()
        self.result_cache = ResultCache()
        
    def get_combined_stats(self) -> Dict[str, Any]:
        """Получить объединенную статистику всех кэшей"""
        
        decision_stats = self.decision_cache.get_cache_stats()
        result_stats = self.result_cache.get_cache_stats()
        
        total_hits = decision_stats["hit_count"] + result_stats["hit_count"]
        total_misses = decision_stats["miss_count"] + result_stats["miss_count"]
        total_requests = total_hits + total_misses
        
        return {
            "overall": {
                "total_requests": total_requests,
                "total_hits": total_hits,
                "total_misses": total_misses,
                "overall_hit_rate": (total_hits / total_requests * 100) if total_requests > 0 else 0,
                "total_cache_size": decision_stats["cache_size"] + result_stats["cache_size"]
            },
            "decision_cache": decision_stats,
            "result_cache": result_stats,
            "result_quality_distribution": self.result_cache.get_quality_distribution()
        }
    
    def clear_all_caches(self):
        """Очистить все кэши"""
        self.decision_cache.clear_cache()
        self.result_cache.clear_cache()
        logger.info("🗑️ All caches cleared")
    
    def optimize_cache_sizes(self):
        """Оптимизировать размеры кэшей на основе использования"""
        
        decision_stats = self.decision_cache.get_cache_stats()
        result_stats = self.result_cache.get_cache_stats()
        
        # Простая эвристика: увеличиваем размер кэша с высоким hit rate
        if decision_stats["hit_rate_percentage"] > 80 and decision_stats["cache_size"] == decision_stats["max_size"]:
            self.decision_cache.max_size = min(2000, int(self.decision_cache.max_size * 1.2))
            logger.info(f"📈 Increased decision cache size to {self.decision_cache.max_size}")
        
        if result_stats["hit_rate_percentage"] > 80 and result_stats["cache_size"] == result_stats["max_size"]:
            self.result_cache.max_size = min(1000, int(self.result_cache.max_size * 1.2))
            logger.info(f"📈 Increased result cache size to {self.result_cache.max_size}")
