"""Чтение PDF/EPUB и сборка файлов пересказа без вызовов моделей."""

import html
import hashlib
import base64
import json
import posixpath
import re
import subprocess
import zipfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET


def save_uploaded_book(root: Path, filename: str, content_base64: str) -> dict:
    filename = Path(filename.replace('\\', '/')).name
    if Path(filename).suffix.lower() not in {'.pdf', '.epub'}:
        raise ValueError('Выберите книгу в формате PDF или EPUB')
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Не удалось прочитать загруженный файл') from exc
    if not content:
        raise ValueError('Файл книги пуст')
    base = (root / 'plots' / 'book_summaries' / uuid.uuid4().hex).resolve()
    source = base / 'input' / filename
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    return {'source_path': str(source), 'output_dir': str(base), 'filename': filename}


def read_chapters(source: Path) -> list[dict]:
    if source.suffix.lower() == '.epub':
        from bs4 import BeautifulSoup

        with zipfile.ZipFile(source) as archive:
            container = ET.fromstring(archive.read('META-INF/container.xml'))
            opf_path = container.find('.//{*}rootfile').attrib['full-path']
            opf = ET.fromstring(archive.read(opf_path))
            items = {item.attrib['id']: item.attrib for item in opf.findall('.//{*}manifest/{*}item')}
            chapters = []
            for ref in opf.findall('.//{*}spine/{*}itemref'):
                if ref.get('linear') == 'no':
                    continue
                item = items[ref.attrib['idref']]
                if item.get('properties') == 'nav':
                    continue
                path = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(item['href'])))
                soup = BeautifulSoup(archive.read(path), 'html.parser')
                for tag in soup(['script', 'style', 'nav']):
                    tag.decompose()
                body = soup.body or soup
                heading = body.find(['h1', 'h2'])
                text = body.get_text('\n', strip=True)
                if text.strip():
                    chapters.append({'title': heading.get_text(' ', strip=True) if heading else Path(path).stem, 'text': text})
    elif source.suffix.lower() == '.pdf':
        text = subprocess.run(
            ['pdftotext', '-enc', 'UTF-8', str(source), '-'],
            capture_output=True, text=True, check=True,
        ).stdout.replace('\f', '\n')
        headings = list(re.finditer(r'(?im)^\s*(?:глава|chapter|часть|part)\s+(?:\d+|[IVXLCDM]+)\b[^\n]*', text))
        chapters = []
        start = 0
        title = source.stem
        for match in headings:
            if text[start:match.start()].strip():
                chapters.append({'title': title, 'text': text[start:match.start()].strip()})
            start, title = match.start(), match.group().strip()
        if text[start:].strip():
            chapters.append({'title': title, 'text': text[start:].strip()})
    else:
        raise ValueError('Нужен файл PDF или EPUB')
    if not chapters:
        raise ValueError('В книге нет извлекаемого текста. Для скана нужен текстовый PDF.')
    return chapters


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def build_files(base: Path, book: dict) -> dict:
    import markdown2
    from weasyprint import HTML

    out = base / 'final'
    out.mkdir(exist_ok=True)
    sections = [('Введение', book['introduction'])]
    sections += [(c['title'], c['text']) for c in book['chapters']]
    sections += [('Заключение', book['conclusion'])]
    markdown = '# ' + book['title'] + '\n\n' + '\n\n'.join('## ' + title + '\n\n' + text for title, text in sections)
    md_path = out / 'book.md'
    md_path.write_text(markdown, encoding='utf-8')
    fragments = [str(markdown2.markdown('## ' + title + '\n\n' + text, extras=['fenced-code-blocks', 'tables'])) for title, text in sections]
    css = 'body {font-family: sans-serif; line-height: 1.6; max-width: 48em; margin: 2em auto; padding: 0 1em;} pre {white-space: pre-wrap;} @page {size: A4; margin: 18mm;}'
    links = ''.join(f'<li><a href="#section-{i}">{html.escape(title)}</a></li>' for i, (title, _) in enumerate(sections))
    body = '<h1>' + html.escape(book['title']) + '</h1><nav><ol>' + links + '</ol></nav>'
    body += ''.join(f'<section id="section-{i}">{fragment}</section>' for i, fragment in enumerate(fragments))
    page = f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(book["title"])}</title><style>{css}</style></head><body>{body}</body></html>'
    html_path = out / 'index.html'
    html_path.write_text(page, encoding='utf-8')
    pdf_path = out / 'book.pdf'
    HTML(string=page).write_pdf(str(pdf_path))
    png_dir = out / 'pages'
    png_dir.mkdir(exist_ok=True)
    # Сначала выводим во временный каталог, чтобы убрать лишние старые страницы
    # только после успешной сборки новой версии.
    import tempfile

    with tempfile.TemporaryDirectory(dir=out) as temp:
        subprocess.run(['pdftoppm', '-png', '-scale-to', '1600', str(pdf_path), str(Path(temp) / 'page')], check=True, capture_output=True)
        for old in png_dir.glob('page-*.png'):
            old.unlink()
        for png in Path(temp).glob('page-*.png'):
            png.replace(png_dir / png.name)

    epub_path = out / 'book.epub'
    identifier = 'urn:sha256:' + hashlib.sha256(markdown.encode()).hexdigest()
    with zipfile.ZipFile(epub_path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('EPUB/style.css', css)
        manifest = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/>'
        spine = ''
        nav_links = ''
        for i, (title, _) in enumerate(sections):
            # HTML-парсер нормализует одиночные теги и сущности для XHTML.
            from bs4 import BeautifulSoup

            fragment = BeautifulSoup(fragments[i], 'html.parser').decode(formatter='minimal')
            archive.writestr(f'EPUB/section-{i}.xhtml', f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="ru"><head><title>{html.escape(title)}</title><link rel="stylesheet" href="style.css"/></head><body>{fragment}</body></html>')
            manifest += f'<item id="s{i}" href="section-{i}.xhtml" media-type="application/xhtml+xml"/>'
            spine += f'<itemref idref="s{i}"/>'
            nav_links += f'<li><a href="section-{i}.xhtml">{html.escape(title)}</a></li>'
        archive.writestr('EPUB/nav.xhtml', f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Оглавление</title></head><body><nav epub:type="toc"><ol>{nav_links}</ol></nav></body></html>')
        modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        archive.writestr('EPUB/package.opf', f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">{identifier}</dc:identifier><dc:title>{html.escape(book["title"])}</dc:title><dc:language>ru</dc:language><meta property="dcterms:modified">{modified}</meta></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
    return {'markdown_path': str(md_path), 'html_path': str(html_path), 'epub_path': str(epub_path), 'png_dir': str(png_dir)}
