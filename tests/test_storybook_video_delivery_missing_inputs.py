import json
from pathlib import Path

from custom_tools.storybook import video_contract


def _write_shots(root: Path, items):
    shots_dir = root / "plots" / "storybooks" / "project-1" / "97_shots"
    shots_dir.mkdir(parents=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")


def _configure_aitunnel(monkeypatch):
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-test")
    monkeypatch.setenv("AITUNNEL_VIDEO_MODEL", "video-model")
    monkeypatch.setattr(video_contract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        video_contract,
        "_get_aitunnel_video_models",
        lambda: {
            "video-model": {
                "supported_frame_images": ["first_frame", "last_frame"],
                "supported_sizes": ["1920x1080"],
                "supported_durations": [6],
                "supports_seed": True,
            }
        },
    )


def _frame(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"frame")


def test_delivery_blocks_missing_start_clip_when_another_clip_is_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _configure_aitunnel(monkeypatch)
    start = tmp_path / "frames" / "start.png"
    _frame(start)
    _write_shots(tmp_path, [
        {
            "scene_number": 1, "shot_number": 1, "shot_type": "start", "video_prompt": "pan",
            "video_path": str(tmp_path / "video" / "1.mp4"), "start_image": str(start),
            "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
        },
        {
            "scene_number": 1, "shot_number": 2, "shot_type": "start", "video_prompt": "tilt",
            "video_path": str(tmp_path / "video" / "2.mp4"), "start_image": str(tmp_path / "frames" / "missing.png"),
            "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
        },
    ])

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is False
    assert result["expected_video_count"] == 2
    assert result["blocking_reasons"] == ["video_input_invalid:1-2"]


def test_delivery_blocks_stale_output_without_confirmed_resume_or_start_frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _configure_aitunnel(monkeypatch)
    stale_video = tmp_path / "video" / "stale.mp4"
    _frame(stale_video)
    _write_shots(tmp_path, [{
        "scene_number": 1, "shot_number": 1, "shot_type": "start", "video_prompt": "pan",
        "video_path": str(stale_video), "start_image": str(tmp_path / "frames" / "removed.png"),
        "width": 1920, "height": 1080, "timing": "00:00 - 00:06",
    }])

    result = video_contract.storybook_video_delivery_promise_tool("session-1", "project-1", language="en")

    assert result["will_generate_video"] is False
    assert result["blocking_reasons"] == ["video_input_invalid:1-1"]
