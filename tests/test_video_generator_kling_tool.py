import json
import logging
from pathlib import Path

from PIL import Image

from custom_tools.storybook import video_generator as kling_module


_STATUS_MARKER = "/image2video/"


class _FakeResponse:
    def __init__(self, status_code=200, json_payload=None, text="", content=b""):
        self.status_code = status_code
        self._json_payload = json_payload
        self.text = text
        self.content = content

    def json(self):
        return self._json_payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


def _create_png(path: Path, size=(64, 64)):
    Image.new("RGB", size, color=(20, 40, 60)).save(path)


def _write_project(tmp_path, monkeypatch, items, project_id="project-kling"):
    monkeypatch.chdir(tmp_path)
    shots_dir = tmp_path / "plots" / "storybooks" / project_id / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return project_id, shots_dir


def _make_item(shots_dir: Path, scene: int = 1, shot: int = 1, prompt: str = "A calm sea at dawn",
               timing: str = "00:00 - 00:05", shot_type: str = "start"):
    # Kling ищет стартовое изображение по паттерну в директории видео.
    start_image = shots_dir / f"img_final_start_{scene:02d}_{shot:02d}.png"
    _create_png(start_image)
    return {
        "scene_number": scene,
        "shot_number": shot,
        "shot_type": shot_type,
        "video_prompt": prompt,
        "video_path": str(shots_dir / f"video-{scene}-{shot}.mp4"),
        "timing": timing,
    }


def _read_provider_jobs(shots_dir: Path):
    return json.loads((shots_dir / "provider_jobs.json").read_text(encoding="utf-8"))["jobs"]


def _patch_common(monkeypatch):
    monkeypatch.setenv("KLING_API_KEY", "kling-key")
    monkeypatch.setenv("KLING_API_SECRET_KEY", "kling-secret")
    monkeypatch.setattr(kling_module, "ak", None)
    monkeypatch.setattr(kling_module, "sk", None)
    monkeypatch.setattr(kling_module.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(kling_module, "update_shots_with_descriptions", lambda *a, **k: False)

    def fake_token(api_key, api_secret):
        assert api_key == "kling-key"
        assert api_secret == "kling-secret"
        return "fake-token"

    monkeypatch.setattr(kling_module, "encode_jwt_token", fake_token)


def _install_success_mocks(monkeypatch, video_url="https://cdn.kling.example/videos/out.mp4?sign=SECRET"):
    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": video_url}]},
            }})
        if url == video_url:
            return _FakeResponse(200, content=b"kling-video-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)


# --- happy path ----------------------------------------------------------------------------------

def test_kling_generates_and_downloads_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    _install_success_mocks(monkeypatch)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    assert Path(item["video_path"]).read_bytes() == b"kling-video-bytes"
    assert result["results"][0]["task_id"] == "task-1"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


# --- L-5: only START shots, dedup by scene+shot --------------------------------------------------

def test_kling_skips_non_start_shot_type(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir, shot_type="end")

    def forbidden(*a, **k):
        raise AssertionError("non-start shot must not reach the API")

    monkeypatch.setattr(kling_module.requests, "post", forbidden)
    monkeypatch.setattr(kling_module.requests, "get", forbidden)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert "Нет кадров" in result["message"]


def test_kling_deduplicates_same_scene_shot(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    first = _make_item(shots_dir, scene=1, shot=1, prompt="first take")
    duplicate = _make_item(shots_dir, scene=1, shot=1, prompt="second take")
    post_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        post_calls.append(1)
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": "https://cdn.kling.example/out.mp4"}]},
            }})
        return _FakeResponse(200, content=b"kling-video-bytes")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [first, duplicate]}, project_id="project-kling",
        enable=True, max_concurrency=2,
    )

    assert result["status"] == "success"
    assert result["stats"]["total"] == 1  # дубликат отфильтрован до генерации
    assert len(post_calls) == 1


# --- M-9: existence filter uses non-empty check --------------------------------------------------

def test_kling_skips_existing_nonempty_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    Path(item["video_path"]).write_bytes(b"already-there")

    def forbidden(*a, **k):
        raise AssertionError("existing non-empty video must not call API")

    monkeypatch.setattr(kling_module.requests, "post", forbidden)
    monkeypatch.setattr(kling_module.requests, "get", forbidden)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["provider"] == "kling"
    assert jobs[0]["status"] == "downloaded"


def test_kling_regenerates_when_existing_video_is_empty(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    Path(item["video_path"]).write_bytes(b"")  # пустой файл: НЕ должен пропускаться
    _install_success_mocks(monkeypatch)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"]["successful"] == 1
    assert Path(item["video_path"]).read_bytes() == b"kling-video-bytes"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


def test_kling_persists_provider_job_and_resumes_without_post(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    video_url = "https://cdn.kling.example/videos/out.mp4?sign=SECRET"
    post_calls = []
    download_calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        del headers, json, timeout
        if post_calls:
            raise AssertionError("resume must not POST a duplicate Kling task")
        post_calls.append(url)
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": video_url}]},
            }})
        if url == video_url:
            download_calls.append(url)
            return _FakeResponse(200, content=f"kling-video-{len(download_calls)}".encode("utf-8"))
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)

    first = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert first["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"kling-video-1"
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["provider"] == "kling"
    assert jobs[0]["task_id"] == "task-1"
    assert jobs[0]["status"] == "downloaded"
    assert jobs[0]["video_url"] == video_url
    assert {"submitted_at", "polled_at", "completed_at", "downloaded_at"}.issubset(jobs[0]["timestamps"])

    provider_jobs_path = shots_dir / "provider_jobs.json"
    provider_jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))
    provider_jobs["jobs"][0]["status"] = "poll_timeout"
    provider_jobs_path.write_text(json.dumps(provider_jobs), encoding="utf-8")
    Path(item["video_path"]).unlink()

    resumed = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert resumed["status"] == "success"
    assert len(post_calls) == 1
    assert Path(item["video_path"]).read_bytes() == b"kling-video-2"


def test_kling_poll_ledger_write_error_keeps_task_resumable(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": "https://cdn.kling.example/out.mp4"}]},
            }})
        raise AssertionError(f"Unexpected GET {url}")

    def failing_poll_update(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("ledger disappeared")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)
    monkeypatch.setattr(kling_module, "_record_kling_poll_update", failing_poll_update)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "error"
    job = _read_provider_jobs(shots_dir)[0]
    assert job["status"] == "poll_timeout"
    assert job["task_id"] == "task-1"


# --- M-8: failed>0 -> status error with top-level error ------------------------------------------

def test_kling_reports_error_when_all_generation_fails(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(500, text="server error")

    def forbidden_get(*a, **k):
        raise AssertionError("failed submit must not proceed to polling")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", forbidden_get)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [item]}, project_id="project-kling",
        enable=True, max_concurrency=1,
    )

    assert result["status"] == "error"
    assert result["error"] == result["message"]
    assert result["stats"] == {"total": 1, "successful": 0, "failed": 1}
    assert result["results"][0]["success"] is False
    assert _read_provider_jobs(shots_dir)[0]["status"] == "failed"


def test_kling_partial_failure_is_reported_as_error(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    good = _make_item(shots_dir, scene=1, shot=1, prompt="A calm sea")
    bad = _make_item(shots_dir, scene=1, shot=2, prompt="A stormy sea")

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, timeout
        if "storm" in json.get("prompt", ""):
            return _FakeResponse(500, text="server error")
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": "https://cdn.kling.example/out.mp4"}]},
            }})
        return _FakeResponse(200, content=b"kling-video-bytes")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)

    result = kling_module.video_generator_tool(
        session_id="s", items={"items": [good, bad]}, project_id="project-kling",
        enable=True, max_concurrency=2,
    )

    assert result["status"] == "error"
    assert result["error"] == result["message"]
    assert result["stats"] == {"total": 2, "successful": 1, "failed": 1}
    assert Path(good["video_path"]).read_bytes() == b"kling-video-bytes"


# --- L-4: non-200 status response is retried, not fatally returned -------------------------------

def test_kling_retries_polling_on_non_200_status(tmp_path, monkeypatch, caplog):
    _patch_common(monkeypatch)
    _, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    video_url = "https://cdn.kling.example/out.mp4"
    status_calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        del url, headers, json, timeout
        return _FakeResponse(200, {"data": {"task_id": "task-1"}})

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout, stream
        if _STATUS_MARKER in url and url.endswith("task-1"):
            status_calls["n"] += 1
            if status_calls["n"] == 1:
                # json_payload=None: если бы код звал .json() до проверки статуса — упал бы иначе
                return _FakeResponse(500, json_payload=None, text="upstream 500")
            return _FakeResponse(200, {"data": {
                "task_status": "succeed",
                "task_result": {"videos": [{"url": video_url}]},
            }})
        if url == video_url:
            return _FakeResponse(200, content=b"kling-video-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(kling_module.requests, "post", fake_post)
    monkeypatch.setattr(kling_module.requests, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        result = kling_module.video_generator_tool(
            session_id="s", items={"items": [item]}, project_id="project-kling",
            enable=True, max_concurrency=1,
        )

    assert result["status"] == "success"
    assert status_calls["n"] == 2  # не фатально: повторили после не-200 и дождались succeed
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Ошибка проверки статуса task task-1: 500" in text
    assert Path(item["video_path"]).read_bytes() == b"kling-video-bytes"
