"""Project reference images and their explicit series associations."""
import contextlib
import fcntl
import hashlib
import json
import shutil
from pathlib import Path

from .project_paths import current_series_scope, reference_project_dir

_REFERENCE_REVIEW_VERSION = 1


def reference_review_key(path, review):
    from agent_command import model_vision
    payload = [_REFERENCE_REVIEW_VERSION, model_vision.model_ids, review, hashlib.sha256(Path(path).read_bytes()).hexdigest()]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def reference_review_current(base, path, review):
    record = _record_for_path(_root(base), _read(base), path)
    return bool(record and record.get('selectable') and reference_review_key(path, review)
                in record['metadata'].get('requirements_reviews', []))


def accept_reference_review(base, path, review):
    if not _enabled(base):
        return
    fingerprint = reference_review_key(path, review)
    with _write(base) as catalog:
        record = _record_for_path(_root(base), catalog, path, _sid(base))
        if not record or not record.get('selectable'):
            raise ValueError('Cannot accept a missing or rejected reference')
        fingerprints = record['metadata'].setdefault('requirements_reviews', [])
        if fingerprint not in fingerprints:
            fingerprints.append(fingerprint)


def _root(base):
    return reference_project_dir(Path(base)).resolve()


def _enabled(base):
    path = _root(base) / "project.json"
    return path.is_file() and json.loads(path.read_text()).get("format") == "storybook-series-v1"


def _sid(base):
    base = Path(base).resolve()
    if base.parent.name == "series":
        return base.name
    scope = current_series_scope()
    return scope[1] if scope and scope[0] == _root(base).name else None


def _catalog_path(base):
    return _root(base) / "shared/reference_catalog.json"


def _read(base):
    path = _catalog_path(base)
    return json.loads(path.read_text()) if path.is_file() else {"version": 1, "references": {}, "initialized_series": []}


@contextlib.contextmanager
def _write(base):
    from .studio.project_store import write_json_atomic
    path = _catalog_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(base)
        yield data
        write_json_atomic(path, data)


def _alias(root, path):
    path = Path(path).resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def _prune_empty(folder, root):
    """Удаляет опустевшие папки до папки references, её категорий и корня проекта."""
    while (folder != root and folder.is_relative_to(root) and folder.parent.name != 'references'
           and folder.name not in ('references', 'scene_character_references')):
        try:
            folder.rmdir()
        except OSError:
            break
        folder = folder.parent


def _drop_file(root, catalog, record):
    """Удаляет файл отклонённой или заменённой записи, если он больше никому не нужен."""
    path = root / record['path']
    if (record.get('selectable') or record['series_ids'] or not path.resolve().is_relative_to(root / 'shared/references')
            or any(r.get('selectable') and r['path'] == record['path'] for r in catalog['references'].values())):
        return
    path.unlink(missing_ok=True)
    _prune_empty(path.parent, root)


def _record_for_path(root, catalog, path, sid=None):
    alias = _alias(root, path)
    matches = [r for r in catalog['references'].values()
               if alias == r['path'] or alias in r.get('aliases', [])]
    if matches:
        # Привязанная к серии запись важнее выбираемой: серия могла остаться на прежнем каноне.
        return (next((r for r in matches if r.get('selectable') and sid in r['series_ids']), None)
                or next((r for r in matches if sid in r['series_ids']), None)
                or next((r for r in matches if r.get('selectable')), matches[-1]))
    path = Path(path)
    if path.is_file():
        return catalog['references'].get(hashlib.sha256(path.read_bytes()).hexdigest())
    return None


def _bind(catalog, record, sid):
    if not sid:
        return
    if sid in record['series_ids']:
        return
    record['series_ids'].append(sid)
    for parent in record.get('parents', []):
        if parent in catalog['references']:
            _bind(catalog, catalog['references'][parent], sid)



def _unbind_with_derivatives(catalog, ids, sid):
    invalid = set(ids)
    while True:
        expanded = invalid | {r['id'] for r in catalog['references'].values()
                              if invalid.intersection(r.get('parents', []))}
        if expanded == invalid:
            break
        invalid = expanded
    for ident in invalid:
        record = catalog['references'][ident]
        if sid in record['series_ids']:
            record['series_ids'].remove(sid)
    return invalid


def store_reference(base, path, kind, key, *, description='', metadata=None, sources=(), series_id=None):
    path = Path(path).resolve()
    if not _enabled(base):
        return str(path)
    root, sid = _root(base), series_id or _sid(base)
    shared = root / 'shared'
    in_place = path.is_relative_to(shared) and not path.is_relative_to(shared / 'references')
    outside = not path.is_relative_to(root)
    category = 'characters' if kind in ('character', 'identity', 'appearance') else kind + 's'
    with _write(base) as catalog:
        if not path.is_file():
            # Файл уже перенесён параллельным вызовом.
            moved = _record_for_path(root, catalog, path, sid)
            if moved:
                return str(root / moved['path'])
            raise FileNotFoundError(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record = catalog['references'].get(digest)
        live = bool(record and (root / record['path']).is_file())
        if live:
            target = (root / record['path']).resolve()
        elif in_place:
            target = path
        else:
            target = root / 'shared/references' / category / (digest + path.suffix.lower())
        if not in_place and target != path:
            if live:
                if not outside:
                    path.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Файл вне проекта не трогаем: копируем, исходник и его папки остаются.
                (shutil.copyfile if outside else shutil.move)(path, target)
            if not outside:
                _prune_empty(path.parent, root)
        alias = _alias(root, path)
        if record is None:
            record = catalog['references'][digest] = {
                'id': digest, 'path': _alias(root, target), 'kinds': [], 'keys': [], 'aliases': [],
                'description': description, 'metadata': {}, 'parents': [], 'series_ids': [], 'selectable': True,
            }
        elif not live:
            record['path'] = _alias(root, target)
        for other in catalog['references'].values():
            if other['id'] != digest and (str(key) in other['keys'] or alias in other['aliases']):
                keys = [k for k in other['keys'] if k != str(key)]
                if any(not (k.startswith('migrated:') or k in other['aliases']) for k in keys):
                    # общий файл: остался настоящий ключ, снимаем только совпавшие, файл не трогаем
                    other['keys'] = keys
                    other['aliases'] = [a for a in other['aliases'] if a != alias]
                    continue
                # остальные пути старой записи переходят новой; у старой остаются для серий, ещё привязанных к ней
                record['aliases'].extend(a for a in dict.fromkeys(other['aliases'])
                                         if a not in (alias, record['path']) and a not in record['aliases'])
                other['selectable'] = False
                _unbind_with_derivatives(catalog, [other['id']], sid)
                _drop_file(root, catalog, other)
        for field, value in [('kinds', kind), ('keys', str(key)), ('aliases', alias)]:
            if value not in record[field] and not (field == 'aliases' and value == record['path']):
                record[field].append(value)
        record['metadata'].update(metadata or {})
        if description:
            record['description'] = description
        for source in sources:
            parent = _record_for_path(root, catalog, source, sid)
            if parent and parent['id'] != digest and parent['id'] not in record['parents']:
                record['parents'].append(parent['id'])
        record['selectable'] = True
        record['metadata'].pop('rejection_reason', None)
        _bind(catalog, record, sid)
    return str(root / record['path'])


def add_alias(base, path, old_path):
    """Дописывает старый путь как alias записи, чей файл лежит по path."""
    if not _enabled(base):
        return
    root = _root(base)
    target = _alias(root, path)
    with _write(base) as catalog:
        record = next((r for r in catalog['references'].values() if r['path'] == target), None)
        alias = _alias(root, old_path)
        if record and alias != record['path'] and alias not in record['aliases']:
            record['aliases'].append(alias)


def _choose(source, candidates, metadata=None):
    if not candidates or not source:
        return []
    from agent_command import model_hard
    from utils import call_openai_api, parse_llm_json
    result = parse_llm_json(call_openai_api(
        prompt=json.dumps({'request': source, 'identity': metadata or {}, 'references': [
            {k: r[k] for k in ('id', 'kinds', 'description', 'metadata')} for r in candidates]}, ensure_ascii=False),
        system_prompt=(
            'Select existing project reference images required by this exact request. Reuse only the SAME '
            'entity, appearance, spatial state and project style, not merely a visually similar one. '
            'Do not select unrelated references or obsolete variants. If uncertain or a new reference is '
            'needed, leave it unselected. Return JSON {"reference_ids": [existing IDs]}. Do not generate images.'
        ), model=model_hard, response_format={'type': 'json_object'}, max_tokens=8192, temperature=0.1,
    ))
    selected = result.get('reference_ids')
    valid = {r['id'] for r in candidates}
    if not isinstance(selected, list) or any(r not in valid for r in selected):
        raise ValueError('Reference selection returned an unknown reference')
    return list(dict.fromkeys(selected))


def reuse_reference(base, kind, key, *, description='', metadata=None):
    if not _enabled(base):
        return None
    catalog, root = _read(base), _root(base)
    candidates = [r for r in catalog['references'].values()
                  if kind in r['kinds'] and r.get('selectable') and (root / r['path']).is_file()]
    record = next((r for r in candidates if str(key) in r['keys']), None)
    if record is None and description:
        candidates = [r for r in candidates if r.get('selectable') and all(
            r['metadata'].get(k) == v for k, v in (metadata or {}).items() if k in ('entity', 'style_id', 'source_hashes', 'reference_size'))]
        chosen = _choose(description, candidates, metadata)
        record = next((r for r in candidates if r['id'] in chosen), None)
    if record is None:
        return None
    with _write(base) as latest:
        record = latest['references'][record['id']]
        if not (record.get('selectable') and (root / record['path']).is_file()):
            return None
        if str(key) not in record['keys']:
            record['keys'].append(str(key))
        _bind(latest, record, _sid(base))
    return str(root / record['path'])


def bound_references(base, kinds=None):
    if not _enabled(base):
        return []
    sid, root = _sid(base), _root(base)
    return [r for r in _read(base)['references'].values()
            if (sid is None or sid in r['series_ids'])
            and (not kinds or set(kinds) & set(r['kinds'])) and (root / r['path']).is_file()]


def bound_reference_paths(base, kinds=None):
    root = _root(base)
    return [str(root / r['path']) for r in bound_references(base, kinds)]


def resolve_reference(base, path):
    """Resolve a managed reference, rejecting a reference outside this series."""
    path = Path(path)
    if not _enabled(base):
        return path
    sid = _sid(base)
    path = path.resolve()
    record = _record_for_path(_root(base), _read(base), path, sid)
    if record:
        if sid and sid not in record['series_ids']:
            raise ValueError(f'Reference is not bound to series {sid}: {path}')
        if sid or record.get('selectable'):
            return _root(base) / record['path']
    if not sid:
        return path
    scoped = _root(base) / 'series' / sid
    if path.is_relative_to(scoped) and not any(p in path.parts for p in ('references', 'scene_character_references')):
        return path
    if path.is_file():
        raise ValueError(f'Unregistered reference for series {sid}: {path}')
    return path


def _shared_reference(root, kind, entity_id, version, sid=None):
    source = root / 'shared' / kind / entity_id / version / 'data.json'
    data = json.loads(source.read_text())
    image = source.parent / str(data.get('reference_image_path') or '')
    if not data.get('reference_image_path') or not image.is_file():
        return None
    return store_reference(root, image, kind.rstrip('s'), f'resource:{kind}:{entity_id}:{version}',
        description=json.dumps({k: v for k, v in data.items() if k not in ('reference_image_path', 'voice')}, ensure_ascii=False),
        metadata={'resource_kind': kind, 'entity_id': entity_id, 'version_id': version,
                  'entity': data.get('name', entity_id)}, series_id=sid)


def sync_snapshot_references(base):
    if not _enabled(base) or not _sid(base):
        return
    root, sid = _root(base), _sid(base)
    path = root / 'series' / sid / 'series.json'
    if not path.is_file():
        return
    for kind, entries in json.loads(path.read_text()).get('snapshot', {}).items():
        for entity_id, version in entries.items():
            with _write(base) as catalog:
                for record in catalog['references'].values():
                    meta = record['metadata']
                    if (meta.get('resource_kind') == kind and meta.get('entity_id') == entity_id
                            and meta.get('version_id') != version and sid in record['series_ids']):
                        _unbind_with_derivatives(catalog, [record['id']], sid)
            _shared_reference(root, kind, entity_id, version, sid)


def bootstrap_series_references(base):
    if not _enabled(base) or not _sid(base):
        return
    root, sid = _root(base), _sid(base)
    if sid in _read(base)['initialized_series']:
        return
    sync_snapshot_references(base)
    folder = root / 'series' / sid
    for kind in ('characters', 'locations'):
        bible = folder / '20_bible' / (kind + '.json')
        for entry in json.loads(bible.read_text()) if bible.is_file() else []:
            value = str(entry.get('reference_image_path') or '')
            if not value:
                continue
            path = root / '20_bible' / value.lstrip('/') if value.lstrip('/').startswith('references/') else Path(value)
            if not path.is_file() and value.lstrip('/').startswith('references/'):
                path = folder / '20_bible' / value.lstrip('/')
            if path.is_file():
                store_reference(base, path, kind.rstrip('s'), f'entity:{kind}:{entry.get("name")}',
                                description=json.dumps(entry, ensure_ascii=False), metadata={'entity': entry.get('name')})
    for directory in ('20_bible/references', '20_bible/scene_character_references', '30_style/references'):
        for image in sorted((folder / directory).rglob('*')) if (folder / directory).is_dir() else []:
            if image.is_file() and image.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                store_reference(base, image, 'reference', _alias(root, image),
                                description=str(image.relative_to(folder)))
    # Migrate only references actually named by this series' current manifests.
    def manifest_paths(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from manifest_paths(child)
        elif isinstance(value, list):
            for child in value:
                yield from manifest_paths(child)
        elif isinstance(value, str) and Path(value).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
            yield value

    for manifest in ('20_bible/continuity_plan.json', '97_shots/character_reference_states.json',
                     '20_bible/references/background_references.json', '30_style/image_styles.json'):
        source = folder / manifest
        if not source.is_file():
            continue
        for value in manifest_paths(json.loads(source.read_text())):
            path = Path(value)
            if value.lstrip('/').startswith('references/'):
                path = root / '20_bible' / value.lstrip('/')
            elif not path.is_absolute():
                path = folder / path
            path = path.resolve()
            if (path.is_file() and path.is_relative_to(root)
                    and any(part in path.parts for part in ('references', 'scene_character_references'))):
                kind = next((kind for segment, kind in (
                    ('scene_character_references', 'appearance'), ('characters', 'identity'),
                    ('locations', 'location'), ('landmarks', 'landmark'), ('background_references', 'background'),
                    ('30_style', 'style')) if segment in path.parts), 'reference')
                store_reference(base, path, kind, f'migrated:{_alias(root, path)}',
                                description=f'{manifest}: {path.name}')
    with _write(base) as catalog:
        if sid not in catalog['initialized_series']:
            catalog['initialized_series'].append(sid)


def initialize_series_references(root, sid, source_material, participant_ids, snapshot):
    """Analyse current project references before writing a new series snapshot."""
    if not _enabled(root):
        return snapshot
    root = _root(root)
    for existing in (root / 'series').iterdir():
        if (existing / 'series.json').is_file():
            bootstrap_series_references(existing)
    project = json.loads((root / 'project.json').read_text())
    for kind, entries in project.get('resources', {}).items():
        for entity_id, version in entries.items():
            _shared_reference(_root(root), kind, entity_id, version)
    catalog = _read(root)
    automatic, candidates = [], []
    for record in catalog['references'].values():
        if not record.get('selectable'):
            continue
        ancestors, visited = [record['id']], set()
        obsolete = False
        while ancestors:
            ident = ancestors.pop()
            if ident in visited:
                continue
            visited.add(ident)
            ancestor = catalog['references'][ident]
            meta = ancestor['metadata']
            kind = meta.get('resource_kind')
            if not ancestor.get('selectable', True):
                obsolete = True
                break
            if kind and project.get('resources', {}).get(kind, {}).get(meta.get('entity_id')) != meta.get('version_id'):
                obsolete = True
                break
            ancestors.extend(ancestor.get('parents', []))
        if obsolete:
            continue
        meta = record['metadata']
        resource_kind = meta.get('resource_kind')
        if resource_kind == 'characters':
            if meta.get('entity_id') in participant_ids:
                automatic.append(record['id'])
            continue
        candidates.append(record)
    selected = set(automatic + _choose(source_material, candidates, {'participant_ids': participant_ids}))
    with _write(root) as latest:
        for ident in selected:
            _bind(latest, latest['references'][ident], sid)
        if sid not in latest['initialized_series']:
            latest['initialized_series'].append(sid)
    for kind in tuple(snapshot):
        if kind in ('characters', 'style', 'narrators'):
            continue
        for entity_id, version in list(snapshot[kind].items()):
            matches = [r for r in catalog['references'].values()
                       if r['metadata'].get('resource_kind') == kind and r['metadata'].get('entity_id') == entity_id]
            if matches and not any(r['id'] in selected for r in matches):
                del snapshot[kind][entity_id]
    return snapshot


def unbind_series(root, sid):
    if not _enabled(root):
        return
    with _write(root) as catalog:
        for record in catalog['references'].values():
            if sid in record['series_ids']:
                record['series_ids'].remove(sid)
        if sid in catalog['initialized_series']:
            catalog['initialized_series'].remove(sid)


def register_canon_reference(path, kind, data):
    path = Path(path).resolve()
    bible = next((parent for parent in path.parents if parent.name == '20_bible'), None)
    if bible is None:
        return str(path)
    return store_reference(bible.parent, path, kind, f'entity:{kind}s:{data.get("name")}',
                           description=json.dumps(data, ensure_ascii=False),
                           metadata={'entity': data.get('name')})


def reject_reference(base, path, reason):
    """Keep the rejected asset as history, excluding it from reuse and this series."""
    if not _enabled(base):
        return
    with _write(base) as catalog:
        root, path = _root(base), Path(path)
        # Существующий файл ищем по содержимому: по пути мог лечь новый кандидат.
        record = (catalog['references'].get(hashlib.sha256(path.read_bytes()).hexdigest()) if path.is_file()
                  else _record_for_path(root, catalog, path, _sid(base)))
        if record:
            rejected = _unbind_with_derivatives(catalog, [record['id']], _sid(base))
            for ident in rejected:
                catalog['references'][ident]['selectable'] = False
                catalog['references'][ident]['metadata']['rejection_reason'] = reason
                _drop_file(_root(base), catalog, catalog['references'][ident])
