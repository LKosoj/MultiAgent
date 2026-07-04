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


def test_music_generator_writes_submitted_manifest_with_clip_ids_before_polling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_submit_first"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    manifest_path = base / "98_audio" / "music_manifest.json"

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-1"}, {"id": "clip-2"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            # Snapshot the on-disk manifest at the moment polling begins.
            if "at_poll" not in captured:
                captured["at_poll"] = json.loads(manifest_path.read_text(encoding="utf-8"))
            return _FakeResponse(
                json_payload={"clips": [{"id": "clip-1", "status": "complete", "audio_url": "https://cdn.example/m.mp3"}]}
            )
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "success"
    assert captured["at_poll"]["status"] == "submitted"
    assert captured["at_poll"]["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_preserves_clip_ids_on_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    clock = itertools.chain([0.0], itertools.repeat(1000.0))
    monkeypatch.setattr(music_generator.time, "monotonic", lambda: next(clock))
    project_id = "proj_music_preserve_timeout"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}
    _install_fakes(
        monkeypatch,
        calls,
        clips=[{"id": "clip-1", "status": "streaming"}, {"id": "clip-2", "status": "queued"}],
    )

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()
    assert not (base / "98_audio" / "music.mp3").exists()
    # Durable submission survives the timeout so a later run can resume-poll (no double charge).
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_resumes_saved_clip_ids_without_resubmitting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_resume"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    # Simulate a prior run that submitted a paid Suno job but was killed before download.
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"provider": "suno", "status": "submitted", "clip_ids": ["clip-1", "clip-2"]},
    )

    calls = {"post": [], "get": []}
    _install_fakes(monkeypatch, calls)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "success"
    assert result["task_id"] == "clip-1"
    assert (base / "98_audio" / "music.mp3").read_bytes() == b"mp3-bytes"
    # No resubmission: /api/generate/v2/ must never be called on resume.
    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"


def test_music_generator_truncates_provider_response_in_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_args, **_kwargs: None)
    project_id = "proj_music_truncate"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    bulky_clips = [
        {
            "id": "clip-1",
            "status": "complete",
            "title": "Theme",
            "audio_url": "https://cdn.example/m.mp3",
            "metadata": {"blob": "x" * 2000},
            "lyric": "la la la",
        },
        {"id": "clip-2", "status": "streaming", "title": "Alt", "metadata": {"blob": "y" * 2000}},
    ]

    def fake_post(url, headers=None, json=None, timeout=None):
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": clip["id"]} for clip in bulky_clips]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(json_payload={"clips": bulky_clips, "major_model_version": "v5", "moderated": True})
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "success"
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    provider_response = manifest["provider_response"]
    for clip in provider_response["clips"]:
        assert set(clip.keys()) <= {"id", "status", "title"}
    assert provider_response["major_model_version"] == "v5"
    assert provider_response["moderated"] is True
    # Bulky fields are stripped.
    assert "x" * 2000 not in json.dumps(manifest, ensure_ascii=False)


def test_music_generator_writes_manifest_atomically(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUNO_COOKIE", raising=False)
    project_id = "proj_music_atomic"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    replaced = []
    real_replace = music_generator.os.replace

    def spy_replace(src, dst):
        replaced.append(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(music_generator.os, "replace", spy_replace)

    result = music_generator.storybook_music_generator_tool("sess", project_id)

    assert result["status"] == "skipped"
    assert any(dst.endswith("music_manifest.json") for dst in replaced)
    assert list((base / "98_audio").glob(".*.tmp")) == []


def test_music_generator_keeps_submission_on_transient_feed_http_error(tmp_path, monkeypatch):
    # Транзиентная HTTP-ошибка feed В ПРОЦЕССЕ ПЕРВОГО поллинга не должна затирать
    # durable "submitted"-манифест в "error" — иначе следующий прогон не резюмирует
    # и ресабмитит оплаченную джобу (двойная оплата Suno).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_transient_first"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-1"}, {"id": "clip-2"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(status_code=429, text="rate limited")
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert not (base / "98_audio" / "music.mp3").exists()
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_keeps_submission_on_transient_feed_error_during_resume(tmp_path, monkeypatch):
    # Транзиент feed при resume: НЕ ресабмитим (иначе двойная оплата), submitted сохраняется.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_transient_resume"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"provider": "suno", "status": "submitted", "clip_ids": ["clip-1", "clip-2"]},
    )

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-new"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(status_code=503, text="upstream unavailable")
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    assert result["status"] == "submitted"
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_keeps_submission_on_transient_download_error(tmp_path, monkeypatch):
    # Джоба сгенерирована и ОПЛАЧЕНА (poll вернул audio_url), но СКАЧИВАНИЕ временно упало
    # (CDN 5xx). Манифест НЕ должен уйти в "error" → иначе следующий прогон не резюмирует и
    # ресабмитит оплаченную джобу (двойная оплата).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_dl_fail"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-1"}, {"id": "clip-2"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(json_payload={"clips": [
                {"id": "clip-1", "status": "complete", "audio_url": "https://cdn.example/music.mp3"},
            ]})
        # Скачивание готового аудио временно недоступно.
        return _FakeResponse(status_code=503, text="cdn unavailable")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert not (base / "98_audio" / "music.mp3").exists()
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_resume_download_failure_keeps_submission(tmp_path, monkeypatch):
    # То же на resume-пути: клип готов, скачивание упало → сохраняем "submitted", НЕ ресабмитим.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_resume_dl"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"provider": "suno", "status": "submitted", "clip_ids": ["clip-1", "clip-2"]},
    )

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-new"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            return _FakeResponse(json_payload={"clips": [
                {"id": "clip-1", "status": "complete", "audio_url": "https://cdn.example/music.mp3"},
            ]})
        return _FakeResponse(status_code=503, text="cdn unavailable")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_transient_auth_preserves_existing_submission(tmp_path, monkeypatch):
    # Транзиент в auth ДО resume не должен затирать durable "submitted"-манифест оплаченной
    # pending-джобы (иначе следующий прогон ресабмитит = двойная оплата).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_auth_transient"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"provider": "suno", "status": "submitted", "clip_ids": ["clip-1", "clip-2"]},
    )

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        raise AssertionError(f"no POST expected before resume: {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        # Транзиент Clerk на самом первом шаге auth (до resume).
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(status_code=503, text="clerk down")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    # Существующий submitted-манифест не тронут; сабмит не выполнялся.
    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_keeps_submission_on_connection_transient_during_poll(tmp_path, monkeypatch):
    # Сетевой транзиент (ReadTimeout/обрыв соединения) при ПЕРВОМ поллинге feed — ответ не
    # получен, это НЕ genuine-провал джобы. Собственный сабмит уже записал durable "submitted";
    # манифест НЕ должен уйти в "error", иначе следующий прогон ресабмитит оплаченную джобу.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_conn_first"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-1"}, {"id": "clip-2"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            raise music_generator.requests.exceptions.ReadTimeout("read timed out")
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "error"
    assert not (base / "98_audio" / "music.mp3").exists()
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]


def test_music_generator_keeps_submission_on_connection_transient_during_resume(tmp_path, monkeypatch):
    # Сетевой транзиент (ConnectionError) при поллинге на RESUME-пути: НЕ ресабмитим оплаченную
    # джобу (иначе двойная оплата), durable "submitted" сохраняется для следующего прогона.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUNO_COOKIE", _COOKIE)
    monkeypatch.delenv("SUNO_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(music_generator.time, "sleep", lambda *_a, **_k: None)
    project_id = "proj_music_conn_resume"
    base = tmp_path / "plots" / "storybooks" / project_id
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"provider": "suno", "status": "submitted", "clip_ids": ["clip-1", "clip-2"]},
    )

    calls = {"post": [], "get": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["post"].append({"url": url})
        if "/v1/client/sessions/" in url:
            return _FakeResponse(json_payload={"jwt": "jwt-xyz"})
        if "/api/c/check" in url:
            return _FakeResponse(json_payload={"required": False})
        if "/api/generate/v2/" in url:
            return _FakeResponse(json_payload={"clips": [{"id": "clip-new"}]})
        raise AssertionError(f"unexpected POST {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"].append({"url": url})
        if "/v1/client" in url and "/sessions/" not in url:
            return _FakeResponse(json_payload={"response": {"last_active_session_id": "sid-1"}})
        if "/api/feed/v2" in url:
            raise music_generator.requests.exceptions.ConnectionError("connection reset by peer")
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(music_generator.requests, "post", fake_post)
    monkeypatch.setattr(music_generator.requests, "get", fake_get)

    result = music_generator.storybook_music_generator_tool(
        "sess", project_id, poll_interval_seconds=1, timeout_seconds=5
    )

    assert result["status"] == "submitted"
    assert not any("/api/generate/v2/" in call["url"] for call in calls["post"])
    manifest = json.loads((base / "98_audio" / "music_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "submitted"
    assert manifest["clip_ids"] == ["clip-1", "clip-2"]
