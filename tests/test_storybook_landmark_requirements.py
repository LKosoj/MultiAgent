import hashlib
import json
from pathlib import Path

import pytest

from custom_tools.storybook import continuity, reference_catalog, artist_batch_edit, background_references
from custom_tools import image_tools


def test_landmark_creates_missing_location_source_first(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    bible = base / '20_bible'
    bible.mkdir(parents=True)
    source = bible / 'references/locations/place.png'
    (bible / 'locations.json').write_text(json.dumps([{'name': 'Place', 'reference_image_path': str(source)}]))
    (bible / 'continuity_plan.json').write_text(json.dumps({'entities': [
        {'id': 'gate', 'name': 'Gate', 'kind': 'landmark', 'world_id': 'world',
         'locations': ['Place'], 'reference_location': 'Place', 'geometry': 'Two posts'}]}))
    requested = []
    def prepare(session_id, project_id, items, *args, **kwargs):
        requested.append((project_id, items))
        source.parent.mkdir(parents=True)
        source.write_bytes(b'location source')
    monkeypatch.setattr(artist_batch_edit, '_ensure_references_exist', prepare)
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', lambda **k: Path(k['output_path']).write_bytes(b'landmark'))
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *a: True)

    continuity.ensure_landmark_references(base, 's', 0)

    assert requested == [('project', [{'reference_image_paths': [str(source)]}])]


@pytest.mark.parametrize('cached_source', [False, True])
def test_unchanged_landmark_edit_keeps_source_reference(tmp_path, monkeypatch, cached_source):
    (tmp_path / 'project.json').write_text('{"format":"storybook-series-v1"}')
    source = tmp_path / 'source.png'
    source.write_bytes(b'source')
    source = Path(reference_catalog.store_reference(tmp_path, source, 'location', 'location:place'))
    bible = tmp_path / '20_bible'
    bible.mkdir()
    (bible / 'locations.json').write_text(json.dumps([{'name': 'Place', 'reference_image_path': str(source)}]))
    (bible / 'continuity_plan.json').write_text(json.dumps({'entities': [
        {'id': 'gate', 'name': 'Gate', 'kind': 'landmark', 'world_id': 'world',
         'locations': ['Place'], 'reference_location': 'Place', 'geometry': 'Two posts'}]}))
    monkeypatch.setattr(reference_catalog, 'reuse_reference', lambda *a, **k: str(source) if cached_source else None)
    monkeypatch.setattr(reference_catalog, 'reference_review_current', lambda *a: False)
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *a: False)
    monkeypatch.setattr(continuity, 'existing_reference_path', lambda base, value: str(source))
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', lambda **k: Path(k['output_path']).write_bytes(b'source'))

    with pytest.raises(RuntimeError, match='unchanged source'):
        continuity.ensure_landmark_references(tmp_path, 's', 0)

    assert source.read_bytes() == b'source'
    records = reference_catalog._read(tmp_path)['references']
    assert len(records) == 1
    assert records[hashlib.sha256(b'source').hexdigest()]['selectable']
    assert records[hashlib.sha256(b'source').hexdigest()]['kinds'] == ['location']


def test_landmark_keeps_fixed_parts_and_excludes_movable_props(tmp_path, monkeypatch):
    bible = tmp_path / '20_bible'
    bible.mkdir()
    source = tmp_path / 'source.png'
    source.write_bytes(b'source')
    entity = {'id': 'gate', 'name': 'Gate', 'kind': 'landmark', 'world_id': 'world',
              'geometry': 'Gate with a fixed panel', 'locations': ['Place'], 'reference_location': 'Place'}
    props = [dict(id=name, name=name, kind='prop', world_id='world', locations=['Place'], states=states)
             for name, states in [
                 ('Panel', [{'support': 'wall', 'holder': None, 'motion': 'stationary'}]),
                 ('Tool', [{'support': 'hand', 'holder': 'Worker', 'motion': 'stationary'}]),
                 ('Card', [{'support': 'tray', 'holder': None, 'motion': 'stationary'},
                           {'support': 'wall', 'holder': None, 'motion': 'stationary'}])]]
    (bible / 'continuity_plan.json').write_text(json.dumps({'entities': [entity, *props]}))
    (bible / 'locations.json').write_text(json.dumps([{'name': 'Place', 'reference_image_path': str(source)}]))
    generated, reviews = [], []
    def generate(**kwargs):
        generated.append(kwargs)
        Path(kwargs['output_path']).write_bytes(b'new landmark')
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', generate)
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda path, data: reviews.append(data) or True)

    continuity.ensure_landmark_references(tmp_path, 's', 0)

    assert 'Panel' not in generated[0]['negative_prompt']
    assert all('Panel' not in requirement for requirement in reviews[0]['reference_requirements'])
    for name in ('Tool', 'Card'):
        assert name in generated[0]['negative_prompt']
        assert name in reviews[0]['reference_requirements'][-1]


@pytest.mark.parametrize('cached_content', [b'valid', b'invalid', None])
def test_landmark_requirements_checked_before_reuse_or_publication(tmp_path, monkeypatch, cached_content):
    (tmp_path / '20_bible').mkdir()
    source = tmp_path / 'source.png'
    source.write_bytes(b'source')
    entity = {'id': 'fixed-object', 'name': 'Gate', 'kind': 'landmark', 'world_id': 'place',
              'geometry': 'Two equal posts with one lintel', 'locations': ['Place'], 'reference_location': 'Place'}
    (tmp_path / '20_bible/continuity_plan.json').write_text(json.dumps({'entities': [entity]}))
    (tmp_path / '20_bible/locations.json').write_text(json.dumps([
        {'name': 'Place', 'reference_image_path': str(source), 'spatial_layout': 'Declared arrangement'}]))
    cached = tmp_path / 'cached.png'
    if cached_content is not None:
        cached.write_bytes(cached_content)
    monkeypatch.setattr(background_references, '_reference_path', lambda base, value: Path(value))
    monkeypatch.setattr(reference_catalog, 'reuse_reference', lambda *args, **kwargs: str(cached) if cached.is_file() else None)
    monkeypatch.setattr(reference_catalog, 'reference_review_current', lambda *args: False)
    rejected, accepted, generated, reviewed = [], [], [], []
    monkeypatch.setattr(reference_catalog, 'reject_reference', lambda base, path, reason: rejected.append(str(path)))
    monkeypatch.setattr(reference_catalog, 'accept_reference_review', lambda base, path, review: accepted.append(str(path)))
    monkeypatch.setattr(reference_catalog, 'store_reference', lambda base, path, *args, **kwargs: str(path))
    def check(path, data):
        reviewed.append((str(path), data))
        return Path(path).read_bytes() == b'valid'
    def generate(**kwargs):
        generated.append(kwargs)
        Path(kwargs['output_path']).write_bytes(b'valid')
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', check)
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', generate)
    continuity.ensure_landmark_references(tmp_path, 's', 0)
    assert len(reviewed) == (2 if cached_content == b'invalid' else 1)
    assert all(entity['geometry'] in data['reference_requirements'] for _, data in reviewed)
    assert len(generated) == (0 if cached_content == b'valid' else 1)
    assert rejected == ([str(cached)] if cached_content == b'invalid' else [])
    saved = json.loads((tmp_path / '20_bible/continuity_plan.json').read_text())
    assert saved['entities'][0]['reference_path'] == accepted[-1]
    if generated:
        assert entity['geometry'] in generated[0]['prompt']
        assert reviewed[-1][1]['reference_requirements'][1] in generated[0]['prompt']


@pytest.mark.parametrize('unavailable', [False, True])
def test_invalid_or_unverified_new_landmark_is_not_published(tmp_path, monkeypatch, unavailable):
    real_store = reference_catalog.store_reference
    real_reject = reference_catalog.reject_reference
    real_reuse = reference_catalog.reuse_reference
    (tmp_path / 'project.json').write_text(json.dumps({'format': 'storybook-series-v1'}))
    (tmp_path / '20_bible').mkdir()
    source = tmp_path / 'source.png'
    source.write_bytes(b'source')
    plan = {'entities': [{'id': 'object', 'name': 'Gate', 'kind': 'landmark', 'world_id': 'place',
                         'geometry': 'Two posts', 'locations': ['Place'], 'reference_location': 'Place'}]}
    (tmp_path / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
    (tmp_path / '20_bible/locations.json').write_text(json.dumps([
        {'name': 'Place', 'reference_image_path': str(source)}]))
    monkeypatch.setattr(background_references, '_reference_path', lambda base, value: Path(value))
    monkeypatch.setattr(reference_catalog, 'reuse_reference', lambda *args, **kwargs: None)
    monkeypatch.setattr(reference_catalog, 'reference_review_current', lambda *args: False)
    archived = []
    def archive(base, path, *args, **kwargs):
        if unavailable:
            pytest.fail('Image published without receiving a review')
        history = real_store(base, path, *args, **kwargs)
        archived.append(history)
        return history
    monkeypatch.setattr(reference_catalog, 'store_reference', archive)
    rejected = []
    def reject(base, path, reason):
        rejected.append(str(path))
        real_reject(base, path, reason)
    monkeypatch.setattr(reference_catalog, 'reject_reference', reject)
    monkeypatch.setattr(reference_catalog, 'accept_reference_review', lambda *args: pytest.fail('Cannot approve invalid image'))
    def check(*args):
        if unavailable:
            raise RuntimeError('Reference image unavailable')
        return False
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', check)
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', lambda **kwargs: Path(kwargs['output_path']).write_bytes(b'invalid'))
    with pytest.raises(RuntimeError, match='Reference image unavailable' if unavailable else 'Landmark reference failed requirements'):
        continuity.ensure_landmark_references(tmp_path, 's', 0)
    if unavailable:
        assert rejected == []
        assert len(list((tmp_path / '20_bible/references/landmarks').glob('*.png'))) == 1
    else:
        assert rejected == archived and len(rejected) == 1
        # Отклонённый файл удаляется с диска; имя в хранилище равно sha его содержимого.
        assert not Path(rejected[0]).exists() and Path(rejected[0]).stem == hashlib.sha256(b'invalid').hexdigest()
        assert not list((tmp_path / '20_bible/references/landmarks').glob('*.png'))
        records = json.loads((tmp_path / 'shared/reference_catalog.json').read_text())['references']
        record = next(iter(records.values()))
        assert not record['selectable'] and record['metadata']['rejection_reason']
        assert real_reuse(tmp_path, 'landmark', record['keys'][0]) is None
    assert json.loads((tmp_path / '20_bible/continuity_plan.json').read_text()) == plan
    if unavailable:
        monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: True)
        monkeypatch.setattr(reference_catalog, 'store_reference', lambda base, path, *args, **kwargs: str(path))
        monkeypatch.setattr(reference_catalog, 'accept_reference_review', lambda *args: None)
        monkeypatch.setattr(image_tools, 'edit_image_vse_tool', lambda **kwargs: pytest.fail('Candidate already exists'))
        continuity.ensure_landmark_references(tmp_path, 's', 0)
        saved = json.loads((tmp_path / '20_bible/continuity_plan.json').read_text())
        assert Path(saved['entities'][0]['reference_path']).is_file()
