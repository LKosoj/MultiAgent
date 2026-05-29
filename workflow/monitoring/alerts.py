"""
Система алертов для мониторинга workflow
"""
import logging
from typing import Dict, Any, List, Optional, Callable, Union
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import asyncio
import threading
import json

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    """Уровни серьезности алертов"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertState(Enum):
    """Состояния алертов"""
    FIRING = "firing"        # Алерт активен
    RESOLVED = "resolved"    # Алерт решен
    SUPPRESSED = "suppressed" # Алерт подавлен


class ConditionOperator(Enum):
    """Операторы для условий алертов"""
    GREATER_THAN = ">"
    LESS_THAN = "<"
    EQUALS = "=="
    NOT_EQUALS = "!="
    GREATER_EQUAL = ">="
    LESS_EQUAL = "<="


@dataclass
class AlertCondition:
    """Условие для срабатывания алерта"""
    metric_name: str
    operator: ConditionOperator
    threshold: float
    duration_minutes: int = 1  # Как долго условие должно выполняться
    labels: Dict[str, str] = field(default_factory=dict)
    
    def evaluate(self, current_value: float, historical_values: List[float]) -> bool:
        """Оценить выполняется ли условие"""
        
        if current_value is None:
            return False
        
        # Проверяем текущее значение
        condition_met = False
        
        if self.operator == ConditionOperator.GREATER_THAN:
            condition_met = current_value > self.threshold
        elif self.operator == ConditionOperator.LESS_THAN:
            condition_met = current_value < self.threshold
        elif self.operator == ConditionOperator.EQUALS:
            condition_met = abs(current_value - self.threshold) < 0.001
        elif self.operator == ConditionOperator.NOT_EQUALS:
            condition_met = abs(current_value - self.threshold) >= 0.001
        elif self.operator == ConditionOperator.GREATER_EQUAL:
            condition_met = current_value >= self.threshold
        elif self.operator == ConditionOperator.LESS_EQUAL:
            condition_met = current_value <= self.threshold
        
        return condition_met


@dataclass
class Alert:
    """Алерт"""
    id: str
    rule_name: str
    severity: AlertSeverity
    message: str
    state: AlertState
    fired_at: datetime
    resolved_at: Optional[datetime] = None
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    values: Dict[str, float] = field(default_factory=dict)
    
    def get_duration(self) -> timedelta:
        """Получить длительность алерта"""
        end_time = self.resolved_at or datetime.now()
        return end_time - self.fired_at
    
    def to_dict(self) -> Dict[str, Any]:
        """Преобразовать в словарь"""
        return {
            "id": self.id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "message": self.message,
            "state": self.state.value,
            "fired_at": self.fired_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "duration_seconds": self.get_duration().total_seconds(),
            "labels": self.labels,
            "annotations": self.annotations,
            "values": self.values
        }


@dataclass
class AlertRule:
    """Правило алерта"""
    name: str
    conditions: List[AlertCondition]
    severity: AlertSeverity
    message_template: str
    enabled: bool = True
    cooldown_minutes: int = 5  # Минимальное время между алертами
    max_alerts_per_hour: int = 10
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    
    def generate_message(self, values: Dict[str, float]) -> str:
        """Сгенерировать сообщение алерта"""
        try:
            # Простая подстановка значений в шаблон
            message = self.message_template
            for key, value in values.items():
                placeholder = f"{{{key}}}"
                if placeholder in message:
                    message = message.replace(placeholder, f"{value:.2f}")
            
            return message
        except Exception as e:
            logger.error(f"❌ Error generating alert message: {e}")
            return f"Alert: {self.name}"


class AlertManager:
    """Менеджер алертов"""
    
    def __init__(self):
        self.rules: Dict[str, AlertRule] = {}
        self.active_alerts: Dict[str, Alert] = {}
        self.alert_history: List[Alert] = []
        self.notification_handlers: List[Callable[[Alert], None]] = []
        
        # Состояние для rate limiting
        self.rule_last_fired: Dict[str, datetime] = {}
        self.rule_alert_counts: Dict[str, List[datetime]] = {}
        
        self._lock = threading.Lock()
        
        # Создаем стандартные правила
        self._create_standard_rules()
    
    def _create_standard_rules(self):
        """Создать стандартные правила алертов"""
        
        # Алерт на низкий success rate
        self.add_rule(AlertRule(
            name="workflow_success_rate_low",
            conditions=[AlertCondition(
                metric_name="workflow_success_rate",
                operator=ConditionOperator.LESS_THAN,
                threshold=80.0,  # Менее 80%
                duration_minutes=2
            )],
            severity=AlertSeverity.WARNING,
            message_template="Workflow success rate is low: {workflow_success_rate}%",
            labels={"category": "performance"},
            annotations={"runbook": "Check recent failed workflows"}
        ))
        
        # Алерт на высокое время выполнения
        self.add_rule(AlertRule(
            name="workflow_duration_high",
            conditions=[AlertCondition(
                metric_name="avg_workflow_duration",
                operator=ConditionOperator.GREATER_THAN,
                threshold=300.0,  # Более 5 минут
                duration_minutes=1
            )],
            severity=AlertSeverity.WARNING,
            message_template="Average workflow duration is high: {avg_workflow_duration}s",
            labels={"category": "performance"}
        ))
        
        # Алерт на низкое качество
        self.add_rule(AlertRule(
            name="quality_score_low",
            conditions=[AlertCondition(
                metric_name="avg_quality_score",
                operator=ConditionOperator.LESS_THAN,
                threshold=0.6,  # Менее 60%
                duration_minutes=3
            )],
            severity=AlertSeverity.CRITICAL,
            message_template="Quality score is critically low: {avg_quality_score}",
            labels={"category": "quality"}
        ))
        
        # Алерт на открытые circuit breaker'ы
        self.add_rule(AlertRule(
            name="circuit_breakers_open",
            conditions=[AlertCondition(
                metric_name="circuit_breaker_opens",
                operator=ConditionOperator.GREATER_THAN,
                threshold=0,
                duration_minutes=1
            )],
            severity=AlertSeverity.CRITICAL,
            message_template="Circuit breakers are opening: {circuit_breaker_opens} agents affected",
            labels={"category": "reliability"}
        ))
        
        # Алерт на высокую стоимость
        self.add_rule(AlertRule(
            name="cost_spike",
            conditions=[AlertCondition(
                metric_name="total_cost",
                operator=ConditionOperator.GREATER_THAN,
                threshold=50.0,  # Более $50
                duration_minutes=1
            )],
            severity=AlertSeverity.WARNING,
            message_template="Cost spike detected: ${total_cost}",
            labels={"category": "cost"}
        ))
        
        logger.info(f"📢 Created {len(self.rules)} standard alert rules")
    
    def add_rule(self, rule: AlertRule):
        """Добавить правило алерта"""
        with self._lock:
            self.rules[rule.name] = rule
            logger.info(f"📢 Added alert rule: {rule.name}")
    
    def remove_rule(self, rule_name: str):
        """Удалить правило алерта"""
        with self._lock:
            if rule_name in self.rules:
                del self.rules[rule_name]
                logger.info(f"📢 Removed alert rule: {rule_name}")
    
    def enable_rule(self, rule_name: str):
        """Включить правило"""
        if rule_name in self.rules:
            self.rules[rule_name].enabled = True
            logger.info(f"📢 Enabled alert rule: {rule_name}")
    
    def disable_rule(self, rule_name: str):
        """Отключить правило"""
        if rule_name in self.rules:
            self.rules[rule_name].enabled = False
            logger.info(f"📢 Disabled alert rule: {rule_name}")
    
    def evaluate_rules(self, metrics_values: Dict[str, float]):
        """Оценить все правила на основе текущих метрик"""
        
        current_time = datetime.now()
        
        for rule_name, rule in self.rules.items():
            if not rule.enabled:
                continue
            
            try:
                # Проверяем rate limiting
                if self._is_rate_limited(rule_name, current_time):
                    continue
                
                # Оцениваем условия
                all_conditions_met = True
                condition_values = {}
                
                for condition in rule.conditions:
                    metric_value = metrics_values.get(condition.metric_name)
                    
                    if metric_value is None:
                        all_conditions_met = False
                        break
                    
                    condition_met = condition.evaluate(metric_value, [])
                    condition_values[condition.metric_name] = metric_value
                    
                    if not condition_met:
                        all_conditions_met = False
                        break
                
                # Если все условия выполнены, создаем алерт
                if all_conditions_met:
                    self._fire_alert(rule, condition_values, current_time)
                else:
                    # Проверяем нужно ли разрешить активный алерт
                    self._check_alert_resolution(rule_name)
                    
            except Exception as e:
                logger.error(f"❌ Error evaluating rule {rule_name}: {e}")
    
    def _is_rate_limited(self, rule_name: str, current_time: datetime) -> bool:
        """Проверить rate limiting для правила"""
        
        rule = self.rules[rule_name]
        
        # Проверяем cooldown
        if rule_name in self.rule_last_fired:
            time_since_last = current_time - self.rule_last_fired[rule_name]
            if time_since_last < timedelta(minutes=rule.cooldown_minutes):
                return True
        
        # Проверяем hourly limit
        if rule_name not in self.rule_alert_counts:
            self.rule_alert_counts[rule_name] = []
        
        # Удаляем старые записи (старше часа)
        hour_ago = current_time - timedelta(hours=1)
        self.rule_alert_counts[rule_name] = [
            t for t in self.rule_alert_counts[rule_name] if t > hour_ago
        ]
        
        # Проверяем лимит
        if len(self.rule_alert_counts[rule_name]) >= rule.max_alerts_per_hour:
            return True
        
        return False
    
    def _fire_alert(self, rule: AlertRule, values: Dict[str, float], current_time: datetime):
        """Создать и отправить алерт"""
        
        alert_id = f"{rule.name}_{int(current_time.timestamp())}"
        
        # Проверяем нет ли уже активного алерта для этого правила
        existing_alert_id = None
        for aid, alert in self.active_alerts.items():
            if alert.rule_name == rule.name and alert.state == AlertState.FIRING:
                existing_alert_id = aid
                break
        
        if existing_alert_id:
            # Обновляем существующий алерт
            logger.debug(f"📢 Alert {rule.name} still firing")
            return
        
        # Создаем новый алерт
        message = rule.generate_message(values)
        
        alert = Alert(
            id=alert_id,
            rule_name=rule.name,
            severity=rule.severity,
            message=message,
            state=AlertState.FIRING,
            fired_at=current_time,
            labels=rule.labels.copy(),
            annotations=rule.annotations.copy(),
            values=values.copy()
        )
        
        # Добавляем в активные алерты
        with self._lock:
            self.active_alerts[alert_id] = alert
            self.alert_history.append(alert)
            
            # Ограничиваем историю
            if len(self.alert_history) > 1000:
                self.alert_history = self.alert_history[-500:]
            
            # Обновляем rate limiting
            self.rule_last_fired[rule.name] = current_time
            if rule.name not in self.rule_alert_counts:
                self.rule_alert_counts[rule.name] = []
            self.rule_alert_counts[rule.name].append(current_time)
        
        logger.warning(f"🚨 Alert FIRED: {rule.name} - {message}")
        
        # Отправляем уведомления
        self._send_notifications(alert)
    
    def _check_alert_resolution(self, rule_name: str):
        """Проверить нужно ли разрешить алерт"""
        
        # Находим активные алерты для этого правила
        alerts_to_resolve = []
        
        for alert_id, alert in self.active_alerts.items():
            if alert.rule_name == rule_name and alert.state == AlertState.FIRING:
                alerts_to_resolve.append(alert_id)
        
        # Разрешаем алерты
        current_time = datetime.now()
        for alert_id in alerts_to_resolve:
            self._resolve_alert(alert_id, current_time)
    
    def _resolve_alert(self, alert_id: str, resolution_time: datetime):
        """Разрешить алерт"""
        
        if alert_id in self.active_alerts:
            alert = self.active_alerts[alert_id]
            alert.state = AlertState.RESOLVED
            alert.resolved_at = resolution_time
            
            logger.info(f"✅ Alert RESOLVED: {alert.rule_name} (duration: {alert.get_duration()})")
            
            # Отправляем уведомление о разрешении
            self._send_notifications(alert)
            
            # Удаляем из активных
            del self.active_alerts[alert_id]
    
    def _send_notifications(self, alert: Alert):
        """Отправить уведомления об алерте"""
        
        for handler in self.notification_handlers:
            try:
                handler(alert)
            except Exception as e:
                logger.error(f"❌ Notification handler failed: {e}")
    
    def add_notification_handler(self, handler: Callable[[Alert], None]):
        """Добавить обработчик уведомлений"""
        self.notification_handlers.append(handler)
        logger.info(f"📢 Added notification handler")
    
    def get_active_alerts(self) -> List[Alert]:
        """Получить активные алерты"""
        return list(self.active_alerts.values())
    
    def get_alert_history(self, hours: int = 24) -> List[Alert]:
        """Получить историю алертов"""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        
        return [
            alert for alert in self.alert_history
            if alert.fired_at >= cutoff_time
        ]
    
    def get_alerts_summary(self) -> Dict[str, Any]:
        """Получить сводку по алертам"""
        
        active_alerts = self.get_active_alerts()
        recent_history = self.get_alert_history(24)
        
        # Группируем по severity
        severity_counts = {}
        for alert in active_alerts:
            severity = alert.severity.value
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        
        # Статистика по правилам
        rule_stats = {}
        for alert in recent_history:
            rule_name = alert.rule_name
            if rule_name not in rule_stats:
                rule_stats[rule_name] = {"fired": 0, "resolved": 0, "avg_duration": 0}
            
            rule_stats[rule_name]["fired"] += 1
            if alert.state == AlertState.RESOLVED:
                rule_stats[rule_name]["resolved"] += 1
                
                # Обновляем среднюю длительность
                current_avg = rule_stats[rule_name]["avg_duration"]
                rule_stats[rule_name]["avg_duration"] = (current_avg + alert.get_duration().total_seconds()) / 2
        
        return {
            "active_alerts_count": len(active_alerts),
            "active_alerts_by_severity": severity_counts,
            "recent_alerts_24h": len(recent_history),
            "total_rules": len(self.rules),
            "enabled_rules": len([r for r in self.rules.values() if r.enabled]),
            "rule_statistics": rule_stats,
            "most_frequent_alerts": self._get_most_frequent_alerts(recent_history)
        }
    
    def _get_most_frequent_alerts(self, alerts: List[Alert], limit: int = 5) -> List[Dict[str, Any]]:
        """Получить наиболее частые алерты"""
        
        rule_counts = {}
        for alert in alerts:
            rule_name = alert.rule_name
            rule_counts[rule_name] = rule_counts.get(rule_name, 0) + 1
        
        # Сортируем по частоте
        sorted_rules = sorted(rule_counts.items(), key=lambda x: x[1], reverse=True)
        
        return [
            {"rule_name": rule_name, "count": count}
            for rule_name, count in sorted_rules[:limit]
        ]
    
    def suppress_alert(self, alert_id: str, reason: str = ""):
        """Подавить алерт"""
        if alert_id in self.active_alerts:
            alert = self.active_alerts[alert_id]
            alert.state = AlertState.SUPPRESSED
            alert.annotations["suppression_reason"] = reason
            
            logger.info(f"🔇 Alert SUPPRESSED: {alert.rule_name} - {reason}")
    
    def export_alerts_json(self) -> str:
        """Экспортировать алерты в JSON"""
        
        export_data = {
            "timestamp": datetime.now().isoformat(),
            "active_alerts": [alert.to_dict() for alert in self.get_active_alerts()],
            "recent_history": [alert.to_dict() for alert in self.get_alert_history(24)]
        }
        
        return json.dumps(export_data, indent=2, ensure_ascii=False)
    
    def cleanup_old_history(self, days: int = 7):
        """Очистить старую историю алертов"""
        cutoff_time = datetime.now() - timedelta(days=days)
        
        with self._lock:
            original_count = len(self.alert_history)
            self.alert_history = [
                alert for alert in self.alert_history
                if alert.fired_at >= cutoff_time
            ]
            
            removed_count = original_count - len(self.alert_history)
            logger.info(f"🧹 Cleaned up {removed_count} old alerts")


# Стандартные notification handlers

def log_notification_handler(alert: Alert):
    """Простой handler для логирования"""
    if alert.state == AlertState.FIRING:
        logger.warning(f"🚨 ALERT: {alert.message}")
    elif alert.state == AlertState.RESOLVED:
        logger.info(f"✅ RESOLVED: {alert.rule_name}")


def console_notification_handler(alert: Alert):
    """Handler для вывода в консоль"""
    severity_icons = {
        AlertSeverity.INFO: "ℹ️",
        AlertSeverity.WARNING: "⚠️",
        AlertSeverity.CRITICAL: "🔥",
        AlertSeverity.EMERGENCY: "💥"
    }
    
    icon = severity_icons.get(alert.severity, "🔔")
    
    if alert.state == AlertState.FIRING:
        print(f"{icon} [{alert.severity.value.upper()}] {alert.message}")
    elif alert.state == AlertState.RESOLVED:
        duration = alert.get_duration().total_seconds()
        print(f"✅ RESOLVED: {alert.rule_name} (lasted {duration:.0f}s)")


# Функция для создания webhook handler'а
def create_webhook_handler(webhook_url: str) -> Callable[[Alert], None]:
    """Создать webhook handler"""
    
    def webhook_handler(alert: Alert):
        """Отправить алерт на webhook"""
        try:
            import requests
            
            payload = {
                "alert": alert.to_dict(),
                "timestamp": datetime.now().isoformat()
            }
            
            response = requests.post(webhook_url, json=payload, timeout=5)
            response.raise_for_status()
            
            logger.debug(f"📤 Alert sent to webhook: {webhook_url}")
            
        except Exception as e:
            logger.error(f"❌ Webhook handler failed: {e}")
    
    return webhook_handler
