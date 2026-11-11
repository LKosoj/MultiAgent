"""Чистая логика оболочки Tk (без окон): разделы, цвета, прогон, тосты, окно, клавиши."""

import unittest

from StoryBookManager.gui import theme_tokens as tk_tokens
from StoryBookManager.gui.shell import run_status, sections, shortcuts, window_state
from StoryBookManager.gui.shell.status_poller import StatusPoller
from StoryBookManager.gui.shell.toast_model import ToastModel


class TestSections(unittest.TestCase):
    def test_sections_and_landing(self):
        self.assertEqual(len(sections.SECTIONS), 9)
        self.assertEqual(sections.LANDING_AFTER_OPEN, "overview")
        self.assertEqual(sections.section_by_number(1).id, "projects")
        self.assertFalse(sections.section_by_id("projects").needs_project)
        self.assertTrue(sections.section_by_id("shots").needs_project)


class TestTheme(unittest.TestCase):
    def test_text_contrast_on_main_backgrounds(self):
        c = tk_tokens.COLORS
        for fg in ("text_primary", "text_muted"):
            for bg in ("window_bg", "surface", "card"):
                self.assertGreaterEqual(tk_tokens.contrast_ratio(c[fg], c[bg]), 4.5, (fg, bg))

    def test_scale(self):
        self.assertEqual(tk_tokens.normalize_ui_scale(999), 100)
        self.assertEqual(tk_tokens.normalize_ui_scale(150), 150)


class TestRunStatus(unittest.TestCase):
    def test_poll_interval(self):
        self.assertEqual(run_status.poll_interval_ms("running"), 2000)
        self.assertEqual(run_status.poll_interval_ms("idle"), 10000)

    def test_rub_format(self):
        self.assertEqual(run_status.format_rub(1204), "1 204 ₽")

    def test_no_brief_is_not_running(self):
        view = run_status.build_run_view(None)
        self.assertEqual(view.owner, "none")


class TestToastModel(unittest.TestCase):
    def test_limit_and_errors_stay(self):
        t = [0.0]
        model = ToastModel(now=lambda: t[0], limit=4)
        model.add("error", "сбой")
        for i in range(6):
            model.add("info", f"сообщение {i}")
        self.assertLessEqual(len(model.items), 4)
        t[0] = 100.0
        model.expire()
        self.assertEqual([x.kind for x in model.items], ["error"])

    def test_repeat_merged(self):
        model = ToastModel(now=lambda: 0.0)
        model.add("info", "то же")
        model.add("info", "то же")
        self.assertEqual(len(model.items), 1)


class TestWindowState(unittest.TestCase):
    def test_clamp_into_screen(self):
        geo = window_state.clamp_geometry("1200x800+5000+5000", 1600, 1000, 1024, 680)
        w, rest = geo.split("x", 1)
        h, x, y = rest.replace("+", " +").replace("-", " -").split()
        self.assertLess(int(x), 1600)
        self.assertLess(int(y), 1000)

    def test_bad_value_gives_default(self):
        self.assertTrue(window_state.clamp_geometry("мусор", 1600, 1000, 1024, 680))


class TestShortcuts(unittest.TestCase):
    def test_cyrillic_and_mac(self):
        seqs = shortcuts.expand_sequences("k", platform="linux")
        self.assertTrue(any("Cyrillic" in s for s in seqs))
        self.assertTrue(any("Command" in s for s in shortcuts.expand_sequences("k", platform="darwin")))

    def test_help_mentions_ctrl_k(self):
        self.assertIn("Ctrl+K", shortcuts.help_text())


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, cap, payload, on_success, on_error, latest_only_key=None):
        self.calls.append((payload, on_success, on_error))


class FakeWidget:
    def __init__(self):
        self.timers = {}
        self._n = 0

    def after(self, ms, fn):
        self._n += 1
        self.timers[self._n] = (ms, fn)
        return self._n

    def after_cancel(self, i):
        self.timers.pop(i, None)

    def winfo_viewable(self):
        return True


class TestStatusPoller(unittest.TestCase):
    def _make(self):
        self.bridge, self.widget = FakeBridge(), FakeWidget()
        self.updates, self.bad = [], []
        return StatusPoller(self.bridge, self.widget, on_update=self.updates.append,
                            on_unavailable=lambda: self.bad.append(1))

    def test_single_request_in_flight_and_extra_refresh(self):
        poller = self._make()
        poller.start("p1")
        poller.request_refresh()
        poller.request_refresh()
        self.assertEqual(len(self.bridge.calls), 1)
        self.bridge.calls[0][1]({"run": {"state": "idle"}})
        self.assertEqual(len(self.bridge.calls), 2)  # один внеочередной запрос после ответа
        self.assertEqual(len(self.updates), 1)

    def test_refresh_when_idle_asks_immediately(self):
        poller = self._make()
        poller.start("p1")
        self.bridge.calls[0][1]({"run": {"state": "idle"}})
        self.assertEqual(self.widget.timers[max(self.widget.timers)][0], 10000)
        poller.request_refresh()
        self.assertEqual(len(self.bridge.calls), 2)

    def test_project_change_drops_old_answer(self):
        poller = self._make()
        poller.start("p1")
        old_ok = self.bridge.calls[0][1]
        poller.start("p2")
        old_ok({"project": {"id": "p1"}})
        self.assertEqual(self.updates, [])
        self.assertEqual(self.bridge.calls[1][0]["project_id"], "p2")

    def test_three_errors_make_unavailable(self):
        poller = self._make()
        poller.start("p1")
        for _ in range(3):
            self.bridge.calls[-1][2]({})
            if len(self.bad) == 0:
                self.assertTrue(self.widget.timers)
                _, tick = self.widget.timers[max(self.widget.timers)]
                tick()
        self.assertEqual(len(self.bad), 1)

    def test_stop_ignores_late_answer_and_refresh(self):
        poller = self._make()
        poller.start("p1")
        ok = self.bridge.calls[0][1]
        poller.stop()
        ok({"run": {"state": "running"}})
        poller.request_refresh()
        self.assertEqual(self.updates, [])
        self.assertEqual(len(self.bridge.calls), 1)
        self.assertFalse(self.widget.timers)


class TestRunViewMatrix(unittest.TestCase):
    def brief(self, state, surface="react"):
        return {"run": {"state": state, "text": "t", "surface": surface}}

    def test_remote_run_needs_registry(self):
        v = run_status.build_run_view(self.brief("running"))
        self.assertEqual(v.owner, "remote")
        self.assertFalse(v.pause_enabled or v.stop_enabled)
        self.assertEqual(v.pause_hint, run_status.NO_REMOTE_HINT)
        v = run_status.build_run_view(self.brief("running"), {"run_set_pause", "run_cancel"})
        self.assertTrue(v.pause_enabled and v.stop_enabled)
        self.assertEqual(v.stop_action, "remote")
        v = run_status.build_run_view(self.brief("running"), {"run_cancel"})
        self.assertTrue(v.stop_enabled and not v.pause_enabled)

    def test_stopping_disables_both(self):
        v = run_status.build_run_view(self.brief("stopping"), {"run_set_pause", "run_cancel"})
        self.assertFalse(v.pause_enabled or v.stop_enabled)
        self.assertEqual(v.stop_hint, run_status.STOPPING_HINT)

    def test_pause_requested_offers_resume(self):
        v = run_status.build_run_view(self.brief("pause_requested"), {"run_set_pause"})
        self.assertEqual(v.pause_label, "Продолжить")
        self.assertFalse(v.pause_target)

    def test_interrupted_has_no_owner(self):
        v = run_status.build_run_view(self.brief("interrupted"))
        self.assertEqual((v.owner, v.tone), ("none", "warning"))


if __name__ == "__main__":
    unittest.main()
