"""Каталог провайдеров видео: без сети, с подменой сети, без утечки секретов."""

import json

import pytest

from custom_tools.storybook import video_contract
from custom_tools.storybook import video_run_config as cfg
from custom_tools.storybook.studio import provider_catalog as pc


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "demo").mkdir()
    for name in ("STORYBOOK_VIDEO_PROVIDER", "ROUTERAI_API_KEY", "ROUTERAI_VIDEO_MODEL", "AITUNNEL_API_KEY",
                 "AITUNNEL_VIDEO_MODEL", "MINIMAX_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from custom_tools.storybook import video_generator_common as common
    # кэши каталогов могут остаться от других тестов процесса
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None, raising=False)
    monkeypatch.setattr(common, "_AITUNNEL_MODELS_CACHE", None, raising=False)
    cfg.bind_run_project(None)
    yield
    cfg.bind_run_project(None)


def by_id(catalog):
    return {p["id"]: p for p in catalog["providers"]}


def test_no_network_without_refresh(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("сеть без refresh_remote")
    monkeypatch.setattr(pc, "_fetch_remote", boom)
    catalog = pc.build_catalog()
    assert [p["id"] for p in catalog["providers"]] == ["routerai", "aitunnel", "minimax", "veo", "kling"]
    assert {p["catalog_status"] for p in catalog["providers"]} == {"static_fallback"}
    assert catalog["selected_provider"] is None


def test_kling_is_not_selectable():
    info = by_id(pc.build_catalog())
    assert info["kling"]["selectable"] is False
    assert {k for k, v in info.items() if v["selectable"]} == {"routerai", "aitunnel", "minimax", "veo"}


def test_mocked_network_marks_models(monkeypatch):
    remote = {"routerai": {"vendor/model-x": {"supported_durations": [5, 10]}}}
    monkeypatch.setattr(pc, "_fetch_remote", lambda ids: (remote, []))
    info = by_id(pc.build_catalog(refresh_remote=True))["routerai"]
    assert info["catalog_status"] == "network"
    model = next(m for m in info["models"] if m["id"] == "vendor/model-x")
    assert model["source"] == "network"
    assert by_id(pc.build_catalog(refresh_remote=True))["aitunnel"]["catalog_status"] == "static_fallback"


def test_network_failure_falls_back_with_warning(monkeypatch):
    def boom(ids):
        raise OSError("down")
    monkeypatch.setattr(pc, "_fetch_remote", boom)
    catalog = pc.build_catalog(refresh_remote=True)
    assert [w["code"] for w in catalog["warnings"]] == ["remote_catalog_unavailable"]
    assert by_id(catalog)["routerai"]["catalog_status"] == "static_fallback"


def test_secrets_never_leak(monkeypatch):
    for name in ("ROUTERAI_API_KEY", "AITUNNEL_API_KEY", "MINIMAX_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "SECRET-VALUE-123")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m")
    catalog = pc.build_catalog(project_id="demo")
    text = json.dumps(catalog, ensure_ascii=False)
    assert "SECRET-VALUE-123" not in text

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)
    bad = [k for k in keys(catalog) if k.lower().startswith(("api_key", "token", "secret", "password"))]
    assert bad == []
    assert by_id(catalog)["routerai"]["credentials_found"] is True


def test_missing_env_names_listed():
    info = by_id(pc.build_catalog(project_id="demo"))
    assert info["routerai"]["credentials_found"] is False
    assert "ROUTERAI_API_KEY" in info["routerai"]["missing_env"]


def test_limitations_from_signatures():
    support, labels, _ = pc.limitations_for("video_generator_routerai_tool")
    assert support == "full" and labels == []
    support, labels, _ = pc.limitations_for("video_generator_mm_tool")
    assert support == "limited" and labels


def test_limitations_for_patched_veo_with_shot_keys(monkeypatch):
    monkeypatch.setattr(pc, "_params_from_source", lambda tool: {"session_id", "project_id", "shot_keys"})
    monkeypatch.setattr(pc.importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError("x")), raising=False)
    support, labels, error = pc.limitations_for("video_generator_veo_tool")
    assert support == "limited"
    assert labels  # у veo без нужных параметров есть ограничения


def test_consistency_with_video_contract_select_provider(monkeypatch):
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m")
    contract = {p["name"]: p["configured"] for p in video_contract._select_provider("demo", "")[1]}
    catalog = by_id(pc.build_catalog(project_id="demo"))
    for name in ("routerai", "aitunnel", "minimax"):
        assert catalog[name]["credentials_found"] == contract[name], name
    assert catalog["routerai"]["credentials_found"] is True


def test_selected_provider_follows_project_file(tmp_path):
    (tmp_path / "demo" / ".studio").mkdir()
    (tmp_path / "demo" / ".studio" / "run_settings.json").write_text(json.dumps({"video": {"provider": "aitunnel"}}))
    assert pc.build_catalog(project_id="demo")["selected_provider"] == "aitunnel"
