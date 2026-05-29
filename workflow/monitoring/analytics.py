"""
Аналитика и анализ трендов для workflow мониторинга
"""
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import statistics
import math

logger = logging.getLogger(__name__)


class TrendDirection(Enum):
    """Направления трендов"""
    INCREASING = "increasing"
    DECREASING = "decreasing"
    STABLE = "stable"
    VOLATILE = "volatile"


@dataclass
class TrendAnalysis:
    """Результат анализа тренда"""
    metric_name: str
    direction: TrendDirection
    slope: float  # Наклон тренда
    confidence: float  # Уверенность в анализе (0-1)
    change_percentage: float  # Изменение в процентах
    significance: str  # "low", "medium", "high"
    description: str
    
    def is_significant(self) -> bool:
        """Является ли тренд значимым"""
        return self.significance in ["medium", "high"] and self.confidence > 0.6


@dataclass
class PerformanceInsight:
    """Инсайт о производительности"""
    category: str
    title: str
    description: str
    impact: str  # "positive", "negative", "neutral"
    priority: int  # 1-5, где 5 - наивысший
    recommendations: List[str]
    data_points: Dict[str, Any]


class TrendAnalyzer:
    """Анализатор трендов метрик"""
    
    def __init__(self):
        self.analysis_history: Dict[str, List[TrendAnalysis]] = {}
    
    def analyze_trend(self, values: List[float], timestamps: List[datetime],
                     metric_name: str, window_size: int = 10) -> TrendAnalysis:
        """Анализировать тренд метрики"""
        
        if len(values) < 3:
            return TrendAnalysis(
                metric_name=metric_name,
                direction=TrendDirection.STABLE,
                slope=0.0,
                confidence=0.0,
                change_percentage=0.0,
                significance="low",
                description="Недостаточно данных для анализа тренда"
            )
        
        try:
            # Используем скользящее окно если данных много
            if len(values) > window_size:
                values = values[-window_size:]
                timestamps = timestamps[-window_size:]
            
            # Вычисляем линейную регрессию
            slope, confidence = self._calculate_linear_regression(values, timestamps)
            
            # Определяем направление тренда
            direction = self._determine_direction(slope, values)
            
            # Вычисляем изменение в процентах
            if len(values) >= 2 and values[0] != 0:
                change_percentage = ((values[-1] - values[0]) / abs(values[0])) * 100
            else:
                change_percentage = 0.0
            
            # Определяем значимость
            significance = self._determine_significance(slope, confidence, change_percentage)
            
            # Генерируем описание
            description = self._generate_description(direction, change_percentage, significance)
            
            analysis = TrendAnalysis(
                metric_name=metric_name,
                direction=direction,
                slope=slope,
                confidence=confidence,
                change_percentage=change_percentage,
                significance=significance,
                description=description
            )
            
            # Сохраняем в историю
            if metric_name not in self.analysis_history:
                self.analysis_history[metric_name] = []
            
            self.analysis_history[metric_name].append(analysis)
            
            # Ограничиваем историю
            if len(self.analysis_history[metric_name]) > 100:
                self.analysis_history[metric_name] = self.analysis_history[metric_name][-50:]
            
            return analysis
            
        except Exception as e:
            logger.error(f"❌ Error analyzing trend for {metric_name}: {e}")
            return TrendAnalysis(
                metric_name=metric_name,
                direction=TrendDirection.STABLE,
                slope=0.0,
                confidence=0.0,
                change_percentage=0.0,
                significance="low",
                description=f"Ошибка анализа: {e}"
            )
    
    def _calculate_linear_regression(self, values: List[float], 
                                   timestamps: List[datetime]) -> Tuple[float, float]:
        """Вычислить линейную регрессию"""
        
        n = len(values)
        if n < 2:
            return 0.0, 0.0
        
        # Преобразуем timestamps в числовые значения (секунды от первого)
        base_time = timestamps[0]
        x_values = [(ts - base_time).total_seconds() for ts in timestamps]
        y_values = values
        
        # Вычисляем коэффициенты линейной регрессии
        x_mean = statistics.mean(x_values)
        y_mean = statistics.mean(y_values)
        
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
        denominator = sum((x - x_mean) ** 2 for x in x_values)
        
        if denominator == 0:
            return 0.0, 0.0
        
        slope = numerator / denominator
        
        # Вычисляем коэффициент детерминации (R²) как меру уверенности
        y_pred = [slope * (x - x_mean) + y_mean for x in x_values]
        ss_res = sum((y - y_pred) ** 2 for y, y_pred in zip(y_values, y_pred))
        ss_tot = sum((y - y_mean) ** 2 for y in y_values)
        
        if ss_tot == 0:
            confidence = 1.0 if slope == 0 else 0.0
        else:
            r_squared = 1 - (ss_res / ss_tot)
            confidence = max(0.0, min(1.0, r_squared))
        
        return slope, confidence
    
    def _determine_direction(self, slope: float, values: List[float]) -> TrendDirection:
        """Определить направление тренда"""
        
        # Вычисляем волатильность
        if len(values) > 1:
            mean_value = statistics.mean(values)
            volatility = statistics.stdev(values) / mean_value if mean_value != 0 else 0
        else:
            volatility = 0
        
        # Если высокая волатильность, тренд считается неустойчивым
        if volatility > 0.5:  # 50% стандартное отклонение
            return TrendDirection.VOLATILE
        
        # Определяем направление по наклону
        if abs(slope) < 1e-6:  # Очень маленький наклон
            return TrendDirection.STABLE
        elif slope > 0:
            return TrendDirection.INCREASING
        else:
            return TrendDirection.DECREASING
    
    def _determine_significance(self, slope: float, confidence: float, 
                              change_percentage: float) -> str:
        """Определить значимость тренда"""
        
        # Комбинируем факторы для определения значимости
        abs_change = abs(change_percentage)
        
        if confidence > 0.8 and abs_change > 20:
            return "high"
        elif confidence > 0.6 and abs_change > 10:
            return "medium"
        elif confidence > 0.4 and abs_change > 5:
            return "low"
        else:
            return "low"
    
    def _generate_description(self, direction: TrendDirection, 
                            change_percentage: float, significance: str) -> str:
        """Сгенерировать описание тренда"""
        
        direction_desc = {
            TrendDirection.INCREASING: "растет",
            TrendDirection.DECREASING: "снижается", 
            TrendDirection.STABLE: "стабильна",
            TrendDirection.VOLATILE: "нестабильна"
        }
        
        significance_desc = {
            "high": "значительно",
            "medium": "умеренно",
            "low": "слегка"
        }
        
        base_desc = f"Метрика {significance_desc.get(significance, '')} {direction_desc.get(direction, '')}"
        
        if abs(change_percentage) > 1:
            base_desc += f" ({change_percentage:+.1f}%)"
        
        return base_desc
    
    def get_trend_summary(self, metric_name: str, days: int = 7) -> Dict[str, Any]:
        """Получить сводку трендов для метрики"""
        
        if metric_name not in self.analysis_history:
            return {"error": f"No trend history for metric {metric_name}"}
        
        cutoff_time = datetime.now() - timedelta(days=days)
        
        # Фильтруем анализы по времени (если у нас есть временные метки)
        recent_analyses = self.analysis_history[metric_name][-50:]  # Последние 50 анализов
        
        if not recent_analyses:
            return {"error": "No recent trend analyses"}
        
        # Агрегируем статистику
        directions = [a.direction.value for a in recent_analyses]
        confidences = [a.confidence for a in recent_analyses]
        changes = [a.change_percentage for a in recent_analyses]
        
        # Подсчитываем частоту направлений
        direction_counts = {}
        for direction in directions:
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
        
        # Находим доминирующее направление
        dominant_direction = max(direction_counts.keys(), key=lambda k: direction_counts[k])
        
        return {
            "metric_name": metric_name,
            "analysis_count": len(recent_analyses),
            "dominant_direction": dominant_direction,
            "direction_distribution": direction_counts,
            "average_confidence": statistics.mean(confidences),
            "average_change_percentage": statistics.mean(changes),
            "latest_analysis": recent_analyses[-1].__dict__ if recent_analyses else None
        }


class AnalyticsEngine:
    """Основной движок аналитики"""
    
    def __init__(self):
        self.trend_analyzer = TrendAnalyzer()
        self.insights_history: List[PerformanceInsight] = []
        
    def analyze_workflow_performance(self, metrics_data: Dict[str, Any]) -> List[PerformanceInsight]:
        """Анализировать производительность workflow"""
        
        insights = []
        
        try:
            # Анализ success rate
            success_rate = metrics_data.get("workflow_success_rate", 100)
            if success_rate < 90:
                priority = 5 if success_rate < 70 else 3
                insights.append(PerformanceInsight(
                    category="reliability",
                    title="Низкий процент успешных выполнений",
                    description=f"Success rate составляет {success_rate:.1f}%, что ниже нормы",
                    impact="negative",
                    priority=priority,
                    recommendations=[
                        "Проанализировать логи ошибок",
                        "Проверить состояние агентов",
                        "Рассмотреть увеличение timeout'ов"
                    ],
                    data_points={"success_rate": success_rate}
                ))
            
            # Анализ времени выполнения
            avg_duration = metrics_data.get("avg_workflow_duration", 0)
            if avg_duration > 300:  # Более 5 минут
                insights.append(PerformanceInsight(
                    category="performance",
                    title="Высокое время выполнения workflow",
                    description=f"Среднее время выполнения {avg_duration:.1f}s превышает норму",
                    impact="negative",
                    priority=3,
                    recommendations=[
                        "Оптимизировать медленные шаги",
                        "Рассмотреть параллельное выполнение",
                        "Включить кэширование результатов"
                    ],
                    data_points={"avg_duration": avg_duration}
                ))
            
            # Анализ качества
            avg_quality = metrics_data.get("avg_quality_score", 1.0)
            if avg_quality < 0.7:
                insights.append(PerformanceInsight(
                    category="quality",
                    title="Снижение качества результатов",
                    description=f"Средний скор качества {avg_quality:.2f} ниже приемлемого",
                    impact="negative",
                    priority=4,
                    recommendations=[
                        "Улучшить промпты для агентов",
                        "Настроить более строгую валидацию",
                        "Рассмотреть использование более мощных агентов"
                    ],
                    data_points={"avg_quality": avg_quality}
                ))
            
            # Анализ стоимости
            total_cost = metrics_data.get("total_cost", 0)
            if total_cost > 100:  # Более $100
                insights.append(PerformanceInsight(
                    category="cost",
                    title="Высокие затраты на выполнение",
                    description=f"Общие затраты ${total_cost:.2f} превышают бюджет",
                    impact="negative",
                    priority=3,
                    recommendations=[
                        "Оптимизировать использование дорогих агентов",
                        "Включить агрессивное кэширование",
                        "Установить лимиты на затраты"
                    ],
                    data_points={"total_cost": total_cost}
                ))
            
            # Анализ cache hit rate
            cache_hit_rate = metrics_data.get("cache_hit_rate", 0)
            if cache_hit_rate > 0 and cache_hit_rate < 50:
                insights.append(PerformanceInsight(
                    category="optimization",
                    title="Низкий hit rate кэша",
                    description=f"Hit rate кэша {cache_hit_rate:.1f}% можно улучшить",
                    impact="neutral",
                    priority=2,
                    recommendations=[
                        "Увеличить размер кэша",
                        "Оптимизировать TTL параметры",
                        "Улучшить ключи кэширования"
                    ],
                    data_points={"cache_hit_rate": cache_hit_rate}
                ))
            
            # Позитивные инсайты
            if success_rate > 95:
                insights.append(PerformanceInsight(
                    category="reliability",
                    title="Отличная надежность",
                    description=f"Success rate {success_rate:.1f}% превосходит ожидания",
                    impact="positive",
                    priority=1,
                    recommendations=["Поддерживать текущую конфигурацию"],
                    data_points={"success_rate": success_rate}
                ))
            
            if cache_hit_rate > 80:
                insights.append(PerformanceInsight(
                    category="optimization",
                    title="Эффективное кэширование",
                    description=f"Hit rate кэша {cache_hit_rate:.1f}% обеспечивает хорошую производительность",
                    impact="positive",
                    priority=1,
                    recommendations=["Распространить настройки кэша на другие компоненты"],
                    data_points={"cache_hit_rate": cache_hit_rate}
                ))
            
        except Exception as e:
            logger.error(f"❌ Error analyzing workflow performance: {e}")
            insights.append(PerformanceInsight(
                category="system",
                title="Ошибка анализа",
                description=f"Не удалось провести полный анализ: {e}",
                impact="neutral",
                priority=2,
                recommendations=["Проверить состояние системы мониторинга"],
                data_points={}
            ))
        
        # Сохраняем в историю
        self.insights_history.extend(insights)
        
        # Ограничиваем историю
        if len(self.insights_history) > 1000:
            self.insights_history = self.insights_history[-500:]
        
        return insights
    
    def detect_anomalies(self, metric_values: List[float], metric_name: str) -> List[Dict[str, Any]]:
        """Обнаружить аномалии в метриках"""
        
        anomalies = []
        
        if len(metric_values) < 10:
            return anomalies
        
        try:
            # Вычисляем статистики
            mean_value = statistics.mean(metric_values)
            std_dev = statistics.stdev(metric_values)
            
            # Z-score анализ
            threshold = 2.5  # 2.5 стандартных отклонения
            
            for i, value in enumerate(metric_values):
                if std_dev > 0:
                    z_score = abs(value - mean_value) / std_dev
                    
                    if z_score > threshold:
                        anomaly_type = "spike" if value > mean_value else "drop"
                        
                        anomalies.append({
                            "index": i,
                            "value": value,
                            "z_score": z_score,
                            "type": anomaly_type,
                            "severity": "high" if z_score > 3 else "medium",
                            "description": f"{anomaly_type.title()} в метрике {metric_name}: {value:.2f} (z-score: {z_score:.2f})"
                        })
            
            # Детекция последовательных выбросов
            consecutive_threshold = 3
            consecutive_count = 0
            
            for i in range(1, len(metric_values)):
                change = abs(metric_values[i] - metric_values[i-1])
                if change > std_dev * 1.5:  # Значительное изменение
                    consecutive_count += 1
                else:
                    consecutive_count = 0
                
                if consecutive_count >= consecutive_threshold:
                    anomalies.append({
                        "index": i,
                        "value": metric_values[i],
                        "type": "trend_change",
                        "severity": "medium",
                        "description": f"Резкое изменение тренда в метрике {metric_name}"
                    })
                    consecutive_count = 0  # Сбрасываем счетчик
            
        except Exception as e:
            logger.error(f"❌ Error detecting anomalies in {metric_name}: {e}")
        
        return anomalies
    
    def generate_recommendations(self, insights: List[PerformanceInsight]) -> List[str]:
        """Сгенерировать общие рекомендации на основе инсайтов"""
        
        recommendations = []
        
        # Группируем инсайты по категориям и приоритету
        high_priority_issues = [i for i in insights if i.priority >= 4 and i.impact == "negative"]
        reliability_issues = [i for i in insights if i.category == "reliability" and i.impact == "negative"]
        performance_issues = [i for i in insights if i.category == "performance" and i.impact == "negative"]
        cost_issues = [i for i in insights if i.category == "cost" and i.impact == "negative"]
        
        # Критические проблемы
        if high_priority_issues:
            recommendations.append("🔥 КРИТИЧНО: Немедленно устраните проблемы с высоким приоритетом")
            for issue in high_priority_issues[:3]:  # Топ-3
                recommendations.extend(issue.recommendations[:2])  # Первые 2 рекомендации
        
        # Надежность
        if reliability_issues:
            recommendations.append("🛡️ НАДЕЖНОСТЬ: Улучшите стабильность системы")
            recommendations.append("- Настройте мониторинг и алерты")
            recommendations.append("- Реализуйте graceful degradation")
        
        # Производительность
        if performance_issues:
            recommendations.append("⚡ ПРОИЗВОДИТЕЛЬНОСТЬ: Оптимизируйте скорость выполнения")
            recommendations.append("- Включите параллельное выполнение")
            recommendations.append("- Оптимизируйте кэширование")
        
        # Стоимость
        if cost_issues:
            recommendations.append("💰 СТОИМОСТЬ: Снизьте затраты на выполнение")
            recommendations.append("- Установите бюджетные лимиты")
            recommendations.append("- Оптимизируйте использование дорогих ресурсов")
        
        # Общие рекомендации
        if not any([high_priority_issues, reliability_issues, performance_issues, cost_issues]):
            recommendations.append("✅ Система работает стабильно")
            recommendations.append("- Продолжайте мониторинг ключевых метрик")
            recommendations.append("- Рассмотрите дальнейшие оптимизации")
        
        return recommendations[:10]  # Ограничиваем количество рекомендаций
    
    def get_analytics_summary(self) -> Dict[str, Any]:
        """Получить сводку аналитики"""
        
        recent_insights = self.insights_history[-50:] if self.insights_history else []
        
        # Группируем по категориям
        category_counts = {}
        impact_counts = {}
        priority_counts = {}
        
        for insight in recent_insights:
            category_counts[insight.category] = category_counts.get(insight.category, 0) + 1
            impact_counts[insight.impact] = impact_counts.get(insight.impact, 0) + 1
            priority_counts[f"priority_{insight.priority}"] = priority_counts.get(f"priority_{insight.priority}", 0) + 1
        
        return {
            "total_insights": len(self.insights_history),
            "recent_insights": len(recent_insights),
            "category_distribution": category_counts,
            "impact_distribution": impact_counts,
            "priority_distribution": priority_counts,
            "top_categories": sorted(category_counts.items(), key=lambda x: x[1], reverse=True)[:5],
            "trend_analyses_available": len(self.trend_analyzer.analysis_history)
        }
