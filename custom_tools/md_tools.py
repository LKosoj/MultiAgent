import os
import json
import glob
import shutil
from typing import List, Optional, Dict, Any, Tuple


def _ensure_parent_dir(path: Optional[str]) -> None:
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _natural_key(s: str):
    import re
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def _sort_paths(paths: List[str], order: str, order_regex: Optional[str]) -> List[str]:
    if order == "by_regex" and order_regex:
        import re
        pattern = re.compile(order_regex)

        def key_fn(p: str) -> Tuple[int, str]:
            m = pattern.search(p)
            if m:
                try:
                    return (int(m.group(1)), p)
                except Exception:
                    return (0, p)
            return (0, p)

        return sorted(paths, key=key_fn)

    if order == "by_mtime":
        return sorted(paths, key=lambda p: (os.path.getmtime(p), _natural_key(p)))

    # default: alphanumeric/natural
    return sorted(paths, key=_natural_key)


def _rewrite_link(image_path: str, output_dir: str, link_strategy: str, base_url: Optional[str]) -> str:
    abs_path = os.path.abspath(image_path)
    if link_strategy == "absolute":
        return abs_path
    if link_strategy == "url" and base_url:
        rel = os.path.relpath(abs_path, output_dir).replace(os.sep, "/")
        if not base_url.endswith("/"):
            base_url_use = base_url + "/"
        else:
            base_url_use = base_url
        return base_url_use + rel
    # default: relative
    return os.path.relpath(abs_path, output_dir)


def _copy_asset_if_needed(src_path: str, assets_copy_dir: Optional[str]) -> str:
    if not assets_copy_dir:
        return src_path
    os.makedirs(assets_copy_dir, exist_ok=True)
    dst_path = os.path.join(assets_copy_dir, os.path.basename(src_path))
    if os.path.abspath(src_path) != os.path.abspath(dst_path):
        try:
            shutil.copy2(src_path, dst_path)
            return dst_path
        except Exception:
            # если копирование не удалось, используем исходный
            return src_path
    return src_path


def md_assembler_tool(
    session_id: str,
    mode: str,
    output_path: str,
    # discovery params
    image_globs: Optional[List[str]] = None,
    fallback_globs: Optional[List[str]] = None,
    order: str = "by_regex",
    order_regex: Optional[str] = r"page_(\d+)",
    # link/render options
    title: Optional[str] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
    link_strategy: str = "relative",
    base_url: Optional[str] = None,
    assets_copy_dir: Optional[str] = None,
    toc: bool = False,
    heading_level_offset: int = 0,
    # manifest params
    manifest_path: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None,
    # optional story text
    story_json_path: Optional[str] = None,
    text_mode: str = "none",  # none | caption | body
    skip_if_exists: bool = True,
) -> str:
    """
    Универсальный инструмент сборки Markdown-документа из изображений и/или манифеста.

    Args:
        session_id: Идентификатор сессии, используется для трассировки.
        mode: 'manifest' | 'discovery'. Режим работы инструмента.
        output_path: Абсолютный путь, куда сохранить итоговый .md файл.
        image_globs: (discovery) Список glob-шаблонов для поиска изображений.
        fallback_globs: (discovery) Доп. шаблоны, если по основным ничего не найдено.
        order: (discovery) Порядок: 'by_regex' | 'by_mtime' | 'alphanumeric'.
        order_regex: (discovery) Регулярное выражение с группой номера для сортировки.
        title: Заголовок документа (опц.).
        frontmatter: YAML frontmatter (dict), будет добавлен в начале документа (опц.).
        link_strategy: 'absolute' | 'relative' | 'url' — как формировать ссылки.
        base_url: Базовый URL (если link_strategy='url').
        assets_copy_dir: Куда копировать ассеты (опц.). Если задано — ссылки укажут на копии.
        toc: Генерировать оглавление (простая маркдаун-версия по заголовкам).
        heading_level_offset: Сдвиг уровней заголовков (например, +1 добавит один #).
        manifest_path: (manifest) Путь к JSON/JSONL манифесту блоков.
        variables: (manifest) Переменные для подстановки плейсхолдеров.
        story_json_path: Путь к JSON файлу с текстом истории по страницам.
        text_mode: 'none' | 'caption' | 'body' - режим добавления текста к изображениям.
        skip_if_exists: Пропускать, если файл уже существует.

    Returns:
        JSON-строка: { "md_path": str, "num_blocks": int, "num_images": int, "warnings": [] }
    """
    warnings: List[str] = []

    if not output_path:
        raise ValueError("output_path обязателен")
    if not os.path.isabs(output_path):
        output_path = os.path.abspath(output_path)
    _ensure_parent_dir(output_path)
    output_dir = os.path.dirname(output_path) or os.getcwd()

    # Проверяем, существует ли уже файл
    import logging
    logger = logging.getLogger(__name__)
    if os.path.exists(output_path) and skip_if_exists:
        logger.info(f"✏️ Файл уже существует (найден {output_path}), пропускаем генерацию")
        result = {
            "md_path": output_path,
            "num_blocks": 0,
            "num_images": 0,
            "warnings": "Файл уже существует, генерация пропущена",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    lines: List[str] = []

    # frontmatter
    if frontmatter:
        try:
            from ruamel.yaml import YAML  # type: ignore
            yaml = YAML()
            from io import StringIO
            buf = StringIO()
            yaml.dump(frontmatter, buf)
            lines.append("---")
            lines.append(buf.getvalue().strip())
            lines.append("---\n")
        except Exception:
            # fallback: пропускаем frontmatter, чтобы не ломать сборку
            warnings.append("Не удалось сериализовать frontmatter, пропущено.")

    # title
    if title:
        prefix = "#" * max(1, 1 + max(0, int(heading_level_offset)))
        lines.append(f"{prefix} {title}")
        lines.append("")

    num_blocks = 0
    num_images = 0

    if mode == "manifest":
        if not manifest_path:
            raise ValueError("В режиме 'manifest' требуется manifest_path")
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(f"manifest_path не найден: {manifest_path}")
        # Поддержка JSON и JSONL (по строкам)
        blocks: List[Dict[str, Any]] = []
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content.startswith("["):
                blocks = json.loads(content)
            else:
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    blocks.append(json.loads(line))

        def replace_vars(text: str) -> str:
            if not variables or not isinstance(text, str):
                return text
            out = text
            for k, v in variables.items():
                out = out.replace(f"{{{{{k}}}}}", str(v))
            return out

        for b in blocks:
            btype = (b.get("type") or "paragraph").lower()
            if btype == "heading":
                level = int(b.get("level", 1)) + max(0, int(heading_level_offset))
                level = max(1, min(level, 6))
                text = replace_vars(b.get("text", "").strip())
                lines.append(f"{'#' * level} {text}")
                lines.append("")
                num_blocks += 1
                continue
            if btype == "paragraph":
                text = replace_vars(b.get("text", "").strip())
                if text:
                    lines.append(text)
                    lines.append("")
                    num_blocks += 1
                continue
            if btype == "image":
                images = b.get("images") or [b]
                for img in images:
                    src = img.get("path") or img.get("src")
                    if not src or not os.path.exists(src):
                        warnings.append(f"Изображение не найдено в манифесте: {src}")
                        continue
                    copied = _copy_asset_if_needed(src, assets_copy_dir)
                    link = _rewrite_link(copied, output_dir, link_strategy, base_url)
                    alt = replace_vars(img.get("alt", os.path.basename(src)))
                    title_attr = img.get("title")
                    if title_attr:
                        lines.append(f"![{alt}]({link} \"{title_attr}\")")
                    else:
                        lines.append(f"![{alt}]({link})")
                    lines.append("")
                    num_images += 1
                num_blocks += 1
                continue
            if btype == "divider":
                lines.append("\n---\n")
                num_blocks += 1
                continue
            # неизвестный тип — как параграф
            text = replace_vars(b.get("text", "").strip())
            if text:
                lines.append(text)
                lines.append("")
                num_blocks += 1

    else:  # discovery
        gathered: List[str] = []
        for pattern in (image_globs or []):
            gathered.extend(glob.glob(pattern, recursive=True))
        if not gathered:
            for pattern in (fallback_globs or []):
                gathered.extend(glob.glob(pattern, recursive=True))

        # фильтруем только существующие файлы
        gathered = [p for p in gathered if os.path.isfile(p)]
        gathered = list(dict.fromkeys(gathered))  # уникальные, сохраняя порядок

        if not gathered:
            warnings.append("Не найдено ни одного изображения по заданным шаблонам")

        sorted_paths = _sort_paths(gathered, order=order, order_regex=order_regex)

        # Подгружаем story.json если нужно для подписей/текста
        story_pages: List[Dict[str, Any]] = []
        if text_mode != "none" and story_json_path and os.path.exists(story_json_path):
            try:
                with open(story_json_path, "r", encoding="utf-8") as f:
                    sj = json.load(f)
                    story_pages = sj.get("pages", [])
            except Exception:
                warnings.append("Не удалось загрузить story_json_path, текст не будет вставлен.")

        for idx, img_path in enumerate(sorted_paths, start=1):
            copied = _copy_asset_if_needed(img_path, assets_copy_dir)
            link = _rewrite_link(copied, output_dir, link_strategy, base_url)
            alt = os.path.basename(img_path)
            lines.append(f"![{alt}]({link})")
            # добавляем текст, если требуется
            if text_mode != "none" and story_pages:
                # пытаемся найти страницу по индексу
                page_obj = None
                # сначала по полю page
                for p in story_pages:
                    if int(p.get("page", -1)) == idx:
                        page_obj = p
                        break
                # если не нашли — по порядку
                if page_obj is None and idx <= len(story_pages):
                    page_obj = story_pages[idx - 1]

                if page_obj:
                    title_txt = (page_obj.get("title") or "").strip()
                    body_txt = (page_obj.get("body") or "").strip()
                    if text_mode == "caption" and title_txt:
                        lines.append(f"**{title_txt}**")
                    if text_mode in ("caption", "body") and body_txt:
                        lines.append("")
                        lines.append(body_txt)
            lines.append("")
            num_images += 1
            num_blocks += 1

    # toc (простой): если включен и есть заголовок — добавим оглавление-заглушку
    # В большинстве рендеров TOC строится инструментами, поэтому оставим как опцию на будущее.

    content = "\n".join(lines).rstrip() + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    result = {
        "md_path": output_path,
        "num_blocks": num_blocks,
        "num_images": num_images,
        "warnings": warnings,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def md_to_pdf_tool(
    session_id: str,
    md_path: str,
    pdf_path: str | None = None,
    css_path: str | None = None,
    title: str | None = None,
    skip_if_up_to_date: bool = True,
    page_size: str | None = None,
    margins: str | None = None,
) -> str:
    """
    Конвертирует Markdown-файл в PDF с поддержкой относительных ссылок на изображения.

    Args:
        session_id: Идентификатор сессии для трассировки выполнения.
        md_path: Путь к исходному .md файлу.
        pdf_path: Путь для сохранения PDF. Если не указан — рядом с md, имя book.pdf.
        css_path: Опциональный путь к CSS стилям печати.
        title: Опциональный заголовок документа.
        skip_if_up_to_date: Пропускать, если PDF свежeе MD.
        page_size: Необязательный размер страницы (A4/Letter). Используется в CSS, если задан.
        margins: Необязательные поля страницы (например, "12mm"). Используется в CSS, если задано.

    Returns:
        JSON-строка: { "pdf_path": str, "warnings": List[str], "images_total": int }
    """
    import re
    from markdown2 import Markdown
    warnings: list[str] = []

    if not md_path or not os.path.exists(md_path):
        raise FileNotFoundError(f"Markdown файл не найден: {md_path}")

    md_path_abs = os.path.abspath(md_path)
    base_dir = os.path.dirname(md_path_abs)

    if not pdf_path:
        pdf_path = os.path.join(base_dir, "book.pdf")
    if not os.path.isabs(pdf_path):
        pdf_path = os.path.abspath(pdf_path)
    _ensure_parent_dir(pdf_path)

    # Идемпотентность
    try:
        if skip_if_up_to_date and os.path.exists(pdf_path):
            if os.path.getmtime(pdf_path) >= os.path.getmtime(md_path_abs):
                return json.dumps({
                    "pdf_path": pdf_path,
                    "warnings": ["Пропущено: PDF новее MD"],
                    "images_total": 0
                }, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # Подсчёт и проверка изображений из Markdown (локальные пути)
    images_total = 0
    try:
        with open(md_path_abs, "r", encoding="utf-8") as f:
            md_text = f.read()
        # Находим markdown-изображения ![alt](path "title")
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", md_text):
            src = m.group(1)
            if not re.match(r"^[a-zA-Z]+://", src):  # локальные/относительные
                img_path = os.path.normpath(os.path.join(base_dir, src))
                images_total += 1
                if not os.path.exists(img_path):
                    warnings.append(f"Изображение не найдено: {src}")
    except Exception:
        # Не критично, продолжаем
        pass

    # Конвертация Markdown → HTML
    md = Markdown(extras=["fenced-code-blocks", "tables", "strike", "target-blank-links"])
    html_body = md.convert(md_text)

    # Оборачивание в минимальный HTML-шаблон
    doc_title = title or os.path.basename(md_path_abs)
    html = (
        "<!DOCTYPE html>\n"
        "<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{doc_title}</title>\n"
        "</head>\n<body>\n" + html_body + "\n</body>\n</html>\n"
    )

    # Рендер HTML → PDF с указанием base_url, чтобы относительные пути к ресурсам разрешались
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "WeasyPrint не установлен. Добавьте 'weasyprint' в requirements.txt и установите зависимости"
        ) from e

    stylesheets = []
    if css_path:
        if not os.path.isabs(css_path):
            css_path = os.path.abspath(css_path)
        if os.path.exists(css_path):
            stylesheets.append(CSS(filename=css_path))
        else:
            warnings.append(f"CSS не найден: {css_path}")

    # Динамические параметры страницы можно задавать через дополнительный CSS
    dynamic_css_rules: list[str] = []
    if page_size:
        dynamic_css_rules.append(f"@page {{ size: {page_size}; }}")
    if margins:
        dynamic_css_rules.append(f"@page {{ margin: {margins}; }}")
    if dynamic_css_rules:
        stylesheets.append(CSS(string="\n".join(dynamic_css_rules)))

    HTML(string=html, base_url=base_dir).write_pdf(pdf_path, stylesheets=stylesheets)

    return json.dumps({
        "pdf_path": pdf_path,
        "warnings": warnings,
        "images_total": images_total,
    }, ensure_ascii=False, indent=2)

