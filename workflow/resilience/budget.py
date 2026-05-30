"""
Budget Manager для контроля затрат и ресурсов
"""
import logging
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class BudgetType(Enum):
    """Типы бюджетов"""
    TOKENS = "tokens"
    TIME = "time"
    COST = "cost"
    RETRIES = "retries"
    API_CALLS = "api_calls"


class BudgetStatus(Enum):
    """Статусы бюджета"""
    AVAILABLE = "available"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


@dataclass
class BudgetLimit:
    """Лимит бюджета"""
    budget_type: BudgetType
    limit: float
    warning_threshold: float = 0.8  # 80% от лимита
    critical_threshold: float = 0.95  # 95% от лимита
    current_usage: float = 0.0
    
    def get_status(self) -> BudgetStatus:
        """Получить статус бюджета"""
        usage_ratio = self.current_usage / self.limit if self.limit > 0 else 0
        
        if usage_ratio >= 1.0:
            return BudgetStatus.EXHAUSTED
        elif usage_ratio >= self.critical_threshold:
            return BudgetStatus.CRITICAL
        elif usage_ratio >= self.warning_threshold:
            return BudgetStatus.WARNING
        else:
            return BudgetStatus.AVAILABLE
    
    def get_remaining(self) -> float:
        """Получить остаток бюджета"""
        return max(0, self.limit - self.current_usage)
    
    def get_usage_percentage(self) -> float:
        """Получить процент использования"""
        return (self.current_usage / self.limit * 100) if self.limit > 0 else 0


class BudgetManager:
    """Менеджер бюджетов ресурсов"""
    
    def __init__(self):
        self._lock = threading.Lock()
        # Бюджеты по workflow
        self.workflow_budgets: Dict[str, Dict[BudgetType, BudgetLimit]] = {}
        
        # Бюджеты по шагам
        self.step_budgets: Dict[str, Dict[BudgetType, BudgetLimit]] = {}
        
        # История потребления
        self.consumption_history: Dict[str, List[Dict[str, Any]]] = {}
        
        # Алерты
        self.alert_callbacks: List[callable] = []
        
        # Дефолтные лимиты
        self.default_limits = {
            BudgetType.TOKENS: 100000,
            BudgetType.TIME: 1800,  # 30 минут
            BudgetType.COST: 10.0,  # $10
            BudgetType.RETRIES: 5,
            BudgetType.API_CALLS: 50
        }
    
    def create_workflow_budget(self, workflow_id: str, 
                              limits: Dict[BudgetType, float] = None) -> Dict[BudgetType, BudgetLimit]:
        """Создать бюджет для workflow"""
        
        if limits is None:
            limits = self.default_limits
        
        budget = {}
        for budget_type, limit in limits.items():
            budget[budget_type] = BudgetLimit(
                budget_type=budget_type,
                limit=limit
            )
        
        self.workflow_budgets[workflow_id] = budget
        logger.info(f"💰 Created budget for workflow '{workflow_id}': {len(budget)} budget types")
        
        return budget
    
    def create_step_budget(self, step_id: str, 
                          limits: Dict[BudgetType, float] = None) -> Dict[BudgetType, BudgetLimit]:
        """Создать бюджет для шага"""
        
        if limits is None:
            # Дефолтные лимиты для шага (меньше чем для workflow)
            limits = {
                BudgetType.TOKENS: 20000,
                BudgetType.TIME: 300,  # 5 минут
                BudgetType.COST: 2.0,  # $2
                BudgetType.RETRIES: 3,
                BudgetType.API_CALLS: 10
            }
        
        budget = {}
        for budget_type, limit in limits.items():
            budget[budget_type] = BudgetLimit(
                budget_type=budget_type,
                limit=limit
            )
        
        self.step_budgets[step_id] = budget
        logger.info(f"💰 Created budget for step '{step_id}': {len(budget)} budget types")
        
        return budget
    
    def check_budget(self, entity_id: str, budget_type: BudgetType, 
                    amount: float, entity_type: str = "workflow") -> bool:
        """Проверить возможность потратить amount ресурса"""
        
        budget_dict = (self.workflow_budgets if entity_type == "workflow" 
                      else self.step_budgets)
        
        if entity_id not in budget_dict:
            logger.warning(f"⚠️ No budget found for {entity_type} '{entity_id}'")
            return True  # Разрешаем если бюджет не настроен
        
        if budget_type not in budget_dict[entity_id]:
            logger.warning(f"⚠️ No {budget_type.value} budget for {entity_type} '{entity_id}'")
            return True
        
        budget_limit = budget_dict[entity_id][budget_type]
        
        # Проверяем хватает ли бюджета
        if budget_limit.current_usage + amount > budget_limit.limit:
            logger.warning(f"💸 Budget exceeded for {entity_type} '{entity_id}': "
                         f"{budget_type.value} {budget_limit.current_usage + amount} > {budget_limit.limit}")
            return False
        
        return True
    
    def consume_budget(self, entity_id: str, budget_type: BudgetType,
                      amount: float, entity_type: str = "workflow",
                      description: str = "") -> bool:
        """Потратить бюджет"""

        with self._lock:
            # Проверяем и списываем атомарно под lock
            budget_dict = (self.workflow_budgets if entity_type == "workflow"
                          else self.step_budgets)

            if entity_id not in budget_dict:
                logger.warning(f"⚠️ No budget found for {entity_type} '{entity_id}'")
                return True  # Разрешаем если бюджет не настроен

            if budget_type not in budget_dict[entity_id]:
                logger.warning(f"⚠️ No {budget_type.value} budget for {entity_type} '{entity_id}'")
                return True

            budget_limit = budget_dict[entity_id][budget_type]

            if budget_limit.current_usage + amount > budget_limit.limit:
                logger.warning(f"💸 Budget exceeded for {entity_type} '{entity_id}': "
                             f"{budget_type.value} {budget_limit.current_usage + amount} > {budget_limit.limit}")
                return False

            old_usage = budget_limit.current_usage
            budget_limit.current_usage += amount
            new_usage = budget_limit.current_usage
            new_remaining = budget_limit.get_remaining()
            new_status = budget_limit.get_status()
            # Снимок скаляров под локом для алерта: сам алерт уходит в asyncio.create_task
            # уже ПОСЛЕ выхода из лока, и читать живой budget_limit там нельзя — другой
            # поток может успеть изменить current_usage и алерт сообщит завышенный процент.
            alert_usage_pct = budget_limit.get_usage_percentage()
            alert_limit = budget_limit.limit

            # Записываем потребление под тем же локом, чтобы избежать race condition
            consumption_record = {
                "timestamp": datetime.now().isoformat(),
                "entity_id": entity_id,
                "entity_type": entity_type,
                "budget_type": budget_type.value,
                "amount": amount,
                "total_usage": new_usage,
                "remaining": new_remaining,
                "description": description
            }

            history_key = f"{entity_type}:{entity_id}"
            self.consumption_history.setdefault(history_key, []).append(consumption_record)

        # Проверяем пороги и отправляем алерты
        if new_status in [BudgetStatus.WARNING, BudgetStatus.CRITICAL, BudgetStatus.EXHAUSTED]:
            # Создаем task для отправки алерта (передаём снимок скаляров, не живой объект)
            import asyncio
            asyncio.create_task(self._send_budget_alert(
                entity_id, entity_type, budget_type, new_status,
                alert_usage_pct, new_remaining, alert_limit,
            ))

        logger.debug(f"💰 Consumed {amount} {budget_type.value} for {entity_type} '{entity_id}' "
                    f"({old_usage:.1f} -> {new_usage:.1f})")
        
        return True
    
    async def _send_budget_alert(self, entity_id: str, entity_type: str,
                               budget_type: BudgetType, status: BudgetStatus,
                               usage_percentage: float, remaining: float, limit: float):
        """Отправить алерт о состоянии бюджета.

        Принимает скаляры (снятые под локом в consume_budget), а не живой BudgetLimit,
        чтобы отложенный asyncio-task сообщал значения на момент пересечения порога.
        """

        alert_data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "budget_type": budget_type.value,
            "status": status.value,
            "usage_percentage": usage_percentage,
            "remaining": remaining,
            "limit": limit,
            "timestamp": datetime.now().isoformat()
        }

        logger.warning(f"🚨 Budget alert: {entity_type} '{entity_id}' "
                      f"{budget_type.value} budget is {status.value} "
                      f"({usage_percentage:.1f}%)")
        
        # Вызываем зарегистрированные callback'и
        for callback in self.alert_callbacks:
            try:
                await callback(alert_data)
            except Exception as e:
                logger.error(f"❌ Budget alert callback failed: {e}")
    
    def get_budget_status(self, entity_id: str, entity_type: str = "workflow") -> Dict[str, Any]:
        """Получить статус бюджета"""
        
        budget_dict = (self.workflow_budgets if entity_type == "workflow" 
                      else self.step_budgets)
        
        if entity_id not in budget_dict:
            return {"error": f"No budget found for {entity_type} '{entity_id}'"}
        
        budget = budget_dict[entity_id]
        status = {}
        
        for budget_type, budget_limit in budget.items():
            status[budget_type.value] = {
                "limit": budget_limit.limit,
                "current_usage": budget_limit.current_usage,
                "remaining": budget_limit.get_remaining(),
                "usage_percentage": budget_limit.get_usage_percentage(),
                "status": budget_limit.get_status().value,
                "warning_threshold": budget_limit.warning_threshold,
                "critical_threshold": budget_limit.critical_threshold
            }
        
        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "budgets": status,
            "overall_status": self._calculate_overall_status(budget)
        }
    
    def _calculate_overall_status(self, budget: Dict[BudgetType, BudgetLimit]) -> str:
        """Вычислить общий статус бюджета"""
        
        statuses = [limit.get_status() for limit in budget.values()]
        
        if BudgetStatus.EXHAUSTED in statuses:
            return BudgetStatus.EXHAUSTED.value
        elif BudgetStatus.CRITICAL in statuses:
            return BudgetStatus.CRITICAL.value
        elif BudgetStatus.WARNING in statuses:
            return BudgetStatus.WARNING.value
        else:
            return BudgetStatus.AVAILABLE.value
    
    def get_consumption_report(self, entity_id: str = None, 
                             entity_type: str = None,
                             hours: int = 24) -> Dict[str, Any]:
        """Получить отчет о потреблении ресурсов"""
        
        cutoff_time = datetime.now() - timedelta(hours=hours)
        report = {
            "period_hours": hours,
            "cutoff_time": cutoff_time.isoformat(),
            "entities": {},
            "summary": {
                "total_entities": 0,
                "total_consumption": {},
                "alerts_count": 0
            }
        }
        
        for history_key, history in self.consumption_history.items():
            # Фильтруем по времени и параметрам
            filtered_history = []
            for record in history:
                record_time = datetime.fromisoformat(record["timestamp"])
                if record_time >= cutoff_time:
                    
                    # Фильтруем по entity_id и entity_type если указаны
                    if entity_id and record["entity_id"] != entity_id:
                        continue
                    if entity_type and record["entity_type"] != entity_type:
                        continue
                    
                    filtered_history.append(record)
            
            if filtered_history:
                # Агрегируем потребление по типам бюджета
                entity_consumption = {}
                for record in filtered_history:
                    budget_type = record["budget_type"]
                    if budget_type not in entity_consumption:
                        entity_consumption[budget_type] = 0
                    entity_consumption[budget_type] += record["amount"]
                
                report["entities"][history_key] = {
                    "consumption": entity_consumption,
                    "records_count": len(filtered_history),
                    "latest_record": filtered_history[-1]["timestamp"]
                }
                
                # Обновляем summary
                report["summary"]["total_entities"] += 1
                for budget_type, amount in entity_consumption.items():
                    if budget_type not in report["summary"]["total_consumption"]:
                        report["summary"]["total_consumption"][budget_type] = 0
                    report["summary"]["total_consumption"][budget_type] += amount
        
        return report
    
    def reset_budget(self, entity_id: str, entity_type: str = "workflow"):
        """Сбросить бюджет (обнулить потребление)"""
        
        budget_dict = (self.workflow_budgets if entity_type == "workflow" 
                      else self.step_budgets)
        
        if entity_id in budget_dict:
            for budget_limit in budget_dict[entity_id].values():
                budget_limit.current_usage = 0.0
            
            logger.info(f"🔄 Reset budget for {entity_type} '{entity_id}'")
        else:
            logger.warning(f"⚠️ Cannot reset budget - {entity_type} '{entity_id}' not found")
    
    def add_alert_callback(self, callback: callable):
        """Добавить callback для алертов бюджета"""
        self.alert_callbacks.append(callback)
        logger.info(f"📢 Added budget alert callback")
    
    def get_budget_summary(self) -> Dict[str, Any]:
        """Получить общую сводку по всем бюджетам"""
        
        summary = {
            "workflows": {
                "total": len(self.workflow_budgets),
                "by_status": {}
            },
            "steps": {
                "total": len(self.step_budgets),
                "by_status": {}
            },
            "consumption_history_entries": sum(len(h) for h in self.consumption_history.values())
        }
        
        # Статистика по статусам workflow бюджетов
        for workflow_id, budget in self.workflow_budgets.items():
            status = self._calculate_overall_status(budget)
            summary["workflows"]["by_status"][status] = summary["workflows"]["by_status"].get(status, 0) + 1
        
        # Статистика по статусам step бюджетов
        for step_id, budget in self.step_budgets.items():
            status = self._calculate_overall_status(budget)
            summary["steps"]["by_status"][status] = summary["steps"]["by_status"].get(status, 0) + 1
        
        return summary
    
    def cleanup_old_history(self, days: int = 7):
        """Очистить старую историю потребления"""
        cutoff_time = datetime.now() - timedelta(days=days)
        
        for history_key in list(self.consumption_history.keys()):
            filtered_history = []
            for record in self.consumption_history[history_key]:
                record_time = datetime.fromisoformat(record["timestamp"])
                if record_time >= cutoff_time:
                    filtered_history.append(record)
            
            if filtered_history:
                self.consumption_history[history_key] = filtered_history
            else:
                del self.consumption_history[history_key]
        
        logger.info(f"🧹 Cleaned budget history older than {days} days")
