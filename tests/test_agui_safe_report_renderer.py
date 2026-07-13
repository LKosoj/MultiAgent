from __future__ import annotations

import base64
import gzip
from hashlib import sha256
from html.parser import HTMLParser

import pytest


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _DomAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _decode(envelope) -> tuple[bytes, str]:
    compressed = base64.b64decode(envelope.content_b64_gzip, validate=True)
    rendered = gzip.decompress(compressed)
    return rendered, rendered.decode("utf-8")


def test_static_renderer_escapes_every_scalar_and_emits_only_inert_dom() -> None:
    from backend.fastapi_app.agui.report_renderer import (
        CodeSection,
        ReportTable,
        TextSection,
        TrustedPng,
        render_static_report,
    )

    attack = (
        '<script>alert(1)</script><img src=x onerror=alert(2)>'
        '<form action="https://evil"><iframe src="javascript:alert(3)"></iframe></form>'
    )
    envelope = render_static_report(
        title=attack,
        text_sections=[TextSection(heading=attack, text=attack)],
        code_sections=[CodeSection(label=attack, code=attack, language=attack)],
        tables=[ReportTable(title=attack, columns=[attack], rows=[[attack]])],
        images=[TrustedPng(label=attack, filename=attack, alt=attack, data=_PNG_1X1)],
    )
    rendered, html = _decode(envelope)
    audit = _DomAudit()
    audit.feed(html)

    assert envelope.renderer_version == "static-v1"
    assert envelope.mime_type == "text/html; charset=utf-8"
    assert envelope.content_encoding == "gzip+base64"
    assert envelope.content_sha256 == sha256(rendered).hexdigest()
    assert set(audit.tags) <= {
        "html", "head", "meta", "title", "style", "body", "main", "header",
        "h1", "section", "h2", "p", "pre", "code", "table", "thead",
        "tbody", "tr", "th", "td", "figure", "img", "figcaption",
    }
    assert not ({"script", "form", "iframe", "frame", "svg", "object", "embed"} & set(audit.tags))
    assert not any(name.lower().startswith("on") for name, _ in audit.attributes)
    sources = [value for name, value in audit.attributes if name.lower() == "src"]
    assert sources and all(value and value.startswith("data:image/png;base64,") for value in sources)
    assert not [value for name, value in audit.attributes if name.lower() in {"href", "action"}]
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html


def test_static_renderer_csp_digest_and_payload_are_deterministic() -> None:
    from backend.fastapi_app.agui.report_renderer import TextSection, render_static_report

    first = render_static_report(title="Report", text_sections=[TextSection(text="Body")])
    second = render_static_report(title="Report", text_sections=[TextSection(text="Body")])
    rendered, html = _decode(first)

    assert first == second
    assert first.content_sha256 == sha256(rendered).hexdigest()
    assert (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'"
    ) in html
    assert '<meta http-equiv="Content-Security-Policy"' in html
    assert "<script" not in html.lower()
    assert "http://" not in html.lower()
    assert "https://" not in html.lower()
    assert set(first.to_mapping()) == {
        "renderer_version",
        "content_sha256",
        "mime_type",
        "content_encoding",
        "content_b64_gzip",
    }


@pytest.mark.parametrize(
    "image_data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
        b"data:text/html,<script>alert(1)</script>",
        b"https://example.test/report.png",
        b"not-a-png",
    ],
)
def test_static_renderer_rejects_non_png_image_bytes(image_data: bytes) -> None:
    from backend.fastapi_app.agui.report_renderer import TrustedPng, render_static_report

    with pytest.raises(ValueError, match="PNG"):
        render_static_report(
            title="Report",
            images=[TrustedPng(data=image_data, alt="image")],
        )


def test_static_renderer_rejects_table_row_width_mismatch() -> None:
    from backend.fastapi_app.agui.report_renderer import ReportTable, render_static_report

    with pytest.raises(ValueError, match="columns"):
        render_static_report(
            title="Report",
            tables=[ReportTable(columns=["one"], rows=[["a", "b"]])],
        )
