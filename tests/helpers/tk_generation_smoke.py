"""Дымовая проверка экрана «Генерация» под Xvfb (отдельный процесс).

    xvfb-run -a -s "-screen 0 1700x1000x24" python3 tests/helpers/tk_generation_smoke.py \
        --width 1280 --height 800 --shots /tmp/shots

Вместо сервера подставлен фейковый мост: платных вызовов и реальных запусков нет.
Последняя строка вывода - JSON-отчёт (SMOKE_REPORT).
"""

import argparse
import copy
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

from tk_shell_smoke import _make_projects, _pump, find_clipped, shoot  # noqa: E402

PID = "demo_one"

SETTINGS = {
    "video": {"provider": "demo", "model": "demo-1", "fallback_model": None, "retry_cycles": 2,
              "resolution": None, "allowed_durations": None},
    "style": {"profile": None, "preset": None},
    "audio": {"music": True, "sfx": True, "subtitles": True, "narration_path": None},
    "budget_rub": 1500,
    "advanced": {"force_update_prompts": False, "skip_prompt_enhancement": False,
                 "sample_before_batch": False, "sample_shot_key": None, "final_allow_missing_audio": False},
}
STEPS = [{"id": f"step_{i}", "title": t} for i, t in enumerate(
    ("Сценарий", "Раскадровка", "Картинки", "Видео", "Озвучка", "Музыка", "Монтаж"), 1)]
ESTIMATE = {"total_known_rub": 1180.0, "complete": True, "budget_verdict": "enough", "components": [
    {"id": "video", "amount_rub": 900.0, "basis": "exact"}, {"id": "images", "amount_rub": 250.0, "basis": "rough"},
    {"id": "music", "amount_rub": 30.0, "basis": "exact"}]}
PLAN = {"can_start": True, "plan_digest": "d1", "problems": [], "steps_to_run": [s["id"] for s in STEPS],
        "skip_steps": [], "estimate": ESTIMATE, "inputs": {"language": "ru", "generate_end_shots": True}}


class FakeBridge:
    def __init__(self):
        self.state = "idle"
        self.calls = []

    def status(self):
        steps = [{"id": s["id"], "status": "completed" if i < 3 else "running" if i == 3 else "pending",
                  "duration_s": 40 if i < 3 else None} for i, s in enumerate(STEPS)]
        busy = self.state == "running"
        return {"status": "ok", "state": self.state, "locked": busy, "steps": steps if busy else [],
                "run": {"run_id": "r1"} if self.state != "idle" else None, "resumable": True,
                "progress": {"step_id": "step_4", "step_index": 4, "step_total": 7, "percent": 52} if busy else {},
                "video": {"done": 3, "total": 8, "failed": 0} if busy else {},
                "cost": {"spent_rub": 410, "budget_rub": 1500} if busy else {},
                "eta": {"seconds": 1500, "basis": "history"} if busy else None, "engine": {"available": True}}

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
        self.calls.append(cap)
        answers = {
            "run_settings_get": {"status": "ok", "settings": SETTINGS, "effective": {}, "defaults": SETTINGS,
                                 "digest": "s1", "run_active": False, "issues": []},
            "provider_catalog": {"status": "ok", "selected_provider": "demo", "providers": [
                {"id": "demo", "title": "Демо-провайдер", "selectable": True, "credentials_found": True,
                 "models": [{"id": "demo-1", "supported_resolutions": ["720p", "1080p"], "supported_durations": [4, 6, 8]}]}]},
            "style_catalog": {"status": "ok", "profiles": [{"id": "p1", "presets": [{"id": "a", "title": "Акварель"}]}]},
            "run_steps_get": {"status": "ok", "steps": STEPS},
            "run_history": {"status": "ok", "runs": [{"run_id": "r0", "mode": "full", "surface": "tk",
                                                       "started_at": "2026-11-02T10:00:00", "finished_at": "2026-11-02T10:42:00",
                                                       "outcome": "completed"}]},
            "run_plan": {"status": "ok", "plan": copy.deepcopy(PLAN)},
            "run_preflight_summary": {"status": "ok", "estimate": ESTIMATE, "readiness": {}},
            "run_status": self.status(),
            "run_log_tail": self._log_tail(payload),
            "run_start": {"status": "ok"},
        }
        result = answers.get(cap, {"status": "ok"})
        if cap == "run_start":
            self.state = "running"
        if result.get("status") == "ok":
            if on_success:
                on_success(result)
        elif on_error:
            on_error(result)

    def _log_tail(self, payload):
        """Как настоящий мост: с after_offset отдаёт только новое, иначе журнал дублировался бы."""
        lines = ["10:00:01 INFO старт", "10:00:02 WARNING мало места", "10:00:03 ERROR сбой шота"]
        start = (payload or {}).get("after_offset")
        return {"status": "ok", "lines": lines[int(start):] if start else lines, "offset": len(lines), "truncated": False}

    def close(self):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--shots", default="")
    ap.add_argument("--tag", default="generation")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sbm_gen_"))
    projects = tmp / "projects"
    projects.mkdir()
    _make_projects(projects)
    os.environ["STORYBOOK_PROJECTS_DIR"] = str(projects)

    import tkinter as tk
    from StoryBookManager.config.settings import app_settings
    # настоящий settings.json не трогаем: настройки пишутся во временную папку
    app_settings.config_file = tmp / "settings.json"
    app_settings.set("projects_directory", str(projects))
    app_settings.set("backup_directory", str(tmp / "backups"))
    app_settings.set("logs_directory", str(tmp / "logs"))
    app_settings.set("window_geometry", f"{args.width}x{args.height}")
    app_settings.set("ui_scale", 100)
    app_settings.set("auto_save_interval", 0)

    report = {"size": f"{args.width}x{args.height}", "errors": [], "clipped": {}, "checks": {}}
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    from StoryBookManager.gui.main_window import MainWindow
    root = tk.Tk()
    app = None
    try:
        app = MainWindow(root)
        root.geometry(f"{args.width}x{args.height}+0+0")
        root.update()
        fake = FakeBridge()
        screen = app.generation
        screen.ctl.bridge = fake
        screen.ctl.poller.bridge = fake
        # окна подтверждения не открываем: отвечаем «да»
        asked = []
        screen.ask_confirm = lambda spec: asked.append(spec.title) or True
        screen.ask_choice = lambda *a, **k: "go"

        def snap(name):
            _pump(root, 0.4)
            report["clipped"][name] = find_clipped(root)
            if shots:
                shoot(root, shots / f"{args.tag}_{args.width}x{args.height}_{name}.png")

        app.on_project_selected(app.project_manager.load_project(PID))
        app.show_section("generation")
        _pump(root, 1.5)
        ctl = screen.ctl
        report["checks"]["loaded"] = [ctl.load_state, ctl.start_button().enabled, ctl.start_button().label]
        snap("ready")
        screen.show_tab("summary")
        snap("summary")
        screen.show_tab("settings")
        ctl.apply_intent(mode="from_step", step_id="step_3")
        _pump(root, 1)
        report["checks"]["from_step"] = [ctl.selection.mode, ctl.selection.step_id, ctl.start_button().enabled]
        snap("from_step")
        ctl.apply_intent(mode="full")
        _pump(root, 1)
        screen.request_run()
        _pump(root, 2.5)
        report["checks"]["started"] = [ctl.run_state, "run_start" in fake.calls, asked]
        screen.show_tab("progress")
        snap("running")
        screen.show_tab("log")
        _pump(root, 2.5)
        report["checks"]["log_lines"] = len(ctl.log.lines)
        snap("log")
        ctl.apply_intent(shot_keys=["1-1", "1-2"])
        report["checks"]["regen_mode"] = [ctl.selection.mode, list(ctl.selection.shot_keys)]
        _pump(root, 1)
        snap("regen")
    except Exception:
        report["errors"].append(traceback.format_exc())
    finally:
        try:
            if app is not None:
                app._shutdown_shell()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
    print("SMOKE_REPORT " + json.dumps(report, ensure_ascii=False, default=str))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
