"""Модель экрана «Обзор»: build_overview, with_error, loading_view, no_project_view."""

import unittest

from StoryBookManager.gui.sections import overview_logic as O


STATUS = {
    "project": {"id": "p", "title": "Сказка"},
    "active_run": {"state": "running", "text": "Идёт шаг 3"},
    "next_action": {"title": "Проверьте кадры", "detail": "д", "severity": "warning",
                    "target": {"screen": "shots"}},
    "warnings": {"count": 2},
    "incomplete_runs": [{}],
}


class TestOverviewLogic(unittest.TestCase):
    def test_no_project_and_loading(self):
        self.assertEqual(O.no_project_view().mode, "no_project")
        self.assertEqual(O.loading_view().mode, "loading")

    def test_build_overview_fills_fields(self):
        view = O.build_overview(STATUS)
        self.assertEqual((view.mode, view.title, view.run_text), ("ready", "Сказка", "Идёт шаг 3"))
        self.assertEqual(view.next_action.screen, "shots")
        self.assertIn("2", view.warnings_text)
        self.assertIn("1", view.last_run_note)

    def test_same_status_gives_equal_views(self):
        # экран пересобирается только если вид изменился
        self.assertEqual(O.build_overview(STATUS), O.build_overview(dict(STATUS)))

    def test_no_reports_text(self):
        self.assertEqual(O.build_overview({"project": {"id": "p"}}).warnings_text, O.TXT_NO_REPORTS)

    def test_with_error_keeps_data_and_marks_stale(self):
        stale = O.with_error(O.build_overview(STATUS), {"message": "Нет связи."})
        self.assertEqual((stale.mode, stale.title, stale.stale), ("ready", "Сказка", True))
        self.assertIn("Нет связи", stale.error)

    def test_with_error_without_data_is_error_view(self):
        view = O.with_error(O.loading_view(), {"message": "x"})
        self.assertEqual(view.mode, "error")
        self.assertFalse(view.gone)

    def test_not_found_is_gone_and_drops_data(self):
        view = O.with_error(O.build_overview(STATUS), {"code": "not_found"})
        self.assertTrue(view.gone)
        self.assertEqual(view.error, O.TXT_GONE)

    def test_final_video_path(self):
        final = O.FinalVideo(True, "99_final/final_video.mp4")
        self.assertEqual(str(O.final_video_path("/p", final)), "/p/99_final/final_video.mp4")
        self.assertIsNone(O.final_video_path("/p", O.FinalVideo(False)))


if __name__ == "__main__":
    unittest.main()
