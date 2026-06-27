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
    
    def __init__(self, parent, on_generation_started: Callable,
                 on_generation_complete: Optional[Callable] = None):
        super().__init__(parent)

        self.on_generation_started = on_generation_started
        self.on_generation_complete = on_generation_complete
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
        self.screenplay_time_var = tk.StringVar()
        self.generate_screenplay_var = tk.BooleanVar()
        self.generate_end_shots_var = tk.BooleanVar()
        self.force_update_prompts_var = tk.BooleanVar()
        self.skip_prompt_enhancement_var = tk.BooleanVar()
        self.sample_before_batch_var = tk.BooleanVar()
        self.sample_shot_key_var = tk.StringVar()
        self.generate_music_var = tk.BooleanVar()
        self.final_allow_missing_audio_var = tk.BooleanVar()
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
        self.screenplay_time_var.set(str(self.pipeline_inputs.get("screenplay_time", "")))
        self.generate_screenplay_var.set(
            self._to_bool(self.pipeline_inputs.get("generate_screenplay", False))
        )
        self.generate_end_shots_var.set(
            self._to_bool(self.pipeline_inputs.get("generate_end_shots", False))
        )
        self.force_update_prompts_var.set(
            self._to_bool(self.pipeline_inputs.get("force_update_prompts", False))
        )
        self.skip_prompt_enhancement_var.set(
            self._to_bool(self.pipeline_inputs.get("skip_prompt_enhancement", False))
        )
        self.sample_before_batch_var.set(
            self._to_bool(self.pipeline_inputs.get("sample_before_batch", False))
        )
        self.sample_shot_key_var.set(str(self.pipeline_inputs.get("sample_shot_key", "")))
        self.generate_music_var.set(
            self._to_bool(self.pipeline_inputs.get("generate_music", False))
        )
        self.final_allow_missing_audio_var.set(
            self._to_bool(self.pipeline_inputs.get("final_allow_missing_audio", False))
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
        if "generate_end_shots" in brief_data:
            self.generate_end_shots_var.set(self._to_bool(brief_data.get("generate_end_shots")))
        if "screenplay_time" in brief_data and brief_data.get("screenplay_time") not in (None, ""):
            self.screenplay_time_var.set(str(brief_data.get("screenplay_time")))
        if "force_update_prompts" in brief_data:
            self.force_update_prompts_var.set(self._to_bool(brief_data.get("force_update_prompts")))
        if "skip_prompt_enhancement" in brief_data:
            self.skip_prompt_enhancement_var.set(
                self._to_bool(brief_data.get("skip_prompt_enhancement"))
            )
        if "sample_before_batch" in brief_data:
            self.sample_before_batch_var.set(self._to_bool(brief_data.get("sample_before_batch")))
        if "sample_shot_key" in brief_data:
            self.sample_shot_key_var.set(str(brief_data.get("sample_shot_key") or "").strip())
        if "generate_music" in brief_data:
            self.generate_music_var.set(self._to_bool(brief_data.get("generate_music")))
        if "final_allow_missing_audio" in brief_data:
            self.final_allow_missing_audio_var.set(
                self._to_bool(brief_data.get("final_allow_missing_audio"))
            )
        self._sync_sample_shot_key_state()

    def _collect_pipeline_params(self) -> Dict[str, Any]:
        """Собирает и валидирует параметры pipeline из UI."""
        try:
            pages_min = int(self.pipeline_pages_min_var.get())
            pages_max = int(self.pipeline_pages_max_var.get())
            words_per_page_min = int(self.pipeline_words_per_page_min_var.get())
            words_per_page_max = int(self.pipeline_words_per_page_max_var.get())
            screenplay_time = int(self.screenplay_time_var.get())
        except ValueError as e:
            raise ValueError("Параметры страниц, слов и времени должны быть целыми числами") from e

        if pages_min < 1:
            raise ValueError("Минимальное количество страниц должно быть не меньше 1")
        if pages_max < pages_min:
            raise ValueError("Максимальное количество страниц не может быть меньше минимального")
        if words_per_page_min < 1:
            raise ValueError("Минимальное количество слов на страницу должно быть не меньше 1")
        if words_per_page_max < words_per_page_min:
            raise ValueError("Максимальное количество слов на страницу не может быть меньше минимального")
        if screenplay_time < 1:
            raise ValueError("Длительность screenplay должна быть не меньше 1 секунды")

        language = self.pipeline_language_var.get().strip()
        if not language:
            raise ValueError("Выберите язык pipeline")

        sample_before_batch = bool(self.sample_before_batch_var.get())
        sample_shot_key = self.sample_shot_key_var.get().strip() if sample_before_batch else ""
        if sample_shot_key:
            sample_parts = sample_shot_key.split("-", 1)
            if len(sample_parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in sample_parts):
                raise ValueError("Sample shot должен быть в формате scene-shot, например 1-2")

        return {
            "pages_min": pages_min,
            "pages_max": pages_max,
            "words_per_page_min": words_per_page_min,
            "words_per_page_max": words_per_page_max,
            "language": language,
            "screenplay_time": screenplay_time,
            "generate_screenplay": bool(self.generate_screenplay_var.get()),
            "generate_end_shots": bool(self.generate_end_shots_var.get()),
            "force_update_prompts": bool(self.force_update_prompts_var.get()),
            "skip_prompt_enhancement": bool(self.skip_prompt_enhancement_var.get()),
            "sample_before_batch": sample_before_batch,
            "sample_shot_key": sample_shot_key,
            "generate_music": bool(self.generate_music_var.get()),
            "final_allow_missing_audio": bool(self.final_allow_missing_audio_var.get()),
        }

    def _sync_sample_shot_key_state(self):
        """Включает поле sample-shot только для sample-before-batch режима."""
        if not hasattr(self, "sample_shot_key_combo"):
            return
        state = "normal" if bool(self.sample_before_batch_var.get()) else "disabled"
        self.sample_shot_key_combo.config(state=state)
    
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

        screenplay_time_frame = ttk.Frame(pipeline_settings_frame)
        screenplay_time_frame.pack(fill="x", pady=6)
        ttk.Label(screenplay_time_frame, text="Длительность видео, сек.:").pack(side="left")
        self.screenplay_time_spinbox = ttk.Spinbox(
            screenplay_time_frame,
            from_=1,
            to=sys.maxsize,
            textvariable=self.screenplay_time_var,
            width=8
        )
        self.screenplay_time_spinbox.pack(side="left", padx=(5, 0))

        options_frame = ttk.Frame(pipeline_settings_frame)
        options_frame.pack(fill="x", pady=(6, 0))
        self.generate_screenplay_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Генерировать screenplay",
            variable=self.generate_screenplay_var
        )
        self.generate_screenplay_checkbutton.pack(anchor="w")
        self.generate_end_shots_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Генерировать финальные кадры shots",
            variable=self.generate_end_shots_var
        )
        self.generate_end_shots_checkbutton.pack(anchor="w", pady=(2, 0))
        self.force_update_prompts_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Принудительно обновлять промпты видео",
            variable=self.force_update_prompts_var
        )
        self.force_update_prompts_checkbutton.pack(anchor="w", pady=(2, 0))
        self.skip_prompt_enhancement_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Не улучшать video-промпты LLM",
            variable=self.skip_prompt_enhancement_var
        )
        self.skip_prompt_enhancement_checkbutton.pack(anchor="w", pady=(2, 0))
        self.sample_before_batch_checkbutton = ttk.Checkbutton(
            options_frame,
            text="Сначала sample-shot перед batch",
            variable=self.sample_before_batch_var,
            command=self._sync_sample_shot_key_state
        )
        self.sample_before_batch_checkbutton.pack(anchor="w", pady=(2, 0))

        sample_frame = ttk.Frame(pipeline_settings_frame)
        sample_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(sample_frame, text="Sample shot:").pack(side="left")
        self.sample_shot_key_combo = ttk.Combobox(
            sample_frame,
            textvariable=self.sample_shot_key_var,
            width=10
        )
        self.sample_shot_key_combo.pack(side="left", padx=(5, 0))
        ttk.Label(sample_frame, text="формат 1-2").pack(side="left", padx=(5, 0))

        final_frame = ttk.Frame(pipeline_settings_frame)
        final_frame.pack(fill="x", pady=(6, 0))
        self.final_allow_missing_audio_checkbutton = ttk.Checkbutton(
            final_frame,
            text="Разрешить финальную сборку без audio",
            variable=self.final_allow_missing_audio_var
        )
        self.final_allow_missing_audio_checkbutton.pack(anchor="w")
        self.generate_music_checkbutton = ttk.Checkbutton(
            final_frame,
            text="Генерировать музыку Suno",
            variable=self.generate_music_var
        )
        self.generate_music_checkbutton.pack(anchor="w", pady=(2, 0))
        self._sync_sample_shot_key_state()

        ttk.Button(pipeline_frame, text="🚀 Запустить полный pipeline",
                  command=self.run_full_pipeline,
                  style="Accent.TButton").pack(fill="x")
        
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
                  command=self.regenerate_image, state="disabled").pack(side="right")
        
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
                  command=self.regenerate_video, state="disabled").pack(fill="x", pady=(10, 0))
        
        # Валидация проекта
        validation_frame = ttk.LabelFrame(parent, text="Валидация", padding=10)
        validation_frame.pack(fill="x")
        
        ttk.Button(validation_frame, text="✓ Проверить проект", 
                  command=self.validate_project).pack(fill="x", pady=2)
        ttk.Button(validation_frame, text="🎚 Проверить video/music providers",
                  command=self.check_provider_readiness).pack(fill="x", pady=2)
        ttk.Button(validation_frame, text="🔧 Исправить ошибки",
                  command=self.fix_project_errors, state="disabled").pack(fill="x", pady=2)
    
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
        
        # Настройка тегов для раскраски логов (тёплая палитра)
        self.logs_text.tag_configure("info", foreground="#2B5797")
        self.logs_text.tag_configure("success", foreground="#4A7C59")
        self.logs_text.tag_configure("warning", foreground="#8B6914")
        self.logs_text.tag_configure("error", foreground="#9B2335")
        
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

        project_id = self.current_project.project_id
        generation_params = {
            "project_id": project_id,
            "task": task,
            **pipeline_inputs,
        }

        # Показываем лёгкий статус без перевода UI в режим генерации
        self.after(0, lambda: self.add_log("Проверка проекта...", "info"))

        def _validate_then_run():
            try:
                validation_result = self.pipeline_runner.validate_project_for_pipeline(project_id)
                if not validation_result.get("valid", False):
                    msg = validation_result.get("message", "")

                    proceed = threading.Event()
                    user_said_yes = [False]

                    def _ask():
                        user_said_yes[0] = messagebox.askyesno(
                            "Ошибки валидации",
                            f"Проект содержит ошибки:\n{msg}\n\nВсе равно запустить?"
                        )
                        proceed.set()

                    self.after(0, _ask)
                    proceed.wait()

                    if not user_said_yes[0]:
                        with self._generation_lock:
                            self.is_generating = False
                        return

                # Валидация прошла — старт UI-состояния и потока в одном Tk-колбэке,
                # чтобы гарантировать: start_generation (сброс cancel-event, кнопки)
                # выполняется ДО thread.start(), а self.generation_thread пишется
                # только в Tk-потоке (нет гонки атрибута).
                def _begin(pid=project_id, t=task, pi=pipeline_inputs, gp=generation_params):
                    self.start_generation("Полный pipeline", gp)
                    self.generation_thread = threading.Thread(
                        target=self._run_full_pipeline_thread,
                        args=(pid, t, pi),
                        daemon=True,
                    )
                    self.generation_thread.start()
                self.after(0, _begin)
            except Exception as exc:
                logger.error(f"Ошибка предстартовой валидации: {exc}")
                self.after(0, lambda e=exc: self.add_log(f"❌ Ошибка валидации: {e}", "error"))
                self.after(0, self.finish_generation)
                with self._generation_lock:
                    self.is_generating = False

        threading.Thread(target=_validate_then_run, daemon=True).start()
    
    def _run_full_pipeline_thread(self, project_id: str, task: str,
                                  pipeline_inputs: Dict[str, Any]):
        """Запуск полного pipeline в отдельном потоке"""
        def _log(msg, level="info"):
            self.after(0, lambda m=msg, l=level: self.add_log(m, l))

        def _progress(value, msg):
            self.after(0, lambda v=value, m=msg: self.update_progress(v, m))

        try:
            _log(f"Запуск полного pipeline для проекта {project_id}")
            _log(f"Задача: {task}")

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                _log(message, level)
                if progress is not None:
                    _progress(progress, message)
                if step_id and step_status:
                    self.after(0, lambda si=step_id, ss=step_status, sd=step_duration:
                               self.step_tracker.update_step(si, ss, sd))

            result = run_pipeline_sync(
                self.pipeline_runner,
                project_id,
                task,
                progress_callback,
                input_overrides=pipeline_inputs,
            )

            if result.get("status") == "cancelled":
                _log("⏹ Pipeline отменён после завершения активных шагов", "warning")
            elif result.get("status") == "success":
                _log("✅ Pipeline завершен успешно!", "success")
                _progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                _log(f"❌ Pipeline завершен с ошибкой: {error_msg}", "error")
                _progress(0, "Ошибка")
            self._append_video_artifact_summary(project_id)

        except Exception as e:
            logger.error(f"Ошибка выполнения pipeline: {e}")
            _log(f"❌ Критическая ошибка: {e}", "error")
            self._append_video_artifact_summary(project_id)
        finally:
            self.after(0, self.finish_generation)
    
    def _run_from_step_thread(self, project_id: str, step_id: str,
                              task: Optional[str], pipeline_inputs: Dict[str, Any]):
        """Запуск pipeline с определенного шага в отдельном потоке"""
        def _log(msg, level="info"):
            self.after(0, lambda m=msg, l=level: self.add_log(m, l))

        def _progress(value, msg):
            self.after(0, lambda v=value, m=msg: self.update_progress(v, m))

        try:
            _log(f"Запуск частичного pipeline для проекта {project_id} с шага {step_id}")

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                _log(message, level)
                if progress is not None:
                    _progress(progress, message)
                if step_id and step_status:
                    self.after(0, lambda si=step_id, ss=step_status, sd=step_duration:
                               self.step_tracker.update_step(si, ss, sd))

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
                _log("⏹ Частичный pipeline отменён после завершения активных шагов", "warning")
            elif result.get("status") == "success":
                skipped_steps = result.get("skipped_steps", 0)
                _log(f"✅ Частичный pipeline завершен успешно! Пропущено шагов: {skipped_steps}", "success")
                _progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                _log(f"❌ Частичный pipeline завершен с ошибкой: {error_msg}", "error")
                _progress(0, "Ошибка")
            self._append_video_artifact_summary(project_id)

        except Exception as e:
            logger.error(f"Ошибка выполнения частичного pipeline: {e}")
            _log(f"❌ Критическая ошибка: {e}", "error")
            self._append_video_artifact_summary(project_id)
        finally:
            self.after(0, self.finish_generation)

    def _run_resume_pipeline_thread(self, workflow_id: str, completed_steps: List[str]):
        """Возобновляет workflow из checkpoint в отдельном потоке."""
        def _log(msg, level="info"):
            self.after(0, lambda m=msg, l=level: self.add_log(m, l))

        def _progress(value, msg):
            self.after(0, lambda v=value, m=msg: self.update_progress(v, m))

        try:
            _log(f"Возобновление pipeline из checkpoint {workflow_id}")

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                _log(message, level)
                if progress is not None:
                    _progress(progress, message)
                if step_id and step_status:
                    self.after(0, lambda si=step_id, ss=step_status, sd=step_duration:
                               self.step_tracker.update_step(si, ss, sd))

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
                _log("⏹ Восстановленный pipeline отменён", "warning")
            elif result.get("status") == "success":
                _log("✅ Pipeline успешно восстановлен из checkpoint", "success")
                _progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                _log(f"❌ Ошибка восстановления pipeline: {error_msg}", "error")
                _progress(0, "Ошибка")
            if self.current_project:
                self._append_video_artifact_summary(self.current_project.project_id)
        except Exception as e:
            logger.error(f"Ошибка восстановления workflow {workflow_id}: {e}")
            _log(f"❌ Критическая ошибка восстановления: {e}", "error")
            if self.current_project:
                self._append_video_artifact_summary(self.current_project.project_id)
        finally:
            self.after(0, self.finish_generation)

    def _run_single_step_thread(self, project_id: str, step_id: str,
                                task: Optional[str], pipeline_inputs: Dict[str, Any]):
        """Перезапускает только один шаг pipeline в отдельном потоке."""
        def _log(msg, level="info"):
            self.after(0, lambda m=msg, l=level: self.add_log(m, l))

        def _progress(value, msg):
            self.after(0, lambda v=value, m=msg: self.update_progress(v, m))

        try:
            _log(f"Перезапуск одного шага для проекта {project_id}: {step_id}")

            def progress_callback(message: str, progress: float = None,
                                   step_id: str = None, step_status: str = None,
                                   step_duration: float = None):
                level = "error" if step_status == "failed" else "info"
                _log(message, level)
                if progress is not None:
                    _progress(progress, message)
                if step_id and step_status:
                    self.after(0, lambda si=step_id, ss=step_status, sd=step_duration:
                               self.step_tracker.update_step(si, ss, sd))

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
                _log(
                    f"✅ Шаг '{step_id}' перезапущен успешно без повтора остальных шагов",
                    "success",
                )
                _progress(100, "Завершено")
            else:
                error_msg = result.get("message", "Неизвестная ошибка")
                _log(
                    f"❌ Перезапуск шага '{step_id}' завершился с ошибкой: {error_msg}",
                    "error",
                )
                _progress(0, "Ошибка")
            self._append_video_artifact_summary(project_id)

        except Exception as e:
            logger.error(f"Ошибка single-step rerun для шага {step_id}: {e}")
            _log(f"❌ Критическая ошибка: {e}", "error")
            self._append_video_artifact_summary(project_id)
        finally:
            self.after(0, self.finish_generation)
    
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
        project_id = self.current_project.project_id

        self.start_generation(f"Частичный pipeline с {step_id}", {
            "project_id": project_id,
            "start_step": step_id,
            **pipeline_inputs,
            **({"task": task} if task else {}),
        })

        def _validate_then_run():
            try:
                self.after(0, lambda: self.add_log("Валидация проекта...", "info"))
                validation_result = self.pipeline_runner.validate_project_for_pipeline(
                    project_id, start_step=step_id
                )
                if not validation_result.get("valid", False):
                    msg = validation_result.get("message", "")

                    proceed = threading.Event()
                    user_said_yes = [False]

                    def _ask():
                        user_said_yes[0] = messagebox.askyesno(
                            "Ошибки валидации",
                            f"Проект содержит ошибки:\n{msg}\n\nВсе равно запустить?"
                        )
                        proceed.set()

                    self.after(0, _ask)
                    proceed.wait()

                    if not user_said_yes[0]:
                        self.after(0, self.finish_generation)
                        return

                self.generation_thread = threading.Thread(
                    target=self._run_from_step_thread,
                    args=(project_id, step_id, task, pipeline_inputs),
                    daemon=True,
                )
                self.generation_thread.start()
            except Exception as exc:
                logger.error(f"Ошибка предстартовой валидации: {exc}")
                self.after(0, lambda m=str(exc): self.add_log(f"❌ Ошибка валидации: {m}", "error"))
                self.after(0, self.finish_generation)

        threading.Thread(target=_validate_then_run, daemon=True).start()

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
        """Регенерация изображения — не реализовано"""
        messagebox.showinfo("Не реализовано", "Регенерация изображения не реализована")

    def regenerate_video(self):
        """Регенерация видео — не реализовано"""
        messagebox.showinfo("Не реализовано", "Регенерация видео не реализована")
    
    def validate_project(self):
        """Валидация проекта (в фоновом потоке)"""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        project_id = self.current_project.project_id
        self.add_log("Запуск валидации проекта...", "info")

        def _run():
            try:
                result = self.pipeline_runner.validate_project_for_pipeline(project_id)
                if result.get("valid", False):
                    self.after(0, lambda: self.add_log("✅ Проект прошел валидацию", "success"))
                else:
                    message = result.get("message", "Неизвестная ошибка")
                    self.after(0, lambda: self.add_log(f"❌ Ошибки валидации: {message}", "error"))
            except Exception as e:
                logger.error(f"Ошибка валидации проекта: {e}")
                self.after(0, lambda: self.add_log(f"❌ Ошибка валидации: {e}", "error"))

        threading.Thread(target=_run, daemon=True).start()

    def check_provider_readiness(self):
        """Показывает pre-run сводку доступности video/music провайдеров (в фоновом потоке)."""
        if not self.current_project:
            messagebox.showwarning("Предупреждение", "Выберите проект")
            return

        project_id = self.current_project.project_id
        self.add_log("Проверка готовности провайдеров...", "info")

        def _run():
            try:
                readiness = self._load_provider_readiness(project_id)
                lines = self._format_provider_readiness_summary(readiness)
                for message, level in lines:
                    self.after(0, lambda m=message, lv=level: self.add_log(m, lv))
            except Exception as e:
                logger.error(f"Ошибка проверки video/music providers: {e}")
                self.after(0, lambda: self.add_log(f"❌ Ошибка проверки video/music providers: {e}", "error"))

        threading.Thread(target=_run, daemon=True).start()
    
    def fix_project_errors(self):
        """Исправление ошибок проекта — не реализовано"""
        messagebox.showinfo("Не реализовано", "Автоматическое исправление ошибок не реализовано")

    def _resolve_project_path(self, project_id: str) -> Path:
        """Возвращает путь проекта для чтения артефактов video pipeline."""
        if (
            self.current_project
            and self.current_project.project_id == project_id
            and getattr(self.current_project, "project_path", None)
        ):
            project_path = Path(self.current_project.project_path)
            if project_path.exists():
                return project_path

        return Path(__file__).parent.parent.parent / "plots" / "storybooks" / project_id

    @staticmethod
    def _read_json_artifact(project_path: Path, relative_path: str) -> Optional[Dict[str, Any]]:
        artifact_path = project_path / relative_path
        if not artifact_path.exists():
            return None
        try:
            with open(artifact_path, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except Exception as e:
            logger.warning("Не удалось прочитать artifact %s: %s", artifact_path, e)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _format_capabilities(capabilities: Dict[str, Any]) -> str:
        if not isinstance(capabilities, dict) or not capabilities:
            return "неизвестно"
        parts = []
        for key in ("image", "video", "audio", "music", "render"):
            if key in capabilities:
                parts.append(f"{key}={'yes' if capabilities.get(key) else 'no'}")
        return ", ".join(parts) if parts else "неизвестно"

    @staticmethod
    def _format_workflow_actions(actions_contract: Dict[str, Any]) -> str:
        actions = actions_contract.get("actions") if isinstance(actions_contract, dict) else None
        if not isinstance(actions, list):
            return "неизвестно"
        interesting_ids = {
            "project_inventory",
            "artifact_inventory",
            "media_inventory",
            "artifact_edit",
            "media_edit",
            "project_create_backup_export_delete",
            "full_pipeline",
            "validate_project",
            "video_music_readiness",
            "run_from_step",
            "rerun_single_step",
            "pause_resume",
            "regenerate_image",
            "regenerate_video",
            "yaml_builder",
        }
        parts = []
        for action in actions:
            if not isinstance(action, dict) or action.get("id") not in interesting_ids:
                continue
            parts.append(f"{action.get('id')}={action.get('status', 'unknown')}")
        return ", ".join(parts) if parts else "неизвестно"

    def _load_provider_readiness(self, project_id: str) -> Dict[str, Any]:
        """Загружает единый readiness-контракт для UI без запуска pipeline."""
        from custom_tools.storybook.video_contract import storybook_video_music_readiness

        language = self.pipeline_language_var.get().strip() if hasattr(self, "pipeline_language_var") else "ru"
        return storybook_video_music_readiness(
            project_id=project_id,
            session_id="storybook-manager-readiness",
            language=language or "ru",
            enable=bool(self.generate_screenplay_var.get()) if hasattr(self, "generate_screenplay_var") else True,
            generate_music=bool(self.generate_music_var.get()) if hasattr(self, "generate_music_var") else True,
        )

    def _format_provider_readiness_summary(self, readiness: Dict[str, Any]) -> List[tuple[str, str]]:
        """Форматирует readiness-контракт в короткие UI-строки."""
        capabilities = readiness.get("capabilities") if isinstance(readiness, dict) else {}
        video = readiness.get("video") if isinstance(readiness, dict) else {}
        music = readiness.get("music") if isinstance(readiness, dict) else {}
        render = readiness.get("render") if isinstance(readiness, dict) else {}
        artifacts = readiness.get("artifacts") if isinstance(readiness, dict) else {}
        final_review = readiness.get("final_review") if isinstance(readiness, dict) else {}
        workflow_actions = readiness.get("workflow_actions") if isinstance(readiness, dict) else {}
        blockers = readiness.get("blocking_reasons") if isinstance(readiness, dict) else []
        warnings = readiness.get("warnings") if isinstance(readiness, dict) else []
        errors = readiness.get("errors") if isinstance(readiness, dict) else []
        ready = readiness.get("ready") if isinstance(readiness, dict) else False

        messages: List[tuple[str, str]] = []
        messages.append((
            "Provider readiness: "
            f"ready={ready}, "
            f"capabilities={self._format_capabilities(capabilities or {})}, "
            f"blockers={', '.join(blockers) if blockers else 'нет'}, "
            f"warnings={', '.join(warnings) if warnings else 'нет'}, "
            f"errors={', '.join(errors) if errors else 'нет'}",
            "error" if blockers or errors else ("warning" if warnings else "success"),
        ))
        messages.append((
            "Video provider: "
            f"provider={video.get('provider', 'unknown')}, "
            f"expected_clips={video.get('expected_clip_count', 0)}, "
            f"shots_exists={video.get('shots_exists')}",
            "success" if capabilities.get("video") else "warning",
        ))
        messages.append((
            "Music provider: "
            f"provider={music.get('provider', 'unknown')}, "
            f"enabled={music.get('enabled')}, "
            f"configured={music.get('configured')}, "
            f"model={music.get('model', 'unknown')}, "
            f"callback={music.get('callback_configured')}, "
            f"status={music.get('status', 'unknown')}, "
            f"music_exists={music.get('music_exists')}",
            self._music_readiness_level(music, errors),
        ))
        messages.append((
            "Render runtime: "
            f"ffmpeg={render.get('ffmpeg_path') or 'missing'}, "
            f"ffprobe={render.get('ffprobe_path') or 'missing'}",
            "success" if render.get("configured") else "warning",
        ))
        messages.append((
            "Workflow actions: "
            f"{self._format_workflow_actions(workflow_actions or {})}",
            "info",
        ))
        if isinstance(artifacts, dict):
            artifact_bits = []
            for key in (
                "cue_sheet",
                "subtitles",
                "music_manifest",
                "music",
                "final_video",
                "timeline",
                "manifest",
                "asset_manifest",
                "edit_decisions",
                "render_report",
                "final_review",
            ):
                item = artifacts.get(key)
                if isinstance(item, dict):
                    artifact_bits.append(f"{key}={item.get('status')}")
            if artifact_bits:
                messages.append(("Artifacts: " + ", ".join(artifact_bits), "info"))
        if isinstance(final_review, dict) and final_review.get("exists"):
            failed = final_review.get("failed_checks") or []
            messages.append((
                "Final review readiness: "
                f"passed={final_review.get('passed')}, "
                f"failed_checks={', '.join(failed) if failed else 'нет'}",
                "success" if final_review.get("passed") else "error",
            ))
        return messages

    @staticmethod
    def _music_readiness_level(music: Dict[str, Any], errors: List[str]) -> str:
        status = str(music.get("status") or "").lower()
        if status in {"error", "failed"} or "music_generation_failed" in errors:
            return "error"
        if music.get("enabled") is False or status in {"disabled", "skipped"}:
            return "info"
        if music.get("configured") or music.get("music_exists"):
            return "success"
        return "warning"

    @staticmethod
    def _music_artifact_level(music_manifest: Optional[Dict[str, Any]], music_exists: bool) -> str:
        status = str((music_manifest or {}).get("status") or "").lower()
        if status in {"error", "failed"}:
            return "error"
        if status in {"disabled", "skipped"}:
            return "info"
        if status == "success":
            return "success" if music_exists else "warning"
        return "success" if music_exists and not music_manifest else "warning"

    @staticmethod
    def _failed_review_checks(final_review: Dict[str, Any]) -> List[str]:
        checks = final_review.get("checks")
        if not isinstance(checks, dict):
            return []
        return [
            check_name
            for check_name, check_data in checks.items()
            if isinstance(check_data, dict) and not check_data.get("passed", False)
        ]

    @staticmethod
    def _provider_job_summary(provider_jobs: Dict[str, Any]) -> str:
        jobs = provider_jobs.get("jobs")
        if not isinstance(jobs, list):
            return "jobs=0"

        status_counts: Dict[str, int] = {}
        total_cost = 0.0
        cost_seen = False
        for job in jobs:
            if not isinstance(job, dict):
                continue
            status = str(job.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            cost = job.get("cost_rub", job.get("cost"))
            if isinstance(cost, (int, float)):
                total_cost += float(cost)
                cost_seen = True

        parts = [f"jobs={len(jobs)}"]
        parts.extend(f"{status}={count}" for status, count in sorted(status_counts.items()))
        if cost_seen:
            parts.append(f"cost={total_cost:.2f}")
        return ", ".join(parts)

    def _build_video_artifact_summary(self, project_id: str) -> List[tuple[str, str]]:
        """Собирает краткую сводку video pipeline артефактов для UI-логов."""
        project_path = self._resolve_project_path(project_id)
        messages: List[tuple[str, str]] = []

        if not project_path.exists():
            return [(
                f"Видео-артефакты недоступны: проект не найден ({project_path})",
                "warning",
            )]

        preflight = self._read_json_artifact(
            project_path,
            "96_video_contract/provider_menu_summary.json",
        )
        if preflight:
            messages.append((
                "Video preflight: "
                f"provider={preflight.get('provider', 'unknown')}, "
                f"capabilities={self._format_capabilities(preflight.get('capabilities') or {})}, "
                f"expected_clips={preflight.get('expected_video_count', 0)}",
                "success" if preflight.get("capabilities", {}).get("video") else "warning",
            ))

        delivery = self._read_json_artifact(
            project_path,
            "96_video_contract/delivery_promise.json",
        )
        if delivery:
            blockers = delivery.get("blocking_reasons") or []
            level = "success" if delivery.get("will_generate_video") else "warning"
            if delivery.get("status") == "error":
                level = "error"
            blocker_text = ", ".join(str(item) for item in blockers) if blockers else "нет"
            messages.append((
                "Delivery promise: "
                f"will_generate_video={delivery.get('will_generate_video')}, "
                f"expected_outputs={delivery.get('expected_video_count', 0)}, "
                f"blockers={blocker_text}",
                level,
            ))

        provider_jobs = self._read_json_artifact(project_path, "97_shots/provider_jobs.json")
        if provider_jobs:
            jobs = provider_jobs.get("jobs")
            jobs_list = jobs if isinstance(jobs, list) else []
            has_failed = any(
                isinstance(job, dict) and job.get("status") in {"failed", "download_failed"}
                for job in jobs_list
            )
            messages.append((
                f"Provider jobs: {self._provider_job_summary(provider_jobs)}",
                "error" if has_failed else "success",
            ))

        audio_manifest = self._read_json_artifact(project_path, "98_audio/audio_manifest.json")
        subtitles_path = project_path / "98_audio" / "subtitles.srt"
        if audio_manifest or subtitles_path.exists():
            tts_status = audio_manifest.get("tts_status", "unknown") if audio_manifest else "missing"
            music_status = audio_manifest.get("music_status", "unknown") if audio_manifest else "missing"
            tracks = audio_manifest.get("audio_tracks") if audio_manifest else []
            track_count = len(tracks) if isinstance(tracks, list) else 0
            messages.append((
                "Audio/subtitles: "
                f"tts_status={tts_status}, music_status={music_status}, "
                f"tracks={track_count}, subtitles={subtitles_path.exists()}",
                "success" if subtitles_path.exists() else "warning",
            ))

        music_manifest = self._read_json_artifact(project_path, "98_audio/music_manifest.json")
        music_path = project_path / "98_audio" / "music.mp3"
        if music_manifest or music_path.exists():
            messages.append((
                "Music artifact: "
                f"status={(music_manifest or {}).get('status', 'present')}, "
                f"task_id={(music_manifest or {}).get('task_id') or 'none'}, "
                f"music_exists={music_path.exists()}",
                self._music_artifact_level(music_manifest, music_path.exists()),
            ))

        final_dir = project_path / "99_final"
        final_video_path = final_dir / "final_video.mp4"
        final_review = self._read_json_artifact(project_path, "99_final/final_review.json")
        if final_review:
            failed_checks = self._failed_review_checks(final_review)
            errors = final_review.get("errors")
            error_count = len(errors) if isinstance(errors, list) else 0
            messages.append((
                "Final review: "
                f"passed={final_review.get('passed')}, "
                f"failed_checks={', '.join(failed_checks) if failed_checks else 'нет'}, "
                f"errors={error_count}",
                "success" if final_review.get("passed") else "error",
            ))

        if final_dir.exists():
            expected = [
                "final_video.mp4",
                "timeline.fcpxml",
                "subtitles.srt",
                "manifest.json",
                "asset_manifest.json",
                "edit_decisions.json",
                "render_report.json",
                "final_review.json",
            ]
            present = [name for name in expected if (final_dir / name).exists()]
            messages.append((
                f"Final artifacts: {', '.join(present) if present else 'нет'} ({final_dir})",
                "success" if final_video_path.exists() else "warning",
            ))

        if not messages:
            messages.append(("Видео-артефакты пока не найдены", "warning"))
        return messages

    def _append_video_artifact_summary(self, project_id: str):
        """Пишет сводку video artifacts в UI-лог после выполнения pipeline.

        Метод thread-safe: может вызываться из фонового потока.
        """
        for message, level in self._build_video_artifact_summary(project_id):
            self.after(0, lambda m=message, l=level: self.add_log(m, l))

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
            if self.on_generation_complete:
                try:
                    self.on_generation_complete()
                except Exception as _e:
                    logger.warning(f"on_generation_complete callback error: {_e}")

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
