import json
import sys
import types as _pytypes
from pathlib import Path

from PIL import Image

# --- Заглушки google.genai / google.genai.types --------------------------------------------------
# В тестовом окружении пакет google.genai не установлен, а veo-модуль импортирует его на верхнем
# уровне. Регистрируем минимальные заглушки ДО импорта модуля. setdefault не перетирает реальный
# пакет, если он присутствует.


class _StubImage:
    def __init__(self, image_bytes=None, mime_type=None):
        self.image_bytes = image_bytes
        self.mime_type = mime_type


class _StubGenerateVideosConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


_genai_types_stub = _pytypes.ModuleType("google.genai.types")
_genai_types_stub.Image = _StubImage
_genai_types_stub.GenerateVideosConfig = _StubGenerateVideosConfig

_genai_stub = _pytypes.ModuleType("google.genai")
_genai_stub.types = _genai_types_stub


def _unpatched_client(*args, **kwargs):
    raise AssertionError("genai.Client must be patched per test")


_genai_stub.Client = _unpatched_client

sys.modules.setdefault("google.genai", _genai_stub)
sys.modules.setdefault("google.genai.types", _genai_types_stub)
try:
    import google as _google_pkg
    if not hasattr(_google_pkg, "genai"):
        _google_pkg.genai = sys.modules["google.genai"]
except ImportError:
    pass

from custom_tools.storybook import video_generator_veo_tool as veo_module  # noqa: E402


# --- Фейки клиента genai -------------------------------------------------------------------------

class _FakeVideoObj:
    def __init__(self, uri=None, video_bytes=None):
        self.uri = uri
        self.video_bytes = video_bytes


class _FakeGenVideo:
    def __init__(self, video):
        self.video = video


class _FakeOpResponse:
    def __init__(self, generated_videos):
        self.generated_videos = generated_videos
        self.rai_media_filtered_count = 0
        self.rai_media_filtered_reasons = None


class _FakeOperation:
    def __init__(self, name="op-1", done=True, response=None, error=None):
        self.name = name
        self.done = done
        self.response = response
        self.error = error


class _FakeModels:
    def __init__(self, resolver, captured):
        self._resolver = resolver
        self._captured = captured

    def generate_videos(self, model=None, prompt=None, image=None, config=None):
        self._captured.setdefault("prompts", []).append(prompt)
        self._captured["model"] = model
        self._captured["config"] = config
        return self._resolver(prompt)


class _FakeOperations:
    def __init__(self, poll_results):
        self._poll = list(poll_results)

    def get(self, operation):
        if self._poll:
            return self._poll.pop(0)
        return operation


class _FakeClient:
    def __init__(self, models, operations):
        self.models = models
        self.operations = operations


class _FakeClock:
    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self):
        if self._i < len(self._values):
            value = self._values[self._i]
            self._i += 1
            return value
        return self._values[-1]


class _FakeDownloadResponse:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.content


def _install_client(monkeypatch, resolver, poll_results=(), captured=None, init_capture=None):
    captured = captured if captured is not None else {}

    def client_factory(**kwargs):
        if init_capture is not None:
            init_capture.update(kwargs)
        return _FakeClient(_FakeModels(resolver, captured), _FakeOperations(poll_results))

    monkeypatch.setattr(veo_module.genai, "Client", client_factory)
    return captured


def _create_png(path: Path, size=(64, 64)):
    Image.new("RGB", size, color=(20, 40, 60)).save(path)


def _write_project(tmp_path, monkeypatch, items, project_id="project-veo"):
    monkeypatch.chdir(tmp_path)
    shots_dir = tmp_path / "plots" / "storybooks" / project_id / "97_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return project_id, shots_dir


def _make_item(shots_dir: Path, scene: int = 1, shot: int = 1, prompt: str = "A calm sea",
               shot_type: str = "start"):
    start_image = shots_dir / f"start-{scene}-{shot}.png"
    _create_png(start_image)
    return {
        "scene_number": scene,
        "shot_number": shot,
        "shot_type": shot_type,
        "video_prompt": prompt,
        "video_path": str(shots_dir / f"video-{scene}-{shot}.mp4"),
        "timing": "00:00 - 00:06",
        "start_image": str(start_image),
    }


def _patch_common(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(veo_module.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(veo_module, "update_shots_with_descriptions", lambda *a, **k: False)


def _success_from_bytes(prompt):
    del prompt
    return _FakeOperation(done=True, response=_FakeOpResponse([_FakeGenVideo(_FakeVideoObj(video_bytes=b"veo-bytes"))]))


def _success_from_uri(uri):
    def resolver(prompt):
        del prompt
        return _FakeOperation(done=True, response=_FakeOpResponse([_FakeGenVideo(_FakeVideoObj(uri=uri))]))
    return resolver


# --- guard rails ---------------------------------------------------------------------------------

def test_veo_requires_project_id(monkeypatch):
    result = veo_module.video_generator_veo_tool(session_id="s", project_id="", enable=True)
    assert result["status"] == "error"
    assert "project_id" in result["message"]


# --- L-1: signed download uses x-goog-api-key header, key is not put in the URL -------------------

def test_veo_download_sends_api_key_as_header_not_query(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")

    video_uri = "https://generativelanguage.googleapis.com/v1beta/files/out.mp4:download?alt=media"
    _install_client(monkeypatch, _success_from_uri(video_uri))

    captured = {}

    def fake_get(url, headers=None, stream=False, timeout=None):
        del stream, timeout
        captured["url"] = url
        captured["headers"] = headers
        return _FakeDownloadResponse(200, content=b"veo-video-bytes")

    monkeypatch.setattr(veo_module.requests, "get", fake_get)

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 1, "successful": 1, "failed": 0}
    assert Path(item["video_path"]).read_bytes() == b"veo-video-bytes"
    # L-1: ключ уходит заголовком, а URL остаётся чистым (без key= в query)
    assert captured["headers"].get("x-goog-api-key") == "gemini-key"
    assert captured["url"] == video_uri
    assert "key=" not in captured["url"]


def test_veo_saves_video_from_inline_bytes(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")
    _install_client(monkeypatch, _success_from_bytes)

    def forbidden_get(*a, **k):
        raise AssertionError("inline bytes must not trigger an HTTP download")

    monkeypatch.setattr(veo_module.requests, "get", forbidden_get)

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert Path(item["video_path"]).read_bytes() == b"veo-bytes"


# --- M-9: existence filter uses non-empty check --------------------------------------------------

def test_veo_skips_existing_nonempty_video(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    Path(item["video_path"]).write_bytes(b"already-there")
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")

    def forbidden_client(*a, **k):
        raise AssertionError("existing non-empty video must not call Veo")

    monkeypatch.setattr(veo_module.genai, "Client", forbidden_client)

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"] == {"total": 0, "successful": 0, "failed": 0}


def test_veo_regenerates_when_existing_video_is_empty(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    Path(item["video_path"]).write_bytes(b"")  # пустой файл: НЕ должен пропускаться
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")
    _install_client(monkeypatch, _success_from_bytes)

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "success"
    assert result["stats"]["successful"] == 1
    assert Path(item["video_path"]).read_bytes() == b"veo-bytes"


# --- M-8: failed>0 -> status error with top-level error ------------------------------------------

def test_veo_partial_failure_is_reported_as_error(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    good = _make_item(shots_dir, scene=1, shot=1, prompt="A calm sea")
    bad = _make_item(shots_dir, scene=1, shot=2, prompt="A bad sea")
    (shots_dir / "shots.json").write_text(json.dumps({"items": [good, bad]}), encoding="utf-8")

    def resolver(prompt):
        if "bad" in prompt:
            return _FakeOperation(done=True, error="boom")
        return _FakeOperation(done=True, response=_FakeOpResponse([_FakeGenVideo(_FakeVideoObj(video_bytes=b"veo-bytes"))]))

    _install_client(monkeypatch, resolver)

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "error"
    assert result["error"] == result["message"]
    assert result["stats"] == {"total": 2, "successful": 1, "failed": 1}
    assert Path(good["video_path"]).read_bytes() == b"veo-bytes"
    assert not Path(bad["video_path"]).exists()


# --- M-10: polling has a deadline instead of looping forever -------------------------------------

def test_veo_polling_times_out_after_deadline(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    project_id, shots_dir = _write_project(tmp_path, monkeypatch, [])
    item = _make_item(shots_dir)
    (shots_dir / "shots.json").write_text(json.dumps({"items": [item]}), encoding="utf-8")

    # Операция никогда не завершается: дедлайн (600s) должен разорвать цикл.
    def resolver(prompt):
        del prompt
        return _FakeOperation(done=False)

    _install_client(monkeypatch, resolver, poll_results=[])
    # start_time -> 0.0, затем проверка дедлайна -> 601.0 (> 600) -> timeout
    monkeypatch.setattr(veo_module.time, "time", _FakeClock([0.0, 601.0]))

    result = veo_module.video_generator_veo_tool(
        session_id="s", project_id=project_id, enable=True, max_concurrency=1,
    )

    assert result["status"] == "error"
    assert result["stats"] == {"total": 1, "successful": 0, "failed": 1}
    assert "время ожидания" in result["results"][0]["error"]
