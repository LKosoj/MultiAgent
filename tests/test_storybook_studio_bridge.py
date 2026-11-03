import threading
import time

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.errors import success
from StoryBookManager.core import studio_bridge


class FakeWidget:
    """Виджет без дисплея: after складывает вызовы, pump() гоняет их в главном потоке."""

    def __init__(self):
        self.alive = True
        self.queue = []

    def after(self, _ms, func):
        self.queue.append(func)

    def winfo_exists(self):
        return self.alive

    def pump(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            while self.queue:
                self.queue.pop(0)()
            if not self.queue:
                return
            time.sleep(0.01)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(caps, "_REGISTRY", {})
    monkeypatch.setattr(caps, "_LOADED", True)

    @caps.capability("echo", title="x", access="read", project_scoped=False)
    def echo(value: str = "", delay: float = 0.0) -> dict:
        time.sleep(delay)
        return success(value=value)

    @caps.capability("boom", title="x", access="read", project_scoped=False)
    def boom() -> dict:
        raise RuntimeError("x")


def wait_for(widget, cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        widget.pump(0.05)
        time.sleep(0.01)
    return cond()


def test_callback_runs_in_main_thread(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    seen = []
    bridge.call("echo", {"value": "a"}, on_success=lambda r: seen.append((r["value"], threading.current_thread())))
    assert wait_for(widget, lambda: seen)
    assert seen[0][0] == "a" and seen[0][1] is threading.main_thread()
    bridge.close()


def test_latest_only_key_drops_stale(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    seen = []
    bridge.call("echo", {"value": "old", "delay": 0.3}, on_success=lambda r: seen.append(r["value"]), latest_only_key="s")
    bridge.call("echo", {"value": "new"}, on_success=lambda r: seen.append(r["value"]), latest_only_key="s")
    assert wait_for(widget, lambda: "new" in seen)
    time.sleep(0.5)
    widget.pump(0.2)
    assert seen == ["new"]
    bridge.close()


def test_errors_go_to_on_error_and_bridge_survives(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    errors, ok = [], []
    bridge.call("boom", on_error=errors.append)
    bridge.call("nope", on_error=errors.append)
    bridge.call("echo", {"value": "z"}, on_success=ok.append, on_error=errors.append)
    bridge.call("boom")  # без on_error: только лог
    assert wait_for(widget, lambda: len(errors) == 2 and ok)
    assert [e["error"]["code"] for e in errors] == ["internal", "unknown_capability"] or \
        {e["error"]["code"] for e in errors} == {"internal", "unknown_capability"}
    bridge.close()


def test_callback_exception_does_not_break_bridge(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    ok = []

    def bad(_r):
        raise RuntimeError("колбэк упал")

    bridge.call("echo", on_success=bad)
    bridge.call("echo", {"value": "after"}, on_success=ok.append)
    assert wait_for(widget, lambda: ok)
    bridge.close()


def test_dead_widget_drops_results(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    seen = []
    bridge.call("echo", {"delay": 0.2}, on_success=seen.append)
    widget.alive = False
    time.sleep(0.4)
    widget.pump(0.2)
    assert seen == []
    bridge.close()


def test_close_stops_polling(isolated):
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    seen = []
    bridge.call("echo", {"delay": 0.2}, on_success=seen.append)
    bridge.close()
    time.sleep(0.4)
    widget.pump(0.2)
    assert seen == []


def test_tk_handlers_cover_registry(isolated):
    handlers = studio_bridge.tk_handlers()
    assert set(handlers) == {cap.tk_handler for cap in caps.registry().values()}
    widget = FakeWidget()
    bridge = studio_bridge.StudioBridge(widget)
    seen = []
    handlers["echo"](bridge, {"value": "h"}, on_success=seen.append)
    assert wait_for(widget, lambda: seen)
    bridge.close()
