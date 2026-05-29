import json
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
