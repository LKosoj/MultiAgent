"""Gateway music is primary; existing Suno generation remains the fallback."""

import json
from pathlib import Path

import pytest
import requests

from custom_tools.storybook import music_generator as music


class AudioResponse:
    def __init__(self, status=200, content=b"ID3-gateway", content_type="audio/mpeg"):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(music, "_load_env_file", lambda: None)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://gateway.test/v1/")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "test-key")
    monkeypatch.delenv("SUNO_COOKIE", raising=False)
    return tmp_path / "plots" / "storybooks" / "demo"


def test_gateway_generates_registers_and_reuses_without_suno(project, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return AudioResponse()

    monkeypatch.setattr(music.requests, "post", post)
    result = music.storybook_music_generator_tool("session", "demo", prompt="Soft piano")
    assert result["status"] == "success"
    assert Path(result["music_path"]).read_bytes() == b"ID3-gateway"
    url, kwargs = calls[0]
    assert url == "http://gateway.test/v1/audio/speech"
    assert kwargs["json"] == {"model": "llmgateway/music", "input": "Soft piano",
                              "lyrics": "[instrumental]", "response_format": "mp3"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["timeout"] > 360
    manifest = json.loads((project / "98_audio/audio_manifest.json").read_text())
    assert manifest["music_provider"] == "llmgateway/music"
    assert manifest["audio_tracks"][0]["provider"] == "llmgateway/music"
    again = music.storybook_music_generator_tool("session", "demo")
    assert again["status"] == "success"
    saved = json.loads((project / "98_audio/music_manifest.json").read_text())
    assert saved["reused_existing"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [401, 404, 429, 502, 503, 504, "connection", "timeout", "json", "empty", "unconfigured"])
def test_gateway_unavailability_uses_existing_suno(project, monkeypatch, failure):
    calls = []
    monkeypatch.setenv("SUNO_COOKIE", "cookie")
    if failure == "unconfigured":
        monkeypatch.delenv("OPENAI_API_BASE_DB")

    def post(*args, **kwargs):
        calls.append("gateway")
        if failure == "connection":
            raise requests.ConnectionError("unavailable")
        if failure == "timeout":
            raise requests.ReadTimeout("unavailable")
        if failure == "json":
            return AudioResponse(content=b"{}", content_type="application/json")
        if failure == "empty":
            return AudioResponse(content=b"")
        return AudioResponse(status=failure)

    monkeypatch.setattr(music.requests, "post", post)
    monkeypatch.setattr(music, "_authenticate", lambda cookie: {})

    def submit(*args):
        calls.append("suno")
        return {"clips": [{"id": "saved-clip"}]}

    monkeypatch.setattr(music, "_post_suno_generate", submit)
    monkeypatch.setattr(music, "_wait_for_suno_audio_url", lambda **kw: ({}, "https://cdn.test/music", "saved-clip"))
    monkeypatch.setattr(music, "_download_audio", lambda url, path: path.write_bytes(b"ID3-suno"))
    result = music.storybook_music_generator_tool("session", "demo", prompt="Soft piano")
    assert result["status"] == "success"
    assert calls == (["suno"] if failure == "unconfigured" else ["gateway", "suno"])
    manifest = json.loads((project / "98_audio/music_manifest.json").read_text())
    assert manifest["provider"] == "suno"
    assert Path(result["music_path"]).read_bytes() == b"ID3-suno"


def test_gateway_generates_all_planned_tracks_and_reuses_cache(project, monkeypatch):
    audio = project / "98_audio"
    audio.mkdir(parents=True)
    (audio / "music_plan.json").write_text(json.dumps({
        "leitmotifs": {"hero": {"suno_prompt": "Heroic brass"}},
        "neutral": {"suno_prompt": "Gentle piano"},
        "scene_mapping": {"1": "hero", "2": "neutral"},
    }))
    calls = []
    monkeypatch.setattr(music.requests, "post", lambda *a, **kw: calls.append(kw["json"]) or AudioResponse())
    result = music.storybook_music_generator_tool("session", "demo")
    assert result["status"] == "ok"
    assert result["manifest"] == {"hero": "music/hero.mp3", "neutral": "music/neutral.mp3"}
    assert {call["input"] for call in calls} == {"Heroic brass", "Gentle piano"}
    music.storybook_music_generator_tool("session", "demo")
    assert len(calls) == 2


def test_noninstrumental_request_omits_instrumental_lyrics(project, monkeypatch):
    payloads = []
    monkeypatch.setattr(music.requests, "post", lambda *a, **kw: payloads.append(kw["json"]) or AudioResponse())
    result = music.storybook_music_generator_tool("session", "demo", instrumental=False, prompt="Choir")
    assert result["status"] == "success"
    assert "lyrics" not in payloads[0]


def test_pending_suno_job_resumes_without_gateway_or_new_submission(project, monkeypatch):
    audio = project / "98_audio"
    audio.mkdir(parents=True)
    (audio / "music_manifest.json").write_text(json.dumps({"status": "submitted", "clip_ids": ["paid"]}))
    monkeypatch.setenv("SUNO_COOKIE", "cookie")
    monkeypatch.setattr(music.requests, "post", lambda *a, **kw: pytest.fail("new generation"))
    monkeypatch.setattr(music, "_authenticate", lambda cookie: {})
    monkeypatch.setattr(music, "_wait_for_suno_audio_url", lambda **kw: ({}, "https://cdn.test/music", "paid"))
    monkeypatch.setattr(music, "_download_audio", lambda url, path: path.write_bytes(b"ID3-paid"))
    result = music.storybook_music_generator_tool("session", "demo")
    assert result["status"] == "success"
    assert Path(result["music_path"]).read_bytes() == b"ID3-paid"


def test_gateway_regeneration_replaces_suno_audio_entry(project, monkeypatch):
    audio = project / "98_audio"
    audio.mkdir(parents=True)
    (audio / "music.mp3").write_bytes(b"ID3-old")
    (audio / "audio_manifest.json").write_text(json.dumps({"audio_tracks": [
        {"role": "music", "path": "music.mp3", "provider": "suno", "level": 0.2},
    ]}))
    monkeypatch.setattr(music.requests, "post", lambda *a, **kw: AudioResponse())
    result = music.storybook_music_generator_tool("session", "demo", force_regenerate=True)
    assert result["status"] == "success"
    tracks = json.loads((audio / "audio_manifest.json").read_text())["audio_tracks"]
    assert len(tracks) == 1
    assert tracks[0]["provider"] == "llmgateway/music"
    assert tracks[0]["level"] == 0.2


def test_planned_track_falls_back_to_suno_and_resumes_paid_job(project, monkeypatch):
    monkeypatch.setenv("SUNO_COOKIE", "cookie")
    calls = []

    def post(*args, **kwargs):
        calls.append("gateway")
        return AudioResponse(status=503)

    monkeypatch.setattr(music.requests, "post", post)
    monkeypatch.setattr(music, "_authenticate", lambda cookie: {})

    def submit(*args):
        calls.append("suno")
        return {"clips": [{"id": "paid"}]}

    monkeypatch.setattr(music, "_post_suno_generate", submit)
    monkeypatch.setattr(music, "_wait_for_suno_audio_url", lambda **kw: (_ for _ in ()).throw(TimeoutError("pending")))
    kwargs = dict(base_dir=project, track_id="hero", suno_prompt="Heroic brass",
                  target_path=project / "98_audio/music/hero.mp3", instrumental=True,
                  wait_for_completion=True, poll_interval_seconds=1, timeout_seconds=600,
                  force_regenerate=False, provider="llmgateway/music")
    with pytest.raises(TimeoutError):
        music._generate_single_track(**kwargs)
    assert calls == ["gateway", "suno"]
    monkeypatch.setattr(music, "_wait_for_suno_audio_url", lambda **kw: ({}, "https://cdn.test/music", "paid"))
    monkeypatch.setattr(music, "_download_audio", lambda url, path: path.write_bytes(b"ID3-paid"))
    assert music._generate_single_track(**kwargs).read_bytes() == b"ID3-paid"
    assert calls == ["gateway", "suno"]
