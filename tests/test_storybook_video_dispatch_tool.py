"""Диспетчер видеопровайдера: выбор, отказ вместо молчаливой потери параметров, бюджет."""

import json
import sys
import types

import pytest

from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool as dispatch

FULL = ("session_id, project_id=None, items=None, max_concurrency=None, enable=False, seed=None, language='en', "
        "force_update_prompts=False, skip_prompt_enhancement=False, sample_before_batch=False, sample_shot_key=None, "
        "generate_blockout=False, use_blockout_reference=False, shot_keys=None, retry_cycles=None, fallback_model=None")
LIMITED = ("session_id, project_id=None, items=None, max_concurrency=None, enable=False, seed=None, language='en', "
           "force_update_prompts=False, skip_prompt_enhancement=False")


def make_tool(monkeypatch, tool_name, signature, calls):
    namespace = {"calls": calls}
    exec(f"def {tool_name}({signature}):\n    calls.append(dict(locals()))\n    return {{'status': 'success', 'results': []}}", namespace)
    module = types.ModuleType(f"custom_tools.storybook.{tool_name}")
    setattr(module, tool_name, namespace[tool_name])
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("STORYBOOK_VIDEO_PROVIDER", raising=False)
    (tmp_path / "demo").mkdir()
    cfg.bind_run_project(None)
    cfg.set_run_budget_fallback("demo", 0)
    calls = {name: [] for name in ("routerai", "aitunnel", "veo", "minimax")}
    make_tool(monkeypatch, "video_generator_routerai_tool", FULL, calls["routerai"])
    make_tool(monkeypatch, "video_generator_aitunnel_tool", FULL, calls["aitunnel"])
    make_tool(monkeypatch, "video_generator_veo_tool", LIMITED, calls["veo"])
    make_tool(monkeypatch, "video_generator_mm_tool", LIMITED, calls["minimax"])
    yield tmp_path / "demo", calls
    cfg.bind_run_project(None)
    cfg.set_run_budget_fallback("demo", 0)


def write_settings(project, data):
    (project / ".studio").mkdir(exist_ok=True)
    (project / ".studio" / "run_settings.json").write_text(json.dumps(data))


def test_default_provider_is_routerai_with_same_arguments(env):
    _, calls = env
    result = dispatch(session_id="s", project_id="demo", enable=True, seed=7, shot_keys=["1-1"], retry_cycles=2)
    assert result["status"] == "success" and result["provider"] == "routerai" and result["dispatch"] is True
    assert len(calls["routerai"]) == 1 and not calls["aitunnel"]
    got = calls["routerai"][0]
    assert (got["session_id"], got["project_id"], got["enable"], got["seed"]) == ("s", "demo", True, 7)
    assert got["shot_keys"] == ["1-1"] and got["retry_cycles"] == 2
    assert "max_concurrency" in got and got["max_concurrency"] is None


def test_max_concurrency_is_passed_only_when_given(env):
    _, calls = env
    dispatch(session_id="s", project_id="demo", max_concurrency=4)
    assert calls["routerai"][0]["max_concurrency"] == 4


@pytest.mark.parametrize("name", ["aitunnel", "veo", "minimax"])
def test_each_provider_gets_its_own_function(env, name):
    _, calls = env
    result = dispatch(session_id="s", project_id="demo", video_provider=name, enable=True)
    assert result["provider"] == name
    assert len(calls[name]) == 1
    assert sum(len(v) for v in calls.values()) == 1


def test_unknown_provider_is_refused_without_call(env):
    _, calls = env
    result = dispatch(session_id="s", project_id="demo", video_provider="kling")
    assert result["status"] == "error" and result["code"] == "unknown_provider"
    assert not any(calls.values())


def test_veo_refuses_sample_before_batch_and_shot_keys(env):
    _, calls = env
    first = dispatch(session_id="s", project_id="demo", video_provider="veo", sample_before_batch=True)
    second = dispatch(session_id="s", project_id="demo", video_provider="veo", shot_keys=["1-1"])
    for result in (first, second):
        assert result["status"] == "error" and result["code"] == "provider_option_unsupported"
    assert not calls["veo"]


def test_veo_defaults_are_dropped_silently(env):
    _, calls = env
    result = dispatch(session_id="s", project_id="demo", video_provider="veo", shot_keys=None,
                      sample_before_batch=False, generate_blockout=True, use_blockout_reference=False)
    assert result["status"] == "success"
    assert "shot_keys" not in calls["veo"][0] and "sample_before_batch" not in calls["veo"][0]


def test_veo_refuses_blockout_reference(env):
    _, calls = env
    result = dispatch(session_id="s", project_id="demo", video_provider="veo",
                      generate_blockout=True, use_blockout_reference=True)
    assert result["code"] == "provider_option_unsupported" and not calls["veo"]


def test_budget_with_veo_is_refused_before_call(env):
    project, calls = env
    result = dispatch(session_id="s", project_id="demo", video_provider="veo", budget_rub=100)
    assert result["code"] == "budget_unsupported_provider" and not calls["veo"]
    write_settings(project, {"video": {"provider": "minimax"}, "budget_rub": 10})
    assert dispatch(session_id="s", project_id="demo")["code"] == "budget_unsupported_provider"
    assert not calls["minimax"]


def test_project_settings_choose_provider_and_input_wins(env):
    project, calls = env
    write_settings(project, {"video": {"provider": "aitunnel"}})
    assert dispatch(session_id="s", project_id="demo")["provider"] == "aitunnel"
    assert dispatch(session_id="s", project_id="demo", video_provider="routerai")["provider"] == "routerai"
    assert len(calls["aitunnel"]) == 1 and len(calls["routerai"]) == 1


def test_env_provider_is_used_without_file(env, monkeypatch):
    _, calls = env
    monkeypatch.setenv("STORYBOOK_VIDEO_PROVIDER", "aitunnel")
    assert dispatch(session_id="s", project_id="demo")["provider"] == "aitunnel"


def test_budget_fallback_is_registered_for_budget_providers(env):
    dispatch(session_id="s", project_id="demo", budget_rub=33)
    assert cfg.budget_fallback("demo") == 33.0


def test_target_result_is_not_changed_except_two_keys(env, monkeypatch):
    calls = []
    make_tool(monkeypatch, "video_generator_routerai_tool", FULL, calls)
    result = dispatch(session_id="s", project_id="demo")
    assert result == {"status": "success", "results": [], "provider": "routerai", "dispatch": True}
