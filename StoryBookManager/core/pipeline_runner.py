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
        self.engine_error: Optional[str] = None  # причина, если движок не поднялся
        self.current_workflow_id: Optional[str] = None
        self._original_on_step_completed = None
        self._original_execute_workflow_step = None
        self._hook_lock = threading.Lock()  # сериализует install/uninstall хуков
        self._pause_event = threading.Event()
        self._pause_event.set()  # не на паузе по умолчанию
        self._studio_project: Optional[str] = None  # проект, чей замок держит этот раннер
        self._studio_observer = None  # запись состояния прогона для студии (React видит Tk-прогон)
        self._studio_result = None
        self._initialize_engine()
    
    def _initialize_engine(self):
        """Инициализация workflow engine"""
        try:
            from workflow.enhanced_engine import EnhancedWorkflowEngine
            self.engine = EnhancedWorkflowEngine()
            self.engine_error = None
            logger.info("✅ Workflow engine инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации workflow engine: {e}")
            self.engine = None
            self.engine_error = f"{type(e).__name__}: {e}"
    
    def _install_step_hook(self, progress_callback: Callable, total_steps: int,
                           step_ids: Optional[List[str]] = None):
        """Оборачивает engine-хуки для step tracker, progress и паузы."""
        if not self.engine or not progress_callback:
            return
        # Весь install под одним локом: claim-проверка, захват оригиналов, определение
        # замыканий и запись hooked-методов в engine — атомарно. Иначе concurrent
        # _uninstall_step_hook между захватом и записью потерял бы оригиналы, а второй
        # install установил бы хуки дважды. Замыкания исполняются ПОЗЖЕ (во время workflow)
        # и _hook_lock не берут — дедлока нет.
        with self._hook_lock:
            if self._original_on_step_completed is not None:
                return
            self._original_on_step_completed = self.engine._on_step_completed
            self._original_execute_workflow_step = self.engine._execute_workflow_step
            completed_count = [0]
            original = self._original_on_step_completed
            original_execute_step = self._original_execute_workflow_step

            async def _hooked_execute_workflow_step(step, context, workflow_def):
                observer = await self._studio_begin(step_ids)
                if observer is not None:
                    await self._studio_gate(observer)
                    self._studio_safe(observer.session.step_started, step, context)
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
                if self._studio_observer is not None:
                    self._studio_safe(self._studio_observer.session.step_finished, step, step_result)
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
                        step_error=getattr(step_result, "error", None),
                        step_error_class=(getattr(step_result, "error_class", "") or None),
                        step_traceback=(getattr(step_result, "metadata", None) or {}).get("traceback"),
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

    async def _studio_begin(self, step_ids: Optional[List[str]]):
        """Первый шаг прогона заводит наблюдателя студии; любая его ошибка Tk не ломает."""
        if self._studio_observer is not None:
            return self._studio_observer
        if not self._studio_project or not self.current_workflow_id:
            return None
        try:
            from custom_tools.storybook.studio.run_observer import TkRunObserver
            self._studio_observer = TkRunObserver(
                self.engine, self._studio_project, self.current_workflow_id, step_ids or [])
        except Exception as e:
            logger.warning(f"Наблюдатель студии не запущен: {e}")
        return self._studio_observer

    async def _studio_gate(self, observer) -> None:
        try:
            await observer.session.gate()
        except Exception as e:
            logger.warning(f"Гейт паузы студии: {e}")

    @staticmethod
    def _studio_safe(func, *args, **kwargs) -> None:
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Запись состояния студии: {e}")

    def _studio_request(self, **kwargs) -> None:
        if self._studio_observer is not None:
            self._studio_safe(self._studio_observer.request, **kwargs)

    async def _studio_finish(self) -> None:
        observer, result = self._studio_observer, self._studio_result
        self._studio_observer = None
        self._studio_result = None
        if observer is None:
            return
        try:
            error = None if result is not None else RuntimeError("Прогон прерван ошибкой")
            await observer.finish(result, error)
        except Exception as e:
            logger.warning(f"Завершение записи студии: {e}")

    def _uninstall_step_hook(self):
        """Восстанавливает оригинальные engine-хуки."""
        # Под тем же локом, что и install: иначе сброс _original_* мог бы гоняться
        # с claim-проверкой в _install_step_hook.
        with self._hook_lock:
            if self._original_on_step_completed is not None and self.engine:
                self.engine._on_step_completed = self._original_on_step_completed
                self._original_on_step_completed = None
            if self._original_execute_workflow_step is not None and self.engine:
                self.engine._execute_workflow_step = self._original_execute_workflow_step
                self._original_execute_workflow_step = None

    @staticmethod
    def _report_failed_steps(result: Any, progress_callback: Optional[Callable]) -> None:
        """M-29: движок вызывает completion-хук ТОЛЬКО для успешных шагов, поэтому
        упавший шаг иначе не доходит до UI (остаётся pending — «маскируется под
        не-провал»). После завершения воркфлоу реконсилируем по result.step_results:
        каждый FAILED-шаг доводим до progress_callback со step_status='failed' и
        деталями ошибки (класс/traceback) — тем же каналом, что и завершённые шаги."""
        if not progress_callback:
            return
        step_results = getattr(result, "step_results", None) or {}
        if not isinstance(step_results, dict):
            return
        for step_id, sr in step_results.items():
            status = getattr(getattr(sr, "status", None), "value", None) or str(getattr(sr, "status", ""))
            if str(status).lower() != "failed":
                continue
            try:
                progress_callback(
                    message=f"Шаг '{step_id}' завершён с ошибкой",
                    step_id=step_id,
                    step_status="failed",
                    step_duration=getattr(sr, "duration_seconds", None),
                    step_error=getattr(sr, "error", None),
                    step_error_class=(getattr(sr, "error_class", "") or None),
                    step_traceback=(getattr(sr, "metadata", None) or {}).get("traceback"),
                )
            except Exception as exc:
                logger.error(f"Ошибка в progress_callback для упавшего шага {step_id}: {exc}")

    def _acquire_project_lock(self, project_id: str):
        """Не-блокирующий advisory-lock по project_id (M-31).

        Мирроринг паттерна `_ProviderJobsFileLock` (flock по конвенционному файлу
        в корне проекта), но с LOCK_NB: если проект уже выполняется в другой
        сессии/поверхности — сразу возвращаем None, а не ждём. Конвенция файла
        `.pipeline.lock` фиксируется здесь, чтобы позже на неё сели Streamlit/AG-UI.
        flock снимается ОС при закрытии fd/завершении процесса → stale-lock не остаётся.
        """
        from custom_tools.storybook.project_paths import acquire_storybook_project_lock

        handle = acquire_storybook_project_lock(project_id)
        if handle is not None:
            self._studio_project = project_id
        return handle

    @staticmethod
    def _release_project_lock(handle) -> None:
        """Снимает advisory-lock проекта (симметрично _acquire_project_lock)."""
        from custom_tools.storybook.project_paths import release_storybook_project_lock

        release_storybook_project_lock(handle)

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
    def _step_output_artifacts(workflow_def=None) -> Dict[str, List[str]]:
        """Канонические артефакты storybook pipeline для проверки частичного запуска."""
        from custom_tools.storybook.studio import run_plan
        return run_plan.step_output_artifacts(workflow_def)

    @staticmethod
    def _read_generate_blockout_flag(project_path: Optional[Path]) -> bool:
        """Флаг generate_blockout из 00_brief.json (нет файла/ключа - False)."""
        from custom_tools.storybook.studio import run_plan
        return run_plan.read_generate_blockout_flag(project_path)

    @staticmethod
    def _read_blockout_allowed_durations(project_path: Optional[Path]) -> List[int]:
        """blockout_allowed_durations из 00_brief.json тем же безопасным чтением."""
        from custom_tools.storybook.studio import run_plan
        return run_plan.read_blockout_allowed_durations(project_path)

    @classmethod
    def _collect_required_artifacts(
        cls,
        workflow_def,
        start_step: str,
        project_path: Optional[Path] = None,
    ) -> List[str]:
        """Артефакты, обязательные перед запуском start_step (общая логика со студией)."""
        from custom_tools.storybook.studio import run_plan
        return run_plan.collect_required_artifacts(workflow_def, start_step, project_path)

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

        project_lock = None
        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Запуск полного pipeline для проекта {project_id}")

            project_lock = self._acquire_project_lock(project_id)
            if project_lock is None:
                return {
                    "status": "error",
                    "message": f"Проект {project_id} уже выполняется в другой сессии/поверхности",
                }

            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"

            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowContext, WorkflowDefinition
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            total_steps = len(workflow_def.steps)

            if progress_callback:
                self._install_step_hook(progress_callback, total_steps, [s.id for s in workflow_def.steps])

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

            self._studio_result = result
            self._report_failed_steps(result, progress_callback)
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
            await self._studio_finish()
            self._release_project_lock(project_lock)
            self._uninstall_step_hook()
            self.current_workflow_id = None

    @staticmethod
    def validate_step_dependencies(workflow_def, start_step_id: str) -> list:
        """Проверяет что все зависимости start_step_id входят в пропускаемые шаги.

        ДИАГНОСТИЧЕСКИЙ helper, НЕ гейт: run_from_step больше его не вызывает
        (частичный запуск опирается на артефакт-валидацию на диске + развязку
        пограничных depends_on). Оставлен для обратной совместимости с внешним кодом.
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

        project_lock = None
        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Запуск pipeline с шага {step_id} для проекта {project_id}")

            project_lock = self._acquire_project_lock(project_id)
            if project_lock is None:
                return {
                    "status": "error",
                    "message": f"Проект {project_id} уже выполняется в другой сессии/поверхности",
                }

            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"

            if not yaml_path.exists():
                return {"status": "error", "message": f"Pipeline файл не найден: {yaml_path}"}

            from workflow.models import WorkflowDefinition, WorkflowContext
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)

            step_ids = [s.id for s in workflow_def.steps]
            if step_id not in step_ids:
                return {"status": "error", "message": f"Шаг '{step_id}' не найден в pipeline"}

            start_index = step_ids.index(step_id)

            # H-8: hard-fail снят. validate_step_dependencies теперь только
            # ДИАГНОСТИКА (не гейт): логируем, какие апстрим-шаги пропускаются и
            # должны присутствовать на диске как артефакты. Артефакт-валидация уже
            # выполнена GUI-слоем (validate_project_for_pipeline), а пограничные
            # depends_on развязываются ниже, поэтому запуск НЕ блокируется.
            dep_diagnostics = self.validate_step_dependencies(workflow_def, step_id)
            if dep_diagnostics:
                logger.info(
                    "Неудовлетворённые зависимости (диагностика, запуск не блокируется): %s",
                    "; ".join(dep_diagnostics),
                )

            for skipped_step in workflow_def.steps[:start_index]:
                logger.info(f"⏭️ Шаг '{skipped_step.id}' будет пропущен (выполнение с {step_id})")

            # Срез оставшихся шагов на deepcopy, чтобы не мутировать кэшированный
            # WorkflowDefinition между запусками в одном процессе Tkinter.
            remaining_steps = [copy.deepcopy(step) for step in workflow_def.steps[start_index:]]
            remaining_ids = {step.id for step in remaining_steps}
            # Развязываем пограничные depends_on на ВЫРЕЗАННЫЕ шаги: иначе
            # DependencyGraph движка (has_cycles/get_ready_steps) увидит висячие
            # ссылки на несуществующие узлы → ValueError «циклические зависимости»
            # или deadlock. Внутренние зависимости среза сохраняются.
            for step in remaining_steps:
                step.depends_on = [dep for dep in step.depends_on if dep in remaining_ids]
            workflow_def.steps = remaining_steps

            if progress_callback:
                self._install_step_hook(progress_callback, len(workflow_def.steps), [s.id for s in workflow_def.steps])

            workflow_inputs = workflow_def.inputs if isinstance(workflow_def.inputs, dict) else {}
            context_variables = workflow_inputs.copy()
            context_variables["project_id"] = project_id
            if task is not None:
                context_variables["task"] = task
            context_variables.update(input_overrides)

            # Грузим step_outputs вырезанных шагов из последнего checkpoint проекта,
            # чтобы оставшиеся шаги резолвили {вырезанный_шаг.field} в шаблонах/condition
            # (как это делает run_single_step). Нет checkpoint — продолжаем с пустым
            # step_outputs (шаги, читающие апстрим с диска, работают).
            context_step_outputs: Dict[str, Any] = {}
            checkpoint = await self._get_latest_project_checkpoint(project_id)
            base_context = getattr(checkpoint, "context", None) if checkpoint else None
            base_outputs = getattr(base_context, "step_outputs", None)
            if base_outputs:
                context_step_outputs = copy.deepcopy(base_outputs)

            self.current_workflow_id = f"sbm_partial_{project_id}_{uuid.uuid4().hex[:8]}"
            context = WorkflowContext(
                workflow_id=self.current_workflow_id,
                session_id=project_id,
                variables=context_variables,
                step_outputs=context_step_outputs,
            )

            result = await self.engine.execute_workflow(workflow_def, context)

            self._studio_result = result
            self._report_failed_steps(result, progress_callback)
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
            await self._studio_finish()
            self._release_project_lock(project_lock)
            self._uninstall_step_hook()
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

    @staticmethod
    def _like_escape(value: str) -> str:
        """Экранирует \\, % и _ для LIKE ... ESCAPE '\\'."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _workflow_id_matches_project(workflow_id: str, project_id: str,
                                     context_json: Optional[str] = None) -> bool:
        """Точная проверка, что прогон принадлежит проекту (общая логика со студией)."""
        from custom_tools.storybook.studio import run_plan
        return run_plan.workflow_id_matches_project(workflow_id, project_id, context_json)

    async def _get_latest_project_checkpoint(self, project_id: str):
        """Возвращает последний checkpoint для проекта по workflow_id."""
        if not self.engine or not getattr(self.engine, "state_manager", None):
            return None

        try:
            import sqlite3

            store = self.engine.state_manager.store
            with sqlite3.connect(store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # LIKE только сужает выборку, точное совпадение проверяем в Python
                rows = conn.execute(
                    """
                    SELECT workflow_id, context
                    FROM workflow_checkpoints
                    WHERE workflow_id LIKE ? ESCAPE '\\'
                    ORDER BY timestamp DESC
                    """,
                    (f"%{self._like_escape(project_id)}%",),
                )
                # Курсор читаем построчно до первого совпадения, без LIMIT:
                # чужие свежие прогоны с подстрокой id не должны скрыть нужный
                row = next(
                    (r for r in rows
                     if self._workflow_id_matches_project(r["workflow_id"], project_id, r["context"])),
                    None,
                )
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

        project_lock = None
        try:
            input_overrides = input_overrides or {}
            logger.info(f"🚀 Перезапуск одного шага {step_id} для проекта {project_id}")

            project_lock = self._acquire_project_lock(project_id)
            if project_lock is None:
                return {
                    "status": "error",
                    "message": f"Проект {project_id} уже выполняется в другой сессии/поверхности",
                }

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
                self._install_step_hook(progress_callback, 1, [step_id])

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

            self._studio_result = result
            self._report_failed_steps(result, progress_callback)
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
            await self._studio_finish()
            self._release_project_lock(project_lock)
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

        project_lock = None
        try:
            # M-31: сериализуем resume по project_id (session_id из checkpoint-контекста),
            # чтобы он не гонялся с параллельным запуском того же проекта.
            project_id = getattr(checkpoint.context, "session_id", None)
            if project_id:
                project_lock = self._acquire_project_lock(project_id)
                if project_lock is None:
                    return {
                        "status": "error",
                        "message": f"Проект {project_id} уже выполняется в другой сессии/поверхности",
                    }

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
                self._install_step_hook(progress_callback, total_steps, [s.id for s in workflow_def.steps])

            self.current_workflow_id = workflow_id
            result = await self.engine.execute_workflow(workflow_def, context)
            self._studio_result = result
            self._report_failed_steps(result, progress_callback)
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
            await self._studio_finish()
            self._release_project_lock(project_lock)
            self._uninstall_step_hook()
            self.current_workflow_id = None

    async def pause_pipeline(self) -> Dict[str, Any]:
        """Ставит pipeline на паузу между шагами."""
        if not self.current_workflow_id:
            return {"status": "error", "message": "Нет активного pipeline для паузы"}

        self._pause_event.clear()
        self._studio_request(pause=True)
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
        """Снимает pipeline с паузы.

        Помечает workflow как RUNNING (сохраняет checkpoint со статусом RUNNING)
        и разблокирует приостановленный цикл выполнения через _pause_event. Это
        локальное снятие паузы StoryBookManager, а НЕ generic-resume движка
        (engine.resume_workflow), который перезапускает workflow с checkpoint'а,
        пропуская уже завершённые шаги.
        """
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
        self._studio_request(pause=False)
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
            self._studio_request(cancel=True)
            await self.engine.cancel_workflow(
                workflow_id,
                reason="Отменено пользователем из StoryBookManager"
            )
            logger.info(f"🚫 Pipeline {workflow_id} отменён")
            return {"status": "cancelled", "workflow_id": workflow_id}
        except Exception as e:
            logger.error(f"❌ Ошибка отмены pipeline {workflow_id}: {e}")
            return {"status": "error", "message": str(e)}

    async def cancel_stale_workflow(self, workflow_id: str,
                                    project_id: Optional[str] = None) -> Dict[str, Any]:
        """Помечает сохранённый (не обязательно активный) прогон отменённым.

        Нужен для «Начать сначала»: иначе старый checkpoint остаётся running/failed
        и диалог восстановления появляется при каждом открытии проекта.
        """
        if not self.engine:
            return {"status": "error", "message": "Workflow engine не инициализирован"}

        # Если проект сейчас выполняется в другой сессии, чужой живой прогон не трогаем
        lock = None
        if project_id:
            lock = self._acquire_project_lock(project_id)
            if lock is None:
                return {"status": "error", "message": "Проект занят другим прогоном"}

        try:
            await self.engine.cancel_workflow(
                workflow_id,
                reason="Начать сначала (из StoryBookManager)"
            )
            logger.info(f"🚫 Старый прогон {workflow_id} помечен отменённым")
            return {"status": "cancelled", "workflow_id": workflow_id}
        except Exception as e:
            logger.error(f"❌ Ошибка отмены старого прогона {workflow_id}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if lock is not None:
                self._release_project_lock(lock)

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
                           wc.completed_steps, wc.timestamp, wc.resumable, wc.context
                    FROM workflow_checkpoints wc
                    INNER JOIN (
                        SELECT workflow_id, MAX(timestamp) AS max_ts
                        FROM workflow_checkpoints
                        GROUP BY workflow_id
                    ) latest ON wc.workflow_id = latest.workflow_id
                                AND wc.timestamp = latest.max_ts
                    WHERE wc.status NOT IN ('completed', 'cancelled')
                    AND wc.workflow_id LIKE ? ESCAPE '\\'
                    ORDER BY wc.timestamp DESC
                """, (f"%{self._like_escape(project_id)}%",))

                for row in cursor.fetchall():
                    # LIKE только сужает выборку, точное совпадение проверяем в Python
                    if not self._workflow_id_matches_project(row["workflow_id"], project_id, row["context"]):
                        continue
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
            from custom_tools.storybook.project_paths import (
                safe_storybook_project_dir,
            )

            try:
                project_path = safe_storybook_project_dir(project_id)
            except ValueError as exc:
                return {
                    "valid": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "warnings": [],
                }

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

            # --- Блок «Болванка» (ТЗ раздел 18.6) ---------------------------
            # B12 — доступность Blender: проверка ОКРУЖЕНИЯ, выполняется всегда,
            # независимо от generate_blockout. Уровень — предупреждение, а не
            # ошибка: запуск блокирует сам шаг blockout_scene_builder (раздел
            # 13.4), пред-запусковая проверка лишь предупреждает заранее.
            # Переиспользуем то же средство и тот же тайм-аут 5 секунд, что и
            # блок готовности (раздел 18.3) — без тайм-аута синхронный вызов из
            # веб-обработчика мог бы зависнуть.
            from custom_tools.storybook.video_contract import (
                _probe_blockout_blender_readiness,
            )

            blender_probe = _probe_blockout_blender_readiness()
            if not blender_probe.get("available"):
                warnings.append(
                    f"Blockout B12: Blender недоступен ({blender_probe.get('message')})"
                )

            # B01/B15 — проверки СОСТОЯНИЯ проекта: только при generate_blockout:
            # true (Р6) и только если screenplay_shots_generator уже отработал
            # хотя бы раз (маркер — 97_shots/video_model_caps.json; его
            # отсутствие означает, что нормализация длительностей ещё не
            # проводилась, и на свежем проекте это не ошибка). Флаг и набор
            # длительностей читаем уже существующим _read_generate_blockout_flag()
            # (Э7) и парным ему _read_blockout_allowed_durations() тем же
            # безопасным способом, а не парсим 00_brief.json заново (Р3) — оба
            # сами безопасны к отсутствующему или повреждённому брифу, поэтому
            # отдельного brief_data-словаря здесь не нужно.
            if self._read_generate_blockout_flag(project_path):
                caps_path = project_path / "97_shots" / "video_model_caps.json"
                if caps_path.exists():
                    try:
                        with open(caps_path, "r", encoding="utf-8") as caps_file:
                            caps_data = json.load(caps_file)
                    except (OSError, ValueError):
                        caps_data = None
                    supported_durations = (
                        caps_data.get("supported_durations") if isinstance(caps_data, dict) else None
                    ) or []
                    if not supported_durations:
                        errors.append(
                            "B15: не получен набор допустимых длительностей видеомодели "
                            "(97_shots/video_model_caps.json)"
                        )
                    else:
                        supported_set = set()
                        for value in supported_durations:
                            try:
                                supported_set.add(int(value))
                            except (TypeError, ValueError):
                                continue
                        # Допустимый набор — пересечение supported_durations с
                        # непустым blockout_allowed_durations (ТЗ раздел 10.1
                        # пункт 2, раздел 6.1). Пустой blockout_allowed_durations
                        # означает «весь набор модели». Если пересечение пусто, а
                        # supported_set непуст, ограничение проекта игнорируется
                        # целиком — берётся полный набор модели, как и в самом
                        # blockout_scene_builder.
                        configured_durations = set(
                            self._read_blockout_allowed_durations(project_path)
                        )
                        if configured_durations:
                            intersection = supported_set & configured_durations
                            allowed_set = intersection if intersection else supported_set
                        else:
                            allowed_set = supported_set
                        try:
                            with open(
                                project_path / "97_shots" / "shots.json", "r", encoding="utf-8"
                            ) as shots_file:
                                shots_data = json.load(shots_file)
                        except (OSError, ValueError):
                            shots_data = None
                        bad_shots = set()
                        for item in (shots_data.get("items") if isinstance(shots_data, dict) else None) or []:
                            duration = item.get("duration_s")
                            try:
                                in_set = duration is not None and int(duration) in allowed_set
                            except (TypeError, ValueError):
                                in_set = False
                            if not in_set:
                                bad_shots.add(f"scene {item.get('scene_number')} shot {item.get('shot_number')}")
                        if bad_shots:
                            errors.append(
                                "B01: duration_s не входит в допустимый набор длительностей "
                                "у шотов: " + ", ".join(sorted(bad_shots))
                            )

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
                            project_path,
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
