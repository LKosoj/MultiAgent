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
import sys
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

    def pack(self, *args, **kwargs):
        return self

    def config(self, **kwargs):
        self.options.update(kwargs)

    def __setitem__(self, key, value):
        self.options[key] = value

    def __getitem__(self, key):
        return self.options[key]


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

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    scrolledtext_module.ScrolledText = FakeWidget
    project_manager_module.Project = object
    pipeline_runner_module.PipelineRunner = MagicMock
    pipeline_runner_module.run_pipeline_sync = MagicMock
    step_tracker_module.StepTracker = FakeWidget

    with patch.dict(
        sys.modules,
        {
            "tkinter": tk_module,
            "tkinter.ttk": ttk_module,
            "tkinter.messagebox": messagebox_module,
            "tkinter.scrolledtext": scrolledtext_module,
            "StoryBookManager.core.project_manager": project_manager_module,
            "StoryBookManager.core.pipeline_runner": pipeline_runner_module,
            "StoryBookManager.gui.step_tracker": step_tracker_module,
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
            "generate_screenplay": True,
            "force_update_prompts": False,
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
        self.assertEqual(panel.pipeline_language_combo["values"], ("ru", "en", "es"))
        self.assertTrue(hasattr(panel, "generate_screenplay_checkbutton"))
        self.assertTrue(hasattr(panel, "force_update_prompts_checkbutton"))

    def test_collect_pipeline_params_returns_validated_values(self):
        module = _import_generation_panel()
        GenerationPanel = module.GenerationPanel

        panel = GenerationPanel.__new__(GenerationPanel)
        panel.pipeline_pages_min_var = FakeVar("5")
        panel.pipeline_pages_max_var = FakeVar("10")
        panel.pipeline_words_per_page_min_var = FakeVar("120")
        panel.pipeline_words_per_page_max_var = FakeVar("180")
        panel.pipeline_language_var = FakeVar("en")
        panel.generate_screenplay_var = FakeVar(True)
        panel.force_update_prompts_var = FakeVar(False)

        params = GenerationPanel._collect_pipeline_params(panel)

        self.assertEqual(
            params,
            {
                "pages_min": 5,
                "pages_max": 10,
                "words_per_page_min": 120,
                "words_per_page_max": 180,
                "language": "en",
                "generate_screenplay": True,
                "force_update_prompts": False,
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
        panel.generate_screenplay_var = FakeVar(True)
        panel.force_update_prompts_var = FakeVar(False)

        with self.assertRaisesRegex(ValueError, "не может быть меньше"):
            GenerationPanel._collect_pipeline_params(panel)


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
            "generate_screenplay": False,
            "force_update_prompts": True,
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
        self.assertFalse(call_kwargs["generate_screenplay"])
        self.assertTrue(call_kwargs["force_update_prompts"])

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
            "generate_screenplay": True,
            "force_update_prompts": False,
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
                        "force_update_prompts": True,
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
        self.assertTrue(variables["force_update_prompts"])


if __name__ == "__main__":
    unittest.main()
