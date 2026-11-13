import base64
import json
from pathlib import Path

import pytest

from custom_tools.storybook import reference_catalog as catalog
from custom_tools.storybook.project_paths import series_scope
from custom_tools.storybook.studio.capabilities import invoke


def call(name, **kwargs):
    result = invoke(name, kwargs)
    assert result['status'] == 'success', result
    return result


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='Shared world of a show', genre='comedy', target_age='all')['project_id']
    return pid, tmp_path / pid


def resource(pid, kind, entity, image):
    return call('shared_resource_save', project_id=pid, kind=kind, entity_id=entity,
                data={'name': entity}, reference_base64=base64.b64encode(image).decode(),
                reference_filename='reference.png')['version_id']


def series(pid, title, participants=(), source=''):
    return call('series_create', project_id=pid, title=title, source_material=source,
                participant_ids=list(participants), settings={})['series_id']


def test_creation_selects_existing_location_and_only_selected_participants(project, monkeypatch):
    pid, root = project
    resource(pid, 'characters', 'actor', b'actor')
    resource(pid, 'characters', 'other', b'other')
    resource(pid, 'locations', 'wanted', b'wanted location')
    resource(pid, 'locations', 'unrelated', b'unrelated location')
    requests = []
    def choose(source, candidates, metadata=None):
        requests.append(source)
        return [r['id'] for r in candidates if r['metadata'].get('entity_id') == 'wanted']
    monkeypatch.setattr(catalog, '_choose', choose)
    sid = series(pid, 'One', ['actor'], 'Actor at wanted')
    base = root / 'series' / sid
    refs = catalog.bound_references(base)
    assert {r['metadata']['entity_id'] for r in refs} == {'actor', 'wanted'}
    assert requests == ['Actor at wanted']
    snapshot = json.loads((base / 'series.json').read_text())['snapshot']
    assert set(snapshot['locations']) == {'wanted'}
    assert all(Path(path).is_relative_to(root / 'shared') for path in catalog.bound_reference_paths(base))
    assert not list(base.rglob('*.png'))


def test_same_reference_reused_between_series_and_foreign_reference_rejected(project):
    pid, root = project
    a, b = series(pid, 'One'), series(pid, 'Two')
    source = root / 'sample.png'
    source.write_bytes(b'one reference')
    first = catalog.store_reference(root / 'series' / a, source, 'appearance', 'same-request',
                                    description='same appearance')
    assert catalog.reuse_reference(root / 'series' / b, 'appearance', 'same-request') == first
    record, = catalog.bound_references(root / 'series' / b)
    assert set(record['series_ids']) == {a, b}
    foreign = root / 'foreign.png'
    foreign.write_bytes(b'other reference')
    foreign_path = catalog.store_reference(root / 'series' / a, foreign, 'location', 'other')
    with pytest.raises(ValueError, match='not bound'):
        catalog.resolve_reference(root / 'series' / b, foreign_path)
    assert catalog.resolve_reference(root / 'series' / b, first) == Path(first)
    own = root / 'series' / b / '97_shots' / 'start.png'
    own.parent.mkdir(parents=True)
    own.write_bytes(b'frame')
    assert catalog.resolve_reference(root / 'series' / b, own) == own
    with pytest.raises(ValueError, match='Unregistered'):
        catalog.resolve_reference(root / 'series' / a, own)


def test_version_apply_preserves_other_series_binding(project):
    pid, root = project
    old = resource(pid, 'characters', 'actor', b'old image')
    a, b = series(pid, 'One', ['actor']), series(pid, 'Two', ['actor'])
    new = resource(pid, 'characters', 'actor', b'new image')
    call('series_resource_update_apply', project_id=pid, series_id=a, kind='characters', entity_id='actor', version_id=new)
    assert {r['metadata']['version_id'] for r in catalog.bound_references(root / 'series' / a)} == {new}
    assert {r['metadata']['version_id'] for r in catalog.bound_references(root / 'series' / b)} == {old}
    c = series(pid, 'Three', ['actor'])
    assert {r['metadata']['version_id'] for r in catalog.bound_references(root / 'series' / c)} == {new}
    call('series_delete', project_id=pid, series_id=a, confirm=True)
    assert all(a not in r['series_ids'] for r in catalog._read(root)['references'].values())
    assert catalog.bound_reference_paths(root / 'series' / b)


def test_artist_collects_bound_refs_only(project):
    from custom_tools.storybook.artist_batch_edit import _collect_project_references
    pid, root = project
    a, b = series(pid, 'One'), series(pid, 'Two')
    source = root / 'image.png'
    source.write_bytes(b'only series one')
    path = catalog.store_reference(root / 'series' / a, source, 'character', 'actor')
    with series_scope(pid, b):
        assert _collect_project_references(pid, 'character') == []
    with series_scope(pid, a):
        assert _collect_project_references(pid, 'character') == [path]


def test_image_api_cannot_receive_unbound_reference(project, monkeypatch):
    from custom_tools.image_tools import edit_image_vse_tool
    import requests
    pid, root = project
    a, b = series(pid, 'One'), series(pid, 'Two')
    source = root / 'image.png'
    source.write_bytes(b'reference')
    path = catalog.store_reference(root / 'series' / a, source, 'character', 'actor')
    def forbidden(*args, **kwargs):
        pytest.fail('Unbound input reached HTTP')
    monkeypatch.setattr(requests, 'post', forbidden)
    with series_scope(pid, b):
        result = edit_image_vse_tool('test', [path], 'test')
    assert 'not bound' in result


def test_face_update_unbinds_derived_images_only_for_updated_series(project, monkeypatch):
    pid, root = project
    resource(pid, 'characters', 'actor', b'old face')
    a, b = series(pid, 'One', ['actor']), series(pid, 'Two', ['actor'])
    parent, = catalog.bound_reference_paths(root / 'series' / a)
    image = root / 'appearance.png'
    image.write_bytes(b'old face in costume')
    derived = catalog.store_reference(root / 'series' / a, image, 'appearance', 'costume', sources=[parent])
    catalog.reuse_reference(root / 'series' / b, 'appearance', 'costume')
    new = resource(pid, 'characters', 'actor', b'new face')
    call('series_resource_update_apply', project_id=pid, series_id=a, kind='characters', entity_id='actor', version_id=new)
    assert derived not in catalog.bound_reference_paths(root / 'series' / a)
    assert derived in catalog.bound_reference_paths(root / 'series' / b)
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [r['id'] for r in candidates])
    c = series(pid, 'Three', ['actor'], 'Actor in costume')
    assert derived not in catalog.bound_reference_paths(root / 'series' / c)


def test_changed_source_cannot_semantically_reuse_old_face(project, monkeypatch):
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    image = root / 'appearance.png'
    image.write_bytes(b'old face')
    catalog.store_reference(base, image, 'appearance', 'old-key', description='Actor in costume',
                            metadata={'entity': 'actor', 'source_hashes': ['old-face']})
    def choose(source, candidates, metadata=None):
        assert not candidates
        return []
    monkeypatch.setattr(catalog, '_choose', choose)
    assert catalog.reuse_reference(base, 'appearance', 'new-key', description='Actor in costume',
                                   metadata={'entity': 'actor', 'source_hashes': ['new-face']}) is None


def test_new_canon_is_registered_and_bound_in_project(project):
    pid, root = project
    sid = series(pid, 'One')
    output = root / '20_bible/references/locations/new_place.png'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'new location')
    with series_scope(pid, sid):
        canonical = catalog.register_canon_reference(output, 'location', {'name': 'new place'})
    assert Path(canonical).is_relative_to(root / 'shared/references')
    assert catalog.bound_reference_paths(root / 'series' / sid) == [canonical]


def test_missing_canon_reuses_project_reference_before_generation(project, monkeypatch):
    from custom_tools.storybook import artist_batch_edit as artist
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    a, b = series(pid, 'One'), series(pid, 'Two')
    source = root / '20_bible/references/characters/hero.png'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'existing hero')
    canonical = catalog.store_reference(root / 'series' / a, source, 'character', 'first',
                                        description='Hero', metadata={'entity': 'Hero'})
    reference = '/references/characters/hero.png'
    bible = root / 'series' / b / '20_bible/characters.json'
    bible.write_text(json.dumps([{'name': 'Hero', 'reference_image_path': reference}]))
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [r['id'] for r in candidates])
    def forbidden(*args, **kwargs):
        pytest.fail('Existing suitable reference must not generate again')
    monkeypatch.setattr(artist, '_generate_image_from_scratch', forbidden)
    monkeypatch.setattr(artist, '_generate_canon_reference_from_scratch', forbidden)
    with series_scope(pid, b):
        assert _reference_path(root / 'series' / b, reference) != source
        artist._ensure_references_exist('test', pid, [{'reference_image_paths': [reference]}], [], 'workflow')
        assert str(_reference_path(root / 'series' / b, reference)) == canonical
    assert catalog.bound_reference_paths(root / 'series' / b) == [canonical]


def test_legacy_alias_with_identical_bytes_resolves_to_bound_reference(project):
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    resource(pid, 'characters', 'actor', b'actor reference')
    sid = series(pid, 'One', ['actor'])
    legacy = root / '20_bible/references/characters/actor.png'
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b'actor reference')
    canonical, = catalog.bound_reference_paths(root / 'series' / sid)
    assert str(_reference_path(root / 'series' / sid, '/references/characters/actor.png')) == canonical


def test_quality_rejection_prevents_reuse_of_asset_and_derivatives(project):
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    source = root / 'bad.png'
    source.write_bytes(b'bad derived identity')
    parent = catalog.store_reference(base, source, 'identity', 'bad')
    source.write_bytes(b'variant based on bad identity')
    child = catalog.store_reference(base, source, 'appearance', 'child', sources=[parent])
    catalog.reject_reference(base, parent, 'Identity detail missing')
    assert catalog.reuse_reference(base, 'identity', 'bad') is None
    assert catalog.reuse_reference(base, 'appearance', 'child') is None
    assert catalog.bound_reference_paths(base) == []
    assert not Path(parent).exists() and not Path(child).exists()


def test_review_acceptance_requires_same_bytes_and_requirements(project):
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    image = root / 'candidate.png'
    image.write_bytes(b'reviewed image')
    requirements = ['Keep the declared appearance and style.']
    path = catalog.store_reference(base, image, 'identity', 'request')
    assert not catalog.reference_review_current(base, path, requirements)
    catalog.accept_reference_review(base, path, requirements)
    assert catalog.reference_review_current(base, path, requirements)
    assert not catalog.reference_review_current(base, path, ['A changed appearance requirement.'])
    catalog.accept_reference_review(base, path, ['A changed appearance requirement.'])
    assert catalog.reference_review_current(base, path, requirements)
    assert catalog.reference_review_current(base, path, ['A changed appearance requirement.'])
    Path(path).write_bytes(b'changed image bytes')
    assert not catalog.reference_review_current(base, path, requirements)


def test_rejected_reference_cannot_reuse_its_review_acceptance(project):
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    image = root / 'candidate.png'
    image.write_bytes(b'reviewed image')
    requirements = ['Preserve identity.']
    path = catalog.store_reference(base, image, 'identity', 'request')
    catalog.accept_reference_review(base, path, requirements)
    catalog.reject_reference(base, path, 'Explicit visual rejection')
    assert not catalog.reference_review_current(base, path, requirements)


def test_identity_resume_reuses_only_matching_review_acceptance(project, monkeypatch):
    from PIL import Image
    from custom_tools import image_tools
    from custom_tools.storybook import continuity, artist_batch_edit
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    source = root / 'actor.png'
    Image.new('RGB', (16, 16), 'gray').save(source)
    approved = catalog.store_reference(base, source, 'character', 'actor')
    actor = {'name': 'Actor', 'reference_image_path': approved, 'reference_requirements': ['Preserve identity.']}
    plan = {'character_identity': {'Actor': {'excluded_entity_ids': ['prop']}}, 'entities': [
        {'id': 'prop', 'name': 'prop', 'kind': 'prop', 'shape': 'a held object', 'states': [{'holder': 'Actor'}]}]}
    monkeypatch.setattr(continuity, 'load_continuity_plan', lambda base: plan)
    edits, reviews = [], []
    def edit(**kwargs):
        edits.append(kwargs)
        Path(kwargs['output_path']).parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (16, 16), 'blue').save(kwargs['output_path'])
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', edit)
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: reviews.append(args) or True)
    with series_scope(pid, sid):
        first = continuity.identity_reference_path(base, actor, 's', 0)
        assert len(edits) == 1 and len(reviews) == 2
        monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: pytest.fail('Already accepted bytes must not be rejudged'))
        assert continuity.identity_reference_path(base, actor, 's', 0) == first
        assert len(edits) == 1
        actor['reference_requirements'].append('A changed requirement.')
        monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: False)
        with pytest.raises(RuntimeError, match='Identity cleanup failed'):
            continuity.identity_reference_path(base, actor, 's', 0)
        # Отклонённый референс без привязок удаляется с диска, но в каталоге остаётся как история.
        assert not Path(first).exists() and not catalog._read(root)['references'][Path(first).stem]['selectable']
        assert not catalog.reuse_reference(base, 'identity', 'unknown-key')


def test_appearance_resume_reuses_matching_acceptance_and_rechecks_changed_criteria(project, monkeypatch):
    from PIL import Image
    from custom_tools.storybook import character_state_references as states, artist_batch_edit
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    source = root / 'actor.png'
    Image.new('RGB', (16, 16), 'gray').save(source)
    approved = catalog.store_reference(base, source, 'character', 'actor')
    actor = {'name': 'Actor', 'reference_image_path': approved, 'reference_requirements': ['Preserve identity.']}
    (base / '20_bible/characters.json').write_text(json.dumps([actor]))
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start', 'characters': [dict(actor)],
            'reference_image_paths': [approved], '_shot_frame_spec': {'start_state_spec': {'must_show': ['changed']}}}
    (base / '97_shots').mkdir(exist_ok=True)
    (base / '97_shots/shots.json').write_text(json.dumps({'items': [item, dict(item, shot_type='end')]}))
    (base / '91_screenplay').mkdir(exist_ok=True)
    (base / '91_screenplay/screenplay.json').write_text(json.dumps({'screenplay': []}))
    plan = {'characters': [{'name': 'Actor', 'base_frame_keys': [], 'states': [
        {'id': 'changed', 'parent_id': 'base', 'changes': 'a small wound',
         'appearance': 'same actor with a small wound', 'frame_keys': ['1:1:start', '1:1:end']}]}]}
    monkeypatch.setattr(states, 'call_openai_api', lambda **kwargs: json.dumps(plan))
    monkeypatch.setattr(states, 'translate_prompts_in_items', lambda item, language: item)
    edits = []
    def edit(**kwargs):
        edits.append(kwargs)
        Image.new('RGB', (16, 16), 'blue').save(kwargs['output_path'])
    monkeypatch.setattr(states, 'edit_image_vse_tool', edit)
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: True)
    with series_scope(pid, sid):
        first = states.storybook_character_state_references_tool('s', pid)
        path = first['items'][0]['character_reference_states']['Actor']['reference_image_path']
        monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: pytest.fail('Already accepted state must not be rejudged'))
        assert states.storybook_character_state_references_tool('s', pid)['items'][0]['characters'][0]['reference_image_path'] == path
        assert len(edits) == 1
        plan['characters'][0]['states'][0]['changes'] = 'a small wound with a newly required detail'
        (base / '91_screenplay/screenplay.json').write_text(json.dumps({'screenplay': [{'action': 'changed state requirements'}]}))
        monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [r['id'] for r in candidates])
        monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', lambda *args: False)
        with pytest.raises(RuntimeError, match='Character appearance reference failed requirements'):
            states.storybook_character_state_references_tool('s', pid)
        assert len(edits) == 1 and not Path(path).exists()
        assert not catalog._read(root)['references'][Path(path).stem]['selectable']


def test_review_key_uses_stable_model_configuration_not_object_address(tmp_path, monkeypatch):
    import agent_command
    image = tmp_path / 'image.png'
    image.write_bytes(b'image')
    class VisionModel:
        model_ids = ['provider/vision']
    monkeypatch.setattr(agent_command, 'model_vision', VisionModel())
    first = catalog.reference_review_key(image, {'requirements': ['Keep identity.']})
    monkeypatch.setattr(agent_command, 'model_vision', VisionModel())
    assert catalog.reference_review_key(image, {'requirements': ['Keep identity.']}) == first
    changed = VisionModel()
    changed.model_ids = ['provider/other-vision']
    monkeypatch.setattr(agent_command, 'model_vision', changed)
    assert catalog.reference_review_key(image, {'requirements': ['Keep identity.']}) != first


def test_appearance_reuse_keeps_requested_reference_canvas(project, monkeypatch):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    source = root / 'appearance.png'
    source.write_bytes(b'appearance')
    path = catalog.store_reference(base, source, 'appearance', 'old',
        metadata={'entity': 'Actor', 'source_hashes': ['parent'], 'reference_size': [80, 48]})
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [r['id'] for r in candidates])
    assert catalog.reuse_reference(base, 'appearance', 'new-portrait', description='same actor',
        metadata={'entity': 'Actor', 'source_hashes': ['parent'], 'reference_size': [48, 80]}) is None
    assert catalog.reuse_reference(base, 'appearance', 'new-landscape', description='same actor',
        metadata={'entity': 'Actor', 'source_hashes': ['parent'], 'reference_size': [80, 48]}) == path


def test_stored_reference_is_moved_once_into_project_store(project):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    generated = root / '20_bible/scene_character_references/abc/characters/state.png'
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b'state')
    stored = catalog.store_reference(base, generated, 'appearance', 'state')
    assert Path(stored).is_relative_to(root / 'shared/references') and Path(stored).read_bytes() == b'state'
    assert not (root / '20_bible/scene_character_references/abc').exists()
    duplicate = root / '20_bible/references/characters/identity_x.png'
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(b'state')
    assert catalog.store_reference(base, duplicate, 'identity', 'other') == stored
    assert list(root.rglob('*.png')) == [Path(stored)]
    assert catalog.resolve_reference(base, generated) == Path(stored)
    assert catalog.resolve_reference(root, duplicate) == Path(stored)


def test_shared_resource_image_is_registered_in_place(project):
    pid, root = project
    resource(pid, 'characters', 'actor', b'actor')
    path, = catalog.bound_reference_paths(root / 'series' / series(pid, 'One', ['actor']))
    assert Path(path).is_relative_to(root / 'shared/characters/actor')
    assert list(root.rglob('*.png')) == [Path(path)]


def test_rejected_and_replaced_references_leave_disk(project):
    pid, root = project
    a, b = (root / 'series' / series(pid, title) for title in ('One', 'Two'))
    image = root / 'candidate.png'
    image.write_bytes(b'bad')
    bad = catalog.store_reference(a, image, 'identity', 'bad')
    catalog.reuse_reference(b, 'identity', 'bad')
    catalog.reject_reference(a, bad, 'Identity detail missing')
    assert Path(bad).is_file()
    catalog.reject_reference(b, bad, 'Identity detail missing')
    assert not Path(bad).exists()
    image.write_bytes(b'old canon')
    old = catalog.store_reference(a, image, 'character', 'entity:characters:Hero')
    image.write_bytes(b'new canon')
    new = catalog.store_reference(a, image, 'character', 'entity:characters:Hero')
    assert not Path(old).exists() and Path(new).read_bytes() == b'new canon'
    record = catalog._read(root)['references'][Path(old).stem]
    assert record['selectable'] is False


def test_replacing_one_path_keeps_shared_file_for_other_path(project):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    refs = root / '20_bible/references/characters'
    refs.mkdir(parents=True)
    (refs / 'a.png').write_bytes(b'same')
    (refs / 'b.png').write_bytes(b'same')
    old = catalog.store_reference(base, refs / 'a.png', 'character', 'ka')
    assert catalog.store_reference(base, refs / 'b.png', 'character', 'kb') == old
    (refs / 'b.png').write_bytes(b'new')
    new = catalog.store_reference(base, refs / 'b.png', 'character', 'kb')
    assert new != old and Path(old).is_file()
    assert catalog.resolve_reference(base, refs / 'a.png') == Path(old)
    assert catalog.resolve_reference(base, refs / 'b.png') == Path(new)


def test_add_alias_and_existing_reference_path_follow_moved_file(project):
    from custom_tools.storybook.project_paths import existing_reference_path
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    old = root / '20_bible/references/characters/hero.png'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'hero')
    stored = catalog.store_reference(base, old, 'identity', 'hero')
    renamed = root / '20_bible/references/characters/hero_v2.png'
    catalog.add_alias(base, stored, renamed)
    assert catalog.resolve_reference(base, renamed) == Path(stored)
    assert existing_reference_path(base, str(old)) == stored
    assert existing_reference_path(base, stored) == stored


def _moved_reference(root, base, name, kind='character'):
    """Референс, перенесённый в shared; старый путь остался только алиасом."""
    old = root / '20_bible/references/characters' / name
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(name.encode())
    new = catalog.store_reference(base, old, kind, name, description=name)
    assert not old.exists() and Path(new).is_file()
    return old, Path(new)


def test_edit_instruction_resolves_moved_reference_and_background_sheet(project, monkeypatch):
    import utils
    from custom_tools.storybook import artist_batch_edit as artist
    monkeypatch.setattr(utils, 'needs_translation_to_english', lambda text: False)
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    hero, hero_new = _moved_reference(root, base, 'hero.png')
    sheet, sheet_new = _moved_reference(root, base, 'sheet.png', 'location')
    item = {'project_id': pid, 'output_path': str(root / 'shot.png'),
            'reference_image_paths': [str(hero)],
            'background_reference': {'sheet_path': str(sheet), 'card_number': 1}}
    with series_scope(pid, sid):
        _, paths, _, _ = artist._build_edit_instruction('test', item)
    assert paths == [str(hero_new), str(sheet_new)]


def test_preprocess_canon_skips_moved_reference(project, monkeypatch):
    from custom_tools.storybook import artist_batch_edit as artist
    pid, root = project
    sid = series(pid, 'One')
    old, _ = _moved_reference(root, root / 'series' / sid, 'hero.png')
    def forbidden(*args, **kwargs):
        pytest.fail('Moved reference must not be treated as missing')
    monkeypatch.setattr(artist, '_collect_project_references', forbidden)
    items = [{'project_id': pid, 'reference_image_paths': [str(old)],
              'characters': [{'name': 'Hero', 'reference_image_path': '/references/characters/hero.png'}]}] * 2
    with series_scope(pid, sid):
        assert artist._preprocess_canon_references(items, [], 'test', 1) == []


def test_cached_canon_is_aliased_not_copied(project, monkeypatch):
    import hashlib
    from custom_tools.storybook import artist_batch_edit as artist
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    a, b = series(pid, 'One'), series(pid, 'Two')
    reference = '/references/characters/hero.png'
    entity = {'name': 'Hero', 'reference_image_path': reference}
    description = json.dumps(entity, ensure_ascii=False, sort_keys=True)
    source = root / 'unrelated.png'
    source.write_bytes(b'cached hero')
    canonical = catalog.store_reference(root / 'series' / a, source, 'character',
                                        'canon:' + hashlib.sha256(description.encode()).hexdigest(),
                                        description=description, metadata={'entity': 'Hero'})
    (root / 'series' / b / '20_bible/characters.json').write_text(json.dumps([entity]))
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [r['id'] for r in candidates])
    def forbidden(*args, **kwargs):
        pytest.fail('Cached canon must not generate again')
    monkeypatch.setattr(artist, '_generate_image_from_scratch', forbidden)
    monkeypatch.setattr(artist, '_generate_canon_reference_from_scratch', forbidden)
    monkeypatch.setattr(artist.shutil, 'copyfile', forbidden)
    with series_scope(pid, b):
        artist._ensure_references_exist('test', pid, [{'reference_image_paths': [reference]}], [], 'workflow')
        assert str(_reference_path(root / 'series' / b, reference)) == canonical
    assert not list(root.rglob('hero.png'))


def test_store_reference_copies_file_from_outside_project(project, tmp_path_factory):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    outside = tmp_path_factory.mktemp('outside') / 'sub' / 'ext.png'
    outside.parent.mkdir()
    outside.write_bytes(b'external')
    stored = Path(catalog.store_reference(base, outside, 'identity', 'ext'))
    assert stored.read_bytes() == b'external' and stored.is_relative_to(root / 'shared/references')
    assert outside.is_file()
    # Дубль вне проекта тоже остаётся на месте.
    twin = outside.parent / 'twin.png'
    twin.write_bytes(b'external')
    assert catalog.store_reference(base, twin, 'identity', 'ext2') == str(stored)
    assert twin.is_file()


def test_disabled_catalog_does_not_move_or_delete_files(tmp_path):
    plain = tmp_path / 'plain'
    image = plain / '20_bible/references/characters/a.png'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'a')
    assert catalog.store_reference(plain, image, 'identity', 'a') == str(image.resolve())
    catalog.reject_reference(plain, image, 'bad')
    assert image.is_file()


def test_reject_by_alias_of_old_record_hits_new_candidate_not_old_record(project):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    x = root / '20_bible/references/characters/x.png'
    x.parent.mkdir(parents=True)
    x.write_bytes(b'old')
    old = Path(catalog.store_reference(base, x, 'identity', 'old'))
    x.write_bytes(b'new candidate')
    catalog.reject_reference(base, x, 'bad')
    records = catalog._read(root)['references']
    assert records[old.stem]['selectable'] is True and old.is_file()
    assert x.is_file()
    # Кандидат ещё не в каталоге: тоже не трогаем старую запись.
    x.write_bytes(b'unregistered')
    catalog.reject_reference(base, x, 'bad')
    assert catalog._read(root)['references'][old.stem]['selectable'] is True


def test_reuse_reference_ignores_record_whose_file_vanished_under_lock(project, monkeypatch):
    pid, root = project
    a, b = (root / 'series' / series(pid, t) for t in ('One', 'Two'))
    image = root / 'img.png'
    image.write_bytes(b'img')
    stored = Path(catalog.store_reference(a, image, 'identity', 'k'))
    original = catalog._write
    @catalog.contextlib.contextmanager
    def racing(base):
        stored.unlink(missing_ok=True)
        with original(base) as data:
            yield data
    monkeypatch.setattr(catalog, '_write', racing)
    assert catalog.reuse_reference(b, 'identity', 'k') is None
    assert catalog._read(root)['references'][stored.stem]['series_ids'] == [a.name]


def test_shared_resource_reference_survives_reject(project):
    pid, root = project
    resource(pid, 'characters', 'actor', b'actor')
    base = root / 'series' / series(pid, 'One', ['actor'])
    path, = catalog.bound_reference_paths(base)
    catalog.reject_reference(base, path, 'bad')
    assert Path(path).is_file()


def test_file_kept_while_another_selectable_record_uses_same_path(project):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    image = root / 'i.png'
    image.write_bytes(b'shared path')
    path = Path(catalog.store_reference(base, image, 'identity', 'one'))
    records = catalog._read(root)
    twin = dict(records['references'][path.stem], id='twin', keys=['two'], aliases=[], series_ids=[])
    records['references']['twin'] = twin
    catalog._catalog_path(root).write_text(json.dumps(records))
    catalog.reject_reference(base, path, 'bad')
    assert path.is_file()


def _two_records(root, sid):
    """Три записи с общим alias: отклонённая, выбираемая без серии и выбираемая в серии."""
    def rec(ident, selectable, series_ids):
        (root / 'shared/references').mkdir(parents=True, exist_ok=True)
        (root / f'shared/references/{ident}.png').write_bytes(ident.encode())
        return {'id': ident, 'path': f'shared/references/{ident}.png', 'kinds': [], 'keys': [], 'aliases': ['old.png'],
                'description': '', 'metadata': {}, 'parents': [], 'series_ids': series_ids, 'selectable': selectable}
    data = {'version': 1, 'initialized_series': [], 'references': {
        'bound': rec('bound', True, [sid]), 'free': rec('free', True, []), 'dead': rec('dead', False, [])}}
    catalog._catalog_path(root).write_text(json.dumps(data))
    return data


def test_alias_collision_prefers_selectable_bound_to_series(project):
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    data = _two_records(root, sid)
    old = root / 'old.png'
    assert catalog._record_for_path(root, data, old, sid)['id'] == 'bound'
    assert catalog._record_for_path(root, data, old)['id'] == 'bound'
    del data['references']['bound']
    assert catalog._record_for_path(root, data, old, sid)['id'] == 'free'
    del data['references']['free']
    assert catalog._record_for_path(root, data, old, sid)['id'] == 'dead'
    data = _two_records(root, sid)
    assert catalog.resolve_reference(base, old) == root / 'shared/references/bound.png'
    # Без серии берётся выбираемая запись.
    assert catalog.resolve_reference(root, old) == root / 'shared/references/bound.png'


def test_reference_path_finds_moved_legacy_file_with_and_without_series(project):
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    series_base = root / 'series' / series(pid, 'One')
    for base, name in ((series_base, 'with_series.png'), (root, 'no_series.png')):
        old, new = _moved_reference(root, base, name)
        assert Path(_reference_path(base, '/references/characters/' + name)) == new


def test_prune_empty_keeps_reference_boundaries(tmp_path):
    root = tmp_path
    for deep in ('references/characters/a/b', 'scene_character_references/x/y', 'other/c/d'):
        (root / deep).mkdir(parents=True)
    catalog._prune_empty(root / 'references/characters/a/b', root)
    catalog._prune_empty(root / 'scene_character_references/x/y', root)
    catalog._prune_empty(root / 'other/c/d', root)
    assert (root / 'references/characters').is_dir() and not (root / 'references/characters/a').exists()
    assert (root / 'scene_character_references').is_dir() and not (root / 'scene_character_references/x').exists()
    assert root.is_dir() and not (root / 'other').exists()


def test_moved_absolute_reference_is_not_regenerated(project, monkeypatch):
    from custom_tools.storybook import artist_batch_edit as artist
    pid, root = project
    sid = series(pid, 'One')
    old, _ = _moved_reference(root, root / 'series' / sid, 'hero.png')
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [])
    def forbidden(*args, **kwargs):
        pytest.fail('Moved reference must not be generated again')
    monkeypatch.setattr(artist, '_generate_image_from_scratch', forbidden)
    monkeypatch.setattr(artist, '_generate_canon_reference_from_scratch', forbidden)
    monkeypatch.setattr(artist, '_create_canon_reference', forbidden)
    with series_scope(pid, sid):
        artist._ensure_references_exist('test', pid, [{'reference_image_paths': [str(old)],
            'characters': [{'name': 'Hero', 'reference_image_path': '/references/characters/hero.png'}]}], [], 'workflow')


def test_existing_reference_path_returns_empty_value_as_is(project):
    from custom_tools.storybook.project_paths import existing_reference_path
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    assert existing_reference_path(base, '') == ''


def test_replacing_canon_keeps_old_file_for_series_still_bound_to_it(project):
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    a_base, b_base = (root / 'series' / series(pid, name) for name in ('A', 'B'))
    old = root / '20_bible/references/characters/hero.png'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'v1')
    v1 = Path(catalog.store_reference(a_base, old, 'identity', 'hero'))
    catalog.store_reference(b_base, v1, 'identity', 'hero')
    old.write_bytes(b'v2')
    v2 = Path(catalog.store_reference(a_base, old, 'identity', 'hero'))
    assert v2 != v1 and v1.is_file()
    assert catalog.resolve_reference(b_base, old) == v1
    assert Path(_reference_path(b_base, '/references/characters/hero.png')) == v1
    assert catalog.resolve_reference(a_base, old) == v2


def test_bootstrap_of_two_series_with_same_relative_path_keeps_both_references(project):
    pid, root = project
    bases = [root / 'series' / series(pid, name) for name in ('A', 'B')]
    for base, content in zip(bases, (b'from a', b'from b')):
        image = base / '20_bible/references/characters/hero.png'
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(content)
    with catalog._write(root) as data:
        data['initialized_series'] = []
    for base in bases:
        catalog.bootstrap_series_references(base)
    records = catalog._read(root)['references']
    for base, content in zip(bases, (b'from a', b'from b')):
        digest = __import__('hashlib').sha256(content).hexdigest()
        assert records[digest]['selectable'] and records[digest]['series_ids'] == [base.name]
        assert [Path(p).read_bytes() for p in catalog.bound_reference_paths(base)] == [content]


def test_replacing_entity_key_moves_service_alias_to_new_file(project):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    refs = root / '20_bible/references/characters'
    refs.mkdir(parents=True)
    (refs / 'a.png').write_bytes(b'old')
    (refs / 'a2.png').write_bytes(b'old')
    old = catalog.store_reference(base, refs / 'a.png', 'character', 'entity:characters:H')
    assert catalog.store_reference(base, refs / 'a2.png', 'character', 'migrated:a') == old
    (refs / 'n.png').write_bytes(b'new')
    new = catalog.store_reference(base, refs / 'n.png', 'character', 'entity:characters:H')
    assert new != old and not Path(old).exists()
    assert catalog.bound_reference_paths(base) == [new]
    assert catalog.resolve_reference(base, refs / 'a2.png') == Path(new)


def test_regenerating_portrait_by_entity_key_replaces_file_under_both_paths(project):
    from custom_tools.storybook.background_references import _reference_path
    pid, root = project
    sid = series(pid, 'One')
    base = root / 'series' / sid
    plain = root / '20_bible/references/characters/x.png'
    scoped = root / f'20_bible/references/_series/{sid}/characters/x.png'
    for path in (plain, scoped):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'old')
    old = catalog.store_reference(base, plain, 'character', 'entity:characters:X')
    catalog.store_reference(base, scoped, 'character', f'migrated:20_bible/references/_series/{sid}/characters/x.png')
    plain.write_bytes(b'new')
    new = catalog.store_reference(base, plain, 'character', 'entity:characters:X')
    assert new != old and not Path(old).exists()
    assert Path(_reference_path(base, '/references/characters/x.png')).read_bytes() == b'new'
    assert catalog.bound_reference_paths(base) == [new]


def test_replacing_one_key_by_alias_path_keeps_old_file_for_other_key(project, monkeypatch):
    pid, root = project
    base = root / 'series' / series(pid, 'One')
    path = root / '20_bible/references/characters/a.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'old')
    old = catalog.store_reference(base, path, 'character', 'K')
    monkeypatch.setattr(catalog, '_choose', lambda source, candidates, metadata=None: [candidates[0]['id']])
    assert catalog.reuse_reference(base, 'character', 'K2', description='d') == old
    path.write_bytes(b'new')
    new = catalog.store_reference(base, path, 'character', 'K')
    assert new != old and Path(old).is_file()
    assert catalog.reuse_reference(base, 'character', 'K2') == old
    assert catalog.resolve_reference(base, path) == Path(new)


def test_replacing_service_only_record_keeps_old_alias_for_other_series(project):
    pid, root = project
    a_base, b_base = (root / 'series' / series(pid, name) for name in ('A', 'B'))
    path = root / '20_bible/references/characters/h.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'v1')
    v1 = Path(catalog.store_reference(a_base, path, 'character', 'migrated:h'))
    catalog.store_reference(b_base, v1, 'character', 'migrated:h')
    path.write_bytes(b'v2')
    v2 = Path(catalog.store_reference(a_base, path, 'character', 'entity:characters:H'))
    assert v2 != v1 and v1.is_file()
    assert catalog.resolve_reference(b_base, path) == v1
    assert catalog.resolve_reference(a_base, path) == v2
