"""Логика экрана «Генерация» (Tk) без окон: фейковый мост, ручной таймер."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from StoryBookManager.gui.generation import model  # noqa: E402
from StoryBookManager.gui.generation.run_controller import GenerationController  # noqa: E402
from StoryBookManager.gui.shell.run_status import build_run_view  # noqa: E402

SETTINGS = {"video": {"provider": "demo", "model": "demo-1"}, "style": {}, "audio": {}, "budget_rub": 1000,
            "advanced": {}}
EST = {"total_known_rub": 100.0, "complete": True, "budget_verdict": "enough", "components": []}
PLAN = {"can_start": True, "plan_digest": "d1", "problems": [], "steps_to_run": ["a"], "skip_steps": [],
        "estimate": EST, "inputs": {"language": "ru"}}


class Bridge:
    def __init__(self):
        self.calls = []
        self.state = "idle"
        self.plan = copy.deepcopy(PLAN)
        self.fail_save = False

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
        self.calls.append((cap, payload))
        answers = {
            "run_settings_get": {"status": "ok", "settings": SETTINGS, "effective": {}, "defaults": SETTINGS,
                                 "digest": "s", "issues": []},
            "run_settings_save": {"status": "ok", "settings": SETTINGS, "digest": "s2", "issues": []},
            "provider_catalog": {"status": "ok", "providers": [{"id": "demo", "title": "Д", "credentials_found": True,
                                                                  "models": []}]},
            "run_plan": {"status": "ok", "plan": self.plan},
            "run_preflight_summary": {"status": "ok", "estimate": EST},
            "run_status": {"status": "ok", "state": self.state, "resumable": self.state in ("failed", "interrupted"), "engine": {"available": True}},
        }
        res = answers.get(cap, {"status": "ok"})
        if cap == "run_settings_save" and self.fail_save:
            res = {"status": "error", "error": {"code": "invalid", "message": "нельзя", "details": {}}}
        (on_success if res["status"] == "ok" else on_error)(res)


class View:
    def __init__(self):
        self.confirms = []

    def render(self, *what):
        pass

    def ask_confirm(self, spec):
        self.confirms.append(spec.title)
        return True

    def ask_choice(self, *a, **k):
        return "go"

    def show_tab(self, name):
        pass

    def focus_budget(self):
        pass

    def log_wanted(self):
        return False


class Notify:
    def __getattr__(self, name):
        return lambda *a, **k: None


def make(state="idle"):
    bridge, view = Bridge(), View()
    bridge.state = state
    ctl = GenerationController(bridge, view, Notify(), lambda ms, fn: None if ms >= 2000 else fn(), lambda h: None, lambda: "p1")
    ctl.set_project("p1")
    ctl.on_show()
    return ctl, bridge, view


def start_payloads(bridge):
    return [p for c, p in bridge.calls if c == "run_start"]


def test_discard_only_for_full_over_unfinished():
    ctl, bridge, view = make("failed")
    assert ctl.selection.mode == "resume"   # после сбоя по умолчанию продолжаем
    ctl.set_mode("full")
    ctl.request_run()
    assert start_payloads(bridge)[0].get("discard_unfinished") is True
    assert "Начать прогон сначала?" in view.confirms
    ctl2, bridge2, _ = make("failed")
    ctl2.request_run()
    assert "discard_unfinished" not in start_payloads(bridge2)[0]
    ctl3, bridge3, view3 = make("idle")
    ctl3.request_run()
    assert "discard_unfinished" not in start_payloads(bridge3)[0]
    assert "Начать прогон сначала?" not in view3.confirms


def test_changed_digest_rereads_plan_and_blocks_start():
    ctl, bridge, _ = make()
    bridge.plan = {**PLAN, "plan_digest": "d2"}
    before = sum(1 for c, _ in bridge.calls if c == "run_plan")
    ctl.request_run()
    assert not start_payloads(bridge)
    assert sum(1 for c, _ in bridge.calls if c == "run_plan") > before + 1   # план перечитан


def test_dirty_settings_saved_before_start():
    ctl, bridge, _ = make()
    ctl.edit_budget("2000")
    ctl.request_run()
    caps = [c for c, _ in bridge.calls]
    assert "run_settings_save" in caps
    assert caps.index("run_settings_save") < caps.index("run_start")


def test_resume_intent_waits_for_first_status():
    ctl, _, _ = make()
    ctl.status = None
    ctl.apply_intent(mode="resume")
    assert ctl.selection.mode != "resume" and ctl._pending_intent
    ctl._on_status({"state": "interrupted", "resumable": True})
    assert ctl.selection.mode == "resume"


def test_button_off_while_plan_busy():
    ctl, _, _ = make()
    ctl.plan_busy = True
    assert not ctl.start_button().enabled


def test_unresponsive_banner_above_other_surface():
    status = {"state": "running", "unresponsive": True, "other_surface": {"surface": "react", "text": "x"}}
    assert model.banner_for(status, {})["kind"] == "unresponsive"


def test_rerun_resumable_rule():
    run = {"state": "failed", "text": "t", "failed_step": "a"}
    assert build_run_view({"run": run}).rerun_mode == "resume"
    assert build_run_view({"run": {"state": "failed", "text": "t"}}).rerun_mode == "full"
    assert build_run_view({"run": {"state": "interrupted", "text": "t"}}).rerun_mode == "resume"
    assert build_run_view({"run": {"state": "running", "text": "t"}}).rerun_enabled is False


def test_skip_confirm_does_not_stick_after_failed_save():
    ctl, bridge, view = make("failed")
    ctl.set_mode("full")
    ctl.edit_budget("2000")
    bridge.fail_save = True
    ctl.request_run(skip_confirm=True)      # сохранение упало: запуска нет
    assert not start_payloads(bridge)
    bridge.fail_save = False
    ctl.request_run()                        # обычный запуск обязан снова спросить
    assert "Начать прогон сначала?" in view.confirms
    assert start_payloads(bridge)
