from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.workflow_test_utils import load_light_workflow_models

load_light_workflow_models()


class TestProjectValidationDependencies(unittest.TestCase):
    @patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine")
    def test_validate_project_checks_required_artifacts_for_partial_run(self, _mock_init):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()

        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_validation"
            (project_path / "10_synopsis").mkdir(parents=True, exist_ok=True)
            (project_path / "20_bible").mkdir(parents=True, exist_ok=True)
            (project_path / "30_style").mkdir(parents=True, exist_ok=True)

            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "Story", "storybook_prompt": "Prompt"}),
                encoding="utf-8",
            )
            (project_path / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
            (project_path / "10_synopsis" / "beats.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "characters.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "locations.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "consistency_rules.json").write_text(
                "[]",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}):
                result = runner.validate_project_for_pipeline(
                    "proj_validation",
                    start_step="prompt_engineer",
                )

        self.assertFalse(result["valid"])
        self.assertIn("30_style/style_text.json", result["message"])
        self.assertIn("30_style/style_images.json", result["message"])

    def test_required_artifacts_follow_dependency_graph_not_previous_order(self):
        """screenplay branch не должна требовать image/pdf артефакты предыдущей ветки."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        models = load_light_workflow_models()
        workflow_def = models.WorkflowDefinition.from_yaml(
            Path(__file__).resolve().parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
        )

        required = PipelineRunner._collect_required_artifacts(
            workflow_def,
            start_step="screenplay_generator",
        )

        self.assertIn("20_story/story.json", required)
        self.assertNotIn("50_images", required)
        self.assertNotIn("90_md/book.md", required)
        self.assertNotIn("95_pdf/book.pdf", required)

    def test_partial_run_with_generate_blockout_false_does_not_require_blockout_artifacts(self):
        """ТЗ раздел 18.8/A23: частичный запуск с artist_batch_shots на проекте с
        generate_blockout: false не должен требовать артефакты blockout_scene_builder
        (blockout_renderer в этом случае отрабатывает как no-op, раздел 11.2)."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        models = load_light_workflow_models()
        workflow_def = models.WorkflowDefinition.from_yaml(
            Path(__file__).resolve().parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir) / "proj_blockout_off"
            project_path.mkdir(parents=True, exist_ok=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "Story", "generate_blockout": False}),
                encoding="utf-8",
            )

            required = PipelineRunner._collect_required_artifacts(
                workflow_def,
                start_step="artist_batch_shots",
                project_path=project_path,
            )

        self.assertNotIn("93_blockout/chains.json", required)
        self.assertNotIn("93_blockout/scene_spec.json", required)
        self.assertNotIn("93_blockout/asset_map.json", required)
        self.assertNotIn("93_blockout/report.json", required)

    def test_partial_run_with_generate_blockout_true_requires_blockout_artifacts(self):
        """Контрольный кейс к предыдущему тесту: с generate_blockout: true артефакты
        blockout_scene_builder должны требоваться (доказывает, что фильтрация реально
        завязана на флаг, а не на пустой список артефактов по умолчанию)."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        models = load_light_workflow_models()
        workflow_def = models.WorkflowDefinition.from_yaml(
            Path(__file__).resolve().parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            project_path = Path(tmp_dir) / "proj_blockout_on"
            project_path.mkdir(parents=True, exist_ok=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "Story", "generate_blockout": True}),
                encoding="utf-8",
            )

            required = PipelineRunner._collect_required_artifacts(
                workflow_def,
                start_step="artist_batch_shots",
                project_path=project_path,
            )

        self.assertIn("93_blockout/chains.json", required)
        self.assertIn("93_blockout/scene_spec.json", required)

    def test_project_path_omitted_defaults_generate_blockout_to_false(self):
        """Р6: отсутствующий project_path (как в существующих вызовах без 3-го
        аргумента) не должен ломаться и трактуется как generate_blockout=False."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        models = load_light_workflow_models()
        workflow_def = models.WorkflowDefinition.from_yaml(
            Path(__file__).resolve().parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
        )

        required = PipelineRunner._collect_required_artifacts(
            workflow_def,
            start_step="artist_batch_shots",
        )

        self.assertNotIn("93_blockout/chains.json", required)


class TestBlockoutPreLaunchValidation(unittest.TestCase):
    """ТЗ раздел 18.6: блок «Болванка» в validate_project_for_pipeline()."""

    @staticmethod
    def _make_runner():
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        with patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"):
            return PipelineRunner()

    def test_missing_brief_no_unbound_local_error(self):
        """A41: строится через __new__, как обе веб-поверхности (service.py,
        02_Workflows.py) — не через полноценный __init__."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            (projects_root / "proj_no_brief").mkdir()
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}):
                result = runner.validate_project_for_pipeline("proj_no_brief")

        self.assertFalse(result["valid"])
        self.assertIn("Отсутствует файл 00_brief.json", result["message"])
        self.assertNotIn("referenced before assignment", result["message"])

    def test_corrupt_brief_no_unbound_local_error(self):
        """A41: строится через __new__, как обе веб-поверхности (service.py,
        02_Workflows.py) — не через полноценный __init__."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_bad_brief"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text("{not valid json", encoding="utf-8")
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}):
                result = runner.validate_project_for_pipeline("proj_bad_brief")

        self.assertFalse(result["valid"])
        self.assertIn("Некорректный JSON", result["message"])
        self.assertNotIn("referenced before assignment", result["message"])

    def test_validate_project_for_pipeline_works_without_calling_init(self):
        """A41: обе веб-поверхности строят PipelineRunner через
        PipelineRunner.__new__(PipelineRunner), минуя __init__ — блок «Болванка»
        не должен обращаться к self.<инстанс-атрибуты>, иначе первый же вызов
        упадёт с AttributeError вместо человекочитаемого результата."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)

        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_bare"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text(json.dumps({"title": "T"}), encoding="utf-8")

            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": False, "message": "not found"},
            ):
                result = runner.validate_project_for_pipeline("proj_bare")

        self.assertTrue(result["valid"])
        self.assertNotIn("ModuleNotFoundError", result["message"])

    def test_b12_warns_but_stays_valid_when_blender_missing_and_blockout_off(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_no_blender_off"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": False}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": False, "message": "blender binary not found in PATH"},
            ):
                result = runner.validate_project_for_pipeline("proj_no_blender_off")

        self.assertTrue(result["valid"])
        self.assertTrue(any("Blender" in w for w in result["warnings"]))

    def test_b12_warns_but_stays_valid_when_blender_missing_and_blockout_on(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_no_blender_on"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": True}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": False, "message": "blender binary not found in PATH"},
            ):
                result = runner.validate_project_for_pipeline("proj_no_blender_on")

        self.assertTrue(result["valid"])
        self.assertTrue(any("Blender" in w for w in result["warnings"]))

    def test_b01_b15_skipped_without_video_model_caps_file(self):
        """Раздел 18.6: без 97_shots/video_model_caps.json B01/B15 не проверяются
        даже при generate_blockout: true — нормализация ещё ни разу не отрабатывала."""
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_fresh"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": True}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_fresh")

        self.assertTrue(result["valid"])
        self.assertFalse(any("B01" in e or "B15" in e for e in result["errors"]))

    def test_b15_blocks_when_supported_durations_empty_and_blockout_enabled(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_empty_caps"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": True}), encoding="utf-8"
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [], "source": None}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_empty_caps")

        self.assertFalse(result["valid"])
        self.assertTrue(any("B15" in e for e in result["errors"]))

    def test_b15_skipped_when_generate_blockout_false_even_with_empty_caps(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_off_empty_caps"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": False}), encoding="utf-8"
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [], "source": None}), encoding="utf-8"
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_off_empty_caps")

        self.assertTrue(result["valid"])
        self.assertFalse(any("B15" in e for e in result["errors"]))

    def test_b01_flags_shot_with_duration_outside_supported_set(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_bad_duration"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": True}), encoding="utf-8"
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7, 10], "source": "catalog"}), encoding="utf-8"
            )
            (project_path / "97_shots" / "shots.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {"scene_number": 1, "shot_number": 1, "duration_s": 5},
                            {"scene_number": 1, "shot_number": 2, "duration_s": 6},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_bad_duration")

        self.assertFalse(result["valid"])
        self.assertTrue(any("B01" in e and "scene 1 shot 2" in e for e in result["errors"]))

    def test_b01_passes_when_all_durations_in_supported_set(self):
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_good_duration"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "T", "generate_blockout": True}), encoding="utf-8"
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7, 10], "source": "catalog"}), encoding="utf-8"
            )
            (project_path / "97_shots" / "shots.json").write_text(
                json.dumps({"items": [{"scene_number": 1, "shot_number": 1, "duration_s": 5}]}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_good_duration")

        self.assertTrue(result["valid"])
        self.assertFalse(any("B01" in e for e in result["errors"]))

    def test_b01_uses_intersection_with_blockout_allowed_durations_not_full_supported_set(self):
        """ТЗ раздел 10.1 п.2 / 6.1: допустимый набор для B01 — пересечение
        supported_durations с непустым blockout_allowed_durations, а не полный
        набор модели. Сценарий отказа (раздел 18.6): проект сгенерирован при
        blockout_allowed_durations: [] (весь набор модели [5, 7, 10]), у шота
        duration_s=7. Пользователь снимает галочку у 7 на панели генерации,
        набор в 00_brief.json сужается до [5, 10], но screenplay_shots_generator
        повторно не запускается — на диске остаётся duration_s=7. Построен
        через __new__, как обе веб-поверхности (A41)."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_narrowed_durations"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps(
                    {
                        "title": "T",
                        "generate_blockout": True,
                        "blockout_allowed_durations": [5, 10],
                    }
                ),
                encoding="utf-8",
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7, 10], "source": "catalog"}), encoding="utf-8"
            )
            (project_path / "97_shots" / "shots.json").write_text(
                json.dumps({"items": [{"scene_number": 1, "shot_number": 1, "duration_s": 7}]}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_narrowed_durations")

        self.assertFalse(result["valid"])
        self.assertTrue(any("B01" in e and "scene 1 shot 1" in e for e in result["errors"]))

    def test_b01_passes_when_narrowed_durations_still_cover_all_shots(self):
        """Обратный случай к предыдущему тесту: сужение blockout_allowed_durations,
        при котором все duration_s остаются допустимыми — ложной ошибки быть не
        должно."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_narrowed_ok"
            (project_path / "97_shots").mkdir(parents=True)
            (project_path / "00_brief.json").write_text(
                json.dumps(
                    {
                        "title": "T",
                        "generate_blockout": True,
                        "blockout_allowed_durations": [5, 10],
                    }
                ),
                encoding="utf-8",
            )
            (project_path / "97_shots" / "video_model_caps.json").write_text(
                json.dumps({"supported_durations": [5, 7, 10], "source": "catalog"}), encoding="utf-8"
            )
            (project_path / "97_shots" / "shots.json").write_text(
                json.dumps({"items": [{"scene_number": 1, "shot_number": 1, "duration_s": 5}]}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": str(projects_root.resolve())}), patch(
                "custom_tools.storybook.video_contract._probe_blockout_blender_readiness",
                return_value={"available": True, "message": "ok"},
            ):
                result = runner.validate_project_for_pipeline("proj_narrowed_ok")

        self.assertTrue(result["valid"])
        self.assertFalse(any("B01" in e for e in result["errors"]))

    def test_blender_probe_timeout_bounds_validate_call(self):
        """Раздел 18.6/18.3: тайм-аут проверки Blender (5с) реально ограничивает
        время синхронного вызова — обе веб-поверхности зовут
        validate_project_for_pipeline() синхронно из обработчика запроса, и без
        тайм-аута зависший бинарник повесил бы обработчик."""
        import time

        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_hang"
            project_path.mkdir()
            (project_path / "00_brief.json").write_text(json.dumps({"title": "T"}), encoding="utf-8")

            hang_script = projects_root / "hang_blender.sh"
            hang_script.write_text("#!/bin/sh\nsleep 30\n")
            hang_script.chmod(0o755)

            env = {
                "STORYBOOK_PROJECTS_DIR": str(projects_root.resolve()),
                "BLOCKOUT_BLENDER_MODE": "binary",
                "BLOCKOUT_BLENDER_BIN": str(hang_script),
            }
            with patch.dict(os.environ, env):
                started = time.monotonic()
                result = runner.validate_project_for_pipeline("proj_hang")
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 15)
        self.assertTrue(result["valid"])
        self.assertTrue(any("Blender" in w for w in result["warnings"]))
