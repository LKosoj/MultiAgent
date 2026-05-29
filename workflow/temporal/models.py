"""Модели для temporal workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimerStatus(Enum):
    """Статус таймера."""
    SCHEDULED = "scheduled"
    FIRING = "firing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SignalStatus(Enum):
    """Статус сигнала."""
    WAITING = "waiting"
    RECEIVED = "received"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class Timer:
    """Таймер для отложенного выполнения."""
    
    timer_id: str
    workflow_id: str
    fire_at: datetime
    callback_name: str
    callback_args: Dict[str, Any] = field(default_factory=dict)
    status: TimerStatus = TimerStatus.SCHEDULED
    created_at: datetime = field(default_factory=_utcnow)
    fired_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_due(self) -> bool:
        """Проверка наступило ли время срабатывания."""
        return _utcnow() >= self.fire_at
    
    @property
    def remaining_seconds(self) -> float:
        """Секунд до срабатывания."""
        delta = self.fire_at - _utcnow()
        return max(0.0, delta.total_seconds())
    
    def mark_firing(self) -> None:
        """Отметить что таймер срабатывает."""
        self.status = TimerStatus.FIRING
        self.fired_at = _utcnow()
    
    def mark_completed(self) -> None:
        """Отметить что таймер завершён."""
        self.status = TimerStatus.COMPLETED
    
    def cancel(self) -> None:
        """Отменить таймер."""
        self.status = TimerStatus.CANCELLED


@dataclass
class Signal:
    """Сигнал для межпроцессного взаимодействия."""
    
    signal_id: str
    workflow_id: str
    signal_name: str
    timeout: Optional[timedelta] = None
    status: SignalStatus = SignalStatus.WAITING
    created_at: datetime = field(default_factory=_utcnow)
    received_at: Optional[datetime] = None
    payload: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_timed_out(self) -> bool:
        """Проверка истёк ли timeout."""
        if not self.timeout:
            return False
        deadline = self.created_at + self.timeout
        return _utcnow() > deadline
    
    @property
    def remaining_timeout_seconds(self) -> Optional[float]:
        """Секунд до timeout."""
        if not self.timeout:
            return None
        deadline = self.created_at + self.timeout
        delta = deadline - _utcnow()
        return max(0.0, delta.total_seconds())
    
    def receive(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """Получить сигнал."""
        self.status = SignalStatus.RECEIVED
        self.received_at = _utcnow()
        self.payload = payload
    
    def mark_timeout(self) -> None:
        """Отметить timeout."""
        self.status = SignalStatus.TIMEOUT
    
    def cancel(self) -> None:
        """Отменить ожидание сигнала."""
        self.status = SignalStatus.CANCELLED


@dataclass
class WorkflowTimer:
    """Таймер для отложенного запуска workflow."""
    
    timer_id: str
    workflow_name: str
    fire_at: datetime
    context: Dict[str, Any] = field(default_factory=dict)
    status: TimerStatus = TimerStatus.SCHEDULED
    created_at: datetime = field(default_factory=_utcnow)
    fired_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_due(self) -> bool:
        """Проверка наступило ли время срабатывания."""
        return _utcnow() >= self.fire_at
    
    @property
    def remaining_seconds(self) -> float:
        """Секунд до срабатывания."""
        delta = self.fire_at - _utcnow()
        return max(0.0, delta.total_seconds())
    
    def mark_firing(self) -> None:
        """Отметить что таймер срабатывает."""
        self.status = TimerStatus.FIRING
        self.fired_at = _utcnow()
    
    def mark_completed(self) -> None:
        """Отметить что таймер завершён."""
        self.status = TimerStatus.COMPLETED
    
    def cancel(self) -> None:
        """Отменить таймер."""
        self.status = TimerStatus.CANCELLED

