"""Подготовка и пересказ книги с необязательной проверкой плана."""

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from .files import build_files, read_chapters, write_json


RULES_PATH = Path(__file__).resolve().parents[2] / 'config/book_summary/prompts.yaml'


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+(?:[-’']\w+)*\b", text, re.UNICODE))


def check_text(source: str, text: str) -> dict:
    source_words = word_count(source)
    words = word_count(text)
    # Цитаты и блоки кода не интерпретируются моделью при этой проверке.
    issues = []
    for code in re.findall(r'```[^\n]*\n(.*?)```', text, re.DOTALL):
        if code.strip() not in source:
            issues.append('Пример кода отсутствует в исходнике или изменён')
    for quote in re.findall(r'«([^»]+)»', text):
        if quote not in source:
            issues.append('Прямая цитата отсутствует в исходнике: ' + quote)
    return {'source_words': source_words, 'words': words,
            'percent': 100 * words / source_words if source_words else 0,
            'size_ok': source_words > 0 and source_words * .2 <= words <= source_words * .4,
            'issues': issues}


def _ask(stage: str, payload: dict, rules: dict) -> dict:
    from agent_command import model_code, model_ultimate
    from utils import call_openai_api, extract_json_from_markdown

    result = call_openai_api(
        prompt=json.dumps(payload, ensure_ascii=False),
        system_prompt=rules['common'] + '\n' + rules[stage],
        model=model_ultimate if stage in {'write', 'clarity', 'book'} else model_code,
        max_tokens=32768, temperature=.2, response_format={'type': 'json_object'},
    )
    data = result if isinstance(result, dict) else json.loads(extract_json_from_markdown(result or ''))
    if not isinstance(data, dict):
        raise ValueError(f'{stage}: нужен JSON-объект')
    if stage in {'write', 'size', 'review', 'flow', 'clarity'}:
        if not isinstance(data.get('text'), str) or not data['text'].strip():
            raise ValueError(f'{stage}: отсутствует текст')
    if stage == 'review' and data.get('flow') not in {'low', 'good'}:
        raise ValueError('review: не указана связность low/good')
    if stage == 'glossary' and not isinstance(data.get('terms'), list):
        raise ValueError('glossary: отсутствует список terms')
    if stage == 'book':
        if not isinstance(data.get('issues'), list) or any(not isinstance(data.get(k), str) for k in ('introduction', 'conclusion')):
            raise ValueError('book: неполный результат общей проверки')
    return data


def _cached(path: Path, stage: str, payload: dict, rules: dict) -> dict:
    signature = _digest({'stage': stage, 'payload': payload, 'rules': rules})
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('signature') == signature:
            return saved['result']
    result = _ask(stage, payload, rules)
    write_json(path, {'signature': signature, 'result': result})
    return result


def book_prepare_tool(session_id: str, source_path: str, output_dir: str) -> str:
    """Извлекает главы PDF/EPUB и сохраняет план со словарём для проверки человеком.

    Args:
        session_id: Идентификатор сессии.
        source_path: Путь к книге PDF или EPUB.
        output_dir: Каталог материалов пересказа.

    Returns:
        JSON с путём к плану и каталогу материалов.
    """
    source = Path(source_path).resolve()
    base = Path(output_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    plan_path = base / 'plan.json'
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        if plan['source_sha256'] != fingerprint:
            raise ValueError('Каталог уже содержит другую книгу; укажите другой output_dir')
        return json.dumps({'plan_path': str(plan_path), 'output_dir': str(base)})
    rules = yaml.safe_load(RULES_PATH.read_text(encoding='utf-8'))
    chapters, terms = [], []
    for i, chapter in enumerate(read_chapters(source), 1):
        chapter_path = base / 'source' / f'{i:04}.txt'
        chapter_path.parent.mkdir(exist_ok=True)
        chapter_path.write_text(chapter['text'], encoding='utf-8')
        result = _cached(base / 'prep' / f'{i:04}-glossary.json', 'glossary',
                         {'source': chapter['text'], 'glossary': terms}, rules)
        for term in result['terms']:
            if not isinstance(term, dict) or any(not isinstance(term.get(k), str) or not term[k].strip() for k in ('source', 'ru', 'meaning')):
                raise ValueError('glossary: термин должен содержать source, ru, meaning')
            if not any(t['source'] == term['source'] for t in terms):
                terms.append(term)
        chapters.append({'id': f'{i:04}', 'title': chapter['title'],
                         'source_path': str(chapter_path.relative_to(base))})
    write_json(plan_path, {'title': source.stem, 'source_path': str(source),
                          'source_sha256': fingerprint, 'chapters': chapters, 'glossary': terms})
    return json.dumps({'plan_path': str(plan_path), 'output_dir': str(base)})


def _chapter(base: Path, chapter: dict, glossary: list, rules: dict) -> dict:
    source = (base / chapter['source_path']).read_text(encoding='utf-8')
    if not word_count(source):
        raise ValueError(f'Пустая глава: {chapter["id"]}')
    folder = base / 'chapters' / chapter['id']
    payload = {'source': source, 'title': chapter['title'], 'glossary': glossary,
               'min_words': math.ceil(word_count(source) * .2),
               'max_words': math.floor(word_count(source) * .4)}
    result = _cached(folder / 'write.json', 'write', payload, rules)
    payload['text'] = result['text']
    if not check_text(source, payload['text'])['size_ok']:
        result = _cached(folder / 'size.json', 'size', payload, rules)
        payload['text'] = result['text']
        if not check_text(source, payload['text'])['size_ok']:
            (folder / 'size.json').unlink()
            raise ValueError(f'{chapter["id"]}: Size не достиг объёма 20–40%')
    result = _cached(folder / 'review.json', 'review', payload, rules)
    payload['text'] = result['text']
    if result['flow'] == 'low':
        result = _cached(folder / 'flow.json', 'flow', payload, rules)
        payload['text'] = result['text']
    result = _cached(folder / 'clarity.json', 'clarity', payload, rules)
    text = result['text']
    checks = check_text(source, text)
    write_json(folder / 'checks.json', checks)
    if not checks['size_ok'] or checks['issues']:
        (folder / 'clarity.json').unlink()
        raise ValueError(f'{chapter["id"]}: итоговые проверки не пройдены: {checks}')
    return {'id': chapter['id'], 'title': chapter['title'], 'text': text}


def _load_plan(base: Path) -> dict:
    plan = json.loads((base / 'plan.json').read_text(encoding='utf-8'))
    chapters = plan['chapters']
    if not chapters or len({c['id'] for c in chapters}) != len(chapters):
        raise ValueError('План должен содержать главы с уникальными id')
    for chapter in chapters:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', chapter['id']):
            raise ValueError('id главы должен содержать только буквы, цифры, _ и -')
    return plan


def _inputs_digest(base: Path, plan: dict, rules: dict) -> str:
    return _digest({'plan': plan, 'rules': rules, 'sources': [
        (base / c['source_path']).read_text(encoding='utf-8') for c in plan['chapters']]})


def book_summary_tool(session_id: str, output_dir: str, plan_approved: bool = False, review_plan: bool = False) -> str:
    """Пишет и проверяет главы, при необходимости ожидая проверки плана.

    Args:
        session_id: Идентификатор сессии.
        output_dir: Каталог с plan.json и исходными главами.
        plan_approved: Пользователь проверил и одобрил текущий план и словарь.
        review_plan: Требовать проверку плана пользователем перед написанием.

    Returns:
        JSON с путём к пересказу и каталогу материалов.
    """
    base = Path(output_dir).resolve()
    plan = _load_plan(base)
    if review_plan and plan_approved is not True:
        return json.dumps({
            'review_required': True, 'ready_for_build': False,
            'plan_path': str(base / 'plan.json'), 'book_path': '', 'output_dir': str(base),
            'message': 'Проверьте plan.json и словарь; повторите book_summary с plan_approved: true',
        }, ensure_ascii=False)
    rules = yaml.safe_load(RULES_PATH.read_text(encoding='utf-8'))
    signature = _inputs_digest(base, plan, rules)
    with ThreadPoolExecutor(max_workers=3) as pool:
        chapters = list(pool.map(lambda c: _chapter(base, c, plan['glossary'], rules), plan['chapters']))
    review = _cached(base / 'book-review.json', 'book',
                     {'title': plan['title'], 'chapters': chapters, 'glossary': plan['glossary']}, rules)
    if review['issues']:
        (base / 'book-review.json').unlink()
        raise ValueError('Общая проверка книги: ' + '; '.join(review['issues']))
    book_path = base / 'book.json'
    write_json(book_path, {'signature': signature, 'title': plan['title'], 'chapters': chapters,
                          'introduction': review['introduction'], 'conclusion': review['conclusion']})
    return json.dumps({'book_path': str(book_path), 'output_dir': str(base),
                       'review_required': False, 'ready_for_build': True, 'message': ''})


def book_build_tool(session_id: str, output_dir: str) -> str:
    """Собирает проверенный пересказ в Markdown, HTML, EPUB и PNG страниц.

    Args:
        session_id: Идентификатор сессии.
        output_dir: Каталог с проверенным book.json.

    Returns:
        JSON с путями к собранным файлам.
    """
    base = Path(output_dir).resolve()
    book = json.loads((base / 'book.json').read_text(encoding='utf-8'))
    plan = _load_plan(base)
    rules = yaml.safe_load(RULES_PATH.read_text(encoding='utf-8'))
    if book['signature'] != _inputs_digest(base, plan, rules):
        raise ValueError('План или исходники изменились: сначала повторите book_summary')
    return json.dumps(build_files(base, book), ensure_ascii=False)
