import itertools
import json
from pathlib import Path

from custom_tools.storybook import music_generator


_COOKIE = "__client=client-abc; ajs_anonymous_id=anon-1; __session=sess-1"


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


def _find(calls, needle):
    return next(call for call in calls if needle in call["url"])


def _install_fakes(monkeypatch, calls, *, required=False, clips=None):
    clips = clips if clips is not None else [
        {"id": "clip-1", "status": "complete", "audio_url": "https://cdn.example/music.mp3"},
        {"id": "clip-2", "status": "streaming"},
    ]

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url, "headers": headers, "json": json})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": required})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": clip["id"]} for clip in clips]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url, "headers": headers, "params": params})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(json_payload={"clips": clips})
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)


def test_music_generator_skips_without_suno_cookie_and_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_COOKIE", raising=False)
    project_id = "proj_music_skip"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    result = music_generator.storybook_music_generator_tool("sess", project_id)

    assert result["status"] == "skipped"
    music_manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert music_manifest["status"] == "skipped"
    assert music_manifest["message"] == "SUNO_COOKIE is not configured"
    assert audio_manifest["music_status"] == "skipped"
    assert audio_manifest["audio_tracks"] == []


def test_music_generator_submits_polls_downloads_and_updates_audio_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.delenv("SUNO_CUSTOM_MODE", raising=False)
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
    _install_fakes(monkeypatch, calls)

    result = music_generator.storybook_music_generator_tool(
        "sess",
        project_id,
        poll_interval_seconds=1,
        timeout_seconds=5,
    )

    assert result["status"] == "success"
    assert result["task_id"] == "clip-1"
    assert (base / "98_audio" / "music.mp3").read_bytes() == b"mp3-bytes"

    generate = _find(calls["post"], "/api/generate/v2/")
    assert generate["url"] == "https://studio-api.prod.suno.com/api/generate/v2/"
    assert generate["headers"]["Authorization"] == "Bearer jwt-xyz"
    assert generate["json"]["make_instrumental"] is True
    assert generate["json"]["mv"] == "chirp-fenix"
    assert generate["json"]["token"] is None
    assert "gpt_description_prompt" in generate["json"]

    feed = _find(calls["get"], "/api/feed/v2")
    assert feed["params"] == {"ids": "clip-1,clip-2"}

    # JWT must be minted fresh before each studio call (c/check, generate, feed).
    assert len([call for call in calls["post"] if "/v1/client/sessions/" in call["url"]]) == 3

    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert audio_manifest["music_status"] == "success"
    assert audio_manifest["audio_tracks"] == [
        {
            "role": "music",
            "path": "music.mp3",
            "provider": "suno",
            "source": "storybook_music_generator_tool",
            "reused_existing": False,
            "task_id": "clip-1",
        }
    ]


def test_music_generator_skips_when_captcha_required_without_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    project_id = "proj_music_captcha"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(monkeypatch, calls, required=True)

    result = music_generator.storybook_music_generator_tool("sess", project_id)

    assert result["status"] == "skipped"
    assert "captcha" in result["message"].lower()
    assert not (base / "98_audio" / "music.mp3").exists()
    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    music_manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert music_manifest["status"] == "skipped"


def test_music_generator_uses_manual_captcha_token_when_required(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.setenv("SUNO_HCAPTCHA_TOKEN", "hc-token")
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_manual_token"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(monkeypatch, calls, required=True)

    result = music_generator.storybook_music_generator_tool(
        "sess",
        project_id,
        poll_interval_seconds=1,
        timeout_seconds=5,
    )

    assert result["status"] == "success"
    generate = _find(calls["post"], "/api/generate/v2/")
    assert generate["json"]["token"] == "hc-token"
    assert (base / "98_audio" / "music.mp3").read_bytes() == b"mp3-bytes"


def test_music_generator_errors_when_all_clips_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_allfail"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(
        monkeypatch,
        calls,
        clips=[
            {"id": "clip-1", "status": "error", "metadata": {"error_message": "boom"}},
            {"id": "clip-2", "status": "error"},
        ],
    )

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert "failed" in result["message"].lower()
    assert not (base / "98_audio" / "music.mp3").exists()
    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert audio_manifest["music_status"] == "error"


def test_music_generator_errors_on_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    clock = itertools.chain([0.0], itertools.repeat(1000.0))
    monkeypatch.setattr(music_generator.time, "monotonic", lambda: next(clock))
    project_id = "proj_music_timeout"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(
        monkeypatch,
        calls,
        clips=[
            {"id": "clip-1", "status": "streaming"},
            {"id": "clip-2", "status": "queued"},
        ],
    )

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()
    assert not (base / "98_audio" / "music.mp3").exists()


def test_music_generator_submits_without_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    project_id = "proj_music_submitted"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(monkeypatch, calls)

    result = music_generator.storybook_music_generator_tool("sess", project_id, wait_for_completion=False)

    assert result["status"] == "submitted"
    assert result["task_id"] == "clip-1"
    assert not any("/api/feed/v2" in call["url"] for call in calls["get"])
    assert not (base / "98_audio" / "music.mp3").exists()


def test_music_generator_fails_open_on_http_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    project_id = "proj_music_http_error"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    def fake_post(url, headers=None, json=None, timeout=None):
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(status_code=500, text="upstream boom")
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    # fail-open: a provider HTTP error must return an error dict, not raise (pipeline survives).
    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert "HTTP 500" in result["message"]
    audio_manifest = json.loads((base / "98_audio" / "audio_manifest.json").read_text(encoding="utf-8"))
    assert audio_manifest["music_status"] == "error"


def test_music_generator_reuses_existing_music_without_api_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_COOKIE", raising=False)
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
