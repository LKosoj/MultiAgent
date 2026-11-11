from StoryBookManager.core.studio_bridge import StudioBridge


class Widget:
    def after(self, *_args):
        pass

    def winfo_exists(self):
        return True


def test_bridge_captures_series_for_film_calls_and_not_project_calls():
    bridge = StudioBridge(Widget())
    bridge.set_series("world", "episode_a")
    assert bridge.scoped_payload("project_status", {"project_id": "world"})["series_id"] == "episode_a"
    assert "series_id" not in bridge.scoped_payload("projects_get", {"project_id": "world"})
    assert bridge.scoped_payload("project_status", {"project_id": "other"}) == {"project_id": "other"}
    bridge.close()


def test_bridge_discards_old_series_response(monkeypatch):
    import StoryBookManager.core.studio_bridge as module
    import threading
    entered, release = threading.Event(), threading.Event()
    def invoke(_name, payload):
        entered.set(); release.wait(2)
        return {"status": "success", "series_id": payload["series_id"]}
    monkeypatch.setattr(module, "invoke", invoke)
    bridge = StudioBridge(Widget())
    bridge.set_series("world", "episode_a")
    seen = []
    bridge.call("project_status", {"project_id": "world"}, on_success=seen.append)
    assert entered.wait(2)
    bridge.set_series("world", "episode_b")
    bridge.set_series("world", "episode_a")
    release.set()
    bridge._executor.shutdown(wait=True)
    bridge._drain()
    assert seen == []


def test_selected_clip_shows_only_intersecting_speech(tmp_path):
    import json
    from StoryBookManager.gui.shots.panel import ShotsPanel
    def put(name, data):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    put("97_shots/video_plan.json", {"decisions": [{"shot_keys": ["s01_sh01"], "reason": "visible_speech", "method": "references"}]})
    put("97_shots/shots.json", {"items": [{"scene_number": 1, "shot_number": 1, "start_s": 2, "end_s": 4}]})
    put("98_audio/speech.json", {"lines": [{"id": "a", "start": 1, "end": 2}, {"id": "b", "start": 2, "end": 3}, {"id": "c", "start": 4, "end": 5}]})
    class Panel:
        def _resolve(self, rel): return str(tmp_path / rel)
    result = ShotsPanel._shot_speech(Panel(), {"shot_key": "s01_sh01", "scene": 1, "shot": 1})
    assert [line["id"] for line in result["lines"]] == ["b"]
    assert result["decision"]["reason"] == "visible_speech"


def test_two_series_in_real_tk_window():
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    import pytest
    if not shutil.which("xvfb-run"):
        pytest.skip("xvfb-run is unavailable")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run(["xvfb-run", "-a", sys.executable, str(root / "tests/helpers/storybook_series_tk_smoke.py")], cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Two-series Tk check passed" in result.stdout


def test_series_screen_discards_project_and_selection_roundtrip():
    from types import SimpleNamespace
    from StoryBookManager.gui.sections.series_screen import SeriesScreen

    class Bridge:
        def __init__(self): self.calls = []
        def call(self, name, payload, **callbacks): self.calls.append(callbacks)
    class Screen:
        call = SeriesScreen.call
        def selected(self): return {"series_id": self.sid} if self.sid else None
        def _selection_changed(self): pass
    screen = Screen()
    screen.bridge = Bridge(); screen.project = SimpleNamespace(project_id="a")
    screen.sid = "one"; screen._project_generation = 1; screen._selection_generation = 1
    seen = []
    screen.call("projects_get", {}, seen.append)
    screen.call("speech_get", {"series_id": "one"}, seen.append)
    screen._project_generation += 2
    screen._selection_generation += 2
    for pending in screen.bridge.calls: pending["on_success"]({"old": True})
    assert seen == []
    screen.call("speech_get", {"series_id": "one"}, seen.append)
    screen.bridge.calls[-1]["on_success"]({"new": True})
    assert seen == [{"new": True}]


def test_clip_uses_plan_schedule_for_offscreen_and_narrator(tmp_path):
    import json
    from StoryBookManager.gui.shots.panel import ShotsPanel
    def put(name, data):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    put("97_shots/video_plan.json", {"shot_intervals": [{"shot_key": "1-2", "start_s": 10, "end_s": 13,
        "speaker_schedule": [{"line_id": "off", "speaker_id": "hero", "kind": "off_screen", "start": 0, "end": 1},
                             {"line_id": "narrator", "speaker_id": "reader", "kind": "narrator", "start": 2, "end": 3}]}]})
    put("98_audio/speech.json", {"lines": [{"id": "off", "text": "За кадром", "start": 9, "end": 11},
                                            {"id": "narrator", "text": "Рассказчик", "start": 12, "end": 14},
                                            {"id": "other", "text": "Другой кадр", "start": 13, "end": 14}]})
    class Panel:
        def _resolve(self, rel): return str(tmp_path / rel)
    result = ShotsPanel._shot_speech(Panel(), {"shot_key": "1-2", "scene": 1, "shot": 2})
    assert [line["id"] for line in result["lines"]] == ["off", "narrator"]


def test_project_entry_points_open_world_form():
    from StoryBookManager.gui.sections import projects_logic
    from StoryBookManager.gui.sections.projects_screen import ProjectsScreen
    class Screen:
        def create_by_form(self): self.opened = True
    screen = Screen()
    ProjectsScreen.create_new_project(screen)
    assert screen.opened
    assert projects_logic.list_state(loading=False, error=None, total=0, shown=0, query="").actions == ("form",)
    assert "промпт" not in projects_logic.TXT_EMPTY.lower()


def test_project_row_uses_series_count_without_old_stage():
    from StoryBookManager.gui.sections.projects_logic import row_for
    row = row_for({"project_id": "world", "title": "Мир", "series_count": 2, "state": "ready", "stage": {"id": "project", "index": 0}}, None)
    assert row.series == "2"
