"""
Final Aggregator для сборки итогового ответа workflow
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models import StepResult, WorkflowDefinition, WorkflowContext

logger = logging.getLogger(__name__)


class FinalAggregator:
    """Агрегатор для сборки финального результата workflow"""
    
    def __init__(self):
        self.aggregation_strategies = {
            "research": self._aggregate_research_workflow,
            "analysis": self._aggregate_analysis_workflow,
            "sql_generation": self._aggregate_sql_workflow,
            "content_creation": self._aggregate_content_workflow,
            "default": self._aggregate_default_workflow
        }
    
    async def aggregate_final_result(self, step_results: Dict[str, StepResult],
                                   workflow_def: WorkflowDefinition,
                                   context: WorkflowContext) -> Dict[str, Any]:
        """Собрать финальный результат workflow"""
        
        try:
            logger.info(f"📋 Aggregating final result for workflow '{workflow_def.name}'")

            if workflow_def.outputs:
                return await self._aggregate_outputs_mapping(step_results, workflow_def)

            # Определяем стратегию агрегации
            strategy = await self._determine_aggregation_strategy(workflow_def, step_results)
            
            # Фильтруем успешные результаты
            successful_results = await self._filter_successful_results(step_results)
            
            # Применяем стратегию агрегации
            aggregated_result = await strategy(successful_results, workflow_def, context)
            
            # Добавляем метаданные
            final_result = await self._add_metadata(aggregated_result, step_results, workflow_def)
            
            # Валидируем финальный результат
            validation_result = await self._validate_final_result(final_result, workflow_def)
            
            logger.info(f"✅ Final result aggregated: {len(successful_results)} steps, "
                       f"quality score: {validation_result.get('quality_score', 'N/A')}")
            
            return final_result
            
        except Exception as e:
            logger.error(f"❌ Failed to aggregate final result: {e}")
            # Возвращаем базовый результат
            return await self._create_fallback_result(step_results, workflow_def, str(e))

    async def _aggregate_outputs_mapping(self, step_results: Dict[str, StepResult],
                                         workflow_def: WorkflowDefinition) -> Dict[str, Any]:
        """Агрегация по outputs-маппингу из YAML"""
        outputs_map: Dict[str, Any] = {}
        for key, mapping in workflow_def.outputs.items():
            value = self._resolve_output_mapping(mapping, step_results)
            outputs_map[key] = value
        final_value = outputs_map.get("final")
        if final_value is None and outputs_map:
            first_key = next(iter(outputs_map))
            final_value = outputs_map.get(first_key)
        return {
            "type": "workflow_outputs",
            "workflow_name": workflow_def.name,
            "final": final_value,
            "outputs": outputs_map,
        }

    def _resolve_output_mapping(self, mapping: Any, step_results: Dict[str, StepResult]) -> Any:
        """Разрешить одно правило outputs-маппинга."""
        if isinstance(mapping, str):
            mapping = {"from_step": mapping}
        if not isinstance(mapping, dict):
            return mapping
        step_id = mapping.get("from_step")
        if not step_id:
            return mapping.get("default")
        step_result = step_results.get(step_id)
        if not step_result:
            return mapping.get("default")
        field = mapping.get("field", "output")
        value = None
        if field == "output":
            value = step_result.output
        elif field == "metadata":
            value = step_result.metadata
        elif field == "resource_usage":
            value = step_result.resource_usage
        else:
            value = getattr(step_result, field, None)
        path = mapping.get("path")
        if path:
            value = self._resolve_path(value, path, mapping.get("default"))
        return value if value is not None else mapping.get("default")

    def _resolve_path(self, value: Any, path: str, default: Any) -> Any:
        """Извлечь значение по пути вида a.b.0.c"""
        current = value
        for part in str(path).split("."):
            if current is None:
                return default
            if isinstance(current, dict):
                current = current.get(part)
                continue
            if isinstance(current, list):
                try:
                    idx = int(part)
                except ValueError:
                    return default
                if idx < 0 or idx >= len(current):
                    return default
                current = current[idx]
                continue
            return default
        return current if current is not None else default
    
    async def _determine_aggregation_strategy(self, workflow_def: WorkflowDefinition,
                                            step_results: Dict[str, StepResult]):
        """Определить стратегию агрегации"""
        
        # Анализируем метаданные workflow
        category = workflow_def.metadata.get("category", "").lower()
        
        if "research" in category or "researcher" in str(workflow_def.steps):
            return self.aggregation_strategies["research"]
        elif "analysis" in category or "analyst" in str(workflow_def.steps):
            return self.aggregation_strategies["analysis"]
        elif "sql" in category or "sql_generator" in str(workflow_def.steps):
            return self.aggregation_strategies["sql_generation"]
        elif "content" in category or "content_creation" in workflow_def.name.lower():
            return self.aggregation_strategies["content_creation"]
        else:
            return self.aggregation_strategies["default"]
    
    async def _filter_successful_results(self, step_results: Dict[str, StepResult]) -> Dict[str, StepResult]:
        """Фильтровать успешные результаты"""
        
        successful = {}
        
        for step_id, result in step_results.items():
            # Включаем все успешно завершенные шаги с выводом
            # quality_score может быть 0.0 для инфраструктурных шагов
            if (result.status.value == "completed" and 
                result.output is not None):
                successful[step_id] = result
            else:
                logger.warning(f"⚠️ Excluding step '{step_id}' from aggregation: "
                             f"status={result.status.value}, quality={result.quality_score}")
        
        return successful
    
    async def _aggregate_research_workflow(self, step_results: Dict[str, StepResult],
                                         workflow_def: WorkflowDefinition,
                                         context: WorkflowContext) -> Dict[str, Any]:
        """Агрегация для исследовательского workflow"""
        
        result = {
            "type": "research_report",
            "topic": context.variables.get("topic", "Unknown topic"),
            "summary": "",
            "key_findings": [],
            "detailed_analysis": "",
            "sources": [],
            "recommendations": [],
            "confidence_level": 0.0
        }
        
        # Собираем основные компоненты
        research_step = self._find_step_by_type(step_results, ["researcher", "research"])
        analysis_step = self._find_step_by_type(step_results, ["analyst", "analyze"])
        
        if research_step:
            research_output = research_step.output
            if isinstance(research_output, dict):
                result["key_findings"] = research_output.get("key_findings", [])
                result["sources"] = research_output.get("sources", [])
            else:
                result["summary"] = str(research_output)[:500] + "..."
        
        if analysis_step:
            analysis_output = analysis_step.output
            if isinstance(analysis_output, dict):
                result["detailed_analysis"] = analysis_output.get("analysis", "")
                result["recommendations"] = analysis_output.get("recommendations", [])
            else:
                result["detailed_analysis"] = str(analysis_output)
        
        # Вычисляем общий уровень уверенности
        quality_scores = [r.quality_score for r in step_results.values() if r.quality_score > 0]
        if quality_scores:
            result["confidence_level"] = sum(quality_scores) / len(quality_scores)
        
        return result
    
    async def _aggregate_analysis_workflow(self, step_results: Dict[str, StepResult],
                                         workflow_def: WorkflowDefinition,
                                         context: WorkflowContext) -> Dict[str, Any]:
        """Агрегация для аналитического workflow"""
        
        result = {
            "type": "analysis_report",
            "request": context.variables.get("analysis_request", "Analysis request"),
            "executive_summary": "",
            "methodology": "",
            "findings": [],
            "visualizations": [],
            "recommendations": [],
            "data_quality": {},
            "confidence_metrics": {}
        }
        
        # Собираем компоненты анализа
        sql_step = self._find_step_by_type(step_results, ["sql_generator", "generate_sql"])
        execution_step = self._find_step_by_type(step_results, ["code_executor", "execute"])
        analysis_step = self._find_step_by_type(step_results, ["analyst", "analyze"])
        viz_step = self._find_step_by_type(step_results, ["visualizer", "visualization"])
        
        if sql_step and isinstance(sql_step.output, dict):
            result["methodology"] = sql_step.output.get("explanation", "")
        
        if execution_step:
            result["data_quality"]["execution_status"] = "success"
            result["data_quality"]["rows_processed"] = len(str(execution_step.output))
        
        if analysis_step:
            analysis_output = analysis_step.output
            if isinstance(analysis_output, dict):
                result["findings"] = analysis_output.get("findings", [])
                result["recommendations"] = analysis_output.get("recommendations", [])
                result["executive_summary"] = analysis_output.get("summary", "")
            else:
                result["executive_summary"] = str(analysis_output)[:300] + "..."
        
        if viz_step:
            result["visualizations"] = [{"type": "chart", "description": str(viz_step.output)}]
        
        # Метрики уверенности
        for step_id, step_result in step_results.items():
            result["confidence_metrics"][step_id] = {
                "quality_score": step_result.quality_score,
                "retry_count": step_result.retry_count
            }
        
        return result
    
    async def _aggregate_sql_workflow(self, step_results: Dict[str, StepResult],
                                    workflow_def: WorkflowDefinition,
                                    context: WorkflowContext) -> Dict[str, Any]:
        """Агрегация для SQL workflow"""
        
        result = {
            "type": "sql_result",
            "request": context.variables.get("analysis_request", "SQL request"),
            "sql_query": "",
            "explanation": "",
            "execution_result": None,
            "performance_metrics": {},
            "validation_status": "unknown"
        }
        
        # Находим ключевые шаги
        sql_step = self._find_step_by_type(step_results, ["sql_generator", "generate_sql"])
        verify_step = self._find_step_by_type(step_results, ["sql_verifier", "verify"])
        execution_step = self._find_step_by_type(step_results, ["code_executor", "execute"])
        
        if sql_step:
            sql_output = sql_step.output
            if isinstance(sql_output, dict):
                result["sql_query"] = sql_output.get("query", "")
                result["explanation"] = sql_output.get("explanation", "")
            else:
                result["sql_query"] = str(sql_output)
        
        if verify_step:
            result["validation_status"] = "verified" if verify_step.quality_score > 0.8 else "needs_review"
        
        if execution_step:
            result["execution_result"] = execution_step.output
            result["performance_metrics"] = {
                "execution_time": execution_step.duration_seconds,
                "quality_score": execution_step.quality_score
            }
        
        return result
    
    async def _aggregate_content_workflow(self, step_results: Dict[str, StepResult],
                                        workflow_def: WorkflowDefinition,
                                        context: WorkflowContext) -> Dict[str, Any]:
        """Агрегация для контент-создающего workflow"""
        
        result = {
            "type": "content_result",
            "title": context.variables.get("topic", "Generated Content"),
            "content": "",
            "sections": [],
            "word_count": 0,
            "quality_assessment": {}
        }
        
        # Собираем контент из всех шагов
        content_parts = []
        
        for step_id, step_result in step_results.items():
            if step_result.output and isinstance(step_result.output, str):
                content_parts.append({
                    "step": step_id,
                    "content": step_result.output,
                    "quality": step_result.quality_score
                })
        
        # Сортируем по качеству и объединяем
        content_parts.sort(key=lambda x: x["quality"], reverse=True)
        
        if content_parts:
            result["content"] = "\n\n".join(part["content"] for part in content_parts)
            result["word_count"] = len(result["content"].split())
            result["sections"] = [{"title": part["step"], "quality": part["quality"]} 
                                for part in content_parts]
        
        # Оценка качества
        if content_parts:
            avg_quality = sum(part["quality"] for part in content_parts) / len(content_parts)
            result["quality_assessment"] = {
                "average_quality": avg_quality,
                "sections_count": len(content_parts),
                "total_words": result["word_count"]
            }
        
        return result
    
    async def _aggregate_default_workflow(self, step_results: Dict[str, StepResult],
                                        workflow_def: WorkflowDefinition,
                                        context: WorkflowContext) -> Dict[str, Any]:
        """Дефолтная агрегация"""
        
        result = {
            "type": "workflow_result",
            "workflow_name": workflow_def.name,
            "summary": "",
            "outputs": {},
            "quality_metrics": {},
            "execution_path": []
        }
        
        # Собираем все выходы
        for step_id, step_result in step_results.items():
            result["outputs"][step_id] = {
                "output": step_result.output,
                "quality_score": step_result.quality_score,
                "duration": step_result.duration_seconds
            }
            
            result["execution_path"].append({
                "step_id": step_id,
                "status": step_result.status.value,
                "quality": step_result.quality_score
            })
        
        # Создаем краткое резюме
        successful_steps = len([r for r in step_results.values() if r.status.value == "completed"])
        avg_quality = (sum(r.quality_score for r in step_results.values()) / len(step_results)) if step_results else 0.0
        
        result["summary"] = (f"Workflow '{workflow_def.name}' completed {successful_steps} steps "
                           f"with average quality {avg_quality:.2f}")
        
        result["quality_metrics"] = {
            "average_quality": avg_quality,
            "successful_steps": successful_steps,
            "total_steps": len(step_results)
        }
        
        return result
    
    def _find_step_by_type(self, step_results: Dict[str, StepResult], 
                          keywords: List[str]) -> Optional[StepResult]:
        """Найти шаг по ключевым словам в ID"""
        
        for step_id, result in step_results.items():
            step_id_lower = step_id.lower()
            if any(keyword.lower() in step_id_lower for keyword in keywords):
                return result
        
        return None
    
    async def _add_metadata(self, aggregated_result: Dict[str, Any],
                           step_results: Dict[str, StepResult],
                           workflow_def: WorkflowDefinition) -> Dict[str, Any]:
        """Добавить метаданные к результату"""
        
        aggregated_result["metadata"] = {
            "workflow_name": workflow_def.name,
            "workflow_version": workflow_def.version,
            "completion_time": datetime.now().isoformat(),
            "total_steps": len(step_results),
            "successful_steps": len([r for r in step_results.values() if r.status.value == "completed"]),
            "average_quality": sum(r.quality_score for r in step_results.values()) / len(step_results) if step_results else 0,
            "total_duration": sum(r.duration_seconds or 0 for r in step_results.values()),
            "retry_count": sum(r.retry_count for r in step_results.values())
        }
        
        return aggregated_result
    
    async def _validate_final_result(self, final_result: Dict[str, Any],
                                   workflow_def: WorkflowDefinition) -> Dict[str, Any]:
        """Валидация финального результата"""
        
        validation = {
            "is_valid": True,
            "quality_score": 0.8,  # Базовая оценка
            "issues": [],
            "completeness": 1.0
        }
        
        # Проверяем наличие ключевых компонентов
        if "type" not in final_result:
            validation["issues"].append("Missing result type")
            validation["quality_score"] -= 0.1
        
        if "metadata" not in final_result:
            validation["issues"].append("Missing metadata")
            validation["quality_score"] -= 0.05
        
        # Проверяем полноту в зависимости от типа
        result_type = final_result.get("type", "")
        
        if result_type == "research_report":
            required_fields = ["topic", "key_findings", "sources"]
            for field in required_fields:
                if not final_result.get(field):
                    validation["issues"].append(f"Missing {field}")
                    validation["quality_score"] -= 0.1
        
        elif result_type == "analysis_report":
            required_fields = ["findings", "methodology"]
            for field in required_fields:
                if not final_result.get(field):
                    validation["issues"].append(f"Missing {field}")
                    validation["quality_score"] -= 0.1
        
        validation["quality_score"] = max(0.0, validation["quality_score"])
        validation["is_valid"] = len(validation["issues"]) == 0
        
        return validation
    
    async def _create_fallback_result(self, step_results: Dict[str, StepResult],
                                    workflow_def: WorkflowDefinition,
                                    error: str) -> Dict[str, Any]:
        """Создать fallback результат при ошибке агрегации"""
        
        return {
            "type": "fallback_result",
            "error": f"Aggregation failed: {error}",
            "workflow_name": workflow_def.name,
            "raw_outputs": {
                step_id: {
                    "output": str(result.output)[:200] + "..." if result.output else None,
                    "status": result.status.value,
                    "quality": result.quality_score
                }
                for step_id, result in step_results.items()
            },
            "metadata": {
                "completion_time": datetime.now().isoformat(),
                "fallback_reason": "aggregation_error"
            }
        }
