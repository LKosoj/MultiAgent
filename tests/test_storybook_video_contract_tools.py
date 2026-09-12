import json
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import video_contract
from custom_tools.storybook import video_generator_aitunnel_tool as aitunnel_module
from custom_tools.storybook.video_generator_aitunnel_jobs import _build_input_hash, _hash_text


def _write_shots(root: Path, project_id: str, items):
    shots_path = root / "plots" / "storybooks" / project_id / "97_shots" / "shots.json"
    shots_path.parent.mkdir(parents=True, exist_ok=True)
    shots_path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return shots_path


def _enable_video_env(monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "video-model")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")


def _aitunnel_catalog(*, frame_images=("first_frame", "last_frame"), sizes=("1920x1080",), durations=(6,)):
    return {
        "video-model": {
            "supported_frame_images": list(frame_images),
            "supported_sizes": list(sizes),
            "supported_durations": list(durations),
            "supports_seed": True,
        }
    }


def _write_frame(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"frame")


class _AitunnelResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = ""

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


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


def test_preflight_keeps_suno_separate_from_audio_capability(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    monkeypatch.setenv("SUNO_COOKIE", "__client=sk-suno")

    result = video_contract.storybook_video_preflight_tool(
        session_id="session-1",
        project_id="project-1",
        language="ru",
        enable="true",
    )

    artifact = tmp_path / result["artifact_path"]
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["capabilities"]["audio"] is False
    assert "SUNO_COOKIE" not in saved["capability_details"]["audio_env"]

    readiness = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
    )
    assert readiness["capabilities"]["music"] is True
    assert readiness["music"]["configured"] is True


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
    start_image = tmp_path / "images" / "start.png"
    _write_frame(start_image)
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: _aitunnel_catalog())
    _write_shots(
        tmp_path,
        "project-1",
        [
            {
                "scene_number": 1,
                "shot_number": 1,
                "video_prompt": "pan right",
                "video_path": "video/1.mp4",
                "start_image": str(start_image),
                "width": 1920,
                "height": 1080,
                "timing": "00:00 - 00:06",
            },
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


def test_delivery_promise_blocks_missing_start_frame_after_artist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(
        tmp_path,
        "project-1",
        [
            {
                "scene_number": 1,
                "shot_number": 1,
                "video_prompt": "pan right",
                "video_path": str(tmp_path / "video" / "1.mp4"),
                "start_image": str(tmp_path / "images" / "missing.png"),
                "width": 1920,
                "height": 1080,
                "timing": "00:00 - 00:06",
            }
        ],
    )
    monkeypatch.setattr(
        video_contract,
        "_get_aitunnel_video_models",
        lambda: _aitunnel_catalog(),
        raising=False,
    )

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == ["video_input_invalid:1-1"]


def test_delivery_promise_blocks_unsupported_last_frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    start_image = tmp_path / "images" / "start.png"
    end_image = tmp_path / "images" / "end.png"
    _write_frame(start_image)
    _write_frame(end_image)
    _write_shots(tmp_path, "project-1", [{
        "scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": "video/1.mp4",
        "start_image": str(start_image), "end_image": str(end_image), "width": 1920, "height": 1080,
        "timing": "00:00 - 00:06",
    }])
    monkeypatch.setattr(
        video_contract, "_get_aitunnel_video_models", lambda: _aitunnel_catalog(frame_images=("first_frame",))
    )

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == ["video_input_invalid:1-1"]


def test_delivery_promise_blocks_unsupported_size_or_duration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    start_image = tmp_path / "images" / "start.png"
    _write_frame(start_image)
    _write_shots(tmp_path, "project-1", [{
        "scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": "video/1.mp4",
        "start_image": str(start_image), "width": 1000, "height": 1000, "timing": "00:00 - 00:06",
    }])
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: _aitunnel_catalog())

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == ["video_input_invalid:1-1"]


def test_delivery_promise_blocks_model_without_supported_duration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    start_image = tmp_path / "images" / "start.png"
    _write_frame(start_image)
    _write_shots(tmp_path, "project-1", [{
        "scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": "video/1.mp4",
        "start_image": str(start_image), "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
    }])
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: _aitunnel_catalog(durations=()))

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == ["video_input_invalid:1-1"]


def test_delivery_promise_allows_trusted_aitunnel_resume_without_start_frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    video_path = tmp_path / "video" / "1.mp4"
    _write_frame(video_path)
    item = {
        "scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": str(video_path),
        "start_image": str(tmp_path / "images" / "removed.png"), "width": 1920, "height": 1080,
        "timing": "00:00 - 00:06",
    }
    source_hashes = {"start_image": "saved-start-hash", "end_image": None}
    input_hash = _build_input_hash(
        model_name="video-model", prompt_hash=_hash_text("pan right"),
        source_image_hashes=source_hashes, requested_duration=6, requested_width=1920, requested_height=1080,
        seed=None, frame_types=["first_frame"],
    )
    _write_shots(tmp_path, "project-1", [item])
    jobs_path = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots" / "provider_jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [{
        "provider": "aitunnel", "shot_key": "1-1", "output_path": str(video_path), "status": "downloaded",
        "source_image_hashes": source_hashes, "input_hash": input_hash, "original_input_hash": input_hash,
        "original_prompt_language": "en",
    }]}), encoding="utf-8")
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: (_ for _ in ()).throw(AssertionError()))

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is True
    assert result["blocking_reasons"] == []


@pytest.mark.parametrize(
    ("resume_status", "remove_output", "use_blockout_reference"),
    [
        ("download_failed", True, False),
        ("submitted", True, False),
        ("downloaded", True, False),
        ("submitted", False, False),
        ("download_failed", True, True),
    ],
)
def test_delivery_allows_matching_paid_task_without_frames(
    tmp_path, monkeypatch, resume_status, remove_output, use_blockout_reference,
):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    start_image = shots_dir / "start.png"
    _write_frame(start_image)
    video_path = shots_dir / "video.mp4"
    item = {
        "scene_number": 1, "shot_number": 1, "video_prompt": "pan right", "video_path": str(video_path),
        "start_image": str(start_image), "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
    }
    _write_shots(tmp_path, "project-1", [item])
    catalog = _aitunnel_catalog()
    job_store = aitunnel_module._ProviderJobStore(str(shots_dir / "provider_jobs.json"))

    submitted_payloads = []

    def first_post(*_args, **kwargs):
        submitted_payloads.append(kwargs["json"])
        return _AitunnelResponse(202, {"id": "job-1", "status": "pending"})

    monkeypatch.setattr(aitunnel_module.requests, "post", first_post)

    def first_get(url, **_kwargs):
        if url.endswith("/videos/job-1"):
            return _AitunnelResponse(200, {"id": "job-1", "status": "completed", "unsigned_urls": ["https://cdn/old.mp4"]})
        if url == "https://cdn/old.mp4":
            return _AitunnelResponse(200, content=b"old")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(aitunnel_module.requests, "get", first_get)
    assert aitunnel_module._generate_single_video_aitunnel(
        item, "session-1", "sk-test", catalog, "video-model", None, "en", job_store,
        use_blockout_reference=use_blockout_reference, generate_blockout=use_blockout_reference,
    )["success"] is True
    if use_blockout_reference:
        assert "reference_video" not in submitted_payloads[0]

    jobs_path = shots_dir / "provider_jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0].update({"status": resume_status, "video_url": "https://cdn/old.mp4"})
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    start_image.unlink()
    assert video_path.read_bytes() == b"old"
    if remove_output:
        video_path.unlink()

    monkeypatch.setattr(
        aitunnel_module.requests, "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resume must not POST")),
    )

    def resumed_get(url, **_kwargs):
        if url.endswith("/videos/job-1"):
            return _AitunnelResponse(200, {"id": "job-1", "status": "completed", "unsigned_urls": ["https://cdn/fresh.mp4"]})
        if resume_status == "downloaded" and url == "https://cdn/old.mp4":
            return _AitunnelResponse(200, content=b"fresh")
        if url == "https://cdn/fresh.mp4":
            return _AitunnelResponse(200, content=b"fresh")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(aitunnel_module.requests, "get", resumed_get)
    monkeypatch.setattr(aitunnel_module, "_get_aitunnel_video_models", lambda: catalog)
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: catalog)
    assert aitunnel_module.resumable_aitunnel_job_for_item(
        item, aitunnel_module._ProviderJobStore(str(jobs_path)), "video-model", None, "en"
    ) is True
    promise = video_contract.storybook_video_delivery_promise_tool(
        "session-1", "project-1", language="en",
        generate_blockout=use_blockout_reference, use_blockout_reference=use_blockout_reference,
    )

    assert promise["blocking_reasons"] == []
    assert promise["will_generate_video"] is True
    resumed = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
        max_concurrency=1,
        language="en",
        skip_prompt_enhancement=True,
        generate_blockout=use_blockout_reference,
        use_blockout_reference=use_blockout_reference,
    )
    assert resumed["status"] == "success"
    assert video_path.read_bytes() == b"fresh"
    assert json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"][0]["status"] == "downloaded"


def test_delivery_promise_allows_ru_trusted_resume_without_retranslating(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    start_image = shots_dir / "start.png"
    start_image.parent.mkdir(parents=True)
    Image.new("RGB", (1920, 1080)).save(start_image)
    video_path = shots_dir / "video.mp4"
    item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start", "video_prompt": "Панорама вправо",
        "video_path": str(video_path), "start_image": str(start_image), "width": 1920, "height": 1080,
        "timing": "00:00 - 00:06",
    }
    _write_shots(tmp_path, "project-1", [item])
    catalog = _aitunnel_catalog()
    monkeypatch.setattr(
        aitunnel_module, "translate_prompts_in_items", lambda source, _language: {**source, "video_prompt": "pan right"}
    )
    monkeypatch.setattr(
        aitunnel_module.requests, "post",
        lambda *_args, **_kwargs: _AitunnelResponse(202, {"id": "job-1", "status": "pending"}),
    )

    def fake_get(url, **_kwargs):
        if url.endswith("/videos/job-1"):
            return _AitunnelResponse(200, {"id": "job-1", "status": "completed", "unsigned_urls": ["https://cdn/video.mp4"]})
        if url == "https://cdn/video.mp4":
            return _AitunnelResponse(200, content=b"video")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)
    job_store = aitunnel_module._ProviderJobStore(str(shots_dir / "provider_jobs.json"))
    generated = aitunnel_module._generate_single_video_aitunnel(
        item, "session-1", "sk-test", catalog, "video-model", None, "ru", job_store
    )
    assert generated["success"] is True
    saved_job = json.loads((shots_dir / "provider_jobs.json").read_text(encoding="utf-8"))["jobs"][0]
    assert saved_job["prompt_hash"] == _hash_text("pan right")
    assert saved_job["original_input_hash"]
    saved_job.pop("original_input_hash")
    (shots_dir / "provider_jobs.json").write_text(
        json.dumps({"jobs": [saved_job]}), encoding="utf-8",
    )
    start_image.unlink()
    translations = []
    monkeypatch.setattr(
        aitunnel_module,
        "translate_prompts_in_items",
        lambda source, _language: (translations.append(source), {**source, "video_prompt": "pan right"})[1],
    )
    monkeypatch.setattr(
        aitunnel_module.requests, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no POST"))
    )
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: catalog)

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="ru")

    assert result["will_generate_video"] is True
    assert result["blocking_reasons"] == []
    assert len(translations) == 1
    migrated = json.loads((shots_dir / "provider_jobs.json").read_text(encoding="utf-8"))["jobs"][0]
    assert migrated["original_input_hash"]
    resumed = aitunnel_module._generate_single_video_aitunnel(
        item, "session-1", "sk-test", catalog, "video-model", None, "ru",
        aitunnel_module._ProviderJobStore(str(shots_dir / "provider_jobs.json")),
    )
    assert resumed["success"] is True
    assert len(translations) == 1
    wrong_language = aitunnel_module._generate_single_video_aitunnel(
        item, "session-1", "sk-test", catalog, "video-model", None, "en",
        aitunnel_module._ProviderJobStore(str(shots_dir / "provider_jobs.json")),
    )
    assert wrong_language["success"] is False


def test_preflight_rejects_project_id_path_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    try:
        video_contract.storybook_video_preflight_tool(
            session_id="session-1",
            project_id="../outside",
        )
    except ValueError as exc:
        assert "project_id must be a safe path segment" in str(exc)
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

    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_aitunnel_tool"
    )
    provider, providers = video_contract._select_provider()
    assert provider == "aitunnel"
    aitunnel = next(p for p in providers if p["name"] == "aitunnel")
    assert aitunnel["configured"] is True
    assert aitunnel["active"] is True


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


def test_video_music_readiness_treats_missing_shots_as_pre_run_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
        generate_music=False,
    )

    assert result["ready"] is True
    assert result["generation"] == {
        "video_enabled": True,
        "music_enabled": False,
        "language": "ru",
    }
    assert result["blocking_reasons"] == []
    assert "shots_json_missing_before_run" in result["warnings"]
    assert result["music"]["status"] == "disabled"
    action_ids = [action["id"] for action in result["workflow_actions"]["actions"]]
    assert "project_inventory" in action_ids
    assert "artifact_edit" in action_ids
    assert "media_edit" in action_ids
    assert "video_music_readiness" in action_ids


def test_video_music_readiness_reports_music_final_artifacts_and_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    monkeypatch.setenv("SUNO_COOKIE", "__client=sk-suno")
    base = tmp_path / "plots" / "storybooks" / "project-1"
    _write_shots(
        tmp_path,
        "project-1",
        [{"scene_number": 1, "shot_number": 1, "video_prompt": "pan", "video_path": "97_shots/1-1.mp4"}],
    )
    (base / "97_shots" / "provider_jobs.json").write_text(
        json.dumps({"jobs": [{"shot_key": "1-1", "status": "failed", "cost_rub": 2.5}]}),
        encoding="utf-8",
    )
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "music.mp3").write_bytes(b"mp3")
    (base / "98_audio" / "music_manifest.json").write_text(
        json.dumps({"status": "success", "task_id": "suno-task-1"}),
        encoding="utf-8",
    )
    (base / "98_audio" / "cue_sheet.json").write_text("{}", encoding="utf-8")
    (base / "99_final").mkdir(parents=True, exist_ok=True)
    for name in [
        "final_video.mp4",
        "timeline.fcpxml",
        "subtitles.srt",
        "manifest.json",
        "asset_manifest.json",
        "edit_decisions.json",
        "render_report.json",
    ]:
        (base / "99_final" / name).write_text("x", encoding="utf-8")
    (base / "99_final" / "final_review.json").write_text(
        json.dumps(
            {
                "passed": False,
                "checks": {"audio": {"passed": False}},
                "errors": ["Audio track missing"],
            }
        ),
        encoding="utf-8",
    )

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
    )

    assert result["ready"] is False
    assert result["capabilities"]["music"] is True
    assert result["music"]["configured"] is True
    assert result["music"]["music_exists"] is True
    assert result["artifacts"]["cue_sheet"]["exists"] is True
    assert result["artifacts"]["asset_manifest"]["exists"] is True
    assert result["artifacts"]["edit_decisions"]["exists"] is True
    assert result["artifacts"]["render_report"]["exists"] is True
    assert result["final_review"]["failed_checks"] == ["audio"]
    assert "provider_job_failed:1-1" in result["errors"]
    assert "final_review_failed" in result["errors"]
    assert "Audio track missing" in result["errors"]


def test_video_music_readiness_reports_invalid_json_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    base = tmp_path / "plots" / "storybooks" / "project-1"
    _write_shots(
        tmp_path,
        "project-1",
        [{"scene_number": 1, "shot_number": 1, "video_prompt": "pan", "video_path": "97_shots/1-1.mp4"}],
    )
    (base / "99_final").mkdir(parents=True, exist_ok=True)
    (base / "99_final" / "final_review.json").write_text("{", encoding="utf-8")

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
    )

    assert result["ready"] is False
    assert result["artifacts"]["final_review"]["status"] == "invalid"
    assert any(error.startswith("invalid_json:final_review.json") for error in result["errors"])


def test_video_music_readiness_reports_invalid_provider_jobs_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    base = tmp_path / "plots" / "storybooks" / "project-1"
    _write_shots(
        tmp_path,
        "project-1",
        [{"scene_number": 1, "shot_number": 1, "video_prompt": "pan", "video_path": "97_shots/1-1.mp4"}],
    )
    (base / "97_shots" / "provider_jobs.json").write_text(
        json.dumps({"unexpected": []}),
        encoding="utf-8",
    )

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
    )

    assert result["ready"] is False
    assert result["artifacts"]["provider_jobs"]["status"] == "invalid"
    assert "invalid_json_shape:provider_jobs.json" in result["errors"]


def _clear_provider_env(monkeypatch):
    for name in [
        "AITUNNEL_API_KEY",
        "AITUNNEL_VIDEO_MODEL",
        "MINIMAX_API_KEY",
        "KLING_API_KEY",
        "KLING_API_SECRET_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_delivery_promise_does_not_promise_when_active_provider_unconfigured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    # Only minimax is configured, but the active pipeline tool is aitunnel.
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_aitunnel_tool"
    )
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["provider"] == "aitunnel"
    assert result["capabilities"]["video"] is False
    assert result["will_generate_video"] is False
    assert "video_provider_unavailable" in result["blocking_reasons"]


def test_preflight_reports_active_provider_not_first_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    # Both aitunnel (first in the menu) and minimax are configured, but minimax is active.
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "video-model")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_mm_tool"
    )

    result = video_contract.storybook_video_preflight_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    artifact = tmp_path / result["artifact_path"]
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["provider"] == "minimax"
    assert saved["capabilities"]["video"] is True
    active = [p for p in saved["capability_details"]["providers_considered"] if p["active"]]
    assert [p["name"] for p in active] == ["minimax"]


def test_delivery_promise_promises_when_active_provider_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_aitunnel_tool"
    )
    start_image = tmp_path / "images" / "start.png"
    _write_frame(start_image)
    monkeypatch.setattr(video_contract, "_get_aitunnel_video_models", lambda: _aitunnel_catalog())
    _write_shots(tmp_path, "project-1", [{
        "video_prompt": "pan", "video_path": "video/1.mp4", "start_image": str(start_image),
        "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
    }])

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["provider"] == "aitunnel"
    assert result["will_generate_video"] is True
    assert result["blocking_reasons"] == []


def test_decision_log_records_active_provider_not_menu_pick(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "video-model")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_mm_tool"
    )
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])
    video_contract.storybook_video_preflight_tool("session-1", "project-1")
    video_contract.storybook_video_delivery_promise_tool("session-1", "project-1")

    result = video_contract.storybook_video_decision_log_tool(
        session_id="session-1",
        project_id="project-1",
    )

    provider_decision = next(d for d in result["decisions"] if d["name"] == "video_provider")
    assert provider_decision["selected"] == "minimax"


def test_active_provider_unresolved_blocks_promise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)  # aitunnel is fully configured
    # The pipeline YAML can no longer be resolved to a known provider -> fail-closed.
    monkeypatch.setattr(video_contract, "_active_video_tool_name", lambda: None)
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["provider"] == "none"
    assert result["capabilities"]["video"] is False
    assert result["will_generate_video"] is False
    assert "video_provider_unresolved" in result["blocking_reasons"]


def test_decision_log_status_reflects_missing_final_video(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    monkeypatch.setattr(
        video_contract, "_active_video_tool_name", lambda: "video_generator_aitunnel_tool"
    )
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])
    video_contract.storybook_video_preflight_tool("session-1", "project-1")
    video_contract.storybook_video_delivery_promise_tool("session-1", "project-1")

    result = video_contract.storybook_video_decision_log_tool("session-1", "project-1")
    assert result["final_video_status"] == "missing"
    assert result["status"] == "incomplete"

    final_dir = tmp_path / "plots" / "storybooks" / "project-1" / "99_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "final_video.mp4").write_bytes(b"mp4")

    present = video_contract.storybook_video_decision_log_tool("session-1", "project-1")
    assert present["final_video_status"] == "present"
    assert present["status"] == "success"


# === Э8 (раздел 11.4): состояние слоя болванок в сводках/promise ==============

def test_preflight_summary_includes_blockout_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)

    result = video_contract.storybook_video_preflight_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
        generate_blockout=True,
        use_blockout_reference=True,
    )

    assert result["blockout"]["enabled"] is True
    assert result["blockout"]["use_reference"] is True
    assert "reference_forecast" in result["blockout"]


def test_preflight_summary_blockout_defaults_to_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)

    result = video_contract.storybook_video_preflight_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["blockout"] == {
        "enabled": False,
        "use_reference": False,
        "reference_forecast": {"eligible": 0, "not_eligible": 0},
    }


def test_delivery_promise_blockout_reference_forecast_counts_eligible_shots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    base = tmp_path / "plots" / "storybooks" / "project-1"
    blockout_ok = base / "93_blockout" / "scene_01_shot_01" / "blockout_ref.mp4"
    blockout_ok.parent.mkdir(parents=True, exist_ok=True)
    blockout_ok.write_bytes(b"clip")
    _write_shots(
        tmp_path,
        "project-1",
        [
            # condition 2 satisfied (file exists), no junction_failed -> eligible.
            {"scene_number": 1, "shot_number": 1, "video_prompt": "pan", "video_path": "video/1.mp4", "blockout_video": str(blockout_ok)},
            # no blockout_video at all -> condition 2 fails.
            {"scene_number": 1, "shot_number": 2, "video_prompt": "tilt", "video_path": "video/2.mp4"},
            # blockout_video present but junction_failed -> condition 5 fails.
            {
                "scene_number": 1, "shot_number": 3, "video_prompt": "zoom", "video_path": "video/3.mp4",
                "blockout_video": str(blockout_ok), "blockout_junction_failed": True,
            },
        ],
    )

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
        generate_blockout=True,
        use_blockout_reference=True,
    )

    assert result["blockout_reference_forecast"] == {"eligible": 1, "not_eligible": 2}


def test_delivery_promise_blockout_reference_forecast_zero_when_layer_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])

    result = video_contract.storybook_video_delivery_promise_tool(
        session_id="session-1",
        project_id="project-1",
        enable=True,
    )

    assert result["blockout_reference_forecast"] == {"eligible": 0, "not_eligible": 1}


def test_music_readiness_exposes_blockout_state_under_video(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
        generate_blockout=True,
        blockout_use_as_video_reference=True,
    )

    assert result["video"]["blockout"]["enabled"] is True
    assert result["video"]["blockout"]["use_reference"] is True


def test_decision_log_ignores_blockout_flags_by_design(tmp_path, monkeypatch):
    """раздел 11.4: журнал решений о болванке ничего не решает и полей не получает."""
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    _write_shots(tmp_path, "project-1", [{"video_prompt": "pan", "video_path": "video/1.mp4"}])
    video_contract.storybook_video_preflight_tool("session-1", "project-1")
    video_contract.storybook_video_delivery_promise_tool("session-1", "project-1")

    result = video_contract.storybook_video_decision_log_tool(
        session_id="session-1",
        project_id="project-1",
    )
    assert "blockout" not in result


def test_resolve_blockout_flags_prioritizes_explicit_args_over_brief(tmp_path, monkeypatch):
    """Раздел 18.3: явный аргумент важнее 00_brief.json."""
    monkeypatch.chdir(tmp_path)
    project_dir = tmp_path / "plots" / "storybooks" / "project-1"
    project_dir.mkdir(parents=True)
    (project_dir / "00_brief.json").write_text(
        json.dumps({"generate_blockout": True, "blockout_use_as_video_reference": True}),
        encoding="utf-8",
    )

    resolved_blockout, resolved_reference = video_contract._resolve_blockout_flags(
        project_dir, generate_blockout=False, blockout_use_as_video_reference=False
    )

    assert resolved_blockout is False
    assert resolved_reference is False


def test_resolve_blockout_flags_falls_back_to_brief_when_args_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_dir = tmp_path / "plots" / "storybooks" / "project-1"
    project_dir.mkdir(parents=True)
    (project_dir / "00_brief.json").write_text(
        json.dumps({"generate_blockout": True, "blockout_use_as_video_reference": True}),
        encoding="utf-8",
    )

    resolved_blockout, resolved_reference = video_contract._resolve_blockout_flags(
        project_dir, generate_blockout=None, blockout_use_as_video_reference=None
    )

    assert resolved_blockout is True
    assert resolved_reference is True


def test_resolve_blockout_flags_defaults_to_false_without_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_dir = tmp_path / "plots" / "storybooks" / "project-1"
    project_dir.mkdir(parents=True)

    resolved_blockout, resolved_reference = video_contract._resolve_blockout_flags(
        project_dir, generate_blockout=None, blockout_use_as_video_reference=None
    )

    assert resolved_blockout is False
    assert resolved_reference is False


def test_probe_blockout_blender_readiness_reports_missing_binary(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_BLENDER_MODE", raising=False)
    monkeypatch.delenv("BLOCKOUT_BLENDER_BIN", raising=False)
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: None)

    result = video_contract._probe_blockout_blender_readiness()

    assert result["available"] is False
    assert result["version"] is None
    assert "not found" in result["message"]


def test_probe_blockout_blender_readiness_reports_available_when_version_ok(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_BLENDER_MODE", raising=False)
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/blender")
    fake_proc = type("Proc", (), {"returncode": 0, "stdout": "Blender 4.2.1"})()
    monkeypatch.setattr(video_contract.subprocess, "run", lambda *a, **k: fake_proc)

    result = video_contract._probe_blockout_blender_readiness()

    assert result["available"] is True
    assert result["version"] == "4.2"
    assert result["path"] == "/usr/bin/blender"


def test_probe_blockout_blender_readiness_rejects_version_below_minimum(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_BLENDER_MODE", raising=False)
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/blender")
    fake_proc = type("Proc", (), {"returncode": 0, "stdout": "Blender 3.6.0"})()
    monkeypatch.setattr(video_contract.subprocess, "run", lambda *a, **k: fake_proc)

    result = video_contract._probe_blockout_blender_readiness()

    assert result["available"] is False
    assert "below minimum" in result["message"]


def test_probe_blockout_blender_readiness_never_raises_in_module_mode(monkeypatch):
    # bpy действительно не установлен в этом окружении — реальный import-error путь.
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "module")

    result = video_contract._probe_blockout_blender_readiness()

    assert result["available"] is False
    assert result["mode"] == "module"


def test_blockout_asset_library_summary_reports_object_count_and_fetch_flag(monkeypatch):
    monkeypatch.setattr(
        video_contract, "_read_blockout_asset_index", lambda: {"objects": [{"id": "a"}, {"id": "b"}]}
    )
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "off")

    result = video_contract._blockout_asset_library_summary()

    assert result == {"object_count": 2, "fetch_enabled": False}


def test_blockout_asset_library_summary_fetch_enabled_by_default(monkeypatch):
    monkeypatch.setattr(video_contract, "_read_blockout_asset_index", lambda: {"objects": []})
    monkeypatch.delenv("BLOCKOUT_ASSET_FETCH", raising=False)

    result = video_contract._blockout_asset_library_summary()

    assert result == {"object_count": 0, "fetch_enabled": True}


def test_music_readiness_includes_blender_and_asset_library_under_blockout(tmp_path, monkeypatch):
    """Раздел 18.3: панель читает video.blockout.blender/asset_library из этого контракта."""
    monkeypatch.chdir(tmp_path)
    _enable_video_env(monkeypatch)
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    fake_proc = type("Proc", (), {"returncode": 0, "stdout": "Blender 4.2.0"})()
    monkeypatch.setattr(video_contract.subprocess, "run", lambda *a, **k: fake_proc)
    monkeypatch.setattr(video_contract, "_read_blockout_asset_index", lambda: {"objects": [{"id": "a"}]})

    result = video_contract.storybook_video_music_readiness(
        project_id="project-1",
        session_id="session-1",
        generate_blockout=True,
        blockout_use_as_video_reference=True,
    )

    assert result["video"]["blockout"]["blender"]["available"] is True
    assert result["video"]["blockout"]["asset_library"]["object_count"] == 1


def test_live_yaml_video_step_maps_to_known_provider():
    # Switch-agnostic drift guard: whichever tool_name is left uncommented in the
    # canonical pipeline must map to a known provider (catches step-id/tool typos).
    tool_name = video_contract._active_video_tool_name()
    assert tool_name in video_contract._TOOL_NAME_TO_PROVIDER
    provider = video_contract._active_video_provider()
    assert provider is not None
    assert provider in set(video_contract._TOOL_NAME_TO_PROVIDER.values())


def test_blockout_actions_registered_manager_only_and_web_unsupported():
    """A36 (раздел 18.7): пять новых действий болванки в реестре, с
    status: manager_only и surfaces.storybook_manager: available; на веб —
    unsupported. Старая regenerate_image остаётся нетронутой."""
    actions = {action["id"]: action for action in video_contract.STORYBOOK_WORKFLOW_ACTIONS}

    for action_id in (
        "blockout_render",
        "blockout_preview_build",
        "blockout_review",
        "blockout_asset_map_edit",
        "blockout_regenerate_shots",
    ):
        assert action_id in actions, action_id
        action = actions[action_id]
        assert action["status"] == "manager_only"
        assert action["surfaces"]["storybook_manager"] == "available"
        assert action["surfaces"]["react"] == "unsupported"
        assert action["surfaces"]["streamlit"] == "unsupported"

    assert actions["blockout_regenerate_shots"]["category"] == "selective_regeneration"

    old_regenerate_image = actions["regenerate_image"]
    assert old_regenerate_image["status"] == "not_implemented"
    assert old_regenerate_image["surfaces"]["storybook_manager"] == "not_implemented"


def test_storybook_workflow_actions_tool_returns_blockout_entries():
    result = video_contract.storybook_workflow_actions()
    ids = {action["id"] for action in result["actions"]}
    assert {"blockout_render", "blockout_preview_build", "blockout_review",
            "blockout_asset_map_edit", "blockout_regenerate_shots"} <= ids
