import shutil
import subprocess

import pytest

from custom_tools.storybook import color_match, montage_assembler

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _clip(path, color):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s=160x96:r=24:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return {"path": str(path), "exists": True, "planned_duration_seconds": 1.0}


def _mean(path):
    return [mean for mean, _ in color_match._frame_stats("ffmpeg", str(path), 0.5)]


def test_scene_clips_are_pulled_toward_first_shot_within_limits(tmp_path):
    reference = dict(_clip(tmp_path / "ref.mp4", "0x606060"), scene_number=1)
    bright = dict(_clip(tmp_path / "bright.mp4", "0x707070"), scene_number=1)
    night = dict(_clip(tmp_path / "night.mp4", "0x101010"), scene_number=2)
    warnings = []

    mapping = color_match.match_scene_colors("ffmpeg", [reference, bright, night], tmp_path / "out", warnings)

    assert warnings == []
    assert set(mapping) == {str((tmp_path / "bright.mp4").resolve())}
    before, after, target = _mean(tmp_path / "bright.mp4"), _mean(mapping[str((tmp_path / "bright.mp4").resolve())]), _mean(tmp_path / "ref.mp4")
    assert all(abs(a - t) < abs(b - t) for a, b, t in zip(after, before, target))
    again = color_match.match_scene_colors("ffmpeg", [reference, bright, night], tmp_path / "out", warnings)
    assert again == mapping


def test_correction_shift_is_clamped():
    stats = [(200.0, 10.0)] * 3
    reference = [(20.0, 10.0)] * 3
    gain, offset = color_match._channel_correction(stats[0], reference[0])
    assert gain == 1.0 and gain * 200 + offset == 200 - color_match._SHIFT_LIMIT
    assert color_match.color_match_filter(reference, reference) is None


def test_broken_clip_is_reported_and_left_unchanged(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    warnings = []
    assert color_match.match_scene_colors("ffmpeg", [{"path": str(broken), "scene_number": 1}], tmp_path, warnings) == {}
    assert warnings and warnings[0].startswith("color_match_skipped:broken.mp4")


def test_montage_filter_adds_film_grain_on_top():
    graph = montage_assembler._build_video_filter([{"planned_start_seconds": 0, "planned_end_seconds": 1,
                                                     "planned_duration_seconds": 1}])
    assert graph.endswith(f"concat=n=1:v=1:a=0,{color_match.FILM_GRAIN_FILTER}[vout]")
