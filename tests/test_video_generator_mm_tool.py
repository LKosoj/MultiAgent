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

def test_mm_snap_to_supported_duration():
    assert mm_module._snap_to_supported_duration_mm(6) == 6
    assert mm_module._snap_to_supported_duration_mm(10) == 10
    assert mm_module._snap_to_supported_duration_mm(5) == 6
    assert mm_module._snap_to_supported_duration_mm(9) == 10
    assert mm_module._snap_to_supported_duration_mm(8) == 6  # ничья → меньшее
    assert mm_module._snap_to_supported_duration_mm(1) == 6


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


# --- M-9: atomic download + non-empty existence filter -------------------------------------------

def test_mm_skips_existing_nonempty_video_without_api(tmp_path, monkeypatch):
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
    assert "Нет START кадров" in result["message"]


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
