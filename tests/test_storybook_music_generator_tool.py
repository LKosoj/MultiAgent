import json
from pathlib import Path

from custom_tools.storybook import music_generator


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, content=b"", text=""):
        self.status_code = status_code
        self._json_payload = json_payload if json_payload is not None else {}
        self.content = content
        self.text = text

    def json(self):
        return self._json_payload

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_music_generator_skips_without_suno_key_and_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_API_KEY", raising=False)
    project_id = "proj_music_skip"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    result = music_generator.storybook_music_generator_tool("sess", project_id)

    assert result["status"] == "skipped"
    music_manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert music_manifest["status"] == "skipped"
    assert music_manifest["message"] == "SUNO_API_KEY is not configured"
    assert audio_manifest["music_status"] == "skipped"
    assert audio_manifest["audio_tracks"] == []


def test_music_generator_submits_polls_downloads_and_updates_audio_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_API_KEY", "sk-suno")
    monkeypatch.setenv("SUNO_API_BASE_URL", "https://suno.example")
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_success"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "concept": {"music_concept": "Gentle orchestral adventure theme"},
            "screenplay": [{"sound": "Soft bells and warm strings"}],
        },
    )

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse(json_payload={"code": 200, "data": {"taskId": "task-1"}})

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if params:
            return _FakeResponse(
                json_payload={
                    "code": 200,
                    "data": {
                        "status": "SUCCESS",
                        "response": {"sunoData": [{"audioUrl": "https://cdn.example/music.mp3"}]},
                    },
                }
            )
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess",
        project_id,
        poll_interval_seconds=1,
        timeout_seconds=5,
    )

    assert result["status"] == "success"
    assert (base / "98_audio" / "music.mp3").read_bytes() == b"mp3-bytes"
    assert calls["post"][0]["url"] == "https://suno.example/api/v1/generate"
    assert calls["post"][0]["headers"]["Authorization"] == "Bearer sk-suno"
    assert calls["post"][0]["json"]["instrumental"] is True
    assert calls["get"][0]["params"] == {"taskId": "task-1"}
    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert audio_manifest["music_status"] == "success"
    assert audio_manifest["audio_tracks"] == [
        {
            "role": "music",
            "path": "music.mp3",
            "provider": "suno",
            "source": "storybook_music_generator_tool",
            "reused_existing": False,
            "task_id": "task-1",
        }
    ]


def test_music_generator_reuses_existing_music_without_api_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_API_KEY", raising=False)
    project_id = "proj_music_reuse"
    music_path = tmp_path / "plots" / "storybooks" / project_id / "98_audio" / "music.mp3"
    music_path.parent.mkdir(parents=True)
    music_path.write_bytes(b"existing")
    monkeypatch.setattr(
        music_generator.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call provider")),
    )

    result = music_generator.storybook_music_generator_tool("sess", project_id)

    assert result["status"] == "success"
    manifest = json.loads((music_path.parent / "audio_manifest.json").read_text(encoding="utf-8"))
    assert manifest["audio_tracks"][0]["reused_existing"] is True


def test_music_generator_rejects_project_id_path_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = music_generator.storybook_music_generator_tool("sess", "../outside")

    assert result["status"] == "error"
    assert "project_id must be a safe path segment" in result["message"]
    assert result["music_manifest_path"] == ""
    assert result["music_path"] == ""
    assert result["results"] == {
        "music_manifest_path": "",
        "music_path": "",
        "music_status": "error",
    }
