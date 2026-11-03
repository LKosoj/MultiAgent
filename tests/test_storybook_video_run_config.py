"""Настройки видео-прогона: порядок источников и устойчивость чтения."""

import json
import threading

import pytest

from custom_tools.storybook import video_run_config as cfg


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "ROUTERAI_VIDEO_MODEL", "ROUTERAI_RESOLUTION", "AITUNNEL_VIDEO_MODEL"):
        monkeypatch.delenv(name, raising=False)
    cfg.bind_run_project(None)
    (tmp_path / "demo").mkdir()
    yield tmp_path / "demo"
    cfg.bind_run_project(None)


def write(project, data):
    target = project / ".studio" / "run_settings.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_provider_order_input_file_env_default(isolated, monkeypatch):
    assert cfg.effective_provider("demo") == "routerai"
    monkeypatch.setenv("STORYBOOK_VIDEO_PROVIDER", "Veo")
    assert cfg.effective_provider("demo") == "veo"
    write(isolated, {"video": {"provider": "aitunnel"}})
    assert cfg.effective_provider("demo") == "aitunnel"
    assert cfg.effective_provider("demo", explicit="minimax") == "minimax"


def test_model_and_resolution_come_only_from_selected_provider(isolated, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "env-model")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "tunnel-env")
    write(isolated, {"video": {"provider": "aitunnel", "model": "file-model", "resolution": "720p"}})
    assert cfg.video_setting("VIDEO_MODEL", "aitunnel", "demo") == "file-model"
    # настройки файла не просачиваются в другого провайдера
    assert cfg.video_setting("VIDEO_MODEL", "routerai", "demo") == "env-model"
    assert cfg.video_setting("RESOLUTION", "routerai", "demo") == ""


def test_broken_file_does_not_break(isolated, monkeypatch):
    write(isolated, "{ not json")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "env-model")
    assert cfg.read_stored("demo") == {}
    assert cfg.effective_provider("demo") == "routerai"
    assert cfg.video_setting("VIDEO_MODEL", "routerai", "demo") == "env-model"
    write(isolated, "[1, 2]")
    assert cfg.read_stored("demo") == {}


def test_unknown_or_unsafe_project_is_ignored(isolated):
    assert cfg.read_stored("../etc") == {}
    assert cfg.read_stored("") == {}
    assert cfg.read_stored(None) == {}


def test_bind_run_project_is_used_when_project_not_given(isolated):
    write(isolated, {"video": {"provider": "veo"}})
    assert cfg.effective_provider() == "routerai"
    cfg.bind_run_project("demo")
    assert cfg.bound_project() == "demo"
    assert cfg.effective_provider() == "veo"
    cfg.bind_run_project(None)
    assert cfg.bound_project() is None


def test_tool_names():
    assert cfg.tool_name_for("routerai") == "video_generator_routerai_tool"
    assert cfg.tool_name_for(" VEO ") == "video_generator_veo_tool"
    assert cfg.tool_name_for("kling") is None
    assert cfg.tool_name_for(None) is None


def test_budget_is_read_fresh_and_ignores_bool(isolated):
    assert cfg.stored_budget_rub_at(isolated) is None
    write(isolated, {"budget_rub": 50})
    assert cfg.stored_budget_rub_at(isolated) == 50.0
    write(isolated, {"budget_rub": 75.5})
    assert cfg.stored_budget_rub_at(isolated) == 75.5
    write(isolated, {"budget_rub": True})
    assert cfg.stored_budget_rub_at(isolated) is None


def test_budget_fallback_set_and_clear():
    cfg.set_run_budget_fallback("demo", 40)
    assert cfg.budget_fallback("demo") == 40.0
    cfg.set_run_budget_fallback("demo", 0)
    assert cfg.budget_fallback("demo") == 0.0
    cfg.set_run_budget_fallback("demo", "мусор")
    assert cfg.budget_fallback("demo") == 0.0


def test_parallel_reads_are_consistent(isolated):
    write(isolated, {"video": {"provider": "aitunnel"}})
    seen, errors = [], []

    def worker():
        try:
            for _ in range(50):
                seen.append(cfg.effective_provider("demo"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert set(seen) == {"aitunnel"}
