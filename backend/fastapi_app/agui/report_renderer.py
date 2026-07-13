"""Inert-by-construction renderer for browser-delivered static reports."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import gzip
from hashlib import sha256
from html import escape
from typing import Any, Mapping, Sequence


RENDERER_VERSION = "static-v1"
REPORT_MIME_TYPE = "text/html; charset=utf-8"
REPORT_CONTENT_ENCODING = "gzip+base64"
REPORT_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class TextSection:
    text: str
    heading: str | None = None


@dataclass(frozen=True)
class CodeSection:
    code: str
    label: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class ReportTable:
    columns: Sequence[Any]
    rows: Sequence[Sequence[Any]]
    title: str | None = None


@dataclass(frozen=True)
class TrustedPng:
    data: bytes
    alt: str
    label: str | None = None
    filename: str | None = None


@dataclass(frozen=True)
class StaticReportEnvelope:
    renderer_version: str
    content_sha256: str
    mime_type: str
    content_encoding: str
    content_b64_gzip: str

    def to_mapping(self) -> Mapping[str, str]:
        return {
            "renderer_version": self.renderer_version,
            "content_sha256": self.content_sha256,
            "mime_type": self.mime_type,
            "content_encoding": self.content_encoding,
            "content_b64_gzip": self.content_b64_gzip,
        }


def _escaped(value: Any) -> str:
    return escape(str(value), quote=True)


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or null")
    return value


def _render_text_section(section: TextSection) -> str:
    if not isinstance(section, TextSection) or not isinstance(section.text, str):
        raise TypeError("text sections must contain string text")
    heading = _optional_text(section.heading, "text heading")
    parts = ['<section class="text-section">']
    if heading is not None:
        parts.append(f"<h2>{_escaped(heading)}</h2>")
    parts.append(f"<p>{_escaped(section.text)}</p></section>")
    return "".join(parts)


def _render_code_section(section: CodeSection) -> str:
    if not isinstance(section, CodeSection) or not isinstance(section.code, str):
        raise TypeError("code sections must contain string code")
    label = _optional_text(section.label, "code label")
    language = _optional_text(section.language, "code language")
    parts = ['<section class="code-section">']
    if label is not None:
        parts.append(f"<h2>{_escaped(label)}</h2>")
    if language is not None:
        parts.append(f'<p class="meta">Language: {_escaped(language)}</p>')
    parts.append(f"<pre><code>{_escaped(section.code)}</code></pre></section>")
    return "".join(parts)


def _render_table(table: ReportTable) -> str:
    if not isinstance(table, ReportTable):
        raise TypeError("tables must be ReportTable instances")
    title = _optional_text(table.title, "table title")
    columns = list(table.columns)
    rows = [list(row) for row in table.rows]
    if any(len(row) != len(columns) for row in rows):
        raise ValueError("table rows must match table columns")
    parts = ['<section class="table-section">']
    if title is not None:
        parts.append(f"<h2>{_escaped(title)}</h2>")
    parts.append("<table><thead><tr>")
    parts.extend(f"<th>{_escaped(column)}</th>" for column in columns)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(f"<td>{_escaped(value)}</td>" for value in row)
        parts.append("</tr>")
    parts.append("</tbody></table></section>")
    return "".join(parts)


def _render_png(image: TrustedPng) -> str:
    if not isinstance(image, TrustedPng):
        raise TypeError("images must be TrustedPng instances")
    if (
        type(image.data) is not bytes
        or not image.data.startswith(_PNG_SIGNATURE)
        or len(image.data) < 24
        or image.data[12:16] != b"IHDR"
        or b"IEND" not in image.data[-20:]
    ):
        raise ValueError("trusted image data must be PNG bytes")
    if not isinstance(image.alt, str):
        raise TypeError("image alt must be a string")
    label = _optional_text(image.label, "image label")
    filename = _optional_text(image.filename, "image filename")
    payload = base64.b64encode(image.data).decode("ascii")
    parts = ['<figure class="image-section">']
    if label is not None:
        parts.append(f"<h2>{_escaped(label)}</h2>")
    parts.append(f'<img src="data:image/png;base64,{payload}" alt="{_escaped(image.alt)}">')
    if filename is not None:
        parts.append(f"<figcaption>{_escaped(filename)}</figcaption>")
    parts.append("</figure>")
    return "".join(parts)


def render_static_report(
    *,
    title: str,
    text_sections: Sequence[TextSection] = (),
    code_sections: Sequence[CodeSection] = (),
    tables: Sequence[ReportTable] = (),
    images: Sequence[TrustedPng] = (),
) -> StaticReportEnvelope:
    """Render structured data through a fixed inert HTML template."""
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    body = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f'<meta http-equiv="Content-Security-Policy" content="{REPORT_CSP}">',
        f"<title>{_escaped(title)}</title>",
        "<style>",
        "body{margin:0;background:#0b0d12;color:#e8ecf3;font:14px/1.55 system-ui,sans-serif}",
        "main{max-width:1120px;margin:0 auto;padding:32px}header,section,figure{margin:0 0 24px}",
        "h1,h2{line-height:1.2}h1{font-size:28px}h2{font-size:18px}",
        "p,pre{white-space:pre-wrap}pre{overflow:auto;padding:16px;background:#141821}",
        "table{width:100%;border-collapse:collapse}th,td{padding:8px;border:1px solid #303746;text-align:left}",
        "img{display:block;max-width:100%;height:auto}.meta,figcaption{color:#9aa4b5}",
        "</style></head><body><main><header>",
        f"<h1>{_escaped(title)}</h1></header>",
    ]
    body.extend(_render_text_section(section) for section in text_sections)
    body.extend(_render_code_section(section) for section in code_sections)
    body.extend(_render_table(table) for table in tables)
    body.extend(_render_png(image) for image in images)
    body.append("</main></body></html>")
    rendered = "".join(body).encode("utf-8")
    compressed = gzip.compress(rendered, compresslevel=9, mtime=0)
    return StaticReportEnvelope(
        renderer_version=RENDERER_VERSION,
        content_sha256=sha256(rendered).hexdigest(),
        mime_type=REPORT_MIME_TYPE,
        content_encoding=REPORT_CONTENT_ENCODING,
        content_b64_gzip=base64.b64encode(compressed).decode("ascii"),
    )

