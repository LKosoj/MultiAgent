import json
from pathlib import Path
import pytest
from PIL import Image

from custom_tools.storybook import artist_batch_edit as artist
from custom_tools.storybook import project_paths
from custom_tools.storybook.background_references import _reference_path


def test_two_series_generate_one_project_reference(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    scope = project_paths.ContextVar('reference_storage_test', default=None)
    monkeypatch.setattr(project_paths, '_series_scope', scope)
    reference = '/references/characters/hero.png'
    for episode in ('one', 'two'):
        bible = tmp_path / 'p/series' / episode / '20_bible'
        bible.mkdir(parents=True)
        (bible / 'characters.json').write_text(json.dumps([
            {'name': 'Hero', 'age': '21', 'reference_image_path': reference}]))
    calls = []

    def generate(_session, _project, _character, output, _pipeline, seed=None):
        calls.append(output)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b'new project reference')
        return True

    monkeypatch.setattr(artist, '_generate_image_from_scratch', generate)
    for episode in ('one', 'two'):
        scope.set(('p', episode))
        artist._ensure_references_exist('test', 'p', [{'reference_image_paths': [reference]}], [], 'workflow')
        assert _reference_path(project_paths.safe_storybook_project_dir('p'), reference) == tmp_path / 'p/20_bible/references/characters/hero.png'
    assert len(calls) == 1
    assert not list((tmp_path / 'p/series').rglob('*.png'))


@pytest.mark.parametrize('size,review_passed,accepted', [
    ((1536, 1024), True, True), ((941, 1672), True, False), ((1536, 1024), False, False),
])
def test_reference_generation_uses_style_and_rejects_wrong_size(tmp_path, monkeypatch, size, review_passed, accepted):
    import utils
    from custom_tools.storybook import style_keeper
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    style = tmp_path / 'p/30_style/style_images.json'
    style.parent.mkdir(parents=True)
    style.write_text('{"art_style":"photorealism"}')
    output = tmp_path / 'p/20_bible/references/characters/hero.png'
    generated = tmp_path / 'generated.png'
    captured = []
    monkeypatch.setattr(utils, 'translate_prompts_in_items', lambda item, *_: item)
    monkeypatch.setattr(style_keeper, 'ensure_image_style_catalog', lambda *_: {
        'default_style_id': 'main', 'styles': {'main': {'prompt': 'PHOTO_STYLE_MARKER'}}})

    class Agent:
        def run(self, task, stream=False):
            captured.append(task)
            Image.new('RGB', size).save(generated)
            return str(generated)

    class Factory:
        def create_agent(self, **kwargs):
            return Agent()

    monkeypatch.setattr(artist, 'AgentFactory', Factory)
    monkeypatch.setattr(artist, '_parse_output_path', lambda *_: str(generated))
    monkeypatch.setattr(artist, '_reference_requirements_met', lambda *_: review_passed)
    assert artist._generate_canon_reference_from_scratch('test', 'character', {'name': 'Hero'}, str(output), [], 'workflow') is accepted
    assert output.is_file() is accepted
    assert 'PHOTO_STYLE_MARKER' in captured[0]
    assert 'Face close-up' in captured[0] and 'half the sheet width' in captured[0]
    assert 'width: 1536' in captured[0] and 'height: 1024' in captured[0]


@pytest.mark.parametrize('passed,violations,accepted', [
    (False, ['Cartoonishly oversized ears'], False),
    (True, ['Cartoonishly oversized ears'], False),
    (True, [], True),
])
def test_reference_requirements_are_checked_on_actual_pixels(tmp_path, monkeypatch, passed, violations, accepted):
    import utils
    image = tmp_path / 'reference.png'
    Image.new('RGB', (1536, 1024)).save(image)
    calls = []
    def review(**kwargs):
        calls.append(kwargs)
        return json.dumps({'passed': passed, 'violations': violations})
    monkeypatch.setattr(utils, 'call_openai_api', review)
    requirements = ['Ordinary adult human ear size, only slightly protruding; no caricature.']
    assert artist._reference_requirements_met(str(image), {'name': 'Actor', 'reference_requirements': requirements}) is accepted
    assert calls[0]['image_url'].startswith('data:image/png;base64,')
    assert json.loads(calls[0]['prompt'])['requirements'] == requirements
    assert 'ears' not in calls[0]['system_prompt'].lower()
    assert 'photorealism' not in calls[0]['system_prompt'].lower()


@pytest.mark.parametrize('entity_type', ['character', 'location'])
def test_reference_generator_uses_each_projects_shared_style(tmp_path, monkeypatch, entity_type):
    import utils
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    scope = project_paths.ContextVar('project_style_test', default=None)
    monkeypatch.setattr(project_paths, '_series_scope', scope)
    monkeypatch.setattr(utils, 'translate_prompts_in_items', lambda item, *_: item)
    captured = []
    generated = tmp_path / 'generated.png'

    class Agent:
        def run(self, task, stream=False):
            captured.append(task)
            Image.new('RGB', (1536, 1024)).save(generated)
            return str(generated)

    class Factory:
        def create_agent(self, **kwargs):
            return Agent()

    monkeypatch.setattr(artist, 'AgentFactory', Factory)
    monkeypatch.setattr(artist, '_parse_output_path', lambda *_: str(generated))
    for pid, style in [('photo', 'Photorealistic natural photograph'), ('toon', 'Hand-drawn cartoon animation')]:
        root = tmp_path / pid
        shared = root / 'shared/style/main/v1'
        shared.mkdir(parents=True)
        (shared / 'data.json').write_text(json.dumps({'style_images': {'art_style': style}}))
        (root / 'project.json').write_text(json.dumps({'resources': {'style': {'main': 'v1'}}}))
        episode = root / 'series/one/30_style'
        episode.mkdir(parents=True)
        (episode / 'style_images.json').write_text('{"art_style":"WRONG_SERIES_STYLE"}')
        scope.set((pid, 'one'))
        assert artist._generate_canon_reference_from_scratch('test', entity_type, {'name': 'Reference'},
            str(root / '20_bible/references' / (entity_type + 's') / 'reference.png'), [], 'workflow')
        assert style in captured[-1] and 'WRONG_SERIES_STYLE' not in captured[-1]
    assert 'cartoon' not in captured[0].lower()
    assert 'photorealistic' not in captured[1].lower()


def test_legacy_series_reference_remains_readable(tmp_path):
    base = tmp_path / 'p/series/one'
    legacy = base / '20_bible/references/characters/hero.png'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy reference')
    assert _reference_path(base, '/references/characters/hero.png') == legacy
    common = tmp_path / 'p/20_bible/references/characters/hero.png'
    common.parent.mkdir(parents=True)
    common.write_bytes(b'shared reference')
    assert _reference_path(base, '/references/characters/hero.png') == common


def test_existing_series_alias_is_reused_without_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(project_paths, '_series_scope', project_paths.ContextVar('legacy_alias', default=('p', 'one')))
    base = tmp_path / 'p/series/one'
    legacy = base / '20_bible/references/characters/hero.png'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'existing reference')
    alias = '/references/characters/hero.png'
    def unexpected_generation(*args, **kwargs):
        raise AssertionError('Existing reference must be reused')
    monkeypatch.setattr(artist, '_generate_image_from_scratch', unexpected_generation)
    monkeypatch.setattr(artist, '_generate_canon_reference_from_scratch', unexpected_generation)
    monkeypatch.setattr(artist, '_collect_visible_text_bindings', lambda *_: [])
    artist._ensure_references_exist('test', 'p', [{'reference_image_paths': [alias]}], [], 'workflow')
    _, paths, _, _ = artist._build_edit_instruction('test', {
        'project_id': 'p', 'english_prompt': 'A neutral room', 'reference_image_paths': [alias]}, language='en')
    assert paths == [str(legacy)]


def test_identity_cleanup_is_reused_across_series(tmp_path, monkeypatch):
    from custom_tools.storybook.continuity import identity_reference_path
    from custom_tools import image_tools
    root = tmp_path / 'p'
    source = root / '20_bible/references/characters/hero.png'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'canonical identity')
    plan = {'entities': [{'id': 'book', 'kind': 'prop', 'name': 'Book', 'states': [{'holder': 'Hero'}]}]}
    calls = []

    def edit(**kwargs):
        calls.append(kwargs)
        Path(kwargs['output_path']).write_bytes(b'cleaned identity')

    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', edit)
    results = []
    for episode in ('one', 'two'):
        base = root / 'series' / episode
        (base / '20_bible').mkdir(parents=True)
        (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
        results.append(identity_reference_path(base, {'name': 'Hero', 'reference_image_path': '/references/characters/hero.png'}, 'test', 42))
    assert results[0] == results[1]
    assert Path(results[0]).is_relative_to(root / '20_bible/references')
    assert len(calls) == 1
