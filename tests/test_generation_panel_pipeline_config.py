"""
Тесты для P2.1: UI панели конфигурации параметров pipeline.

Проверяет:
- В generation_panel создаются виджеты для pages_min/max
- Создаются виджеты для words_per_page_min/max
- Создаётся combobox для language
- Создаются checkbox для generate_screenplay и force_update_prompts
- Параметры из UI доходят до PipelineRunner для полного и частичного запуска
"""

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class FakeVar:
    """Простая замена Tk переменных для unit-тестов без реального Tk."""

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    """Минимальный mock-виджет Tk/ttk."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.options = {}
        self.children = []
        self._parent = args[0] if args and hasattr(args[0], "children") else None
        if self._parent is not None:
            self._parent.children.append(self)

    def pack(self, *args, **kwargs):
        return self

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def __setitem__(self, key, value):
        self.options[key] = value

    def __getitem__(self, key):
        return self.options[key]

    def bind(self, *args, **kwargs):
        return None

    def winfo_children(self):
        return list(self.children)

    def winfo_exists(self):
        return True

    def destroy(self):
        if self._parent is not None and self in self._parent.children:
            self._parent.children.remove(self)

    def focus_set(self):
        return None

    def create_window(self, *args, **kwargs):
        return None

    def bbox(self, *args, **kwargs):
        return (0, 0, 0, 0)

    def yview_scroll(self, *args, **kwargs):
        return None

    def yview(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None


class FakeText(FakeWidget):
    """Текстовый виджет с простым хранением содержимого."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.content = ""

    def delete(self, *args, **kwargs):
        self.content = ""

    def insert(self, _index, text):
        self.content = text

    def get(self, *_args, **_kwargs):
        return self.content


def _import_generation_panel():
    """Импортирует generation_panel с фейковым tkinter для headless-среды."""
    sys.modules.pop("StoryBookManager.gui.generation_panel", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")
    scrolledtext_module = types.ModuleType("tkinter.scrolledtext")
    project_manager_module = types.ModuleType("StoryBookManager.core.project_manager")
    pipeline_runner_module = types.ModuleType("StoryBookManager.core.pipeline_runner")
    step_tracker_module = types.ModuleType("StoryBookManager.gui.step_tracker")

    tk_module.Text = FakeText
    tk_module.StringVar = FakeVar
    tk_module.BooleanVar = FakeVar
    tk_module.Canvas = FakeWidget
    tk_module.WORD = "word"
    tk_module.END = "end"
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module
    tk_module.scrolledtext = scrolledtext_module

    ttk_module.Frame = FakeWidget
    ttk_module.LabelFrame = FakeWidget
    ttk_module.Label = FakeWidget
    ttk_module.Button = FakeWidget
    ttk_module.Spinbox = FakeWidget
    ttk_module.Combobox = FakeWidget
    ttk_module.Checkbutton = FakeWidget
    ttk_module.Scrollbar = FakeWidget
    ttk_module.Separator = FakeWidget

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    scrolledtext_module.ScrolledText = FakeWidget
    project_manager_module.Project = object
    pipeline_runner_module.PipelineRunner = MagicMock
    pipeline_runner_module.run_pipeline_sync = MagicMock
    step_tracker_module.StepTracker = FakeWidget

    file_manager_module = types.ModuleType("StoryBookManager.core.file_manager")
    file_manager_module.FileManager = MagicMock
    scroll_utils_module = types.ModuleType("StoryBookManager.utils.scroll_utils")
    scroll_utils_module.bind_mousewheel_to_canvas_frame = lambda *args, **kwargs: None

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "tkinter.scrolledtext": scrolledtext_module,
            "StoryBookManager.core.project_manager": project_manager_module,
            "StoryBookManager.core.pipeline_runner": pipeline_runner_module,
            "StoryBookManager.core.file_manager": file_manager_module,
            "StoryBookManager.gui.step_tracker": step_tracker_module,
            "StoryBookManager.utils.scroll_utils": scroll_utils_module,
        },
    ):
        return importlib.import_module("StoryBookManager.gui.generation_panel")


class TestGenerationPanelPipelineConfigUI(unittest.TestCase):
    """Проверяет создание виджетов конфигурации pipeline."""

    def test_create_generation_controls_adds_pipeline_config_widgets(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_steps = ["brief_from_prompt", "story_writer"]
        panel.pipeline_inputs = {
            "pages_min": 8,
            "pages_max": 16,
            "words_per_page_min": 100,
            "words_per_page_max": 300,
            "language": "ru",
            "screenplay_time": 120,
            "generate_screenplay": True,
            "generate_end_shots": True,
            "force_update_prompts": False,
            "skip_prompt_enhancement": True,
            "sample_before_batch": False,
            "sample_shot_key": "",
            "generate_music": True,
            "final_allow_missing_audio": False,
            "generate_blockout": True,
            "blockout_fps": 24,
            "blockout_resolution": "1280x720",
            "blockout_allowed_durations": [],
            "blockout_scope": "all",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": True,
            "blockout_use_as_video_reference": True,
            "blockout_preview_burnin": True,
        }
        panel.supported_languages = ["ru", "en", "es"]
        panel.run_full_pipeline = MagicMock()
        panel.run_from_step = MagicMock()
        panel.refresh_pipeline_steps = MagicMock()
        panel.regenerate_image = MagicMock()
        panel.regenerate_video = MagicMock()
        panel.validate_project = MagicMock()
        panel.fix_project_errors = MagicMock()

        with patch.multiple(
            module.ttk,
            Frame=FakeWidget,
            LabelFrame=FakeWidget,
            Label=FakeWidget,
            Button=FakeWidget,
            Spinbox=FakeWidget,
            Combobox=FakeWidget,
            Checkbutton=FakeWidget,
        ), patch.multiple(
            module.tk,
            Text=FakeText,
            StringVar=FakeVar,
            BooleanVar=FakeVar,
            WORD="word",
        ):
            GenerationPanel.create_generation_controls(panel, FakeWidget())

        self.assertEqual(panel.pipeline_pages_min_var.get(), "8")
        self.assertEqual(panel.pipeline_pages_max_var.get(), "16")
        self.assertEqual(panel.pipeline_words_per_page_min_var.get(), "100")
        self.assertEqual(panel.pipeline_words_per_page_max_var.get(), "300")
        self.assertEqual(panel.screenplay_time_var.get(), "120")
        self.assertEqual(panel.pipeline_language_combo["values"], ("ru", "en", "es"))
        self.assertTrue(hasattr(panel, "generate_screenplay_checkbutton"))
        self.assertTrue(hasattr(panel, "generate_end_shots_checkbutton"))
        self.assertTrue(hasattr(panel, "force_update_prompts_checkbutton"))
        self.assertTrue(hasattr(panel, "skip_prompt_enhancement_checkbutton"))
        self.assertTrue(hasattr(panel, "sample_before_batch_checkbutton"))
        self.assertEqual(panel.sample_shot_key_combo.options["state"], "disabled")
        self.assertTrue(hasattr(panel, "generate_music_checkbutton"))
        self.assertTrue(hasattr(panel, "final_allow_missing_audio_checkbutton"))

        self.assertTrue(panel.blockout_enabled_var.get())
        self.assertEqual(panel.blockout_fps_var.get(), "24")
        self.assertEqual(panel.blockout_resolution_var.get(), "1280x720")
        self.assertEqual(panel.blockout_fps_combo.kwargs["values"], ("24", "25", "30"))
        self.assertEqual(
            panel.blockout_resolution_combo.kwargs["values"], ("960x540", "1280x720", "1920x1080")
        )
        self.assertTrue(hasattr(panel, "blockout_enabled_checkbutton"))
        self.assertTrue(hasattr(panel, "blockout_img_ref_checkbutton"))
        self.assertTrue(hasattr(panel, "blockout_vid_ref_checkbutton"))
        self.assertTrue(hasattr(panel, "blockout_burnin_checkbutton"))
        self.assertTrue(hasattr(panel, "blockout_durations_frame"))
        self.assertTrue(hasattr(panel, "blockout_p17_label"))
        # Флажок «Генерировать болванку» выставлен из pipeline_inputs -> группа активна.
        self.assertEqual(panel.blockout_fps_combo.options["state"], "readonly")

    def test_apply_project_settings_syncs_sample_shot_key_state(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_inputs = {
            "pages_min": 1,
            "pages_max": 2,
            "words_per_page_min": 100,
            "words_per_page_max": 200,
            "language": "ru",
            "screenplay_time": 120,
            "generate_screenplay": True,
            "generate_end_shots": True,
            "force_update_prompts": False,
            "skip_prompt_enhancement": False,
            "sample_before_batch": False,
            "sample_shot_key": "",
            "generate_music": True,
            "final_allow_missing_audio": False,
        }
        panel.supported_languages = ["ru", "en"]
        panel.pipeline_pages_min_var = FakeVar()
        panel.pipeline_pages_max_var = FakeVar()
        panel.pipeline_words_per_page_min_var = FakeVar()
        panel.pipeline_words_per_page_max_var = FakeVar()
        panel.pipeline_language_var = FakeVar()
        panel.screenplay_time_var = FakeVar()
        panel.generate_screenplay_var = FakeVar()
        panel.generate_end_shots_var = FakeVar()
        panel.force_update_prompts_var = FakeVar()
        panel.skip_prompt_enhancement_var = FakeVar()
        panel.sample_before_batch_var = FakeVar()
        panel.sample_shot_key_var = FakeVar()
        panel.generate_music_var = FakeVar()
        panel.final_allow_missing_audio_var = FakeVar()
        panel.blockout_enabled_var = FakeVar()
        panel.blockout_fps_var = FakeVar()
        panel.blockout_resolution_var = FakeVar()
        panel.blockout_scope_var = FakeVar()
        panel.blockout_jobs_var = FakeVar()
        panel.blockout_img_ref_var = FakeVar()
        panel.blockout_vid_ref_var = FakeVar()
        panel.blockout_burnin_var = FakeVar()
        panel.blockout_durations_vars = {}
        panel.sample_shot_key_combo = FakeWidget()

        GenerationPanel._apply_project_pipeline_settings(
            panel,
            {"sample_before_batch": True, "sample_shot_key": "1-2"},
        )

        self.assertTrue(panel.sample_before_batch_var.get())
        self.assertEqual(panel.sample_shot_key_var.get(), "1-2")
        self.assertEqual(panel.sample_shot_key_combo.options["state"], "normal")

    def test_collect_pipeline_params_returns_validated_values(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_pages_min_var = FakeVar("5")
        panel.pipeline_pages_max_var = FakeVar("10")
        panel.pipeline_words_per_page_min_var = FakeVar("120")
        panel.pipeline_words_per_page_max_var = FakeVar("180")
        panel.pipeline_language_var = FakeVar("en")
        panel.screenplay_time_var = FakeVar("240")
        panel.generate_screenplay_var = FakeVar(True)
        panel.generate_end_shots_var = FakeVar(False)
        panel.force_update_prompts_var = FakeVar(False)
        panel.skip_prompt_enhancement_var = FakeVar(True)
        panel.sample_before_batch_var = FakeVar(True)
        panel.sample_shot_key_var = FakeVar(" 1-2 ")
        panel.generate_music_var = FakeVar(True)
        panel.final_allow_missing_audio_var = FakeVar(False)
        panel.blockout_enabled_var = FakeVar(True)
        panel.blockout_fps_var = FakeVar("24")
        panel.blockout_resolution_var = FakeVar(" 1280x720 ")
        panel.blockout_scope_var = FakeVar(" all ")
        panel.blockout_jobs_var = FakeVar("2")
        panel.blockout_img_ref_var = FakeVar(True)
        panel.blockout_vid_ref_var = FakeVar(False)
        panel.blockout_burnin_var = FakeVar(True)
        panel.blockout_durations_vars = {5: FakeVar(True), 7: FakeVar(False), 10: FakeVar(True)}

        params = GenerationPanel._collect_pipeline_params(panel)

        self.assertEqual(
            params,
            {
                "pages_min": 5,
                "pages_max": 10,
                "words_per_page_min": 120,
                "words_per_page_max": 180,
                "language": "en",
                "screenplay_time": 240,
                "generate_screenplay": True,
                "generate_end_shots": False,
                "force_update_prompts": False,
                "skip_prompt_enhancement": True,
                "sample_before_batch": True,
                "sample_shot_key": "1-2",
                "generate_music": True,
                "final_allow_missing_audio": False,
                "generate_blockout": True,
                "blockout_fps": 24,
                "blockout_resolution": "1280x720",
                "blockout_allowed_durations": [5, 10],
                "blockout_scope": "all",
                "blockout_jobs": 2,
                "blockout_use_as_image_reference": True,
                "blockout_use_as_video_reference": False,
                "blockout_preview_burnin": True,
            },
        )

    def test_collect_pipeline_params_rejects_invalid_ranges(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_pages_min_var = FakeVar("12")
        panel.pipeline_pages_max_var = FakeVar("10")
        panel.pipeline_words_per_page_min_var = FakeVar("120")
        panel.pipeline_words_per_page_max_var = FakeVar("180")
        panel.pipeline_language_var = FakeVar("ru")
        panel.screenplay_time_var = FakeVar("120")
        panel.generate_screenplay_var = FakeVar(True)
        panel.generate_end_shots_var = FakeVar(True)
        panel.force_update_prompts_var = FakeVar(False)
        panel.skip_prompt_enhancement_var = FakeVar(True)
        panel.sample_before_batch_var = FakeVar(False)
        panel.sample_shot_key_var = FakeVar("")
        panel.generate_music_var = FakeVar(True)
        panel.final_allow_missing_audio_var = FakeVar(False)

        with self.assertRaisesRegex(ValueError, "не может быть меньше"):
            GenerationPanel._collect_pipeline_params(panel)

    def test_collect_pipeline_params_rejects_invalid_video_values(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_pages_min_var = FakeVar("1")
        panel.pipeline_pages_max_var = FakeVar("2")
        panel.pipeline_words_per_page_min_var = FakeVar("120")
        panel.pipeline_words_per_page_max_var = FakeVar("180")
        panel.pipeline_language_var = FakeVar("ru")
        panel.screenplay_time_var = FakeVar("0")
        panel.generate_screenplay_var = FakeVar(True)
        panel.generate_end_shots_var = FakeVar(True)
        panel.force_update_prompts_var = FakeVar(False)
        panel.skip_prompt_enhancement_var = FakeVar(True)
        panel.sample_before_batch_var = FakeVar(False)
        panel.sample_shot_key_var = FakeVar("")
        panel.generate_music_var = FakeVar(True)
        panel.final_allow_missing_audio_var = FakeVar(False)

        with self.assertRaisesRegex(ValueError, "Длительность screenplay"):
            GenerationPanel._collect_pipeline_params(panel)

        panel.screenplay_time_var = FakeVar("120")
        panel.sample_before_batch_var = FakeVar(True)
        panel.sample_shot_key_var = FakeVar("bad")
        with self.assertRaisesRegex(ValueError, "Sample shot"):
            GenerationPanel._collect_pipeline_params(panel)

    def test_video_artifact_summary_reports_final_review(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir) / "project-1"
            (project_path / "96_video_contract").mkdir(parents=True)
            (project_path / "97_shots").mkdir()
            (project_path / "98_audio").mkdir()
            (project_path / "99_final").mkdir()

            (project_path / "96_video_contract" / "provider_menu_summary.json").write_text(
                json.dumps(
                    {
                        "provider": "aitunnel",
                        "capabilities": {"image": True, "video": True, "audio": False, "render": True},
                        "expected_video_count": 2,
                    }
                ),
                encoding="utf-8",
            )
            (project_path / "97_shots" / "provider_jobs.json").write_text(
                json.dumps({"jobs": [{"status": "downloaded", "cost_rub": 1.5}]}),
                encoding="utf-8",
            )
            (project_path / "98_audio" / "audio_manifest.json").write_text(
                json.dumps({"tts_status": "unavailable", "audio_tracks": []}),
                encoding="utf-8",
            )
            (project_path / "98_audio" / "music_manifest.json").write_text(
                json.dumps({"status": "success", "task_id": "suno-task-1"}),
                encoding="utf-8",
            )
            (project_path / "98_audio" / "music.mp3").write_bytes(b"mp3")
            (project_path / "98_audio" / "subtitles.srt").write_text("1\n", encoding="utf-8")
            (project_path / "99_final" / "final_review.json").write_text(
                json.dumps({"passed": False, "checks": {"audio": {"passed": False}}, "errors": ["audio"]}),
                encoding="utf-8",
            )
            (project_path / "99_final" / "manifest.json").write_text("{}", encoding="utf-8")
            (project_path / "99_final" / "asset_manifest.json").write_text("{}", encoding="utf-8")
            (project_path / "99_final" / "edit_decisions.json").write_text("{}", encoding="utf-8")
            (project_path / "99_final" / "render_report.json").write_text("{}", encoding="utf-8")

            panel = GenerationPanel.__new__(GenerationPanel)
            panel.current_project = types.SimpleNamespace(
                project_id="project-1",
                project_path=project_path,
            )

            messages = GenerationPanel._build_video_artifact_summary(panel, "project-1")

        rendered = "\n".join(message for message, _level in messages)
        self.assertIn("Video preflight", rendered)
        self.assertIn("Provider jobs: jobs=1", rendered)
        self.assertIn("Music artifact: status=success", rendered)
        self.assertIn("Final review: passed=False", rendered)
        self.assertIn("asset_manifest.json", rendered)
        self.assertIn("edit_decisions.json", rendered)
        self.assertIn("render_report.json", rendered)

    def test_resolve_project_path_returns_none_on_unsafe_project_id(self):
        """Предупреждение 1 код-ревью Э10 (раздел 18.0 ТЗ): при project_id,
        который safe_storybook_project_dir() отвергает (escape/невалидный
        сегмент), _resolve_project_path() обязан вернуть None, а не
        непроверенный storybook_projects_root()/project_id."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = None  # нет открытого проекта -> обязателен резолвер

        result = GenerationPanel._resolve_project_path(panel, "../evil")

        self.assertIsNone(result)

    def test_build_video_artifact_summary_handles_unsafe_project_id(self):
        """_build_video_artifact_summary() не должен работать с непроверенным
        путём на unsafe project_id — сообщение обязано явно называть причину
        (резолвер отказал), а не молча идти в "проект не найден" по пути,
        который вообще не должен был вычисляться."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = None

        messages = GenerationPanel._build_video_artifact_summary(panel, "../evil")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][1], "warning")
        self.assertIn("не удалось безопасно определить путь", messages[0][0])

    def test_provider_readiness_summary_reports_music_and_final_artifacts(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        readiness = {
            "ready": True,
            "capabilities": {"image": True, "video": True, "audio": True, "music": False, "render": True},
            "video": {"provider": "aitunnel", "expected_clip_count": 2, "shots_exists": True},
            "music": {
                "enabled": True,
                "provider": "suno",
                "configured": False,
                "model": "chirp-fenix",
                "captcha_token_configured": False,
                "status": "not_generated",
                "music_exists": False,
            },
            "render": {"ffmpeg_path": "/usr/bin/ffmpeg", "ffprobe_path": "/usr/bin/ffprobe", "configured": True},
            "artifacts": {
                "cue_sheet": {"status": "present"},
                "music_manifest": {"status": "missing"},
                "final_video": {"status": "present"},
                "asset_manifest": {"status": "present"},
                "edit_decisions": {"status": "present"},
                "render_report": {"status": "present"},
                "final_review": {"status": "failed"},
            },
            "final_review": {"exists": True, "passed": False, "failed_checks": ["audio"]},
            "workflow_actions": {
                "actions": [
                    {"id": "full_pipeline", "status": "available"},
                    {"id": "run_from_step", "status": "manager_only"},
                    {"id": "regenerate_image", "status": "not_implemented"},
                ]
            },
            "blocking_reasons": [],
            "warnings": ["music_provider_unavailable"],
            "errors": ["final_review_failed"],
        }

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)

        rendered = "\n".join(message for message, _level in messages)
        self.assertIn("ready=True", rendered)
        self.assertIn("music_provider_unavailable", rendered)
        self.assertIn("final_review_failed", rendered)
        self.assertIn("cue_sheet=present", rendered)
        self.assertIn("asset_manifest=present", rendered)
        self.assertIn("render_report=present", rendered)
        self.assertIn("Final review readiness: passed=False", rendered)
        self.assertIn("Workflow actions: full_pipeline=available", rendered)
        self.assertIn("run_from_step=manager_only", rendered)
        self.assertIn("regenerate_image=not_implemented", rendered)

    def test_music_readiness_and_artifact_levels_prioritize_failed_status(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        self.assertEqual(
            GenerationPanel._music_readiness_level(
                {"enabled": True, "configured": True, "music_exists": True, "status": "failed"},
                [],
            ),
            "error",
        )
        self.assertEqual(
            GenerationPanel._music_readiness_level(
                {"enabled": True, "configured": True, "music_exists": True, "status": "success"},
                ["music_generation_failed"],
            ),
            "error",
        )
        self.assertEqual(
            GenerationPanel._music_artifact_level({"status": "failed"}, True),
            "error",
        )
        self.assertEqual(
            GenerationPanel._music_artifact_level({"status": "skipped"}, True),
            "info",
        )

    def test_pluralize_ru_selects_correct_form(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        self.assertEqual(GenerationPanel._pluralize_ru(1, "объект", "объекта", "объектов"), "объект")
        self.assertEqual(GenerationPanel._pluralize_ru(2, "объект", "объекта", "объектов"), "объекта")
        self.assertEqual(GenerationPanel._pluralize_ru(5, "объект", "объекта", "объектов"), "объектов")
        self.assertEqual(GenerationPanel._pluralize_ru(11, "объект", "объекта", "объектов"), "объектов")
        self.assertEqual(GenerationPanel._pluralize_ru(21, "объект", "объекта", "объектов"), "объект")

    def _readiness_with_blockout(self, blockout):
        return {
            "ready": True,
            "capabilities": {"image": True, "video": True, "audio": True, "music": False, "render": True},
            "video": {
                "provider": "aitunnel",
                "expected_clip_count": 1,
                "shots_exists": True,
                "blockout": blockout,
            },
            "music": {
                "enabled": False, "provider": "suno", "configured": False, "model": "m",
                "captcha_token_configured": False, "status": "disabled", "music_exists": False,
            },
            "render": {"ffmpeg_path": "/usr/bin/ffmpeg", "ffprobe_path": "/usr/bin/ffprobe", "configured": True},
            "artifacts": {},
            "final_review": {},
            "workflow_actions": {"actions": []},
            "blocking_reasons": [],
            "warnings": [],
            "errors": [],
        }

    def test_provider_readiness_summary_reports_blender_available(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": True, "use_reference": True,
            "blender": {"available": True, "version": "4.2", "path": "/usr/bin/blender", "message": "ok"},
            "asset_library": {"object_count": 0, "fetch_enabled": True},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)

        blender_messages = [(m, lvl) for m, lvl in messages if m.startswith("Blender:")]
        self.assertEqual(len(blender_messages), 1)
        self.assertIn("✅", blender_messages[0][0])
        self.assertIn("4.2", blender_messages[0][0])
        self.assertEqual(blender_messages[0][1], "success")

    def test_provider_readiness_summary_blender_missing_and_blockout_enabled_is_error(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": True, "use_reference": True,
            "blender": {"available": False, "version": None, "path": None, "message": "not found"},
            "asset_library": {"object_count": 0, "fetch_enabled": True},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)

        blender_messages = [(m, lvl) for m, lvl in messages if m.startswith("Blender:")]
        self.assertEqual(blender_messages[0][1], "error")

    def test_provider_readiness_summary_blender_missing_and_blockout_disabled_is_warning(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": False, "use_reference": False,
            "blender": {"available": False, "version": None, "path": None, "message": "not found"},
            "asset_library": {"object_count": 0, "fetch_enabled": True},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)

        blender_messages = [(m, lvl) for m, lvl in messages if m.startswith("Blender:")]
        self.assertEqual(blender_messages[0][1], "warning")

    def test_provider_readiness_summary_reports_asset_library_and_layer_states(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": True, "use_reference": True,
            "blender": {"available": True, "version": "4.2", "path": "/usr/bin/blender", "message": "ok"},
            "asset_library": {"object_count": 5, "fetch_enabled": True},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)
        rendered = "\n".join(m for m, _lvl in messages)

        self.assertIn("Библиотека объектов", rendered)
        self.assertIn("5 объектов", rendered)
        self.assertIn("Слой болванок: ✅ включён, видео-референс будет подан", rendered)

    def test_provider_readiness_summary_layer_warns_when_enabled_without_reference(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": True, "use_reference": False,
            "blender": {"available": True, "version": "4.2", "path": "/usr/bin/blender", "message": "ok"},
            "asset_library": {"object_count": 0, "fetch_enabled": False},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)
        rendered = "\n".join(m for m, _lvl in messages)

        self.assertIn("Слой болванок: ⚠️ включён, видео-референс не подан", rendered)

    def test_provider_readiness_summary_layer_info_when_disabled(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)

        readiness = self._readiness_with_blockout({
            "enabled": False, "use_reference": False,
            "blender": {"available": True, "version": "4.2", "path": "/usr/bin/blender", "message": "ok"},
            "asset_library": {"object_count": 0, "fetch_enabled": False},
        })

        messages = GenerationPanel._format_provider_readiness_summary(panel, readiness)
        rendered = "\n".join(m for m, _lvl in messages)

        self.assertIn("Слой болванок: ℹ️ выключен", rendered)

    def test_collect_blockout_allowed_durations_all_checked_means_no_restriction(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_vars = {5: FakeVar(True), 7: FakeVar(True), 10: FakeVar(True)}

        self.assertEqual(GenerationPanel._collect_blockout_allowed_durations(panel), [])

    def test_collect_blockout_allowed_durations_none_checked_means_no_restriction(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_vars = {5: FakeVar(False), 7: FakeVar(False), 10: FakeVar(False)}

        self.assertEqual(GenerationPanel._collect_blockout_allowed_durations(panel), [])

    def test_collect_blockout_allowed_durations_empty_when_no_durations_known(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_vars = {}

        self.assertEqual(GenerationPanel._collect_blockout_allowed_durations(panel), [])

    def test_sync_blockout_group_state_disables_descendants_when_unchecked(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_enabled_var = FakeVar(False)
        panel.blockout_settings_frame = FakeWidget()
        child = FakeWidget(panel.blockout_settings_frame)
        grandchild = FakeWidget(child)

        GenerationPanel._sync_blockout_group_state(panel)

        self.assertEqual(child.options["state"], "disabled")
        self.assertEqual(grandchild.options["state"], "disabled")

    def test_sync_blockout_group_state_enables_descendants_when_checked(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_enabled_var = FakeVar(True)
        panel.blockout_settings_frame = FakeWidget()
        combo = module.ttk.Combobox(panel.blockout_settings_frame)

        GenerationPanel._sync_blockout_group_state(panel)

        self.assertEqual(combo.options["state"], "readonly")

    def test_sync_blockout_group_state_noop_before_widgets_built(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_enabled_var = FakeVar(True)

        # Не должно бросать исключение, даже если группа виджетов ещё не создана.
        GenerationPanel._sync_blockout_group_state(panel)

    def test_rebuild_blockout_duration_checkboxes_shows_placeholder_without_caps_file(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_frame = FakeWidget()
        panel.blockout_p17_label = FakeWidget()

        with tempfile.TemporaryDirectory() as tmp_dir:
            project = types.SimpleNamespace(project_path=Path(tmp_dir), brief_data={})
            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)

        self.assertEqual(panel.blockout_durations_vars, {})
        self.assertEqual(len(panel.blockout_durations_frame.children), 1)
        self.assertEqual(panel.blockout_p17_label.options.get("text"), "")

    def test_rebuild_blockout_duration_checkboxes_creates_checkbox_per_duration_and_p17_line(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_frame = FakeWidget()
        panel.blockout_p17_label = FakeWidget()

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir)
            (project_path / "97_shots").mkdir()
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({
                    "supported_durations": [5, 7, 10],
                    "warnings": [{"code": "P17", "level": "info", "message": "сумма 42с при плане 45с"}],
                }),
                encoding="utf-8",
            )
            project = types.SimpleNamespace(project_path=project_path, brief_data={})
            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)

        self.assertEqual(sorted(panel.blockout_durations_vars.keys()), [5, 7, 10])
        self.assertTrue(all(var.get() for var in panel.blockout_durations_vars.values()))
        self.assertEqual(panel.blockout_p17_label.options["text"], "сумма 42с при плане 45с")

    def test_rebuild_blockout_duration_checkboxes_restricts_checked_state_from_brief(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_frame = FakeWidget()
        panel.blockout_p17_label = FakeWidget()

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir)
            (project_path / "97_shots").mkdir()
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7, 10]}), encoding="utf-8"
            )
            project = types.SimpleNamespace(
                project_path=project_path, brief_data={"blockout_allowed_durations": [5, 10]}
            )
            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)

        self.assertTrue(panel.blockout_durations_vars[5].get())
        self.assertFalse(panel.blockout_durations_vars[7].get())
        self.assertTrue(panel.blockout_durations_vars[10].get())

    def test_rebuild_blockout_duration_checkboxes_clears_previous_widgets_on_second_call(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_frame = FakeWidget()
        panel.blockout_p17_label = FakeWidget()

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir)
            (project_path / "97_shots").mkdir()
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7]}), encoding="utf-8"
            )
            project = types.SimpleNamespace(project_path=project_path, brief_data={})

            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)
            first_children = list(panel.blockout_durations_frame.children)
            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)

        self.assertEqual(len(panel.blockout_durations_frame.children), len(first_children))
        for widget in first_children:
            self.assertNotIn(widget, panel.blockout_durations_frame.children)

    def test_rebuild_blockout_duration_checkboxes_ignores_malformed_caps_file(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.blockout_durations_frame = FakeWidget()
        panel.blockout_p17_label = FakeWidget()

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir)
            (project_path / "97_shots").mkdir()
            (project_path / "97_shots" / "video_model_caps.json").write_text("not json", encoding="utf-8")
            project = types.SimpleNamespace(project_path=project_path, brief_data={})
            GenerationPanel._rebuild_blockout_duration_checkboxes(panel, project)

        self.assertEqual(panel.blockout_durations_vars, {})

    def test_persist_blockout_settings_merges_into_brief_and_updates_current_project(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(
            project_id="proj1", brief_data={"storybook_prompt": "old"}
        )
        blockout_settings = {
            "generate_blockout": True,
            "blockout_fps": 24,
            "blockout_resolution": "1280x720",
            "blockout_allowed_durations": [],
            "blockout_scope": "all",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": True,
            "blockout_use_as_video_reference": True,
            "blockout_preview_burnin": True,
        }

        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {"storybook_prompt": "old", "other_key": "keep"}
        fake_fm.save_json_file.return_value = True

        with patch.object(module, "FileManager", return_value=fake_fm):
            result = GenerationPanel._persist_blockout_settings(panel, "proj1", blockout_settings)

        self.assertTrue(result)
        saved_brief = fake_fm.save_json_file.call_args.args[0]
        self.assertEqual(saved_brief["other_key"], "keep")
        self.assertTrue(saved_brief["generate_blockout"])
        self.assertEqual(saved_brief["blockout_fps"], 24)
        self.assertEqual(panel.current_project.brief_data["blockout_fps"], 24)

    def test_persist_blockout_settings_logs_warning_and_returns_false_on_save_failure(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = None
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        blockout_settings = {
            "generate_blockout": False,
            "blockout_fps": 24,
            "blockout_resolution": "",
            "blockout_allowed_durations": [],
            "blockout_scope": "",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": False,
            "blockout_use_as_video_reference": False,
            "blockout_preview_burnin": False,
        }

        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {}
        fake_fm.save_json_file.return_value = False

        with patch.object(module, "FileManager", return_value=fake_fm):
            result = GenerationPanel._persist_blockout_settings(panel, "proj1", blockout_settings)

        self.assertFalse(result)
        panel.add_log.assert_called_once()
        self.assertEqual(panel.add_log.call_args.args[1], "warning")

    def test_persist_blockout_settings_overrides_take_precedence(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = None
        blockout_settings = {
            "generate_blockout": False,
            "blockout_fps": 24,
            "blockout_resolution": "",
            "blockout_allowed_durations": [],
            "blockout_scope": "",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": False,
            "blockout_use_as_video_reference": False,
            "blockout_preview_burnin": False,
        }

        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {}
        fake_fm.save_json_file.return_value = True

        with patch.object(module, "FileManager", return_value=fake_fm):
            result = GenerationPanel._persist_blockout_settings(
                panel, "proj1", blockout_settings, overrides={"generate_blockout": True}
            )

        self.assertTrue(result)
        saved_brief = fake_fm.save_json_file.call_args.args[0]
        self.assertTrue(saved_brief["generate_blockout"])

    def test_persist_blockout_settings_ignores_extra_keys_outside_blockout_schema(self):
        """Раздел 18.2: слияние затрагивает только девять ключей болванки —
        если снимок (например, весь pipeline_inputs) содержит посторонние
        поля (language, pages_min и т.п.), они не должны попасть в брифе."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = None
        blockout_settings = {
            "generate_blockout": True,
            "blockout_fps": 24,
            "blockout_resolution": "1280x720",
            "blockout_allowed_durations": [],
            "blockout_scope": "all",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": False,
            "blockout_use_as_video_reference": False,
            "blockout_preview_burnin": False,
            "language": "ru",
            "pages_min": 5,
        }

        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {}
        fake_fm.save_json_file.return_value = True

        with patch.object(module, "FileManager", return_value=fake_fm):
            GenerationPanel._persist_blockout_settings(panel, "proj1", blockout_settings)

        saved_brief = fake_fm.save_json_file.call_args.args[0]
        self.assertNotIn("language", saved_brief)
        self.assertNotIn("pages_min", saved_brief)

    def test_create_ui_wires_left_column_through_canvas_and_scrollbar(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.toggle_pause = MagicMock()
        panel.stop_generation = MagicMock()
        panel.create_generation_controls = MagicMock()
        panel.create_execution_panel = MagicMock()

        with patch.object(module, "bind_mousewheel_to_canvas_frame") as mock_bind:
            GenerationPanel.create_ui(panel)

        self.assertTrue(panel.create_generation_controls.called)
        self.assertTrue(panel.create_execution_panel.called)
        self.assertTrue(mock_bind.called)
        canvas_arg = mock_bind.call_args.args[0]
        self.assertIsInstance(canvas_arg, module.tk.Canvas)

    def test_run_full_pipeline_persists_blockout_settings_after_validation(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(project_id="proj1")
        panel._pipeline_load_error = None
        panel._generation_lock = module.threading.Lock()
        panel.is_generating = False
        panel.task_text = FakeText()
        panel.task_text.insert("1.0", "a story")
        panel._collect_pipeline_params = MagicMock(return_value={"generate_blockout": True})
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        panel._persist_blockout_settings = MagicMock()
        panel.start_generation = MagicMock()
        panel.pipeline_runner = MagicMock()
        panel.pipeline_runner.validate_project_for_pipeline = MagicMock(
            return_value={"valid": True}
        )
        panel.generation_thread = None
        panel._run_full_pipeline_thread = MagicMock()

        class SyncThread:
            """Заменяет threading.Thread, выполняя target синхронно — тест не
            должен зависеть от планировщика ОС, чтобы проверить порядок вызовов."""

            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                if self._target:
                    self._target(*self._args, **self._kwargs)

        with patch.object(module.threading, "Thread", SyncThread):
            GenerationPanel.run_full_pipeline(panel)

        panel._persist_blockout_settings.assert_called_once_with(
            "proj1", {"generate_blockout": True}
        )

    def test_run_full_pipeline_persists_snapshot_from_before_validation_not_live_widget(self):
        """Дефект код-ревью (раздел 18.2): _persist_blockout_settings()
        вызывается ПОСЛЕ успешной валидации, а валидация идёт в фоновом
        потоке — если метод в этот момент заново читает живые Tk-переменные,
        он ловит значение, которое пользователь успел поменять уже ПОСЛЕ
        нажатия «Запустить» (виджеты болванки валидацией не блокируются).
        В брифе обязан оказаться снимок на момент нажатия кнопки
        (pipeline_inputs, собранный в главном потоке до старта фонового
        потока), а не текущее состояние виджета в момент записи."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(project_id="proj1", brief_data={})
        panel._pipeline_load_error = None
        panel._generation_lock = module.threading.Lock()
        panel.is_generating = False
        panel.task_text = FakeText()
        panel.task_text.insert("1.0", "a story")
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        panel.start_generation = MagicMock()
        panel.generation_thread = None
        panel._run_full_pipeline_thread = MagicMock()

        # Кнопка «Запустить» нажата, когда флажок болванки СНЯТ — снимок
        # (то, что реально вернул бы _collect_pipeline_params() в главном
        # потоке в этот момент) фиксирует generate_blockout: False.
        panel.blockout_enabled_var = FakeVar(False)
        panel.blockout_fps_var = FakeVar("24")
        panel.blockout_resolution_var = FakeVar("1280x720")
        panel.blockout_scope_var = FakeVar("all")
        panel.blockout_jobs_var = FakeVar("0")
        panel.blockout_img_ref_var = FakeVar(False)
        panel.blockout_vid_ref_var = FakeVar(False)
        panel.blockout_burnin_var = FakeVar(False)
        panel.blockout_durations_vars = {}
        snapshot_at_click = {
            "generate_blockout": False,
            "blockout_fps": 24,
            "blockout_resolution": "1280x720",
            "blockout_allowed_durations": [],
            "blockout_scope": "all",
            "blockout_jobs": 0,
            "blockout_use_as_image_reference": False,
            "blockout_use_as_video_reference": False,
            "blockout_preview_burnin": False,
        }
        panel._collect_pipeline_params = MagicMock(return_value=dict(snapshot_at_click))

        def _validate_and_toggle_checkbox_mid_flight(project_id):
            # Пока фоновый поток валидации работает, пользователь успевает
            # поставить галку «Генерировать болванку» — виджеты валидацией
            # не блокируются.
            panel.blockout_enabled_var.set(True)
            return {"valid": True}

        panel.pipeline_runner = MagicMock()
        panel.pipeline_runner.validate_project_for_pipeline = MagicMock(
            side_effect=_validate_and_toggle_checkbox_mid_flight
        )

        fake_fm = MagicMock()
        fake_fm.load_json_file.return_value = {}
        fake_fm.save_json_file.return_value = True

        class SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                if self._target:
                    self._target(*self._args, **self._kwargs)

        with patch.object(module, "FileManager", return_value=fake_fm), \
                patch.object(module.threading, "Thread", SyncThread):
            GenerationPanel.run_full_pipeline(panel)

        saved_brief = fake_fm.save_json_file.call_args.args[0]
        self.assertFalse(
            saved_brief["generate_blockout"],
            "в 00_brief.json должен уйти снимок на момент нажатия кнопки "
            "(generate_blockout: False), а не значение, изменённое в виджете "
            "уже после старта валидации",
        )

    def test_validate_project_logs_warnings_as_warning_level_when_valid(self):
        """Раздел 18.6 ТЗ: непустой список warnings печатается уровнем
        "warning" в журнал, в том числе когда valid: true (сегодня
        предупреждения из validate_project_for_pipeline() не видны никому,
        если нет хотя бы одной ошибки)."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(project_id="proj1")
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        panel.pipeline_runner = MagicMock()
        panel.pipeline_runner.validate_project_for_pipeline = MagicMock(
            return_value={"valid": True, "warnings": ["в brief не указан title"]}
        )

        class SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        with patch.object(module.threading, "Thread", SyncThread):
            GenerationPanel.validate_project(panel)

        logged = [call.args for call in panel.add_log.call_args_list]
        self.assertIn(("⚠️ в brief не указан title", "warning"), logged)
        self.assertTrue(any(level == "success" for _, level in logged))
        self.assertFalse(any(level == "error" for _, level in logged))

    def test_run_full_pipeline_logs_warnings_without_dialog_when_valid(self):
        """Раздел 18.6 ТЗ: точки запуска выводят warnings в журнал перед
        стартом, не открывая диалога — диалог остаётся только для errors."""
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(project_id="proj1")
        panel._pipeline_load_error = None
        panel._generation_lock = module.threading.Lock()
        panel.is_generating = False
        panel.task_text = FakeText()
        panel.task_text.insert("1.0", "a story")
        panel._collect_pipeline_params = MagicMock(return_value={"generate_blockout": True})
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        panel._persist_blockout_settings = MagicMock()
        panel.start_generation = MagicMock()
        panel.pipeline_runner = MagicMock()
        panel.pipeline_runner.validate_project_for_pipeline = MagicMock(
            return_value={"valid": True, "warnings": ["Blender не найден в PATH"]}
        )
        panel.generation_thread = None
        panel._run_full_pipeline_thread = MagicMock()

        with patch.object(module.threading, "Thread", _SyncThread), \
                patch.object(module.messagebox, "askyesno") as mock_ask:
            GenerationPanel.run_full_pipeline(panel)

        mock_ask.assert_not_called()
        logged = [call.args for call in panel.add_log.call_args_list]
        self.assertIn(("⚠️ Blender не найден в PATH", "warning"), logged)
        panel.start_generation.assert_called_once()


class _SyncThread:
    """Заменяет threading.Thread, выполняя target синхронно (без реального
    планировщика ОС) — образец: TestGenerationPanelPipelineConfigUI выше."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class TestRunBlockoutScopedStep(unittest.TestCase):
    """Раздел 18.4: запуск одного шага болванки со вкладки «Болванка» —
    GenerationPanel.run_blockout_scoped_step()."""

    def _make_panel(self, module):
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel.current_project = types.SimpleNamespace(project_id="proj1")
        panel._generation_lock = module.threading.Lock()
        panel.is_generating = False
        panel.task_text = FakeText()
        panel._persist_blockout_settings = MagicMock()
        panel.start_generation = MagicMock()
        panel.generation_thread = None
        panel._run_single_step_thread = MagicMock()
        return panel

    def test_forces_generate_blockout_true_and_sets_scope(self):
        module = _import_generation_panel()
        panel = self._make_panel(module)
        panel._collect_pipeline_params = MagicMock(
            return_value={"generate_blockout": False, "blockout_scope": "all", "other": 1}
        )

        with patch.object(module.threading, "Thread", _SyncThread):
            result = module.GenerationPanel.run_blockout_scoped_step(
                panel, "blockout_renderer", "scene_01_shot_02"
            )

        self.assertTrue(result)
        panel._run_single_step_thread.assert_called_once()
        call_args = panel._run_single_step_thread.call_args.args
        self.assertEqual(call_args[0], "proj1")
        self.assertEqual(call_args[1], "blockout_renderer")
        pipeline_inputs = call_args[3]
        self.assertTrue(pipeline_inputs["generate_blockout"])
        self.assertEqual(pipeline_inputs["blockout_scope"], "scene_01_shot_02")
        self.assertEqual(pipeline_inputs["other"], 1)
        panel._persist_blockout_settings.assert_called_once_with(
            "proj1", pipeline_inputs, overrides={"generate_blockout": True}
        )

    def test_extra_overrides_take_precedence_over_scope(self):
        module = _import_generation_panel()
        panel = self._make_panel(module)
        panel._collect_pipeline_params = MagicMock(return_value={})

        with patch.object(module.threading, "Thread", _SyncThread):
            module.GenerationPanel.run_blockout_scoped_step(
                panel, "artist_batch_shots", "all",
                extra_overrides={"blockout_scope": "chain_sc01_ch01"},
            )

        pipeline_inputs = panel._run_single_step_thread.call_args.args[3]
        # extra_overrides накладываются ПОСЛЕ scope/generate_blockout -> побеждают.
        self.assertEqual(pipeline_inputs["blockout_scope"], "chain_sc01_ch01")

    def test_returns_false_and_warns_when_already_generating(self):
        module = _import_generation_panel()
        panel = self._make_panel(module)
        panel.is_generating = True
        panel._collect_pipeline_params = MagicMock()

        with patch.object(module.messagebox, "showwarning") as mock_warn:
            result = module.GenerationPanel.run_blockout_scoped_step(panel, "blockout_renderer", "all")

        self.assertFalse(result)
        mock_warn.assert_called_once()
        panel._collect_pipeline_params.assert_not_called()
        panel._run_single_step_thread.assert_not_called()

    def test_returns_false_when_no_project_selected(self):
        module = _import_generation_panel()
        panel = self._make_panel(module)
        panel.current_project = None

        with patch.object(module.messagebox, "showwarning") as mock_warn:
            result = module.GenerationPanel.run_blockout_scoped_step(panel, "blockout_renderer", "all")

        self.assertFalse(result)
        mock_warn.assert_called_once()

    def test_returns_false_and_resets_is_generating_on_param_error(self):
        module = _import_generation_panel()
        panel = self._make_panel(module)
        panel._collect_pipeline_params = MagicMock(side_effect=ValueError("bad range"))

        with patch.object(module.messagebox, "showerror") as mock_err:
            result = module.GenerationPanel.run_blockout_scoped_step(panel, "blockout_renderer", "all")

        self.assertFalse(result)
        mock_err.assert_called_once()
        self.assertFalse(panel.is_generating)


class TestLastRunningStepIdTracking(unittest.TestCase):
    """Раздел 18.4: панель генерации запоминает id последнего запущенного шага
    (status == "running") в self._last_running_step_id — по нему вкладка
    «Болванка» блокирует ручную правку длительности, пока идёт
    screenplay_shots_generator."""

    def test_progress_callback_records_running_step_id(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel
        panel = GenerationPanel.__new__(GenerationPanel)
        panel._last_running_step_id = None
        panel.after = lambda delay, callback: callback()
        panel.add_log = MagicMock()
        panel.update_progress = MagicMock()
        panel.step_tracker = MagicMock()
        panel._append_video_artifact_summary = MagicMock()
        panel.finish_generation = MagicMock()
        panel.pipeline_runner = MagicMock()

        async def fake_rerun_single_step(project_id, step_id, progress_callback,
                                          task=None, input_overrides=None):
            progress_callback(
                "running step", step_status="running", step_id="screenplay_shots_generator"
            )
            return {"status": "success"}

        panel.pipeline_runner.rerun_single_step = fake_rerun_single_step

        GenerationPanel._run_single_step_thread(panel, "proj1", "blockout_renderer", None, {})

        self.assertEqual(panel._last_running_step_id, "screenplay_shots_generator")


class TestPipelineRunnerPipelineConfig(unittest.TestCase):
    """Проверяет передачу настроек панели в runtime pipeline."""

    def _make_runner(self):
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner

            runner = PipelineRunner()
            runner.engine = MagicMock()
            runner.engine.execute_workflow_from_yaml = AsyncMock(return_value=MagicMock())
            runner.engine.execute_workflow = AsyncMock(return_value=MagicMock())
            return runner

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_full_pipeline_passes_input_overrides_to_engine(self):
        runner = self._make_runner()
        overrides = {
            "pages_min": 9,
            "pages_max": 12,
            "words_per_page_min": 130,
            "words_per_page_max": 220,
            "language": "en",
            "screenplay_time": 180,
            "generate_screenplay": False,
            "generate_end_shots": False,
            "force_update_prompts": True,
            "skip_prompt_enhancement": False,
            "sample_before_batch": True,
            "sample_shot_key": "1-2",
            "generate_music": True,
            "final_allow_missing_audio": True,
        }
        mock_ctx_cls = MagicMock(return_value=MagicMock())

        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowContext=mock_ctx_cls, WorkflowDefinition=MagicMock())},
        ):
            result = asyncio.run(
                runner.run_full_pipeline("proj1", "custom task", input_overrides=overrides)
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(mock_ctx_cls.call_args.kwargs["variables"]["language"], "en")
        self.assertTrue(mock_ctx_cls.call_args.kwargs["variables"]["force_update_prompts"])

        call_kwargs = runner.engine.execute_workflow_from_yaml.call_args.kwargs
        self.assertEqual(call_kwargs["pages_min"], 9)
        self.assertEqual(call_kwargs["pages_max"], 12)
        self.assertEqual(call_kwargs["words_per_page_min"], 130)
        self.assertEqual(call_kwargs["words_per_page_max"], 220)
        self.assertEqual(call_kwargs["language"], "en")
        self.assertEqual(call_kwargs["screenplay_time"], 180)
        self.assertFalse(call_kwargs["generate_screenplay"])
        self.assertFalse(call_kwargs["generate_end_shots"])
        self.assertTrue(call_kwargs["force_update_prompts"])
        self.assertFalse(call_kwargs["skip_prompt_enhancement"])
        self.assertTrue(call_kwargs["sample_before_batch"])
        self.assertEqual(call_kwargs["sample_shot_key"], "1-2")
        self.assertTrue(call_kwargs["generate_music"])
        self.assertTrue(call_kwargs["final_allow_missing_audio"])

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_from_step_merges_yaml_inputs_with_ui_overrides(self):
        runner = self._make_runner()

        mock_workflow_def = MagicMock()
        step_a = MagicMock()
        step_a.id = "brief_from_prompt"
        step_a.depends_on = []
        step_a.condition = None
        step_b = MagicMock()
        step_b.id = "story_writer"
        step_b.depends_on = []
        step_b.condition = None
        mock_workflow_def.steps = [step_a, step_b]
        mock_workflow_def.inputs = {
            "task": "yaml task",
            "pages_min": 1,
            "pages_max": 2,
            "words_per_page_min": 100,
            "words_per_page_max": 200,
            "language": "ru",
            "screenplay_time": 120,
            "generate_screenplay": True,
            "generate_end_shots": True,
            "force_update_prompts": False,
            "skip_prompt_enhancement": True,
            "sample_before_batch": False,
            "sample_shot_key": "",
            "generate_music": True,
            "final_allow_missing_audio": False,
        }

        mock_ctx = MagicMock()
        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_workflow_def))
        mock_ctx_cls = MagicMock(return_value=mock_ctx)

        with patch.dict(
            "sys.modules",
            {
                "workflow.models": MagicMock(
                    WorkflowDefinition=mock_wf_def_cls,
                    WorkflowContext=mock_ctx_cls,
                ),
            },
        ):
            result = asyncio.run(
                runner.run_from_step(
                    "proj42",
                    "story_writer",
                    task="ui task",
                    input_overrides={
                        "pages_min": 7,
                        "words_per_page_max": 260,
                        "language": "de",
                        "screenplay_time": 240,
                        "force_update_prompts": True,
                        "skip_prompt_enhancement": False,
                        "sample_before_batch": True,
                        "sample_shot_key": "1-2",
                        "generate_music": True,
                        "final_allow_missing_audio": True,
                    },
                )
            )

        self.assertEqual(result["status"], "success")
        variables = mock_ctx_cls.call_args.kwargs["variables"]
        self.assertEqual(variables["project_id"], "proj42")
        self.assertEqual(variables["task"], "ui task")
        self.assertEqual(variables["pages_min"], 7)
        self.assertEqual(variables["pages_max"], 2)
        self.assertEqual(variables["words_per_page_min"], 100)
        self.assertEqual(variables["words_per_page_max"], 260)
        self.assertEqual(variables["language"], "de")
        self.assertEqual(variables["screenplay_time"], 240)
        self.assertTrue(variables["force_update_prompts"])
        self.assertFalse(variables["skip_prompt_enhancement"])
        self.assertTrue(variables["sample_before_batch"])
        self.assertEqual(variables["sample_shot_key"], "1-2")
        self.assertTrue(variables["generate_music"])
        self.assertTrue(variables["final_allow_missing_audio"])


if __name__ == "__main__":
    unittest.main()
