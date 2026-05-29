"""
Интеграция с storybook pipeline
==============================

Модуль для запуска workflow и отдельных операций генерации.
Интегрируется с существующим workflow engine и custom tools.
"""

import asyncio
import copy
import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List, Set
import logging

# Добавляем путь к основному проекту для импорта workflow
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Интеграция с workflow engine для запуска pipeline"""

    def __init__(self):
        self.engine = None
        self.current_workflow_id: Optional[str] = None
        self._original_on_step_completed = None
        self._original_execute_workflow_step = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # не на паузе по умолчанию
        self._initialize_engine()
    
    def _initialize_engine(self):
        """Инициализация workflow engine"""
        try:
            from workflow.enhanced_engine import EnhancedWorkflowEngine
            self.engine = EnhancedWorkflowEngine()
            logger.info("✅ Workflow engine инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации workflow engine: {e}")
            self.engine = None
    
    def _install_step_hook(self, progress_callback: Callable, total_steps: int):
        """Оборачивает engine-хуки для step tracker, progress и паузы."""
        if not self.engine or not progress_callback:
            return
        self._original_on_step_completed = self.engine._on_step_completed
        self._original_execute_workflow_step = self.engine._execute_workflow_step
        completed_count = [0]
        original = self._original_on_step_completed
        original_execute_step = self._original_execute_workflow_step

        async def _hooked_execute_workflow_step(step, context, workflow_def):
            try:
                progress_callback(
                    message=f"Шаг '{step.id}' выполняется",
                    step_id=step.id,
                    step_status="running",
                )
            except Exception as e:
                logger.error(f"Ошибка в progress_callback для запуска шага {step.id}: {e}")

            return await original_execute_step(step, context, workflow_def)

        async def _hooked_on_step_completed(workflow_id, step, step_result, context, step_results):
            await original(workflow_id, step, step_result, context, step_results)
            completed_count[0] += 1
            progress = (completed_count[0] / total_steps) * 100 if total_steps > 0 else 0
            duration = step_result.duration_seconds if hasattr(step_result, 'duration_seconds') else None
            status = step_result.status.value if hasattr(step_result.status, 'value') else str(step_result.status)
            try:
                progress_callback(
                    message=f"Шаг '{step.id}' завершён ({status})",
                    progress=progress,
                    step_id=step.id,
                    step_status=status,
                    step_duration=duration,
                )
            except Exception as e:
                logger.error(f"Ошибка в progress_callback для шага {step.id}: {e}")

            # Если pipeline на паузе — блокируем до resume
            if not self._pause_event.is_set():
                logger.info(f"⏸ Pipeline приостановлен после шага '{step.id}'")
                loop = asyncio.get_running_loop()
                # self._pause_event.wait() выполняем через executor, чтобы не блокировать event loop.
                await loop.run_in_executor(None, self._pause_event.wait)
                logger.info("▶ Pipeline возобновлён")

        self.engine._execute_workflow_step = _hooked_execute_workflow_step
        self.engine._on_step_completed = _hooked_on_step_completed

    def _uninstall_step_hook(self):
        """Восстанавливает оригинальные engine-хуки."""
        if self._original_on_step_completed is not None and self.engine:
            self.engine._on_step_completed = self._original_on_step_completed
            self._original_on_step_completed = None
        if self._original_execute_workflow_step is not None and self.engine:
            self.engine._execute_workflow_step = self._original_execute_workflow_step
            self._original_execute_workflow_step = None

    @staticmethod
    def _normalize_workflow_status(result: Any) -> Optional[str]:
        """Нормализует status WorkflowResult/словаря к строке."""
        if result is None:
            return None

        status = getattr(result, "status", None)
        value = getattr(status, "value", None)
        if isinstance(value, str):
            return value
        if isinstance(status, str):
            return status
        if isinstance(result, dict):
            status = result.get("status")
            value = getattr(status, "value", None)
            if isinstance(value, str):
                return value
            if isinstance(status, str):
                return status
        return None

    @classmethod
    def _build_runner_response(
        cls,
        result: Any,
        *,
        success_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Преобразует WorkflowResult в контракт StoryBookManager."""
        status = cls._normalize_workflow_status(result)
        if status == "cancelled":
            payload = dict(success_payload)
            payload["status"] = "cancelled"
            payload["result"] = result
            return payload

        if status in {None, "completed"}:
            payload = dict(success_payload)
            payload["status"] = "success"
            payload["result"] = result
            return payload

        error_message = getattr(result, "error", None)
        if not error_message and isinstance(result, dict):
            error_message = result.get("message")
        if not error_message:
            error_message = f"Workflow завершился со статусом '{status}'"
        return {"status": "error", "message": error_message, "result": result}

    @staticmethod
    def _step_output_artifacts() -> Dict[str, List[str]]:
        """Канонические артефакты storybook pipeline для проверки частичного запуска."""
        # TODO: перенести ожидаемые артефакты шагов в pipeline metadata/source of truth,
        # чтобы валидация частичного запуска не зависела от локального словаря путей.
        return {
            "brief_from_prompt": ["00_brief.json"],
            "init_project": ["00_brief.json"],
            "story_planner": ["10_synopsis/synopsis.json", "10_synopsis/beats.json"],
            "bible_builder": [
                "20_bible/characters.json",
                "20_bible/locations.json",
                "20_bible/consistency_rules.json",
            ],
            "style_keeper": ["30_style/style_text.json", "30_style/style_images.json"],
            "story_writer": ["20_story/story.json"],
            "story_editor": ["20_story/story.json"],
            "prompt_engineer": ["40_prompts"],
            "items_builder": ["40_prompts"],
            "artist_batch": ["50_images"],
            "assemble_md": ["90_md/book.md"],
            "md_to_pdf": ["95_pdf/book.pdf"],
            "screenplay_generator": ["91_screenplay/screenplay.json"],
            "screenplay_shots_generator": ["97_shots/shots.json"],
            "shots_prompt_qa": ["97_shots/shots.json"],
            "artist_batch_shots": ["97_shots"],
        }

    @classmethod
    def _collect_required_artifacts(
        cls,
        workflow_def,
        start_step: str,
    ) -> List[str]:
        """Возвращает список артефактов, обязательных перед запуском start_step."""
        step_map = {step.id: step for step in workflow_def.steps}
        artifact_map = cls._step_output_artifacts()
        visited: Set[str] = set()
        visiting: Set[str] = set()
        required: List[str] = []

        def _require_dependency(step_id: str):
            if step_id == start_step or step_id in visited or step_id in visiting:
                return
            if step_id not in step_map:
                return
            visiting.add(step_id)
            for dep_id in step_map[step_id].depends_on:
                _require_dependency(dep_id)
            visiting.remove(step_id)
            visited.add(step_id)
            required.extend(artifact_map.get(step_id, []))

        start = step_map.get(start_step)
        if start is not None:
            for dep_id in start.depends_on:
                _require_dependency(dep_id)

        unique_required: List[str] = []
        seen: Set[str] = set()
        for artifact in required:
            if artifact not in seen:
                seen.add(artifact)
                unique_required.append(artifact)
        return unique_required

    async def run_full_pipeline(self, project_id: str, task: str,
                               progress_callback: Optional[Callable] = None,
                               input_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Запускает полный storybook pipeline
        
        Args:
            project_id: ID проекта
            task: Описание сказки для генерации
            progress_callback: Функция для отслеживания прогресса
        
        Returns:
            Результат выполнения workflow
        """
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}
        
        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Запуск полного pipeline для проекта {project_id}")

            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"

            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowContext, WorkflowDefinition
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            total_steps = len(workflow_def.steps)

            if progress_callback:
                self._install_step_hook(progress_callback, total_steps)

            execution_variables = {"project_id": project_id, "task": task}
            execution_variables.update(input_overrides)

            self.current_workflow_id = f"sbm_{project_id}_{uuid.uuid4().hex[:8]}"
            context = WorkflowContext(
                workflow_id=self.current_workflow_id,
                session_id=project_id,
                variables=execution_variables.copy()
            )

            result = await self.engine.execute_workflow_from_yaml(
                yaml_path=str(yaml_path),
                context=context,
                **execution_variables
            )

            logger.info(f"✅ Pipeline завершен для проекта {project_id}")
            return self._build_runner_response(
                result,
                success_payload={
                    "project_id": project_id,
                    "task": task,
                },
            )

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения pipeline: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._uninstall_step_hook()
            self.current_workflow_id = None
    
    @staticmethod
    def validate_step_dependencies(workflow_def, start_step_id: str) -> list:
        """Проверяет что все зависимости start_step_id входят в пропускаемые шаги.

        Возвращает список неудовлетворённых зависимостей (пустой = ОК).
        Зависимости читаются из depends_on в YAML.
        """
        step_ids = [s.id for s in workflow_def.steps]
        if start_step_id not in step_ids:
            return []

        start_index = step_ids.index(start_step_id)
        skipped_ids = set(step_ids[:start_index])
        included_ids = set(step_ids[start_index:])

        # Собираем все зависимости включённых шагов
        missing = []
        for step in workflow_def.steps[start_index:]:
            for dep in step.depends_on:
                if dep in skipped_ids and dep not in included_ids:
                    missing.append(f"Шаг '{step.id}' зависит от '{dep}', "
                                   f"который будет пропущен")
        return missing

    async def run_from_step(self, project_id: str, step_id: str,
                           progress_callback: Optional[Callable] = None,
                           task: Optional[str] = None,
                           input_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Запускает pipeline с определенного шага
        
        Args:
            project_id: ID проекта
            step_id: ID шага с которого начать
            progress_callback: Функция для отслеживания прогресса
        
        Returns:
            Результат выполнения workflow
        """
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}
        
        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Запуск pipeline с шага {step_id} для проекта {project_id}")

            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"

            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowDefinition, WorkflowContext
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)

            step_ids = [s.id for s in workflow_def.steps]
            if step_id not in step_ids:
                return {"status": "error", "message": f"Шаг '{step_id}' не найден в pipeline"}

            start_index = step_ids.index(step_id)

            dep_errors = self.validate_step_dependencies(workflow_def, step_id)
            if dep_errors:
                msg = "Неудовлетворённые зависимости:\n" + "\n".join(
                    f"  • {e}" for e in dep_errors
                )
                return {"status": "error", "message": msg}

            for skipped_step in workflow_def.steps[:start_index]:
                logger.info(f"⏭️ Шаг '{skipped_step.id}' будет пропущен (выполнение с {step_id})")

            workflow_def.steps = workflow_def.steps[start_index:]

            workflow_inputs = workflow_def.inputs if isinstance(workflow_def.inputs, dict) else {}
            context_variables = workflow_inputs.copy()
            context_variables["project_id"] = project_id
            if task is not None:
                context_variables["task"] = task
            context_variables.update(input_overrides)

            self.current_workflow_id = f"sbm_partial_{project_id}_{uuid.uuid4().hex[:8]}"
            context = WorkflowContext(
                workflow_id=self.current_workflow_id,
                session_id=project_id,
                variables=context_variables
            )

            result = await self.engine.execute_workflow(workflow_def, context)

            logger.info(f"✅ Partial pipeline завершен для проекта {project_id} с шага {step_id}")
            response = self._build_runner_response(
                result,
                success_payload={
                    "project_id": project_id,
                    "start_step": step_id,
                    "skipped_steps": start_index,
                },
            )
            response.setdefault("skipped_steps", start_index)
            return response

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения pipeline с шага {step_id}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self.current_workflow_id = None

    async def _get_latest_workflow_checkpoint(self, workflow_id: str):
        """Возвращает последний checkpoint по workflow_id."""
        if not self.engine or not getattr(self.engine, "state_manager", None):
            return None

        try:
            return await self.engine.state_manager.store.get_latest_checkpoint(workflow_id)
        except Exception as e:
            logger.error(f"Ошибка получения checkpoint для workflow {workflow_id}: {e}")
            return None

    async def _get_latest_project_checkpoint(self, project_id: str):
        """Возвращает последний checkpoint для проекта по workflow_id."""
        if not self.engine or not getattr(self.engine, "state_manager", None):
            return None

        try:
            import sqlite3

            store = self.engine.state_manager.store
            with sqlite3.connect(store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT workflow_id
                    FROM workflow_checkpoints
                    WHERE workflow_id LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (f"%{project_id}%",),
                ).fetchone()

            if not row:
                return None

            return await store.get_latest_checkpoint(row["workflow_id"])
        except Exception as e:
            logger.error(f"Ошибка получения checkpoint для проекта {project_id}: {e}")
            return None

    async def rerun_single_step(self, project_id: str, step_id: str,
                                progress_callback: Optional[Callable] = None,
                                task: Optional[str] = None,
                                input_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Перезапускает один шаг, используя последний сохранённый контекст проекта."""
        checkpoint = await self._get_latest_project_checkpoint(project_id)
        if not checkpoint or not checkpoint.context:
            return {
                "status": "error",
                "message": (
                    f"Для проекта {project_id} не найден сохранённый checkpoint "
                    f"с контекстом для перезапуска шага '{step_id}'"
                ),
            }

        return await self.run_single_step(
            project_id=project_id,
            step_id=step_id,
            progress_callback=progress_callback,
            task=task,
            input_overrides=input_overrides,
            base_context=checkpoint.context,
            source_workflow_id=checkpoint.workflow_id,
        )

    async def run_single_step(self, project_id: str, step_id: str,
                              progress_callback: Optional[Callable] = None,
                              task: Optional[str] = None,
                              input_overrides: Optional[Dict[str, Any]] = None,
                              base_context=None,
                              source_workflow_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Выполняет только один выбранный шаг pipeline, сохраняя контекст остальных шагов.

        Для explicit rerun шаг выполняется без depends_on/condition, а результаты других
        шагов берутся из base_context.step_outputs.
        """
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}

        if base_context is None:
            return {
                "status": "error",
                "message": f"Для шага '{step_id}' требуется сохранённый контекст предыдущего pipeline",
            }

        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Перезапуск одного шага {step_id} для проекта {project_id}")

            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowDefinition, WorkflowContext
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)

            step_map = {step.id: step for step in workflow_def.steps}
            if step_id not in step_map:
                return {"status": "error", "message": f"Шаг '{step_id}' не найден в pipeline"}

            single_step = copy.deepcopy(step_map[step_id])
            single_step.depends_on = []
            single_step.condition = None

            workflow_def.steps = [single_step]

            if progress_callback:
                self._install_step_hook(progress_callback, 1)

            workflow_inputs = workflow_def.inputs if isinstance(workflow_def.inputs, dict) else {}
            context_variables = copy.deepcopy(workflow_inputs)
            if getattr(base_context, "variables", None):
                context_variables.update(copy.deepcopy(base_context.variables))
            context_variables["project_id"] = project_id
            if task is not None:
                context_variables["task"] = task
            context_variables.update(input_overrides)

            context_step_outputs = {}
            if getattr(base_context, "step_outputs", None):
                context_step_outputs = copy.deepcopy(base_context.step_outputs)

            context_metadata = {}
            if getattr(base_context, "metadata", None):
                context_metadata = copy.deepcopy(base_context.metadata)
            if source_workflow_id:
                context_metadata["source_workflow_id"] = source_workflow_id
            context_metadata["single_step_rerun"] = step_id

            self.current_workflow_id = (
                f"sbm_single_{project_id}_{step_id}_{uuid.uuid4().hex[:8]}"
            )
            context = WorkflowContext(
                workflow_id=self.current_workflow_id,
                session_id=project_id,
                variables=context_variables,
                step_outputs=context_step_outputs,
                metadata=context_metadata,
            )

            result = await self.engine.execute_workflow(workflow_def, context)

            logger.info(f"✅ Single-step rerun завершён для проекта {project_id}, шаг {step_id}")
            response = self._build_runner_response(
                result,
                success_payload={
                    "project_id": project_id,
                    "step_id": step_id,
                    "mode": "single_step",
                    "source_workflow_id": source_workflow_id,
                },
            )
            response.setdefault("mode", "single_step")
            return response

        except Exception as e:
            logger.error(f"❌ Ошибка single-step rerun для шага {step_id}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._uninstall_step_hook()
            self.current_workflow_id = None

    async def resume_workflow_from_checkpoint(
        self,
        workflow_id: str,
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """Возобновляет storybook pipeline из сохранённого checkpoint-контекста."""
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}

        checkpoint = await self._get_latest_workflow_checkpoint(workflow_id)
        if not checkpoint or not checkpoint.context:
            return {
                "status": "error",
                "message": f"Не найден checkpoint с контекстом для workflow '{workflow_id}'",
            }

        try:
            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowDefinition

            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            completed_steps = set(checkpoint.completed_steps or [])
            remaining_steps = [
                copy.deepcopy(step)
                for step in workflow_def.steps
                if step.id not in completed_steps
            ]

            if not remaining_steps:
                return {
                    "status": "success",
                    "message": "Для восстановления нет оставшихся шагов",
                    "workflow_id": workflow_id,
                }

            workflow_def.steps = remaining_steps
            context = checkpoint.context
            context.workflow_id = workflow_id
            context.current_step = checkpoint.current_step

            total_steps = len(workflow_def.steps)
            if progress_callback:
                self._install_step_hook(progress_callback, total_steps)

            self.current_workflow_id = workflow_id
            result = await self.engine.execute_workflow(workflow_def, context)
            logger.info(
                "✅ Workflow %s возобновлён из checkpoint (%s)",
                workflow_id,
                checkpoint.current_step,
            )
            response = self._build_runner_response(
                result,
                success_payload={
                    "workflow_id": workflow_id,
                    "current_step": checkpoint.current_step,
                    "completed_steps": list(completed_steps),
                    "remaining_steps": [step.id for step in workflow_def.steps],
                    "mode": "checkpoint_resume",
                },
            )
            response.setdefault("mode", "checkpoint_resume")
            return response
        except Exception as e:
            logger.error(f"❌ Ошибка восстановления workflow {workflow_id}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            self._uninstall_step_hook()
            self.current_workflow_id = None
    
    async def pause_pipeline(self) -> Dict[str, Any]:
        """Ставит pipeline на паузу между шагами."""
        if not self.current_workflow_id:
            return {"status": "error", "message": "Нет активного pipeline для паузы"}

        self._pause_event.clear()
        workflow_id = self.current_workflow_id

        if self.engine:
            from workflow.models import WorkflowStatus, WorkflowContext
            latest_checkpoint = await self._get_latest_workflow_checkpoint(workflow_id)
            context = (
                latest_checkpoint.context
                if latest_checkpoint and latest_checkpoint.context
                else WorkflowContext(workflow_id=workflow_id, session_id=workflow_id)
            )
            step_results = latest_checkpoint.step_results if latest_checkpoint else {}
            current_step = latest_checkpoint.current_step if latest_checkpoint else None
            await self.engine.state_manager.save_checkpoint(
                workflow_id=workflow_id,
                status=WorkflowStatus.PAUSED,
                context=context,
                step_results=step_results,
                current_step=current_step,
                metadata={"paused_by": "StoryBookManager"}
            )

        logger.info(f"⏸ Pipeline {workflow_id} поставлен на паузу")
        return {"status": "paused", "workflow_id": workflow_id}

    async def resume_pipeline(self) -> Dict[str, Any]:
        """Снимает pipeline с паузы через engine.resume_workflow()."""
        if not self.current_workflow_id:
            return {"status": "error", "message": "Нет активного pipeline для возобновления"}

        workflow_id = self.current_workflow_id

        if self.engine:
            try:
                from workflow.models import WorkflowStatus, WorkflowContext
                latest_checkpoint = await self._get_latest_workflow_checkpoint(workflow_id)
                context = (
                    latest_checkpoint.context
                    if latest_checkpoint and latest_checkpoint.context
                    else WorkflowContext(workflow_id=workflow_id, session_id=workflow_id)
                )
                step_results = latest_checkpoint.step_results if latest_checkpoint else {}
                current_step = latest_checkpoint.current_step if latest_checkpoint else None
                await self.engine.state_manager.save_checkpoint(
                    workflow_id=workflow_id,
                    status=WorkflowStatus.RUNNING,
                    context=context,
                    step_results=step_results,
                    current_step=current_step,
                    metadata={"resumed_by": "StoryBookManager"}
                )
            except Exception as e:
                logger.error(f"Ошибка обновления статуса при resume: {e}")

        self._pause_event.set()
        logger.info(f"▶ Pipeline {workflow_id} возобновлён")
        return {"status": "resumed", "workflow_id": workflow_id}

    async def cancel_pipeline(self) -> Dict[str, Any]:
        """Отменяет текущий pipeline через engine.cancel_workflow()"""
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}

        workflow_id = self.current_workflow_id
        if not workflow_id:
            return {"status": "error", "message": "Нет активного pipeline для отмены"}

        try:
            await self.engine.cancel_workflow(
                workflow_id,
                reason="Отменено пользователем из StoryBookManager"
            )
            logger.info(f"🚫 Pipeline {workflow_id} отменён")
            return {"status": "cancelled", "workflow_id": workflow_id}
        except Exception as e:
            logger.error(f"❌ Ошибка отмены pipeline {workflow_id}: {e}")
            return {"status": "error", "message": str(e)}

    async def get_incomplete_workflows(self, project_id: str) -> list:
        """Находит незавершённые workflow для проекта в SQLite.

        Возвращает список словарей с информацией о workflow со статусом
        отличным от completed/cancelled.
        """
        if not self.engine:
            return []

        try:
            store = self.engine.state_manager.store
            import sqlite3
            import json as _json

            results = []
            with sqlite3.connect(store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT wc.workflow_id, wc.status, wc.current_step,
                           wc.completed_steps, wc.timestamp, wc.resumable
                    FROM workflow_checkpoints wc
                    INNER JOIN (
                        SELECT workflow_id, MAX(timestamp) AS max_ts
                        FROM workflow_checkpoints
                        GROUP BY workflow_id
                    ) latest ON wc.workflow_id = latest.workflow_id
                                AND wc.timestamp = latest.max_ts
                    WHERE wc.status NOT IN ('completed', 'cancelled')
                    AND wc.workflow_id LIKE ?
                    ORDER BY wc.timestamp DESC
                """, (f"%{project_id}%",))

                for row in cursor.fetchall():
                    results.append({
                        "workflow_id": row["workflow_id"],
                        "status": row["status"],
                        "current_step": row["current_step"],
                        "completed_steps": _json.loads(row["completed_steps"]),
                        "timestamp": row["timestamp"],
                        "resumable": bool(row["resumable"]),
                    })

            return results
        except Exception as e:
            logger.error(f"Ошибка поиска незавершённых workflow: {e}")
            return []

    def regenerate_image(self, project_id: str, page_num: int, shot_num: Optional[int] = None) -> Dict[str, Any]:
        """
        Регенерирует изображение для страницы или кадра
        
        Args:
            project_id: ID проекта
            page_num: Номер страницы
            shot_num: Номер кадра (для видео)
        
        Returns:
            Результат регенерации
        """
        try:
            logger.info(f"🎨 Регенерация изображения для проекта {project_id}, страница {page_num}")
            
            # Импортируем необходимые инструменты
            from custom_tools.storybook.artist_batch_edit import artist_agent_batch_edit_tool
            
            # TODO: Реализовать логику регенерации конкретного изображения
            # Нужно подготовить items для artist_agent_batch_edit_tool
            
            return {"status": "not_implemented", "message": "Регенерация изображений пока не реализована"}
            
        except Exception as e:
            logger.error(f"❌ Ошибка регенерации изображения: {e}")
            return {"status": "error", "message": str(e)}
    
    def regenerate_video(self, project_id: str, scene_num: int, shot_num: int) -> Dict[str, Any]:
        """
        Регенерирует видео для кадра
        
        Args:
            project_id: ID проекта
            scene_num: Номер сцены
            shot_num: Номер кадра
        
        Returns:
            Результат регенерации
        """
        try:
            logger.info(f"🎬 Регенерация видео для проекта {project_id}, сцена {scene_num}, кадр {shot_num}")
            
            # Импортируем необходимые инструменты
            from custom_tools.storybook.video_generator import video_generator_tool
            
            # TODO: Реализовать логику регенерации конкретного видео
            # Нужно подготовить items для video_generator_tool
            
            return {"status": "not_implemented", "message": "Регенерация видео пока не реализована"}
            
        except Exception as e:
            logger.error(f"❌ Ошибка регенерации видео: {e}")
            return {"status": "error", "message": str(e)}
    
    def get_pipeline_status(self, session_id: str) -> Dict[str, Any]:
        """
        Получает статус выполнения pipeline
        
        Args:
            session_id: ID сессии
        
        Returns:
            Статус выполнения
        """
        try:
            # TODO: Реализовать получение статуса выполнения
            # Нужно интегрироваться с системой мониторинга workflow engine
            
            return {"status": "not_implemented", "message": "Получение статуса pipeline пока не реализовано"}
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения статуса pipeline: {e}")
            return {"status": "error", "message": str(e)}
    
    def validate_project_for_pipeline(self, project_id: str,
                                      start_step: Optional[str] = None) -> Dict[str, Any]:
        """
        Проверяет готовность проекта для запуска pipeline.
        Валидирует наличие 00_brief.json, синтаксис JSON и зависимости частичного запуска.
        """
        try:
            from config.settings import app_settings

            projects_dir = app_settings.get_projects_directory()
            project_path = projects_dir / project_id

            if not project_path.exists():
                return {
                    "valid": False,
                    "message": f"Проект {project_id} не найден",
                    "errors": [f"Директория не найдена: {project_path}"],
                    "warnings": [],
                }

            errors: List[str] = []
            warnings: List[str] = []

            brief_path = project_path / "00_brief.json"
            if not brief_path.exists():
                errors.append("Отсутствует файл 00_brief.json (ТЗ проекта)")
            else:
                try:
                    with open(brief_path, "r", encoding="utf-8") as file_obj:
                        brief_data = json.load(file_obj)
                    if not brief_data.get("title"):
                        warnings.append("В brief не указан title")
                except json.JSONDecodeError as e:
                    errors.append(
                        f"Некорректный JSON в 00_brief.json (строка {e.lineno}): {e.msg}"
                    )

            invalid_json = []
            for json_file in project_path.rglob("*.json"):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        json.load(f)
                except json.JSONDecodeError as e:
                    rel_path = json_file.relative_to(project_path)
                    invalid_json.append(f"{rel_path} (строка {e.lineno}): {e.msg}")

            if invalid_json:
                errors.extend([f"Некорректный JSON: {err}" for err in invalid_json])

            if start_step:
                yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
                if not yaml_path.exists():
                    errors.append(f"Pipeline файл не найден: {yaml_path}")
                else:
                    from workflow.models import WorkflowDefinition

                    workflow_def = WorkflowDefinition.from_yaml(yaml_path)
                    step_ids = [step.id for step in workflow_def.steps]
                    if start_step not in step_ids:
                        errors.append(f"Шаг '{start_step}' не найден в pipeline")
                    else:
                        required_artifacts = self._collect_required_artifacts(
                            workflow_def,
                            start_step,
                        )
                        for artifact in required_artifacts:
                            artifact_path = project_path / artifact
                            if not artifact_path.exists():
                                errors.append(
                                    f"Для запуска с шага '{start_step}' нужен артефакт: {artifact}"
                                )

            message_parts: List[str] = []
            if errors:
                message_parts.append("Ошибки:\n" + "\n".join(f"  • {err}" for err in errors))
            if warnings:
                message_parts.append(
                    "Предупреждения:\n" + "\n".join(f"  • {warn}" for warn in warnings)
                )

            return {
                "valid": not errors,
                "message": "\n\n".join(message_parts) if message_parts else "Проект готов для запуска pipeline",
                "errors": errors,
                "warnings": warnings,
            }

        except Exception as e:
            logger.error(f"❌ Ошибка валидации проекта: {e}")
            return {
                "valid": False,
                "message": str(e),
                "errors": [str(e)],
                "warnings": [],
            }


def run_pipeline_sync(runner: PipelineRunner, project_id: str, task: str,
                      progress_callback: Optional[Callable] = None,
                      input_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Синхронная обертка для запуска pipeline.
    Принимает существующий PipelineRunner, чтобы cancel_pipeline()
    мог обратиться к тому же экземпляру engine и workflow_id.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                runner.run_full_pipeline(
                    project_id,
                    task,
                    progress_callback,
                    input_overrides=input_overrides,
                )
            )
            return result
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Ошибка синхронного запуска pipeline: {e}")
        return {"status": "error", "message": str(e)}
