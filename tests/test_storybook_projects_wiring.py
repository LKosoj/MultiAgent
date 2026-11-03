"""Проводка экрана «Проекты» без tkinter: подтверждения, подсказки серверу, задания, режим опроса."""

import unittest

from StoryBookManager.gui.sections.job_runner import JobRunner
from StoryBookManager.gui.sections.projects_controller import ProjectsController
from StoryBookManager.gui.shell.status_poller import StatusPoller


class Bridge:
    def __init__(self):
        self.calls = []   # (capability, payload, ok, err)

    def call(self, cap, payload=None, on_success=None, on_error=None, latest_only_key=None):
        self.calls.append((cap, dict(payload or {}), on_success, on_error))

    def last(self, cap):
        return [c for c in self.calls if c[0] == cap][-1]

    def names(self):
        return [c[0] for c in self.calls]


class Widget:
    def __init__(self):
        self.timers = {}
        self.n = 0

    def after(self, ms, fn):
        self.n += 1
        self.timers[self.n] = (ms, fn)
        return self.n

    def after_cancel(self, i):
        self.timers.pop(i, None)

    def winfo_viewable(self):
        return True

    def fire(self):
        for key in list(self.timers):
            self.timers.pop(key)[1]()


class UI:
    def __init__(self):
        self.answer = True
        self.save_path = "/tmp/out.zip"
        self.specs, self.success, self.errors, self.blocking, self.removed = [], [], [], [], []
        self.selected = []

    def list_updated(self): pass
    def row_updated(self, pid): pass
    def select_project(self, pid): self.selected.append(pid)
    def open_project(self, pid): pass
    def confirm(self, spec): self.specs.append(spec); return self.answer
    def ask_save_path(self, initial): return self.save_path
    def toast_success(self, text): self.success.append(text)
    def toast_info(self, text): pass
    def toast_error(self, text, details=""): self.errors.append(text)
    def show_blocking(self, text): self.blocking.append(text)
    def set_status(self, text): pass
    def project_removed(self, pid): self.removed.append(pid)
    def schedule(self, ms, fn): pass
    def is_visible(self): return True


def make(copied=None):
    bridge, widget, ui = Bridge(), Widget(), UI()
    ctl = ProjectsController(bridge, JobRunner(bridge, widget), ui,
                             copy_file=lambda a, b: (copied if copied is not None else []).append((a, b)))
    ctl.cards = [{"project_id": "p1", "title": "Сказка", "state": "ready", "busy": False}]
    ctl.projects_dir = "/data/projects"
    return ctl, bridge, widget, ui


def finish_job(bridge, widget, result):
    bridge.calls[-1][2]({"state": "done", "result": result})


class TestDelete(unittest.TestCase):
    def test_no_delete_without_confirmation(self):
        ctl, bridge, widget, ui = make()
        ui.answer = False
        ctl.delete("p1")
        bridge.last("projects_size")[2]({"size_bytes": 2048})
        self.assertEqual(len(ui.specs), 1)
        self.assertNotIn("projects_delete", bridge.names())

    def test_delete_only_after_confirm_with_flag(self):
        ctl, bridge, widget, ui = make()
        ctl.delete("p1")
        bridge.last("projects_size")[2]({"size_bytes": 2048})
        cap, payload, ok, err = bridge.last("projects_delete")
        self.assertEqual(payload, {"project_id": "p1", "confirm": True})
        ok({"job_id": "j1"})
        finish_job(bridge, widget, {"backup_size_bytes": 10})
        self.assertEqual(ui.removed, ["p1"])
        self.assertTrue(ui.success)

    def test_busy_project_is_not_deleted(self):
        ctl, bridge, widget, ui = make()
        ctl.cards[0]["busy"] = True
        ctl.delete("p1")
        self.assertEqual(bridge.calls, [])


class TestBackupsAndHints(unittest.TestCase):
    def test_backups_without_project_send_empty_for_project_id(self):
        ctl, bridge, widget, ui = make()
        ctl.backups_load("", lambda rows, err: None)
        self.assertEqual(bridge.last("projects_backups_list")[1], {"for_project_id": ""})

    def test_backups_for_project(self):
        ctl, bridge, widget, ui = make()
        ctl.backups_load("p1", lambda rows, err: None)
        self.assertEqual(bridge.last("projects_backups_list")[1], {"for_project_id": "p1"})

    def test_suggest_id_uses_id_hint(self):
        ctl, bridge, widget, ui = make()
        ctl.suggest_id("Зима", lambda pid: None)
        self.assertEqual(bridge.last("projects_suggest_id")[1], {"id_hint": "Зима"})

    def test_duplicate_uses_id_hint_and_blocks_second_job(self):
        ctl, bridge, widget, ui = make()
        done = []
        self.assertTrue(ctl.duplicate("p1", "копия", lambda *a: done.append(a)))
        self.assertEqual(bridge.last("projects_duplicate")[1], {"project_id": "p1", "id_hint": "копия"})
        self.assertFalse(ctl.duplicate("p1", "", lambda *a: None))

    def test_restore_needs_confirmation(self):
        ctl, bridge, widget, ui = make()
        ui.answer = False
        ctl.backup_restore({"project_id": "p1", "backup_id": "b1", "created_at": "2026-11-03T10:00:00"},
                           lambda ok, err: None)
        self.assertNotIn("projects_backup_restore", bridge.names())
        ui.answer = True
        ctl.backup_restore({"project_id": "p1", "backup_id": "b1", "created_at": "2026-11-03T10:00:00"},
                           lambda ok, err: None)
        self.assertEqual(bridge.last("projects_backup_restore")[1]["confirm"], True)


class TestExport(unittest.TestCase):
    def test_export_copies_zip(self):
        copied = []
        ctl, bridge, widget, ui = make(copied)
        ctl.export("p1")
        bridge.last("projects_export")[2]({"job_id": "j"})
        finish_job(bridge, widget, {"path": ".studio/exports/p1.zip"})
        self.assertEqual(copied, [("/data/projects/p1/.studio/exports/p1.zip", "/tmp/out.zip")])
        self.assertTrue(ui.success)

    def test_cancelled_dialog_starts_nothing(self):
        ctl, bridge, widget, ui = make()
        ui.save_path = ""
        ctl.export("p1")
        self.assertEqual(bridge.calls, [])


class TestJobRunner(unittest.TestCase):
    def run_job(self, final):
        bridge, widget = Bridge(), Widget()
        runner = JobRunner(bridge, widget)
        out = {}
        runner.start("projects_export", {}, project_id="p", on_done=lambda r: out.update(done=r),
                     on_error=lambda e: out.update(error=e))
        bridge.calls[-1][2]({"job_id": "j"})
        bridge.calls[-1][2](final)
        return runner, bridge, out

    def test_done(self):
        runner, bridge, out = self.run_job({"state": "done", "result": {"x": 1}})
        self.assertEqual(out["done"], {"x": 1})
        self.assertEqual(runner.active_count(), 0)

    def test_interrupted_is_error(self):
        runner, bridge, out = self.run_job({"state": "interrupted"})
        self.assertEqual(out["error"]["code"], "interrupted")

    def test_one_poll_per_job(self):
        bridge, widget = Bridge(), Widget()
        runner = JobRunner(bridge, widget)
        runner.start("x", {}, project_id="p")
        bridge.calls[-1][2]({"job_id": "j"})
        bridge.calls[-1][2]({"state": "running", "progress": {}})
        widget.fire()
        polls = [c for c in bridge.calls if c[0] == "projects_job_status"]
        self.assertEqual(len(polls), 2)
        self.assertEqual(len(widget.timers), 0)

    def test_same_project_second_start_refused(self):
        bridge, widget = Bridge(), Widget()
        runner = JobRunner(bridge, widget)
        self.assertTrue(runner.start("x", {}, project_id="p"))
        self.assertFalse(runner.start("x", {}, project_id="p"))


class TestPollerModes(unittest.TestCase):
    def make(self):
        bridge, widget = Bridge(), Widget()
        self.briefs, self.fulls = [], []
        poller = StatusPoller(bridge, widget, on_update=self.briefs.append, on_unavailable=lambda: None,
                              on_full=self.fulls.append)
        return poller, bridge, widget

    def test_full_mode_asks_project_status_without_second_timer(self):
        poller, bridge, widget = self.make()
        poller.start("p1")
        self.assertEqual(bridge.calls[-1][0], "project_status_brief")
        poller.set_mode("full")
        self.assertEqual(bridge.calls[-1][0], "project_status")
        bridge.calls[-1][2]({"project": {"id": "p1", "title": "Т"}, "active_run": {"state": "idle"}})
        self.assertEqual(len(self.fulls), 1)
        self.assertEqual(len(self.briefs), 1)
        self.assertLessEqual(len(widget.timers), 1)

    def test_mode_set_before_start_sends_no_brief_request(self):
        poller, bridge, widget = self.make()
        poller.set_mode("full")
        poller.start("p1")
        self.assertEqual([c[0] for c in bridge.calls], ["project_status"])

    def test_manual_refresh_forces_server_refresh(self):
        poller, bridge, widget = self.make()
        poller.start("p1")
        poller.set_mode("full")
        bridge.calls[-1][2]({"project": {"id": "p1"}, "active_run": {"state": "idle"}})
        poller.request_refresh(force=True)
        self.assertTrue(bridge.calls[-1][1].get("refresh"))


if __name__ == "__main__":
    unittest.main()
