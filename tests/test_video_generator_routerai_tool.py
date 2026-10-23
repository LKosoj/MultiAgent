import json

import pytest
from PIL import Image

from custom_tools.storybook import video_contract
from custom_tools.storybook import video_generator_aitunnel_tool as generator
from custom_tools.storybook import video_generator_common as common
from custom_tools.storybook.video_generator_routerai_tool import video_generator_routerai_tool


BASE = "https://routerai.ru/api/v1"
MODEL = "google/veo-3.1-fast"


class Response:
    def __init__(self, payload=None, status=200, content=b"video"):
        self.status_code = status
        self.payload = payload
        self.content = content
        self.text = "test response"

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.content


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROUTERAI_API_KEY", "router-key")
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", MODEL)
    monkeypatch.delenv("ROUTERAI_API_BASE", raising=False)
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None)
    monkeypatch.setattr(generator, "update_shots_with_descriptions", lambda *a, **kw: False)
    monkeypatch.setattr(generator.time, "sleep", lambda *a: None)
    monkeypatch.setattr(video_contract, "_active_video_tool_name", lambda: "video_generator_routerai_tool")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    shots = tmp_path / "plots/storybooks/router-project/97_shots"
    shots.mkdir(parents=True)
    for name in ("start.png", "end.png"):
        Image.new("RGB", (1280, 720)).save(shots / name)
    item = {
        "scene_number": 1, "shot_number": 1, "shot_type": "start",
        "video_prompt": "The camera pans right", "timing": "00:00 - 00:06",
        "start_image": str(shots / "start.png"), "end_image": str(shots / "end.png"),
        "width": 1280, "height": 720, "video_path": str(shots / "clip.mp4"),
    }
    (shots / "shots.json").write_text(json.dumps({"items": [item]}))
    record = {
        "id": MODEL,
        "architecture": {"output_modalities": ["video"], "input_modalities": ["text", "image", "video"]},
        "supported_durations": [4, 6, 8], "supported_resolutions": ["720p", "1080p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_frame_images": ["first_frame", "last_frame"], "seed": True,
    }
    return shots, item, record


def run_video(**kwargs):
    return video_generator_routerai_tool(
        session_id="test", project_id="router-project", enable=True, language="en", **kwargs,
    )


@pytest.mark.parametrize("external_url", [False, True])
def test_routerai_submit_poll_download_and_reuse(project, monkeypatch, external_url):
    shots, item, record = project
    submits, downloads, polls = [], [], []
    video_url = "https://cdn.example/video.mp4" if external_url else f"{BASE}/videos/task-1/content?index=0"

    def post(url, headers, json, timeout):
        assert url == f"{BASE}/videos"
        assert headers["Authorization"] == "Bearer router-key"
        submits.append(json)
        return Response({"id": "task-1", "status": "pending", "polling_url": f"{BASE}/videos/task-1"}, 202)

    def get(url, **kwargs):
        if url == f"{BASE}/models":
            return Response({"data": [record, {"id": "text-model", "architecture": {"output_modalities": ["text"]}}]})
        if url == f"{BASE}/videos/task-1":
            assert kwargs["headers"]["Authorization"] == "Bearer router-key"
            polls.append(url)
            if len(polls) == 1:
                return Response({"status": "in_progress"})
            return Response({"status": "completed", "unsigned_urls": [video_url], "usage": {"cost": 18.2}})
        assert url == video_url
        assert kwargs["headers"] == ({} if external_url else {"Authorization": "Bearer router-key"})
        downloads.append(url)
        return Response(content=b"mp4-result")

    monkeypatch.setattr(generator.requests, "post", post)
    monkeypatch.setattr(generator.requests, "get", get)
    result = run_video(seed=42)
    assert result["status"] == "success"
    assert result["results"][0]["cost_rub"] == 18.2
    assert submits[0]["model"] == MODEL
    assert submits[0]["duration"] == 6
    assert submits[0]["seed"] == 42
    assert submits[0]["resolution"] == "720p"
    assert submits[0]["aspect_ratio"] == "16:9"
    assert [f["frame_type"] for f in submits[0]["frame_images"]] == ["first_frame", "last_frame"]
    assert submits[0]["frame_images"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (shots / "clip.mp4").read_bytes() == b"mp4-result"
    job = json.loads((shots / "provider_jobs.json").read_text())["jobs"][0]
    assert (job["provider"], job["status"], job["currency"]) == ("routerai", "downloaded", "RUB")
    assert list(common._get_routerai_video_models()) == [MODEL]
    assert common._get_routerai_video_models()[MODEL]["supports_seed"] is True
    assert run_video(seed=42)["status"] == "success"
    assert len(submits) == len(downloads) == 1


def test_routerai_resume_after_poll_error_without_resubmission(project, monkeypatch):
    shots, item, record = project
    submits = []
    unavailable = True

    def get(url, **kwargs):
        if url.endswith("/models"):
            return Response({"data": [record]})
        if url.endswith("/content?index=0"):
            return Response()
        assert url == f"{BASE}/videos/task-1"
        return Response(status=503) if unavailable else Response({"status": "completed"})

    def post(url, **kwargs):
        submits.append(url)
        return Response({"id": "task-1", "status": "pending"}, 202)

    monkeypatch.setattr(generator.requests, "get", get)
    monkeypatch.setattr(generator.requests, "post", post)
    assert run_video()["status"] == "error"
    job = json.loads((shots / "provider_jobs.json").read_text())["jobs"][0]
    assert job["status"] == "poll_timeout"
    (shots / "start.png").unlink()
    (shots / "end.png").unlink()
    promise = video_contract.storybook_video_delivery_promise_tool("test", "router-project", language="en")
    assert promise["status"] == "success"
    unavailable = False
    assert run_video()["status"] == "success"
    assert len(submits) == 1
    assert generator._ProviderJobStore(str(shots / "provider_jobs.json")).has_job_for_shot("1-1") is False


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
def test_routerai_terminal_statuses(project, monkeypatch, status):
    monkeypatch.setattr(generator.requests, "get", lambda *a, **kw: Response({"status": status}))
    with pytest.raises(generator._ProviderGenerationError, match=status):
        generator._wait_for_video_completion_aitunnel("task-1", {}, BASE, provider_name="routerai")


def test_routerai_preflight_capabilities_and_invalid_model(project, monkeypatch):
    shots, item, record = project
    monkeypatch.setattr(generator.requests, "get", lambda *a, **kw: Response({"data": [record]}))
    preflight = video_contract.storybook_video_preflight_tool("test", "router-project")
    assert preflight["provider"] == "routerai"
    assert preflight["capabilities"]["video"] is True
    assert video_contract.storybook_video_delivery_promise_tool("test", "router-project")["status"] == "success"
    caps = common.resolve_video_model_capabilities("video_generator_routerai_tool", str(shots / "video_model_caps.json"))
    assert caps["model"] == MODEL
    assert caps["supported_durations"] == [4, 6, 8]
    monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "missing-model")
    caps = common.resolve_video_model_capabilities("video_generator_routerai_tool", str(shots / "video_model_caps.json"))
    assert caps["model"] is None
    promise = video_contract.storybook_video_delivery_promise_tool("test", "router-project")
    assert "video_input_invalid:1-1" in promise["blocking_reasons"]


@pytest.mark.parametrize("unsupported", ["model", "last_frame", "seed"])
def test_routerai_rejects_unsupported_inputs_before_submit(project, monkeypatch, unsupported):
    shots, item, record = project
    if unsupported == "model":
        monkeypatch.setenv("ROUTERAI_VIDEO_MODEL", "unknown")
    elif unsupported == "last_frame":
        record["supported_frame_images"] = ["first_frame"]
    else:
        record["seed"] = False
    monkeypatch.setattr(generator.requests, "get", lambda *a, **kw: Response({"data": [record]}))

    def unexpected_submit(*args, **kwargs):
        pytest.fail("Unsupported inputs must not create a paid job")

    monkeypatch.setattr(generator.requests, "post", unexpected_submit)
    assert run_video(seed=42)["status"] == "error"


def test_capabilities_cache_does_not_cross_provider_boundary(project, monkeypatch):
    shots, item, record = project
    caps_path = shots / "video_model_caps.json"
    monkeypatch.setattr(generator.requests, "get", lambda *a, **kw: Response({"data": [record]}))
    common.resolve_video_model_capabilities("video_generator_routerai_tool", str(caps_path))
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", MODEL)

    def unavailable():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(common, "_get_aitunnel_video_models", unavailable)
    caps = common.resolve_video_model_capabilities("video_generator_aitunnel_tool", str(caps_path))
    assert caps["source"] is None
    assert caps["supported_durations"] == []


@pytest.mark.parametrize("supports_video,exclusive,attached", [(True, False, True), (False, False, False), (True, True, False)])
def test_routerai_blockout_uses_input_references(project, monkeypatch, supports_video, exclusive, attached):
    shots, item, record = project
    record["frames_exclusive_with_references"] = exclusive
    if not supports_video:
        record["architecture"]["input_modalities"].remove("video")
    blockout = shots / "blockout.mp4"
    blockout.write_bytes(b"blockout")
    (shots / "manifest.json").write_text(json.dumps({"duration_s": 6, "resolution": [1280, 720]}))
    item["blockout_video"] = str(blockout)
    (shots / "shots.json").write_text(json.dumps({"items": [item]}))
    payloads = []

    def post(url, **kwargs):
        payloads.append(kwargs["json"])
        return Response({"id": "task-1"}, 202)

    def get(url, **kwargs):
        if url.endswith("/models"):
            return Response({"data": [record]})
        if "/content" in url:
            return Response()
        return Response({"status": "completed"})

    monkeypatch.setattr(generator.requests, "post", post)
    monkeypatch.setattr(generator.requests, "get", get)
    assert run_video(generate_blockout=True, use_blockout_reference=True)["status"] == "success"
    assert "reference_video" not in payloads[0]
    assert ("input_references" in payloads[0]) is attached
    if attached:
        assert payloads[0]["input_references"][0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert run_video(generate_blockout=True, use_blockout_reference=True)["status"] == "success"
    assert len(payloads) == 1


def test_routerai_disabled_and_missing_credentials(project, monkeypatch):
    assert video_generator_routerai_tool("test", enable=False)["status"] == "skipped"
    monkeypatch.delenv("ROUTERAI_API_KEY")
    assert "ROUTERAI_API_KEY" in run_video()["message"]
