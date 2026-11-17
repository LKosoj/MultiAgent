import base64
from pathlib import Path

import pytest

from backend.fastapi_app.agui import service
from backend.fastapi_app.agui.auth import Principal


@pytest.fixture
def upload(tmp_path, monkeypatch):
    monkeypatch.setattr(service, '_project_root', lambda: tmp_path)
    admin = Principal(subject='test', roles=frozenset({'admin'}))

    def upload(filename='Книга.pdf', content=b'%PDF-1.7\nbook'):
        return service.handle_service_action('workflows.upload_book', {
            'filename': filename, 'content_base64': base64.b64encode(content).decode('ascii'),
        }, principal=admin)

    return upload


@pytest.mark.parametrize('name', ['Книга.pdf', 'Book.EPUB'])
def test_upload_preserves_bytes_and_returns_workflow_parameters(upload, name):
    data = b'book\x00\xff\x01'
    result = upload(name, data)
    source = Path(result['source_path'])
    assert source.read_bytes() == data
    assert source.name == name
    assert source.parent == Path(result['output_dir']) / 'input'
    assert result['filename'] == name


def test_same_filename_does_not_overwrite_another_upload(upload):
    first = upload(content=b'first')
    second = upload(content=b'second')
    assert first['output_dir'] != second['output_dir']
    assert Path(first['source_path']).read_bytes() == b'first'
    assert Path(second['source_path']).read_bytes() == b'second'


@pytest.mark.parametrize('name', ['../../book.pdf', 'C:\\books\\book.pdf'])
def test_browser_filename_stays_in_upload_directory(upload, name):
    result = upload(name)
    assert result['filename'] == 'book.pdf'
    assert Path(result['source_path']).parent == Path(result['output_dir']) / 'input'


@pytest.mark.parametrize('filename,content,error', [
    ('book.txt', b'text', 'PDF или EPUB'),
    ('book.pdf', b'', 'пуст'),
])
def test_invalid_book_is_not_saved(upload, tmp_path, filename, content, error):
    with pytest.raises(ValueError, match=error):
        upload(filename, content)
    assert not list(tmp_path.rglob('*'))


@pytest.mark.parametrize('encoded', ['???', None])
def test_invalid_file_payload_is_not_saved(tmp_path, monkeypatch, encoded):
    monkeypatch.setattr(service, '_project_root', lambda: tmp_path)
    with pytest.raises(ValueError, match='прочитать'):
        service.handle_service_action('workflows.upload_book', {
            'filename': 'book.pdf', 'content_base64': encoded,
        }, principal=Principal(subject='test', roles=frozenset({'admin'})))
    assert not list(tmp_path.rglob('*'))


def test_upload_uses_workflow_access_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(service, '_project_root', lambda: tmp_path)
    with pytest.raises(PermissionError, match='admin'):
        service.handle_service_action('workflows.upload_book', {
            'filename': 'book.pdf', 'content_base64': 'Ym9vaw==',
        }, principal=Principal(subject='user'))
    assert not list(tmp_path.rglob('*'))
