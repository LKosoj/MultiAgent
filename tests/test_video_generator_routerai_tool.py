import json
import threading

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
    monkeypatch.delenv("ROUTERAI_RESOLUTION", raising=False)
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
        assert timeout == 1200
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


def test_routerai_test_resolution_and_sample_preparation_are_scoped(project, monkeypatch):
    shots, item, record = project
    monkeypatch.setenv("ROUTERAI_RESOLUTION", "480p")
    record["supported_resolutions"].append("480p")
    record["supported_sizes"] = ["1920x1080", "854x480"]
    item.update(width=1920, height=1088)
    other = dict(item, scene_number=2, video_path=str(shots / "other.mp4"))
    (shots / "shots.json").write_text(json.dumps({"items": [item, other]}))
    prepared, payloads = [], []
    monkeypatch.setattr(generator, "update_shots_with_descriptions", lambda path, items, **kwargs: prepared.extend(items) or 0)

    def get(url, **kwargs):
        if url.endswith("/models"):
            return Response({"data": [record]})
        return Response({"status": "completed"})

    def post(url, **kwargs):
        payloads.append(kwargs["json"])
        return Response({"id": "test-only"}, 202)

    monkeypatch.setattr(generator.requests, "get", get)
    monkeypatch.setattr(generator.requests, "post", post)
    result = run_video(sample_before_batch=True, sample_shot_key="1-1")
    assert result["status"] == "success"
    assert [(i["scene_number"], i["shot_number"]) for i in prepared] == [(1, 1)]
    assert len(payloads) == 1
    assert payloads[0]["resolution"] == "480p"
    assert payloads[0]["aspect_ratio"] == "16:9"
    assert "size" not in payloads[0]
    assert not (shots / "other.mp4").exists()


def test_routerai_resolution_is_part_of_requested_input_hash(monkeypatch):
    args = dict(model_name=MODEL, prompt_hash="prompt", source_image_hashes={},
                requested_duration=6, requested_width=1920, requested_height=1088,
                seed=None, frame_types=["first_frame", "last_frame"], provider_name="routerai")
    monkeypatch.setenv("ROUTERAI_RESOLUTION", "480p")
    low = generator._build_input_hash(**args)
    monkeypatch.setenv("ROUTERAI_RESOLUTION", "720p")
    assert generator._build_input_hash(**args) != low


def test_seedance_documented_capabilities_are_stored_in_code(monkeypatch):
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None)
    record = {"id": "bytedance/seedance-1-5-pro", "architecture": {"output_modalities": ["video"]},
              "supports_reference_images": True, "max_image_references": None}
    monkeypatch.setattr(common.requests, "get", lambda *args, **kwargs: Response({"data": [record]}))
    config = common._get_routerai_video_models()[record["id"]]
    assert config["supported_durations"] == list(range(4, 13))
    assert config["supported_resolutions"] == ["480p", "720p", "1080p"]
    assert config["supported_frame_images"] == ["first_frame", "last_frame"]
    assert config["supports_image_references"] is True
    assert config["max_image_references"] == 2


def test_grok_reference_rejection_overrides_advertised_support(monkeypatch):
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None)
    record = {"id": "x-ai/grok-imagine-video-1.5-lite",
              "architecture": {"output_modalities": ["video"]}, "supports_reference_images": True}
    monkeypatch.setattr(common.requests, "get", lambda *a, **kw: Response({"data": [record]}))
    assert common._get_routerai_video_models()[record["id"]]["supports_image_references"] is False


def test_prepared_video_prompt_skips_image_analysis(project, monkeypatch):
    shots, item, record = project
    payloads = []
    monkeypatch.setattr(generator, "update_shots_with_descriptions", lambda *args, **kwargs: pytest.fail("Prepared prompts must not re-analyze images"))
    monkeypatch.setattr(generator.requests, "get", lambda url, **kwargs: Response({"data": [record]}) if url.endswith("/models") else Response({"status": "completed"}))

    def post(url, **kwargs):
        payloads.append(kwargs["json"])
        return Response({"id": "prepared-prompt"}, 202)

    monkeypatch.setattr(generator.requests, "post", post)
    result = run_video(skip_prompt_enhancement=True, sample_before_batch=True, sample_shot_key="1-1")
    assert result["status"] == "success"
    assert len(payloads) == 1
    assert payloads[0]["prompt"] == item["video_prompt"]


def test_wan_documented_capabilities_are_stored_in_code(monkeypatch):
    monkeypatch.setattr(common, "_ROUTERAI_MODELS_CACHE", None)
    record = {"id": "alibaba/wan-3.0", "architecture": {"output_modalities": ["video"]}}
    monkeypatch.setattr(common.requests, "get", lambda *args, **kwargs: Response({"data": [record]}))
    config = common._get_routerai_video_models()[record["id"]]
    assert config["supported_durations"] == list(range(2, 31))
    assert config["supported_resolutions"] == ["480p", "720p", "1080p"]
    assert config["supported_frame_images"] == ["first_frame", "last_frame"]
    assert config["supported_aspect_ratios"] == ["16:9", "4:3", "1:1", "3:4", "9:16"]


def test_routerai_default_runs_four_video_jobs_in_parallel(project, monkeypatch):
    shots, item, record = project
    items = [dict(item, shot_number=n, video_path=str(shots / f"clip{n}.mp4"), video_prompt=f"Camera movement {n}") for n in range(1, 5)]
    (shots / "shots.json").write_text(json.dumps({"items": items}))
    barrier = threading.Barrier(4)
    submitted = []

    def post(url, **kwargs):
        task = kwargs["json"]["prompt"].split()[-1]
        submitted.append(task)
        barrier.wait(timeout=5)
        return Response({"id": task}, 202)

    monkeypatch.setattr(generator.requests, "post", post)
    monkeypatch.setattr(generator.requests, "get", lambda url, **kwargs: Response({"data": [record]}) if url.endswith("/models") else Response({"status": "completed"}))
    result = run_video(skip_prompt_enhancement=True)
    assert result["stats"] == {"total": 4, "successful": 4, "failed": 0}
    assert len(submitted) == 4


def test_selected_video_scope_never_touches_other_clips(project, monkeypatch):
    shots, item, record = project
    protected = shots / 'user_replaced.mp4'
    protected.write_bytes(b'user-video')
    other = dict(item, shot_number=2, video_prompt='Do not submit', video_path=str(protected))
    (shots / 'shots.json').write_text(json.dumps({'items': [item, other]}))
    submits = []

    def post(url, **kwargs):
        submits.append(kwargs['json'])
        return Response({'id': 'selected-only'}, 202)

    monkeypatch.setattr(generator.requests, 'post', post)
    monkeypatch.setattr(generator.requests, 'get', lambda url, **kwargs:
                        Response({'data': [record]}) if url.endswith('/models') else Response({'status': 'completed'}))
    result = run_video(shot_keys=['1-1'], skip_prompt_enhancement=True)
    assert result['stats']['total'] == 1
    assert len(submits) == 1 and submits[0]['prompt'] == item['video_prompt']
    assert protected.read_bytes() == b'user-video'
    jobs = json.loads((shots / 'provider_jobs.json').read_text())['jobs']
    assert {job['shot_key'] for job in jobs} == {'1-1'}


def test_fallback_and_cycles_are_loaded_from_environment(project, monkeypatch):
    shots, item, record = project
    monkeypatch.setenv("ROUTERAI_VIDEO_FALLBACK_MODEL", "vendor/backup-video")
    monkeypatch.setenv("ROUTERAI_VIDEO_RETRY_CYCLES", "2")
    monkeypatch.setattr(generator, "_get_routerai_video_models", lambda: {})
    calls = []

    def generate(item, **params):
        calls.append(params["configured_model"])
        return dict(success=len(calls) == 4, scene_number=1, shot_number=1,
                    video_path=item["video_path"], model=params["configured_model"])

    monkeypatch.setattr(generator, "_generate_single_video_aitunnel", generate)
    result = run_video()
    assert result["status"] == "success"
    assert calls == [MODEL, "vendor/backup-video"] * 2


def test_tool_parameters_override_fallback_environment(project, monkeypatch):
    monkeypatch.setenv("ROUTERAI_VIDEO_FALLBACK_MODEL", "unused/backup")
    monkeypatch.setenv("ROUTERAI_VIDEO_RETRY_CYCLES", "9")
    monkeypatch.setattr(generator, "_get_routerai_video_models", lambda: {})
    calls = []

    def generate(item, **params):
        calls.append(params["configured_model"])
        return dict(success=False, scene_number=1, shot_number=1,
                    video_path=item["video_path"], model=params["configured_model"])

    monkeypatch.setattr(generator, "_generate_single_video_aitunnel", generate)
    result = run_video(retry_cycles=1, fallback_model="chosen/backup")
    assert result["status"] == "error"
    assert calls == [MODEL, "chosen/backup"]


def test_keyframe_generation_falls_back_and_reuses_downloaded_video(project, monkeypatch):
    shots, item, record = project
    backup = "vendor/backup-video"
    monkeypatch.setenv("ROUTERAI_VIDEO_FALLBACK_MODEL", backup)
    monkeypatch.setenv("ROUTERAI_VIDEO_RETRY_CYCLES", "2")
    submitted = []

    def post(url, **kwargs):
        submitted.append(kwargs["json"]["model"])
        if len(submitted) < 4:
            return Response(status=504)
        return Response({"id": "backup-task", "status": "pending"}, 202)

    def get(url, **kwargs):
        if url.endswith("/models"):
            return Response({"data": [record, dict(record, id=backup)]})
        if "/content" in url:
            return Response(content=b"backup-video")
        return Response({"status": "completed"})

    monkeypatch.setattr(generator.requests, "post", post)
    monkeypatch.setattr(generator.requests, "get", get)
    result = run_video()
    assert result["status"] == "success"
    assert submitted == [MODEL, backup] * 2
    assert (shots / "clip.mp4").read_bytes() == b"backup-video"
    assert run_video()["status"] == "success"
    assert len(submitted) == 4
