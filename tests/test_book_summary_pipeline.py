import json
import shutil
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml

from custom_tools.book_summary import pipeline as p
from custom_tools.book_summary.files import build_files, read_chapters, write_json


SOURCE = ' '.join(f'слово{i}' for i in range(100))
TEXT = ' '.join(f'слово{i}' for i in range(30))
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'source.txt').write_text(SOURCE, encoding='utf-8')
    write_json(tmp_path / 'plan.json', {
        'title': 'Тестовая книга', 'glossary': [],
        'chapters': [{'id': '001', 'title': 'Первая глава', 'source_path': 'source.txt'}],
    })
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'glossary':
            return {'terms': []}
        if stage == 'verify':
            return {'issues': []}
        if stage == 'book':
            return {'introduction': 'Введение.', 'conclusion': 'Заключение.', 'issues': []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    return calls


@pytest.mark.parametrize('words,ok', [(19, False), (20, True), (40, True), (41, False)])
def test_size_boundaries(words, ok):
    assert p.check_text(SOURCE, ' '.join(['текст'] * words))['size_ok'] is ok


def test_quotes_and_code_checked_against_source():
    assert p.check_text('Пример «точная цитата»\nprint(1)', '«точная цитата»\n```python\nprint(1)\n```')['issues'] == []
    assert len(p.check_text('исходник', '«выдуманная цитата»\n```python\nprint(2)\n```')['issues']) == 2


@pytest.mark.parametrize('approved', [False, 'false', 'true', 1])
def test_optional_review_waits_for_explicit_boolean_approval(project, calls, approved):
    result = json.loads(p.book_summary_tool('test', str(project), approved, review_plan=True))
    assert result['review_required'] is True
    assert result['ready_for_build'] is False
    assert Path(result['plan_path']).is_file()
    assert 'plan_approved: true' in result['message']
    assert calls == []
    assert not (project / 'book.json').exists()


def test_default_runs_without_user_review(project, calls):
    result = json.loads(p.book_summary_tool('test', str(project)))
    assert result['review_required'] is False
    assert result['ready_for_build'] is True
    assert calls == ['write', 'review', 'clarity', 'verify', 'book']


def test_enabled_review_continues_after_approval(project, calls):
    p.book_summary_tool('test', str(project), review_plan=True)
    result = json.loads(p.book_summary_tool('test', str(project), plan_approved=True, review_plan=True))
    assert result['ready_for_build'] is True
    assert calls == ['write', 'review', 'clarity', 'verify', 'book']


@pytest.mark.parametrize('review_plan,approved,skip_build', [
    (False, False, False), (True, False, True), (True, True, False),
])
def test_workflow_build_condition_uses_review_result(project, calls, review_plan, approved, skip_build):
    from types import SimpleNamespace
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowDefinition

    definition = WorkflowDefinition.from_yaml(ROOT / 'workflow_pipelines/book_summary.yaml')
    engine = object.__new__(WorkflowEngine)
    context = SimpleNamespace(variables={'review_plan': review_plan, 'plan_approved': approved},
                              step_outputs={}, session_id='test', workflow_id='test')
    params = engine._substitute_variables_in_params(definition.steps[1].tool_params,
                                                    {**context.variables, 'output_dir': str(project)})
    result = json.loads(p.book_summary_tool('test', **params))
    engine._write_step_output(context, 'summarize', result)
    assert engine._should_skip_step_by_condition(definition.steps[2], context) is skip_build
    if skip_build:
        assert context.step_outputs['build'] == {
            'markdown_path': '', 'html_path': '', 'epub_path': '', 'png_dir': '',
        }
        assert calls == []
    else:
        assert calls == ['write', 'review', 'clarity', 'verify', 'book']


def test_skips_conditional_steps_and_resumes(project, calls):
    p.book_summary_tool('test', str(project), True)
    assert calls == ['write', 'review', 'clarity', 'verify', 'book']
    calls.clear()
    p.book_summary_tool('test', str(project), True)
    assert calls == []
    plan = json.loads((project / 'plan.json').read_text())
    plan['glossary'] = [{'source': 'term', 'ru': 'термин', 'meaning': 'значение'}]
    write_json(project / 'plan.json', plan)
    p.book_summary_tool('test', str(project), True)
    assert calls == ['write', 'review', 'clarity', 'verify', 'book']


def test_size_and_flow_conditions(project, monkeypatch):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'verify':
            return {'issues': []}
        if stage == 'book':
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': 'коротко' if stage == 'write' else TEXT, 'flow': 'low'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project), True)
    assert calls == ['write', 'size', 'review', 'flow', 'clarity', 'verify', 'book']


def test_failed_correction_resumes_without_rewriting(project, monkeypatch):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'clarity':
            return {'text': 'слишком коротко'}
        if stage == 'repair' and calls.count(stage) == 1:
            raise RuntimeError('Временный сбой редактора')
        if stage == 'verify':
            return {'issues': []}
        if stage == 'book':
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    with pytest.raises(RuntimeError, match='Временный сбой редактора'):
        p.book_summary_tool('test', str(project), True)
    assert not (project / 'book.json').exists()
    p.book_summary_tool('test', str(project), True)
    assert calls == ['write', 'review', 'clarity', 'repair', 'repair', 'verify', 'book']


def test_chapters_independent_and_book_waits(project, monkeypatch):
    plan = json.loads((project / 'plan.json').read_text())
    plan['chapters'].append({'id': '002', 'title': 'Вторая', 'source_path': 'source.txt'})
    write_json(project / 'plan.json', plan)
    second_started = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()

    def ask(stage, payload, rules):
        if stage == 'verify':
            return {'issues': []}
        if stage == 'book':
            assert first_finished.is_set() and second_finished.is_set()
            return {'introduction': '', 'conclusion': '', 'issues': []}
        if stage == 'write':
            if payload['title'] == 'Первая глава':
                assert second_started.wait(5)
            else:
                second_started.set()
                assert first_finished.wait(5)
        if stage == 'clarity':
            (first_finished if payload['title'] == 'Первая глава' else second_finished).set()
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project), True)


def test_stale_book_cannot_build(project, calls):
    p.book_summary_tool('test', str(project), True)
    (project / 'source.txt').write_text(SOURCE + ' изменено', encoding='utf-8')
    with pytest.raises(ValueError, match='изменились'):
        p.book_build_tool('test', str(project))


@pytest.mark.parametrize('bad_text,issue', [
    ('слишком коротко', '20–40%'),
    (TEXT + ' «выдуманная цитата»', 'цитата'),
    (TEXT + '\n```python\nprint(999)\n```', 'код'),
])
def test_failed_final_checks_are_corrected_in_same_run(project, monkeypatch, bad_text, issue):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'clarity':
            return {'text': bad_text}
        if stage == 'repair':
            assert issue in str(payload['issues'])
            assert payload['source'] == SOURCE
            assert payload['text'] == bad_text
            return {'text': TEXT}
        if stage == 'verify':
            assert payload['text'] == TEXT
            return {'issues': []}
        if stage == 'book':
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project))
    assert calls.count('repair') == 1
    assert calls.index('repair') < calls.index('verify') < calls.index('book')
    assert json.loads((project / 'book.json').read_text())['chapters'][0]['text'] == TEXT


def test_quality_verdict_triggers_repair_and_fresh_verification(project, monkeypatch):
    calls = []
    corrected = TEXT + ' исправлено'

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'verify':
            return {'issues': ['Искажена мысль автора'] if payload['text'] == TEXT else []}
        if stage == 'repair':
            assert payload['issues'] == ['Искажена мысль автора']
            return {'text': corrected}
        if stage == 'book':
            assert payload['chapters'][0]['text'] == corrected
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project))
    assert calls[-4:] == ['verify', 'repair', 'verify', 'book']


def test_book_findings_repair_only_affected_chapter_and_recheck_book(project, monkeypatch):
    plan = json.loads((project / 'plan.json').read_text())
    plan['chapters'].append({'id': '002', 'title': 'Вторая', 'source_path': 'source.txt'})
    write_json(project / 'plan.json', plan)
    repaired, book_calls = [], []

    def ask(stage, payload, rules):
        if stage == 'verify':
            return {'issues': []}
        if stage == 'repair':
            repaired.append(payload['chapter_id'])
            assert payload['source'] == SOURCE
            assert payload['issues'] == ['Термин противоречит словарю']
            return {'text': TEXT + ' исправлено'}
        if stage == 'book':
            book_calls.append(payload['chapters'])
            return {'introduction': '', 'conclusion': '', 'issues': [
                {'chapter_id': '001', 'message': 'Термин противоречит словарю'},
            ] if len(book_calls) == 1 else []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project))
    assert repaired == ['001']
    assert len(book_calls) == 2
    assert book_calls[1][0]['text'] == TEXT + ' исправлено'
    assert book_calls[1][1]['text'] == TEXT
    repaired.clear()
    book_calls.clear()
    p.book_summary_tool('test', str(project))
    assert repaired == []
    assert book_calls == []


def test_persistent_findings_stop_after_three_repairs(project, monkeypatch):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'verify':
            return {'issues': ['Осталось искажение факта']}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    with pytest.raises(ValueError, match='Осталось искажение факта'):
        p.book_summary_tool('test', str(project))
    assert calls.count('repair') == 3
    assert 'book' not in calls
    assert not (project / 'book.json').exists()


def test_failed_size_edit_is_corrected_instead_of_stopping(project, monkeypatch):
    calls = []

    def ask(stage, payload, rules):
        calls.append(stage)
        if stage == 'repair':
            return {'text': TEXT}
        if stage == 'verify':
            return {'issues': []}
        if stage == 'book':
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': 'коротко', 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project))
    assert calls == ['write', 'size', 'review', 'clarity', 'repair', 'verify', 'book']


def test_repair_introducing_wrong_size_is_corrected_again(project, monkeypatch):
    repairs, verified = [], []

    def ask(stage, payload, rules):
        if stage == 'verify':
            verified.append(payload['text'])
            return {'issues': ['Ошибка смысла'] if len(verified) == 1 else []}
        if stage == 'repair':
            repairs.append(payload['issues'])
            return {'text': 'коротко' if len(repairs) == 1 else TEXT + ' исправлено'}
        if stage == 'book':
            return {'introduction': '', 'conclusion': '', 'issues': []}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    p.book_summary_tool('test', str(project))
    assert repairs[0] == ['Ошибка смысла']
    assert '20–40%' in repairs[1][0]
    assert verified == [TEXT, TEXT + ' исправлено']


def test_persistent_book_findings_stop_without_accepting_result(project, monkeypatch):
    repairs, reviews = [], []

    def ask(stage, payload, rules):
        if stage == 'verify':
            return {'issues': []}
        if stage == 'repair':
            repairs.append(payload['chapter_id'])
        if stage == 'book':
            reviews.append(True)
            return {'introduction': '', 'conclusion': '', 'issues': [
                {'chapter_id': '001', 'message': 'Противоречие между главами'},
            ]}
        return {'text': TEXT, 'flow': 'good'}

    monkeypatch.setattr(p, '_ask', ask)
    with pytest.raises(ValueError, match='Противоречие между главами'):
        p.book_summary_tool('test', str(project))
    assert repairs == ['001'] * 3
    assert len(reviews) == 4
    assert not (project / 'book.json').exists()


def make_epub(path):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        z.writestr('OEBPS/book.opf', '<package><manifest><item id="b" href="b.xhtml"/><item id="a" href="a.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
        z.writestr('OEBPS/a.xhtml', '<html><body><h1>Первая</h1><p>Первый текст</p></body></html>')
        z.writestr('OEBPS/b.xhtml', '<html><body><h1>Вторая</h1><p>Второй текст</p></body></html>')


def test_epub_spine_and_prepare_preserves_reviewed_plan(tmp_path, calls):
    epub = tmp_path / 'input.epub'
    make_epub(epub)
    chapters = read_chapters(epub)
    assert [c['title'] for c in chapters] == ['Первая', 'Вторая']
    out = tmp_path / 'project'
    result = json.loads(p.book_prepare_tool('test', str(epub), str(out)))
    plan = json.loads(Path(result['plan_path']).read_text())
    assert len(plan['chapters']) == 2
    plan['title'] = 'Одобренное название'
    write_json(out / 'plan.json', plan)
    calls.clear()
    p.book_prepare_tool('test', str(epub), str(out))
    assert calls == []
    assert json.loads((out / 'plan.json').read_text())['title'] == 'Одобренное название'
    epub.write_bytes('другая книга'.encode())
    with pytest.raises(ValueError, match='другую книгу'):
        p.book_prepare_tool('test', str(epub), str(out))


@pytest.mark.skipif(not shutil.which('pdftotext') or not shutil.which('pdftoppm'), reason='Нужен Poppler')
def test_real_build_and_pdf_readback(tmp_path):
    from PIL import Image

    book = {'title': 'Книга & пересказ', 'introduction': 'Краткое введение.',
            'conclusion': 'Заключение.', 'chapters': [{'title': 'Глава 1', 'text': 'Текст **главы**.\n\n```python\nprint(1)\n```'}]}
    result = build_files(tmp_path, book)
    assert Path(result['markdown_path']).read_text().startswith('# Книга & пересказ')
    assert 'href="#section-1"' in Path(result['html_path']).read_text()
    with zipfile.ZipFile(result['epub_path']) as z:
        assert z.namelist()[0] == 'mimetype'
        assert z.getinfo('mimetype').compress_type == zipfile.ZIP_STORED
        for name in z.namelist():
            if name.endswith(('.xml', '.opf', '.xhtml')):
                ET.fromstring(z.read(name))
    pages = list(Path(result['png_dir']).glob('*.png'))
    assert pages
    with Image.open(pages[0]) as image:
        assert max(image.size) == 1600
    chapters = read_chapters(tmp_path / 'final/book.pdf')
    assert any('Текст' in c['text'] for c in chapters)


def test_yaml_and_registry():
    from agent_factory import ToolRegistry
    from workflow.models import WorkflowDefinition

    registry = ToolRegistry()
    names = ['book_prepare_tool', 'book_summary_tool', 'book_build_tool']
    resolved = registry.resolve_many(names, profile_name='direct')
    assert all(resolved[name].name == name for name in names)
    assert not (ROOT / 'workflow_pipelines/book_prep.yaml').exists()
    for filename in ['book_summary.yaml']:
        definition = WorkflowDefinition.from_yaml(ROOT / 'workflow_pipelines' / filename)
        assert definition.steps
        data = yaml.safe_load((ROOT / 'workflow_pipelines' / filename).read_text())
        assert all(s['tool_name'] in names for s in data['steps'])
        assert data['global_retry_policy']['max_retries'] == 0
        assert data['inputs']['review_plan'] is False
        assert [s['id'] for s in data['steps']] == ['prepare', 'summarize', 'build']
        assert data['steps'][1]['depends_on'] == ['prepare']
        assert data['steps'][2]['condition'] == '{summarize.ready_for_build}'
