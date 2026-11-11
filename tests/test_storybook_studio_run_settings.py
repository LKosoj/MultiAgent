"""Настройки прогона studio: схема, проверки, сохранение, блокировка во время прогона."""

import json
import threading

import pytest

from tests.studio_run_fakes import make_series_project
from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.project_paths import acquire_storybook_project_lock, release_storybook_project_lock
from custom_tools.storybook.studio import run_settings as rs
from custom_tools.storybook.studio.capabilities import invoke


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "STORYBOOK_STYLE_PROFILE", "ROUTERAI_VIDEO_MODEL", "ROUTERAI_RESOLUTION",
                 "ROUTERAI_VIDEO_RETRY_CYCLES", "ROUTERAI_VIDEO_FALLBACK_MODEL", "SUNO_COOKIE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m0")
    monkeypatch.setenv("SUNO_COOKIE", "c")
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    base = make_series_project(tmp_path)
    with series_scope("demo", "main"):
        yield base


def ok(result):
    assert result["status"] == "success", result
    return result


def err(result, code):
    assert result["status"] == "error" and result["error"]["code"] == code, result
    return result["error"]


def codes(issues, level=None):
    return {i["code"] for i in issues if level is None or i["level"] == level}


def settings(**patch):
    base = rs.default_settings()
    for key, value in patch.items():
        if isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def test_capabilities_registered_with_expected_access():
    from custom_tools.storybook.studio import capabilities as caps
    reg = caps.registry()
    expected = {"run_settings_get": "read", "run_settings_save": "write",
                "provider_catalog": "read", "style_catalog": "read", "run_preflight_summary": "read"}
    assert {k: reg[k].access for k in expected} == expected
    assert "run_settings_validate" not in reg and "cost_estimate" not in reg
    assert reg["provider_catalog"].project_scoped is False and reg["style_catalog"].project_scoped is False


def test_get_for_project_without_file(project, monkeypatch):
    monkeypatch.setenv("STORYBOOK_VIDEO_PROVIDER", "aitunnel")
    monkeypatch.setenv("ROUTERAI_RESOLUTION", "720p")
    result = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    assert result["settings"] == rs.default_settings()
    eff = result["effective"]
    assert eff["video.provider"] == {"value": "aitunnel", "source": "env"}
    assert eff["audio.music"] == {"value": True, "source": "default"}
    assert eff["budget_rub"]["value"] == 0
    assert result["run_active"] is False and len(result["digest"]) == 64
    assert not (project / ".studio").exists()  # чтение ничего не создаёт


def test_env_defaults_and_null_means_default(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "m1")
    monkeypatch.setenv("ROUTERAI_VIDEO_RETRY_CYCLES", "3")
    eff = rs.effective(rs.default_settings(), "demo")
    assert eff["video.model"] == {"value": "m1", "source": "env"}
    assert eff["video.retry_cycles"] == {"value": 3, "source": "env"}
    eff = rs.effective(settings(video={"model": "own", "retry_cycles": 0}), "demo")
    assert eff["video.model"] == {"value": "own", "source": "setting"}
    assert eff["video.retry_cycles"] == {"value": 0, "source": "setting"}


def test_broken_json_is_reported_not_raised(project):
    (project / ".studio").mkdir()
    (project / ".studio" / "run_settings.json").write_text("{ oops")
    result = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    assert result["settings"] == rs.default_settings()
    assert "settings_unreadable" in codes(result["issues"], "warning")


@pytest.mark.parametrize("patch,code", [
    ({"budget_rub": True}, "invalid_type"),
    ({"budget_rub": -1}, "out_of_range"),
    ({"budget_rub": 1.234}, "out_of_range"),
    ({"budget_rub": "10"}, "invalid_type"),
    ({"video": {"retry_cycles": 6}}, "out_of_range"),
    ({"video": {"retry_cycles": True}}, "invalid_type"),
    ({"video": {"allowed_durations": [0, 5]}}, "out_of_range"),
    ({"video": {"allowed_durations": [5, 5]}}, "out_of_range"),
    ({"video": {"allowed_durations": ["5"]}}, "invalid_type"),
    ({"video": {"resolution": "big"}}, "invalid_type"),
    ({"video": {"model": "has space"}}, "invalid_type"),
    ({"video": {"provider": "kling"}}, "unknown_provider"),
    ({"style": {"profile": "nope"}}, "unknown_profile"),
    ({"style": {"preset": "nope"}}, "unknown_preset"),
    ({"audio": {"music": "yes"}}, "invalid_type"),
    ({"advanced": {"sample_shot_key": "abc"}}, "invalid_type"),
])
def test_validate_errors(project, patch, code):
    issues = rs.validate(settings(**patch), "demo")
    assert code in codes(issues, "error"), issues


def test_unknown_field_is_error_on_save_but_warning_on_read(project):
    bad = settings()
    bad["surprise"] = 1
    bad["video"]["extra"] = 2
    assert codes(rs.validate(bad, "demo", strict=True), "error") == {"unknown_field"}
    assert codes(rs.validate(bad, "demo", strict=False), "warning") >= {"unknown_field"}
    err(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": bad}), "invalid_argument")


@pytest.mark.parametrize("path", ["../outside.mp3", "/etc/passwd.mp3", "98_audio/missing.mp3", "98_audio/voice.txt"])
def test_narration_path_checks(project, path):
    (project / "98_audio").mkdir()
    (project / "98_audio" / "voice.txt").write_text("x")
    assert "narration_path_invalid" in codes(rs.validate(settings(audio={"narration_path": path}), "demo"), "error")


def test_narration_symlink_outside_is_rejected(project, tmp_path):
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    (project / "98_audio").mkdir()
    (project / "98_audio" / "link.mp3").symlink_to(outside)
    assert "narration_path_invalid" in codes(rs.validate(settings(audio={"narration_path": "98_audio/link.mp3"}), "demo"), "error")


def test_narration_inside_project_is_ok_and_becomes_absolute(project):
    (project / "98_audio").mkdir()
    (project / "98_audio" / "voice.mp3").write_bytes(b"x")
    s = settings(audio={"narration_path": "98_audio/voice.mp3"})
    assert "narration_path_invalid" not in codes(rs.validate(s, "demo"))
    assert rs.settings_to_inputs("demo", s)["narration_path"] == str((project / "98_audio" / "voice.mp3").resolve())


def test_budget_rules(project, monkeypatch):
    assert "budget_price_unknown" in codes(rs.validate(settings(budget_rub=100), "demo"), "error")
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    assert "budget_price_unknown" not in codes(rs.validate(settings(budget_rub=100), "demo"))
    issues = rs.validate(settings(budget_rub=100, video={"provider": "veo"}), "demo")
    assert "budget_unsupported_provider" in codes(issues, "error")


def test_limited_provider_warns_and_refuses_sample(project):
    issues = rs.validate(settings(video={"provider": "minimax"}, advanced={"sample_before_batch": True}), "demo")
    assert "provider_limited" in codes(issues, "warning")
    assert "provider_option_unsupported" in codes(issues, "error")


def test_environment_problems_block_run_not_save(project, monkeypatch):
    monkeypatch.delenv("ROUTERAI_API_KEY")
    monkeypatch.delenv("SUNO_COOKIE")
    issues = rs.validate(settings(), "demo")
    run_only = [i for i in issues if i["code"] in {"video_provider_not_configured", "music_cookie_missing"}]
    assert {i["code"] for i in run_only} == {"video_provider_not_configured", "music_cookie_missing"}
    assert all(i["scope"] == "run" for i in run_only)
    ok(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings()}))
    with pytest.raises(Exception):
        rs.settings_to_inputs("demo")  # запуск с такими настройками блокируется


def test_save_roundtrip_atomic_and_digest(project):
    first = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    s = settings(video={"provider": "aitunnel", "retry_cycles": 2}, budget_rub=None)
    saved = ok(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": s, "expected_digest": first["digest"]}))
    path = project / ".studio" / "run_settings.json"
    assert json.loads(path.read_text()) == saved["settings"]
    assert saved["settings"]["video"]["provider"] == "aitunnel"
    again = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    assert again["digest"] == saved["digest"] != first["digest"]
    assert not list((project / ".studio").glob("*.tmp"))


def test_save_conflict_on_stale_digest(project):
    first = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    ok(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(budget_rub=None, video={"retry_cycles": 1}),
                                    "expected_digest": first["digest"]}))
    err(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(video={"retry_cycles": 2}),
                                     "expected_digest": first["digest"]}), "conflict")
    err(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(video={"retry_cycles": 2})}), "conflict")


def test_parallel_saves_leave_valid_file(project):
    start = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))["digest"]
    barrier = threading.Barrier(2)
    results = []

    def worker(n):
        barrier.wait(timeout=5)
        results.append(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "expected_digest": start,
                                                    "settings": settings(video={"retry_cycles": n})}))

    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["status"] for r in results) == ["error", "success"]
    data = json.loads((project / ".studio" / "run_settings.json").read_text())
    assert data["video"]["retry_cycles"] in (1, 2)


def test_during_run_only_budget_can_change(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_COST_RUB", "10")
    first = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    saved = ok(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(budget_rub=50),
                                            "expected_digest": first["digest"]}))
    handle = acquire_storybook_project_lock("demo")
    try:
        assert ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))["run_active"] is True
        err(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(budget_rub=50, video={"retry_cycles": 1}),
                                         "expected_digest": saved["digest"]}), "locked")
        changed = ok(invoke("run_settings_save", {"project_id": "demo", "series_id": "main", "settings": settings(budget_rub=80),
                                                  "expected_digest": saved["digest"]}))
        assert changed["settings"]["budget_rub"] == 80
    finally:
        release_storybook_project_lock(handle)


def test_unknown_project_is_not_found():
    err(invoke("run_settings_get", {"project_id": "no_such", "series_id": "main"}), "not_found")


def test_style_catalog_and_provider_catalog_capabilities(project):
    styles = ok(invoke("style_catalog", {}))
    assert styles["profiles"][0]["id"] == "default" and styles["profiles"][0]["presets"]
    catalog = ok(invoke("provider_catalog", {"for_project_id": "demo"}))
    assert [p["id"] for p in catalog["providers"]][:4] == ["routerai", "aitunnel", "minimax", "veo"]
    assert catalog["selected_provider"] == "routerai"


@pytest.mark.parametrize("bad", [5, ["routerai"], {"a": 1}, True])
def test_non_string_provider_gives_invalid_type_and_get_survives(project, bad):
    (project / ".studio").mkdir()
    (project / ".studio" / "run_settings.json").write_text(json.dumps({"video": {"provider": bad}}))
    result = ok(invoke("run_settings_get", {"project_id": "demo", "series_id": "main"}))
    assert result["effective"]["video.provider"]["value"] == "routerai"
    assert any(i["code"] == "invalid_type" and i["field"] == "video.provider" for i in result["issues"])
    assert any(i["field"] == "video.provider" for i in rs.validate(settings(video={"provider": bad}), "demo"))


def test_preflight_summary_uses_unsaved_provider_for_readiness(project, monkeypatch):
    seen = {}
    from custom_tools.storybook import video_contract

    def fake(project_id, **kwargs):
        seen.update(kwargs)
        return {"ready": True, "blocking_reasons": [], "warnings": [], "errors": [], "video": {}}

    monkeypatch.setattr(video_contract, "storybook_video_music_readiness", fake)
    ok(invoke("run_preflight_summary", {"project_id": "demo", "series_id": "main", "settings": settings(video={"provider": "aitunnel"})}))
    assert seen["video_provider"] == "aitunnel"
