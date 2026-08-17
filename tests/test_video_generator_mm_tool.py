import json
import logging
import os
from pathlib import Path

from PIL import Image

from custom_tools.storybook import video_generator_mm_tool as mm_module


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


def _write_project(tmp_path, monkeypatch, items, project_id="project-mm"):
    monkeypatch.chdir(tmp_path)
    shots_dir = tmp_path / "plots" / "storybooks" / project_id / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return project_id, shots_dir


def _make_item(shots_dir: Path, shot_number: int, timing: str = "00:00 - 00:06"):
    start_image = shots_dir / f"start-{shot_number}.png"
    _create_png(start_image)
    return {
        "scene_number": 1,
        "shot_number": shot_number,
        "shot_type": "start",
        "video_prompt": "A calm sea",
        "video_path": str(shots_dir / f"video-{shot_number}.mp4"),
        "timing": timing,
        "start_image": str(start_image),
    }


def _read_provider_jobs(shots_dir: Path):
    return json.loads((shots_dir / "provider_jobs.json").read_text(encoding="utf-8"))["jobs"]


def _patch_common(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setattr(mm_module.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(mm_module, "update_shots_with_descriptions", lambda *a, **k: False)


def _install_success_mocks(monkeypatch, captured, download_url="https://cdn.example.com/videos/final.mp4?token=SECRET"):
    def fake_request(method, url, headers=None, data=None, timeout=None):
        del headers, timeout
        if method == "POST" and url.endswith("/video_generation"):
            captured["payload"] = json.loads(data)
            return _FakeResponse(200, {"task_id": "task-1"})
        if method == "GET" and "query/video_generation" in url:
            return _FakeResponse(200, {"status": "Success", "file_id": "file-1"})
        if method == "GET" and "files/retrieve" in url:
            return _FakeResponse(200, {"file": {"download_url": download_url}})
        raise AssertionError(f"Unexpected request {method} {url}")

    def fake_get(url, timeout=None, stream=False):
        del timeout, stream
        if url == download_url:
            return _FakeResponse(200, content=b"mm-video-bytes")
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(mm_module.requests, "request", fake_request)
    monkeypatch.setattr(mm_module.requests, "get", fake_get)


# --- M-11: load_env_file uses setdefault ---------------------------------------------------------

def test_mm_load_env_file_uses_setdefault(tmp_path, monkeypatch):
    module_dir = tmp_path / "custom_tools" / "storybook"
    module_dir.mkdir(parents=True)
    fake_file = module_dir / "video_generator_mm_tool.py"
    fake_file.write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("MM_EXISTING=from_env\nMM_NEW=from_env\n", encoding="utf-8")

    monkeypatch.setattr(mm_module, "__file__", str(fake_file))
    monkeypatch.setenv("MM_EXISTING", "preexisting")
    monkeypatch.delenv("MM_NEW", raising=False)

    mm_module.load_env_file()

    assert os.environ["MM_EXISTING"] == "preexisting"  # setdefault: не перезаписывает env процесса
    assert os.environ["MM_NEW"] == "from_env"           # setdefault: заполняет отсутствующее


# --- L-3: duration parsed from timing, snapped to supported set, not hardcoded to 6 --------------
# Тай-брейк общей _select_best_supported_duration() покрыт
# tests/test_video_generator_aitunnel_media.py (раздел 6.1 ТЗ, критерий A39),
# включая случай (8, [6, 10]) -> 6 — эквивалент удалённого
# _snap_to_supported_duration_mm(); дублировать здесь не нужно.

def test_mm_uses_parsed_duration_instead_of_hardcoded_six(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1, timing="00:00 - 00:10")
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    _install_success_mocks(monkeypatch, captured)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    assert Path(item["video_path"]).read_bytes() == b"mm-video-bytes"
    assert captured["payload"]["duration"] == 10  # L-3: длительность больше не форсируется в 6


# --- M-6: hash carries the requested duration, request carries the snapped one -------------------

def test_mm_snaps_duration_tie_to_smaller_but_hashes_requested(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1, timing="00:00 - 00:08")  # 8s: ничья между 6 и 10 в {6, 10} -> меньшее
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    _install_success_mocks(monkeypatch, captured)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "success"
    assert captured["payload"]["duration"] == 6

    job = _read_provider_jobs(shots_dir)[0]
    assert job["resolved_duration"] == 6
    assert job["hash_inputs_version"] == 3

    # ...а в хеш входа идёт именно запрошенная длительность (M-6, раздел 6.1 ТЗ), не подогнанная.
    expected_hash = mm_module._build_input_hash(
        model_name=mm_module._MM_MODEL_NAME,
        prompt_hash=mm_module._hash_text(item["video_prompt"]),
        source_image_hashes={
            "start_image": mm_module._hash_source_image(item["start_image"]),
            "end_image": None,
        },
        requested_duration=8,
        requested_width=0,
        requested_height=0,
        seed=None,
        frame_types=["first_frame"],
        provider_name="minimax",
    )
    assert job["input_hash"] == expected_hash


# --- M-9: atomic download + non-empty existence filter -------------------------------------------

def test_mm_existing_nonempty_video_marks_downloaded_without_api(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    Path(item["video_path"]).write_bytes(b"already-there")
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def forbidden(*a, **k):
        raise AssertionError("existing non-empty video must not call API")

    monkeypatch.setattr(mm_module.requests, "request", forbidden)
    monkeypatch.setattr(mm_module.requests, "get", forbidden)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["provider"] == "minimax"
    assert jobs[0]["status"] == "downloaded"
    assert jobs[0]["output_path"] == item["video_path"]


def test_mm_regenerates_when_existing_video_is_empty(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    Path(item["video_path"]).write_bytes(b"")  # пустой файл: НЕ должен пропускаться
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    _install_success_mocks(monkeypatch, captured)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "success"
    assert result["stats"]["successful"] == 1
    assert Path(item["video_path"]).read_bytes() == b"mm-video-bytes"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "downloaded"


# --- M-8: failed>0 -> status error with top-level error ------------------------------------------

def test_mm_reports_error_when_all_generation_fails(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_request(method, url, headers=None, data=None, timeout=None):
        del url, headers, data, timeout
        if method == "POST":
            return _FakeResponse(500, {}, text="server error")
        raise AssertionError("failed submit must not proceed to polling")

    monkeypatch.setattr(mm_module.requests, "request", fake_request)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "error"
    assert result["error"] == result["message"]
    assert result["stats"] == {"total": 1, "successful": 0, "failed": 1}
    assert result["results"][0]["success"] is False
    assert _read_provider_jobs(shots_dir)[0]["status"] == "failed"


def test_mm_project_id_run_persists_provider_job_and_resumes_without_post(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    post_calls = []
    download_calls = []
    download_url = "https://cdn.example.com/videos/final.mp4?token=SECRET"

    def fake_request(method, url, headers=None, data=None, timeout=None):
        del headers, timeout
        if method == "POST" and url.endswith("/video_generation"):
            if post_calls:
                raise AssertionError("resume must not POST a duplicate MiniMax job")
            post_calls.append(url)
            captured["payload"] = json.loads(data)
            return _FakeResponse(200, {"task_id": "task-1"})
        if method == "GET" and "query/video_generation" in url:
            return _FakeResponse(200, {"status": "Success", "file_id": "file-1"})
        if method == "GET" and "files/retrieve" in url:
            return _FakeResponse(200, {"file": {"download_url": download_url}})
        raise AssertionError(f"Unexpected request {method} {url}")

    def fake_get(url, timeout=None, stream=False):
        del timeout, stream
        if url == download_url:
            download_calls.append(url)
            return _FakeResponse(200, content=f"mm-video-{len(download_calls)}".encode("utf-8"))
        raise AssertionError(f"Unexpected GET {url}")

    monkeypatch.setattr(mm_module.requests, "request", fake_request)
    monkeypatch.setattr(mm_module.requests, "get", fake_get)

    first = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert first["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"mm-video-1"
    jobs = _read_provider_jobs(shots_dir)
    assert len(jobs) == 1
    assert jobs[0]["provider"] == "minimax"
    assert jobs[0]["task_id"] == "task-1"
    assert jobs[0]["file_id"] == "file-1"
    assert jobs[0]["status"] == "downloaded"
    assert jobs[0]["video_url"] == download_url
    assert {"submitted_at", "polled_at", "completed_at", "downloaded_at"}.issubset(jobs[0]["timestamps"])

    provider_jobs_path = shots_dir / "provider_jobs.json"
    provider_jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))
    provider_jobs["jobs"][0]["status"] = "poll_timeout"
    provider_jobs_path.write_text(json.dumps(provider_jobs), encoding="utf-8")
    Path(item["video_path"]).unlink()

    resumed = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert resumed["status"] == "success"
    assert len(post_calls) == 1
    assert Path(item["video_path"]).read_bytes() == b"mm-video-2"


def test_mm_changed_prompt_with_missing_source_does_not_trust_old_output(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    _install_success_mocks(monkeypatch, captured)

    first = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )
    assert first["status"] == "success"

    Path(item["start_image"]).unlink()
    changed = dict(item)
    changed["video_prompt"] = "A different prompt"
    (shots_dir / "shots.json").write_text(json.dumps({"items": [changed]}), encoding="utf-8")

    def forbidden_request(*a, **k):
        raise AssertionError("missing source frame must fail before paid API")

    monkeypatch.setattr(mm_module.requests, "request", forbidden_request)

    second = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert second["status"] == "error"
    jobs = _read_provider_jobs(shots_dir)
    assert [job["status"] for job in jobs] == ["stale", "failed"]


def test_mm_submitting_job_without_task_id_blocks_duplicate_post(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    store = mm_module._ProviderJobStore(str(shots_dir / "provider_jobs.json"), provider_name="minimax")
    source_hashes = {
        "start_image": mm_module._hash_source_image(item["start_image"]),
        "end_image": None,
    }
    prompt_hash = mm_module._hash_text(item["video_prompt"])
    input_hash = mm_module._build_input_hash(
        model_name="MiniMax-Hailuo-02",
        prompt_hash=prompt_hash,
        source_image_hashes=source_hashes,
        requested_duration=6,
        requested_width=0,
        requested_height=0,
        seed=None,
        frame_types=["first_frame"],
        provider_name="minimax",
    )
    store.ensure_job({
        **mm_module._new_provider_job(
            shot_key="1-1",
            model="MiniMax-Hailuo-02",
            prompt_hash=prompt_hash,
            source_image_hashes=source_hashes,
            input_hash=input_hash,
            output_path=item["video_path"],
            provider_name="minimax",
        ),
        "status": "submitting",
    })

    def forbidden_post(*a, **k):
        raise AssertionError("submitting job without task_id must not POST a duplicate")

    monkeypatch.setattr(mm_module.requests, "request", forbidden_post)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "error"
    assert "дубликат платной задачи" in result["results"][0]["error"]


def test_mm_unknown_submit_outcome_keeps_submitting_and_blocks_next_post(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def timeout_post(method, url, headers=None, data=None, timeout=None):
        del method, url, headers, data, timeout
        raise mm_module.requests.Timeout("submit timeout")

    monkeypatch.setattr(mm_module.requests, "request", timeout_post)

    first = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert first["status"] == "error"
    assert _read_provider_jobs(shots_dir)[0]["status"] == "submitting"

    def forbidden_request(*a, **k):
        raise AssertionError("ambiguous submitting job must not POST again")

    monkeypatch.setattr(mm_module.requests, "request", forbidden_request)

    second = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert second["status"] == "error"
    assert "дубликат платной задачи" in second["results"][0]["error"]


def test_mm_poll_ledger_write_error_is_not_swallowed(monkeypatch):
    _patch_common(monkeypatch)

    def fake_query(task_id, api_key):
        del task_id, api_key
        return {"status": "Processing"}

    def failing_poll_update(payload):
        del payload
        raise FileNotFoundError("ledger disappeared")

    monkeypatch.setattr(mm_module, "_query_video_generation_mm", fake_query)

    try:
        mm_module._wait_for_video_completion_mm(
            "task-1",
            "s",
            "mm-key",
            on_poll=failing_poll_update,
        )
    except Exception as exc:
        assert "ledger disappeared" in str(exc)
    else:
        raise AssertionError("on_poll errors must not be swallowed by polling retry")


def test_mm_poll_ledger_write_error_keeps_task_resumable(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])

    def fake_request(method, url, headers=None, data=None, timeout=None):
        del headers, data, timeout
        if method == "POST" and url.endswith("/video_generation"):
            return _FakeResponse(200, {"task_id": "task-1"})
        if method == "GET" and "query/video_generation" in url:
            return _FakeResponse(200, {"status": "Success", "file_id": "file-1"})
        raise AssertionError(f"Unexpected request {method} {url}")

    def failing_poll_update(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("ledger disappeared")

    monkeypatch.setattr(mm_module.requests, "request", fake_request)
    monkeypatch.setattr(mm_module, "_record_poll_update_mm", failing_poll_update)

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "error"
    job = _read_provider_jobs(shots_dir)[0]
    assert job["status"] == "poll_timeout"
    assert job["task_id"] == "task-1"


def test_provider_job_store_claim_submitting_is_atomic_by_provider(tmp_path):
    provider_jobs_path = tmp_path / "provider_jobs.json"
    store_one = mm_module._ProviderJobStore(str(provider_jobs_path), provider_name="minimax")
    store_two = mm_module._ProviderJobStore(str(provider_jobs_path), provider_name="minimax")
    job = mm_module._new_provider_job(
        shot_key="1-1",
        model="MiniMax-Hailuo-02",
        prompt_hash="prompt",
        source_image_hashes={"start_image": "hash"},
        input_hash="input",
        output_path="video.mp4",
        provider_name="minimax",
    )
    store_one.ensure_job(job)

    first = store_one.claim_submitting_job("1-1", "input", {"output_path": "video.mp4"})
    second = store_two.claim_submitting_job("1-1", "input", {"output_path": "video.mp4"})

    assert first["claimed"] is True
    assert second["claimed"] is False
    store_one.update_job("1-1", "input", {"status": "submitted", "task_id": "task-1"}, "submitted_at")
    third = store_two.claim_submitting_job("1-1", "input", {"output_path": "video.mp4"})
    assert third["claimed"] is False
    jobs = json.loads(provider_jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert jobs[0]["status"] == "submitted"


def test_mm_corrupt_provider_jobs_returns_structured_error(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    (shots_dir / "provider_jobs.json").write_text("{not-json", encoding="utf-8")

    result = mm_module.video_generator_mm_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1
    )

    assert result["status"] == "error"
    assert "provider_jobs.json" in result["message"]
    assert result["results"] == []


# --- L-2: signed download URL is not logged verbatim ---------------------------------------------

def test_mm_does_not_log_signed_download_url(tmp_path, monkeypatch, caplog):
    _patch_common(monkeypatch)
    captured = {}
    shots_dir = tmp_path / "plots" / "storybooks" / "project-mm" / "97_shots"
    shots_dir.mkdir(parents=True)
    item = _make_item(shots_dir, 1)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [item])
    _install_success_mocks(monkeypatch, captured)

    with caplog.at_level(logging.INFO):
        result = mm_module.video_generator_mm_tool(
            session_id="s", project_id=project_id, enable=True, max_concurrency=1
        )

    assert result["status"] == "success"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "token=SECRET" not in text
    assert "cdn.example.com" in text
