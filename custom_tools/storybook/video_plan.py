"""Choose video generation per continuous fragment; keep image storyboards intact."""
import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .project_paths import safe_storybook_project_dir
from .video_generator_common import parse_duration_seconds_from_timing


def _key(item):
    return f"{item['scene_number']}-{item['shot_number']}"


def _duration(item):
    return float(item.get('duration_s') or parse_duration_seconds_from_timing(item.get('timing', '5s')))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _starts(items):
    return [i for i in items if i.get('shot_type', 'start') == 'start']


def _fingerprint(items, language='ru', catalog=None, seed=None, base=None):
    from .video_contract import _active_video_tool_name
    from .style_keeper import _STYLE_REFERENCE_INSTRUCTION
    from .background_references import _reference_path
    prefix = 'AITUNNEL' if _active_video_tool_name() == 'video_generator_aitunnel_tool' else 'ROUTERAI'
    assets = {str(_reference_path(base, entry['reference_image_path']))
              for item in items for field in ('characters', 'locations') for entry in item.get(field) or []
              if base is not None and entry.get('reference_image_path')}
    return _digest({'items': [_planning_shot(i) for i in _starts(items)], 'language': language, 'reference_prompt_version': 8,
                    'styles': {key: style['prompt'] for key, style in (catalog or {}).get('styles', {}).items()},
                    'style_image_model': os.getenv('IMG2IMG_MODEL') or os.getenv('VSEGPT_IMG2IMG_MODEL'), 'seed': seed,
                    'style_reference_instruction': _STYLE_REFERENCE_INSTRUCTION,
                    'style_source_frames': [(_key(item), item.get('shot_type'), item.get('style_id'),
                                             item.get('characters'), item.get('locations')) for item in items],
                    'style_source_images': {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                            if Path(path).is_file() else None for path in assets},
                    'model': os.getenv(f'{prefix}_VIDEO_MODEL'),
                    'provider_tool': _active_video_tool_name(),
                    'resolution': os.getenv(f'{prefix}_RESOLUTION')})


def apply_video_plan(project_id, items):
    from .continuity import bind_continuity_to_items, unit_continuity_hash
    base = safe_storybook_project_dir(project_id)
    items = bind_continuity_to_items(base, items)
    path = safe_storybook_project_dir(project_id) / '97_shots/video_plan.json'
    if not path.is_file():
        return items
    units = json.loads(path.read_text()).get('units', [])
    starts = {_key(i): i for i in _starts(items)}
    order = list(starts)
    replacements, covered = {}, set()
    for unit in units:
        keys = unit['shot_keys']
        expected_continuity = unit_continuity_hash(base, keys)
        if expected_continuity is not None and unit.get('continuity_hash') != expected_continuity:
            raise ValueError('Video unit continuity is stale')
        positions = [order.index(k) for k in keys]
        if positions != list(range(positions[0], positions[0] + len(keys))):
            raise ValueError('Video unit must cover contiguous shots')
        if covered.intersection(keys) or len({starts[k]['scene_number'] for k in keys}) != 1:
            raise ValueError('Video units must not overlap or cross scenes')
        duration = sum(_duration(starts[k]) for k in keys)
        if abs(duration - unit['duration_s']) > 0.001:
            raise ValueError('Video unit duration must preserve source timing')
        item = dict(starts[keys[0]], video_mode='references', duration_s=duration,
                    timing=f'{duration:g}s', video_path=unit['video_path'],
                    video_prompt=unit['video_prompt'], reference_image_paths=unit['reference_image_paths'],
                    video_seed=unit.get('seed'), video_resolution=unit['resolution'],
                    video_model=unit.get('model'),
                    start_reference_path=unit.get('start_reference_path'),
                    end_reference_path=unit.get('end_reference_path'),
                    covered_shot_keys=keys, start_image=None, end_image=None,
                    voiceover='\n'.join(str(starts[k].get('voiceover') or '').strip()
                                        for k in keys if starts[k].get('voiceover')))
        replacements[keys[0]] = item
        covered.update(keys)
    result = []
    for item in items:
        key = _key(item)
        if key not in covered:
            result.append(item)
        elif key in replacements and item.get('shot_type', 'start') == 'start':
            result.append(replacements[key])
    return result


def reference_request(item, model, seed, provider_name='routerai'):
    import base64
    import io
    from PIL import Image
    from .video_generator_aitunnel_media import _infer_aspect_ratio
    model = item.get('video_model') or model
    paths = item['reference_image_paths']
    hashes = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths]
    payload = {'model': model, 'prompt': item['video_prompt'],
               'duration': int(round(_duration(item))),
               'resolution': item.get('video_resolution') or os.getenv(f'{provider_name.upper()}_RESOLUTION', '480p'),
               'aspect_ratio': _infer_aspect_ratio(int(item.get('width') or 1920), int(item.get('height') or 1088)),
               'generate_audio': False}
    selected_seed = item['video_seed'] if 'video_seed' in item else seed
    if selected_seed is not None:
        payload['seed'] = selected_seed
    identity = dict(payload, mode='references', image_hashes=hashes)
    images = []
    for path in paths:
        if provider_name == 'routerai':
            with Image.open(path) as image:
                output = io.BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=85)
                data = output.getvalue()
            mime = 'image/jpeg'
        else:
            data = Path(path).read_bytes()
            mime = 'image/png'
        images.append({'type': 'image_url', 'image_url': {
            'url': f'data:{mime};base64,' + base64.b64encode(data).decode()}})
    payload['input_references'] = images
    return payload, identity


def reference_input_hash(item, model, seed, provider_name='routerai'):
    return _digest(reference_request(item, model, seed, provider_name)[1])


def trusted_reference_output(item, store, model, seed):
    if not store or not Path(item['video_path']).is_file():
        return False
    job = store.find_current_job(_key(item), reference_input_hash(item, model, seed, store.provider_name))
    return bool(job and job.get('status') == 'downloaded')


def generate_reference_video(item, api_key, model, seed, store, model_catalog,
                             deadline=None, started_at=None, remaining=1, workers=1, provider_name='routerai'):
    from .video_generator_aitunnel_jobs import _new_provider_job
    from .video_generator_aitunnel_tool import (
        _wait_for_video_completion_aitunnel, _download_video_aitunnel,
        _record_poll_update, _require_budget_for_new_video_submission,
    )
    model = item.get('video_model') or model
    config = model_catalog[model]
    if not config.get('supports_image_references'):
        raise ValueError('The selected model does not support reference images')
    payload, identity = reference_request(item, model, seed, provider_name)
    if payload['duration'] not in config['supported_durations']:
        raise ValueError('Reference fragment duration is unsupported')
    if config.get('max_image_references') is not None and len(item['reference_image_paths']) > config['max_image_references']:
        raise ValueError('Too many reference images')
    key, input_hash = _key(item), _digest(identity)
    output = item['video_path']
    result = dict(success=True, video_path=output, scene_number=item['scene_number'],
                  shot_number=item['shot_number'], model=model, error=None)
    if trusted_reference_output(item, store, model, seed):
        return result
    job = store.find_current_job(key, input_hash)
    if not job or job.get('status') == 'failed':
        job = store.ensure_job(_new_provider_job(
            shot_key=key, model=model, prompt_hash=_digest(item['video_prompt']),
            source_image_hashes={str(n): h for n, h in enumerate(identity['image_hashes'])},
            input_hash=input_hash, output_path=output, provider_name=provider_name,
            resolved_duration=payload['duration'], resolved_size_params={
                'resolution': payload['resolution'], 'aspect_ratio': payload['aspect_ratio']},
        ))
    default_base = 'https://routerai.ru/api/v1' if provider_name == 'routerai' else 'https://api.aitunnel.ru/v1'
    base = os.getenv(f'{provider_name.upper()}_API_BASE', default_base).rstrip('/')
    headers = {'Authorization': 'Bearer ' + api_key}
    task_id = job.get('task_id')
    if not task_id:
        if job['status'] == 'submitting':
            raise RuntimeError('Reference submission without ID must not be repeated')
        _require_budget_for_new_video_submission(deadline, started_at, remaining, workers)
        store.update_job(key, input_hash, {'status': 'submitting'}, 'submitting_at')
        response = requests.post(base + '/videos', headers=headers, json=payload, timeout=1200)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            store.update_job(key, input_hash, {'error': f'{response.status_code}: {response.text}'}, 'submit_error_at')
            raise
        task_id = response.json()['id']
        store.update_job(key, input_hash, {'status': 'submitted', 'task_id': task_id}, 'submitted_at')
    status = _wait_for_video_completion_aitunnel(
        task_id, headers, base, max_wait_time=1200, provider_name=provider_name,
        on_poll=lambda p: _record_poll_update(store, key, input_hash, p))
    urls = status.get('unsigned_urls') or []
    url = urls[0] if urls else f'{base}/videos/{task_id}/content?index=0'
    store.update_job(key, input_hash, {'status': 'downloading', 'video_url': url}, 'completed_at')
    _download_video_aitunnel(url, output, headers if url.startswith(base + '/') else {})
    store.update_job(key, input_hash, {'status': 'downloaded', 'error': None}, 'downloaded_at')
    return dict(result, task_id=task_id)


def _decide(state, questions):
    base = os.environ['OPENAI_API_BASE_DB'].rstrip('/')
    response = requests.post(base + '/decisions', headers={
        'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY_DB']}, json={
            'model': 'llmgateway/decision', 'state': state,
            'questions': questions,
        }, timeout=1200)
    response.raise_for_status()
    answers = response.json()['answers']
    result = {name: answers[name]['choice'] for name in questions}
    if any(choice not in questions[name]['criteria'] for name, choice in result.items()):
        raise ValueError('Unknown video planning choice')
    return result


def choose_video_method(state):
    return _decide(state, {
        'method': {'type': 'choice', 'instructions': (
            'Use current_video_prompt as the current motion direction when present, subject to '
            'the current physical continuity and required state changes. '
            'Classify the CURRENT SHOT by its main content. Action: running, pursuit, fighting, '
            'racing, dynamic driving including close-ups; physical action and spatial progress '
            'are the subject. Non-action: dialogue, reflection, emotion, observation, static inserts '
            'and reactions. Driving with dialogue viewed from inside the cabin is non-action: '
            'movement is background. Camera movement alone does not make a shot action. '
            'Moving-object impacts, collisions and destruction are physical action too. Classify by '
            'content, not camera labels: INSERT or CLOSE-UP does not make dynamic action a static insert. '
            'Action normally uses references, but use keyframes when an essential exact causal boundary, '
            'named landmark part or contact position must match the supplied boundary image and the '
            'transition between known states is controllable. Keep free multi-step action with flexible '
            'boundaries in references. Use existing separate shots to keep a precise endpoint out of '
            'the surrounding free-action group; ordinary planned START/END poses alone do not require keyframes. '
            'Classify inserts separately even inside an action scene. Preserve required events and text.'),
            'criteria': {'references': 'Free continuous action with flexible boundaries',
                         'keyframes': 'Non-action or a controllable transition with an essential exact boundary'}},
        'continuity': {'type': 'choice', 'instructions': (
            'Does the current shot continue the same uninterrupted physical action as the immediately '
            'previous shot? A change of shot size alone does not interrupt it. A new action, time jump, '
            'insert, dialogue or reaction interrupts it. Use new if there is no previous shot.'),
            'criteria': {'continue': 'Same continuous action', 'new': 'New fragment'}},
    })


def _references(base, item):
    from .background_references import _reference_path
    paths = [s['reference_image_path'] for s in (item.get('character_reference_states') or {}).values()]
    if not paths:
        paths = [c['reference_image_path'] for c in (item.get('characters') or []) if c.get('reference_image_path')]
    paths += [str(_reference_path(base, c['reference_image_path']))
              for c in (item.get('locations') or []) if c.get('reference_image_path')]
    for phase in ('start', 'end'):
        context = (item.get('_continuity') or {}).get(phase) or {}
        paths += [str(_reference_path(base, p)) for p in context.get('landmark_reference_paths', [])]
    return list(dict.fromkeys(str(_reference_path(base, p)) for p in paths))


def _phase(spec):
    return {k: spec.get(k) for k in ('camera_anchor', 'pose_signature', 'must_show', 'visible_readable_texts',
                                     'must_not_show', 'world_physics', 'visible_characters')}


def _planning_shot(item):
    spec = item.get('_shot_frame_spec') or {}
    result = dict(shot_key=_key(item), duration_s=_duration(item),
                action=str(item.get('shot_description') or item.get('video_prompt') or '')[:1600],
                current_video_prompt=str(item.get('video_prompt') or ''),
                camera=item.get('camera_plan'), voiceover=item.get('voiceover'),
                opening=_phase(spec.get('start_state_spec') or spec),
                ending=_phase(spec.get('end_state_spec') or spec),
                characters=[{k: c.get(k) for k in ('name', 'reference_image_path')} for c in item.get('characters', [])],
                locations=[{k: l.get(k) for k in ('name', 'reference_image_path')} for l in item.get('locations', [])],
                character_reference_states={name: {k: s.get(k) for k in ('state_id', 'reference_image_path')}
                                            for name, s in (item.get('character_reference_states') or {}).items()},
                style_id=item.get('style_id'))
    if item.get('_continuity') is not None:
        result['continuity'] = item['_continuity']
    return result


def _compatible(left, right):
    a, b = left.get('character_reference_states') or {}, right.get('character_reference_states') or {}
    return (all(a[name].get('state_id') == b[name].get('state_id') for name in a.keys() & b.keys())
            and left.get('style_id') == right.get('style_id')
            and sorted(l['name'] for l in left.get('locations', []))
            == sorted(l['name'] for l in right.get('locations', [])))


def _action_groups(items, choices, positions=None):
    groups, states = [], {}
    for position, item in enumerate(items):
        choice = choices[_key(item)]
        if choice['method'] != 'references':
            continue
        previous = items[position - 1] if position else None
        join = (previous is not None and choices[_key(previous)]['method'] == 'references'
                and (positions is None or positions[_key(previous)] + 1 == positions[_key(item)])
                and choice['continuity'] == 'continue' and previous['scene_number'] == item['scene_number']
                and _compatible(previous, item)
                and all(name not in states or states[name] == state.get('state_id')
                        for name, state in (item.get('character_reference_states') or {}).items()))
        if join:
            groups[-1].append(item)
        else:
            groups.append([item])
            states = {}
        states.update({name: state.get('state_id') for name, state in (item.get('character_reference_states') or {}).items()})
    return groups


def _frame_path(item, phase=None):
    phase = phase or item.get('shot_type', 'start')
    if item.get('output_path') and item.get('shot_type', 'start') == phase:
        return str(Path(item['output_path']).resolve())
    directory = Path(item['video_path']).parent
    return str((directory / f"img_final_{phase}_{int(item['scene_number']):02d}_{int(item['shot_number']):02d}.png").resolve())


def _split_action(group, config):
    allowed = set(config.get('supported_durations') or [])
    limit = min(30, max(allowed or [0]))
    chunks, cursor = [], 0
    while cursor < len(group):
        duration, end = 0, None
        for index in range(cursor, len(group)):
            duration += _duration(group[index])
            if duration > limit:
                break
            if duration in allowed:
                end = index + 1
        if end is None:
            raise ValueError(f'Action shot {_key(group[cursor])} cannot fit supported video durations up to {limit}s')
        chunks.append(group[cursor:end])
        cursor = end
    return chunks


def select_image_items(project_id, items):
    """Keep keyframe pairs and action boundaries; rebase skipped image dependencies."""
    path = safe_storybook_project_dir(project_id) / '97_shots/video_plan.json'
    if not path.is_file():
        return items
    units = [u for u in json.loads(path.read_text()).get('units', []) if u.get('start_reference_path')]
    if not units:
        return items
    by_key = {key: u for u in units for key in u['shot_keys']}
    required = {p for u in units for p in (u['start_reference_path'], u['end_reference_path'])}
    all_paths = {_frame_path(i) for i in items}
    selected = [i for i in items if _key(i) not in by_key or _frame_path(i) in required]
    kept = {_frame_path(i) for i in selected}
    result = []
    for original in selected:
        item = dict(original)
        unit = by_key.get(_key(item))
        anchor = unit['start_reference_path'] if unit else None

        def rebase(value):
            if value and str(Path(value).resolve()) in all_paths - kept:
                if not anchor or anchor == _frame_path(item):
                    raise ValueError(f'Missing image dependency for {_key(item)}: {value}')
                return anchor
            return value

        for name in ('reference_image_paths', 'image_paths', 'base_image_paths'):
            if isinstance(item.get(name), list):
                item[name] = [rebase(p) for p in item[name]]
        for name in ('image_path', 'base_image_path', 'source_end_path'):
            if item.get(name):
                item[name] = rebase(item[name])
        result.append(item)
    return result


def photo_items_for_plan(project_id, items):
    result = []
    for item in apply_video_plan(project_id, items):
        if item.get('video_mode') != 'references':
            result.append(item)
        elif item.get('start_reference_path') and item.get('end_reference_path'):
            for phase, field in [('start', 'start_reference_path'), ('end', 'end_reference_path')]:
                result.append(dict(item, shot_type=phase, output_path=item[field], image_path=item[field]))
        else:
            # Previously adopted films retain their complete original photo storyboard.
            result.extend(i for i in items if _key(i) in item['covered_shot_keys'])
    return result


def prepare_reference_prompt(base, group, state, refs, language='ru', final_image=None,
                             initial_image=None, continuity_image=None, style_image=None):
    """Bind scene-state images and a separate saved style reference."""
    from agent_command import model_ultimate
    from utils import call_openai_api, parse_llm_json
    from .video_generator_aitunnel_tool import _discover_shot_image
    first, last = group[0], group[-1]
    initial_image = initial_image or _discover_shot_image(
        str(Path(first['video_path']).parent), first['scene_number'], first['shot_number'], 'start')
    final_image = final_image or _discover_shot_image(
        str(Path(last['video_path']).parent), last['scene_number'], last['shot_number'], 'end')
    refs = list(refs)
    state = dict(state)
    for role, path in [('style_image_number', style_image), ('initial_image_number', initial_image),
                       ('continuity_image_number', continuity_image)]:
        if path:
            path = str(Path(path).resolve())
            if path not in refs:
                refs.append(path)
            state[role] = refs.index(path) + 1
    if final_image:
        final_image = str(Path(final_image).resolve())
        if final_image not in refs:
            refs.append(final_image)
        state['final_image_number'] = refs.index(final_image) + 1
    state['landmark_image_roles'] = list({entity['id']: dict(
        entity_id=entity['id'], name=entity.get('name', entity['id']),
        image_number=refs.index(entity['reference_path']) + 1)
        for item in group for context in item.get('_continuity', {}).values()
        for entity in context.get('entities', [])
        if entity['kind'] == 'landmark' and entity.get('reference_path') in refs}.values())
    response = call_openai_api(prompt=json.dumps(state, ensure_ascii=False), model=model_ultimate,
        system_prompt=(f'Write one concise video prompt in {language} for one continuous shot. '
                       'Bind every reference explicitly using Image N and its assigned role. '
                       'If final_image_number is provided, reach its required visual state by the end: '
                       'preserve its environment geometry, object condition and drawn treatment. '
                       'It is an ordinary image reference, not an API last_frame; do not mention API '
                       'fields or this distinction in the output prompt. '
                       'style_image_number identifies a dedicated visual-style image: explicitly bind it to the '
                       'medium, palette, lighting, line and surface treatment throughout the clip. It supplies '
                       'style, not characters, objects, location geometry or narrative events. Opening and ending '
                       'images supply staged world states, not the choice of style. '
                       'Respect the shot time of day and physical light sources. '
                       'Explicit user_override takes priority over earlier source staging. '
                       'If initial_image_number is given, it defines the opening visual state. '
                       'Subjects visible in that opening are already visible from the first frame, '
                       'performing the planned initial action; preserve their initial relative placement. '
                       'An entrance or delayed reveal is allowed only when explicitly required by the opening state. '
                       'A continuity_image_number refers to an earlier interrupted action: preserve identities, '
                       'direction and achieved progress, while respecting the current fragment camera and lighting. '
                       'Continuity states govern movable props and shared landmark identity throughout the clip. '
                       'Preserve current_video_prompt motion choices when adapting existing directions; '
                       'do not restore outdated actions from source descriptions. Current physical continuity '
                       'and recorded continuity events still govern any necessary correction. '
                       'Treat must_not_show as phase-scoped constraints, not blanket prohibitions for the whole clip. '
                       'Character references define appearance only; location references define materials and architecture. '
                       'landmark_image_roles assigns each numbered image to one named object construction only. '
                       'Other objects in its background are not authoritative: use their own masters. '
                       'Current continuity states govern changeable contents, fuel, damage and staging. '
                       'Use continuity and phase anchors for the exact shared landmark. Temporary objects from identity '
                       'sheets do not enter staging unless the continuity or phase state requires them. '
                       'All of these images are ordinary numbered references. Include every intermediate '
                       'required event at its supplied time; do not turn boundary images into still holds. '
                       'Boundary references anchor world state and character '
                       'placement, not a frozen pose or a compulsory copied camera angle. Follow the fragment camera plan. '
                       'Describe enough world-space travel for the duration, camera movement and required actions. '
                       'Write concisely about actions and causal events, not a limb-by-limb gait inventory. '
                       'When character visibility changes, distinguish source-required physical exits or entrances '
                       'from camera reveals or occlusion of already-present actors. Preserve established '
                       'positions, source order and distinct identities; do not interpolate people. '
                       'Snapshot limb positions are not timed instructions to repeat an identical stride or pose. '
                       'Keep meaningful story-beat timing; omit incidental step phases, repeated object inventories '
                       'and redundant pose constraints already visible in references. Identity sheets define '
                       'appearance, not staging order or a prescribed limb trajectory. Use the reference visual '
                       'state when incidental pose prose conflicts with it. '
                       'Do not invent fixed-duration holds. Preserve clothing and character order. '
                       'Reference sheets supply identity, not a multi-panel output. Do not invent events, '
                       'paste the film bible, generate a new style, or include narration, dialogue, '
                       'subtitles or audio instructions. Sound is assembled separately. Return JSON video_prompt.'),
        max_tokens=2600, temperature=0.2, response_format={'type': 'json_object'})
    prompt = parse_llm_json(response)['video_prompt']
    catalog_path = base / '30_style/image_styles.json'
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text())
        first = group[0]
        binding = f"{first['scene_number']}:{first['shot_number']}:start"
        from .style_keeper import resolve_image_style_id
        style_id = resolve_image_style_id(catalog, binding, first.get('locations'), first.get('style_id'))
        style = catalog['styles'][style_id]['prompt']
        prompt = style + '\n\n' + prompt
    return prompt, refs


def storybook_video_plan_tool(session_id: str, project_id: str, enable: bool = True, language: str = 'ru',
                              shots_data: dict = None, shot_keys: list = None) -> dict:
    """Select keyframes or reference images for continuous video fragments.

    Args:
        session_id: Execution identifier.
        project_id: Storybook project identifier.
        enable: Whether video planning is enabled.
        language: Project prompt language.
        shots_data: Checked shot tasks with character-state references.
        shot_keys: Optional source shot keys to replan, preserving other existing units.
    """
    if not enable:
        return {'status': 'skipped'}
    from .video_generator_common import _get_routerai_video_models, _get_aitunnel_video_models
    from .video_contract import _active_video_tool_name
    from .style_keeper import ensure_image_style_catalog, ensure_video_style_references, resolve_image_style_id
    base = safe_storybook_project_dir(project_id)
    shots = shots_data or json.loads((base / '97_shots/shots.json').read_text())
    if isinstance(shots, str):
        shots = json.loads(shots)
    if isinstance(shots, list):
        shots = {'items': shots}
    items = shots['items'] if isinstance(shots, dict) else shots
    from .continuity import bind_continuity_to_items
    items = bind_continuity_to_items(base, items)
    catalog_path = base / '30_style/image_styles.json'
    style_source_path = base / '30_style/style_images.json'
    catalog = (ensure_image_style_catalog(str(base), json.loads(style_source_path.read_text()))
               if style_source_path.is_file() else
               json.loads(catalog_path.read_text()) if catalog_path.is_file() else {})
    if catalog:
        items = [dict(item, style_id=resolve_image_style_id(
            catalog, f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}",
            item.get('locations'), item.get('style_id'))) for item in items]
        bindings = {f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}": item['style_id']
                    for item in items}
        if any(catalog.get('frame_bindings', {}).get(key) != value for key, value in bindings.items()):
            catalog.setdefault('frame_bindings', {}).update(bindings)
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    target = base / '97_shots/video_plan.json'
    source_hash = _fingerprint(items, language, catalog, shots.get('seed'), base)
    previous_plan = json.loads(target.read_text()) if target.is_file() else {}
    scope = set(shot_keys) if shot_keys is not None else {_key(i) for i in _starts(items)}
    known_keys = {_key(i) for i in _starts(items)}
    if not scope or not scope.issubset(known_keys):
        raise ValueError('Video planning scope contains missing or no shots')
    if shot_keys is not None:
        for unit in previous_plan.get('units', []):
            keys = set(unit['shot_keys'])
            if keys & scope and not keys.issubset(scope):
                raise ValueError('Planning scope must include a complete existing video unit')
        source_hash = _digest({'source': source_hash, 'scope': sorted(scope)})

    def render_items():
        return [i for i in select_image_items(project_id, items) if _key(i) in scope]

    if target.is_file() and json.loads(target.read_text()).get('source_hash') == source_hash:
        cached_styles = {unit['style_id'] for unit in previous_plan.get('units', [])
                         if set(unit['shot_keys']) & scope and unit.get('style_id')}
        if cached_styles:
            ensure_video_style_references(base, catalog, cached_styles, session_id, shots.get('seed'), items)
        return dict(shots, status='success', video_plan_path=str(target), cached=True,
                    scope_shot_keys=sorted(scope) if shot_keys is not None else None,
                    items=render_items())
    active_tool = _active_video_tool_name()
    prefix = 'AITUNNEL' if active_tool == 'video_generator_aitunnel_tool' else 'ROUTERAI'
    model = os.environ.get(f'{prefix}_VIDEO_MODEL', '')
    config = (_get_aitunnel_video_models().get(model, {}) if prefix == 'AITUNNEL' else
              _get_routerai_video_models().get(model, {}) if active_tool == 'video_generator_routerai_tool' else {})
    all_starts = _starts(items)
    positions = {_key(item): index for index, item in enumerate(all_starts)}
    starts = [i for i in all_starts if _key(i) in scope]
    frames = {(_key(item), item.get('shot_type', 'start')): item for item in items}

    def classify(index):
        original_index = positions[_key(starts[index])]
        return choose_video_method(dict(current=_planning_shot(starts[index]),
            previous=_planning_shot(all_starts[original_index - 1]) if original_index else None,
            next=_planning_shot(all_starts[original_index + 1]) if original_index + 1 < len(all_starts) else None))

    with ThreadPoolExecutor(max_workers=4) as pool:
        choices = dict(zip((_key(i) for i in starts), pool.map(classify, range(len(starts)))))
    groups = _action_groups(starts, choices, positions)
    if groups and not config.get('supports_image_references'):
        raise ValueError(f'Model {model} does not support reference videos required by action shots')
    if groups and not catalog:
        raise ValueError('Reference videos require a saved image-style catalog')
    style_references = ensure_video_style_references(
        base, catalog, {group[0]['style_id'] for group in groups}, session_id, shots.get('seed'), items) if groups else {}

    def boundary(item, phase):
        source = frames.get((_key(item), phase))
        if source is None:
            raise ValueError(f'Missing {phase} image task for {_key(item)}')
        return _frame_path(source)

    def plan(entry):
        group_index, group = entry
        previous = [g for g in groups[:group_index]
                    if (positions[_key(g[-1])] + 1 < positions[_key(group[0])]
                        or g[-1]['scene_number'] != group[0]['scene_number'])
                    and _compatible(g[-1], group[0])]
        resumed = None
        if previous:
            candidates = {_key(g[-1]): g[-1] for g in previous}
            answer = _decide({'current': [_planning_shot(group[0])],
                             'ending': _planning_shot(group[-1])['ending'],
                             'earlier_actions': [_planning_shot(i) for i in candidates.values()]}, {
                'resume': {'type': 'choice', 'instructions': (
                    'Does this action resume an earlier action interrupted by other scenes or inserts? '
                    'Choose its last shot only when this is the same ongoing action, with coherent '
                    'direction and achieved progress. Shared characters or location alone are insufficient. '
                    'Do not merge across the interruption; this only links continuity references.'),
                    'criteria': {'new': 'Independent action', **{key: f'Resume action ending at {key}' for key in candidates}}}})
            resumed = candidates.get(answer['resume'])
        chunks = _split_action(group, config)
        planned = []
        for number, chunk in enumerate(chunks):
            initial = boundary(chunks[number - 1][-1], 'end') if number else boundary(chunk[0], 'start')
            final = boundary(chunk[-1], 'end')
            continuity = boundary(resumed, 'end') if resumed is not None else None
            refs = list(dict.fromkeys(path for item in chunk for path in _references(base, item)))
            if not refs:
                raise ValueError(f'Action {_key(chunk[0])} has no identity/location references')
            state_shots, offset = [], 0.0
            for item in chunk:
                summary = _planning_shot(item)
                summary.pop('voiceover', None)
                summary.update(start_s=offset, end_s=offset + _duration(item))
                offset += _duration(item)
                state_shots.append(summary)
            state = {'duration_s': offset, 'shots': state_shots,
                     'opening': state_shots[0]['opening'], 'ending': state_shots[-1]['ending']}
            from .background_references import _reference_path
            roles = {str(_reference_path(base, c['reference_image_path'])): c['name']
                     for item in chunk for c in item.get('characters', []) + item.get('locations', [])
                     if c.get('reference_image_path')}
            for item in chunk:
                for name, variant in (item.get('character_reference_states') or {}).items():
                    roles[str(_reference_path(base, variant['reference_image_path']))] = name
            state['reference_images'] = [{'image': n, 'subject': roles.get(path)} for n, path in enumerate(refs, 1)]
            style_id = chunk[0]['style_id']
            style_image = style_references[style_id]
            required = set(refs) | {initial, final, style_image} | ({continuity} if continuity else set())
            if config.get('max_image_references') is not None and len(required) > config['max_image_references']:
                raise ValueError(f'Too many references for action {_key(chunk[0])}')
            prompt, refs = prepare_reference_prompt(base, chunk, state, refs, language,
                final_image=final, initial_image=initial, continuity_image=continuity, style_image=style_image)
            keys = [_key(i) for i in chunk]
            path = base / '97_shots/video_units' / f'{keys[0]}_{keys[-1]}.mp4'
            path.parent.mkdir(parents=True, exist_ok=True)
            planned.append(dict(shot_keys=keys, duration_s=offset, video_path=str(path), video_prompt=prompt,
                reference_image_paths=refs, start_reference_path=initial, end_reference_path=final,
                style_id=style_id, style_reference_path=style_image,
                continues_from_shot=_key(chunks[number - 1][-1]) if number else (_key(resumed) if resumed is not None else None),
                seed=shots.get('seed'), model=model, resolution=os.getenv(f'{prefix}_RESOLUTION', '480p')))
            from .continuity import unit_continuity_hash
            continuity_hash = unit_continuity_hash(base, keys)
            if continuity_hash is not None:
                planned[-1]['continuity_hash'] = continuity_hash
        return planned

    with ThreadPoolExecutor(max_workers=4) as pool:
        units = [unit for planned in pool.map(plan, enumerate(groups)) for unit in planned]
    decisions = [dict(shot_keys=[_key(item)], **choices[_key(item)]) for item in starts]
    if shot_keys is not None:
        units = [u for u in previous_plan.get('units', []) if not set(u['shot_keys']) & scope] + units
        decisions = [d for d in previous_plan.get('decisions', []) if not set(d['shot_keys']) & scope] + decisions
        units.sort(key=lambda u: positions[u['shot_keys'][0]])
    target.write_text(json.dumps({'source_hash': source_hash, 'units': units, 'decisions': decisions},
                                 ensure_ascii=False, indent=2))
    return dict(shots, status='success', video_plan_path=str(target), cached=False,
                scope_shot_keys=sorted(scope) if shot_keys is not None else None,
                items=render_items())


def adopt_reference_test(project_id, directory, shot_keys):
    """Register a verified experiment as an existing result, without a video request."""
    from .video_generator_aitunnel_jobs import _ProviderJobStore, _new_provider_job
    base, directory = safe_storybook_project_dir(project_id), Path(directory)
    source = json.loads((base / '97_shots/shots.json').read_text())
    items = source['items']
    starts = {_key(i): i for i in _starts(items)}
    context = json.loads((directory / 'context.json').read_text())
    test = json.loads((directory / 'provider_jobs.json').read_text())['jobs'][-1]
    if test['status'] != 'downloaded':
        raise ValueError('Only a downloaded test may be adopted')
    output = base / '97_shots/video_units' / f'{shot_keys[0]}_{shot_keys[-1]}_{_digest(test["task_id"])[:10]}.mp4'
    unit = dict(shot_keys=shot_keys, duration_s=sum(_duration(starts[k]) for k in shot_keys),
                video_path=str(output), video_prompt=json.loads((directory / 'prompt.json').read_text())['video_prompt'],
                reference_image_paths=[r['path'] for r in context['references']], seed=context['seed'],
                model=context['model'], resolution=context['resolution'])
    if unit['duration_s'] != context['duration_seconds']:
        raise ValueError('Accepted test duration differs from covered shots')
    for ref in context['references']:
        if hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest() != ref['sha256']:
            raise ValueError('Test reference changed')
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.is_file():
        shutil.copy2(test['output_path'], output)
    plan_path = base / '97_shots/video_plan.json'
    plan_path.write_text(json.dumps({'source_hash': _fingerprint(items, context.get('language', 'ru')), 'units': [unit],
                                    'decisions': [{'shot_keys': shot_keys, 'method': 'references',
                                                   'accepted_task_id': test['task_id']}]}, ensure_ascii=False, indent=2))
    item = next(i for i in apply_video_plan(project_id, items) if _key(i) == shot_keys[0])
    _, identity = reference_request(item, context['model'], context['seed'])
    input_hash = _digest(identity)
    store = _ProviderJobStore(str(base / '97_shots/provider_jobs.json'), provider_name='routerai')
    store.mark_stale_for_changed_input(shot_keys[0], input_hash)
    store.ensure_job(_new_provider_job(
        shot_key=shot_keys[0], model=context['model'], prompt_hash=_digest(item['video_prompt']),
        source_image_hashes={str(n): h for n, h in enumerate(identity['image_hashes'])},
        input_hash=input_hash, output_path=unit['video_path'], provider_name='routerai',
        resolved_duration=context['duration_seconds'], resolved_size_params={
            'resolution': context['resolution'], 'aspect_ratio': context['aspect_ratio']},
    ))
    store.update_job(shot_keys[0], input_hash, {
        'status': 'downloaded', 'task_id': test['task_id'], 'cost': test.get('cost'),
        'currency': test.get('currency'), 'accepted_test_path': str(directory), 'error': None,
    }, 'downloaded_at')
    return unit
