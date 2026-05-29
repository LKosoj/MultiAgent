import base64
import sys
import types
from io import BytesIO

from PIL import Image

sys.modules.setdefault("agent_command", types.SimpleNamespace(model_vision=None))
sys.modules.setdefault(
    "utils",
    types.SimpleNamespace(
        extract_json_from_markdown=lambda value: value,
        call_openai_api=None,
    ),
)

from custom_tools import image_tools


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, captured, response_payload):
        self.headers = {}
        self._captured = captured
        self._response_payload = response_payload

    def post(self, url, json=None, timeout=None):
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["timeout"] = timeout
        return _FakeResponse(status_code=200, payload=self._response_payload)


def _png_b64(size=(64, 64)):
    image = Image.new("RGB", size, color=(10, 20, 30))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def test_edit_image_vse_tool_uses_horizontal_16_9_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("IMG2IMG_MODEL", "img2img-google/flash-edit")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1920, 1080))}]}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["edits_url"] = url
        return _FakeResponse(
            status_code=404,
            text='{"detail":"No image edit route configured for model \'x\'."}',
        )

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    output_path = tmp_path / "edited.png"
    result = image_tools.edit_image_vse_tool(
        prompt="cinematic horizontal shot",
        image_paths=[str(source_image)],
        session_id="sess-1",
        output_path=str(output_path),
    )

    assert result == str(output_path.absolute())
    assert output_path.exists()
    assert captured["edits_url"] == "http://example.test/v1/images/edits"
    assert captured["url"] == "http://example.test/v1/images/generations"
    assert captured["json"]["size"] == "1920x1080"
    assert captured["json"]["model"] == "img2img-google/flash-edit"


def test_edit_image_vse_tool_respects_explicit_size_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.delenv("IMG2IMG_MODEL", raising=False)
    monkeypatch.setenv("VSEGPT_IMG2IMG_MODEL", "img2img-google/flash-edit")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1280, 720))}]}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["edits_url"] = url
        return _FakeResponse(
            status_code=404,
            text='{"detail":"No image edit route configured for model \'x\'."}',
        )

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    output_path = tmp_path / "edited.png"
    image_tools.edit_image_vse_tool(
        prompt="cinematic horizontal shot",
        image_paths=[str(source_image)],
        session_id="sess-1",
        output_path=str(output_path),
        width=1280,
        height=720,
    )

    assert captured["url"] == "http://example.test/v1/images/generations"
    assert captured["json"]["size"] == "1280x720"


def test_edit_image_vse_tool_default_uses_images_edits(tmp_path, monkeypatch):
    """Редактирование: сначала POST .../images/edits (IMG2IMG_MODEL)."""
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/flux.image-edit")

    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data) if data is not None else {}
        return _FakeResponse(
            status_code=200,
            payload={"data": [{"url": "data:image/png;base64," + _png_b64(size=(1920, 1080))}]},
        )

    monkeypatch.setattr(image_tools.requests, "post", fake_post)

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))

    output_path = tmp_path / "edited.png"
    result = image_tools.edit_image_vse_tool(
        prompt="cinematic horizontal shot",
        image_paths=[str(source_image)],
        session_id="sess-1",
        output_path=str(output_path),
    )

    assert result == str(output_path.absolute())
    assert output_path.exists()
    assert captured["url"] == "http://example.test/v1/images/edits"
    assert captured["data"]["model"] == "llmgateway/flux.image-edit"
    assert captured["data"]["size"] == "1920x1080"


def test_edit_image_vse_tool_accepts_data_url_in_response(tmp_path, monkeypatch):
    """Шлюз может вернуть data:image/...;base64,... в data[0].url вместо b64_json."""
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("IMG2IMG_MODEL", "gw/img2img")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))

    data_uri = "data:image/png;base64," + _png_b64(size=(512, 512))
    captured = {}
    response_payload = {"data": [{"url": data_uri}]}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["edits_url"] = url
        return _FakeResponse(
            status_code=404,
            text='{"detail":"No image edit route configured for model \'x\'."}',
        )

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    output_path = tmp_path / "edited.png"
    result = image_tools.edit_image_vse_tool(
        prompt="test",
        image_paths=[str(source_image)],
        session_id="sess-1",
        output_path=str(output_path),
        width=512,
        height=512,
    )

    assert result == str(output_path.absolute())
    assert output_path.exists()


def test_generate_image_tool_uses_horizontal_16_9_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "txt2img-test-model")

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1920, 1080))}]}
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    result = image_tools.generate_image_tool(
        prompt="cinematic horizontal shot",
        session_id="sess-1",
        number=7,
    )

    assert result.startswith("plots/")
    assert (tmp_path / result).is_file()
    assert captured["url"] == "http://example.test/v1/images/generations"
    assert captured["json"]["size"] == "1920x1080"
    assert captured["json"]["model"] == "txt2img-test-model"


def test_generate_image_tool_respects_explicit_size_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "txt2img-test-model")

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1280, 720))}]}
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    image_tools.generate_image_tool(
        prompt="cinematic horizontal shot",
        session_id="sess-1",
        number=7,
        width=1280,
        height=720,
    )

    assert captured["json"]["size"] == "1280x720"
