import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from custom_tools.storybook import montage_assembler as montage
from custom_tools.storybook import sfx_generator as sfx


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(sfx, "_safe_project_dir", lambda project_id: tmp_path)
    monkeypatch.setattr(sfx, "_load_env_file", lambda: None)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "https://gateway.example/v1/")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "gateway-key")
    return tmp_path


@pytest.mark.parametrize("effect,expected_duration", [
    ({"prompt": "Ветер, птицы. Выстрел. Пауза. Птицы поют громче", "duration": 20}, 20),
    ({"prompt": "Ветер, птицы. Выстрел. 20 секунд"}, None),
    ("Ветер и птицы", None),
])
def test_generates_mp3_with_optional_duration_and_reuses_it(project, monkeypatch, effect, expected_duration):
    _write(project / "91_screenplay/screenplay.json", {"screenplay": [{"scene_number": 2, "sfx": effect}]})
    _write(project / "98_audio/audio_manifest.json", {"audio_tracks": [{"role": "music", "path": "music.mp3"}]})
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(content=b"mp3-bytes", headers={"Content-Type": "audio/mpeg"}, raise_for_status=lambda: None)

    monkeypatch.setattr(sfx.requests, "post", post)
    assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "success"
    assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "success"
    assert len(calls) == 1
    url, options = calls[0]
    assert url == "https://gateway.example/v1/audio/speech"
    assert options["headers"]["Authorization"] == "Bearer gateway-key"
    body = options["json"]
    assert body["model"] == "llmgateway/small-sfx"
    assert body["input"] == (effect if isinstance(effect, str) else effect["prompt"])
    assert body["response_format"] == "mp3"
    if expected_duration is None:
        assert "duration" not in body
    else:
        assert body["duration"] == expected_duration
    manifest = json.loads((project / "98_audio/audio_manifest.json").read_text())
    assert len(manifest["audio_tracks"]) == 2
    assert manifest["audio_tracks"][0]["role"] == "music"
    track = manifest["audio_tracks"][1]
    assert track["scene_number"] == 2 and track["role"] == "sfx"
    assert (project / "98_audio" / track["path"]).read_bytes() == b"mp3-bytes"


def test_changed_description_regenerates_and_replaces_manifest_entry(project, monkeypatch):
    calls = []

    def generate(payload, path):
        calls.append(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")

    monkeypatch.setattr(sfx, "_generate_mp3", generate)
    for prompt in ["wind", "birds"]:
        _write(project / "91_screenplay/screenplay.json", {"screenplay": [{"scene_number": 1, "sfx": {"prompt": prompt}}]})
        assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "success"
    assert len(calls) == 2
    tracks = json.loads((project / "98_audio/audio_manifest.json").read_text())["audio_tracks"]
    assert len(tracks) == 1 and tracks[0]["prompt"] == "birds"


@pytest.mark.parametrize("content,content_type", [(b'{"error":"oops"}', "application/json"), (b"", "audio/mpeg")])
def test_invalid_audio_is_not_cached(project, monkeypatch, content, content_type):
    _write(project / "91_screenplay/screenplay.json", {"screenplay": [{"scene_number": 1, "sfx": "wind"}]})
    monkeypatch.setattr(sfx.requests, "post", lambda *a, **kw: SimpleNamespace(
        content=content, headers={"Content-Type": content_type}, raise_for_status=lambda: None,
    ))
    result = sfx.storybook_sfx_generator_tool("session", "project")
    assert result["status"] == "error"
    assert not list(project.rglob("*.mp3"))
    assert json.loads((project / "98_audio/audio_manifest.json").read_text())["sfx_status"] == "error"


def test_silent_scene_does_not_call_model(project, monkeypatch):
    _write(project / "91_screenplay/screenplay.json", {"screenplay": [{"scene_number": 1, "sfx": None}]})
    monkeypatch.setattr(sfx.requests, "post", lambda *a, **kw: pytest.fail("Silent scenes must not generate audio"))
    assert sfx.storybook_sfx_generator_tool("session", "project")["track_count"] == 0


@pytest.mark.parametrize("duration", [0, -1, "NaN"])
def test_invalid_duration_does_not_call_model(project, monkeypatch, duration):
    _write(project / "91_screenplay/screenplay.json", {"screenplay": [{
        "scene_number": 1, "sfx": {"prompt": "wind", "duration": duration},
    }]})
    monkeypatch.setattr(sfx.requests, "post", lambda *a, **kw: pytest.fail("Invalid duration must not reach the provider"))
    assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "error"


def test_retry_reuses_sfx_completed_before_failure(project, monkeypatch):
    _write(project / "91_screenplay/screenplay.json", {"screenplay": [
        {"scene_number": 1, "sfx": "wind"}, {"scene_number": 2, "sfx": "birds"},
    ]})
    calls = []

    def generate(payload, path):
        calls.append(payload["input"])
        if len(calls) == 2:
            raise TimeoutError("Provider unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")

    monkeypatch.setattr(sfx, "_generate_mp3", generate)
    assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "error"
    assert sfx.storybook_sfx_generator_tool("session", "project")["status"] == "success"
    assert calls == ["wind", "birds", "birds"]


def test_sfx_is_exported_on_separate_final_cut_lane(tmp_path, monkeypatch):
    clips = [
        {"scene_number": 1, "path": str(tmp_path / "a.mp4"), "planned_start_seconds": 0, "planned_duration_seconds": 4},
        {"scene_number": 2, "path": str(tmp_path / "b.mp4"), "planned_start_seconds": 4, "planned_duration_seconds": 3},
        {"scene_number": 2, "path": str(tmp_path / "c.mp4"), "planned_start_seconds": 7, "planned_duration_seconds": 5},
    ]
    effect = tmp_path / "forest.mp3"
    monkeypatch.setattr(montage, "_probe_media", lambda path: {
        "format": {"duration": "20"}, "streams": [{"codec_type": "audio", "sample_rate": "48000", "channels": 2}],
    })
    path = tmp_path / "timeline.fcpxml"
    montage._write_fcpxml(path, "project", clips, [{"path": str(effect), "scene_number": 2}])
    tree = ET.parse(path)
    video = tree.findall(".//spine/asset-clip")
    assert len(video) == 3
    assert video[0].find("asset-clip") is None
    audio = video[1].find("asset-clip")
    assert audio.get("lane") == "-1" and audio.get("audioRole") == "effects"
    assert audio.get("offset") == "0s" and audio.get("start") == "0s"
    assert audio.get("duration") == "8000/1000s"
    assert video[1].get("offset") == "4000/1000s"
    asset = tree.find(f"./resources/asset[@id='{audio.get('ref')}']")
    assert asset.get("hasAudio") == "1" and asset.get("audioRate") == "48000"
    assert asset.find("media-rep").get("src") == effect.as_uri()


def test_montage_passes_sfx_only_to_xml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "plots/storybooks/project"
    video = base / "97_shots/video.mp4"
    effect = base / "98_audio/sfx/forest.mp3"
    for path in (video, effect):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    _write(base / "97_shots/shots.json", {"items": [{
        "scene_number": 1, "shot_number": 1, "timing": "4s", "video_path": str(video), "description": "forest",
    }]})
    _write(base / "98_audio/audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": [{"role": "sfx", "path": "sfx/forest.mp3", "scene_number": 1}]})
    (base / "98_audio/subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:04,000\nForest\n")
    rendered = []
    exported = []
    monkeypatch.setattr(montage, "_write_fcpxml", lambda *args: exported.extend(args[3]))
    monkeypatch.setattr(montage, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage, "_probe_media", lambda path: {
        "format": {"format_name": "mp4", "duration": "4"}, "streams": [{"codec_type": "video"}],
    })
    monkeypatch.setattr(montage, "_run_command", lambda *a, **kw: {"returncode": 0, "stderr": ""})

    def render(**kwargs):
        rendered.extend(kwargs["audio_tracks"])
        kwargs["output_path"].write_bytes(b"video")
        return {"returncode": 0}

    monkeypatch.setattr(montage, "_render_final_video", render)
    result = montage.montage_assembler_tool("session", "project")
    assert result["status"] == "success", result
    assert rendered == []
    assert len(exported) == 1 and exported[0]["scene_number"] == 1
