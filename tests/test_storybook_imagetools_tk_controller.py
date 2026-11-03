"""Контроллер правки картинки ИИ с подставным мостом: порядок вызовов и реакции на ошибки (без дисплея)."""

from StoryBookManager.gui.imagetools.edit_controller import EditController
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT
from StoryBookManager.gui.sections.job_runner import JobRunner

TARGET = {"path": "97_shots/s/a.png", "name": "a.png", "shot_key": "1-1", "part": "start"}
PREVIEW = {"plan_digest": "d1", "estimate": {"cost_rub": 0.0, "unknown_cost": True}, "can_start": True}


class Widget:
    """after() копит таймеры: тест сам решает, когда сработает очередной опрос."""

    def __init__(self):
        self.timers = []

    def after(self, ms, fn):
        self.timers.append(fn)
        return len(self.timers)

    def after_cancel(self, _id):
        pass

    def fire(self):
        timers, self.timers = self.timers, []
        for fn in timers:
            fn()


class Bridge:
    def __init__(self, answers):
        self.answers = answers   # cap -> ответ или список ответов по очереди
        self.calls = []

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
        self.calls.append((cap, payload))
        answer = self.answers[cap]
        if isinstance(answer, list):
            answer = answer.pop(0)
        (on_success if answer.get("status") == "success" else on_error)(answer)


class Notify:
    def __init__(self):
        self.log = []

    def __getattr__(self, kind):
        return lambda text: self.log.append((kind, text))


def err(code, reason=None, message="м", **details):
    return {"status": "error", "error": {"code": code, "message": message,
                                         "details": {"reason": reason, **details} if reason else {}}}


def make(answers, project="p"):
    bridge, widget, notify = Bridge(answers), Widget(), Notify()
    ctl = EditController(bridge, JobRunner(bridge, widget), notify, lambda: project)
    return ctl, bridge, widget, notify


def run_start(ctl, **kw):
    seen = {"started": 0, "done": [], "fail": [], "status": []}
    ctl.start(TARGET, "сделай небо", [], PREVIEW, on_started=lambda: seen.__setitem__("started", seen["started"] + 1),
              on_fail=seen["fail"].append, on_done=seen["done"].append, on_status=seen["status"].append)
    return seen


def test_preview_payload_and_stale_answer_dropped():
    pending = []

    class Slow(Bridge):
        def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
            self.calls.append((cap, payload))
            pending.append(on_success)

    bridge = Slow({})
    ctl = EditController(bridge, JobRunner(bridge, Widget()), Notify(), lambda: "p")
    got = []
    ctl.preview(TARGET, ["r.png"], got.append, print)
    ctl.preview(TARGET, [], got.append, print)
    pending[0]({"status": "success", "n": 1})
    pending[1]({"status": "success", "n": 2})
    assert [g["n"] for g in got] == [2]
    assert bridge.calls[0] == ("image_edit_preview", {"project_id": "p", "shot_key": "1-1", "part": "start",
                                                      "reference_paths": ["r.png"]})
    assert "reference_paths" not in bridge.calls[1][1]


def test_references_exclude_target():
    items = [{"path": TARGET["path"], "name": "a.png", "servable": True},
             {"path": "x/b.png", "name": "b.png", "servable": True}]
    ctl, bridge, _, _ = make({"media_inventory": {"status": "success", "items": items}})
    got = []
    ctl.load_references(TARGET["path"], got.append, print)
    assert got == [[{"path": "x/b.png", "label": "x/b.png"}]]
    assert bridge.calls[0][1] == {"project_id": "p", "kinds": ["image"], "limit": 500, "probe_limit": 0}


def test_start_then_poll_until_done():
    answers = {"image_edit_start": {"status": "success", "job_id": "j1"},
               "projects_job_status": [{"status": "success", "state": "running"},
                                       {"status": "success", "state": "done", "result": {"path": "x", "modified_ts": 2}}]}
    ctl, bridge, widget, notify = make(answers)
    seen = run_start(ctl)
    assert [c for c, _ in bridge.calls] == ["image_edit_start", "projects_job_status"]
    assert seen["started"] == 1 and seen["status"] == [IT["edit_running"]] and seen["done"] == []
    assert ctl.is_running()
    widget.fire()
    assert seen["done"] == [{"path": "x", "modified_ts": 2}] and seen["status"][-1] == ""
    assert not ctl.is_running()
    assert ("success", IT["edit_done"]) in notify.log
    start = bridge.calls[0][1]
    assert start["surface"] == "tk" and start["confirm_unknown_cost"] is True and start["expected_plan_digest"] == "d1"


def test_job_error_shows_toast_and_keeps_old_picture():
    answers = {"image_edit_start": {"status": "success", "job_id": "j1"},
               "projects_job_status": [{"status": "success", "state": "error",
                                        "error": {"code": "unavailable", "message": "секрет"}}]}
    ctl, _, _, notify = make(answers)
    seen = run_start(ctl)
    assert seen["done"] == [] and seen["status"][-1] == ""
    assert ("error", IT["edit_failed"]) in notify.log and not any("секрет" in t for _, t in notify.log)
    assert not ctl.is_running()


def test_start_errors():
    cases = [(err("conflict", "plan_changed"), "refresh", IT["edit_plan_changed"]),
             (err("conflict", "cost_changed"), "refresh", IT["edit_cost_changed"]),
             (err("conflict", "cost_confirmation_required"), "refresh", IT["edit_cost_changed"]),
             (err("conflict", "cost_confirmation_required", unknown_cost=True), "refresh", IT["edit_confirm_unknown"]),
             (err("locked"), "failed", IT["edit_busy"]),
             (err("conflict", "regeneration_pending"), "failed", IT["edit_busy"]),
             (err("conflict", "edit_running"), "failed", IT["edit_busy"]),
             (err("invalid_argument"), "failed", IT["edit_failed"])]
    for answer, kind, text in cases:
        ctl, _, _, notify = make({"image_edit_start": answer})
        seen = run_start(ctl)
        assert seen["fail"] == [{"kind": kind, "text": text}], answer
        assert seen["started"] == 0 and not ctl.is_running() and notify.log == []


def test_project_is_pinned_at_dialog_open_for_preview_references_and_start():
    current = {"id": "old"}
    answers = {"media_inventory": {"status": "success", "items": []},
               "image_edit_preview": {"status": "success", "plan_digest": "d"},
               "image_edit_start": {"status": "success", "job_id": "j1"},
               "projects_job_status": {"status": "success", "state": "running"}}
    bridge, widget = Bridge(answers), Widget()
    ctl = EditController(bridge, JobRunner(bridge, widget), Notify(), lambda: current["id"])
    current["id"] = "new"   # проект переключили, пока диалог был открыт
    ctl.load_references(TARGET["path"], print, print, project_id="old")
    ctl.preview(TARGET, [], print, print, project_id="old")
    ctl.start(TARGET, "сделай небо", [], PREVIEW, on_started=lambda: None, on_fail=print, on_done=print,
              on_status=print, project_id="old")
    projects = [p["project_id"] for c, p in bridge.calls if c != "projects_job_status"]
    assert projects == ["old", "old", "old"]
    assert ctl.runner.is_busy("old") and not ctl.runner.is_busy("new")


def test_job_errors_not_found_and_interrupted_are_generic_failure():
    for code in ("not_found", "interrupted"):
        answers = {"image_edit_start": {"status": "success", "job_id": "j1"},
                   "projects_job_status": [{"status": "success", "state": "error",
                                            "error": {"code": code, "message": "м"}}]}
        ctl, _, _, notify = make(answers)
        run_start(ctl)
        assert ("error", IT["edit_failed"]) in notify.log, code
    answers = {"image_edit_start": {"status": "success", "job_id": "j1"},
               "projects_job_status": [{"status": "success", "state": "interrupted"}]}
    ctl, _, _, notify = make(answers)
    run_start(ctl)
    assert ("error", IT["edit_failed"]) in notify.log


def test_second_start_while_running_is_refused():
    ctl, bridge, _, _ = make({"image_edit_start": {"status": "success", "job_id": "j1"},
                              "projects_job_status": {"status": "success", "state": "running"}})
    run_start(ctl)
    seen = run_start(ctl)
    assert seen["fail"] == [{"kind": "failed", "text": IT["edit_busy"]}]
    assert [c for c, _ in bridge.calls].count("image_edit_start") == 1
