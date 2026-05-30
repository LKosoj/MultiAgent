import base64
from pathlib import Path

import pytest

import html_utils


def test_download_image_for_embed_rejects_loopback_without_request(monkeypatch):
    called = False

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("requests.get must not be called for loopback URLs")

    monkeypatch.setattr(html_utils.requests, "get", fake_get)

    with pytest.raises(ValueError, match="non-public|not allowed"):
        html_utils._download_image_for_embed("http://127.0.0.1/image.png")

    assert called is False


def test_download_image_for_embed_rejects_dns_to_private_ip(monkeypatch):
    called = False

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 0))]

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("requests.get must not be called for private DNS results")

    monkeypatch.setattr(html_utils.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(html_utils.requests, "get", fake_get)

    with pytest.raises(ValueError, match="non-public"):
        html_utils._download_image_for_embed("https://example.test/image.png")

    assert called is False


def test_convert_markdown_embeds_only_local_images_inside_plots(tmp_path):
    plots_dir = tmp_path / "plots"
    plots_dir.mkdir()
    inside = plots_dir / "inside.png"
    outside = tmp_path / "outside.png"
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADElEQVR42mP8z8AARQAHAQGJ"
        "p7X2AAAAAElFTkSuQmCC"
    )
    inside.write_bytes(png_bytes)
    outside.write_bytes(png_bytes)

    visualizer = html_utils.HTMLVisualizer(plots_dir=str(plots_dir))

    blocked_html = visualizer._convert_markdown(f'<img src="{outside}">')
    allowed_html = visualizer._convert_markdown(f'<img src="{inside}">')

    assert "data:image/png;base64" not in blocked_html
    assert str(outside) in blocked_html
    assert "data:image/png;base64" in allowed_html
