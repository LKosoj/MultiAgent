import pytest
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


def test_edit_image_vse_tool_uses_default_size(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("IMG2IMG_MODEL", "img2img-google/flash-edit")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1920, 1088))}]}

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
    assert captured["json"]["size"] == "1920x1088"
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
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")

    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data) if data is not None else {}
        captured["timeout"] = timeout
        return _FakeResponse(
            status_code=200,
            payload={"data": [{"url": "data:image/png;base64," + _png_b64(size=(1920, 1088))}]},
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
    assert captured["data"]["model"] == "llmgateway/gpt-image-2"
    assert captured["data"]["size"] == "1920x1088"
    assert captured["timeout"] == (30, 1200)


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


def test_generate_image_tool_uses_default_size(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "txt2img-test-model")

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=(1920, 1088))}]}
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
    assert captured["json"]["size"] == "1920x1088"
    assert captured["json"]["model"] == "txt2img-test-model"
    assert captured["timeout"] == 1200


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


@pytest.mark.parametrize(
    ("width", "height", "api_size", "response_size", "output_size"),
    [
        (1920, 1088, "1920x1088", (1920, 1088), (1920, 1088)),
        (1280, 720, "1280x720", (1280, 720), (1280, 720)),
    ],
)
def test_generate_image_tool_gpt_image_2_size_contract(
    tmp_path, monkeypatch, width, height, api_size, response_size, output_size
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("TEXT2IMAGE_MODEL", "llmgateway/gpt-image-2")

    captured = {}
    response_payload = {"data": [{"b64_json": _png_b64(size=response_size)}]}
    monkeypatch.setattr(
        image_tools.requests,
        "Session",
        lambda: _FakeSession(captured, response_payload),
    )

    result = image_tools.generate_image_tool(
        prompt="cinematic horizontal shot",
        session_id="sess-gpt-image-2",
        number=7,
        seed=123,
        negative_prompt="лишние надписи",
        width=width,
        height=height,
    )

    assert (tmp_path / result).is_file()
    with Image.open(tmp_path / result) as image:
        assert image.size == output_size
    assert captured["url"] == "http://example.test/v1/images/generations"
    assert captured["json"]["model"] == "llmgateway/gpt-image-2"
    assert captured["json"]["size"] == api_size
    assert "лишние надписи" in captured["json"]["prompt"]
    assert "response_format" not in captured["json"]
    assert "negative_prompt" not in captured["json"]
    assert captured["json"]["seed"] == 123
    assert captured["timeout"] == 1200


@pytest.mark.parametrize(
    ("width", "height", "api_size", "response_size", "output_size"),
    [
        (1920, 1088, "1920x1088", (1920, 1088), (1920, 1088)),
        (1280, 720, "1280x720", (1280, 720), (1280, 720)),
    ],
)
def test_edit_image_vse_tool_gpt_image_2_size_contract(
    tmp_path, monkeypatch, width, height, api_size, response_size, output_size
):
    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "sk-test")
    monkeypatch.setenv("IMG2IMG_MODEL", "llmgateway/gpt-image-2")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(base64.b64decode(_png_b64()))
    output_path = tmp_path / "edited.png"
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data) if data is not None else {}
        captured["timeout"] = timeout
        return _FakeResponse(
            status_code=200,
            payload={
                "data": [
                    {"url": "data:image/png;base64," + _png_b64(size=response_size)}
                ]
            },
        )

    monkeypatch.setattr(image_tools.requests, "post", fake_post)

    result = image_tools.edit_image_vse_tool(
        prompt="cinematic horizontal shot",
        image_paths=[str(source_image)],
        session_id="sess-gpt-image-2",
        output_path=str(output_path),
        seed=123,
        negative_prompt="лишние надписи",
        width=width,
        height=height,
    )

    assert result == str(output_path.absolute())
    with Image.open(output_path) as image:
        assert image.size == output_size
    assert captured["url"] == "http://example.test/v1/images/edits"
    assert captured["data"]["model"] == "llmgateway/gpt-image-2"
    assert captured["data"]["size"] == api_size
    assert "лишние надписи" in captured["data"]["prompt"]
    assert "response_format" not in captured["data"]
    assert "negative_prompt" not in captured["data"]
    assert captured["data"]["seed"] == "123"
    assert captured["timeout"] == (30, 1200)


@pytest.mark.parametrize('operation', ['generate', 'edit'])
def test_provider_error_inside_http_200_is_reported(tmp_path, monkeypatch, operation):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OPENAI_API_BASE_DB', 'http://example.test/v1')
    monkeypatch.setenv('OPENAI_API_KEY_DB', 'test')
    monkeypatch.setenv('TEXT2IMAGE_MODEL', 'llmgateway/gpt-image-2')
    monkeypatch.setenv('IMG2IMG_MODEL', 'llmgateway/gpt-image-2')
    payload = {'error': {'code': 403, 'message': 'Provider Terms Of Service restriction'}}
    monkeypatch.setattr(image_tools.requests, 'Session', lambda: _FakeSession({}, payload))
    monkeypatch.setattr(image_tools.requests, 'post', lambda *args, **kwargs: _FakeResponse(payload=payload))
    if operation == 'generate':
        result = image_tools.generate_image_tool('A fully clothed character', 's', 1)
    else:
        source = tmp_path / 'source.png'
        source.write_bytes(base64.b64decode(_png_b64()))
        result = image_tools.edit_image_vse_tool('Retain the character', [str(source)], 's')
    assert '403' in result
    assert 'Terms Of Service' in result
