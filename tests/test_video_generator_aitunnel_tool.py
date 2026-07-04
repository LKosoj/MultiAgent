import json
import threading
from pathlib import Path

from PIL import Image

from custom_tools.storybook import video_generator_aitunnel_tool as aitunnel_module


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, text="", content=b""):
        self.status_code = status_code
        self._json_payload = json_payload
        self.text = text
        self.content = content

    def json(self):
        return self._json_payload

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


def _create_png(path: Path, size=(1920, 1080)):
    image = Image.new("RGB", size, color=(12, 34, 56))
    image.save(path)


def _patch_project_mode(monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    monkeypatch.setattr(aitunnel_module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        aitunnel_module,
        "update_shots_with_descriptions",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        aitunnel_module,
        "_get_aitunnel_video_models",
        lambda force_refresh=False: {
            "installed-model": {
                "min_price_per_second": 1,
                "max_price_per_second": 2,
                "supported_sizes": ["1920x1080"],
                "supported_resolutions": ["1080p"],
                "supported_aspect_ratios": ["16:9"],
                "supported_durations": [6],
                "supported_frame_images": ["first_frame", "last_frame"],
                "supports_seed": True,
            },
        },
    )


def _write_project(tmp_path, monkeypatch, items, project_id="project-1"):
    monkeypatch.chdir(tmp_path)
    shots_dir = tmp_path / "plots" / "storybooks" / project_id / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return project_id, shots_dir


def _make_project_item(shots_dir: Path, shot_number: int, prompt: str = "Test prompt"):
    start_image = shots_dir / f"start-{shot_number}.png"
    output_video = shots_dir / f"video-{shot_number}.mp4"
    _create_png(start_image)
    return {
        "scene_number": 1,
        "shot_number": shot_number,
        "shot_type": "start",
        "video_prompt": prompt,
        "video_path": str(output_video),
        "timing": "00:00 - 00:06",
        "start_image": str(start_image),
        "width": 1920,
        "height": 1080,
    }


def _read_provider_jobs(shots_dir: Path):
    return json.loads((shots_dir / "provider_jobs.json").read_text(encoding="utf-8"))["jobs"]


def test_video_generator_aitunnel_tool_generates_video_with_installed_model_and_optimized_params(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")
    monkeypatch.setattr(aitunnel_module.time, "sleep", lambda *_args, **_kwargs: None)

    start_image = tmp_path / "start.png"
    end_image = tmp_path / "end.png"
    output_video = tmp_path / "video.mp4"
    _create_png(start_image)
    _create_png(end_image)

    captured_payload = {}

    monkeypatch.setattr(
        aitunnel_module,
        "_get_aitunnel_video_models",
        lambda force_refresh=False: {
            "installed-model": {
                "min_price_per_second": 99,
                "max_price_per_second": 99,
                "supported_sizes": ["854x480", "1280x720"],
                "supported_resolutions": ["480p", "720p"],
                "supported_aspect_ratios": ["16:9"],
                "supported_durations": [4, 8],
                "supported_frame_images": ["first_frame", "last_frame"],
                "supports_seed": True,
            },
        },
    )

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, timeout
        captured_payload["url"] = url
        captured_payload["json"] = json
        return _FakeResponse(
            status_code=202,
            json_payload={"id": "job-1", "status": "pending", "polling_url": f"{url}/job-1"},
        )

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://api.aitunnel.ru/v1/videos/job-1/content?index=0"],
                    "usage": {"cost_rub": 12.34},
                },
            )
        if url.endswith("/content?index=0"):
            return _FakeResponse(status_code=200, content=b"fake-mp4-bytes")
        raise AssertionError(f"Unexpected GET url: {url}, stream={stream}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    items = {
        "items": [
            {
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "video_prompt": "A cinematic transition between two frames",
                "video_path": str(output_video),
                "timing": "00:00 - 00:06",
                "start_image": str(start_image),
                "end_image": str(end_image),
                "width": 1920,
                "height": 1080,
            }
        ]
    }

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        items=json.dumps(items),
        enable=True,
        seed=123,
        max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    assert output_video.read_bytes() == b"fake-mp4-bytes"
    assert result["results"][0]["task_id"] == "job-1"
    assert result["results"][0]["model"] == "installed-model"
    assert captured_payload["json"]["model"] == "installed-model"
    assert captured_payload["json"]["duration"] == 8
    assert captured_payload["json"]["size"] == "1280x720"
    assert captured_payload["json"]["generate_audio"] is False
    assert captured_payload["json"]["seed"] == 123
    assert captured_payload["json"]["frame_images"][0]["frame_type"] == "first_frame"
    assert captured_payload["json"]["frame_images"][1]["frame_type"] == "last_frame"
    assert captured_payload["json"]["frame_images"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_video_generator_aitunnel_tool_reports_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AITUNNEL_API_KEY", raising=False)
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "installed-model")

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        items={
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "shot_type": "start",
                    "video_prompt": "Test prompt",
                    "video_path": str(tmp_path / "video.mp4"),
                }
            ]
        },
        enable=True,
    )

    assert result["status"] == "error"
    assert "AITUNNEL_API_KEY" in result["message"]
    assert "AITUNNEL_API_KEY" in result["error"]


def test_video_generator_aitunnel_tool_skips_without_env_when_disabled(monkeypatch):
    monkeypatch.delenv("AITUNNEL_API_KEY", raising=False)
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        enable=False,
    )

    assert result["status"] == "skipped"
    assert "error" not in result


def test_disabled_project_run_skips_when_shots_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AITUNNEL_API_KEY", raising=False)
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id="project-without-video-shots",
        enable=False,
    )

    assert result["status"] == "skipped"
    assert "error" not in result


def test_disabled_project_run_updates_shot_descriptions_before_skipping(tmp_path, monkeypatch):
    monkeypatch.delenv("AITUNNEL_API_KEY", raising=False)
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, _shots_dir = _write_project(tmp_path, monkeypatch, [item])
    calls = []

    def fake_update(shots_file_path, items_list, force_update=False, skip_prompt_enhancement=False):
        calls.append(
            {
                "shots_file_path": shots_file_path,
                "items_count": len(items_list),
                "force_update": force_update,
                "skip_prompt_enhancement": skip_prompt_enhancement,
            }
        )
        return False

    def forbidden_models(*_args, **_kwargs):
        raise AssertionError("disabled run must not request AITUNNEL model capabilities")

    monkeypatch.setattr(aitunnel_module, "update_shots_with_descriptions", fake_update)
    monkeypatch.setattr(aitunnel_module, "_get_aitunnel_video_models", forbidden_models)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=False,
        force_update_prompts="true",
        skip_prompt_enhancement="true",
    )

    assert result["status"] == "skipped"
    assert calls == [
        {
            "shots_file_path": "plots/storybooks/project-1/97_shots/shots.json",
            "items_count": 1,
            "force_update": True,
            "skip_prompt_enhancement": True,
        }
    ]


def test_video_generator_aitunnel_tool_reports_missing_installed_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.delenv("AITUNNEL_VIDEO_MODEL", raising=False)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        items={
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "shot_type": "start",
                    "video_prompt": "Test prompt",
                    "video_path": str(tmp_path / "video.mp4"),
                }
            ]
        },
        enable=True,
    )

    assert result["status"] == "error"
    assert "AITUNNEL_VIDEO_MODEL" in result["message"]


def test_video_generator_aitunnel_tool_returns_failed_item_when_no_compatible_model(tmp_path, monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "model-without-last-frame")
    monkeypatch.setattr(
        aitunnel_module,
        "_get_aitunnel_video_models",
        lambda force_refresh=False: {
            "model-without-last-frame": {
                "min_price_per_second": 1,
                "max_price_per_second": 2,
                "supported_sizes": ["1920x1080"],
                "supported_resolutions": ["1080p"],
                "supported_aspect_ratios": ["16:9"],
                "supported_durations": [6],
                "supported_frame_images": ["first_frame"],
                "supports_seed": True,
            }
        },
    )

    start_image = tmp_path / "start.png"
    end_image = tmp_path / "end.png"
    _create_png(start_image)
    _create_png(end_image)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-2",
        items={
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 3,
                    "shot_type": "start",
                    "video_prompt": "Test prompt",
                    "video_path": str(tmp_path / "video.mp4"),
                    "timing": "00:00 - 00:06",
                    "start_image": str(start_image),
                    "end_image": str(end_image),
                    "width": 1920,
                    "height": 1080,
                }
            ]
        },
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "error"
    assert result["stats"] == {"total": 1, "successful": 0, "failed": 1}
    assert result["results"][0]["success"] is False
    assert "не поддерживает last_frame" in result["results"][0]["error"]


def test_project_id_run_persists_provider_job_and_resumes_without_post(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    post_calls = []
    download_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        if post_calls:
            raise AssertionError("resume must not POST a duplicate AITunnel job")
        post_calls.append(url)
        return _FakeResponse(
            status_code=202,
            json_payload={"id": "job-1", "status": "pending"},
        )

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                    "usage": {"cost_rub": 7.5},
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            download_calls.append(url)
            return _FakeResponse(status_code=200, content=f"video-{len(download_calls)}".encode("utf-8"))
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"video-1"
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["shot_key"] == "1-1"
    assert jobs[0]["provider"] == "aitunnel"
    assert jobs[0]["model"] == "installed-model"
    assert jobs[0]["task_id"] == "job-1"
    assert jobs[0]["status"] == "downloaded"
    assert jobs[0]["cost"] == 7.5
    assert jobs[0]["currency"] == "RUB"
    assert jobs[0]["video_url"] == "https://cdn.example/job-1.mp4"
    assert jobs[0]["error"] is None
    assert set(jobs[0]).issuperset(
        {
            "shot_key",
            "provider",
            "model",
            "prompt_hash",
            "source_image_hashes",
            "input_hash",
            "task_id",
            "status",
            "cost",
            "currency",
            "output_path",
            "video_url",
            "error",
            "timestamps",
        }
    )
    assert {"submitted_at", "polled_at", "completed_at", "downloaded_at"}.issubset(jobs[0]["timestamps"])

    provider_jobs_path = shots_dir / "provider_jobs.json"
    provider_jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))
    provider_jobs["jobs"][0]["status"] = "pending"
    provider_jobs["jobs"][0]["video_url"] = None
    provider_jobs_path.write_text(json.dumps(provider_jobs), encoding="utf-8")
    Path(item["video_path"]).unlink()

    resumed = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert resumed["status"] == "success"
    assert len(post_calls) == 1
    assert Path(item["video_path"]).read_bytes() == b"video-2"


def test_project_id_unsigned_video_download_does_not_send_bearer_token(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, _ = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del timeout, stream
        if url.endswith("/videos/job-1"):
            assert headers and headers.get("Authorization") == "Bearer sk-test"
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            assert not headers or "Authorization" not in headers
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"


def test_project_id_fallback_content_download_keeps_bearer_token(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, _ = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload={"id": "job-1", "status": "completed"})
        if url.endswith("/videos/job-1/content?index=0"):
            assert headers and headers.get("Authorization") == "Bearer sk-test"
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"


def test_project_id_completed_download_failed_job_downloads_without_post(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get_first(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"first")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_first)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )
    assert first["status"] == "success"

    provider_jobs_path = shots_dir / "provider_jobs.json"
    provider_jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))
    provider_jobs["jobs"][0]["status"] = "download_failed"
    provider_jobs_path.write_text(json.dumps(provider_jobs), encoding="utf-8")
    Path(item["video_path"]).unlink()

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("download_failed resume must not POST")

    def fake_get_resume(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        # M-14: a download_failed job with a live task_id must re-poll the task for a
        # fresh unsigned URL instead of blindly re-hitting the (possibly expired) old one.
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"resumed")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_resume)

    resumed = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert resumed["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"resumed"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_project_id_input_hash_change_marks_old_job_stale_and_submits_new(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1, prompt="Original prompt")
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    submitted_ids = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        task_id = f"job-{len(submitted_ids) + 1}"
        submitted_ids.append(task_id)
        return _FakeResponse(status_code=202, json_payload={"id": task_id, "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url.endswith("/videos/job-2"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-2",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-2.mp4"],
                },
            )
        if url in {"https://cdn.example/job-1.mp4", "https://cdn.example/job-2.mp4"}:
            return _FakeResponse(status_code=200, content=url.rsplit("/", 1)[-1].encode("utf-8"))
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )
    assert first["status"] == "success"

    item["video_prompt"] = "Changed prompt"
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")

    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert second["status"] == "success"
    assert submitted_ids == ["job-1", "job-2"]
    jobs = _read_provider_jobs(shots_dir)
    assert [job["status"] for job in jobs] == ["stale", "downloaded"]
    assert jobs[0]["input_hash"] != jobs[1]["input_hash"]
    assert "stale_at" in jobs[0]["timestamps"]


def test_project_id_existing_output_marks_downloaded_without_post(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    Path(item["video_path"]).write_bytes(b"already-there")
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("existing output must not POST")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    assert result["results"][0]["task_id"] is None
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "downloaded"
    assert jobs[0]["output_path"] == item["video_path"]


def test_project_id_existing_output_with_previous_job_survives_missing_source_frame(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )
    assert first["status"] == "success"

    Path(item["start_image"]).unlink()

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("trusted existing output must not POST after source frame disappeared")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)

    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert second["status"] == "success"
    assert len(post_calls) == 1
    assert Path(item["video_path"]).read_bytes() == b"video"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_project_id_existing_output_with_previous_job_survives_missing_end_frame(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    end_image = shots_dir / "end-1.png"
    _create_png(end_image)
    item["end_image"] = str(end_image)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )
    assert first["status"] == "success"

    end_image.unlink()

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("trusted existing output must not POST after end frame disappeared")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)

    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert second["status"] == "success"
    assert len(post_calls) == 1
    assert [job["status"] for job in _read_provider_jobs(shots_dir)] == ["downloaded"]


def test_project_id_failed_provider_job_submits_new_task_instead_of_resuming(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        task_id = f"job-{len(post_calls) + 1}"
        post_calls.append(task_id)
        return _FakeResponse(status_code=202, json_payload={"id": task_id, "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={"id": "job-1", "status": "failed", "error": "provider failed"},
            )
        if url.endswith("/videos/job-2"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-2",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-2.mp4"],
                },
            )
        if url == "https://cdn.example/job-2.mp4":
            return _FakeResponse(status_code=200, content=b"retry-video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )
    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert first["status"] == "error"
    assert second["status"] == "success"
    assert post_calls == ["job-1", "job-2"]
    assert Path(item["video_path"]).read_bytes() == b"retry-video"
    assert [job["status"] for job in _read_provider_jobs(shots_dir)] == ["failed", "downloaded"]


def test_project_id_submitting_job_without_task_id_blocks_duplicate_post(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    store = aitunnel_module._ProviderJobStore(str(shots_dir / "provider_jobs.json"))
    prompt_hash = aitunnel_module._hash_text(item["video_prompt"])
    source_hashes = {
        "start_image": aitunnel_module._hash_source_image(item["start_image"]),
        "end_image": None,
    }
    input_hash = aitunnel_module._build_input_hash(
        model_name="installed-model",
        prompt_hash=prompt_hash,
        source_image_hashes=source_hashes,
        requested_duration=6,
        requested_width=1920,
        requested_height=1080,
        seed=None,
        frame_types=["first_frame"],
    )
    store.ensure_job(
        {
            **aitunnel_module._new_provider_job(
                shot_key="1-1",
                model="installed-model",
                prompt_hash=prompt_hash,
                source_image_hashes=source_hashes,
                input_hash=input_hash,
                output_path=item["video_path"],
            ),
            "status": "submitting",
        }
    )

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("submitting job without task_id must not POST a duplicate")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "error"
    assert "дубликат платной задачи" in result["results"][0]["error"]


def test_project_id_corrupt_provider_jobs_returns_structured_error(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    (shots_dir / "provider_jobs.json").write_text("{not-json", encoding="utf-8")

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "error"
    assert "provider_jobs.json" in result["message"]
    assert result["results"] == []


def test_provider_job_store_merges_writes_from_multiple_instances(tmp_path):
    provider_jobs_path = tmp_path / "provider_jobs.json"
    store_one = aitunnel_module._ProviderJobStore(str(provider_jobs_path))
    store_two = aitunnel_module._ProviderJobStore(str(provider_jobs_path))

    store_one.ensure_job(
        aitunnel_module._new_provider_job(
            shot_key="1-1",
            model="model",
            prompt_hash="prompt-1",
            source_image_hashes={"start_image": "hash-1"},
            input_hash="input-1",
            output_path="video-1.mp4",
        )
    )
    store_two.ensure_job(
        aitunnel_module._new_provider_job(
            shot_key="1-2",
            model="model",
            prompt_hash="prompt-2",
            source_image_hashes={"start_image": "hash-2"},
            input_hash="input-2",
            output_path="video-2.mp4",
        )
    )

    jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert {job["shot_key"] for job in jobs} == {"1-1", "1-2"}


def test_provider_job_store_merges_thread_concurrent_instances(tmp_path):
    provider_jobs_path = tmp_path / "provider_jobs.json"
    errors = []

    def write_job(index: int):
        try:
            store = aitunnel_module._ProviderJobStore(str(provider_jobs_path))
            store.ensure_job(
                aitunnel_module._new_provider_job(
                    shot_key=f"1-{index}",
                    model="model",
                    prompt_hash=f"prompt-{index}",
                    source_image_hashes={"start_image": f"hash-{index}"},
                    input_hash=f"input-{index}",
                    output_path=f"video-{index}.mp4",
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_job, args=(index,)) for index in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert {job["shot_key"] for job in jobs} == {f"1-{index}" for index in range(1, 9)}


def test_sample_before_batch_processes_selected_shot_and_reports_remaining(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    first_item = _make_project_item(shots_dir, 1)
    second_item = _make_project_item(shots_dir, 2)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [first_item, second_item])

    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-2", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-2"):
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-2",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-2.mp4"],
                },
            )
        if url == "https://cdn.example/job-2.mp4":
            return _FakeResponse(status_code=200, content=b"sample")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        sample_before_batch=True,
        sample_shot_key="1-2",
        max_concurrency=2,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0, "remaining": 1}
    assert len(post_calls) == 1
    assert not Path(first_item["video_path"]).exists()
    assert Path(second_item["video_path"]).read_bytes() == b"sample"
    assert _read_provider_jobs(shots_dir)[0]["shot_key"] == "1-2"


def test_sample_before_batch_string_false_does_not_enable_sample_mode(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    first_item = _make_project_item(shots_dir, 1)
    second_item = _make_project_item(shots_dir, 2)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [first_item, second_item])

    submitted_ids = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        job_id = f"job-{len(submitted_ids) + 1}"
        submitted_ids.append(job_id)
        return _FakeResponse(status_code=202, json_payload={"id": job_id, "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        for job_id in submitted_ids:
            if url.endswith(f"/videos/{job_id}"):
                return _FakeResponse(
                    status_code=200,
                    json_payload={
                        "id": job_id,
                        "status": "completed",
                        "unsigned_urls": [f"https://cdn.example/{job_id}.mp4"],
                    },
                )
            if url == f"https://cdn.example/{job_id}.mp4":
                return _FakeResponse(status_code=200, content=job_id.encode("utf-8"))
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        sample_before_batch="false",
        max_concurrency=2,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 2, "successful": 2, "failed": 0}
    assert "remaining" not in result["stats"]
    assert len(submitted_ids) == 2
    assert Path(first_item["video_path"]).exists()
    assert Path(second_item["video_path"]).exists()
    assert [job["status"] for job in _read_provider_jobs(shots_dir)] == ["downloaded", "downloaded"]


def _completed_job_payload(task_id: str, url: str) -> dict:
    return {
        "id": task_id,
        "status": "completed",
        "unsigned_urls": [url],
    }


def test_poll_transient_non200_retries(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    poll_calls = []

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            poll_calls.append(url)
            # H-3: the first polls fail transiently (non-200); the job must retry
            # inside the deadline instead of failing and dropping the paid task_id.
            if len(poll_calls) <= 2:
                return _FakeResponse(status_code=503, text="upstream unavailable")
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"
    assert len(poll_calls) == 3
    assert Path(item["video_path"]).read_bytes() == b"video"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_poll_transient_exhaustion_marks_poll_timeout_and_resumes(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get_failing(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=502, text="bad gateway")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_failing)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert first["status"] == "error"
    job = _read_provider_jobs(shots_dir)[0]
    # H-3: exhausting the transient budget must keep the paid task_id and leave the
    # job resumable (poll_timeout), never failed.
    assert job["status"] == "poll_timeout"
    assert job["task_id"] == "job-1"

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("poll_timeout resume must not POST a duplicate paid task")

    def fake_get_recovered(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"recovered")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_recovered)

    resumed = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert resumed["status"] == "success"
    assert len(post_calls) == 1
    assert Path(item["video_path"]).read_bytes() == b"recovered"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_unknown_provider_status_treated_as_in_progress(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    poll_calls = []

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            poll_calls.append(url)
            # M-13: an unrecognized provider status is not a terminal failure; polling
            # keeps going until a real completed/failed verdict arrives.
            if len(poll_calls) == 1:
                return _FakeResponse(status_code=200, json_payload={"id": "job-1", "status": "moderation"})
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1",
        project_id=project_id,
        enable=True,
        max_concurrency=1,
    )

    assert result["status"] == "success"
    assert len(poll_calls) == 2
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_provider_status_recorded_separately(tmp_path):
    provider_jobs_path = tmp_path / "provider_jobs.json"
    store = aitunnel_module._ProviderJobStore(str(provider_jobs_path))
    store.ensure_job(
        aitunnel_module._new_provider_job(
            shot_key="1-1",
            model="installed-model",
            prompt_hash="prompt-1",
            source_image_hashes={"start_image": "hash-1", "end_image": None},
            input_hash="input-1",
            output_path=str(tmp_path / "video.mp4"),
        )
    )
    store.update_job("1-1", "input-1", {"status": "submitted", "task_id": "job-1"}, "submitted_at")

    # M-13: a poll update writes the raw provider value into provider_status only; the
    # internal lifecycle status is untouched and no failure timestamp is stamped.
    aitunnel_module._record_poll_update(store, "1-1", "input-1", {"status": "moderation", "error": None})

    job = _read_provider_jobs(tmp_path)[0]
    assert job["provider_status"] == "moderation"
    assert job["status"] == "submitted"
    assert "failed_at" not in job["timestamps"]
    assert "polled_at" in job["timestamps"]


def test_download_failed_repolls_for_fresh_url(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get_first(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/stale-signed.mp4"))
        if url == "https://cdn.example/stale-signed.mp4":
            return _FakeResponse(status_code=200, content=b"first")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_first)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )
    assert first["status"] == "success"

    # Simulate a download that failed against an already-expired signed URL.
    provider_jobs_path = shots_dir / "provider_jobs.json"
    provider_jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))
    provider_jobs["jobs"][0]["status"] = "download_failed"
    provider_jobs["jobs"][0]["video_url"] = "https://cdn.example/stale-signed.mp4"
    provider_jobs_path.write_text(json.dumps(provider_jobs), encoding="utf-8")
    Path(item["video_path"]).unlink()

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("download_failed re-poll must not POST")

    def fake_get_resume(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            # M-14: re-poll yields a *fresh* unsigned URL, not the stale one.
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/fresh-signed.mp4"))
        if url == "https://cdn.example/fresh-signed.mp4":
            return _FakeResponse(status_code=200, content=b"fresh")
        raise AssertionError(f"stale URL must never be re-fetched: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_resume)

    resumed = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert resumed["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"fresh"
    job = _read_provider_jobs(shots_dir)[0]
    assert job["status"] == "downloaded"
    assert job["video_url"] == "https://cdn.example/fresh-signed.mp4"


def test_verify_old_task_id_before_resubmit_adopts_completed(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get_failed(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(
                status_code=200,
                json_payload={"id": "job-1", "status": "failed", "error": "provider glitch"},
            )
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_failed)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )
    assert first["status"] == "error"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "failed"

    # The paid task actually finished at the provider after we gave up on it.
    verify_calls = []

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("must adopt the completed paid task, not resubmit a duplicate")

    def fake_get_completed(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            verify_calls.append(url)
            return _FakeResponse(
                status_code=200,
                json_payload={
                    "id": "job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://cdn.example/job-1.mp4"],
                    "usage": {"cost_rub": 5.0},
                },
            )
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"adopted")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get_completed)

    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert second["status"] == "success"
    # Exactly-once: the second run adopts job-1 via a single free GET, no new submit.
    assert len(post_calls) == 1
    assert len(verify_calls) == 1
    assert second["results"][0]["task_id"] == "job-1"
    assert Path(item["video_path"]).read_bytes() == b"adopted"
    assert _read_provider_jobs(shots_dir)[-1]["status"] == "downloaded"


def test_input_hash_ignores_catalog_size_and_duration(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    catalog_a = {
        "installed-model": {
            "min_price_per_second": 1,
            "max_price_per_second": 2,
            "supported_sizes": ["1920x1080"],
            "supported_resolutions": ["1080p"],
            "supported_aspect_ratios": ["16:9"],
            "supported_durations": [6],
            "supported_frame_images": ["first_frame", "last_frame"],
            "supports_seed": True,
        }
    }
    catalog_b = {
        "installed-model": {
            "min_price_per_second": 1,
            "max_price_per_second": 2,
            "supported_sizes": ["1280x720"],
            "supported_resolutions": ["720p"],
            "supported_aspect_ratios": ["16:9"],
            "supported_durations": [8],
            "supported_frame_images": ["first_frame", "last_frame"],
            "supports_seed": True,
        }
    }
    active_catalog = {"value": catalog_a}
    monkeypatch.setattr(
        aitunnel_module,
        "_get_aitunnel_video_models",
        lambda force_refresh=False: active_catalog["value"],
    )

    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        post_calls.append(url)
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    first = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )
    assert first["status"] == "success"
    hash_after_first = _read_provider_jobs(shots_dir)[0]["input_hash"]

    # The provider catalog now resolves a different size/duration for the same shot.
    active_catalog["value"] = catalog_b

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("M-6: a catalog change must not force a paid resubmit")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)

    second = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert second["status"] == "success"
    assert len(post_calls) == 1
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["input_hash"] == hash_after_first
    assert all(job["status"] != "stale" for job in jobs)


def test_input_hash_stores_resolved_params_as_metadata(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    # Requested 1920x1080 / 6s resolves to a different catalog size/duration.
    monkeypatch.setattr(
        aitunnel_module,
        "_get_aitunnel_video_models",
        lambda force_refresh=False: {
            "installed-model": {
                "min_price_per_second": 1,
                "max_price_per_second": 2,
                "supported_sizes": ["1280x720"],
                "supported_resolutions": ["720p"],
                "supported_aspect_ratios": ["16:9"],
                "supported_durations": [8],
                "supported_frame_images": ["first_frame", "last_frame"],
                "supports_seed": True,
            }
        },
    )

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, timeout
        captured["json"] = json
        return _FakeResponse(status_code=202, json_payload={"id": "job-1", "status": "pending"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-1"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-1", "https://cdn.example/job-1.mp4"))
        if url == "https://cdn.example/job-1.mp4":
            return _FakeResponse(status_code=200, content=b"video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", fake_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    # The provider actually received the catalog-resolved size/duration...
    assert captured["json"]["size"] == "1280x720"
    assert captured["json"]["duration"] == 8

    job = _read_provider_jobs(shots_dir)[0]
    # ...which is stored only as job metadata, never folded into the input hash.
    assert job["resolved_size_params"] == {"size": "1280x720"}
    assert job["resolved_duration"] == 8
    assert job["hash_inputs_version"] == 2

    expected_hash = aitunnel_module._build_input_hash(
        model_name="installed-model",
        prompt_hash=aitunnel_module._hash_text(item["video_prompt"]),
        source_image_hashes={
            "start_image": aitunnel_module._hash_source_image(item["start_image"]),
            "end_image": None,
        },
        requested_duration=6,
        requested_width=1920,
        requested_height=1080,
        seed=None,
        frame_types=["first_frame"],
    )
    assert job["input_hash"] == expected_hash


def test_legacy_v1_jobs_not_marked_stale_and_resume_by_shot_identity(tmp_path, monkeypatch):
    _patch_project_mode(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_project_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    # A legacy (pre-M-6) job: same shot identity, an old-formula input_hash and no
    # hash_inputs_version field. Its live paid task_id must be reused, not resubmitted.
    prompt_hash = aitunnel_module._hash_text(item["video_prompt"])
    source_hashes = {
        "start_image": aitunnel_module._hash_source_image(item["start_image"]),
        "end_image": None,
    }
    legacy_jobs = {
        "version": 1,
        "jobs": [
            {
                "shot_key": "1-1",
                "provider": "aitunnel",
                "model": "installed-model",
                "prompt_hash": prompt_hash,
                "source_image_hashes": source_hashes,
                "input_hash": "legacy-old-formula-hash",
                "task_id": "job-legacy",
                "status": "submitted",
                "cost": None,
                "currency": None,
                "output_path": item["video_path"],
                "video_url": None,
                "error": None,
                "timestamps": {"submitted_at": "2026-01-01T00:00:00Z"},
            }
        ],
    }
    (shots_dir / "provider_jobs.json").write_text(json.dumps(legacy_jobs), encoding="utf-8")

    def forbidden_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        raise AssertionError("legacy job must resume by shot identity, not resubmit a paid task")

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if url.endswith("/videos/job-legacy"):
            return _FakeResponse(status_code=200, json_payload=_completed_job_payload("job-legacy", "https://cdn.example/job-legacy.mp4"))
        if url == "https://cdn.example/job-legacy.mp4":
            return _FakeResponse(status_code=200, content=b"legacy-video")
        raise AssertionError(f"Unexpected GET url: {url}")

    monkeypatch.setattr(aitunnel_module.requests, "post", forbidden_post)
    monkeypatch.setattr(aitunnel_module.requests, "get", fake_get)

    result = aitunnel_module.video_generator_aitunnel_tool(
        session_id="session-1", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["results"][0]["task_id"] == "job-legacy"
    assert Path(item["video_path"]).read_bytes() == b"legacy-video"

    jobs = _read_provider_jobs(shots_dir)
    legacy_job = next(job for job in jobs if job.get("task_id") == "job-legacy")
    # M-6 compat: a hash-formula bump must never stale a legacy job (its task_id lives).
    assert legacy_job["status"] != "stale"
