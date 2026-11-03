"""Обёртка вызовов студии для экранов (без дисплея): ошибка при отсутствующей возможности, передача ключа."""

from StoryBookManager.gui.screens import calls


class Bridge:
    def __init__(self):
        self.seen = []

    def call(self, cap, payload, on_success=None, on_error=None, latest_only_key=None):
        self.seen.append((cap, payload, latest_only_key))
        return "token"


def test_known_capability_goes_to_bridge_with_key():
    bridge = Bridge()
    out = calls.StudioCalls(bridge).call(calls.CAP_MEDIA_TIMELINE, {"project_id": "p"}, key="film_timeline")
    assert out == "token"
    assert bridge.seen == [("media_timeline", {"project_id": "p"}, "film_timeline")]


def test_missing_capability_reports_unavailable_without_bridge(monkeypatch):
    monkeypatch.setattr(calls.capabilities, "find", lambda cap_id: None)
    bridge, errors = Bridge(), []
    out = calls.StudioCalls(bridge).call("нет_такой", {}, on_error=errors.append)
    assert out is None and bridge.seen == []
    assert errors[0]["error"]["code"] == "unavailable"


def test_all_cap_ids_are_distinct_strings():
    ids = [v for k, v in vars(calls).items() if k.startswith("CAP_")]
    assert len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids)
