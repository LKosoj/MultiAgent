"""Shared media handling for editable FCPXML timelines and ffmpeg assembly."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlsplit

from custom_tools.storybook.project_paths import safe_storybook_project_dir
from custom_tools.storybook.video_generator_common import parse_duration_seconds_from_timing


def timeline_items(items):
    result = []
    for item in items:
        duration = item.get("duration_s") or parse_duration_seconds_from_timing(item.get("timing", "5s"))
        result.append(dict(item, duration_s=float(duration)))
    return sorted(result, key=lambda i: (int(i["scene_number"]), int(i["shot_number"]), i.get("shot_type") != "start"))


def _seconds(value):
    return float(Fraction(str(value).removesuffix("s")))


def _time(value):
    return f"{round(float(value) * 6000)}/6000s"


def _read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _probe_media(path):
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], capture_output=True, text=True, check=True, timeout=120)
    return json.loads(result.stdout)


def _probe_audio(path):
    probe = _probe_media(path)
    stream = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    return float(probe["format"]["duration"]), stream


def _retime_ratio(clip):
    points = clip.findall("timeMap/timept")
    if not points:
        return 1.0
    source = _seconds(points[-1].get("value")) - _seconds(points[0].get("value"))
    target = _seconds(points[-1].get("time")) - _seconds(points[0].get("time"))
    return source / target


def _audio_segments(base, items, total):
    audio_dir = base / "98_audio"
    manifest = _read_json(audio_dir / "audio_manifest.json", {})
    plan = _read_json(audio_dir / "music_plan.json", {})
    music = _read_json(audio_dir / "music_manifest.json", {})
    scenes, seen, position = {}, set(), 0.0
    for item in items:
        key = (item["scene_number"], item["shot_number"])
        if key in seen:
            continue
        seen.add(key)
        scene = scenes.setdefault(str(key[0]), {"start": position, "duration": 0})
        scene["duration"] += item["duration_s"]
        position += item["duration_s"]
    segments, probes = [], {}

    def add(track, offset, duration):
        path = Path(track["path"])
        if not path.is_absolute():
            path = audio_dir / path
        path = path.resolve()
        if path not in probes:
            probes[path] = _probe_audio(path)
        source_duration, stream = probes[path]
        source_start = float(track.get("source_start_seconds") or 0)
        remaining = min(float(duration), total - offset)
        while remaining > 0.0001:
            length = min(remaining, source_duration - source_start)
            if length <= 0:
                raise ValueError(f"Audio source interval is empty: {path}")
            segments.append(dict(track, path=str(path), offset=offset, duration=length,
                                 source_start=source_start, source_duration=source_duration, stream=stream))
            if track.get("role") != "music":
                break
            remaining -= length
            offset += length
            source_start = 0

    mapping = plan.get("scene_mapping") or {}
    planned_music = bool(mapping and music)
    for raw in manifest.get("audio_tracks") or []:
        track = {"path": raw, "role": "audio"} if isinstance(raw, str) else raw
        if planned_music and track.get("role") == "music":
            continue
        scene = scenes.get(str(track.get("scene_number")))
        if track.get("scene_number") is not None and scene is None:
            continue
        offset = float(track.get("offset_seconds") or (scene["start"] if scene else 0))
        length = float(track.get("duration_seconds") or (scene["duration"] if scene else total - offset))
        add(track, offset, length)
    if planned_music:
        for scene_id, scene in scenes.items():
            music_id = mapping.get(scene_id)
            if music_id is None:
                continue
            entry = music.get(music_id)
            if entry is None:
                raise ValueError(f"Missing mapped music asset: {music_id}")
            track = {"path": entry} if isinstance(entry, str) else entry
            add(dict(track, role="music"), scene["start"], scene["duration"])
    return segments


def decorate_timeline(root, path, project_id, items):
    """Attach individual sound clips; never bake or merge editable XML tracks."""
    base = safe_storybook_project_dir(project_id)
    resources = root.find("resources")
    sequence = root.find(".//sequence")
    visual_clips = [c for c in sequence.find("spine") if c.tag in {"video", "asset-clip"}]
    first = visual_clips[0] if visual_clips else None
    source_durations = {}
    jobs = _read_json(base / "97_shots/provider_jobs.json", {}).get("jobs", [])
    providers = {Path(job["output_path"]).resolve(): job.get("provider") for job in jobs
                 if job.get("status") == "downloaded" and job.get("output_path")}
    resources_by_id = {a.get("id"): a for a in resources.findall("asset")}
    for fmt in resources.findall("format"):
        fmt.set("width", str(items[0].get("width") or 1920))
        fmt.set("height", str(items[0].get("height") or 1088))
        if fmt.get("frameDuration") is not None:
            fmt.set("frameDuration", "1/24s")
    for asset in resources.findall("asset"):
        if asset.get("hasAudio") == "1" and asset.get("hasVideo") == "1":
            asset.set("hasAudio", "0")
            for name in ("audioSources", "audioChannels", "audioRate"):
                asset.attrib.pop(name, None)
            source = _media_path(asset)
            if source.is_file():
                probe = _probe_media(source)
                video = next(s for s in probe["streams"] if s["codec_type"] == "video")
                source_format = resources.find(f"format[@id='{asset.get('format')}']")
                source_format.set("width", str(video["width"]))
                source_format.set("height", str(video["height"]))
                source_format.set("frameDuration", f"{1 / Fraction(video['r_frame_rate'])}s")
                source_durations[asset.get("id")] = float(video.get("duration") or probe["format"]["duration"])
                asset.set("duration", _time(source_durations[asset.get("id")]))
                stream = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
                if stream:
                    asset.set("hasAudio", "1")
                    asset.set("audioSources", "1")
                    asset.set("audioChannels", str(stream["channels"]))
                    asset.set("audioRate", str(stream["sample_rate"]))
    for clip in visual_clips:
        conform = clip.find("conform-rate")
        if conform is not None:
            conform.attrib.pop("srcFrameRate", None)
        # Video providers receive generate_audio=false; ignore audio returned despite it.
        if providers.get(_media_path(resources_by_id[clip.get("ref")])) in {"routerai", "aitunnel"}:
            clip.set("srcEnable", "video")
        actual = source_durations.get(clip.get("ref"))
        planned = _seconds(clip.get("duration"))
        if actual is not None and abs(actual - planned) > 0.001:
            mapping = ET.SubElement(clip, "timeMap")
            ET.SubElement(mapping, "timept", time="0s", value="0s", interp="linear")
            ET.SubElement(mapping, "timept", time=_time(planned), value=_time(actual), interp="linear")
    root.find("library").attrib.pop("location", None)
    for element in root.findall(".//event") + root.findall(".//project"):
        element.set("uid", str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}/{element.tag}/{element.get('name')}")))
        element.attrib.pop("modDate", None)
    if first is not None:
        assets, lanes = {}, {}
        for track in _audio_segments(base, items, _seconds(sequence.get("duration"))):
            key = (track.get("role"), track["path"])
            if key not in assets:
                asset_id = f"audio{len(assets) + 1}"
                assets[key] = asset_id
                lanes[key] = str(-len(assets))
                stream = track["stream"]
                asset = ET.SubElement(resources, "asset", id=asset_id, name=Path(track["path"]).name,
                                      start="0s", duration=_time(track["source_duration"]), hasAudio="1",
                                      audioSources="1", audioChannels=str(stream["channels"]),
                                      audioRate=str(stream["sample_rate"]))
                ET.SubElement(asset, "media-rep", kind="original-media", src=Path(track["path"]).as_uri())
            role = {"narration": "dialogue.narration", "sfx": "effects"}.get(track.get("role"), track.get("role") or "dialogue")
            anchor = next(c for c in reversed(visual_clips) if _seconds(c.get("offset", "0s")) <= track["offset"])
            local_offset = (track["offset"] - _seconds(anchor.get("offset", "0s"))) * _retime_ratio(anchor) + _seconds(anchor.get("start", "0s"))
            clip = ET.Element("asset-clip", ref=assets[key], name=Path(track["path"]).stem,
                              lane=lanes[key], offset=_time(local_offset), start=_time(track["source_start"]),
                              duration=_time(track["duration"]), audioRole=role)
            before = next((index for index, child in enumerate(anchor)
                           if child.tag in {"keyword", "marker", "rating"}), len(anchor))
            anchor.insert(before, clip)
    script = Path(path).parent / "fix_timeline_paths.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("fix_timeline_paths.sh"), script)
    script.chmod(0o755)


def _media_path(asset):
    return Path(unquote(urlsplit(asset.find("media-rep").get("src")).path))


def render_fcpxml(timeline_path, output_path, ffmpeg_bin="ffmpeg"):
    """Render the exported timeline, rebuilding when XML or referenced bytes change."""
    timeline_path, output_path = Path(timeline_path), Path(output_path)
    root = ET.parse(timeline_path).getroot()
    resources = {a.get("id"): a for a in root.findall("./resources/asset")}
    sequence = root.find(".//sequence")
    duration = _seconds(sequence.get("duration"))
    fmt = root.find(f"./resources/format[@id='{sequence.get('format')}']")
    width, height = int(fmt.get("width")), int(fmt.get("height"))
    rate = 1 / _seconds(fmt.get("frameDuration", "1/24s"))
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(timeline_path.read_bytes())
    for path in sorted({_media_path(a) for a in resources.values()}):
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    input_hash = digest.hexdigest()
    report_path = output_path.with_suffix(".render.json")
    previous = _read_json(report_path, {})
    if previous.get("input_hash") == input_hash and output_path.is_file():
        if previous.get("output_hash") == hashlib.sha256(output_path.read_bytes()).hexdigest():
            return {"returncode": 0, "cached": True, "output_path": str(output_path)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    spine = list(sequence.find("spine"))
    groups, index = [], 0
    while index < len(spine):
        clip = spine[index]
        if clip.tag not in {"video", "asset-clip"}:
            raise ValueError(f"Unsupported timeline element: {clip.tag}")
        group = [clip]
        if index + 1 < len(spine) and spine[index + 1].tag == "transition":
            group.extend(spine[index + 1:index + 3])
        groups.append(group)
        index += len(group)
    sounds = [c for c in root.findall(".//asset-clip") if c.get("audioRole")]
    sounds.extend(c for c in spine if c.tag == "asset-clip" and not c.get("audioRole")
                  and c.get("srcEnable") != "video"
                  and resources[c.get("ref")].get("hasAudio") == "1")
    parents = {child: parent for parent in root.iter() for child in parent}

    def run(command):
        completed = subprocess.run(command, capture_output=True, text=True, timeout=86400)
        if completed.returncode:
            raise RuntimeError(completed.stderr[-6000:])
        return completed

    with tempfile.TemporaryDirectory(prefix="timeline_", dir=output_path.parent) as temporary:
        temporary = Path(temporary)

        def render_group(index_group):
            number, group = index_group
            command = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1"]
            filters, lengths = [], []
            transition = _seconds(group[1].get("duration")) if len(group) == 3 else 0
            visual = [group[0], group[-1]] if len(group) == 3 else group
            for n, clip in enumerate(visual):
                path = _media_path(resources[clip.get("ref")])
                length = _seconds(clip.get("duration")) + transition
                lengths.append(length)
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    command += ["-loop", "1", "-framerate", str(rate)]
                else:
                    command += ["-ss", str(_seconds(clip.get("start", "0s")))]
                command += ["-i", str(path)]
                retime = _retime_ratio(clip)
                filters.append(f"[{n}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                               f"setpts=(PTS-STARTPTS)/{retime},fps={rate}:round=down,"
                               f"format=yuv420p,settb=AVTB,tpad=stop_mode=clone:stop_duration={length},"
                               f"trim=duration={length},setpts=PTS-STARTPTS[v{n}]")
            if transition:
                filters.append(f"[v0][v1]xfade=transition=fade:duration={transition}:offset={lengths[0]-transition}[v]")
            label = "[v]" if transition else "[v0]"
            target = temporary / f"{number:04d}.mp4"
            command += ["-filter_complex", ";".join(filters), "-map", label, "-an", "-c:v", "libx264",
                        "-threads", "2", "-preset", "veryfast", "-crf", "18", "-video_track_timescale", "24000", str(target)]
            run(command)
            return target

        with ThreadPoolExecutor(max_workers=4) as executor:
            chunks = list(executor.map(render_group, enumerate(groups)))
        concat_path = temporary / "concat.txt"
        concat_path.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in chunks))
        command = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1",
                   "-f", "concat", "-safe", "0", "-i", str(concat_path)]
        filters = []
        for n, clip in enumerate(sounds, 1):
            command += ["-i", str(_media_path(resources[clip.get("ref")]))]
            offset = _seconds(clip.get("offset", "0s"))
            parent = parents[clip]
            if parent.tag in {"video", "asset-clip"}:
                offset = _seconds(parent.get("offset", "0s")) + (offset - _seconds(parent.get("start", "0s"))) / _retime_ratio(parent)
            retime = _retime_ratio(clip)
            filters.append(f"[{n}:a]atrim=start={_seconds(clip.get('start', '0s'))}:duration={_seconds(clip.get('duration')) * retime},"
                           f"asetpts=PTS-STARTPTS,atempo={retime},aresample=48000,aformat=channel_layouts=stereo,"
                           f"adelay={round(offset*1000)}:all=1,"
                           f"apad=whole_dur={duration}[a{n}]")
        command += ["-map", "0:v", "-c:v", "copy"]
        if sounds:
            labels = "".join(f"[a{n}]" for n in range(1, len(sounds) + 1))
            filters.append(f"{labels}amix=inputs={len(sounds)}:duration=longest:normalize=0,alimiter=limit=0.95:latency=1[a]")
            command += ["-filter_complex", ";".join(filters), "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        target = temporary / "complete.mp4"
        command += ["-t", str(duration), "-movflags", "+faststart", str(target)]
        run(command)
        target.replace(output_path)
    report = {"input_hash": input_hash, "output_hash": hashlib.sha256(output_path.read_bytes()).hexdigest(),
              "duration_seconds": duration, "audio_clip_count": len(sounds), "timeline_path": str(timeline_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return {"returncode": 0, "cached": False, "output_path": str(output_path), **report}


def storybook_photo_montage_tool(session_id: str, project_id: str, enable: bool = True) -> dict:
    """Export both complete timelines and render the photo film after images and audio.

    Args:
        session_id: Pipeline execution identifier.
        project_id: Storybook project identifier.
        enable: Whether to export timelines and assemble the photo film.
    """
    if not enable:
        return {"status": "skipped"}
    from custom_tools.storybook.screenplay_shots_generator_utils.fcpxml_generator import _generate_fcpxml, _generate_photo_fcpxml
    base = safe_storybook_project_dir(project_id)
    from .continuity_review import require_continuity_acceptance
    require_continuity_acceptance(base, 'images')
    items = _read_json(base / "97_shots/shots.json", {})["items"]
    phases = {}
    for item in items:
        phases.setdefault((item["scene_number"], item["shot_number"]), set()).add(item.get("shot_type"))
    if not phases or any(not {"start", "end"}.issubset(value) for value in phases.values()):
        raise ValueError("The complete photo film requires a START/END pair for every shot")
    motion = base / "97_shots/shots_timeline.fcpxml"
    photo = base / "97_shots/photo_shots_timeline.fcpxml"
    _generate_fcpxml(project_id, items, str(motion))
    if not _generate_photo_fcpxml(project_id, items, str(photo)):
        raise ValueError("Photo film requires START and END images")
    result = render_fcpxml(photo, base / "99_final/photo_video.mp4")
    return {"status": "success", "photo_timeline_path": str(photo), "timeline_path": str(motion),
            "photo_video_path": result["output_path"], "cached": result["cached"]}
