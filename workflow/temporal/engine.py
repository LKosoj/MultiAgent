"""Temporal Engine для управления таймерами и сигналами."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .models import (
    Signal,
    SignalStatus,
    Timer,
    TimerStatus,
    WorkflowTimer,
)

logger = logging.getLogger(__name__)

WorkflowExecutor = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class TemporalEngine:
    """Управляет таймерами, сигналами и отложенными workflow."""

    def __init__(self, workflow_executor: Optional[WorkflowExecutor] = None) -> None:
        self._workflow_executor = workflow_executor
        
        # Хранилища
        self._timers: Dict[str, Timer] = {}
        self._signals: Dict[str, Signal] = {}
        self._workflow_timers: Dict[str, WorkflowTimer] = {}
        
        # Runtime
        self._running = asyncio.Event()
        self._timer_loop_task: Optional[asyncio.Task[None]] = None
        self._signal_timeout_task: Optional[asyncio.Task[None]] = None
        
        self._lock = asyncio.Lock()
    
    @property
    def is_running(self) -> bool:
        return self._running.is_set()
    
    async def start(self) -> None:
        """Запустить temporal engine."""
        if self.is_running:
            return
        
        self._running.set()
        self._timer_loop_task = asyncio.create_task(self._timer_processing_loop())
        self._signal_timeout_task = asyncio.create_task(self._signal_timeout_loop())
        logger.info("⏰ TemporalEngine started")
    
    async def stop(self) -> None:
        """Остановить temporal engine."""
        if not self.is_running:
            return
        
        self._running.clear()
        
        if self._timer_loop_task:
            self._timer_loop_task.cancel()
            try:
                await self._timer_loop_task
            except asyncio.CancelledError:
                pass
        
        if self._signal_timeout_task:
            self._signal_timeout_task.cancel()
            try:
                await self._signal_timeout_task
            except asyncio.CancelledError:
                pass
        
        logger.info("⏰ TemporalEngine stopped")
    
    # ------------------------------------------------------------------
    # Timers API
    # ------------------------------------------------------------------
    
    async def schedule_timer(
        self,
        workflow_id: str,
        fire_at: datetime,
        callback_name: str,
        callback_args: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Запланировать таймер для workflow."""
        timer_id = f"timer_{uuid.uuid4().hex[:12]}"
        
        timer = Timer(
            timer_id=timer_id,
            workflow_id=workflow_id,
            fire_at=fire_at,
            callback_name=callback_name,
            callback_args=callback_args or {},
            metadata=metadata or {}
        )
        
        async with self._lock:
            self._timers[timer_id] = timer
        
        logger.info("⏰ Timer %s scheduled for workflow %s at %s", 
                   timer_id, workflow_id, fire_at)
        return timer_id
    
    async def schedule_timer_after(
        self,
        workflow_id: str,
        delay: timedelta,
        callback_name: str,
        callback_args: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Запланировать таймер с задержкой."""
        from datetime import timezone
        fire_at = datetime.now(timezone.utc) + delay
        return await self.schedule_timer(
            workflow_id, fire_at, callback_name, callback_args, metadata
        )
    
    async def cancel_timer(self, timer_id: str) -> bool:
        """Отменить таймер."""
        async with self._lock:
            timer = self._timers.get(timer_id)
            if not timer:
                return False
            timer.cancel()
            logger.info("⏰ Timer %s cancelled", timer_id)
            return True
    
    async def get_timer(self, timer_id: str) -> Optional[Timer]:
        """Получить таймер по ID."""
        return self._timers.get(timer_id)
    
    async def list_timers(self, workflow_id: Optional[str] = None) -> List[Timer]:
        """Список таймеров (опционально для конкретного workflow)."""
        if workflow_id:
            return [t for t in self._timers.values() if t.workflow_id == workflow_id]
        return list(self._timers.values())
    
    # ------------------------------------------------------------------
    # Signals API
    # ------------------------------------------------------------------
    
    async def wait_for_signal(
        self,
        workflow_id: str,
        signal_name: str,
        timeout: Optional[timedelta] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Создать ожидание сигнала."""
        signal_id = f"signal_{uuid.uuid4().hex[:12]}"
        
        signal = Signal(
            signal_id=signal_id,
            workflow_id=workflow_id,
            signal_name=signal_name,
            timeout=timeout,
            metadata=metadata or {}
        )
        
        async with self._lock:
            self._signals[signal_id] = signal
        
        logger.info("📡 Signal %s waiting for workflow %s (timeout=%s)", 
                   signal_id, workflow_id, timeout)
        return signal_id
    
    async def send_signal(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Optional[Dict[str, Any]] = None
    ) -> int:
        """Отправить сигнал всем ожидающим."""
        count = 0
        async with self._lock:
            for signal in self._signals.values():
                if (signal.workflow_id == workflow_id and
                    signal.signal_name == signal_name and
                    signal.status == SignalStatus.WAITING):
                    signal.receive(payload)
                    count += 1
                    logger.info("📡 Signal %s received by %s", 
                               signal.signal_id, workflow_id)
        
        return count
    
    async def cancel_signal(self, signal_id: str) -> bool:
        """Отменить ожидание сигнала."""
        async with self._lock:
            signal = self._signals.get(signal_id)
            if not signal:
                return False
            signal.cancel()
            logger.info("📡 Signal %s cancelled", signal_id)
            return True
    
    async def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Получить сигнал по ID."""
        return self._signals.get(signal_id)
    
    async def list_signals(self, workflow_id: Optional[str] = None) -> List[Signal]:
        """Список сигналов (опционально для конкретного workflow)."""
        if workflow_id:
            return [s for s in self._signals.values() if s.workflow_id == workflow_id]
        return list(self._signals.values())
    
    # ------------------------------------------------------------------
    # Workflow Timers API
    # ------------------------------------------------------------------
    
    async def schedule_workflow(
        self,
        workflow_name: str,
        fire_at: datetime,
        context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Запланировать запуск workflow на определённое время."""
        timer_id = f"wf_timer_{uuid.uuid4().hex[:12]}"
        
        wf_timer = WorkflowTimer(
            timer_id=timer_id,
            workflow_name=workflow_name,
            fire_at=fire_at,
            context=context or {},
            metadata=metadata or {}
        )
        
        async with self._lock:
            self._workflow_timers[timer_id] = wf_timer
        
        logger.info("⏰ Workflow %s scheduled at %s (timer_id=%s)", 
                   workflow_name, fire_at, timer_id)
        return timer_id
    
    async def schedule_workflow_after(
        self,
        workflow_name: str,
        delay: timedelta,
        context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Запланировать запуск workflow через delay."""
        from datetime import timezone
        fire_at = datetime.now(timezone.utc) + delay
        return await self.schedule_workflow(workflow_name, fire_at, context, metadata)
    
    async def cancel_workflow_timer(self, timer_id: str) -> bool:
        """Отменить отложенный запуск workflow."""
        async with self._lock:
            wf_timer = self._workflow_timers.get(timer_id)
            if not wf_timer:
                return False
            wf_timer.cancel()
            logger.info("⏰ Workflow timer %s cancelled", timer_id)
            return True
    
    async def list_workflow_timers(self) -> List[WorkflowTimer]:
        """Список отложенных workflow."""
        return list(self._workflow_timers.values())
    
    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------
    
    async def _timer_processing_loop(self) -> None:
        """Цикл обработки таймеров."""
        logger.info("⏰ Timer processing loop started")
        
        while self.is_running:
            try:
                await asyncio.sleep(1)  # Проверяем каждую секунду
                
                # Обрабатываем обычные таймеры
                await self._process_due_timers()
                
                # Обрабатываем workflow таймеры
                await self._process_due_workflow_timers()
                
            except Exception as exc:
                logger.error("Error in timer loop: %s", exc, exc_info=True)
    
    async def _process_due_timers(self) -> None:
        """Обработка наступивших таймеров."""
        due_timers: List[Timer] = []
        
        async with self._lock:
            for timer in list(self._timers.values()):
                if timer.status == TimerStatus.SCHEDULED and timer.is_due:
                    timer.mark_firing()
                    due_timers.append(timer)
        
        for timer in due_timers:
            try:
                # TODO: Вызвать callback в контексте workflow
                # Пока просто логируем и отмечаем completed
                logger.info("⏰ Timer %s fired (callback=%s)", 
                           timer.timer_id, timer.callback_name)
                timer.mark_completed()
            except Exception as exc:
                logger.error("Error firing timer %s: %s", timer.timer_id, exc)
    
    async def _process_due_workflow_timers(self) -> None:
        """Обработка наступивших workflow таймеров."""
        due_wf_timers: List[WorkflowTimer] = []
        
        async with self._lock:
            for wf_timer in list(self._workflow_timers.values()):
                if wf_timer.status == TimerStatus.SCHEDULED and wf_timer.is_due:
                    wf_timer.mark_firing()
                    due_wf_timers.append(wf_timer)
        
        for wf_timer in due_wf_timers:
            try:
                if self._workflow_executor:
                    logger.info("⏰ Executing scheduled workflow %s", 
                               wf_timer.workflow_name)
                    await self._workflow_executor(
                        wf_timer.workflow_name,
                        wf_timer.context
                    )
                    wf_timer.mark_completed()
                else:
                    logger.warning("No workflow executor configured for timer %s", 
                                 wf_timer.timer_id)
            except Exception as exc:
                logger.error("Error executing scheduled workflow %s: %s", 
                           wf_timer.workflow_name, exc)
    
    async def _signal_timeout_loop(self) -> None:
        """Цикл проверки timeout для сигналов."""
        logger.info("📡 Signal timeout loop started")
        
        while self.is_running:
            try:
                await asyncio.sleep(1)
                
                async with self._lock:
                    for signal in list(self._signals.values()):
                        if (signal.status == SignalStatus.WAITING and 
                            signal.is_timed_out):
                            signal.mark_timeout()
                            logger.info("📡 Signal %s timed out", signal.signal_id)
                
            except Exception as exc:
                logger.error("Error in signal timeout loop: %s", exc, exc_info=True)
    
    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику temporal engine."""
        return {
            "timers": {
                "total": len(self._timers),
                "scheduled": sum(1 for t in self._timers.values() 
                               if t.status == TimerStatus.SCHEDULED),
                "firing": sum(1 for t in self._timers.values() 
                            if t.status == TimerStatus.FIRING),
                "completed": sum(1 for t in self._timers.values() 
                               if t.status == TimerStatus.COMPLETED),
                "cancelled": sum(1 for t in self._timers.values() 
                               if t.status == TimerStatus.CANCELLED),
            },
            "signals": {
                "total": len(self._signals),
                "waiting": sum(1 for s in self._signals.values() 
                             if s.status == SignalStatus.WAITING),
                "received": sum(1 for s in self._signals.values() 
                              if s.status == SignalStatus.RECEIVED),
                "timeout": sum(1 for s in self._signals.values() 
                             if s.status == SignalStatus.TIMEOUT),
                "cancelled": sum(1 for s in self._signals.values() 
                               if s.status == SignalStatus.CANCELLED),
            },
            "workflow_timers": {
                "total": len(self._workflow_timers),
                "scheduled": sum(1 for wt in self._workflow_timers.values() 
                               if wt.status == TimerStatus.SCHEDULED),
                "completed": sum(1 for wt in self._workflow_timers.values() 
                               if wt.status == TimerStatus.COMPLETED),
            }
        }

