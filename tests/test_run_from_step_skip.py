"""
Тесты для run_from_step (частичный запуск pipeline) и связанных находок WS-J.

H-8: run_from_step работоспособен для любого шага (не только первого):
- hard-fail validate_step_dependencies снят (helper остался диагностическим),
- пограничные depends_on на ВЫРЕЗАННЫЕ шаги развязываются (нет висячих ссылок),
- step_outputs грузятся из checkpoint.
Тесты идут на реальном WorkflowDefinition.from_yaml (НЕ MagicMock), чтобы
ловить «второй слой» H-8 (висячие depends_on → ValueError/deadlock движка).

Дополнительно:
- M-29: проброс step_error/step_error_class/step_traceback в progress_callback.
- M-30: артефакт-маркеры результата (леджер вместо каталога).
- M-31: файловый advisory-lock по project_id (не-блокирующий).
- отсутствие хардкод-условия condition="true" в исходнике.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

YAML_PATH = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"


def _run(coro):
    """Гоняет корутину в изолированном event loop (in-process, без общего loop)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_runner():
    """PipelineRunner с замоканным engine, локом и checkpoint (без FS-side-effects)."""
    with patch(
        "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
    ):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()
    runner.engine = MagicMock()
    runner.engine.execute_workflow = AsyncMock(return_value=MagicMock())
    # Лок мокаем — реальный fcntl-lock проверяется отдельно в TestProjectLock.
    runner._acquire_project_lock = lambda project_id: "LOCK"
    runner._release_project_lock = lambda handle: None
    runner._get_latest_project_checkpoint = AsyncMock(return_value=None)
    return runner


class TestRunFromStepRealYaml(unittest.TestCase):
    """H-8 на реальном YAML: частичный запуск с середины pipeline."""

    def setUp(self):
        if not YAML_PATH.exists():
            self.skipTest("storybook_pipeline.yaml not found")
        from workflow.models import WorkflowDefinition

        self._full_ids = [s.id for s in WorkflowDefinition.from_yaml(YAML_PATH).steps]

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_from_midpipeline_step_succeeds(self):
        """Запуск с середины (video_generator) → success (снятый hard-fail)."""
        runner = _make_runner()
        result = _run(runner.run_from_step("proj1", "video_generator"))
        self.assertEqual(result["status"], "success")
        runner.engine.execute_workflow.assert_awaited_once()

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_from_step_with_upstream_dep_succeeds(self):
        """storybook_audio_subtitle зависит от вырезанного video_generator → всё равно success."""
        runner = _make_runner()
        result = _run(runner.run_from_step("proj1", "storybook_audio_subtitle"))
        self.assertEqual(result["status"], "success")

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_sliced_def_has_no_dangling_depends_on(self):
        """Главный «второй слой» H-8: в срезе нет depends_on на ВЫРЕЗАННЫЕ шаги."""
        runner = _make_runner()
        _run(runner.run_from_step("proj1", "video_generator"))
        passed_def = runner.engine.execute_workflow.call_args[0][0]
        remaining = {s.id for s in passed_def.steps}
        dangling = [
            (s.id, dep)
            for s in passed_def.steps
            for dep in s.depends_on
            if dep not in remaining
        ]
        self.assertEqual(dangling, [], f"Висячие depends_on на вырезанные шаги: {dangling}")

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_start_step_slicing(self):
        """Первый шаг среза == запрошенный; skipped_steps == индекс шага."""
        runner = _make_runner()
        result = _run(runner.run_from_step("proj1", "video_generator"))
        passed_def = runner.engine.execute_workflow.call_args[0][0]
        self.assertEqual(passed_def.steps[0].id, "video_generator")
        self.assertEqual(result["skipped_steps"], self._full_ids.index("video_generator"))

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_start_from_first_step_skips_nothing(self):
        """Запуск с первого шага ничего не режет."""
        runner = _make_runner()
        result = _run(runner.run_from_step("proj1", self._full_ids[0]))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["skipped_steps"], 0)
        passed_def = runner.engine.execute_workflow.call_args[0][0]
        self.assertEqual(len(passed_def.steps), len(self._full_ids))

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_nonexistent_step_returns_error(self):
        """Несуществующий шаг → error, execute_workflow не await'ится."""
        runner = _make_runner()
        result = _run(runner.run_from_step("proj1", "nonexistent_step"))
        self.assertEqual(result["status"], "error")
        self.assertIn("не найден", result["message"])
        runner.engine.execute_workflow.assert_not_awaited()

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_checkpoint_step_outputs_loaded_into_context(self):
        """step_outputs вырезанных шагов подтягиваются из checkpoint в контекст."""
        runner = _make_runner()
        runner._get_latest_project_checkpoint = AsyncMock(
            return_value=SimpleNamespace(
                context=SimpleNamespace(step_outputs={"video_generator": {"ok": True}})
            )
        )
        _run(runner.run_from_step("proj1", "storybook_audio_subtitle"))
        passed_ctx = runner.engine.execute_workflow.call_args[0][1]
        self.assertEqual(passed_ctx.step_outputs.get("video_generator"), {"ok": True})


class TestStepOutputArtifacts(unittest.TestCase):
    """M-30: артефакт-маркеры результата вместо существования каталога."""

    def test_video_generator_marker_is_ledger_file(self):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        artifacts = PipelineRunner._step_output_artifacts()
        self.assertEqual(artifacts["video_generator"], ["97_shots/provider_jobs.json"])
        self.assertEqual(artifacts["artist_batch_shots"], ["97_shots/shots.json"])

    def test_blockout_scene_builder_and_preview_artifacts_registered(self):
        """ТЗ раздел 18.8: реестр артефактов болванки."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        artifacts = PipelineRunner._step_output_artifacts()
        self.assertEqual(
            artifacts["blockout_scene_builder"],
            [
                "93_blockout/chains.json",
                "93_blockout/scene_spec.json",
                "93_blockout/asset_map.json",
                "93_blockout/report.json",
            ],
        )
        self.assertEqual(
            artifacts["blockout_preview"],
            ["93_blockout/preview/blockout_all.mp4", "93_blockout/preview/contact_sheet.png"],
        )
        self.assertNotIn("blockout_renderer", artifacts)

    def test_metadata_override_precedence(self):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        step = MagicMock()
        step.id = "video_generator"
        step.metadata = {"output_artifacts": ["custom/marker.json"]}
        wf = MagicMock()
        wf.steps = [step]
        artifacts = PipelineRunner._step_output_artifacts(wf)
        self.assertEqual(artifacts["video_generator"], ["custom/marker.json"])

    def test_audio_subtitle_requires_video_ledger(self):
        if not YAML_PATH.exists():
            self.skipTest("storybook_pipeline.yaml not found")
        from workflow.models import WorkflowDefinition
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        wf = WorkflowDefinition.from_yaml(YAML_PATH)
        required = PipelineRunner._collect_required_artifacts(wf, "storybook_audio_subtitle")
        self.assertIn("97_shots/provider_jobs.json", required)


class TestHookErrorPropagation(unittest.TestCase):
    """M-29: причина падения шага пробрасывается в progress_callback."""

    def test_hook_forwards_step_error(self):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            runner = PipelineRunner()
        runner.engine = MagicMock()
        runner.engine._on_step_completed = AsyncMock()
        runner.engine._execute_workflow_step = AsyncMock()

        captured = {}

        def cb(**kwargs):
            captured.update(kwargs)

        runner._install_step_hook(cb, total_steps=1)
        step = SimpleNamespace(id="video_generator")
        step_result = SimpleNamespace(
            status=SimpleNamespace(value="failed"),
            duration_seconds=1.5,
            error="boom",
            error_class="tool_error",
            metadata={"traceback": "Traceback (most recent call last): boom"},
        )
        _run(runner.engine._on_step_completed("wf1", step, step_result, MagicMock(), {}))
        runner._uninstall_step_hook()

        self.assertEqual(captured.get("step_status"), "failed")
        self.assertEqual(captured.get("step_error"), "boom")
        self.assertEqual(captured.get("step_error_class"), "tool_error")
        self.assertEqual(
            captured.get("step_traceback"), "Traceback (most recent call last): boom"
        )

    def test_report_failed_steps_reconciles_from_result(self):
        """M-29 (реальный канал): движок вызывает completion-хук ТОЛЬКО для
        успешных шагов, поэтому упавший шаг доводится до UI постфактум —
        реконсиляцией по result.step_results со step_status='failed'."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        captured = []

        def cb(**kwargs):
            captured.append(kwargs)

        result = SimpleNamespace(step_results={
            "video_generator": SimpleNamespace(
                status=SimpleNamespace(value="failed"),
                duration_seconds=2.0,
                error="boom",
                error_class="tool_error",
                metadata={"traceback": "TB"},
            ),
            "story_writer": SimpleNamespace(
                status=SimpleNamespace(value="completed"),
                duration_seconds=1.0,
                error=None,
                metadata={},
            ),
        })

        PipelineRunner._report_failed_steps(result, cb)

        # Только упавший шаг доведён, завершённый — пропущен.
        self.assertEqual(len(captured), 1)
        failed = captured[0]
        self.assertEqual(failed["step_id"], "video_generator")
        self.assertEqual(failed["step_status"], "failed")
        self.assertEqual(failed["step_error"], "boom")
        self.assertEqual(failed["step_error_class"], "tool_error")
        self.assertEqual(failed["step_traceback"], "TB")

    def test_report_failed_steps_noop_without_callback(self):
        """Без progress_callback реконсиляция — no-op (не бросает)."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        result = SimpleNamespace(step_results={
            "x": SimpleNamespace(
                status=SimpleNamespace(value="failed"), error="e", metadata={}
            ),
        })
        PipelineRunner._report_failed_steps(result, None)


class TestProjectLock(unittest.TestCase):
    """M-31: не-блокирующий advisory-lock по project_id."""

    def test_second_acquire_blocked_then_released(self):
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner

            runner = PipelineRunner()

        with tempfile.TemporaryDirectory() as td:
            proj_dir = Path(td)
            with patch(
                "custom_tools.storybook.project_paths.safe_storybook_project_dir",
                return_value=proj_dir,
            ):
                h1 = runner._acquire_project_lock("proj1")
                self.assertIsNotNone(h1)
                # Второй захват (другой fd, тот же файл) — не блокируется, сразу None.
                h2 = runner._acquire_project_lock("proj1")
                self.assertIsNone(h2)
                # После освобождения — снова доступен.
                runner._release_project_lock(h1)
                h3 = runner._acquire_project_lock("proj1")
                self.assertIsNotNone(h3)
                runner._release_project_lock(h3)


class TestNoHardcodedConditionTrue(unittest.TestCase):
    """Проверяет отсутствие condition='true' хака в исходном коде"""

    def test_no_condition_true_in_run_from_step(self):
        """В run_from_step нет condition = 'true'"""
        source = (
            project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")

        start = source.index("async def run_from_step(")
        next_def = source.index("\n    async def ", start + 1) if "\n    async def " in source[start + 1:] else source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertNotIn('condition = "true"', method_body)
        self.assertNotIn("condition = 'true'", method_body)


if __name__ == "__main__":
    unittest.main()
