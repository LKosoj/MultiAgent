"""
Loop Detection для предотвращения зацикливания в workflow
"""
import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from collections import Counter
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class LoopPattern:
    """Паттерн зацикливания"""
    step_id: str
    pattern_hash: str
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    pattern_data: Dict[str, Any]
    
    def is_loop(self, threshold: int = 3) -> bool:
        """Определить является ли это зацикливанием"""
        return self.occurrences >= threshold


class LoopDetector:
    """Детектор зацикливания в выполнении workflow"""
    
    def __init__(self):
        # История выполнения шагов для обнаружения паттернов
        self.execution_history: Dict[str, List[Dict[str, Any]]] = {}
        
        # Обнаруженные паттерны зацикливания
        self.detected_patterns: Dict[str, LoopPattern] = {}
        
        # Конфигурация детектора
        self.config = {
            "loop_threshold": 3,  # Сколько повторений считать зацикливанием
            "time_window_minutes": 30,  # Временное окно для анализа
            "pattern_similarity_threshold": 0.9,  # Порог схожести паттернов
            "max_history_entries": 100  # Максимум записей в истории
        }
        
    def record_step_execution(self, workflow_id: str, step_id: str, 
                            execution_data: Dict[str, Any]) -> bool:
        """
        Записать выполнение шага и проверить на зацикливание
        
        Returns:
            True если обнаружено зацикливание
        """
        
        # Создаем запись о выполнении
        execution_record = {
            "timestamp": datetime.now(),
            "step_id": step_id,
            "task_hash": self._hash_task(execution_data.get("task", "")),
            "output_hash": self._hash_output(execution_data.get("output")),
            "error": execution_data.get("error"),
            "quality_score": execution_data.get("quality_score", 0.0),
            "decision": execution_data.get("decision", ""),
            "retry_count": execution_data.get("retry_count", 0)
        }
        
        # Добавляем в историю
        history_key = f"{workflow_id}:{step_id}"
        if history_key not in self.execution_history:
            self.execution_history[history_key] = []
        
        self.execution_history[history_key].append(execution_record)
        
        # Ограничиваем размер истории
        if len(self.execution_history[history_key]) > self.config["max_history_entries"]:
            self.execution_history[history_key] = self.execution_history[history_key][-self.config["max_history_entries"]:]
        
        # Проверяем на зацикливание
        loop_detected = self._analyze_for_loops(workflow_id, step_id)
        
        if loop_detected:
            logger.warning(f"🔄 Loop detected for workflow '{workflow_id}', step '{step_id}'")
        
        return loop_detected
    
    def _hash_task(self, task: str) -> str:
        """Создать хеш задачи для сравнения"""
        # Нормализуем задачу (убираем лишние пробелы, приводим к нижнему регистру)
        normalized_task = " ".join(task.lower().split())
        return hashlib.md5(normalized_task.encode()).hexdigest()[:16]
    
    def _hash_output(self, output: Any) -> str:
        """Создать хеш выхода для сравнения"""
        if output is None:
            return "none"
        
        output_str = str(output)
        # Для очень длинных выходов берем только начало и конец
        if len(output_str) > 1000:
            output_str = output_str[:500] + output_str[-500:]
        
        return hashlib.md5(output_str.encode()).hexdigest()[:16]
    
    def _analyze_for_loops(self, workflow_id: str, step_id: str) -> bool:
        """Анализировать историю на предмет зацикливания"""
        
        history_key = f"{workflow_id}:{step_id}"
        history = self.execution_history.get(history_key, [])
        
        if len(history) < self.config["loop_threshold"]:
            return False
        
        # Анализируем последние записи в временном окне
        cutoff_time = datetime.now() - timedelta(minutes=self.config["time_window_minutes"])
        recent_history = [r for r in history if r["timestamp"] >= cutoff_time]
        
        if len(recent_history) < self.config["loop_threshold"]:
            return False
        
        # Проверяем различные типы зацикливания
        loop_types = [
            self._detect_identical_executions,
            self._detect_error_retry_loops,
            self._detect_quality_loops,
            self._detect_decision_loops
        ]
        
        for detect_func in loop_types:
            if detect_func(workflow_id, step_id, recent_history):
                return True
        
        return False
    
    def _detect_identical_executions(self, workflow_id: str, step_id: str, 
                                   history: List[Dict[str, Any]]) -> bool:
        """Обнаружить одинаковые выполнения"""
        
        # Ищем повторяющиеся комбинации task_hash + output_hash
        signatures = []
        for record in history[-10:]:  # Анализируем последние 10 записей
            signature = f"{record['task_hash']}:{record['output_hash']}"
            signatures.append(signature)
        
        # Подсчитываем повторения
        signature_counts = Counter(signatures)
        
        for signature, count in signature_counts.items():
            if count >= self.config["loop_threshold"]:
                pattern_key = f"{workflow_id}:{step_id}:identical:{signature}"
                self._record_loop_pattern(
                    pattern_key, workflow_id, step_id, "identical_executions",
                    {"signature": signature, "count": count}
                )
                return True
        
        return False
    
    def _detect_error_retry_loops(self, workflow_id: str, step_id: str,
                                history: List[Dict[str, Any]]) -> bool:
        """Обнаружить зацикливание в retry ошибок"""
        
        # Ищем повторяющиеся ошибки с одной и той же задачей
        error_patterns = []
        
        for record in history[-10:]:
            if record["error"]:
                pattern = f"{record['task_hash']}:{record['error'][:100]}"  # Первые 100 символов ошибки
                error_patterns.append(pattern)
        
        pattern_counts = Counter(error_patterns)
        
        for pattern, count in pattern_counts.items():
            if count >= self.config["loop_threshold"]:
                pattern_key = f"{workflow_id}:{step_id}:error_retry:{pattern[:50]}"
                self._record_loop_pattern(
                    pattern_key, workflow_id, step_id, "error_retry_loop",
                    {"error_pattern": pattern, "count": count}
                )
                return True
        
        return False
    
    def _detect_quality_loops(self, workflow_id: str, step_id: str,
                            history: List[Dict[str, Any]]) -> bool:
        """Обнаружить зацикливание по качеству"""
        
        # Ищем паттерны где качество не улучшается несколько раз подряд
        quality_scores = [r["quality_score"] for r in history[-10:] if r["quality_score"] > 0]
        
        if len(quality_scores) < self.config["loop_threshold"]:
            return False
        
        # Проверяем нет ли роста качества в последних попытках
        recent_scores = quality_scores[-self.config["loop_threshold"]:]
        
        # Если все оценки низкие и примерно одинаковые
        if (all(score < 0.5 for score in recent_scores) and 
            max(recent_scores) - min(recent_scores) < 0.1):
            
            pattern_key = f"{workflow_id}:{step_id}:quality_stagnation"
            self._record_loop_pattern(
                pattern_key, workflow_id, step_id, "quality_stagnation",
                {"scores": recent_scores, "avg_score": sum(recent_scores) / len(recent_scores)}
            )
            return True
        
        return False
    
    def _detect_decision_loops(self, workflow_id: str, step_id: str,
                             history: List[Dict[str, Any]]) -> bool:
        """Обнаружить зацикливание в решениях"""
        
        # Ищем повторяющиеся решения "retry"
        recent_decisions = [r["decision"] for r in history[-10:]]
        retry_count = recent_decisions.count("retry")
        
        if retry_count >= self.config["loop_threshold"]:
            pattern_key = f"{workflow_id}:{step_id}:decision_retry"
            self._record_loop_pattern(
                pattern_key, workflow_id, step_id, "decision_retry_loop",
                {"retry_count": retry_count, "recent_decisions": recent_decisions[-5:]}
            )
            return True
        
        return False
    
    def _record_loop_pattern(self, pattern_key: str, workflow_id: str, step_id: str,
                           pattern_type: str, pattern_data: Dict[str, Any]):
        """Записать обнаруженный паттерн зацикливания"""
        
        current_time = datetime.now()
        
        if pattern_key in self.detected_patterns:
            # Обновляем существующий паттерн
            pattern = self.detected_patterns[pattern_key]
            pattern.occurrences += 1
            pattern.last_seen = current_time
            pattern.pattern_data.update(pattern_data)
        else:
            # Создаем новый паттерн
            pattern = LoopPattern(
                step_id=step_id,
                pattern_hash=pattern_key,
                occurrences=1,
                first_seen=current_time,
                last_seen=current_time,
                pattern_data={
                    "workflow_id": workflow_id,
                    "pattern_type": pattern_type,
                    **pattern_data
                }
            )
            self.detected_patterns[pattern_key] = pattern
        
        logger.warning(f"🔄 Loop pattern recorded: {pattern_type} for {workflow_id}:{step_id} "
                      f"(occurrence #{pattern.occurrences})")
    
    def is_step_in_loop(self, workflow_id: str, step_id: str) -> Tuple[bool, Optional[LoopPattern]]:
        """Проверить находится ли шаг в зацикливании"""
        
        # Ищем активные паттерны для этого шага
        for pattern_key, pattern in self.detected_patterns.items():
            if (f"{workflow_id}:{step_id}" in pattern_key and 
                pattern.is_loop(self.config["loop_threshold"])):
                
                # Проверяем что паттерн недавний
                time_since_last = datetime.now() - pattern.last_seen
                if time_since_last < timedelta(minutes=self.config["time_window_minutes"]):
                    return True, pattern
        
        return False, None
    
    def get_loop_prevention_suggestion(self, workflow_id: str, step_id: str) -> Optional[str]:
        """Получить предложение по предотвращению зацикливания"""
        
        is_loop, pattern = self.is_step_in_loop(workflow_id, step_id)
        
        if not is_loop or not pattern:
            return None
        
        pattern_type = pattern.pattern_data.get("pattern_type", "unknown")
        
        suggestions = {
            "identical_executions": "Обнаружены идентичные выполнения. Рекомендуется изменить подход к задаче или использовать альтернативный агент.",
            "error_retry_loop": "Обнаружено зацикливание в retry ошибок. Рекомендуется эскалировать к человеку или изменить стратегию retry.",
            "quality_stagnation": "Качество результатов не улучшается. Рекомендуется упростить задачу или использовать другой подход.",
            "decision_retry_loop": "Обнаружено зацикливание в решениях retry. Рекомендуется принудительно перейти к следующему шагу или остановить workflow."
        }
        
        return suggestions.get(pattern_type, "Обнаружено зацикливание. Рекомендуется вмешательство человека.")
    
    def break_loop(self, workflow_id: str, step_id: str) -> bool:
        """Принудительно прервать зацикливание"""
        
        # Удаляем паттерны зацикливания для этого шага
        patterns_to_remove = []
        for pattern_key, pattern in self.detected_patterns.items():
            if f"{workflow_id}:{step_id}" in pattern_key:
                patterns_to_remove.append(pattern_key)
        
        for pattern_key in patterns_to_remove:
            del self.detected_patterns[pattern_key]
        
        # Очищаем историю выполнения
        history_key = f"{workflow_id}:{step_id}"
        if history_key in self.execution_history:
            # Оставляем только последнюю запись
            self.execution_history[history_key] = self.execution_history[history_key][-1:]
        
        logger.info(f"🔧 Manually broke loop for workflow '{workflow_id}', step '{step_id}'")
        return True
    
    def get_loop_statistics(self) -> Dict[str, Any]:
        """Получить статистику зацикливаний"""
        
        active_loops = 0
        loop_types = Counter()
        
        current_time = datetime.now()
        
        for pattern in self.detected_patterns.values():
            if pattern.is_loop():
                # Проверяем активность (недавность)
                time_since_last = current_time - pattern.last_seen
                if time_since_last < timedelta(minutes=self.config["time_window_minutes"]):
                    active_loops += 1
                
                pattern_type = pattern.pattern_data.get("pattern_type", "unknown")
                loop_types[pattern_type] += 1
        
        return {
            "total_patterns": len(self.detected_patterns),
            "active_loops": active_loops,
            "loop_types": dict(loop_types),
            "detection_config": self.config,
            "history_entries": sum(len(h) for h in self.execution_history.values())
        }
    
    def cleanup_old_data(self, hours: int = 24):
        """Очистить старые данные"""
        
        cutoff_time = datetime.now() - timedelta(hours=hours)
        
        # Очищаем старые паттерны
        patterns_to_remove = []
        for pattern_key, pattern in self.detected_patterns.items():
            if pattern.last_seen < cutoff_time:
                patterns_to_remove.append(pattern_key)
        
        for pattern_key in patterns_to_remove:
            del self.detected_patterns[pattern_key]
        
        # Очищаем старую историю
        for history_key in list(self.execution_history.keys()):
            filtered_history = []
            for record in self.execution_history[history_key]:
                if record["timestamp"] >= cutoff_time:
                    filtered_history.append(record)
            
            if filtered_history:
                self.execution_history[history_key] = filtered_history
            else:
                del self.execution_history[history_key]
        
        logger.info(f"🧹 Cleaned loop detection data older than {hours} hours")
    
    def configure(self, config: Dict[str, Any]):
        """Обновить конфигурацию детектора"""
        self.config.update(config)
        logger.info(f"🔧 Updated loop detector configuration: {config}")
