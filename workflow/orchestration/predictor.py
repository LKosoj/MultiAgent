"""
ML-powered предикторы для оптимизации workflow
"""
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import json
import math

logger = logging.getLogger(__name__)


class PredictionConfidence(Enum):
    """Уровни уверенности в предсказаниях"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class QualityPrediction:
    """Предсказание качества результата"""
    predicted_score: float
    confidence: PredictionConfidence
    factors: Dict[str, float]
    reasoning: str


@dataclass
class PerformancePrediction:
    """Предсказание производительности"""
    estimated_duration: float
    estimated_cost: float
    estimated_tokens: int
    confidence: PredictionConfidence
    bottlenecks: List[str]


class QualityPredictor:
    """Предиктор качества результатов на основе исторических данных"""
    
    def __init__(self):
        self.historical_data: List[Dict[str, Any]] = []
        self.agent_quality_profiles: Dict[str, Dict[str, float]] = {}
        
    def predict_quality(self, step_id: str, agent_type: str, task: str,
                       context: Dict[str, Any]) -> QualityPrediction:
        """Предсказать качество результата для задачи"""
        
        try:
            # Простая эвристическая модель
            base_score = 0.7
            agent_quality = self._get_agent_quality(agent_type)
            task_complexity = self._calculate_task_complexity(task)
            
            predicted_score = base_score * agent_quality * (1.0 - task_complexity * 0.3)
            predicted_score = max(0.0, min(1.0, predicted_score))
            
            confidence = PredictionConfidence.MEDIUM
            factors = {
                "agent_quality": agent_quality,
                "task_complexity": task_complexity,
                "base_score": base_score
            }
            
            reasoning = f"Predicted score {predicted_score:.2f} based on agent quality and task complexity"
            
            return QualityPrediction(
                predicted_score=predicted_score,
                confidence=confidence,
                factors=factors,
                reasoning=reasoning
            )
            
        except Exception as e:
            logger.error(f"❌ Quality prediction failed: {e}")
            return QualityPrediction(
                predicted_score=0.5,
                confidence=PredictionConfidence.LOW,
                factors={"unknown": 1.0},
                reasoning="Prediction failed, using default values"
            )
    
    def _calculate_task_complexity(self, task: str) -> float:
        """Вычислить сложность задачи"""
        length_complexity = min(1.0, len(task) / 1000.0)
        
        complexity_keywords = ["analyze", "research", "complex", "detailed"]
        keyword_complexity = 0.0
        
        task_lower = task.lower()
        for keyword in complexity_keywords:
            if keyword in task_lower:
                keyword_complexity = 0.7
                break
        
        return (length_complexity * 0.3 + keyword_complexity * 0.7)
    
    def _get_agent_quality(self, agent_type: str) -> float:
        """Получить историческое качество агента"""
        if agent_type in self.agent_quality_profiles:
            return self.agent_quality_profiles[agent_type].get("avg_quality", 0.7)
        return 0.7


class PerformanceOptimizer:
    """Оптимизатор производительности workflow"""
    
    def __init__(self):
        self.performance_history: Dict[str, List[Dict[str, Any]]] = {}
        
    def predict_performance(self, workflow_definition: Dict[str, Any],
                           context: Dict[str, Any]) -> PerformancePrediction:
        """Предсказать производительность workflow"""
        
        try:
            steps = workflow_definition.get("steps", [])
            
            # Простые оценки
            estimated_duration = len(steps) * 30.0  # 30 сек на шаг
            estimated_cost = len(steps) * 0.1       # $0.10 на шаг
            estimated_tokens = len(steps) * 100     # 100 токенов на шаг
            
            bottlenecks = []
            if len(steps) > 10:
                bottlenecks.append("Large number of steps may cause delays")
            
            return PerformancePrediction(
                estimated_duration=estimated_duration,
                estimated_cost=estimated_cost,
                estimated_tokens=estimated_tokens,
                confidence=PredictionConfidence.MEDIUM,
                bottlenecks=bottlenecks
            )
            
        except Exception as e:
            logger.error(f"❌ Performance prediction failed: {e}")
            return PerformancePrediction(
                estimated_duration=300.0,
                estimated_cost=1.0,
                estimated_tokens=1000,
                confidence=PredictionConfidence.LOW,
                bottlenecks=["Prediction failed"]
            )
    
    def suggest_optimizations(self, workflow_definition: Dict[str, Any]) -> List[str]:
        """Предложить оптимизации для workflow"""
        suggestions = []
        steps = workflow_definition.get("steps", [])
        
        if len(steps) > 5:
            suggestions.append("Consider parallelizing independent steps")
        
        return suggestions
