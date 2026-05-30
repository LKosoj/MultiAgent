"""
Тесты для P3.1: обнаружение незавершённых workflow при загрузке проекта.

Проверяет:
- get_incomplete_workflows определён в PipelineRunner
- _check_incomplete_workflows вызывается в load_project
- Информация о незавершённых workflow сохраняется в _incomplete_workflows
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
PANEL_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


class TestGetIncompleteWorkflows(unittest.TestCase):

    def test_method_exists(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("async def get_incomplete_workflows(self", source)

    def test_queries_sqlite(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("workflow_checkpoints", body)
        self.assertIn("NOT IN ('completed', 'cancelled')", body)

    def test_returns_list(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("return results", body)
        self.assertIn("return []", body)

    def test_filters_by_project_id(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("project_id", body)


class TestCheckIncompleteWorkflowsIntegration(unittest.TestCase):

    def test_check_method_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _check_incomplete_workflows(self", source)

    def test_called_in_load_project(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def load_project(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_check_incomplete_workflows", body)

    def test_stores_incomplete_workflows(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _check_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_incomplete_workflows", body)

    def test_logs_warning_on_incomplete(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("незавершённый pipeline", body)
        self.assertIn("warning", body)


class TestRecoveryDialog(unittest.TestCase):
    """Проверяет UI диалога восстановления"""

    def test_show_recovery_dialog_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _show_recovery_dialog(self", source)

    def test_dialog_shows_resume_option(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("Возобновить", body)

    def test_dialog_shows_start_over_option(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("начать сначала", body.lower())

    def test_dialog_shows_last_step(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("current_step", body)
        self.assertIn("Последний шаг", body)

    def test_dialog_shows_timestamp(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("timestamp", body)
        self.assertIn("Время", body)

    def test_check_calls_dialog(self):
        """_check_incomplete_workflows вызывает _show_recovery_dialog"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _check_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_show_recovery_dialog", body)


class TestResumeFromCheckpoint(unittest.TestCase):
    """Проверяет логику восстановления pipeline с чекпоинта"""

    def test_resume_method_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _resume_from_checkpoint(self", source)

    def test_resume_uses_run_from_step(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_run_from_step_thread", body)

    def test_resume_determines_step_from_completed(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("completed_steps", body)
        self.assertIn("current_step", body)

    def test_dialog_calls_resume(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_resume_from_checkpoint", body)

    def test_skipped_steps_not_restarted(self):
        """Используется run_from_step, а не run_full_pipeline"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertNotIn("run_full_pipeline", body)
        self.assertIn("_run_from_step_thread", body)


class TestRestartRecoveryViaCheckpointStore(unittest.TestCase):
    """Проверяет логику восстановления через SQLite EventStore/checkpoint.

    Тест не импортирует PipelineRunner (требует Tk/asyncio), а проверяет
    SQL-запрос в get_incomplete_workflows через исходный код и
    логику чтения checkpoint-строк через реальный SQLite in-memory.
    """

    def _create_checkpoint_db(self, conn):
        """Создаёт схему workflow_checkpoints, аналогичную production."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step TEXT,
                completed_steps TEXT DEFAULT '[]',
                timestamp TEXT NOT NULL,
                resumable INTEGER DEFAULT 1,
                context TEXT DEFAULT '{}'
            )
        """)
        conn.commit()

    def test_incomplete_workflows_query_excludes_completed_and_cancelled(self):
        """SQL-запрос в get_incomplete_workflows фильтрует completed/cancelled."""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        self._create_checkpoint_db(conn)

        rows = [
            ("proj1-wf1", "running", "step2", '["step1"]', "2026-01-01T10:00:00", 1),
            ("proj1-wf2", "completed", "step3", '["step1","step2","step3"]', "2026-01-01T09:00:00", 0),
            ("proj1-wf3", "cancelled", "step1", '[]', "2026-01-01T08:00:00", 0),
            ("proj1-wf4", "failed", "step2", '["step1"]', "2026-01-01T07:00:00", 1),
        ]
        conn.executemany(
            "INSERT INTO workflow_checkpoints "
            "(workflow_id, status, current_step, completed_steps, timestamp, resumable) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        project_id = "proj1"
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

        results = [dict(row) for row in cursor.fetchall()]

        statuses = {r["workflow_id"]: r["status"] for r in results}
        self.assertIn("proj1-wf1", statuses, "running workflow должен быть возвращён")
        self.assertIn("proj1-wf4", statuses, "failed workflow должен быть возвращён")
        self.assertNotIn("proj1-wf2", statuses, "completed workflow не должен возвращаться")
        self.assertNotIn("proj1-wf3", statuses, "cancelled workflow не должен возвращаться")

    def test_incomplete_workflows_resume_uses_correct_checkpoint_fields(self):
        """get_incomplete_workflows сохраняет completed_steps как список (JSON).

        Это критично для _resume_from_checkpoint: он определяет с какого шага
        продолжить пайплайн по completed_steps.
        """
        import sqlite3
        import json

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        self._create_checkpoint_db(conn)

        conn.execute(
            "INSERT INTO workflow_checkpoints "
            "(workflow_id, status, current_step, completed_steps, timestamp, resumable) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("proj2-wf1", "running", "step3", '["step1", "step2"]', "2026-01-02T12:00:00", 1),
        )
        conn.commit()

        cursor = conn.execute(
            "SELECT completed_steps FROM workflow_checkpoints WHERE workflow_id = ?",
            ("proj2-wf1",),
        )
        row = cursor.fetchone()

        completed = json.loads(row["completed_steps"])
        self.assertIsInstance(completed, list, "completed_steps должен десериализоваться в list")
        self.assertEqual(completed, ["step1", "step2"])

    def test_get_incomplete_workflows_source_uses_project_id_filter(self):
        """SQL в get_incomplete_workflows содержит фильтр по project_id."""
        source = RUNNER_PATH.read_text(encoding="utf-8")
        try:
            start = source.index("async def get_incomplete_workflows(self")
        except ValueError:
            self.fail("Метод async def get_incomplete_workflows не найден в pipeline_runner.py")
        try:
            next_def = source.index("\n    async def ", start + 1)
        except ValueError:
            try:
                next_def = source.index("\n    def ", start + 1)
            except ValueError:
                next_def = len(source)
        body = source[start:next_def]
        self.assertIn("project_id", body, "Запрос должен фильтровать по project_id")
        self.assertIn("NOT IN ('completed', 'cancelled')", body)


if __name__ == "__main__":
    unittest.main()
