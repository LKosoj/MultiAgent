import re
import os
import json
import requests
from bs4 import BeautifulSoup
import trafilatura
from requests.exceptions import RequestException
import subprocess
import tempfile
import time
import random
from typing import Union, List, Dict, Any, Optional, Tuple
from agent_command import model_code, model_big, model_summary, model_reranker, model_vision, model_mapping
from smolagents import logger, ChatMessage, MessageRole
import httpx
from openai import OpenAI

from concurrent.futures import ThreadPoolExecutor

# Импорты для красивого логирования в стиле smolagents
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.padding import Padding
    from rich import box
    import shutil
    
    _rich_available = True
    
    # Исправляем TERM=dumb для корректного определения размера терминала
    if os.environ.get('TERM') == 'dumb':
        os.environ['TERM'] = 'xterm-256color'
    
    # Создаем Console для автоматического определения ширины
    _console = Console(highlight=False)
except ImportError:
    _rich_available = False
    _console = None


def log_smolagents_panel(
    content: Union[str, Dict[str, Any]], 
    title: str = "Process Info",
    title_style: str = "bold blue",
    border_style: str = "blue",
    box_style = box.ROUNDED,
    fallback_logger = None
) -> None:
    """
    Универсальная функция для логирования в стиле smolagents с красивой рамкой.
    Автоматически растягивается на всю ширину терминала с отступами.
    
    Args:
        content: Содержимое для вывода (строка или словарь с данными)
        title: Заголовок панели
        title_style: Стиль заголовка (например, "bold blue")
        border_style: Стиль рамки (например, "blue", "yellow", "green", "red")
        box_style: Стиль коробки (box.ROUNDED, box.SQUARE, box.DOUBLE)
        fallback_logger: Logger для fallback
    """
    if fallback_logger is None:
        fallback_logger = logger
    
    if not _rich_available:
        # Fallback на обычное логирование
        if isinstance(content, dict):
            fallback_logger.info(f"📋 {title}")
            for key, value in content.items():
                fallback_logger.info(f"  {key}: {value}")
        else:
            fallback_logger.info(f"📋 {title}: {content}")
        return
    
    # Обработка содержимого
    if isinstance(content, dict):
        # Форматируем словарь как красивые строки
        content_lines = []
        for key, value in content.items():
            if isinstance(value, (list, tuple)):
                value_str = ", ".join(str(v) for v in value)
            else:
                value_str = str(value)
            
            content_lines.append(f"[bold]{key}:[/bold] {value_str}")
        formatted_content = "\n".join(content_lines)
    else:
        # Обрабатываем строку
        formatted_content = str(content)
    
    # Создаем панель с автоматической шириной (console.width - отступы)
    panel_width = _console.width - 4
    
    panel = Panel(
        formatted_content,
        title=f"[{title_style}]{title}[/{title_style}]",
        title_align="left",
        border_style=border_style,
        box=box_style,
        padding=(0, 1),
        width=panel_width
    )
    
    # Добавляем внешний паддинг
    padded_panel = Padding(panel, pad=(0, 2))
    
    # Выводим через rich console
    _console.print(padded_panel)


def log_smolagents_info(data: Dict[str, Any], title: str = "Info") -> None:
    """Удобная обертка для информационного логирования"""
    log_smolagents_panel(data, title=f"ℹ️ {title}", title_style="bold blue", border_style="blue")


def log_smolagents_success(data: Dict[str, Any], title: str = "Success") -> None:
    """Удобная обертка для успешного выполнения"""
    log_smolagents_panel(data, title=f"✅ {title}", title_style="bold green", border_style="green")


def log_smolagents_warning(data: Dict[str, Any], title: str = "Warning") -> None:
    """Удобная обертка для предупреждений"""
    log_smolagents_panel(data, title=f"⚠️ {title}", title_style="bold yellow", border_style="yellow")


def log_smolagents_error(data: Dict[str, Any], title: str = "Error") -> None:
    """Удобная обертка для ошибок"""
    log_smolagents_panel(data, title=f"❌ {title}", title_style="bold red", border_style="red")


def log_smolagents_process(data: Dict[str, Any], title: str = "Process") -> None:
    """Удобная обертка для логирования процессов"""
    log_smolagents_panel(data, title=f"🔄 {title}", title_style="bold cyan", border_style="cyan")


def get_clean_text_jina(url):
    jina_api_key = os.getenv('JINA_API_KEY', None)

    jina_url = "https://r.jina.ai/"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {jina_api_key}" if jina_api_key else None,
        "Content-Type": "application/json",
        "X-Base": "final",
        "X-Engine": "browser",
        "X-Timeout": "250"
    }
    data = {
        "url": url
    }

    response = requests.post(url=jina_url, headers=headers, json=data)
    response = response.json().get("data", None)
    if response:
        print(f"Страница {url} успешно обработана jina.")
        return response.get("content"), response.get("title")
    else:
        print(f"Страница {url} не обработана jina.")
        return "", ""

def get_clean_text(url):
    # Загрузка страницы с обработкой ошибок
    try:
        # Очистка url от лишних символов ')'
        if '(' in url:
            url = url.split('(')[1]
        url = url.strip(')').strip('(').strip('"').strip("'").strip()
        
        if not url.startswith('http'):
            return "", "Некорректный URL"

        # Добавление современного User-Agent
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0'
        }
        
        # Специальные заголовки для академических сайтов
        if 'sciencedirect.com' in url.lower():
            headers.update({
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.google.com/',
                'Sec-Fetch-Site': 'cross-site'
            })
        
        response = requests.get(url, headers=headers, timeout=20, stream=True)
        response.raise_for_status()
        
        # Проверка на PDF по URL или Content-Type
        content_type = response.headers.get('Content-Type', '').lower()
        if url.lower().endswith('.pdf') or 'application/pdf' in content_type:
            from io import BytesIO
            from pdfminer.high_level import extract_text
            try:
                pdf_content = BytesIO(response.content)
                text = extract_text(pdf_content)
                title = url.split('/')[-1] or "PDF-документ"
                return clean_extra_spaces(text), title
            except Exception as e:
                print(f"Ошибка при обработке PDF: {e}")
                return get_clean_text_jina(url)

    except requests.exceptions.Timeout:
        print(f"Ошибка загрузки страницы {url}: Время ожидания запроса истекло. Пожалуйста, попробуйте позже или проверьте URL.")
        return get_clean_text_jina(url)
        #return "", "Время ожидания запроса истекло. Пожалуйста, попробуйте позже или проверьте URL."
    except RequestException as e:
        print(f"Ошибка загрузки страницы {url}: {e}")
        return get_clean_text_jina(url)
        #return "", f"Ошибка при загрузке веб-страницы {url}: {str(e)}"
    except Exception as e:
        print(f"Произошла непредвиденная ошибка при загрузке страницы {url}: {e}")
        return get_clean_text_jina(url)
        #return "", f"Произошла непредвиденная ошибка при загрузке страницы {url}: {str(e)}"

    title = get_title(response.content)
    # Основная обработка с trafilatura
    try:
        # Извлечение чистого текста с сохранением структуры
        text = trafilatura.extract(
            response.content,
            include_formatting=True,
            include_links=True,
            include_tables=True,
            include_images=False,
            include_comments=False,
            output_format="markdown"  # Для сохранения структуры заголовков
        )
        
        if text:
            # Дополнительная очистка
            text = clean_extra_spaces(text)
            return text, title or "Без заголовка"
    except Exception as e:
        print(f"Ошибка обработки: {e}")

    # Fallback: если trafilatura не сработал
    return fallback_cleaner(response.text), title or "Без заголовка"

def clean_extra_spaces(markdown_content):
    # Очистка текста от лишних пробелов и переносов строк
    markdown_content = re.sub(r'\n{3,}', '\n\n', markdown_content)  # Заменяем множественные переносы строк
    markdown_content = re.sub(r'\s{2,}', ' ', markdown_content)     # Заменяем множественные пробелы
    markdown_content = re.sub(r'(\n\s*)+\n', '\n\n', markdown_content)  # Удаляем пустые строки с пробелами
    
    # Удаляем повторяющиеся спецсимволы Markdown
    markdown_content = re.sub(r'(\*{2,})', '**', markdown_content)  # Исправляем многократные звездочки
    markdown_content = re.sub(r'(_{2,})', '__', markdown_content)   # Исправляем многократные подчеркивания
    return markdown_content

def fallback_cleaner(html):
    # Резервный метод с BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    
    # Удаление ненужных элементов
    for tag in soup(['script', 'style', 'nav', 'footer', 
                'header', 'aside', 'form', 'iframe']):
        tag.decompose()
        
    # Извлечение текста с сохранением структуры
    for element in soup(['br', 'p', 'h1', 'h2', 'h3', 'ul', 'ol', 'li', 'table', 'tr', 'td', 'th']):
        element.append('\n')
        
    text = soup.get_text(separator='\n', strip=True)
    return clean_extra_spaces(text)

def get_title(html):
    try:
        # Способ 1: через trafilatura
        metadata = trafilatura.extract_metadata(html)
        if metadata and metadata.title:
            return metadata.title.strip()
        
        # Способ 2: резервный через BeautifulSoup
        soup = BeautifulSoup(html, 'lxml')
        if soup.title and soup.title.string:
            return soup.title.string.strip()
            
    except Exception as e:
        print(f"Ошибка: {e}")
    
    return "Без заголовка"

def validate_mermaid(diagram_text, filename=None):
    """
    Проверяет корректность синтаксиса Mermaid диаграммы с помощью mmdc CLI.
    
    Args:
        diagram_text (str): Текст диаграммы Mermaid
        filename (str, optional): Имя временного файла. Если не указано, создается временный файл.
    
    Returns:
        tuple: (bool, str) - (валидность, сообщение об ошибке или None)
    """
    # Создаем временный файл для диаграммы
    if filename is None:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mmd', delete=False) as f:
            f.write(diagram_text)
            temp_filename = f.name
    else:
        temp_filename = filename
        with open(temp_filename, 'w', encoding='utf-8') as f:
            f.write(diagram_text)
    
    try:
        # Создаем временный файл для вывода
        with tempfile.NamedTemporaryFile(suffix='.svg', delete=False) as output_f:
            output_filename = output_f.name
        
        # Запускаем mmdc для валидации
        result = subprocess.run(
            ['mmdc', '-i', temp_filename, '-o', output_filename],
            check=True,
            capture_output=True,
            text=True
        )
        
        # Если команда выполнилась успешно, диаграмма корректна
        return True, None
        
    except subprocess.CalledProcessError as e:
        # Если произошла ошибка, диаграмма некорректна
        error_message = e.stderr if e.stderr else str(e)
        return False, error_message
        
    except FileNotFoundError:
        # mmdc не установлен
        return False, "mmdc не установлен. Установите Mermaid CLI: npm install -g @mermaid-js/mermaid-cli"
        
    except Exception as e:
        # Другие ошибки
        return False, f"Неожиданная ошибка: {str(e)}"
        
    finally:
        # Очищаем временные файлы
        try:
            if filename is None and os.path.exists(temp_filename):
                os.unlink(temp_filename)
            if 'output_filename' in locals() and os.path.exists(output_filename):
                os.unlink(output_filename)
        except:
            pass


def validate_mermaid_batch(diagrams_list):
    """
    Проверяет список диаграмм Mermaid на корректность.
    
    Args:
        diagrams_list (list): Список строк с диаграммами Mermaid
    
    Returns:
        list: Список кортежей (index, valid, error_message)
    """
    results = []
    for i, diagram in enumerate(diagrams_list):
        valid, error = validate_mermaid(diagram)
        results.append((i, valid, error))
    return results


def _strip_leading_reasoning_blocks(text: str) -> str:
    """
    Убирает служебные reasoning-блоки, которые некоторые модели могут
    протекать в content перед JSON-ответом.
    """
    if not isinstance(text, str):
        return str(text).strip()
    raw = text.strip()
    if not raw:
        return raw

    cleaned = re.sub(
        r"^\s*(?:<think\b[^>]*>.*?</think>\s*)+",
        "",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    if cleaned != raw:
        return cleaned

    if re.match(r"^\s*<think\b", raw, flags=re.IGNORECASE):
        first_payload = re.search(r"```|[\{\[]", raw)
        if first_payload:
            return raw[first_payload.start():].strip()

    return raw


def extract_json_from_markdown(text):
    """
    Извлекает JSON из markdown блока кода.
    
    Args:
        text (str): Текст, который может содержать JSON в блоке ```json
    
    Returns:
        str: Чистый JSON или оригинальный текст если блок не найден
    """
    if not isinstance(text, str):
        return str(text).strip()
    raw = _strip_leading_reasoning_blocks(text)
    if not raw:
        return raw

    # 1) Prefer fenced ```json ... ``` (tolerant to \r\n and missing terminal newline)
    # Accept both:
    # ```json\n{...}\n```
    # ```json\r\n{...}\r\n```
    # ```json { ... } ```
    m = re.search(r"```json\s*(?:\r?\n)?(.*?)(?:\r?\n)?```", raw, flags=re.IGNORECASE | re.DOTALL)
    if m:
        inner = (m.group(1) or "").strip()
        if inner:
            return inner

    # 2) Any fenced block ``` ... ``` that looks like JSON
    m2 = re.search(r"```\s*(?:\w+)?\s*(?:\r?\n)?(.*?)(?:\r?\n)?```", raw, flags=re.IGNORECASE | re.DOTALL)
    if m2:
        inner = (m2.group(1) or "").strip()
        if inner.startswith("{") or inner.startswith("["):
            return inner

    # 3) If model returned a leading code fence but got truncated (no closing ```),
    # strip the first fence line and continue.
    if raw.startswith("```"):
        # drop the first line "```json" / "```"
        parts = raw.splitlines()
        if len(parts) >= 2:
            raw2 = "\n".join(parts[1:]).strip()
            # also drop trailing ``` if present
            if raw2.endswith("```"):
                raw2 = raw2[: -3].strip()
            if raw2.startswith("{") or raw2.startswith("["):
                return raw2
            raw = raw2

    # 4) Fallback: extract first JSON object/array by braces scan (best-effort).
    # This is safer than returning markdown when caller expects JSON.
    start_candidates = [(raw.find("{"), "{"), (raw.find("["), "[")]
    start_candidates = [(i, ch) for i, ch in start_candidates if i != -1]
    if start_candidates:
        start_idx, start_ch = min(start_candidates, key=lambda x: x[0])
        end_ch = "}" if start_ch == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        for j in range(start_idx, len(raw)):
            c = raw[j]
            if in_str:
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if c == '"':
                    in_str = False
                continue
            else:
                if c == '"':
                    in_str = True
                    continue
                if c == start_ch:
                    depth += 1
                elif c == end_ch:
                    depth -= 1
                    if depth == 0:
                        return raw[start_idx : j + 1].strip()

        # If we never closed (truncated), return from start to end (better than markdown fences)
        return raw[start_idx:].strip()

    # If nothing matched, return stripped original
    return raw


def parse_llm_json(resp: Any, fallback_list_key: str = None) -> Dict[str, Any]:
    """
    Универсальный парсер JSON-ответов от LLM.

    1. Снимает markdown-обёртку (```json ... ```) через extract_json_from_markdown.
    2. Парсит JSON.
    3. Если результат — list и задан fallback_list_key, оборачивает в {fallback_list_key: list}.
    4. Гарантирует возврат dict (при ошибке — пустой {}).

    Args:
        resp: Сырой ответ от LLM (str, dict, list, ...).
        fallback_list_key: Если LLM вернула массив вместо объекта — обернуть
            в dict с этим ключом (например "characters", "prompts").

    Returns:
        Dict[str, Any]: Распарсенный JSON-объект.
    """
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, list):
        return {fallback_list_key: resp} if fallback_list_key else {}

    try:
        clean = extract_json_from_markdown(resp) if isinstance(resp, str) else str(resp)
        parsed = json.loads(clean)
    except Exception:
        try:
            normalized = _normalize_json_newlines(clean)
            parsed = json.loads(normalized)
        except Exception:
            try:
                repaired = _repair_truncated_json(clean)
                parsed = json.loads(repaired)
            except Exception:
                try:
                    repaired_normalized = _repair_truncated_json(_normalize_json_newlines(clean))
                    parsed = json.loads(repaired_normalized)
                except Exception:
                    return {}

    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list) and fallback_list_key:
        return {fallback_list_key: parsed}
    return {}


def _normalize_json_newlines(json_like: str) -> str:
    """
    Best-effort normalization for JSON strings that accidentally contain raw newlines inside JSON string values.
    JSON forbids literal newlines inside string literals; they must be escaped as \\n.
    This function converts:
    - inside a JSON string literal: newline -> \\n
    - outside a JSON string literal: newline -> whitespace
    """
    if not isinstance(json_like, str) or not json_like:
        return json_like
    out = []
    in_str = False
    escape = False
    for ch in json_like:
        if in_str:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == '\\':
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            if ch == '\n':
                out.append('\\n')
                continue
            if ch == '\r':
                continue
            out.append(ch)
        else:
            if ch == '"':
                out.append(ch)
                in_str = True
                continue
            if ch in ('\n', '\r'):
                out.append(' ')
                continue
            out.append(ch)
    return ''.join(out)


def _repair_truncated_json(json_str: str) -> str:
    """
    Пытается починить обрезанный JSON, закрывая незакрытые скобки/кавычки.
    Полезно, когда LLM обрезает ответ по лимиту max_tokens.
    """
    if not isinstance(json_str, str) or not json_str.strip():
        return json_str

    s = json_str.rstrip()

    # Убираем trailing запятую (частый артефакт обрезки)
    while s.endswith(','):
        s = s[:-1].rstrip()

    in_str = False
    escape = False
    stack = []

    for ch in s:
        if in_str:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    if in_str:
        s += '"'

    # Убираем trailing запятую ещё раз (после закрытия строки)
    while s.rstrip().endswith(','):
        s = s.rstrip()[:-1].rstrip()

    for bracket in reversed(stack):
        if bracket == '{':
            s += '}'
        elif bracket == '[':
            s += ']'

    return s


def extract_mermaid_from_text(text):
    """
    Извлекает все блоки Mermaid диаграмм из текста.
    
    Args:
        text (str): Текст, содержащий диаграммы Mermaid
    
    Returns:
        list: Список найденных диаграмм Mermaid
    """
    # Паттерн для поиска блоков кода Mermaid
    pattern = r'```mermaid\s*\n(.*?)\n```'
    matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
    return [match.strip() for match in matches]


def validate_text_mermaid_diagrams(text):
    """
    Находит и проверяет все диаграммы Mermaid в тексте.
    
    Args:
        text (str): Текст, содержащий диаграммы Mermaid
    
    Returns:
        dict: Словарь с результатами проверки
    """
    diagrams = extract_mermaid_from_text(text)
    if not diagrams:
        return {
            'total_diagrams': 0,
            'valid_diagrams': 0,
            'invalid_diagrams': 0,
            'results': []
        }
    
    results = validate_mermaid_batch(diagrams)
    valid_count = sum(1 for _, valid, _ in results if valid)
    
    return {
        'total_diagrams': len(diagrams),
        'valid_diagrams': valid_count,
        'invalid_diagrams': len(diagrams) - valid_count,
        'results': results,
        'diagrams': diagrams
    }

def call_openai_api(prompt: str, system_prompt: str = None, max_tokens: int = 1000, model = None, temperature: float = 0.3, response_format = None, max_retries: int = 3, image_url: str = None) -> str:
    """
    Вызов API OpenAI для генерации контента с механизмом повторных попыток.
    Поддерживает как текстовые запросы, так и анализ изображений.
    Автоматически проверяет валидность JSON ответов и повторяет запросы при ошибках.
    
    Args:
        prompt: Запрос пользователя
        system_prompt: Системный промпт
        max_tokens: Максимальное количество токенов
        model: Модель для использования
        temperature: Температура генерации
        response_format: Формат ответа (если {"type": "json_object"}, будет проверяться валидность JSON)
        max_retries: Максимальное количество повторных попыток (по умолчанию 3)
        image_url: URL изображения для анализа (data URI или HTTP URL)
    
    Returns:
        str: Ответ от API или пустая строка в случае ошибки
    """
    if not system_prompt:
        system_prompt = "Ты агент, помогающий пользователю решить его задачу. Очень важно точно ответить на вопрос пользователя!"
    
    if not model:
        model = model_code
        
    # Создаем сообщения в зависимости от наличия изображения
    if image_url:
        # Для vision API используем специальный формат
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(
                role=MessageRole.USER, 
                content=[
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    }
                ]
            )
        ]
    else:
        # Обычные текстовые сообщения
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=prompt)        
        ]
    
    last_exception = None
    is_json_request = response_format and isinstance(response_format, dict) and response_format.get("type") == "json_object"
    
    if is_json_request:
        logger.info(f"🔍 JSON запрос: будет проверяться валидность ответа")
    
    for attempt in range(max_retries + 1):
        try:
            response = model(messages, max_tokens=max_tokens, temperature=temperature, response_format=response_format)
            
            # Проверяем, что ответ не None
            if response is None:
                if attempt < max_retries:
                    logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: Модель вернула None, повторяем запрос...")
                    time.sleep(0.5)
                    continue
                else:
                    raise Exception("Модель вернула None вместо корректного ответа")
            
            # Проверяем структуру ответа
            if hasattr(response, 'choices'):
                if response.choices is None:
                    if attempt < max_retries:
                        logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: response.choices = None, повторяем запрос...")
                        logger.debug(f"Тип ответа: {type(response)}, содержимое: {response}")
                        time.sleep(0.5)
                        continue
                    else:
                        raise Exception("response.choices = None (некорректная структура ответа)")
                elif len(response.choices) == 0:
                    if attempt < max_retries:
                        logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: response.choices пустой, повторяем запрос...")
                        time.sleep(0.5)
                        continue
                    else:
                        raise Exception("response.choices пустой (нет вариантов ответа)")
            
            # Извлекаем текст из ответа
            response_text = ""
            if hasattr(response, 'content') and isinstance(response.content, str):
                response_text = response.content
            elif hasattr(response, 'choices') and response.choices is not None and len(response.choices) > 0 and hasattr(response.choices[0], 'message'):
                response_text = response.choices[0].message.content
            elif isinstance(response, dict) and 'choices' in response and response['choices'] is not None and len(response['choices']) > 0:
                response_text = response["choices"][0]["message"]["content"]
            else:
                response_text = str(response)
            
            # Проверяем, что ответ не пустой
            if not response_text or response_text.strip() == "":
                if attempt < max_retries:
                    logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: Получен пустой ответ, повторяем запрос...")
                    time.sleep(0.5)
                    continue
                else:
                    raise Exception("Получен пустой ответ от модели")
            
            # Проверяем finish_reason — если "length", ответ обрезан по лимиту токенов
            finish_reason = None
            try:
                if hasattr(response, 'choices') and response.choices and len(response.choices) > 0:
                    finish_reason = getattr(response.choices[0], 'finish_reason', None)
                elif isinstance(response, dict) and 'choices' in response and response['choices']:
                    finish_reason = response['choices'][0].get('finish_reason')
            except Exception:
                pass
            
            truncated_by_length = finish_reason == "length"
            if truncated_by_length:
                logger.warning(
                    f"⚠️ finish_reason='length' (попытка {attempt + 1}/{max_retries + 1}): "
                    f"ответ обрезан по лимиту max_tokens={max_tokens}. "
                    f"Длина ответа: {len(response_text)} символов"
                )
            
            # Если запрашивается JSON, проверяем валидность
            if is_json_request:
                try:
                    # Сначала пытаемся извлечь JSON из markdown, если есть
                    clean_json = extract_json_from_markdown(response_text)
                    
                    # Проверяем, что JSON валидный
                    json.loads(clean_json)
                    logger.info(f"✅ Получен валидный JSON (попытка {attempt + 1}/{max_retries + 1})")
                    return response_text
                    
                except json.JSONDecodeError as json_err:
                    # Диагностика: показываем фрагмент невалидного JSON/ответа, чтобы понять причину поломки.
                    # Важно: логируем только preview (с заменой переносов), чтобы не раздувать логи.
                    try:
                        def _preview(s: str, limit: int = 1200) -> str:
                            if s is None:
                                return ""
                            s = str(s)
                            s = s.replace("\r", "\\r").replace("\n", "\\n")
                            if len(s) <= limit:
                                return s
                            head = s[: int(limit * 0.7)]
                            tail = s[-int(limit * 0.3):]
                            return head + "...<truncated>..." + tail

                        logger.warning(
                            f"🧩 JSON DEBUG (attempt {attempt + 1}/{max_retries + 1}): {json_err} | "
                            f"response_text_preview='{_preview(response_text)}' | "
                            f"clean_json_preview='{_preview(clean_json)}'"
                        )
                    except Exception:
                        pass
                    # Попытка "починить" типичную ошибку: raw newlines внутри JSON string literals
                    try:
                        normalized_json = _normalize_json_newlines(clean_json)
                        json.loads(normalized_json)
                        logger.info(
                            f"✅ JSON восстановлен нормализацией переносов строк (попытка {attempt + 1}/{max_retries + 1})"
                        )
                        return normalized_json
                    except Exception:
                        pass
                    # Попытка починить обрезанный JSON (закрыть незакрытые скобки/кавычки).
                    # Применяем ТОЛЬКО если finish_reason='length' (подтверждённая обрезка по лимиту токенов).
                    # Если finish_reason != 'length', ремонт опасен: он создаст синтаксически валидный,
                    # но семантически неполный JSON с потерей данных — лучше сделать retry.
                    if truncated_by_length:
                        try:
                            repaired_json = _repair_truncated_json(clean_json)
                            json.loads(repaired_json)
                            logger.info(
                                f"✅ JSON восстановлен ремонтом обрезанного ответа "
                                f"(попытка {attempt + 1}/{max_retries + 1}, "
                                f"finish_reason={finish_reason}, truncated_by_length={truncated_by_length})"
                            )
                            return repaired_json
                        except Exception:
                            pass
                        # Комбинация: нормализация + ремонт (только при подтверждённой обрезке)
                        try:
                            repaired_normalized = _repair_truncated_json(_normalize_json_newlines(clean_json))
                            json.loads(repaired_normalized)
                            logger.info(
                                f"✅ JSON восстановлен нормализацией + ремонтом "
                                f"(попытка {attempt + 1}/{max_retries + 1})"
                            )
                            return repaired_normalized
                        except Exception:
                            pass
                    else:
                        logger.warning(
                            f"⚠️ JSON невалиден, но finish_reason={finish_reason} (не 'length') — "
                            f"ремонт обрезанного ответа НЕ применяется, будет retry. "
                            f"(попытка {attempt + 1}/{max_retries + 1})"
                        )
                    if attempt < max_retries:
                        if truncated_by_length:
                            logger.warning(
                                f"🔄 Попытка {attempt + 1}/{max_retries + 1}: JSON обрезан (finish_reason=length, "
                                f"max_tokens={max_tokens}), ремонт не удался, повторяем запрос. Ошибка: {json_err}"
                            )
                        else:
                            logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: JSON невалидный, повторяем запрос. Ошибка: {json_err}")
                        time.sleep(0.5)
                        continue
                    else:
                        raise Exception(f"Получен невалидный JSON после {max_retries + 1} попыток: {json_err}")
                except Exception as extract_err:
                    if attempt < max_retries:
                        logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries + 1}: Ошибка извлечения JSON, повторяем запрос. Ошибка: {extract_err}")
                        # Небольшая задержка перед повторной попыткой
                        time.sleep(0.5)
                        continue
                    else:
                        raise Exception(f"Ошибка извлечения JSON: {extract_err}")
            else:
                # Для не-JSON запросов возвращаем как есть
                return response_text
                
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()
            
            # Логируем попытку
            if attempt < max_retries:
                logger.warning(f"Попытка {attempt + 1}/{max_retries + 1} неудачна: {e}")
                
                # Определяем, стоит ли повторять попытку
                if any(keyword in error_str for keyword in [
                    'model not found', '404', 'bad request', '400',
                    'timeout', 'connection', 'network', 'server error', '500', '502', '503'
                ]):
                    # Экспоненциальная задержка с джиттером
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    logger.info(f"Ожидание {delay:.2f} секунд перед повторной попыткой...")
                    time.sleep(delay)
                    continue
                else:
                    # Для других типов ошибок не повторяем
                    logger.error(f"Критическая ошибка, повторные попытки не помогут: {e}")
                    break
            else:
                logger.error(f"Все попытки исчерпаны. Последняя ошибка: {e}")
    
    logger.error(f"Ошибка вызова OpenAI API после {max_retries + 1} попыток: {last_exception}")
    return ""

def call_openai_api_streaming(
    prompt: str,
    system_prompt: str = None,
    max_tokens: int = 1000,
    model = None,
    model_key: str = None,
    temperature: float = 0.3,
    response_format = None,
    max_retries: int = 3,
    image_url: str = None,
) -> str:
    """
    Вызов API OpenAI с принудительным стримингом. Возвращает собранный текст.
    """
    if not system_prompt:
        system_prompt = "Ты агент, помогающий пользователю решить его задачу. Очень важно точно ответить на вопрос пользователя!"

    if not model:
        if model_key:
            model = model_mapping.get(model_key)
        if not model:
            model = model_code

    if image_url:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(
                role=MessageRole.USER,
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            ),
        ]
    else:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.USER, content=prompt),
        ]

    last_exception = None

    def _extract_stream_text(stream) -> str:
        chunks = []
        for chunk in stream:
            content = None
            if hasattr(chunk, "choices"):
                choice = chunk.choices[0]
                if hasattr(choice, "delta"):
                    delta = choice.delta
                    content = getattr(delta, "content", None)
                    if content is None and isinstance(delta, dict):
                        content = delta.get("content")
                elif hasattr(choice, "message"):
                    content = getattr(choice.message, "content", None)
            elif isinstance(chunk, dict) and "choices" in chunk:
                choice = chunk["choices"][0]
                if "delta" in choice:
                    content = choice["delta"].get("content")
                elif "message" in choice:
                    content = choice["message"].get("content")
            elif hasattr(chunk, "content"):
                content = chunk.content
            if content:
                chunks.append(content)
        return "".join(chunks).strip()

    for attempt in range(max_retries + 1):
        try:
            response = model(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                stream=True,
            )

            if response is None:
                if attempt < max_retries:
                    time.sleep(0.5)
                    continue
                raise Exception("Модель вернула None")

            if hasattr(response, "__iter__") and not hasattr(response, "choices"):
                text = _extract_stream_text(response)
                if text:
                    return text

            if hasattr(response, "choices") and response.choices:
                content = response.choices[0].message.content
                if content:
                    return content.strip()

            text = str(response).strip()
            if text:
                return text
        except Exception as exc:
            last_exception = exc
            if attempt < max_retries:
                time.sleep(0.5)
                continue
            break

    logger.error(f"Ошибка вызова OpenAI API streaming после {max_retries + 1} попыток: {last_exception}")
    return ""

def get_text_topic_relevance_score(
    text: str,
    topic: str,
    max_text_length: int = 10000,
    model: Union[str, None] = None,
) -> float:
    """
    Возвращает степень соответствия текста заданной теме в диапазоне [0.0, 1.0].

    Args:
        text (str): Текст для оценки.
        topic (str): Тема, относительно которой оценивается релевантность.
        max_text_length (int): Максимальная длина текста, учитываемая при оценке (в символах).
        model: ID модели-реранкера (строка). Если не задано — берется из env `RERANK_MODEL`
               (fallback: `Qwen/Qwen3-Reranker-0.6B`).

    Returns:
        float: Число от 0.0 до 1.0, где 0 — не соответствует, 1 — полностью соответствует.
    """
    if not isinstance(text, str):
        text = str(text)
    if not isinstance(topic, str):
        topic = str(topic)

    text_to_score = text.strip()
    if max_text_length and len(text_to_score) > max_text_length:
        # Триммируем очень длинные тексты, чтобы не переполнить контекст
        half = max_text_length // 2
        text_to_score = text_to_score[:half] + "\n...\n" + text_to_score[-half:]

    topic_to_score = topic.strip()
    if not topic_to_score or not text_to_score:
        return 0.0

    # Конфигурация реранкера
    api_key = os.getenv("CLOUD_API_KEY") or ""
    base_url = os.getenv("CLOUD_API_BASE") or "https://foundation-models.api.cloud.ru"
    rerank_model = model or os.getenv("RERANK_MODEL") or "Qwen/Qwen3-Reranker-0.6B"

    if not api_key:
        logger.warning("get_text_topic_relevance_score: не задан API ключ (CLOUD_API_KEY).")
        return 0.0

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)

        # Используем низкоуровневый endpoint /score
        response = client.post(
            path="/score",
            cast_to=httpx.Response,
            body={
                "model": rerank_model,
                "encoding_format": "float",
                "text_1": topic_to_score,
                "text_2": [text_to_score],
            },
        )

        payload = response.json() if isinstance(response, httpx.Response) else {}
        data = payload.get("data") or []
        if not data:
            return 0.0

        # Ожидаем единственный результат (index=0)
        try:
            score_val = float(data[0].get("score", 0.0))
        except Exception:
            score_val = 0.0

        # Приводим к [0,1] (если модель вернула уже нормированный скор — просто пройдет как есть)
        if score_val < 0.0:
            score_val = 0.0
        if score_val > 1.0:
            score_val = 1.0
        return score_val

    except Exception as e:
        logger.warning(f"get_text_topic_relevance_score: ошибка вызова reranker /score: {e}")
        return 0.0

def translate_prompts_in_items(
    items: Union[List[Dict[str, Any]], Dict[str, Any]], 
    target_language: str,
    max_workers: int = 5,
    locked_fragments_by_field: Optional[Dict[str, List[str]]] = None,
    fields_to_translate: Optional[List[str]] = None,
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Переводит только текстовые промпты в items, оставляя JSON структуру нетронутой.
    Параллелит на уровне item для максимальной скорости.
    
    Args:
        items: Список items или один item (из prompt_engineer, shots, video)
        target_language: Целевой язык ('ru', 'en', 'es', etc.)
        max_workers: Количество потоков для параллельного перевода
        locked_fragments_by_field: Фрагменты, которые нельзя переводить, по именам полей
        fields_to_translate: Какие поля переводить. По умолчанию: english_prompt, video_prompt, negative_prompt
    
    Returns:
        items с переведенными промптами
    """
    
#    if target_language == 'en':
#        return items  # Уже на английском
    
    # Нормализация входных данных
    items_list = items if isinstance(items, list) else [items]
    is_single = not isinstance(items, list)
    locked_fragments_by_field = locked_fragments_by_field or {}
    allowed_fields = set(fields_to_translate or ["english_prompt", "video_prompt", "negative_prompt"])
    caret_locked_fragment_re = re.compile(r"\^[^^\n]{1,120}\^")
    residual_term_replacements = {
        "ru": (
            (re.compile(r"\bcomic\s+sans\b", re.IGNORECASE), "комиксовый шрифт"),
            (re.compile(r"\bcgi(?:\s*[- ]\s*эстетика)?\b", re.IGNORECASE), "эстетика компьютерной графики"),
            (re.compile(r"\bfacing[_ -]?camera\b", re.IGNORECASE), "взгляд в камеру"),
        ),
    }

    def _normalize_residual_terms(text: str) -> str:
        normalized = text
        for pattern, replacement in residual_term_replacements.get(target_language, ()):
            normalized = pattern.sub(replacement, normalized)
        return normalized

    def _mask_locked_fragments(
        text: str,
        field_name: str,
        fragments: List[str],
    ) -> Tuple[str, Dict[str, str]]:
        if not text or not fragments:
            auto_fragments = [match.group(0) for match in caret_locked_fragment_re.finditer(text)] if text else []
            if not auto_fragments:
                return text, {}
            fragments = auto_fragments
        else:
            auto_fragments = [match.group(0) for match in caret_locked_fragment_re.finditer(text)]
            fragments = list(fragments) + auto_fragments

        masked_text = text
        restore_map: Dict[str, str] = {}
        unique_fragments = []
        seen = set()
        for fragment in fragments:
            cleaned = " ".join((fragment or "").split()).strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique_fragments.append(cleaned)

        for idx, fragment in enumerate(sorted(unique_fragments, key=len, reverse=True)):
            token = f"__LOCK_{field_name.upper()}_{idx}__"
            if fragment in masked_text:
                masked_text = masked_text.replace(fragment, token)
                restore_map[token] = fragment

        return masked_text, restore_map

    def _restore_locked_fragments(text: str, restore_map: Dict[str, str]) -> str:
        restored = text
        for token, fragment in restore_map.items():
            restored = restored.replace(token, fragment)
        return restored
    
    def _translate_item_prompts(item: Dict[str, Any]) -> Dict[str, Any]:
        """Переводит промпты в одном item"""
        english_prompt = item.get('english_prompt', '').strip()
        video_prompt = item.get('video_prompt', '').strip()
        negative_prompt = item.get('negative_prompt', '').strip()
        
        # Собираем только непустые строки для перевода
        texts_to_translate = []
        field_mapping = []  # Отслеживаем поле и карту восстановления фрагментов
        
        if english_prompt and "english_prompt" in allowed_fields:
            masked_text, restore_map = _mask_locked_fragments(
                english_prompt,
                'english_prompt',
                locked_fragments_by_field.get('english_prompt', []),
            )
            texts_to_translate.append(masked_text)
            field_mapping.append(('english_prompt', restore_map))
            
        if video_prompt and "video_prompt" in allowed_fields:
            masked_text, restore_map = _mask_locked_fragments(
                video_prompt,
                'video_prompt',
                locked_fragments_by_field.get('video_prompt', []),
            )
            texts_to_translate.append(masked_text)
            field_mapping.append(('video_prompt', restore_map))
            
        if negative_prompt and "negative_prompt" in allowed_fields:
            masked_text, restore_map = _mask_locked_fragments(
                negative_prompt,
                'negative_prompt',
                locked_fragments_by_field.get('negative_prompt', []),
            )
            texts_to_translate.append(masked_text)
            field_mapping.append(('negative_prompt', restore_map))
            
        if not texts_to_translate:
            return item.copy()  # Нечего переводить
        
        # Один LLM-вызов для всех промптов item'а
        translated_texts = _translate_texts_batch(texts_to_translate, target_language)
        
        # Обновляем item
        result_item = item.copy()
        
        for idx, field_info in enumerate(field_mapping):
            if idx < len(translated_texts):
                field_name, restore_map = field_info
                new_text = translated_texts[idx]
                if restore_map:
                    new_text = _restore_locked_fragments(new_text, restore_map)
                new_text = _normalize_residual_terms(new_text)
                result_item[field_name] = new_text
        
        return result_item
    
    # Параллельная обработка items
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            translated_items = list(executor.map(_translate_item_prompts, items_list))
        
        return translated_items[0] if is_single else translated_items
    except Exception as e:
        raise RuntimeError(f"Ошибка параллельной обработки перевода: {e}") from e


def _translate_texts_batch(texts: List[str], target_language: str) -> List[str]:
    """Переводит список текстов одним вызовом LLM"""
    import json
    
    # Определяем полное название языка
    language_map = {
        'ru': 'русский язык',
        'en': 'английский язык',
        'es': 'испанский язык',
        'fr': 'французский язык',
        'de': 'немецкий язык'
    }
    full_language = language_map.get(target_language, target_language)
    
    system_prompt = f"""
Ты эксперт по переводу. Твоя единственная задача — переводить текст с входного языка на {full_language}, не меняя его смысл.
    Переведи ВСЕ тексты на {full_language}.
    Перед ответом тщательно проверь, что все слова переведены на {full_language} и нет символов из другого языка! Это очень важно!
    Служебные токены вида __LOCK_SOMETHING__ нельзя переводить, изменять, удалять, разбивать или обрамлять. Верни их в ответе посимвольно без изменений.
    Названия шрифтов, ярлыки стилей и служебные термины внутри промптов тоже переводи на {full_language} описательно; не оставляй их на английском, если они не являются lock-token, путем, именем файла или заблокированным фрагментом.

ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА JSON (используй ТОЧНО это поле):
{{"texts": ["переведенный_текст1", "переведенный_текст2", ...]}}

НЕ используй другие названия полей! ТОЛЬКО "texts"!
"""
    
    user_prompt = json.dumps({"texts": texts}, ensure_ascii=False)
    
    try:
        response = call_openai_api(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model_code,  # Используем быструю модель для перевода
            max_tokens=4000,
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        
        # Извлекаем JSON из ответа
        clean_json = extract_json_from_markdown(response)
        result = json.loads(clean_json)
        
        # LLM может вернуть либо "texts", либо "translated_texts" - проверяем оба варианта
        translated_texts = result.get("texts") or result.get("translated_texts")
        
        if result.get("translated_texts") and not result.get("texts"):
            logger.warning(f"⚠️ LLM вернул 'translated_texts' вместо 'texts' - исправляем автоматически")
        
        # Проверяем, что количество совпадает
        if not isinstance(translated_texts, list):
            raise ValueError(f"TRANSLATION FORMAT ERROR: ожидался список переводов, получено {type(translated_texts).__name__}")

        if len(translated_texts) != len(texts):
            raise ValueError(
                f"TRANSLATION MISMATCH: ожидалось {len(texts)} текстов, получено {len(translated_texts)}"
            )
            
        return translated_texts
        
    except Exception as e:
        raise RuntimeError(f"Ошибка в _translate_texts_batch: {e}") from e


def needs_translation_to_english(item: Dict[str, Any]) -> bool:
    """Определяет, нужен ли перевод на английский по содержимому промпта"""
    english_prompt = item.get('english_prompt', '')
    negative_prompt = item.get('negative_prompt', '')
    
    # Простая эвристика: если есть кириллица — нужен перевод на английский
    joined = f"{english_prompt}\n{negative_prompt}"
    has_cyrillic = any('\u0400' <= char <= '\u04FF' for char in joined)
    return has_cyrillic
