"""
Parallel Executor для параллельного выполнения шагов workflow
"""
import asyncio
import logging
from typing import Dict, Any, List, Set, Optional, Callable
from datetime import datetime
from collections import defaultdict, deque
import inspect

from ..models import WorkflowStep, StepResult, StepStatus, WorkflowContext

logger = logging.getLogger(__name__)


class DependencyGraph:
    """Граф зависимостей для планирования параллельного выполнения"""
    
    def __init__(self, steps: List[WorkflowStep]):
        self.steps = {step.id: step for step in steps}
        self.dependencies = {step.id: set(step.depends_on) for step in steps}
        self.reverse_dependencies = defaultdict(set)
        
        # Строим обратный граф зависимостей
        for step_id, deps in self.dependencies.items():
            for dep in deps:
                self.reverse_dependencies[dep].add(step_id)
    
    def get_ready_steps(self, completed_steps: Set[str]) -> List[str]:
        """Получить шаги, готовые к выполнению"""
        ready = []
        for step_id, deps in self.dependencies.items():
            if step_id not in completed_steps and deps.issubset(completed_steps):
                ready.append(step_id)
        return ready
    
    def has_cycles(self) -> bool:
        """Проверить наличие циклических зависимостей"""
        # Топологическая сортировка для обнаружения циклов
        in_degree = {step_id: len(deps) for step_id, deps in self.dependencies.items()}
        queue = deque([step_id for step_id, degree in in_degree.items() if degree == 0])
        processed = 0
        
        while queue:
            current = queue.popleft()
            processed += 1
            
            for dependent in self.reverse_dependencies[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        return processed != len(self.steps)


class ParallelWorkflowExecutor:
    """Исполнитель параллельных шагов workflow"""
    
    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.execution_stats = {
            "total_steps": 0,
            "parallel_executed": 0,
            "sequential_executed": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0
        }
    
    async def execute_steps_parallel(self, 
                                   steps: List[WorkflowStep],
                                   context: WorkflowContext,
                                   step_executor: Callable,
                                   dependency_checker: Callable,
                                   condition_checker: Callable,
                                   stop_checker: Optional[Callable] = None,
                                   stop_on_failure: bool = False) -> Dict[str, StepResult]:
        """Выполнить шаги с учетом зависимостей параллельно"""
        
        dependency_graph = DependencyGraph(steps)
        
        # Проверяем наличие циклов
        if dependency_graph.has_cycles():
            raise ValueError("Обнаружены циклические зависимости в workflow")
        
        step_results = {}
        context._workflow_step_results = step_results
        completed_steps = set()  # Включает COMPLETED, SKIPPED, FAILED
        
        self.execution_stats["total_steps"] = len(steps)
        
        logger.info(f"🚀 Начинаем параллельное выполнение {len(steps)} шагов (max_concurrent: {self.max_concurrent})")
        
        while len(completed_steps) < len(steps):
            if stop_on_failure and any(
                r.status == StepStatus.FAILED for r in step_results.values()
            ):
                logger.error("🛑 Обнаружен провал шага; stop_on_failure=True, останавливаем запуск новых шагов")
                if self.active_tasks:
                    await self._wait_for_any_task_completion(step_results, completed_steps)
                    continue

                remaining_steps = [
                    step_id for step_id in dependency_graph.steps
                    if step_id not in completed_steps
                ]
                for step_id in remaining_steps:
                    step_results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.SKIPPED,
                        start_time=datetime.now(),
                        end_time=datetime.now(),
                        error="Пропущен из-за stop_on_failure после провала предыдущего шага",
                    )
                    completed_steps.add(step_id)
                    self.execution_stats["skipped"] += 1
                break

            stop_requested = False
            if stop_checker:
                stop_requested = stop_checker()
                if inspect.isawaitable(stop_requested):
                    stop_requested = await stop_requested

            if stop_requested:
                logger.info("🚫 Получен сигнал остановки workflow; новые шаги запускаться не будут")
                if self.active_tasks:
                    await self._wait_for_any_task_completion(step_results, completed_steps)
                    continue

                remaining_steps = [
                    step_id for step_id in dependency_graph.steps
                    if step_id not in completed_steps
                ]
                for step_id in remaining_steps:
                    step_results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.SKIPPED,
                        start_time=datetime.now(),
                        end_time=datetime.now(),
                    )
                    completed_steps.add(step_id)
                    self.execution_stats["skipped"] += 1
                break

            # Получаем шаги, готовые к выполнению (исключаем уже запущенные)
            ready_step_ids = dependency_graph.get_ready_steps(completed_steps)
            ready_step_ids = [s for s in ready_step_ids if s not in self.active_tasks]

            # Никогда не запускаем шаг, если любая его зависимость завершилась FAILED.
            # Такой шаг помечаем FAILED и не планируем к запуску.
            blocked_by_failed_dep: List[str] = []
            for step_id in list(ready_step_ids):
                step = dependency_graph.steps[step_id]
                failed_deps = [
                    dep_id for dep_id in step.depends_on
                    if step_results.get(dep_id) and step_results[dep_id].status == StepStatus.FAILED
                ]
                if failed_deps:
                    step_results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.FAILED,
                        start_time=datetime.now(),
                        end_time=datetime.now(),
                        error=f"Зависимости завершились с ошибкой: {', '.join(failed_deps)}",
                    )
                    completed_steps.add(step_id)
                    self.execution_stats["failed"] += 1
                    blocked_by_failed_dep.append(step_id)

            if blocked_by_failed_dep:
                ready_step_ids = [s for s in ready_step_ids if s not in blocked_by_failed_dep]
                logger.error(
                    "⛔ Шаги заблокированы из-за проваленных зависимостей: %s",
                    blocked_by_failed_dep,
                )
            
            if not ready_step_ids:
                # Если нет готовых шагов, но есть незавершенные задачи - ждем их
                if self.active_tasks:
                    await self._wait_for_any_task_completion(step_results, completed_steps)
                    continue
                else:
                    # Не должно происходить при корректном графе зависимостей
                    remaining = set(dependency_graph.steps.keys()) - completed_steps
                    if remaining:
                        raise RuntimeError(f"Deadlock: нет готовых шагов, но остались незавершенные: {remaining}")
                    else:
                        break  # Все шаги завершены
            
            # Запускаем готовые шаги (с учетом лимита параллелизма)
            logger.info(f"🎯 Готовые к запуску шаги: {ready_step_ids}, активных задач: {len(self.active_tasks)}/{self.max_concurrent}")
            for step_id in ready_step_ids:
                # Проверяем лимит параллелизма
                if len(self.active_tasks) >= self.max_concurrent:
                    logger.info(f"⏸️ Достигнут лимит параллелизма ({self.max_concurrent}), пропускаем шаг {step_id}")
                    break
                
                step = dependency_graph.steps[step_id]
                
                # Дополнительная проверка зависимостей не нужна - 
                # граф зависимостей уже обеспечивает корректный порядок
                # if not dependency_checker(step, step_results):
                #     logger.warning(f"⚠️ Шаг {step_id} не прошел проверку зависимостей, пропускаем")
                #     continue
                
                # Проверяем условие выполнения (если возвращает True - пропускаем)
                logger.info(f"🔍 Проверяем условие для шага {step_id}")
                if condition_checker(step, context):
                    step_results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.SKIPPED,
                        output=context.step_outputs.get(step_id),
                        start_time=datetime.now(),
                        end_time=datetime.now()
                    )
                    completed_steps.add(step_id)
                    self.execution_stats["skipped"] += 1
                    logger.info(f"⏸️ Шаг {step_id} пропущен по условию")
                    continue
                else:
                    logger.info(f"✅ Условие для шага {step_id} выполнено, запускаем")
                
                # Запускаем шаг асинхронно
                task = asyncio.create_task(
                    self._execute_step_with_tracking(step, context, step_executor),
                    name=f"step_{step_id}"
                )
                self.active_tasks[step_id] = task
                logger.info(f"🔄 Запущен шаг {step_id} параллельно (активных задач: {len(self.active_tasks)}/{self.max_concurrent})")
            
            # Если достигли лимита параллелизма, ждем завершения
            if len(self.active_tasks) >= self.max_concurrent:
                await self._wait_for_any_task_completion(step_results, completed_steps)
            
            # Если нет готовых шагов, но есть активные задачи, тоже ждем
            elif not ready_step_ids and self.active_tasks:
                await self._wait_for_any_task_completion(step_results, completed_steps)
        
        # Ждем завершения всех оставшихся задач
        while self.active_tasks:
            await self._wait_for_any_task_completion(step_results, completed_steps)
        
        logger.info(f"✅ Параллельное выполнение завершено. Статистика: {self.execution_stats}")
        return step_results
    
    async def _execute_step_with_tracking(self, 
                                        step: WorkflowStep, 
                                        context: WorkflowContext,
                                        step_executor: Callable) -> StepResult:
        """Выполнить шаг с отслеживанием метрик"""
        start_time = datetime.now()
        
        try:
            result = await step_executor(step, context)
            
            if result.status == StepStatus.COMPLETED:
                self.execution_stats["parallel_executed"] += 1
                self.execution_stats["completed"] += 1
            elif result.status == StepStatus.FAILED:
                self.execution_stats["failed"] += 1
            
            execution_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"✅ Шаг {step.id} завершен за {execution_time:.2f}s со статусом {result.status.value}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Ошибка выполнения шага {step.id}: {e}")
            self.execution_stats["failed"] += 1
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=start_time,
                end_time=datetime.now(),
                error=str(e)
            )
    
    async def _wait_for_any_task_completion(self, 
                                          step_results: Dict[str, StepResult],
                                          completed_steps: Set[str]):
        """Ждать завершения любой из активных задач"""
        if not self.active_tasks:
            return
        
        # Ждем завершения первой задачи
        done, pending = await asyncio.wait(
            self.active_tasks.values(),
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Обрабатываем завершенные задачи
        for task in done:
            step_id = None
            for sid, t in self.active_tasks.items():
                if t == task:
                    step_id = sid
                    break
            
            if step_id:
                try:
                    result = await task
                    step_results[step_id] = result
                    completed_steps.add(step_id)
                    logger.debug(f"📝 Шаг {step_id} завершен и записан в результаты")
                    
                except Exception as e:
                    logger.error(f"❌ Ошибка в задаче {step_id}: {e}")
                    step_results[step_id] = StepResult(
                        step_id=step_id,
                        status=StepStatus.FAILED,
                        start_time=datetime.now(),
                        end_time=datetime.now(),
                        error=str(e)
                    )
                    completed_steps.add(step_id)
                    self.execution_stats["failed"] += 1
                
                # Удаляем завершенную задачу
                del self.active_tasks[step_id]
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Получить статистику выполнения"""
        total = self.execution_stats["total_steps"]
        if total > 0:
            skipped = self.execution_stats["skipped"]
            executed = total - skipped
            success_rate = (self.execution_stats["completed"] / executed * 100) if executed > 0 else 0.0
            return {
                **self.execution_stats,
                "parallel_percentage": (self.execution_stats["parallel_executed"] / total) * 100,
                "success_rate": success_rate
            }
        return self.execution_stats
