import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from custom_tools import image_tools


@pytest.mark.parametrize('format', ['b64_json', 'data_url', 'https_url'])
def test_generate_image_saves_supported_provider_response(tmp_path, monkeypatch, format):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TEXT2IMAGE_MODEL', 'llmgateway/gpt-image-2')
    monkeypatch.setattr(image_tools, '_images_generations_url', lambda: 'https://gateway.example/images/generations')
    monkeypatch.setattr(image_tools, '_resolve_openai_images_api_key', lambda: 'test-key')
    buffer = BytesIO()
    Image.new('RGB', (3, 4), 'green').save(buffer, format='PNG')
    png = buffer.getvalue()
    encoded = base64.b64encode(png).decode()
    url = 'https://images.example/finished.png'
    item = ({'b64_json': encoded} if format == 'b64_json' else
            {'url': 'data:image/png;base64,' + encoded} if format == 'data_url' else {'url': url})

    class Session:
        headers = {}

        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {'data': [item]})

        def close(self):
            pass

    downloads = []

    def download(request_url, **kwargs):
        downloads.append(request_url)
        assert 'headers' not in kwargs
        return SimpleNamespace(content=png, raise_for_status=lambda: None)

    monkeypatch.setattr(image_tools.requests, 'Session', Session)
    monkeypatch.setattr(image_tools.requests, 'get', download)
    result = image_tools.generate_image_tool('A young character', 'pilot', 1)
    assert Path(result).is_file(), result
    with Image.open(result) as image:
        assert image.size == (3, 4)
    assert downloads == ([url] if format == 'https_url' else [])


@pytest.mark.parametrize('model,size,expected', [
    ('llmgateway/gpt-image-2','1080x1920','1088x1920'),
    ('llmgateway/gpt-image-2','1920x1088','1920x1088'),
    ('llmgateway/gpt-image-2','941x1672','944x1680'),
    ('llmgateway/ai-qwen-image','1080x1920','1080x1920'),
])
def test_provider_request_uses_supported_image_dimensions(model, size, expected):
    payload = {'model':model, 'prompt':'A room', 'size':size}
    image_tools._adapt_gpt_image_request(payload)
    assert payload['size'] == expected


def test_edit_image_vse_resolves_moved_reference_without_series_scope(tmp_path, monkeypatch):
    import json
    from custom_tools.storybook import reference_catalog as catalog
    root = tmp_path / 'world'
    (root / 'series/one').mkdir(parents=True)
    (root / 'project.json').write_text(json.dumps({'format': 'storybook-series-v1'}))
    old = root / '20_bible/references/characters/hero.png'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'hero')
    new = catalog.store_reference(root / 'series/one', old, 'character', 'hero')
    assert not old.exists()
    sent = []
    monkeypatch.setattr(image_tools, '_images_generations_url', lambda: 'https://gateway.example/generations')
    monkeypatch.setattr(image_tools, '_images_edits_url', lambda: 'https://gateway.example/edits')
    monkeypatch.setattr(image_tools, '_resolve_openai_images_api_key', lambda: 'test-key')
    monkeypatch.setenv('IMG2IMG_MODEL', 'llmgateway/test')
    monkeypatch.setattr(image_tools, '_edit_image_vse_post_edits_multipart',
                        lambda url, key, model, prompt, paths, *a: sent.append(paths) or SimpleNamespace(status_code=500, text='stop'))
    image_tools.edit_image_vse_tool('edit', [str(old)], 'session')
    assert sent == [[new]]


def test_moved_reference_path_ignores_catalog_errors(tmp_path, monkeypatch):
    from custom_tools.storybook import project_paths
    catalog = tmp_path / 'shared/reference_catalog.json'
    catalog.parent.mkdir()
    catalog.write_text('{}')
    def broken(base, value):
        raise OSError('catalog unavailable')
    monkeypatch.setattr(project_paths, 'existing_reference_path', broken)
    missing = str(tmp_path / '20_bible/references/hero.png')
    assert image_tools._moved_reference_path(missing) == missing
