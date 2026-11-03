"""Контроллер экрана «Кадры» (Tk) на фейковых мосте и окне, без tkinter."""

import unittest

from StoryBookManager.gui.shots.controller import ShotsController


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.handlers = {}

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
        self.calls.append((cap, payload, on_success, on_error))

    def answer(self, cap, result, ok=True):
        for i, (c, p, s, e) in enumerate(self.calls):
            if c == cap:
                self.calls.pop(i)
                (s if ok else e)(result)
                return p
        raise AssertionError(f"нет вызова {cap}: {[c[0] for c in self.calls]}")

    def names(self):
        return [c[0] for c in self.calls]


class FakeView:
    def __init__(self):
        self.form = ({}, "")
        self.unsaved_choice = "cancel"
        self.conflict_choice = "refresh"
        self.restore_ok = True
        self.refreshed = []
        self.regen_keys = None

    def refresh(self, what):
        self.refreshed.append(what)

    def read_form(self):
        return self.form

    def is_visible(self):
        return True

    def ask_unsaved(self, what):
        return self.unsaved_choice

    def ask_conflict(self, body):
        return self.conflict_choice

    def confirm_restore(self, number):
        return self.restore_ok

    def open_regen(self, keys):
        self.regen_keys = keys


class FakeNotify:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        return lambda msg, *a, **k: self.log.append((name, msg))


def row(key, status="pending"):
    return {"shot_key": key, "review": {"status": status}, "state": "ready"}


def make():
    bridge, view, notify = FakeBridge(), FakeView(), FakeNotify()
    timers = []
    ctl = ShotsController(bridge, view, notify, lambda ms, fn: timers.append((ms, fn)), lambda: "p1")
    return ctl, bridge, view, notify, timers


def list_result(keys, total=None):
    return {"items": [row(k) for k in keys], "total": total if total is not None else len(keys),
            "revision": "r1", "baseline": {"missing": 0, "outdated": 0}}


class TestLoad(unittest.TestCase):
    def test_show_loads_first_page_and_selects_first(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        self.assertEqual(ctl.load_state, "loading")
        p = bridge.answer("shots_list", list_result(["s01_sh01", "s01_sh02"]))
        self.assertEqual(p["limit"], 100)
        self.assertEqual(ctl.load_state, "ready")
        self.assertEqual(ctl.selected, "s01_sh01")
        self.assertIn("shot_get", bridge.names())

    def test_empty_project(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        bridge.answer("shots_list", list_result([]))
        self.assertEqual(ctl.load_state, "empty")

    def test_error_state(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        bridge.answer("shots_list", {"status": "error", "error": {"code": "internal", "message": "x"}}, ok=False)
        self.assertEqual(ctl.load_state, "error")

    def test_stale_answer_dropped_after_project_change(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        ctl.set_project("p2")
        bridge.answer("shots_list", list_result(["a"]))
        self.assertEqual(ctl.items, [])

    def test_next_page_loaded_until_total(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        bridge.answer("shots_list", list_result([f"k{i}" for i in range(100)], total=150))
        self.assertEqual(len(ctl.items), 100)
        ctl.maybe_load_more(99)
        p = bridge.answer("shots_list", list_result([f"m{i}" for i in range(50)], total=150))
        self.assertEqual(p["offset"], 100)
        self.assertEqual(len(ctl.items), 150)


class TestReview(unittest.TestCase):
    def setUp(self):
        self.ctl, self.bridge, self.view, self.notify, _ = make()
        self.ctl.on_show()
        self.bridge.answer("shots_list", list_result(["k1", "k2"]))
        self.bridge.answer("shot_get", {"shot": {"digest": "d1", "start": {}, "end": {}, "video": {}}})
        self.bridge.answer("review_get", {"shots": {"k1": {"status": "pending", "entry_digest": "e1", "note": ""}}})

    def test_letter_a_sets_approved_with_expected_digest(self):
        self.ctl.set_review_letter("a")
        p = self.bridge.answer("review_set_status", {"entries": {"k1": "e2"}})
        self.assertEqual(p["keys"], ["k1"])
        self.assertEqual(p["status"], "approved")
        self.assertEqual(p["surface"], "tk")
        self.assertEqual(p["expected_digests"], {"k1": "e1"})
        self.assertEqual(self.ctl.entry["entry_digest"], "e2")

    def test_marked_shots_win_over_current(self):
        self.ctl.toggle_mark("k2")
        self.ctl.set_review_letter("x")
        p = self.bridge.answer("review_set_status", {})
        self.assertEqual(p["keys"], ["k2"])
        self.assertEqual(p["status"], "rejected")

    def test_unsaved_blocks_selection_change(self):
        self.view.form = ({}, "заметка")
        self.ctl.select("k2", immediate=True)
        self.assertEqual(self.ctl.selected, "k1")


class TestSave(unittest.TestCase):
    def setUp(self):
        self.ctl, self.bridge, self.view, self.notify, _ = make()
        self.ctl.on_show()
        self.bridge.answer("shots_list", list_result(["k1"]))
        card = {"digest": "d1", "start": {"prompt": "a"}, "end": {"prompt": "b"}, "video": {"prompt": "c"},
                "editable": {"fields": ["start.prompt"], "locked": False}}
        self.bridge.answer("shot_get", {"shot": card})
        self.bridge.answer("review_get", {"shots": {"k1": {"status": "pending", "entry_digest": "e1", "note": ""}}})

    def test_note_only_does_not_call_shot_update(self):
        self.view.form = (self.ctl_form(), "новая")
        self.ctl.save()
        self.assertNotIn("shot_update", self.bridge.names())
        p = self.bridge.answer("review_set_note", {"entry": {"entry_digest": "e2"}})
        self.assertEqual(p["note"], "новая")
        self.assertEqual(p["expected_digest"], "e1")

    def ctl_form(self):
        from StoryBookManager.gui.shots import model as M
        return M.form_from_card(self.ctl.card)

    def test_failed_shot_update_skips_note(self):
        form = self.ctl_form()
        form = {**form, "start.prompt": "изменено"} if "start.prompt" in form else form
        self.view.form = (form, "новая")
        self.ctl.save()
        if "shot_update" in self.bridge.names():
            self.bridge.answer("shot_update", {"status": "error", "error": {"code": "locked", "message": "m"}}, ok=False)
            self.assertNotIn("review_set_note", self.bridge.names())

    def test_nothing_to_save_is_ok(self):
        done = []
        self.view.form = (self.ctl_form(), "")
        self.ctl.save(then=lambda: done.append(1))
        self.assertEqual(done, [1])
        self.assertNotIn("review_set_note", self.bridge.names())


class TestRegen(unittest.TestCase):
    def test_busy_run_blocks_dialog(self):
        ctl, bridge, view, notify, _ = make()
        ctl.on_show()
        bridge.answer("shots_list", list_result(["k1"]))
        ctl.run_state = "running"
        ctl.open_regen()
        self.assertIsNone(view.regen_keys)
        self.assertEqual(notify.log[-1][0], "info")

    def test_preview_stale_answer_dropped(self):
        ctl, bridge, *_ = make()
        ctl.on_show()
        bridge.answer("shots_list", list_result(["k1"]))
        got = []
        ctl.regen_preview(["k1"], "all", got.append, lambda m: None)
        ctl.regen_preview(["k1"], "all", got.append, lambda m: None)
        calls = [c for c in bridge.calls if c[0] == "regenerate_preview"]
        calls[0][2]({"n": 1})
        calls[1][2]({"n": 2})
        self.assertEqual(got, [{"n": 2}])


if __name__ == "__main__":
    unittest.main()


class TestNewBehaviour(unittest.TestCase):
    def loaded(self, keys=("k1", "k2")):
        ctl, bridge, view, notify, _ = make()
        ctl.on_show()
        bridge.answer("shots_list", list_result(list(keys)))
        return ctl, bridge, view, notify

    def test_restore_blocked_while_run_busy(self):
        ctl, bridge, view, _ = self.loaded()
        ctl.run_state = "running"
        asked = []
        view.confirm_restore = lambda n: asked.append(n) or True
        ctl.restore("v1", 1)
        self.assertEqual(asked, [])
        self.assertNotIn("versions_restore", bridge.names())

    def test_unsaved_what_names_selected_shot(self):
        ctl, *_ = self.loaded()
        self.assertEqual(ctl.unsaved_what(), "Шот k1")

    def test_select_without_ask_does_not_open_dialog(self):
        ctl, bridge, view, _ = self.loaded()
        bridge.answer("shot_get", {"shot": {"digest": "d", "start": {}, "end": {}, "video": {}}})
        view.form = ({}, "правка")
        asked = []
        view.ask_unsaved = lambda what: asked.append(what) or "stay"
        ctl.select("k2", immediate=True, force=True, ask=False)
        self.assertEqual(asked, [])

    def test_locked_error_prefers_concrete_reason(self):
        from StoryBookManager.gui.shots import texts as T
        self.assertEqual(T.error_text("locked", "run_active", "Идёт прогон"), "Идёт прогон")
        self.assertEqual(T.error_text("locked", None, "что-то"), T.PROJECT_LOCKED_TEXT)

    def test_quiet_reload_not_broken_by_load_more(self):
        ctl, bridge, *_ = self.loaded()
        ctl.total = 500
        ctl.reload(quiet=True)
        before = list(bridge.names())
        ctl.maybe_load_more(10 ** 6)
        self.assertEqual(bridge.names(), before)
        bridge.answer("shots_list", list_result(["k1", "k2"], total=2))
        self.assertEqual(ctl.load_state, "ready")
