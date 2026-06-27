import json
from pathlib import Path

from custom_tools.storybook import video_contract


def _write_shots(root: Path, project_id: str, items):
    shots_path = root / "plots" / "storybooks" / project_id / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True, exist_ok=True)
    shots_path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return shots_path


def _enable_video_env(monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "video-model")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_preflight_writes_provider_menu_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(
        tmp_path,
        "project-1",
        [
            {"video_prompt": "move camera", "video_path": "out/shot-1.mp4"},
            {"video_prompt": "", "video_path": "out/shot-2.mp4"},
        ],
    )

    result = video_contract.storybook_video_preflight_tool(
        session_id="session-1",
        project_id="project-1",
        language="ru",
        enable="true",
    )

    artifact = tmp_path / result["artifact_path"]
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["status"] == "success"
    assert saved["provider"] == "aitunnel"
    assert saved["generation_enabled"] is True
    assert saved["items_total"] == 2
    assert saved["expected_video_count"] == 1
    assert saved["capabilities"]["video"] is True
    assert saved["capabilities"]["render"] is True


def test_delivery_promise_blocks_enabled_run_without_capabilities_or_clips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in [
        "AITUNNEL_API_KEY",
        "AITUNNEL_VIDEO_MODEL",
        "MINIMAX_API_KEY",
        "KLING_API_KEY",
        "KLING_API_SECRET_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GEMINI_API_KEY",
        "STORYBOOK_VIDEO_PROVIDER",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(video_contract.shutil, "which", lambda _name: None)

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    artifact = tmp_path / result["artifact_path"]
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["status"] == "error"
    assert saved["will_generate_video"] is False
    assert saved["expected_video_count"] == 0
    assert "video_provider_unavailable" in saved["blocking_reasons"]
    assert "render_capability_unavailable" in saved["blocking_reasons"]
    assert "no_expected_video_clips" in saved["blocking_reasons"]


def test_delivery_promise_promises_video_when_enabled_and_ready(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(
        tmp_path,
        "project-1",
        [
            {"scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": "video/1.mp4"},
            {"scene_number": 1, "shot_number": 2, "video_prompt": "   ", "video_path": "video/2.mp4"},
        ],
    )

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["status"] == "success"
    assert result["will_generate_video"] is True
    assert result["expected_video_count"] == 1
    assert result["expected_outputs"] == ["video/1.mp4"]
    assert result["blocking_reasons"] == []


def test_preflight_rejects_project_id_path_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    try:
        video_contract.storybook_video_preflight_tool(
            session_id="session-1",
            project_id="../outside",
        )
    except ValueError as exc:
        assert "escapes storybook root" in str(exc)
    else:
        raise AssertionError("project_id path traversal must be rejected")

    assert not (tmp_path / "plots" / "outside").exists()


def test_preflight_can_load_provider_env_from_repo_env_file(tmp_path, monkeypatch):
    fake_root = tmp_path
    fake_module = fake_root / "custom_tools" / "storybook" / "video_contract.py"
    fake_module.parent.mkdir(parents=True)
    (fake_root / ".env").write_text(
        "AITUNNEL_API_KEY=from-env-file\nAITUNNEL_VIDEO_MODEL=video-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(video_contract, "__file__", str(fake_module))
    monkeypatch.delenv("AITUNNEL_API_KEY", raising=False)
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)

    video_contract._load_env_file()

    provider, _providers = video_contract._select_provider()
    assert provider == "aitunnel"


def test_delivery_promise_disabled_run_is_explicit_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_contract.shutil, "which", lambda _name: None)

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=False,
    )

    assert result["status"] == "skipped"
    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == []
    assert "disabled" in result["delivery_promise"]


def test_decision_log_defaults_to_unapproved_and_can_be_approved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(
        tmp_path,
        "project-1",
        [{"video_prompt": "pan right", "video_path": "video/1.mp4"}],
    )
    video_contract.storybook_video_preflight_tool("session-1", "project-1")
    video_contract.storybook_video_delivery_promise_tool("session-1", "project-1")

    result = video_contract.storybook_video_decision_log_tool(
        session_id="session-1",
        project_id="project-1",
    )
    approved = video_contract.storybook_video_decision_log_tool(
        session_id="session-1",
        project_id="project-1",
        approved=True,
    )

    assert result["approved"] is False
    assert all(decision["approved"] is False for decision in result["decisions"])
    assert approved["approved"] is True
    assert all(decision["approved"] is True for decision in approved["decisions"])
    assert result["final_video_status"] == "missing"
