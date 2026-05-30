"""
Панель управления генерацией
===========================

Запуск pipeline и управление процессами генерации контента.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import asyncio
from typing import Optional, Callable, Dict, Any, List
import logging
import sys
import json
from pathlib import Path
import yaml

from StoryBookManager.core.project_manager import Project
from StoryBookManager.core.pipeline_runner import PipelineRunner, run_pipeline_sync
from StoryBookManager.gui.step_tracker import StepTracker

logger = logging.getLogger(__name__)


class GenerationPanel(ttk.Frame):
    """Панель управления генерацией"""
    
    def __init__(self, parent, on_generation_started: Callable):
        super().__init__(parent)
        
        self.on_generation_started = on_generation_started
        self.current_project: Optional[Project] = None
        self.pipeline_runner = PipelineRunner()
        self.generation_thread: Optional[threading.Thread] = None
        self.is_generating = False
        self._generation_lock = threading.Lock()
        self.pipeline_steps: List[str] = []
        self.pipeline_inputs: Dict[str, Any] = {}
        self.supported_languages: List[str] = []
        self._cancel_event = threading.Event()
        self._is_paused = False
        
        # Загружаем шаги из pipeline файла
        self.load_pipeline_steps()
        
        self.create_ui()
    
    def load_pipeline_steps(self):
        """Загрузка шагов из storybook_pipeline.yaml"""
        self.pipeline_steps = []
        self._pipeline_load_error: Optional[str] = None

        try:
            current_dir = Path(__file__).parent.parent.parent
            pipeline_file = current_dir / "workflow_pipelines" / "storybook_pipeline.yaml"

            if not pipeline_file.exists():
                self._pipeline_load_error = f"Файл pipeline не найден: {pipeline_file}"
                logger.error(self._pipeline_load_error)
                return

            with open(pipeline_file, 'r', encoding='utf-8') as f:
                pipeline_data = yaml.safe_load(f)

            steps = pipeline_data.get('steps', [])
            self.pipeline_steps = [step.get('id') for step in steps if step.get('id')]
            self.pipeline_inputs = pipeline_data.get('inputs', {}) or {}
            if not isinstance(self.pipeline_inputs, dict):
                raise ValueError("Секция inputs в pipeline должна быть объектом")
            self.supported_languages = self._load_supported_languages(
                self.pipeline_inputs.get("language")
            )

            if not self.pipeline_steps:
                self._pipeline_load_error = "Pipeline файл не содержит шагов"
                logger.error(self._pipeline_load_error)
                return

            logger.info(f"Загружено {len(self.pipeline_steps)} шагов из pipeline")

        except Exception as e:
            self._pipeline_load_error = f"Ошибка загрузки pipeline: {e}"
            logger.error(self._pipeline_load_error)
    
    def refresh_pipeline_steps(self):
        """Обновление списка шагов из pipeline файла"""
        self.load_pipeline_steps()
        if hasattr(self, 'step_combo'):
            self.step_combo['values'] = self.pipeline_steps
        if hasattr(self, 'step_tracker'):
            self.step_tracker.set_steps(self.pipeline_steps)
        self._update_pipeline_error_state()
        if self._pipeline_load_error:
            self.add_log(f"⚠️ {self._pipeline_load_error}", "error")
        else:
            self.add_log(f"Обновлен список шагов: {len(self.pipeline_steps)} шагов", "info")
        if hasattr(self, 'pipeline_language_combo'):
            self.pipeline_language_combo['values'] = tuple(self.supported_languages)

    def _update_pipeline_error_state(self):
        """Обновляет индикатор ошибки загрузки pipeline."""
        if not hasattr(self, "pipeline_error_label"):
            return

        if getattr(self, "_pipeline_load_error", None):
            self.pipeline_error_label.config(text="⚠️ Pipeline не загружен")
            self.pipeline_error_label.pack(anchor="w", pady=(0, 5))
        else:
            if hasattr(self.pipeline_error_label, "pack_forget"):
                self.pipeline_error_label.pack_forget()

    def _load_supported_languages(self, pipeline_language: Any = None) -> List[str]:
        """Загружает поддерживаемые языки из ui_config.json."""
        ui_config_file = Path(__file__).parent.parent / "config" / "ui_config.json"
        languages: List[str] = []

        try:
            with open(ui_config_file, 'r', encoding='utf-8') as f:
                ui_config = json.load(f)

            values = (
                ui_config.get("brief", {})
                .get("field_config", {})
                .get("language", {})
                .get("values", [])
            )
            if not isinstance(values, list):
                raise ValueError("brief.field_config.language.values должен быть списком")
            languages = [str(value).strip() for value in values if str(value).strip()]
        except Exception as e:
            logger.warning(f"Не удалось загрузить список языков из ui_config.json: {e}")

        pipeline_language = str(pipeline_language).strip() if pipeline_language is not None else ""
        if pipeline_language and pipeline_language not in languages:
            languages.append(pipeline_language)

        return languages

    @staticmethod
    def _to_bool(value: Any) -> bool:
        """Приводит значения из YAML/JSON/UI к bool."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _create_pipeline_input_vars(self):
        """Создаёт Tk-переменные для настроек pipeline."""
        self.pipeline_pages_min_var = tk.StringVar()
        self.pipeline_pages_max_var = tk.StringVar()
        self.pipeline_words_per_page_min_var = tk.StringVar()
        self.pipeline_words_per_page_max_var = tk.StringVar()
        self.pipeline_language_var = tk.StringVar()
        self.generate_screenplay_var = tk.BooleanVar()
        self.force_update_prompts_var = tk.BooleanVar()
        self._reset_pipeline_settings_to_defaults()

    def _reset_pipeline_settings_to_defaults(self):
        """Сбрасывает настройки панели к дефолтам из storybook_pipeline.yaml."""
        if not hasattr(self, "pipeline_pages_min_var"):
            return

        self.pipeline_pages_min_var.set(str(self.pipeline_inputs.get("pages_min", "")))
        self.pipeline_pages_max_var.set(str(self.pipeline_inputs.get("pages_max", "")))
        self.pipeline_words_per_page_min_var.set(
            str(self.pipeline_inputs.get("words_per_page_min", ""))
        )
        self.pipeline_words_per_page_max_var.set(
            str(self.pipeline_inputs.get("words_per_page_max", ""))
        )
        self.pipeline_language_var.set(str(self.pipeline_inputs.get("language", "")))
        self.generate_screenplay_var.set(
            self._to_bool(self.pipeline_inputs.get("generate_screenplay", False))
        )
        self.force_update_prompts_var.set(
            self._to_bool(self.pipeline_inputs.get("force_update_prompts", False))
        )

    def _apply_project_pipeline_settings(self, brief_data: Dict[str, Any]):
        """Подтягивает настройки pipeline из 00_brief.json поверх YAML-дефолтов."""
        self._reset_pipeline_settings_to_defaults()

        field_mapping = {
            "pages_min": self.pipeline_pages_min_var,
            "pages_max": self.pipeline_pages_max_var,
            "words_per_page_min": self.pipeline_words_per_page_min_var,
            "words_per_page_max": self.pipeline_words_per_page_max_var,
        }
        for field_name, variable in field_mapping.items():
            value = brief_data.get(field_name)
            if value is not None and value != "":
                variable.set(str(value))

        language = brief_data.get("language")
        if language is not None and str(language).strip():
            language_value = str(language).strip()
            if language_value not in self.supported_languages:
                self.supported_languages.append(language_value)
            self.pipeline_language_var.set(language_value)
            if hasattr(self, "pipeline_language_combo"):
                self.pipeline_language_combo['values'] = tuple(self.supported_languages)

        if "generate_screenplay" in brief_data:
            self.generate_screenplay_var.set(self._to_bool(brief_data.get("generate_screenplay")))
        if "force_update_prompts" in brief_data:
            self.force_update_prompts_var.set(self._to_bool(brief_data.get("force_update_prompts")))

    def _collect_pipeline_params(self) -> Dict[str, Any]:
        """Собирает и валидирует параметры pipeline из UI."""
        try:
            pages_min = int(self.pipeline_pages_min_var.get())
            pages_max = int(self.pipeline_pages_max_var.get())
            words_per_page_min = int(self.pipeline_words_per_page_min_var.get())
            words_per_page_max = int(self.pipeline_words_per_page_max_var.get())
        except ValueError as e:
            raise ValueError("Параметры страниц и слов должны быть целыми числами") from e

        if pages_min < 1:
            raise ValueError("Минимальное количество страниц должно быть не меньше 1")
        if pages_max < pages_min:
            raise ValueError("Максимальное количество страниц не может быть меньше минимального")
        if words_per_page_min < 1:
            raise ValueError("Минимальное количество слов на страницу должно быть не меньше 1")
        if words_per_page_max < words_per_page_min:
            raise ValueError("Максимальное количество слов на страницу не может быть меньше минимального")

        language = self.pipeline_language_var.get().strip()
        if not language:
            raise ValueError("Выберите язык pipeline")

        return {
            "pages_min": pages_min,
            "pages_max": pages_max,
            "words_per_page_min": words_per_page_min,
            "words_per_page_max": words_per_page_max,
            "language": language,
            "generate_screenplay": bool(self.generate_screenplay_var.get()),
            "force_update_prompts": bool(self.force_update_prompts_var.get()),
        }
    
    def create_ui(self):
        """Создание пользовательского интерфейса"""
        # Заголовок
        header_frame = ttk.Frame(self)
        header_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ttk.Label(header_frame, text="Управление генерацией", style="Title.TLabel").pack(side="left")
        
        # Статус генерации
        status_frame = ttk.Frame(header_frame)
        status_frame.pack(side="right")
        
        self.status_label = ttk.Label(status_frame, text="Готов к работе")
        self.status_label.pack(side="left", padx=(0, 10))
        
        self.pause_button = ttk.Button(status_frame, text="⏸ Пауза",
                                      command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(0, 5))

        self.stop_button = ttk.Button(status_frame, text="⏹ Остановить",
                                     command=self.stop_generation, state="disabled")
        self.stop_button.pack(side="left")
        
        # Разделитель
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=5)
        
        # Основная область
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Левая панель - управление
        left_frame = ttk.LabelFrame(main_frame, text="Операции генерации", padding=10)
        left_frame.pack(side="left", fill="y", padx=(0, 5))
        left_frame.config(width=300)
        
        self.create_generation_controls(left_frame)
        
        # Правая панель - логи и прогресс
        right_frame = ttk.LabelFrame(main_frame, text="Выполнение", padding=5)
        right_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))
        
        self.create_execution_panel(right_frame)
    
    def create_generation_controls(self, parent):
        """Создание элементов управления генерацией"""
        # Полный pipeline
        pipeline_frame = ttk.LabelFrame(parent, text="Полный pipeline", padding=10)
        pipeline_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(pipeline_frame, text="Описание сказки:").pack(anchor="w")
        self.task_text = tk.Text(pipeline_frame, height=4, wrap=tk.WORD)
        self.task_text.pack(fill="x", pady=(5, 10))

        self._create_pipeline_input_vars()

        pipeline_settings_frame = ttk.LabelFrame(
            pipeline_frame,
            text="Параметры pipeline",
            padding=10
        )
        pipeline_settings_frame.pack(fill="x", pady=(0, 10))

        pages_frame = ttk.Frame(pipeline_settings_frame)
        pages_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(pages_frame, text="Страницы:").pack(side="left")
        self.pipeline_pages_min_spinbox = ttk.Spinbox(
            pages_frame,
            from_=1,
            to=sys.maxsize,
            textvariable=self.pipeline_pages_min_var,
            width=8
        )
        self.pipeline_pages_min_spinbox.pack(side="left", padx=(5, 2))
        ttk.Label(pages_frame, text="—").pack(side="left", padx=2)
        self.pipeline_pages_max_spinbox = ttk.Spinbox(
            pages_frame,
            from_=1,
            to=sys.maxsize,
            textvariable=self.pipeline_pages_max_var,
            width=8
        )
        self.pipeline_pages_max_spinbox.pack(side="left", padx=(2, 0))

        words_frame = ttk.Frame(pipeline_settings_frame)
        words_frame.pack(fill="x", pady=6)
        ttk.Label(words_frame, text="Слов на страницу:").pack(side="left")
        self.pipeline_words_per_page_min_spinbox = ttk.Spinbox(
            words_frame,
            from_=1,
            to=sys.maxsize,
            textvariable=self.pipeline_words_per_page_min_var,
            width=8
        )
        self.pipeline_words_per_page_min_spinbox.pack(side="left", padx=(5, 2))
        ttk.Label(words_frame, text="—").pack(side="left", padx=2)
        self.pipeline_words_per_page_max_spinbox = ttk.Spinbox(
            words_frame,
            from_=1,
            to=sys.maxsize,
            textvariable=self.pipeline_words_per_page_max_var,
            width=8
        )
        self.pipeline_words_per_page_max_spinbox.pack(side="left", padx=(2, 0))

        language_frame = ttk.Frame(pipeline_settings_frame)
        language_frame.pack(fill="x", pady=6)
        ttk.Label(language_frame, text="Язык:").pack(side="left")
        self.pipeline_language_combo = ttk.Combobox(
            language_frame,
            textvariable=self.pipeline_language_var,
            state="readonly",
            width=10
        )
        self.pipeline_language_combo['values'] = tuple(self.supported_languages)
        self.pipeline_language_combo.pack(side="left", padx=(5, 0))

        options_frame = ttk.Frame(pipeline_settings_frame)
        options_frame.pack(fill="x", pady=(6, 0))
        self.generate_screenplay_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Генерировать screenplay",
            variable=self.generate_screenplay_var
        )
        self.generate_screenplay_checkbutton.pack(anchor="w")
        self.force_update_prompts_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Принудительно обновлять промпты видео",
            variable=self.force_update_prompts_var
        )
        self.force_update_prompts_checkbutton.pack(anchor="w", pady=(2, 0))

        ttk.Button(pipeline_frame, text="🚀 Запустить полный pipeline", 
                  command=self.run_full_pipeline).pack(fill="x")
        
        # Частичная генерация
        partial_frame = ttk.LabelFrame(parent, text="Частичная генерация", padding=10)
        partial_frame.pack(fill="x", pady=(0, 10))
        
        step_label_frame = ttk.Frame(partial_frame)
        step_label_frame.pack(fill="x", anchor="w")
        ttk.Label(step_label_frame, text="Начать с шага:").pack(side="left")
        ttk.Button(step_label_frame, text="🔄", 
                  command=self.refresh_pipeline_steps, width=3).pack(side="right")
        
        self.step_var = tk.StringVar()
        self.step_combo = ttk.Combobox(partial_frame, textvariable=self.step_var, state="readonly")
        self.step_combo['values'] = self.pipeline_steps
        self.step_combo.pack(fill="x", pady=(5, 10))
        self.pipeline_error_label = ttk.Label(partial_frame, text="", foreground="red")
        self._update_pipeline_error_state()
        
        ttk.Button(partial_frame, text="▶️ Запустить с шага", 
                  command=self.run_from_step).pack(fill="x")
        
        # Селективная регенерация
        regen_frame = ttk.LabelFrame(parent, text="Регенерация", padding=10)
        regen_frame.pack(fill="x", pady=(0, 10))
        
        # Регенерация изображений
        ttk.Label(regen_frame, text="Страница для регенерации:").pack(anchor="w")
        page_frame = ttk.Frame(regen_frame)
        page_frame.pack(fill="x", pady=(5, 10))
        
        self.page_var = tk.StringVar()
        page_spin = ttk.Spinbox(page_frame, from_=1, to=50, textvariable=self.page_var, width=10)
        page_spin.pack(side="left")
        
        ttk.Button(page_frame, text="🎨 Регенерировать изображение", 
                  command=self.regenerate_image).pack(side="right")
        
        # Регенерация видео
        ttk.Label(regen_frame, text="Кадр для регенерации:").pack(anchor="w")
        shot_frame = ttk.Frame(regen_frame)
        shot_frame.pack(fill="x", pady=5)
        
        ttk.Label(shot_frame, text="Сцена:").pack(side="left")
        self.scene_var = tk.StringVar()
        ttk.Spinbox(shot_frame, from_=1, to=20, textvariable=self.scene_var, width=5).pack(side="left", padx=(5, 10))
        
        ttk.Label(shot_frame, text="Кадр:").pack(side="left")
        self.shot_var = tk.StringVar()
        ttk.Spinbox(shot_frame, from_=1, to=10, textvariable=self.shot_var, width=5).pack(side="left", padx=5)
        
        ttk.Button(regen_frame, text="🎬 Регенерировать видео", 
                  command=self.regenerate_video).pack(fill="x", pady=(10, 0))
        
        # Валидация проекта
        validation_frame = ttk.LabelFrame(parent, text="Валидация", padding=10)
        validation_frame.pack(fill="x")
        
        ttk.Button(validation_frame, text="✓ Проверить проект", 
                  command=self.validate_project).pack(fill="x", pady=2)
        ttk.Button(validation_frame, text="🔧 Исправить ошибки", 
                  command=self.fix_project_errors).pack(fill="x", pady=2)
    
    def create_execution_panel(self, parent):
        """Создание панели выполнения"""
        # Трекер шагов
        self.step_tracker = StepTracker(
            parent,
            on_restart_requested=self._restart_pipeline_step_from_tracker,
        )
        self.step_tracker.pack(fill="x", pady=(0, 5))
        self.step_tracker.set_steps(self.pipeline_steps)

        # Прогресс
        progress_frame = ttk.Frame(parent)
        progress_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(progress_frame, text="Прогресс выполнения:").pack(anchor="w")
        
        self.progress_bar = ttk.Progressbar(
            progress_frame, 
            mode="determinate",
            length=400
        )
        self.progress_bar.pack(fill="x", pady=5)
        
        self.progress_label = ttk.Label(progress_frame, text="0%")
        self.progress_label.pack(anchor="w")
        
        # Логи выполнения
        logs_frame = ttk.LabelFrame(parent, text="Логи выполнения", padding=5)
        logs_frame.pack(fill="both", expand=True)
        
        self.logs_text = scrolledtext.ScrolledText(
            logs_frame,
            wrap=tk.WORD,
            font=("Courier", 9),
            state="disabled"
        )
        self.logs_text.pack(fill="both", expand=True)
        
        # Настройка тегов для раскраски логов
        self.logs_text.tag_configure("info", foreground="#0000FF")
        self.logs_text.tag_configure("success", foreground="#008000")
        self.logs_text.tag_configure("warning", foreground="#FF8000")
        self.logs_text.tag_configure("error", foreground="#FF0000")
        
        # Кнопки управления логами
        logs_buttons = ttk.Frame(logs_frame)
        logs_buttons.pack(fill="x", pady=(5, 0))
        
        ttk.Button(logs_buttons, text="🗑️ Очистить", command=self.clear_logs).pack(side="left", padx=2)
        ttk.Button(logs_buttons, text="💾 Сохранить", command=self.save_logs).pack(side="left", padx=2)
        ttk.Button(logs_buttons, text="📋 Копировать", command=self.copy_logs).pack(side="left", padx=2)
    
    def load_project(self, project: Project):
        """Загрузка проекта"""
        try:
            self.current_project = project
            self._reset_pipeline_settings_to_defaults()
            
            # Загружаем описание из brief если есть
            if project.brief_data:
                description = project.brief_data.get("storybook_prompt", "")
                if description:
                    self.task_text.delete("1.0", tk.END)
                    self.task_text.insert("1.0", description)
                self._apply_project_pipeline_settings(project.brief_data)
            
            self.add_log(f"Проект {project.project_id} загружен", "info")
            logger.info(f"Проект {project.project_id} загружен в генерацию")

            self._check_incomplete_workflows(project.project_id)

        except Exception as e:
            logger.error(f"Ошибка загрузки проекта в панель генерации: {e}")
            messagebox.showerror("Ошибка", f"Не удалось загрузить проект:\n{e}")
    
    def _check_incomplete_workflows(self, project_id: str):
        """Проверяет наличие незавершённых workflow в фоновом потоке."""
        self._incomplete_workflows: list = []

        def _check_in_thread():
            loop = asyncio.new_event_loop()
            try:
                incomplete = loop.run_until_complete(
                    self.pipeline_runner.get_incomplete_workflows(project_id)
                )
            finally:
                loop.close()

            if incomplete:
                self._incomplete_workflows = incomplete
                wf = incomplete[0]
                self.after(0, lambda: self._show_recovery_dialog(wf))

        threading.Thread(target=_check_in_thread, daemon=True).start()

    def _show_recovery_dialog(self, workflow_info: dict):
        """Показывает диалог восстановления незавершённого pipeline."""
        status = workflow_info.get("status", "неизвестен")
        step = workflow_info.get("current_step", "неизвестен")
        completed = len(workflow_info.get("completed_steps", []))
        timestamp = workflow_info.get("timestamp", "неизвестно")
        wf_id = workflow_info.get("workflow_id", "")

        self.add_log(
            f"⚠️ Обнаружен незавершённый pipeline "
            f"(статус: {status}, шаг: {step}, "
            f"завершено шагов: {completed})",
            "warning"
        )

        result = messagebox.askyesno(
            "Незавершённый pipeline",
            f"Обнаружен незавершённый pipeline:\n\n"
            f"Статус: {status}\n"
            f"Последний шаг: {step}\n"
            f"Завершено шагов: {completed}\n"
            f"Время: {timestamp}\n\n"
            f"Возобновить выполнение?"
        )

        if result:
            self.add_log("▶ Пользователь выбрал возобновление pipeline", "info")
            self._resume_from_checkpoint(workflow_info)
        else:
            self.add_log("🔄 Пользователь выбрал начать сначала", "info")

    def _resume_from_checkpoint(self, workflow_info: dict):
        """Возобновляет pipeline из сохранённого checkpoint-контекста."""
        completed_steps = workflow_info.get("completed_steps", [])
        workflow_id = workflow_info.get("workflow_id")
        current_step = workflow_info.get("current_step")

        if not self.current_project:
            self.add_log("❌ Проект не выбран для восстановления", "error")
            return

        if not workflow_id:
            self.add_log("❌ Для восстановления отсутствует workflow_id", "error")
            return

        # Тот же guard от двойного запуска, что и в обычных стартах pipeline:
        # без него двойной клик «Возобновить» мог пройти is_generating==False дважды.
        already_running = False
        with self._generation_lock:
            if self.is_generating:
                already_running = True
            else:
                self.is_generating = True
        if already_running:
            self.add_log("Генерация уже выполняется", "warning")
            return

        try:
            self.step_tracker.reset()
            for step_id in completed_steps:
                self.step_tracker.update_step(step_id, "completed")

            # Раньше восстановление шло через _run_from_step_thread и current_step,
            # теперь используем checkpoint context, чтобы не терять промежуточные outputs.
            self.add_log(f"▶ Возобновление pipeline из checkpoint {workflow_id}", "info")
            self.start_generation("Восстановление pipeline", {
                "project_id": self.current_project.project_id,
                "workflow_id": workflow_id,
                "completed_steps": completed_steps,
                "current_step": current_step,
            })

            self.generation_thread = threading.Thread(
                target=self._run_resume_pipeline_thread,
                args=(workflow_id, completed_steps),
                daemon=True
            )
            self.generation_thread.start()
        except Exception:
            self.is_generating = False
            raise

    def run_full_pipeline(self):
        """Запуск полного pipeline"""
        if self._pipeline_load_error:
            messagebox.showerror(
                "Pipeline не загружен",
                f"{self._pipeline_load_error}\n\n"
                "Проверьте наличие файла workflow_pipelines/storybook_pipeline.yaml"
            )
            return

        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        already_running = False
        with self._generation_lock:
            if self.is_generating:
                already_running = True
            else:
                self.is_generating = True
        if already_running:
            messagebox.showwarning("Предупреждение", "Генерация уже выполняется")
            return

        task = self.task_text.get("1.0", tk.END).strip()
        if not task:
            self.is_generating = False
            messagebox.showwarning("Предупреждение", "Введите описание сказки")
            return

        try:
            pipeline_inputs = self._collect_pipeline_params()
        except ValueError as e:
            self.is_generating = False
            messagebox.showerror("Ошибка параметров pipeline", str(e))
            return

        try:
            # Валидация проекта перед запуском
            validation_result = self.pipeline_runner.validate_project_for_pipeline(self.current_project.project_id)
            if not validation_result.get("valid", False):
                result = messagebox.askyesno(
                    "Ошибки валидации",
                    f"Проект содержит ошибки:\n{validation_result.get('message', '')}\n\nВсе равно запустить?"
                )
                if not result:
                    # Отмена ДО start_generation: снимаем guard-флаг и явно
                    # возвращаем кнопки в disabled (идемпотентно — они и так
                    # disabled, но защищает от будущих правок порядка вызовов).
                    self.is_generating = False
                    self.stop_button.config(state="disabled")
                    self.pause_button.config(state="disabled", text="⏸ Пауза")
                    return

            # Запуск в отдельном потоке
            self.start_generation("Полный pipeline", {
                "project_id": self.current_project.project_id,
                "task": task,
                **pipeline_inputs,
            })

            # Запускаем pipeline
            self.generation_thread = threading.Thread(
                target=self._run_full_pipeline_thread,
                args=(self.current_project.project_id, task, pipeline_inputs),
                daemon=True
            )
            self.generation_thread.start()
        except Exception:
            self.is_generating = False
            raise
    
    def _run_full_pipeline_thread(self, project_id: str, task: str,
                                  pipeline_inputs: Dict[str, Any]):
        """Запуск полного pipeline в отдельном потоке"""
        try:
            self.add_log(f"Запуск полного pipeline для проекта {project_id}", "info")
            self.add_log(f"Задача: {task}", "info")

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                self.add_log(message, level)
                if progress is not None:
                    self.update_progress(progress, message)
                if step_id and step_status:
                    self.step_tracker.update_step(step_id, step_status, step_duration)

            result = run_pipeline_sync(
                self.pipeline_runner,
                project_id,
                task,
                progress_callback,
                input_overrides=pipeline_inputs,
            )

            if result.get("status") == "cancelled":
                self.add_log("⏹ Pipeline отменён после завершения активных шагов", "warning")
            elif result.get("status") == "success":
                self.add_log("✅ Pipeline завершен успешно!", "success")
                self.update_progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                self.add_log(f"❌ Pipeline завершен с ошибкой: {error_msg}", "error")
                self.update_progress(0, "Ошибка")

        except Exception as e:
            logger.error(f"Ошибка выполнения pipeline: {e}")
            self.add_log(f"❌ Критическая ошибка: {e}", "error")
        finally:
            self.finish_generation()
    
    def _run_from_step_thread(self, project_id: str, step_id: str,
                              task: Optional[str], pipeline_inputs: Dict[str, Any]):
        """Запуск pipeline с определенного шага в отдельном потоке"""
        try:
            self.add_log(f"Запуск частичного pipeline для проекта {project_id} с шага {step_id}", "info")
            
            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                self.add_log(message, level)
                if progress is not None:
                    self.update_progress(progress, message)
                if step_id and step_status:
                    self.step_tracker.update_step(step_id, step_status, step_duration)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    self.pipeline_runner.run_from_step(
                        project_id,
                        step_id,
                        progress_callback,
                        task=task,
                        input_overrides=pipeline_inputs,
                    )
                )
            finally:
                loop.close()
            
            # Обрабатываем результат
            if result.get("status") == "cancelled":
                self.add_log("⏹ Частичный pipeline отменён после завершения активных шагов", "warning")
            elif result.get("status") == "success":
                skipped_steps = result.get("skipped_steps", 0)
                self.add_log(f"✅ Частичный pipeline завершен успешно! Пропущено шагов: {skipped_steps}", "success")
                self.update_progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                self.add_log(f"❌ Частичный pipeline завершен с ошибкой: {error_msg}", "error")
                self.update_progress(0, "Ошибка")
            
        except Exception as e:
            logger.error(f"Ошибка выполнения частичного pipeline: {e}")
            self.add_log(f"❌ Критическая ошибка: {e}", "error")
        finally:
            self.finish_generation()

    def _run_resume_pipeline_thread(self, workflow_id: str, completed_steps: List[str]):
        """Возобновляет workflow из checkpoint в отдельном потоке."""
        try:
            self.add_log(
                f"Возобновление pipeline из checkpoint {workflow_id}",
                "info",
            )

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                self.add_log(message, level)
                if progress is not None:
                    self.update_progress(progress, message)
                if step_id and step_status:
                    self.step_tracker.update_step(step_id, step_status, step_duration)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    self.pipeline_runner.resume_workflow_from_checkpoint(
                        workflow_id,
                        progress_callback,
                    )
                )
            finally:
                loop.close()

            if result.get("status") == "cancelled":
                self.add_log("⏹ Восстановленный pipeline отменён", "warning")
            elif result.get("status") == "success":
                self.add_log("✅ Pipeline успешно восстановлен из checkpoint", "success")
                self.update_progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                self.add_log(f"❌ Ошибка восстановления pipeline: {error_msg}", "error")
                self.update_progress(0, "Ошибка")
        except Exception as e:
            logger.error(f"Ошибка восстановления workflow {workflow_id}: {e}")
            self.add_log(f"❌ Критическая ошибка восстановления: {e}", "error")
        finally:
            self.finish_generation()

    def _run_single_step_thread(self, project_id: str, step_id: str,
                                task: Optional[str], pipeline_inputs: Dict[str, Any]):
        """Перезапускает только один шаг pipeline в отдельном потоке."""
        try:
            self.add_log(
                f"Перезапуск одного шага для проекта {project_id}: {step_id}",
                "info",
            )

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                self.add_log(message, level)
                if progress is not None:
                    self.update_progress(progress, message)
                if step_id and step_status:
                    self.step_tracker.update_step(step_id, step_status, step_duration)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    self.pipeline_runner.rerun_single_step(
                        project_id,
                        step_id,
                        progress_callback,
                        task=task,
                        input_overrides=pipeline_inputs,
                    )
                )
            finally:
                loop.close()

            if result.get("status") == "success":
                self.add_log(
                    f"✅ Шаг '{step_id}' перезапущен успешно без повтора остальных шагов",
                    "success",
                )
                self.update_progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                self.add_log(
                    f"❌ Перезапуск шага '{step_id}' завершился с ошибкой: {error_msg}",
                    "error",
                )
                self.update_progress(0, "Ошибка")

        except Exception as e:
            logger.error(f"Ошибка single-step rerun для шага {step_id}: {e}")
            self.add_log(f"❌ Критическая ошибка: {e}", "error")
        finally:
            self.finish_generation()
    
    def run_from_step(self):
        """Запуск pipeline с определенного шага"""
        if self._pipeline_load_error:
            messagebox.showerror(
                "Pipeline не загружен",
                f"{self._pipeline_load_error}\n\n"
                "Проверьте наличие файла workflow_pipelines/storybook_pipeline.yaml"
            )
            return

        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        step_id = self.step_var.get()
        if not step_id:
            messagebox.showwarning("Предупреждение", "Выберите шаг для запуска")
            return
        
        already_running = False
        with self._generation_lock:
            if self.is_generating:
                already_running = True
            else:
                self.is_generating = True
        if already_running:
            messagebox.showwarning("Предупреждение", "Генерация уже выполняется")
            return

        try:
            pipeline_inputs = self._collect_pipeline_params()
        except ValueError as e:
            self.is_generating = False
            messagebox.showerror("Ошибка параметров pipeline", str(e))
            return

        task = self.task_text.get("1.0", tk.END).strip() or None

        try:
            # Валидация проекта перед запуском
            validation_result = self.pipeline_runner.validate_project_for_pipeline(
                self.current_project.project_id,
                start_step=step_id,
            )
            if not validation_result.get("valid", False):
                result = messagebox.askyesno(
                    "Ошибки валидации",
                    f"Проект содержит ошибки:\n{validation_result.get('message', '')}\n\nВсе равно запустить?"
                )
                if not result:
                    # Отмена ДО start_generation: снимаем guard-флаг и явно
                    # возвращаем кнопки в disabled (идемпотентно — они и так
                    # disabled, но защищает от будущих правок порядка вызовов).
                    self.is_generating = False
                    self.stop_button.config(state="disabled")
                    self.pause_button.config(state="disabled", text="⏸ Пауза")
                    return

            # Запуск в отдельном потоке
            self.start_generation(f"Частичный pipeline с {step_id}", {
                "project_id": self.current_project.project_id,
                "start_step": step_id,
                **pipeline_inputs,
                **({"task": task} if task else {}),
            })

            # Запускаем pipeline
            self.generation_thread = threading.Thread(
                target=self._run_from_step_thread,
                args=(self.current_project.project_id, step_id, task, pipeline_inputs),
                daemon=True
            )
            self.generation_thread.start()
        except Exception:
            self.is_generating = False
            raise

    def _restart_pipeline_step_from_tracker(self, step_id: str):
        """Перезапускает шаг из контекстного меню StepTracker."""
        if self._pipeline_load_error:
            messagebox.showerror(
                "Pipeline не загружен",
                f"{self._pipeline_load_error}\n\n"
                "Проверьте наличие файла workflow_pipelines/storybook_pipeline.yaml"
            )
            return

        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        already_running = False
        with self._generation_lock:
            if self.is_generating:
                already_running = True
            else:
                self.is_generating = True
        if already_running:
            messagebox.showwarning("Предупреждение", "Генерация уже выполняется")
            return

        try:
            pipeline_inputs = self._collect_pipeline_params()
        except ValueError as e:
            self.is_generating = False
            messagebox.showerror("Ошибка параметров pipeline", str(e))
            return

        task = self.task_text.get("1.0", tk.END).strip() or None

        try:
            self.start_generation(f"Перезапуск шага {step_id}", {
                "project_id": self.current_project.project_id,
                "step_id": step_id,
                **pipeline_inputs,
                **({"task": task} if task else {}),
            })

            self.generation_thread = threading.Thread(
                target=self._run_single_step_thread,
                args=(self.current_project.project_id, step_id, task, pipeline_inputs),
                daemon=True,
            )
            self.generation_thread.start()
        except Exception:
            self.is_generating = False
            raise

    def regenerate_image(self):
        """Регенерация изображения"""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return
        
        try:
            page_num = int(self.page_var.get() or "1")
        except ValueError:
            messagebox.showerror("Ошибка", "Неверный номер страницы")
            return
        
        if self.is_generating:
            messagebox.showwarning("Предупреждение", "Генерация уже выполняется")
            return
        
        # TODO: Реализовать регенерацию изображения
        self.add_log(f"Запуск регенерации изображения для страницы {page_num}", "info")
        messagebox.showinfo("Информация", "Функция регенерации изображений будет реализована позже")
    
    def regenerate_video(self):
        """Регенерация видео"""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return
        
        try:
            scene_num = int(self.scene_var.get() or "1")
            shot_num = int(self.shot_var.get() or "1")
        except ValueError:
            messagebox.showerror("Ошибка", "Неверные номера сцены или кадра")
            return
        
        if self.is_generating:
            messagebox.showwarning("Предупреждение", "Генерация уже выполняется")
            return
        
        # TODO: Реализовать регенерацию видео
        self.add_log(f"Запуск регенерации видео для сцены {scene_num}, кадр {shot_num}", "info")
        messagebox.showinfo("Информация", "Функция регенерации видео будет реализована позже")
    
    def validate_project(self):
        """Валидация проекта"""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return
        
        try:
            self.add_log("Запуск валидации проекта...", "info")
            
            result = self.pipeline_runner.validate_project_for_pipeline(self.current_project.project_id)
            
            if result.get("valid", False):
                self.add_log("✅ Проект прошел валидацию", "success")
            else:
                message = result.get("message", "Неизвестная ошибка")
                self.add_log(f"❌ Ошибки валидации: {message}", "error")
            
        except Exception as e:
            logger.error(f"Ошибка валидации проекта: {e}")
            self.add_log(f"❌ Ошибка валидации: {e}", "error")
    
    def fix_project_errors(self):
        """Исправление ошибок проекта"""
        # TODO: Реализовать автоматическое исправление ошибок
        messagebox.showinfo("Информация", "Функция автоматического исправления ошибок будет реализована позже")
    
    def start_generation(self, generation_type: str, params: Dict[str, Any]):
        """Начало генерации.

        Предусловие: self.is_generating уже выставлен в True под self._generation_lock
        вызывающим (test-and-set guard в run_full_pipeline/run_from_step/resume/restart).
        Поэтому здесь флаг повторно НЕ ставим — иначе дублируется защита от гонки и
        новый, не прошедший guard вызывающий мог бы её незаметно обойти.
        """
        import time
        self._is_paused = False
        self._generation_start_time = time.time()
        self._cancel_event.clear()
        self.stop_button.config(state="normal")
        self.pause_button.config(state="normal", text="⏸ Пауза")
        self.status_label.config(text=f"Выполняется: {generation_type}")
        self.update_progress(0, "Инициализация...")
        
        # Уведомляем родительское окно
        if self.on_generation_started:
            self.on_generation_started(generation_type, params)
    
    def finish_generation(self):
        """Завершение генерации (thread-safe через self.after)"""
        import time
        elapsed = time.time() - getattr(self, '_generation_start_time', time.time())
        if elapsed < 60:
            elapsed_str = f"{elapsed:.0f} сек"
        else:
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            elapsed_str = f"{minutes} мин {seconds} сек"

        def _update_ui():
            # Сброс is_generating=False намеренно без _generation_lock: запись bool
            # атомарна в CPython, а блокировки требует только test-and-set в guard'ах
            # запуска (там lock уже берётся). _update_ui всегда исполняется в Tk-потоке.
            self.is_generating = False
            self._is_paused = False
            self.stop_button.config(state="disabled")
            self.pause_button.config(state="disabled", text="⏸ Пауза")
            self.status_label.config(text=f"Завершено за {elapsed_str}")
            self.generation_thread = None

        self.after(0, _update_ui)
    
    def toggle_pause(self):
        """Переключение паузы pipeline"""
        if not self.is_generating:
            return

        if not self._is_paused:
            self._is_paused = True
            self.pause_button.config(text="▶ Продолжить")
            self.add_log("⏸ Pipeline поставлен на паузу", "warning")
            self.status_label.config(text="На паузе")

            def _pause_in_thread():
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self.pipeline_runner.pause_pipeline()
                    )
                finally:
                    loop.close()

            threading.Thread(target=_pause_in_thread, daemon=True).start()
        else:
            self._is_paused = False
            self.pause_button.config(text="⏸ Пауза")
            self.add_log("▶ Pipeline возобновлён", "info")
            self.status_label.config(text="Выполняется")

            def _resume_in_thread():
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        self.pipeline_runner.resume_pipeline()
                    )
                finally:
                    loop.close()

            threading.Thread(target=_resume_in_thread, daemon=True).start()

    def stop_generation(self):
        """Остановка генерации через engine.cancel_workflow()"""
        if not self.is_generating:
            return

        result = messagebox.askyesno(
            "Остановка генерации",
            "Вы действительно хотите остановить выполнение?\n"
            "Текущий шаг будет завершён, прогресс сохранится в чекпоинте."
        )
        if not result:
            return

        self._cancel_event.set()
        # Снимаем паузу чтобы worker thread разблокировался
        self.pipeline_runner._pause_event.set()
        self.status_label.config(text="Остановка после активных шагов...")
        self.add_log("⏹ Отправлен сигнал остановки...", "warning")

        def _cancel_in_thread():
            try:
                loop = asyncio.new_event_loop()
                try:
                    cancel_result = loop.run_until_complete(
                        self.pipeline_runner.cancel_pipeline()
                    )
                finally:
                    loop.close()

                if cancel_result.get("status") == "cancelled":
                    wf_id = cancel_result.get("workflow_id", "")
                    self.add_log(
                        f"⏹ Pipeline отменён (workflow: {wf_id})", "warning"
                    )
                else:
                    msg = cancel_result.get("message", "Неизвестная ошибка")
                    self.add_log(f"⚠️ Ошибка отмены: {msg}", "error")
            except Exception as e:
                self.add_log(f"⚠️ Ошибка при отмене: {e}", "error")

        threading.Thread(target=_cancel_in_thread, daemon=True).start()
    
    def update_progress(self, progress: float, message: str = ""):
        """Обновление прогресса"""
        def update_ui():
            self.progress_bar['value'] = progress
            self.progress_label.config(text=f"{progress:.1f}%")
            if message:
                self.status_label.config(text=message)
        
        # Обновляем UI в главном потоке
        self.after(0, update_ui)
    
    def add_log(self, message: str, level: str = "info"):
        """Добавление сообщения в лог (UI + файл)"""
        log_method = getattr(logger, level if level != "success" else "info", logger.info)
        log_method(f"[GENERATION] {message}")

        def update_log():
            self.logs_text.config(state="normal")
            
            # Добавляем временную метку
            import datetime
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            log_message = f"[{timestamp}] {message}\n"
            
            # Вставляем сообщение с соответствующим тегом
            self.logs_text.insert(tk.END, log_message, level)
            
            # Прокручиваем к концу
            self.logs_text.see(tk.END)
            self.logs_text.config(state="disabled")
        
        # Обновляем UI в главном потоке
        self.after(0, update_log)
    
    def clear_logs(self):
        """Очистка логов"""
        self.logs_text.config(state="normal")
        self.logs_text.delete("1.0", tk.END)
        self.logs_text.config(state="disabled")
    
    def save_logs(self):
        """Сохранение логов в файл"""
        try:
            from tkinter import filedialog
            import datetime
            
            default_name = f"storybook_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            file_path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialvalue=default_name
            )
            
            if file_path:
                logs_content = self.logs_text.get("1.0", tk.END)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(logs_content)
                messagebox.showinfo("Успех", f"Логи сохранены в {file_path}")
                
        except Exception as e:
            logger.error(f"Ошибка сохранения логов: {e}")
            messagebox.showerror("Ошибка", f"Не удалось сохранить логи:\n{e}")
    
    def copy_logs(self):
        """Копирование логов в буфер обмена"""
        try:
            logs_content = self.logs_text.get("1.0", tk.END)
            self.clipboard_clear()
            self.clipboard_append(logs_content)
            messagebox.showinfo("Успех", "Логи скопированы в буфер обмена")
        except Exception as e:
            logger.error(f"Ошибка копирования логов: {e}")
            messagebox.showerror("Ошибка", f"Не удалось скопировать логи:\n{e}")
