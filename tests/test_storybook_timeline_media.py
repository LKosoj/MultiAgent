import json
import shutil
import subprocess
import wave
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook.screenplay_shots_generator_utils import fcpxml_generator as fg


def _seconds(value):
    return float(Fraction(value.rstrip("s")))


def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "plots/storybooks/test"
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(base.parent))
    audio = base / "98_audio"
    audio.mkdir(parents=True)
    for name in ["reader", "music", "sfx"]:
        channels = 2 if name == "music" else 1
        with wave.open(str(audio / f"{name}.wav"), "wb") as f:
            f.setparams((channels, 2, 8000, 0, "NONE", "not compressed"))
            f.writeframes(b"\x00\x00" * 8000 * channels)
    (audio / "audio_manifest.json").write_text(json.dumps({"audio_tracks": [
        {"role": "narration", "path": "reader.wav"},
        {"role": "music", "path": "music.wav"},
        {"role": "sfx", "scene_number": 2, "path": "sfx.wav"},
    ]}))
    items = []
    for scene, timing in [(1, "00:02"), (2, "00:03")]:
        for phase in ["start", "end"]:
            path = base / f"97_shots/scene_{scene:02d}_shot_01/img_final_{phase}_{scene:02d}_01.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (160, 96), "red" if phase == "start" else "blue").save(path)
            items.append({"scene_number": scene, "shot_number": 1, "shot_type": phase,
                          "timing": timing, "output_path": str(path), "width": 160, "height": 96})
    (base / "97_shots/shots.json").write_text(json.dumps({"items": items}))
    return base, items


@pytest.mark.parametrize("photo", [False, True])
def test_export_has_full_duration_and_separate_audio(tmp_path, monkeypatch, photo):
    base, items = _project(tmp_path, monkeypatch)
    path = base / "97_shots" / ("photo_shots_timeline.fcpxml" if photo else "shots_timeline.fcpxml")
    (fg._generate_photo_fcpxml if photo else fg._generate_fcpxml)("test", items, str(path))
    root = ET.parse(path).getroot()
    assert _seconds(root.find(".//sequence").get("duration")) == 5
    sounds = [clip for clip in root.findall(".//asset-clip") if clip.get("audioRole")]
    assert {clip.get("audioRole").split(".")[0] for clip in sounds} == {"dialogue", "music", "effects"}
    assert len({clip.get("lane") for clip in sounds}) == 3
    assert {int(f.get("height")) for f in root.findall("./resources/format")} == {96}
    assert (path.parent / "fix_timeline_paths.sh").is_file()


def test_relocation_works_from_copied_directory_without_project_settings(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    folder = base / "97_shots"
    fg._generate_photo_fcpxml("test", items, str(folder / "photo_shots_timeline.fcpxml"))
    fg._generate_fcpxml("test", items, str(folder / "shots_timeline.fcpxml"))
    copied = tmp_path / "Другая папка" / "Другой проект"
    shutil.copytree(base, copied)
    subprocess.run(["sh", "fix_timeline_paths.sh"], cwd=copied / "97_shots", check=True, capture_output=True)
    from urllib.parse import unquote, urlsplit
    for name in ["photo_shots_timeline.fcpxml", "shots_timeline.fcpxml"]:
        for media in ET.parse(copied / "97_shots" / name).findall(".//media-rep"):
            target = Path(unquote(urlsplit(media.get("src")).path))
            assert target.is_relative_to(copied)
            assert target.is_file() or target.parent.is_dir()
    # Running the same generic script again must keep valid relocated paths.
    subprocess.run(["sh", "fix_timeline_paths.sh"], cwd=copied / "97_shots", check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg is required")
def test_ffmpeg_photo_and_motion_render_and_content_invalidation(tmp_path, monkeypatch):
    from custom_tools.storybook.timeline_media import render_fcpxml
    base, items = _project(tmp_path, monkeypatch)
    photo = base / "97_shots/photo_shots_timeline.fcpxml"
    fg._generate_photo_fcpxml("test", items, str(photo))
    target = base / "99_final/photos.mp4"
    assert not render_fcpxml(photo, target)["cached"]
    assert render_fcpxml(photo, target)["cached"]
    # Re-exporting unchanged inputs must not force an expensive rerender.
    fg._generate_photo_fcpxml("test", items, str(photo))
    assert render_fcpxml(photo, target)["cached"]
    Image.new("RGB", (160, 96), "green").save(items[0]["output_path"])
    assert not render_fcpxml(photo, target)["cached"]
    audio = base / "98_audio/sfx.wav"
    with wave.open(str(audio), "wb") as f:
        f.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        f.writeframes(b"\x01\x00" * 8000)
    assert not render_fcpxml(photo, target)["cached"]
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)]))
    assert abs(float(probe["format"]["duration"]) - 5) < 0.05
    assert [s["codec_type"] for s in probe["streams"]] == ["video", "audio"]
    assert probe["streams"][1]["channels"] == 2
    motion_items = []
    for item in items:
        video = Path(item["output_path"]).parent / f"video_final_{item['scene_number']:02d}_01.mp4"
        shutil.copyfile(target, video)
        motion_items.append(dict(item, video_path=str(video)))
    motion = base / "97_shots/shots_timeline.fcpxml"
    fg._generate_fcpxml("test", motion_items, str(motion))
    assert not render_fcpxml(motion, base / "99_final/motion.mp4")["cached"]
    assert render_fcpxml(motion, base / "99_final/motion.mp4")["cached"]
    tree = ET.parse(motion)
    tree.find(".//asset-clip[@audioRole='effects']").set("offset", "225/100s")
    tree.write(motion)
    assert not render_fcpxml(motion, base / "99_final/motion.mp4")["cached"]
    # Replacing the video bytes, without changing any JSON metadata, invalidates assembly.
    source_video = Path(motion_items[0]["video_path"])
    source_video.write_bytes(source_video.read_bytes() + b"\n")
    assert not render_fcpxml(motion, base / "99_final/motion.mp4")["cached"]


def test_timelines_follow_matching_fcpxml_dtd(tmp_path, monkeypatch):
    dtd_path = Path("/tmp/FCPXMLv1_11.dtd")
    if not dtd_path.is_file():
        pytest.skip("Matching Apple FCPXML DTD not installed")
    from lxml import etree
    base, items = _project(tmp_path, monkeypatch)
    dtd = etree.DTD(str(dtd_path))
    for function, name in [(fg._generate_photo_fcpxml, "photo"), (fg._generate_fcpxml, "motion")]:
        path = base / "97_shots" / f"{name}.fcpxml"
        function("test", items, str(path))
        assert dtd.validate(etree.parse(str(path))), str(dtd.error_log)


def test_motion_montage_uses_complete_exported_timeline(tmp_path, monkeypatch):
    from custom_tools.storybook import montage_assembler, timeline_media
    base, _ = _project(tmp_path, monkeypatch)
    path = base / "97_shots/shots_timeline.fcpxml"
    path.write_text("<timeline/>")
    calls = []
    monkeypatch.setattr(timeline_media, "render_fcpxml", lambda *args, **kwargs: calls.append((args, kwargs)) or {"returncode": 0})
    result = montage_assembler._render_final_video("ffmpeg", [], [], base / "99_final/final_video.mp4", 5, base_dir=base)
    assert result["returncode"] == 0
    assert calls[0][0][0] == path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_motion_timeline_records_native_source_format(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    source = base / "native.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=160x90:r=30:d=2",
        "-c:v", "libx264", str(source),
    ], check=True)
    items = [dict(item, video_path=str(source)) for item in items]
    path = base / "97_shots/shots_timeline.fcpxml"
    fg._generate_fcpxml("test", items, str(path))
    root = ET.parse(path).getroot()
    sequence = root.find(".//sequence")
    timeline_format = root.find(f"./resources/format[@id='{sequence.get('format')}']")
    assert timeline_format.get("frameDuration") == "1/24s"
    for clip in root.findall(".//spine/asset-clip"):
        asset = root.find(f"./resources/asset[@id='{clip.get('ref')}']")
        source_format = root.find(f"./resources/format[@id='{asset.get('format')}']")
        assert source_format.get("frameDuration") == "1/30s"
        assert source_format.get("height") == "90"
        assert clip.find("conform-rate").get("srcFrameRate") is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
@pytest.mark.parametrize("provider", ["routerai", "aitunnel"])
def test_unrequested_audio_is_disabled_in_timeline_and_render(tmp_path, monkeypatch, provider):
    from custom_tools.storybook.timeline_media import render_fcpxml
    base, items = _project(tmp_path, monkeypatch)
    first = base / "97_shots/first.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=160x96:r=24:d=2",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=8000:duration=3",
        "-c:v", "libx264", "-c:a", "aac", str(first),
    ], check=True)
    second = base / "97_shots/second.mp4"
    shutil.copyfile(first, second)
    items = [dict(item, video_path=str(first if item["scene_number"] == 1 else second)) for item in items]
    (base / "97_shots/provider_jobs.json").write_text(json.dumps({"jobs": [
        {"output_path": str(p), "provider": provider, "status": "downloaded"}
        for p in (first, second)
    ]}))
    timeline = base / "97_shots/shots_timeline.fcpxml"
    fg._generate_fcpxml("test", items, str(timeline))
    root = ET.parse(timeline).getroot()
    clips = root.findall(".//spine/asset-clip")
    assert all(c.get("srcEnable") == "video" for c in clips)
    assert clips[0].find("timeMap") is None
    output = base / "99_final/silent.mp4"
    report = render_fcpxml(timeline, output)
    assert report["audio_clip_count"] == len(root.findall(".//asset-clip[@audioRole]"))
    volume = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(output), "-af", "volumedetect", "-vn", "-f", "null", "-",
    ], capture_output=True, text=True, check=True)
    assert "max_volume: -91.0 dB" in volume.stderr


def test_motion_timeline_keeps_clip_end_and_audio_anchor_when_retimed(tmp_path, monkeypatch):
    from custom_tools.storybook.timeline_media import render_fcpxml
    base, items = _project(tmp_path, monkeypatch)
    source = base / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=160x96:r=24:d=3.5",
        "-f", "lavfi", "-i", "color=blue:s=160x96:r=24:d=0.5", "-filter_complex_threads", "1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-an",
        "-c:v", "libx264", "-threads", "1", str(source),
    ], check=True)
    items = [dict(item, video_path=str(source)) for item in items]
    path = base / "97_shots/shots_timeline.fcpxml"
    fg._generate_fcpxml("test", items, str(path))
    root = ET.parse(path).getroot()
    clips = root.findall(".//spine/asset-clip")
    assert _seconds(clips[0].find("timeMap/timept[last()]").get("value")) == 4
    assert _seconds(clips[0].find("timeMap/timept[last()]").get("time")) == 2
    assert clips[0].find("asset-clip[@audioRole='effects']") is None
    assert _seconds(clips[1].find("asset-clip[@audioRole='effects']").get("offset")) == 0
    if Path("/tmp/FCPXMLv1_11.dtd").is_file():
        from lxml import etree
        dtd = etree.DTD("/tmp/FCPXMLv1_11.dtd")
        assert dtd.validate(etree.parse(str(path))), str(dtd.error_log)
    output = base / "99_final/retimed.mp4"
    render_fcpxml(path, output)
    frame = tmp_path / "end.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.9", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        red, green, blue = image.convert("RGB").getpixel((80, 48))
        assert blue > 200 and red < 50
