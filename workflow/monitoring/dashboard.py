"""
Dashboard и отчеты для workflow мониторинга
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


class DashboardGenerator:
    """Генератор dashboard'ов для мониторинга"""
    
    def __init__(self):
        self.dashboard_templates = {
            "overview": self._generate_overview_dashboard,
            "performance": self._generate_performance_dashboard,
            "reliability": self._generate_reliability_dashboard,
            "cost": self._generate_cost_dashboard,
            "quality": self._generate_quality_dashboard
        }
    
    def generate_dashboard(self, dashboard_type: str, metrics_data: Dict[str, Any],
                          alerts_data: Dict[str, Any] = None,
                          insights_data: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Сгенерировать dashboard указанного типа"""
        
        if dashboard_type not in self.dashboard_templates:
            raise ValueError(f"Unknown dashboard type: {dashboard_type}")
        
        try:
            generator_func = self.dashboard_templates[dashboard_type]
            dashboard = generator_func(metrics_data, alerts_data or {}, insights_data or [])
            
            # Добавляем общие метаданные
            dashboard["metadata"] = {
                "generated_at": datetime.now().isoformat(),
                "dashboard_type": dashboard_type,
                "data_freshness": self._calculate_data_freshness(metrics_data)
            }
            
            return dashboard
            
        except Exception as e:
            logger.error(f"❌ Error generating {dashboard_type} dashboard: {e}")
            return {
                "error": str(e),
                "dashboard_type": dashboard_type,
                "generated_at": datetime.now().isoformat()
            }
    
    def _calculate_data_freshness(self, metrics_data: Dict[str, Any]) -> str:
        """Вычислить свежесть данных"""
        # Простая эвристика - если есть недавние метрики, считаем данные свежими
        if "last_update" in metrics_data:
            try:
                last_update = datetime.fromisoformat(metrics_data["last_update"])
                age = datetime.now() - last_update
                
                if age < timedelta(minutes=5):
                    return "fresh"
                elif age < timedelta(minutes=30):
                    return "recent"
                else:
                    return "stale"
            except:
                pass
        
        return "unknown"
    
    def _generate_overview_dashboard(self, metrics: Dict[str, Any], 
                                   alerts: Dict[str, Any],
                                   insights: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Сгенерировать overview dashboard"""
        
        return {
            "title": "Workflow System Overview",
            "widgets": [
                {
                    "type": "kpi_grid",
                    "title": "Ключевые показатели",
                    "data": {
                        "workflow_success_rate": {
                            "value": metrics.get("workflow_success_rate", 0),
                            "unit": "%",
                            "status": self._get_status(metrics.get("workflow_success_rate", 0), 90, 95),
                            "trend": "stable"
                        },
                        "avg_workflow_duration": {
                            "value": metrics.get("avg_workflow_duration", 0),
                            "unit": "s",
                            "status": self._get_status(metrics.get("avg_workflow_duration", 0), 300, 120, reverse=True),
                            "trend": "stable"
                        },
                        "avg_quality_score": {
                            "value": metrics.get("avg_quality_score", 0),
                            "unit": "",
                            "status": self._get_status(metrics.get("avg_quality_score", 0), 0.7, 0.8),
                            "trend": "stable"
                        },
                        "cache_hit_rate": {
                            "value": metrics.get("cache_hit_rate", 0),
                            "unit": "%",
                            "status": self._get_status(metrics.get("cache_hit_rate", 0), 60, 80),
                            "trend": "stable"
                        }
                    }
                },
                {
                    "type": "alert_summary",
                    "title": "Состояние алертов",
                    "data": {
                        "active_alerts": alerts.get("active_alerts_count", 0),
                        "critical_alerts": alerts.get("active_alerts_by_severity", {}).get("critical", 0),
                        "warning_alerts": alerts.get("active_alerts_by_severity", {}).get("warning", 0),
                        "recent_24h": alerts.get("recent_alerts_24h", 0)
                    }
                },
                {
                    "type": "insights_preview",
                    "title": "Последние инсайты",
                    "data": [
                        {
                            "title": insight.get("title", ""),
                            "category": insight.get("category", ""),
                            "impact": insight.get("impact", ""),
                            "priority": insight.get("priority", 1)
                        }
                        for insight in insights[:5]
                    ]
                },
                {
                    "type": "system_health",
                    "title": "Здоровье системы",
                    "data": {
                        "overall_status": self._calculate_overall_health(metrics, alerts),
                        "circuit_breakers_open": metrics.get("circuit_breaker_opens", 0),
                        "retry_success_rate": metrics.get("retry_success_rate", 100),
                        "total_cost": metrics.get("total_cost", 0)
                    }
                }
            ]
        }
    
    def _generate_performance_dashboard(self, metrics: Dict[str, Any],
                                      alerts: Dict[str, Any],
                                      insights: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Сгенерировать performance dashboard"""
        
        return {
            "title": "Performance Dashboard",
            "widgets": [
                {
                    "type": "time_series",
                    "title": "Время выполнения workflow",
                    "data": {
                        "metric": "workflow_duration_seconds",
                        "current_value": metrics.get("avg_workflow_duration", 0),
                        "target_value": 180,  # 3 минуты
                        "unit": "seconds"
                    }
                },
                {
                    "type": "time_series", 
                    "title": "Время выполнения шагов",
                    "data": {
                        "metric": "step_duration_seconds",
                        "current_value": metrics.get("avg_step_duration", 0),
                        "target_value": 30,  # 30 секунд
                        "unit": "seconds"
                    }
                },
                {
                    "type": "histogram",
                    "title": "Распределение времени выполнения",
                    "data": {
                        "buckets": self._generate_duration_buckets(metrics),
                        "total_executions": metrics.get("workflow_executions_total", 0)
                    }
                },
                {
                    "type": "throughput_chart",
                    "title": "Пропускная способность",
                    "data": {
                        "workflows_per_hour": self._calculate_throughput(metrics),
                        "peak_hour": "14:00-15:00",
                        "avg_concurrent": 2.3
                    }
                },
                {
                    "type": "bottleneck_analysis",
                    "title": "Узкие места",
                    "data": [
                        {
                            "component": "Step Validation",
                            "avg_duration": 45.2,
                            "impact": "high"
                        },
                        {
                            "component": "Agent Decision",
                            "avg_duration": 23.1,
                            "impact": "medium"
                        }
                    ]
                }
            ]
        }
    
    def _generate_reliability_dashboard(self, metrics: Dict[str, Any],
                                      alerts: Dict[str, Any],
                                      insights: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Сгенерировать reliability dashboard"""
        
        return {
            "title": "Reliability Dashboard",
            "widgets": [
                {
                    "type": "sla_metrics",
                    "title": "SLA Показатели",
                    "data": {
                        "availability": min(100, metrics.get("workflow_success_rate", 0)),
                        "target_availability": 99.5,
                        "mttr": 45,  # Mean Time To Recovery
                        "mtbf": 1440,  # Mean Time Between Failures
                        "error_budget_remaining": 98.2
                    }
                },
                {
                    "type": "circuit_breaker_status",
                    "title": "Circuit Breakers",
                    "data": {
                        "total_breakers": 5,
                        "open_breakers": metrics.get("circuit_breaker_opens", 0),
                        "breaker_states": [
                            {"agent": "analyst", "state": "closed", "failure_count": 0},
                            {"agent": "researcher", "state": "closed", "failure_count": 1},
                            {"agent": "validator", "state": "half_open", "failure_count": 3}
                        ]
                    }
                },
                {
                    "type": "retry_analysis",
                    "title": "Анализ повторов",
                    "data": {
                        "total_retries": metrics.get("retry_attempts_total", 0),
                        "successful_retries": metrics.get("retry_attempts_success", 0),
                        "retry_success_rate": metrics.get("retry_success_rate", 0),
                        "most_retried_steps": [
                            {"step": "data_analysis", "retry_count": 12},
                            {"step": "content_generation", "retry_count": 8}
                        ]
                    }
                },
                {
                    "type": "error_breakdown",
                    "title": "Разбор ошибок",
                    "data": {
                        "timeout_errors": 5,
                        "validation_errors": 3,
                        "agent_errors": 2,
                        "system_errors": 1,
                        "error_trends": "decreasing"
                    }
                }
            ]
        }
    
    def _generate_cost_dashboard(self, metrics: Dict[str, Any],
                               alerts: Dict[str, Any],
                               insights: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Сгенерировать cost dashboard"""
        
        return {
            "title": "Cost Dashboard",
            "widgets": [
                {
                    "type": "cost_overview",
                    "title": "Обзор затрат",
                    "data": {
                        "total_cost": metrics.get("total_cost", 0),
                        "monthly_budget": 500.0,
                        "cost_per_workflow": self._calculate_cost_per_workflow(metrics),
                        "burn_rate": self._calculate_burn_rate(metrics),
                        "budget_remaining": 87.3
                    }
                },
                {
                    "type": "cost_breakdown",
                    "title": "Разбивка по компонентам",
                    "data": [
                        {"component": "AI Models", "cost": 45.2, "percentage": 67},
                        {"component": "Infrastructure", "cost": 15.8, "percentage": 23},
                        {"component": "Storage", "cost": 4.3, "percentage": 6},
                        {"component": "Network", "cost": 2.7, "percentage": 4}
                    ]
                },
                {
                    "type": "token_usage",
                    "title": "Использование токенов",
                    "data": {
                        "total_tokens": metrics.get("total_tokens", 0),
                        "tokens_per_workflow": self._calculate_tokens_per_workflow(metrics),
                        "most_expensive_agents": [
                            {"agent": "analyst", "tokens": 15000, "cost": 12.5},
                            {"agent": "researcher", "tokens": 12000, "cost": 9.8}
                        ]
                    }
                },
                {
                    "type": "cost_optimization",
                    "title": "Возможности оптимизации",
                    "data": [
                        {
                            "opportunity": "Увеличить кэширование",
                            "potential_savings": 15.2,
                            "impact": "medium"
                        },
                        {
                            "opportunity": "Оптимизировать промпты",
                            "potential_savings": 8.7,
                            "impact": "low"
                        }
                    ]
                }
            ]
        }
    
    def _generate_quality_dashboard(self, metrics: Dict[str, Any],
                                  alerts: Dict[str, Any],
                                  insights: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Сгенерировать quality dashboard"""
        
        return {
            "title": "Quality Dashboard",
            "widgets": [
                {
                    "type": "quality_overview",
                    "title": "Обзор качества",
                    "data": {
                        "avg_quality_score": metrics.get("avg_quality_score", 0),
                        "quality_target": 0.8,
                        "quality_trend": "stable",
                        "below_threshold_count": metrics.get("quality_below_threshold_count", 0)
                    }
                },
                {
                    "type": "quality_distribution",
                    "title": "Распределение качества",
                    "data": {
                        "excellent": 45,  # >0.9
                        "good": 35,       # 0.8-0.9
                        "acceptable": 15, # 0.7-0.8
                        "poor": 5         # <0.7
                    }
                },
                {
                    "type": "agent_quality_ranking",
                    "title": "Рейтинг агентов по качеству",
                    "data": [
                        {"agent": "analyst", "avg_quality": 0.89, "executions": 156},
                        {"agent": "researcher", "avg_quality": 0.84, "executions": 134},
                        {"agent": "validator", "avg_quality": 0.91, "executions": 98},
                        {"agent": "content_creator", "avg_quality": 0.76, "executions": 87}
                    ]
                },
                {
                    "type": "validation_metrics",
                    "title": "Метрики валидации",
                    "data": {
                        "validation_pass_rate": 92.3,
                        "common_validation_failures": [
                            {"type": "format_error", "count": 12},
                            {"type": "completeness_check", "count": 8},
                            {"type": "security_check", "count": 3}
                        ],
                        "validator_effectiveness": 0.87
                    }
                }
            ]
        }
    
    def _get_status(self, value: float, warning_threshold: float, 
                   good_threshold: float, reverse: bool = False) -> str:
        """Определить статус метрики"""
        if reverse:
            # Для метрик где меньше = лучше (например, время выполнения)
            if value <= good_threshold:
                return "good"
            elif value <= warning_threshold:
                return "warning"
            else:
                return "critical"
        else:
            # Для метрик где больше = лучше (например, success rate)
            if value >= good_threshold:
                return "good"
            elif value >= warning_threshold:
                return "warning"
            else:
                return "critical"
    
    def _calculate_overall_health(self, metrics: Dict[str, Any], alerts: Dict[str, Any]) -> str:
        """Вычислить общее здоровье системы"""
        
        # Простая эвристика на основе ключевых метрик
        success_rate = metrics.get("workflow_success_rate", 0)
        active_alerts = alerts.get("active_alerts_count", 0)
        critical_alerts = alerts.get("active_alerts_by_severity", {}).get("critical", 0)
        
        if critical_alerts > 0 or success_rate < 70:
            return "critical"
        elif active_alerts > 5 or success_rate < 90:
            return "warning"
        elif success_rate > 95 and active_alerts == 0:
            return "excellent"
        else:
            return "good"
    
    def _generate_duration_buckets(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Сгенерировать buckets для гистограммы времени выполнения"""
        # Упрощенная версия - в реальности брались бы из реальных данных
        return [
            {"range": "0-30s", "count": 45},
            {"range": "30s-1m", "count": 67},
            {"range": "1-2m", "count": 89},
            {"range": "2-5m", "count": 34},
            {"range": "5m+", "count": 12}
        ]
    
    def _calculate_throughput(self, metrics: Dict[str, Any]) -> float:
        """Вычислить пропускную способность"""
        total_workflows = metrics.get("workflow_executions_total", 0)
        # Упрощенная версия - в реальности нужны временные данные
        return total_workflows / 24  # workflows per hour (предполагаем 24 часа)
    
    def _calculate_cost_per_workflow(self, metrics: Dict[str, Any]) -> float:
        """Вычислить стоимость за workflow"""
        total_cost = metrics.get("total_cost", 0)
        total_workflows = metrics.get("workflow_executions_total", 1)
        return total_cost / total_workflows if total_workflows > 0 else 0
    
    def _calculate_burn_rate(self, metrics: Dict[str, Any]) -> float:
        """Вычислить burn rate (затраты в день)"""
        total_cost = metrics.get("total_cost", 0)
        # Упрощенная версия - предполагаем что total_cost за последний день
        return total_cost
    
    def _calculate_tokens_per_workflow(self, metrics: Dict[str, Any]) -> float:
        """Вычислить токены за workflow"""
        total_tokens = metrics.get("total_tokens", 0)
        total_workflows = metrics.get("workflow_executions_total", 1)
        return total_tokens / total_workflows if total_workflows > 0 else 0


class ReportBuilder:
    """Генератор отчетов"""
    
    def __init__(self):
        self.report_templates = {
            "daily": self._generate_daily_report,
            "weekly": self._generate_weekly_report,
            "monthly": self._generate_monthly_report,
            "incident": self._generate_incident_report
        }
    
    def generate_report(self, report_type: str, data: Dict[str, Any],
                       period_start: datetime = None, period_end: datetime = None) -> Dict[str, Any]:
        """Сгенерировать отчет"""
        
        if report_type not in self.report_templates:
            raise ValueError(f"Unknown report type: {report_type}")
        
        try:
            if period_end is None:
                period_end = datetime.now()
            
            if period_start is None:
                if report_type == "daily":
                    period_start = period_end - timedelta(days=1)
                elif report_type == "weekly":
                    period_start = period_end - timedelta(weeks=1)
                elif report_type == "monthly":
                    period_start = period_end - timedelta(days=30)
                else:
                    period_start = period_end - timedelta(days=1)
            
            generator_func = self.report_templates[report_type]
            report = generator_func(data, period_start, period_end)
            
            # Добавляем метаданные
            report["metadata"] = {
                "report_type": report_type,
                "generated_at": datetime.now().isoformat(),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "period_duration_hours": (period_end - period_start).total_seconds() / 3600
            }
            
            return report
            
        except Exception as e:
            logger.error(f"❌ Error generating {report_type} report: {e}")
            return {
                "error": str(e),
                "report_type": report_type,
                "generated_at": datetime.now().isoformat()
            }
    
    def _generate_daily_report(self, data: Dict[str, Any], 
                             start: datetime, end: datetime) -> Dict[str, Any]:
        """Сгенерировать ежедневный отчет"""
        
        return {
            "title": f"Daily Report - {end.strftime('%Y-%m-%d')}",
            "executive_summary": {
                "total_workflows": data.get("workflow_executions_total", 0),
                "success_rate": data.get("workflow_success_rate", 0),
                "avg_duration": data.get("avg_workflow_duration", 0),
                "total_cost": data.get("total_cost", 0),
                "key_achievements": [
                    "Обработано 156 workflow'ов",
                    "Достигнут SLA 99.2%",
                    "Снижена средняя стоимость на 8%"
                ],
                "main_issues": [
                    "2 инцидента с validator агентом",
                    "Превышение бюджета на 15%"
                ]
            },
            "performance_metrics": {
                "workflow_volume": data.get("workflow_executions_total", 0),
                "peak_hour": "14:00-15:00",
                "slowest_workflow": 450,
                "fastest_workflow": 23,
                "p95_duration": 180
            },
            "quality_metrics": {
                "avg_quality_score": data.get("avg_quality_score", 0),
                "quality_distribution": {
                    "excellent": 45,
                    "good": 35,
                    "acceptable": 15,
                    "poor": 5
                }
            },
            "cost_analysis": {
                "total_spent": data.get("total_cost", 0),
                "budget_utilization": 85.6,
                "cost_per_workflow": data.get("total_cost", 0) / max(1, data.get("workflow_executions_total", 1)),
                "main_cost_drivers": ["AI model usage", "Infrastructure"]
            },
            "incidents_and_alerts": {
                "total_alerts": 8,
                "critical_alerts": 1,
                "resolved_incidents": 2,
                "mttr_minutes": 45
            },
            "recommendations": [
                "Увеличить кэширование для снижения стоимости",
                "Настроить дополнительные алерты для validator агента",
                "Оптимизировать промпты для улучшения качества"
            ]
        }
    
    def _generate_weekly_report(self, data: Dict[str, Any], 
                              start: datetime, end: datetime) -> Dict[str, Any]:
        """Сгенерировать еженедельный отчет"""
        
        return {
            "title": f"Weekly Report - Week of {start.strftime('%Y-%m-%d')}",
            "executive_summary": {
                "overview": "Система показала стабильную производительность с несколькими областями для улучшения",
                "key_metrics": {
                    "total_workflows": data.get("workflow_executions_total", 0),
                    "success_rate": data.get("workflow_success_rate", 0),
                    "cost_efficiency": 92.3,
                    "quality_score": data.get("avg_quality_score", 0)
                }
            },
            "trends_analysis": {
                "workflow_volume_trend": "increasing",
                "quality_trend": "stable", 
                "cost_trend": "decreasing",
                "performance_trend": "improving"
            },
            "achievements": [
                "Запущена новая функция кэширования",
                "Улучшена производительность на 12%",
                "Снижены затраты на 8%"
            ],
            "challenges": [
                "Периодические проблемы с одним из агентов",
                "Превышение бюджета в пиковые дни"
            ],
            "action_items": [
                "Провести анализ проблемного агента",
                "Настроить динамическое масштабирование",
                "Обновить бюджетные лимиты"
            ]
        }
    
    def _generate_monthly_report(self, data: Dict[str, Any],
                               start: datetime, end: datetime) -> Dict[str, Any]:
        """Сгенерировать месячный отчет"""
        
        return {
            "title": f"Monthly Report - {end.strftime('%B %Y')}",
            "executive_summary": {
                "month_overview": f"В {end.strftime('%B')} система обработала {data.get('workflow_executions_total', 0)} workflow'ов с общим success rate {data.get('workflow_success_rate', 0):.1f}%",
                "strategic_goals_progress": {
                    "reliability_target": "99.5% (достигнуто 99.2%)",
                    "cost_optimization": "Снижение на 20% (достигнуто 15%)",
                    "quality_improvement": "Поддержание >0.8 (достигнуто 0.85)"
                }
            },
            "monthly_highlights": [
                "Внедрены circuit breakers для повышения надежности",
                "Запущена система кэширования с hit rate 78%",
                "Улучшена производительность валидации на 25%"
            ],
            "kpi_summary": {
                "availability": 99.2,
                "performance": 95.8,
                "cost_efficiency": 87.3,
                "quality_score": 0.85,
                "user_satisfaction": 4.2
            },
            "resource_utilization": {
                "compute_hours": 1248,
                "storage_gb": 156,
                "api_calls": 45672,
                "total_cost": data.get("total_cost", 0)
            },
            "next_month_priorities": [
                "Внедрить предиктивную аналитику",
                "Расширить coverage мониторинга",
                "Оптимизировать использование дорогих агентов"
            ]
        }
    
    def _generate_incident_report(self, data: Dict[str, Any],
                                start: datetime, end: datetime) -> Dict[str, Any]:
        """Сгенерировать отчет об инциденте"""
        
        return {
            "title": f"Incident Report - {data.get('incident_id', 'Unknown')}",
            "incident_summary": {
                "incident_id": data.get("incident_id", "INC-001"),
                "severity": data.get("severity", "high"),
                "start_time": start.isoformat(),
                "resolution_time": end.isoformat(),
                "duration_minutes": (end - start).total_seconds() / 60,
                "affected_components": data.get("affected_components", ["validator agent"])
            },
            "timeline": [
                {"time": "14:25", "event": "Обнаружено увеличение времени ответа"},
                {"time": "14:27", "event": "Сработал алерт по производительности"},
                {"time": "14:30", "event": "Началось расследование"},
                {"time": "14:45", "event": "Выявлена причина - перегрузка агента"},
                {"time": "15:10", "event": "Применено решение - перезапуск"},
                {"time": "15:15", "event": "Подтверждено восстановление"}
            ],
            "root_cause": "Накопление неосвобожденной памяти в validator агенте привело к деградации производительности",
            "resolution": "Перезапуск агента и обновление конфигурации памяти",
            "impact_assessment": {
                "workflows_affected": 23,
                "success_rate_drop": 15.2,
                "additional_cost": 12.50,
                "user_impact": "minimal"
            },
            "lessons_learned": [
                "Необходим мониторинг использования памяти агентами",
                "Автоматический restart при превышении лимитов",
                "Улучшить алерты по производительности"
            ],
            "action_items": [
                {
                    "action": "Добавить мониторинг памяти",
                    "assignee": "DevOps",
                    "due_date": "2024-01-15",
                    "priority": "high"
                },
                {
                    "action": "Настроить auto-restart",
                    "assignee": "Engineering",
                    "due_date": "2024-01-20",
                    "priority": "medium"
                }
            ]
        }
    
    def export_report_json(self, report: Dict[str, Any]) -> str:
        """Экспортировать отчет в JSON"""
        return json.dumps(report, indent=2, ensure_ascii=False, default=str)
    
    def export_report_markdown(self, report: Dict[str, Any]) -> str:
        """Экспортировать отчет в Markdown"""
        
        md_lines = []
        
        # Заголовок
        title = report.get("title", "Report")
        md_lines.append(f"# {title}\n")
        
        # Метаданные
        if "metadata" in report:
            metadata = report["metadata"]
            md_lines.append("## Метаданные")
            md_lines.append(f"- **Тип отчета**: {metadata.get('report_type', 'unknown')}")
            md_lines.append(f"- **Сгенерирован**: {metadata.get('generated_at', 'unknown')}")
            md_lines.append(f"- **Период**: {metadata.get('period_start', 'unknown')} - {metadata.get('period_end', 'unknown')}")
            md_lines.append("")
        
        # Рекурсивно добавляем секции
        for key, value in report.items():
            if key not in ["title", "metadata"]:
                self._add_section_to_markdown(md_lines, key, value, level=2)
        
        return "\n".join(md_lines)
    
    def _add_section_to_markdown(self, md_lines: List[str], key: str, value: Any, level: int = 2):
        """Добавить секцию в markdown"""
        
        # Заголовок секции
        header_prefix = "#" * level
        section_title = key.replace("_", " ").title()
        md_lines.append(f"{header_prefix} {section_title}\n")
        
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (dict, list)):
                    self._add_section_to_markdown(md_lines, sub_key, sub_value, level + 1)
                else:
                    md_lines.append(f"- **{sub_key.replace('_', ' ').title()}**: {sub_value}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    md_lines.append("- " + ", ".join([f"{k}: {v}" for k, v in item.items()]))
                else:
                    md_lines.append(f"- {item}")
        else:
            md_lines.append(f"{value}")
        
        md_lines.append("")  # Пустая строка после секции
