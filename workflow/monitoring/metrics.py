"""
Система сбора и агрегации метрик для workflow
"""
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque
import time
import threading

logger = logging.getLogger(__name__)


class MetricType(Enum):
    """Типы метрик"""
    COUNTER = "counter"          # Счетчик (всегда растет)
    GAUGE = "gauge"              # Текущее значение
    HISTOGRAM = "histogram"      # Распределение значений
    TIMER = "timer"              # Измерение времени


@dataclass
class MetricValue:
    """Значение метрики"""
    value: float
    timestamp: datetime
    labels: Dict[str, str] = field(default_factory=dict)
    
    def __str__(self):
        labels_str = ",".join([f"{k}={v}" for k, v in self.labels.items()])
        return f"{self.value} [{labels_str}] @ {self.timestamp.isoformat()}"


@dataclass
class HistogramBucket:
    """Bucket для гистограммы"""
    upper_bound: float
    count: int = 0


class Metric:
    """Базовый класс для метрики"""
    
    def __init__(self, name: str, metric_type: MetricType, 
                 description: str = "", unit: str = ""):
        self.name = name
        self.type = metric_type
        self.description = description
        self.unit = unit
        self.values: List[MetricValue] = []
        self.labels_index: Dict[str, List[MetricValue]] = defaultdict(list)
        self._lock = threading.Lock()
        
    def add_value(self, value: float, labels: Dict[str, str] = None):
        """Добавить значение метрики"""
        labels = labels or {}
        
        with self._lock:
            metric_value = MetricValue(
                value=value,
                timestamp=datetime.now(),
                labels=labels.copy()
            )
            
            self.values.append(metric_value)
            
            # Индексируем по labels
            labels_key = self._labels_to_key(labels)
            self.labels_index[labels_key].append(metric_value)
            
            # Ограничиваем историю
            if len(self.values) > 10000:
                # Удаляем старые значения
                cutoff = len(self.values) - 5000
                removed_values = self.values[:cutoff]
                self.values = self.values[cutoff:]
                
                # Очищаем индекс
                self._cleanup_labels_index(removed_values)
    
    def _labels_to_key(self, labels: Dict[str, str]) -> str:
        """Преобразовать labels в строковый ключ"""
        return ",".join([f"{k}={v}" for k, v in sorted(labels.items())])
    
    def _cleanup_labels_index(self, removed_values: List[MetricValue]):
        """Очистить индекс от удаленных значений"""
        removed_set = set(id(v) for v in removed_values)
        keys_to_delete = []
        for labels_key, values_list in self.labels_index.items():
            new_list = [v for v in values_list if id(v) not in removed_set]
            if new_list:
                self.labels_index[labels_key] = new_list
            else:
                keys_to_delete.append(labels_key)
        for key in keys_to_delete:
            del self.labels_index[key]
    
    def get_current_value(self, labels: Dict[str, str] = None) -> Optional[float]:
        """Получить текущее значение метрики"""
        with self._lock:
            if not self.values:
                return None

            if labels:
                labels_key = self._labels_to_key(labels)
                if labels_key in self.labels_index and self.labels_index[labels_key]:
                    return self.labels_index[labels_key][-1].value
                return None

            return self.values[-1].value
    
    def get_values_in_range(self, start_time: datetime, end_time: datetime,
                           labels: Dict[str, str] = None) -> List[MetricValue]:
        """Получить значения в временном диапазоне"""
        values_to_search = self.values
        
        if labels:
            labels_key = self._labels_to_key(labels)
            values_to_search = self.labels_index.get(labels_key, [])
        
        return [
            value for value in values_to_search
            if start_time <= value.timestamp <= end_time
        ]
    
    def calculate_rate(self, duration_minutes: int = 5, 
                      labels: Dict[str, str] = None) -> float:
        """Вычислить rate (изменение в единицу времени)"""
        if self.type != MetricType.COUNTER:
            return 0.0
        
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=duration_minutes)
        
        values = self.get_values_in_range(start_time, end_time, labels)
        
        if len(values) < 2:
            return 0.0
        
        # Для счетчика rate = (последнее значение - первое значение) / время
        time_diff = (values[-1].timestamp - values[0].timestamp).total_seconds()
        if time_diff <= 0:
            return 0.0
        
        value_diff = values[-1].value - values[0].value
        return value_diff / time_diff  # per second
    
    def calculate_average(self, duration_minutes: int = 5,
                         labels: Dict[str, str] = None) -> float:
        """Вычислить среднее значение"""
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=duration_minutes)
        
        values = self.get_values_in_range(start_time, end_time, labels)
        
        if not values:
            return 0.0
        
        return sum(v.value for v in values) / len(values)


class Timer:
    """Контекстный менеджер для измерения времени"""
    
    def __init__(self, metric: Metric, labels: Dict[str, str] = None):
        self.metric = metric
        self.labels = labels or {}
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration = time.time() - self.start_time
            self.metric.add_value(duration, self.labels)


@dataclass
class WorkflowMetrics:
    """Набор метрик для workflow"""
    
    # Основные метрики производительности
    workflow_executions_total: int = 0
    workflow_executions_success: int = 0
    workflow_executions_failed: int = 0
    
    step_executions_total: int = 0
    step_executions_success: int = 0
    step_executions_failed: int = 0
    
    # Метрики времени
    avg_workflow_duration: float = 0.0
    avg_step_duration: float = 0.0
    
    # Метрики качества
    avg_quality_score: float = 0.0
    quality_below_threshold_count: int = 0
    
    # Метрики надежности
    circuit_breaker_opens: int = 0
    retry_attempts_total: int = 0
    retry_attempts_success: int = 0
    
    # Метрики ресурсов
    total_cost: float = 0.0
    total_tokens: int = 0
    avg_memory_usage: float = 0.0
    
    # Метрики кэша
    cache_hits: int = 0
    cache_misses: int = 0
    
    def get_success_rate(self) -> float:
        """Процент успешных выполнений workflow"""
        if self.workflow_executions_total == 0:
            return 0.0
        return (self.workflow_executions_success / self.workflow_executions_total) * 100
    
    def get_cache_hit_rate(self) -> float:
        """Процент попаданий в кэш"""
        total_cache_requests = self.cache_hits + self.cache_misses
        if total_cache_requests == 0:
            return 0.0
        return (self.cache_hits / total_cache_requests) * 100
    
    def get_retry_success_rate(self) -> float:
        """Процент успешных retry"""
        if self.retry_attempts_total == 0:
            return 0.0
        return (self.retry_attempts_success / self.retry_attempts_total) * 100


class MetricsCollector:
    """Сборщик метрик для enhanced workflow"""
    
    def __init__(self):
        self.metrics: Dict[str, Metric] = {}
        self.workflow_metrics = WorkflowMetrics()
        self._lock = threading.Lock()
        
        # Создаем стандартные метрики
        self._create_standard_metrics()
        
    def _create_standard_metrics(self):
        """Создать стандартные метрики"""
        
        # Метрики выполнения
        self.register_metric("workflow_executions_total", MetricType.COUNTER,
                           "Total number of workflow executions")
        self.register_metric("workflow_duration_seconds", MetricType.HISTOGRAM,
                           "Workflow execution duration in seconds")
        self.register_metric("step_executions_total", MetricType.COUNTER,
                           "Total number of step executions")
        self.register_metric("step_duration_seconds", MetricType.HISTOGRAM,
                           "Step execution duration in seconds")
        
        # Метрики качества
        self.register_metric("quality_score", MetricType.GAUGE,
                           "Quality score of workflow results")
        self.register_metric("validation_failures_total", MetricType.COUNTER,
                           "Total number of validation failures")
        
        # Метрики надежности
        self.register_metric("circuit_breaker_state", MetricType.GAUGE,
                           "Circuit breaker state (0=closed, 1=open)")
        self.register_metric("retry_attempts_total", MetricType.COUNTER,
                           "Total number of retry attempts")
        
        # Метрики ресурсов
        self.register_metric("cost_total_usd", MetricType.COUNTER,
                           "Total cost in USD")
        self.register_metric("tokens_consumed_total", MetricType.COUNTER,
                           "Total tokens consumed")
        self.register_metric("memory_usage_bytes", MetricType.GAUGE,
                           "Memory usage in bytes")
        
        # Метрики кэша
        self.register_metric("cache_hits_total", MetricType.COUNTER,
                           "Total cache hits")
        self.register_metric("cache_misses_total", MetricType.COUNTER,
                           "Total cache misses")
        
        logger.info("📊 Standard metrics created")
    
    def register_metric(self, name: str, metric_type: MetricType,
                       description: str = "", unit: str = "") -> Metric:
        """Зарегистрировать новую метрику"""
        
        with self._lock:
            if name in self.metrics:
                logger.warning(f"⚠️ Metric '{name}' already exists")
                return self.metrics[name]
            
            metric = Metric(name, metric_type, description, unit)
            self.metrics[name] = metric
            
            logger.debug(f"📊 Registered metric: {name} ({metric_type.value})")
            return metric
    
    def increment_counter(self, name: str, value: float = 1.0,
                         labels: Dict[str, str] = None):
        """Увеличить счетчик"""
        if name in self.metrics:
            with self._lock:
                current_value = self.metrics[name].get_current_value(labels) or 0.0
                self.metrics[name].add_value(current_value + value, labels)
    
    def set_gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        """Установить значение gauge"""
        if name in self.metrics:
            self.metrics[name].add_value(value, labels)
    
    def observe_histogram(self, name: str, value: float, labels: Dict[str, str] = None):
        """Добавить наблюдение в гистограмму"""
        if name in self.metrics:
            self.metrics[name].add_value(value, labels)
    
    def time_operation(self, name: str, labels: Dict[str, str] = None) -> Timer:
        """Создать timer для операции"""
        if name not in self.metrics:
            self.register_metric(name, MetricType.TIMER, f"Duration of {name}")
        
        return Timer(self.metrics[name], labels)
    
    # Методы для записи workflow событий
    
    def record_workflow_start(self, workflow_id: str, workflow_name: str):
        """Записать начало выполнения workflow"""
        self.increment_counter("workflow_executions_total", 
                             labels={"workflow_name": workflow_name})
        logger.debug(f"📊 Workflow started: {workflow_id}")
    
    def record_workflow_completion(self, workflow_id: str, workflow_name: str,
                                 duration: float, success: bool, quality_score: float = None):
        """Записать завершение workflow"""
        
        status = "success" if success else "failed"
        labels = {"workflow_name": workflow_name, "status": status}
        
        # Записываем длительность
        self.observe_histogram("workflow_duration_seconds", duration, labels)
        
        # Записываем качество если есть
        if quality_score is not None:
            self.set_gauge("quality_score", quality_score, labels)
        
        # Обновляем агрегированные метрики
        with self._lock:
            self.workflow_metrics.workflow_executions_total += 1
            if success:
                self.workflow_metrics.workflow_executions_success += 1
            else:
                self.workflow_metrics.workflow_executions_failed += 1
            
            # Обновляем среднее время
            total = self.workflow_metrics.workflow_executions_total
            current_avg = self.workflow_metrics.avg_workflow_duration
            self.workflow_metrics.avg_workflow_duration = (current_avg * (total - 1) + duration) / total
            
            if quality_score is not None:
                current_avg_quality = self.workflow_metrics.avg_quality_score
                self.workflow_metrics.avg_quality_score = (current_avg_quality * (total - 1) + quality_score) / total
        
        logger.debug(f"📊 Workflow completed: {workflow_id}, duration: {duration:.2f}s, success: {success}")
    
    def record_step_execution(self, step_id: str, agent_type: str, duration: float,
                            success: bool, retry_count: int = 0, quality_score: float = None):
        """Записать выполнение шага"""
        
        status = "success" if success else "failed"
        labels = {"step_id": step_id, "agent_type": agent_type, "status": status}
        
        # Записываем метрики
        self.increment_counter("step_executions_total", labels=labels)
        self.observe_histogram("step_duration_seconds", duration, labels)
        
        if retry_count > 0:
            self.increment_counter("retry_attempts_total", retry_count,
                                 labels={"step_id": step_id, "agent_type": agent_type})
        
        # Обновляем агрегированные метрики
        with self._lock:
            self.workflow_metrics.step_executions_total += 1
            if success:
                self.workflow_metrics.step_executions_success += 1
            else:
                self.workflow_metrics.step_executions_failed += 1
            
            self.workflow_metrics.retry_attempts_total += retry_count
            if success and retry_count > 0:
                self.workflow_metrics.retry_attempts_success += retry_count
    
    def record_circuit_breaker_event(self, agent_type: str, state: str):
        """Записать событие circuit breaker"""
        state_value = 1.0 if state == "open" else 0.0
        self.set_gauge("circuit_breaker_state", state_value, 
                      labels={"agent_type": agent_type})
        
        if state == "open":
            with self._lock:
                self.workflow_metrics.circuit_breaker_opens += 1
    
    def record_cache_event(self, cache_type: str, hit: bool):
        """Записать событие кэша"""
        if hit:
            self.increment_counter("cache_hits_total", labels={"cache_type": cache_type})
            with self._lock:
                self.workflow_metrics.cache_hits += 1
        else:
            self.increment_counter("cache_misses_total", labels={"cache_type": cache_type})
            with self._lock:
                self.workflow_metrics.cache_misses += 1
    
    def record_resource_usage(self, cost: float = 0.0, tokens: int = 0, memory_bytes: float = 0.0):
        """Записать использование ресурсов"""
        if cost > 0:
            self.increment_counter("cost_total_usd", cost)
            with self._lock:
                self.workflow_metrics.total_cost += cost
        
        if tokens > 0:
            self.increment_counter("tokens_consumed_total", tokens)
            with self._lock:
                self.workflow_metrics.total_tokens += tokens
        
        if memory_bytes > 0:
            self.set_gauge("memory_usage_bytes", memory_bytes)
            with self._lock:
                # Простое скользящее среднее
                self.workflow_metrics.avg_memory_usage = (self.workflow_metrics.avg_memory_usage + memory_bytes) / 2
    
    def get_metric(self, name: str) -> Optional[Metric]:
        """Получить метрику по имени"""
        return self.metrics.get(name)
    
    def get_all_metrics(self) -> Dict[str, Metric]:
        """Получить все метрики"""
        return self.metrics.copy()
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Получить сводку по всем метрикам"""
        
        summary = {
            "total_metrics": len(self.metrics),
            "aggregated_metrics": {
                "workflow_success_rate": self.workflow_metrics.get_success_rate(),
                "cache_hit_rate": self.workflow_metrics.get_cache_hit_rate(),
                "retry_success_rate": self.workflow_metrics.get_retry_success_rate(),
                "avg_workflow_duration": self.workflow_metrics.avg_workflow_duration,
                "avg_quality_score": self.workflow_metrics.avg_quality_score,
                "total_cost": self.workflow_metrics.total_cost,
                "total_tokens": self.workflow_metrics.total_tokens
            },
            "recent_metrics": {}
        }
        
        # Добавляем последние значения ключевых метрик
        key_metrics = [
            "workflow_executions_total", "quality_score", 
            "circuit_breaker_state", "memory_usage_bytes"
        ]
        
        for metric_name in key_metrics:
            if metric_name in self.metrics:
                current_value = self.metrics[metric_name].get_current_value()
                if current_value is not None:
                    summary["recent_metrics"][metric_name] = current_value
        
        return summary
    
    def export_prometheus_format(self) -> str:
        """Экспортировать метрики в формате Prometheus"""
        
        lines = []
        
        for metric in self.metrics.values():
            # HELP
            if metric.description:
                lines.append(f"# HELP {metric.name} {metric.description}")
            
            # TYPE
            prom_type = {
                MetricType.COUNTER: "counter",
                MetricType.GAUGE: "gauge", 
                MetricType.HISTOGRAM: "histogram",
                MetricType.TIMER: "histogram"
            }.get(metric.type, "gauge")
            lines.append(f"# TYPE {metric.name} {prom_type}")
            
            # VALUES
            if metric.values:
                # Группируем по labels
                labels_groups = {}
                for value in metric.values[-100:]:  # Последние 100 значений
                    labels_key = metric._labels_to_key(value.labels)
                    if labels_key not in labels_groups:
                        labels_groups[labels_key] = []
                    labels_groups[labels_key].append(value)
                
                # Выводим последнее значение для каждой группы labels
                for labels_key, values in labels_groups.items():
                    latest_value = values[-1]
                    
                    if latest_value.labels:
                        labels_str = "{" + ",".join([f'{k}="{v}"' for k, v in latest_value.labels.items()]) + "}"
                        lines.append(f"{metric.name}{labels_str} {latest_value.value}")
                    else:
                        lines.append(f"{metric.name} {latest_value.value}")
        
        return "\n".join(lines)
    
    def reset_metrics(self):
        """Сбросить все метрики"""
        with self._lock:
            for metric in self.metrics.values():
                metric.values.clear()
                metric.labels_index.clear()
            
            self.workflow_metrics = WorkflowMetrics()
        
        logger.info("📊 All metrics reset")
