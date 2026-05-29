"""
Alternative Executor для параллельного выполнения и выбора лучших результатов
"""
import asyncio
import logging
from typing import Dict, Any, List, Optional, Callable, Tuple
from datetime import datetime, timedelta
from enum import Enum

from ..models import StepResult, StepStatus, WorkflowStep, WorkflowContext

logger = logging.getLogger(__name__)


class ExecutionStrategy(Enum):
    """Стратегии выполнения альтернатив"""
    RACE = "race"                    # Первый успешный результат
    ALL_COMPLETE = "all_complete"    # Ждем завершения всех
    BEST_QUALITY = "best_quality"    # Выбираем лучший по качеству
    FASTEST = "fastest"              # Самый быстрый успешный
    CONSENSUS = "consensus"          # Консенсус между результатами


class AlternativeResult:
    """Результат выполнения альтернативы"""
    
    def __init__(self, alternative_id: str, step_result: StepResult, 
                 execution_time: float, success: bool):
        self.alternative_id = alternative_id
        self.step_result = step_result
        self.execution_time = execution_time
        self.success = success
        self.timestamp = datetime.now()


class ParallelRunner:
    """Исполнитель параллельных задач с контролем ресурсов"""
    
    def __init__(self, max_concurrent: int = 3, timeout_seconds: int = 300):
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.active_tasks: Dict[str, asyncio.Task] = {}
        
    async def run_parallel(self, alternatives: List[Dict[str, Any]], 
                          execution_func: Callable,
                          strategy: ExecutionStrategy = ExecutionStrategy.RACE) -> List[AlternativeResult]:
        """Запустить альтернативы параллельно"""
        
        if len(alternatives) > self.max_concurrent:
            logger.warning(f"⚠️ Too many alternatives ({len(alternatives)}), "
                         f"limiting to {self.max_concurrent}")
            alternatives = alternatives[:self.max_concurrent]
        
        # Создаем задачи для каждой альтернативы
        tasks = {}
        for i, alternative in enumerate(alternatives):
            task_id = f"alternative_{i}"
            task = asyncio.create_task(
                self._execute_alternative(task_id, alternative, execution_func)
            )
            tasks[task_id] = task
            self.active_tasks[task_id] = task
        
        try:
            # Выполняем согласно стратегии
            if strategy == ExecutionStrategy.RACE:
                return await self._race_strategy(tasks)
            elif strategy == ExecutionStrategy.ALL_COMPLETE:
                return await self._all_complete_strategy(tasks)
            elif strategy == ExecutionStrategy.FASTEST:
                return await self._fastest_strategy(tasks)
            elif strategy == ExecutionStrategy.BEST_QUALITY:
                return await self._best_quality_strategy(tasks)
            elif strategy == ExecutionStrategy.CONSENSUS:
                return await self._consensus_strategy(tasks)
            else:
                logger.error(f"❌ Unknown execution strategy: {strategy}")
                return await self._race_strategy(tasks)  # Fallback
                
        finally:
            # Отменяем незавершенные задачи
            await self._cleanup_tasks(tasks)
    
    async def _execute_alternative(self, alternative_id: str, alternative: Dict[str, Any],
                                  execution_func: Callable) -> AlternativeResult:
        """Выполнить одну альтернативу"""
        
        start_time = datetime.now()
        
        try:
            logger.info(f"🚀 Starting alternative '{alternative_id}'")
            
            # Выполняем функцию с timeout
            step_result = await asyncio.wait_for(
                execution_func(alternative),
                timeout=self.timeout_seconds
            )
            
            execution_time = (datetime.now() - start_time).total_seconds()
            success = step_result.status == StepStatus.COMPLETED
            
            logger.info(f"✅ Alternative '{alternative_id}' completed in {execution_time:.1f}s, "
                       f"success: {success}")
            
            return AlternativeResult(alternative_id, step_result, execution_time, success)
            
        except asyncio.TimeoutError:
            execution_time = (datetime.now() - start_time).total_seconds()
            logger.error(f"⏰ Alternative '{alternative_id}' timed out after {execution_time:.1f}s")
            
            # Создаем результат с timeout ошибкой
            timeout_result = StepResult(
                step_id=alternative.get("step_id", alternative_id),
                status=StepStatus.FAILED,
                error=f"Timeout after {self.timeout_seconds}s",
                start_time=start_time,
                end_time=datetime.now(),
                duration_seconds=execution_time
            )
            
            return AlternativeResult(alternative_id, timeout_result, execution_time, False)
            
        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            logger.error(f"❌ Alternative '{alternative_id}' failed: {e}")
            
            # Создаем результат с ошибкой
            error_result = StepResult(
                step_id=alternative.get("step_id", alternative_id),
                status=StepStatus.FAILED,
                error=str(e),
                start_time=start_time,
                end_time=datetime.now(),
                duration_seconds=execution_time
            )
            
            return AlternativeResult(alternative_id, error_result, execution_time, False)
    
    async def _race_strategy(self, tasks: Dict[str, asyncio.Task]) -> List[AlternativeResult]:
        """Стратегия race - первый успешный результат"""
        
        results = []
        pending = set(tasks.values())
        
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            
            for task in done:
                try:
                    result = await task
                    results.append(result)
                    
                    # Если нашли успешный результат, останавливаемся
                    if result.success:
                        logger.info(f"🏆 Race winner: {result.alternative_id}")
                        return results
                        
                except Exception as e:
                    logger.error(f"❌ Task failed in race: {e}")
        
        return results
    
    async def _all_complete_strategy(self, tasks: Dict[str, asyncio.Task]) -> List[AlternativeResult]:
        """Стратегия all_complete - ждем все результаты"""
        
        results = []
        
        for task_id, task in tasks.items():
            try:
                result = await task
                results.append(result)
            except Exception as e:
                logger.error(f"❌ Task {task_id} failed: {e}")
        
        logger.info(f"📊 All alternatives completed: {len(results)} results")
        return results
    
    async def _fastest_strategy(self, tasks: Dict[str, asyncio.Task]) -> List[AlternativeResult]:
        """Стратегия fastest - самый быстрый успешный"""
        
        results = []
        pending = set(tasks.values())
        
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            
            for task in done:
                try:
                    result = await task
                    results.append(result)
                    
                    # Если результат успешный, это наш fastest
                    if result.success:
                        logger.info(f"⚡ Fastest successful: {result.alternative_id} "
                                   f"in {result.execution_time:.1f}s")
                        return results
                        
                except Exception as e:
                    logger.error(f"❌ Task failed in fastest strategy: {e}")
        
        return results
    
    async def _best_quality_strategy(self, tasks: Dict[str, asyncio.Task]) -> List[AlternativeResult]:
        """Стратегия best_quality - ждем все и выбираем лучший"""
        
        # Сначала получаем все результаты
        all_results = await self._all_complete_strategy(tasks)
        
        # Фильтруем успешные
        successful_results = [r for r in all_results if r.success]
        
        if not successful_results:
            return all_results
        
        # Находим лучший по качеству
        best_result = max(successful_results, 
                         key=lambda r: getattr(r.step_result, 'quality_score', 0.0))
        
        logger.info(f"🎯 Best quality: {best_result.alternative_id} "
                   f"with score {getattr(best_result.step_result, 'quality_score', 0.0):.2f}")
        
        return [best_result]
    
    async def _consensus_strategy(self, tasks: Dict[str, asyncio.Task]) -> List[AlternativeResult]:
        """Стратегия consensus - консенсус между результатами"""
        
        # Получаем все результаты
        all_results = await self._all_complete_strategy(tasks)
        successful_results = [r for r in all_results if r.success]
        
        if len(successful_results) < 2:
            return all_results
        
        # Простой консенсус по схожести результатов
        consensus_result = self._find_consensus(successful_results)
        
        if consensus_result:
            logger.info(f"🤝 Consensus reached: {consensus_result.alternative_id}")
            return [consensus_result]
        
        # Если консенсус не достигнут, возвращаем лучший по качеству
        return await self._best_quality_strategy(tasks)
    
    def _find_consensus(self, results: List[AlternativeResult]) -> Optional[AlternativeResult]:
        """Найти консенсус между результатами"""
        
        # Простая реализация: находим результат с наиболее схожими выходами
        if len(results) < 2:
            return results[0] if results else None
        
        # Сравниваем выходы по длине и содержанию
        similarity_scores = {}
        
        for i, result_a in enumerate(results):
            similarity_scores[i] = 0
            output_a = str(result_a.step_result.output or "")
            
            for j, result_b in enumerate(results):
                if i == j:
                    continue
                
                output_b = str(result_b.step_result.output or "")
                
                # Простая мера схожести
                similarity = self._calculate_similarity(output_a, output_b)
                similarity_scores[i] += similarity
        
        # Находим результат с максимальной схожестью с другими
        if similarity_scores:
            best_index = max(similarity_scores.keys(), key=lambda k: similarity_scores[k])
            return results[best_index]
        
        return None
    
    def _calculate_similarity(self, text_a: str, text_b: str) -> float:
        """Вычислить схожесть двух текстов"""
        
        if not text_a or not text_b:
            return 0.0
        
        # Простая схожесть по длине и общим словам
        len_similarity = 1.0 - abs(len(text_a) - len(text_b)) / max(len(text_a), len(text_b))
        
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        
        if not words_a or not words_b:
            return len_similarity * 0.5
        
        word_similarity = len(words_a & words_b) / len(words_a | words_b)
        
        return (len_similarity + word_similarity) / 2
    
    async def _cleanup_tasks(self, tasks: Dict[str, asyncio.Task]):
        """Отменить незавершенные задачи"""
        
        for task_id, task in tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"🚫 Cancelled task {task_id}")
            
            # Удаляем из активных задач
            self.active_tasks.pop(task_id, None)


class AlternativeExecutor:
    """Основной исполнитель альтернативных стратегий"""
    
    def __init__(self):
        self.parallel_runner = ParallelRunner()
        self.execution_history: List[Dict[str, Any]] = []
        
    async def execute_alternatives(self, step: WorkflowStep, context: WorkflowContext,
                                  alternatives: List[Dict[str, Any]],
                                  strategy: ExecutionStrategy = ExecutionStrategy.RACE) -> StepResult:
        """Выполнить альтернативные подходы к шагу"""
        
        if not alternatives:
            raise ValueError("No alternatives provided")
        
        logger.info(f"🔀 Executing {len(alternatives)} alternatives for step '{step.id}' "
                   f"with strategy '{strategy.value}'")
        
        start_time = datetime.now()
        
        try:
            # Создаем функцию выполнения для альтернатив
            async def execute_alternative(alternative_config: Dict[str, Any]) -> StepResult:
                return await self._execute_single_alternative(step, context, alternative_config)
            
            # Запускаем параллельное выполнение
            alternative_results = await self.parallel_runner.run_parallel(
                alternatives, execute_alternative, strategy
            )
            
            # Выбираем финальный результат
            final_result = self._select_final_result(alternative_results, strategy)
            
            # Записываем в историю
            self._record_execution(step.id, alternatives, alternative_results, 
                                 final_result, strategy, start_time)
            
            logger.info(f"✅ Alternatives execution completed for step '{step.id}': "
                       f"selected {final_result.alternative_id if final_result else 'none'}")
            
            return final_result.step_result if final_result else self._create_failure_result(step, start_time)
            
        except Exception as e:
            logger.error(f"❌ Alternatives execution failed for step '{step.id}': {e}")
            return self._create_failure_result(step, start_time, str(e))
    
    async def _execute_single_alternative(self, step: WorkflowStep, context: WorkflowContext,
                                         alternative_config: Dict[str, Any]) -> StepResult:
        """Выполнить одну альтернативу"""
        
        # Модифицируем шаг согласно альтернативной конфигурации
        modified_step = self._apply_alternative_config(step, alternative_config)
        
        # TODO: Здесь должен быть вызов реального execution engine
        # Пока создаем мок результат
        import random
        import time
        
        # Симулируем выполнение
        await asyncio.sleep(random.uniform(0.5, 3.0))
        
        # Симулируем разные исходы
        success_rate = alternative_config.get("success_rate", 0.7)
        success = random.random() < success_rate
        
        if success:
            quality_score = random.uniform(0.6, 1.0)
            output = f"Alternative result from {alternative_config.get('agent_type', 'default')} agent"
            
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output=output,
                start_time=datetime.now() - timedelta(seconds=2),
                end_time=datetime.now(),
                duration_seconds=2.0,
                quality_score=quality_score,
                agent_name=alternative_config.get("agent_type", "alternative")
            )
        else:
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                error="Alternative execution failed",
                start_time=datetime.now() - timedelta(seconds=1),
                end_time=datetime.now(),
                duration_seconds=1.0,
                agent_name=alternative_config.get("agent_type", "alternative")
            )
    
    def _apply_alternative_config(self, step: WorkflowStep, config: Dict[str, Any]) -> WorkflowStep:
        """Применить альтернативную конфигурацию к шагу"""
        
        # Создаем копию шага
        modified_step = WorkflowStep(
            id=step.id,
            agent_type=config.get("agent_type", step.agent_type),
            task=config.get("task", step.task),
            depends_on=step.depends_on,
            condition=step.condition,
            retry_policy=step.retry_policy,
            resource_limits=step.resource_limits,
            timeout=config.get("timeout", step.timeout),
            rollback_action=step.rollback_action,
            metadata={**step.metadata, **config.get("metadata", {})}
        )
        
        return modified_step
    
    def _select_final_result(self, alternative_results: List[AlternativeResult],
                           strategy: ExecutionStrategy) -> Optional[AlternativeResult]:
        """Выбрать финальный результат из альтернатив"""
        
        if not alternative_results:
            return None
        
        # Для большинства стратегий результат уже выбран в ParallelRunner
        if len(alternative_results) == 1:
            return alternative_results[0]
        
        # Дополнительная логика выбора если нужна
        successful_results = [r for r in alternative_results if r.success]
        
        if successful_results:
            if strategy == ExecutionStrategy.BEST_QUALITY:
                return max(successful_results, 
                          key=lambda r: getattr(r.step_result, 'quality_score', 0.0))
            elif strategy == ExecutionStrategy.FASTEST:
                return min(successful_results, key=lambda r: r.execution_time)
            else:
                return successful_results[0]
        
        # Если нет успешных, возвращаем первый
        return alternative_results[0]
    
    def _create_failure_result(self, step: WorkflowStep, start_time: datetime,
                             error: str = "All alternatives failed") -> StepResult:
        """Создать результат неудачи"""
        
        return StepResult(
            step_id=step.id,
            status=StepStatus.FAILED,
            error=error,
            start_time=start_time,
            end_time=datetime.now(),
            duration_seconds=(datetime.now() - start_time).total_seconds()
        )
    
    def _record_execution(self, step_id: str, alternatives: List[Dict[str, Any]],
                         results: List[AlternativeResult], final_result: Optional[AlternativeResult],
                         strategy: ExecutionStrategy, start_time: datetime):
        """Записать выполнение в историю"""
        
        execution_record = {
            "timestamp": start_time.isoformat(),
            "step_id": step_id,
            "strategy": strategy.value,
            "alternatives_count": len(alternatives),
            "results_count": len(results),
            "successful_results": len([r for r in results if r.success]),
            "final_result_id": final_result.alternative_id if final_result else None,
            "total_execution_time": (datetime.now() - start_time).total_seconds(),
            "alternative_configs": [
                {
                    "agent_type": alt.get("agent_type", "unknown"),
                    "timeout": alt.get("timeout", 0)
                }
                for alt in alternatives
            ]
        }
        
        self.execution_history.append(execution_record)
        
        # Ограничиваем размер истории
        if len(self.execution_history) > 100:
            self.execution_history = self.execution_history[-50:]
    
    def get_execution_statistics(self) -> Dict[str, Any]:
        """Получить статистику выполнения альтернатив"""
        
        if not self.execution_history:
            return {"message": "No execution history available"}
        
        total_executions = len(self.execution_history)
        successful_executions = len([r for r in self.execution_history if r["successful_results"] > 0])
        
        # Статистика по стратегиям
        strategy_stats = {}
        for record in self.execution_history:
            strategy = record["strategy"]
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"count": 0, "successful": 0, "avg_time": 0.0}
            
            strategy_stats[strategy]["count"] += 1
            if record["successful_results"] > 0:
                strategy_stats[strategy]["successful"] += 1
            strategy_stats[strategy]["avg_time"] += record["total_execution_time"]
        
        # Вычисляем средние времена
        for stats in strategy_stats.values():
            if stats["count"] > 0:
                stats["avg_time"] /= stats["count"]
                stats["success_rate"] = stats["successful"] / stats["count"]
        
        return {
            "total_executions": total_executions,
            "successful_executions": successful_executions,
            "overall_success_rate": successful_executions / total_executions if total_executions > 0 else 0,
            "strategy_statistics": strategy_stats,
            "recent_executions": self.execution_history[-5:]  # Последние 5
        }
