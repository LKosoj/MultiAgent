"""Visual continuity acceptance using the existing labeled-image QA renderer."""
import json
import math
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fractions import Fraction

from agent_command import model_ultimate
from utils import call_openai_api, parse_llm_json
from .continuity import (_digest, _source, _PLAN_VERSION, _write, bind_continuity_to_items,
                         load_continuity_plan, unit_continuity_hash, validate_continuity_plan)
from .project_paths import safe_storybook_project_dir

_REVIEW_VERSION = 8


def validate_current_continuity(base):
    plan = load_continuity_plan(base)
    if plan:
        source = _source(base)
        validate_continuity_plan(plan, source['screenplay'])
        if plan['input_hash'] != _digest([_PLAN_VERSION, source]):
            raise ValueError('Continuity plan is stale; reconcile the screenplay first')
    return plan


def _file_hash(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tasks(base, kind):
    plan = validate_current_continuity(base)
    if not plan:
        return []
    screenplay = _source(base)['screenplay']
    source_shots = {(scene['scene_number'], shot['shot_number']): shot
                    for scene in screenplay['screenplay'] for shot in scene['storyboard']}
    source = json.loads((base / '97_shots/shots.json').read_text())['items']
    items = bind_continuity_to_items(base, source)
    directions = {(item['scene_number'], item['shot_number']): item.get('video_prompt')
                  for item in items if item.get('shot_type', 'start') == 'start'}
    from .video_plan import select_image_items
    render_items = bind_continuity_to_items(base, select_image_items(base.name, source))
    if kind == 'images':
        selected = [item for item in render_items if item['_continuity'].get(item.get('shot_type', 'start'))]
        selected.sort(key=lambda i: (i['scene_number'], i['shot_number'], i.get('shot_type') != 'start'))
        groups = [selected[index:index + 6] for index in range(0, len(selected), 6)]
    else:
        from .video_plan import apply_video_plan
        starts = [item for item in items if item.get('shot_type', 'start') == 'start']
        groups = [[item] for item in apply_video_plan(base.name, starts)
                  if unit_continuity_hash(base, item.get('covered_shot_keys') or [f"{item['scene_number']}-{item['shot_number']}"])]
    tasks = []
    for group in groups:
        keys = {key for item in group for key in (item.get('covered_shot_keys') or [f"{item['scene_number']}-{item['shot_number']}"])}
        frames = group if kind == 'images' else [
            item for item in items if f"{item['scene_number']}-{item['shot_number']}" in keys]
        media = [str(Path(item['output_path'] if kind == 'images' else item['video_path']).resolve()) for item in group]
        anchors = {str(Path(item['output_path']).resolve()) for item in render_items
                   if f"{item['scene_number']}-{item['shot_number']}" in keys}
        offsets = {}
        if kind == 'videos':
            from .video_plan import _duration
            cursor = 0.0
            for key in group[0].get('covered_shot_keys') or sorted(keys):
                start = next(item for item in items if f"{item['scene_number']}-{item['shot_number']}" == key
                             and item.get('shot_type', 'start') == 'start')
                offsets[key] = (cursor, cursor + _duration(start))
                cursor += _duration(start)
        contexts = []
        for item in frames:
            phase = item.get('shot_type', 'start')
            spec = (item.get('_shot_frame_spec') or {}).get(f'{phase}_state_spec') or {}
            contexts.append(dict(scene_number=item['scene_number'], shot_number=item['shot_number'], phase=phase,
                continuity=item['_continuity'].get(phase), visible_characters=spec.get('visible_characters'),
                source_shot={key: source_shots[(item['scene_number'], item['shot_number'])].get(key)
                             for key in ('description', 'camera_plan')}))
            if offsets:
                contexts[-1]['current_video_prompt'] = directions.get((item['scene_number'], item['shot_number']))
                contexts[-1]['time_s'] = offsets[f"{item['scene_number']}-{item['shot_number']}"][phase != 'start']
        masters = {entity['reference_path'] for item in frames for context in item['_continuity'].values()
                   for entity in context.get('entities', []) if entity.get('reference_path')}
        hashes = {path: _file_hash(path) for path in set(media) | anchors | masters}
        events = []
        if offsets:
            events = sorted({offsets[f'{scene}-{shot}'][phase == 'end']
                             for entity in plan.get('entities', []) for transition in entity.get('transitions', [])
                             for scene, shot, phase in [transition['at'].split(':')]
                             if f'{scene}-{shot}' in offsets
                             and 0 <= offsets[f'{scene}-{shot}'][phase == 'end'] <= cursor})
        signature_parts = [_REVIEW_VERSION, kind, contexts, hashes,
                           [item.get('video_prompt') for item in group] if kind == 'videos' else
                           [item.get('english_prompt') for item in group]]
        if events:
            signature_parts.append(events)
        if kind == 'videos':
            signature_parts.append('video_sampling_v2')
        signature = _digest(signature_parts)
        tasks.append({'id': '|'.join(sorted(keys)), 'input_hash': signature, 'group': group, 'kind': kind,
                      'state_hash': unit_continuity_hash(base, sorted(keys)),
                      'frames': frames, 'media': media, 'anchors': sorted(anchors),
                      'masters': sorted(masters), 'contexts': contexts,
                      **({'events': events, 'planned_duration': cursor} if kind == 'videos' else {})})
    return tasks


def continuity_acceptance(base, kind):
    """Require accepted reviews for the actual current bytes, without a model call."""
    try:
        tasks = _tasks(base, kind)
        if not tasks:
            return {'passed': True, 'status': 'not_required'}
        path = base / f'97_shots/continuity_{kind}_review.json'
        report = json.loads(path.read_text()) if path.is_file() else {}
        records = report.get('tasks', {})
        missing = [task['id'] for task in tasks if not records.get(task['input_hash'], {}).get('passed')]
        return {'passed': not missing, 'status': 'accepted' if not missing else 'review_required',
                'unaccepted': missing}
    except (ValueError, KeyError, FileNotFoundError) as exc:
        return {'passed': False, 'status': 'invalid_inputs', 'error': str(exc)}


def require_continuity_acceptance(base, kind):
    check = continuity_acceptance(base, kind)
    if not check['passed']:
        raise ValueError(f'Continuity {kind} acceptance required: {check}')
    return check


def pending_image_findings(base, shot_keys):
    path = Path(base) / '97_shots/continuity_images_review.json'
    if not path.is_file():
        return []
    findings = []
    for record in json.loads(path.read_text()).get('tasks', {}).values():
        if record.get('review_version') != _REVIEW_VERSION:
            continue
        keys = record['id'].split('|')
        if (record['passed'] or not set(keys).intersection(shot_keys)
                or record.get('state_hash') != unit_continuity_hash(Path(base), keys)
                or any(not Path(p).is_file() or _file_hash(p) != digest for p, digest in record.get('media_hashes', {}).items())):
            continue
        for verdict in record['verdicts']:
            for finding in verdict['violations']:
                parts = str(finding.get('frame_key', '')).split(':')
                if len(parts) == 3 and f'{parts[0]}-{parts[1]}' in shot_keys and parts[2] in {'start', 'end'}:
                    findings.append(dict(scene_number=int(parts[0]), shot_number=int(parts[1]), shot_type=parts[2],
                                         observed=finding['observed'], required=finding['required']))
    return findings


def _audit(task, pictures, chunk_number):
    import base64
    import io
    from PIL import Image
    master_roles = {entity['reference_path']: entity['id']
                    for item in task['frames'] for context in item['_continuity'].values()
                    for entity in context.get('entities', []) if entity.get('reference_path')}
    master_images = [{'scene_number': 'LANDMARK', 'shot_number': master_roles.get(path, n),
                      'shot_type': 'object construction only', 'output_path': path}
                     for n, path in enumerate(task['masters'], 1)]
    request = {'prompt': json.dumps({'kind': task['kind'], 'expected_frames': task['contexts'], 'chunk': chunk_number,
                           'prior_findings': [dict(index=index, finding=finding)
                                              for index, finding in enumerate(task.get('prior_findings', []))],
                           **({'video_mode': task['group'][0].get('video_mode'),
                               'video_prompt': task['group'][0].get('video_prompt'),
                               'video_duration_s': task.get('video_duration_s'),
                               'final_sample_time_s': task.get('final_sample_time_s')}
                              if task['kind'] == 'videos' else {})}, ensure_ascii=False)}
    content = [{'type': 'text', 'text': request['prompt']}]
    for picture in master_images + pictures:
        if not Path(picture['output_path']).is_file():
            continue
        with Image.open(picture['output_path']) as source:
            image = source.convert('RGB')
            image.thumbnail((960, 960))
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=88)
        label = f"{picture['scene_number']}.{picture['shot_number']} {picture['shot_type']}"
        content.extend([{'type': 'text', 'text': label}, {'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode()}}])
    request['prompt'] = content
    response = call_openai_api(
        model=model_ultimate, max_tokens=4000, temperature=0.1, response_format={'type': 'json_object'},
        **request,
        system_prompt=(
            'Inspect actual labeled pixels against their own phase and the shared object identities. '
            'source_shot describes the whole source action; registered continuity defines physical START/END states. '
            'Re-evaluate every prior finding against current source_shot, registered continuity and pixels. '
            'Prior findings marked source=video are hypotheses from a generated clip, not observations of this source image; '
            'verify them on this image and explicitly dismiss video-only or transient defects absent here. '
            'Each text label immediately precedes one separate input image; panels, borders or pose studies '
            'inside that image belong to that single frame, not to an external review layout. '
            'Dismiss unsupported findings, including video-only, discarded derived checklist, framing or unregistered microdetail requirements, '
            'with an explicit nonempty reason and its index in dismissed. Prior findings are hypotheses, not mandates. '
            'When a prior defect remains but its wording is replaced by a clearer actionable finding, dismiss its old wording '
            'with the reason that it was replaced and return the confirmed defect as a new violation. '
            'Scope: physical and narrative continuity only. Reject changes to registered object construction, '
            'identity, location, support, holder, motion, visibility or required causal state changes. '
            'Also enforce explicitly required character presence, relative spatial order and concealed identity. '
            'Facial features on generic background figures do not by themselves reveal a concealed identity; blur and graphic simplification are visual treatment, not narrative anonymity. '
            'Reject reference-sheet overlays copied into the output: card numbers, grids and sheet borders. '
            'This includes contact sheets with multiple independent camera views or pose studies in one frame, '
            'even without borders or numbers; collage texture alone is not a contact sheet. '
            'Intentional source-prescribed in-scene writing is allowed; it is not a reference-sheet label. '
            'Do not infer spatial order from the order of names in a list. '
            'Do not reject exact limb or tool swing phases, gaze, expression, framing scale, texture, grain, '
            'lighting or incidental decor unless they actually violate those continuity requirements. '
            'Camera information only explains the viewpoint; it is not a framing checklist. '
            'Only unresolved doubts about these continuity requirements belong in uncertain. '
            'For physical object states, continuity overrides incompatible legacy phase descriptions or prompts. '
            'Each LANDMARK master shows the visual identity and overall registered geometry of its labeled entity, '
            'not current damage, staging or other objects in its background. Current states determine changeable contents and fuel. '
            'Hard requirements for part counts and arrangement must be supported by registered continuity.geometry or primary source; '
            'incidental reference details and decor are not new fixed constraints. Strictly preserve registered parts, '
            'their damage and location across views; damage changes only the specified part. '
            'Check the object relative to visible persistent neighboring landmarks, not just its shape: '
            'a lookalike at another site is not the same identity. Compare only what the current crop can show; '
            'do not require the full reference inventory or invent uncertainty solely from intentional cropping. '
            'Determine left and right parts within the object relative to its own frame or divider, not the absolute screen half; an object on the right of the screen does not make its visible part the right part. '
            'Derive the screen positions of named parts from their registered geometry and the current camera side. '
            'Opposite viewpoints reverse their projected left/right order; they do not transfer damage between parts. '
            'Reject mirrored damage or contact on the wrong named part even when the overall object looks similar. '
            'Check registered landmark parts independently of character positions in the current viewpoint. '
            'Do not accept rearranged characters if it was achieved by mirroring the landmark against its registered geometry. '
            'Occlusion or cropping does not '
            'remove a physical part. Character appearance references do not authorize temporary props. '
            + (
                'Each expected frame current_video_prompt is its own current motion direction; '
                'This chunk contains only the labeled video sample times shown. Check boundaries and events '
                'only when their times fall within this chunk; other chunks check the remaining times. '
                'The sample at final_sample_time_s represents END, even though it precedes video_duration_s. '
                'Do not report uncertainty because an opening, ending or event lies outside this chunk. '
                'the top-level video_prompt describes the combined video only. Do not restore an older transient gesture '
                'from inherited phase descriptions when it conflicts with that direction. Physical object '
                'states and recorded continuity events remain mandatory. '
              if task['kind'] == 'videos' else '') +
            'Use _continuity for the physical state of recurring objects: space, support, holder, motion and visibility. '
            'An offscreen object must not follow the actor; movement of the camera does not imply movement of a '
            'stationary prop. Check unauthorized contacts, kicks, transfers, extra copies and teleportation. '
            'Extra copies means duplication of the tracked object, not unrelated incidental objects of the same category. '
            'A singular object description in geometry does not prohibit such incidental objects unless the primary source explicitly limits their number or presence. '
            'The same object retains its state after other scenes unless a source transition changes it. '
            'START/END world states apply only to their corresponding boundaries; incidental pose details are not mandatory. '
            'For a merged references video, original internal shot cuts do not require their exact camera '
            'framing or limb poses; only the clip opening and ending are image anchors. Preserve required '
            'physical events, their timing and object continuity throughout the continuous action. '
            'time_s and VIDEO samples use actual clip time. END states are required by the end of the shot; '
            'do not assume an exact collision instant or require the old state immediately before END. '
            'Do not demand the same limb pose throughout a moving clip. Distinguish uncertainty from clear defects. '
            'For images, audit each expected frame separately and never transfer observations from another frame. '
            'CONTINUITY_REFERENCE is context only, never an expected frame; source and LANDMARK masters take priority. '
            'For images, identify each violation with the exact frame_key scene:shot:start or end from expected_frames. '
            'Write required as a concise actionable edit: identify what to move or replace and what nearby elements to preserve; '
            'do not correct a target by changing an unrelated object. Use screen positions relative to visible landmarks '
            'in the current view only when supported by source and registered geometry; do not guess an ambiguous direction. '
            'Identify named parts by visible features or their supported screen positions and limit the edit to the affected area. '
            'For videos, frame_key may identify the sample time. '
            'Return JSON {passed:bool, violations:[{frame_key:string, entity_id:string, observed:string, required:string}], '
            'uncertain:[string], dismissed:[{index:int, reason:string}]}. Pass only if there are no clear violations or unresolved critical identity/state doubts. '
            'Write findings in the language of the supplied descriptions.'),
    )
    result = parse_llm_json(response)
    if not isinstance(result.get('passed'), bool) or not isinstance(result.get('violations'), list) or not isinstance(result.get('uncertain'), list):
        raise ValueError('Invalid visual continuity review')
    if task['kind'] == 'images':
        expected = {f"{context['scene_number']}:{context['shot_number']}:{context['phase']}"
                    for context in task['contexts']}
        if any(not isinstance(finding, dict) or finding.get('frame_key') not in expected
               for finding in result['violations']):
            raise ValueError('Image review violation has an unexpected frame_key')
    dismissed = {entry['index'] for entry in result.get('dismissed', [])
                 if isinstance(entry, dict) and type(entry.get('index')) is int
                 and isinstance(entry.get('reason'), str) and entry['reason'].strip()}
    for index, finding in enumerate(task.get('prior_findings', [])):
        if index not in dismissed and finding not in result['violations']:
            result['violations'].append(finding)
    result['passed'] = result['passed'] and not result['violations'] and not result['uncertain']
    return result


def _repair_media(base, tasks, kind, session_id):
    """One bounded corrective pass through the existing prompt and media tools."""
    keys = sorted({key for task in tasks for key in task['id'].split('|')})
    brief_path = base / '00_brief.json'
    brief = json.loads(brief_path.read_text()) if brief_path.is_file() else {}
    language = brief.get('language') or 'ru'
    if kind == 'images':
        from .shots_prompt_qa import shots_prompt_qa_tool
        from .artist_batch_edit import artist_agent_batch_edit_tool
        from .video_plan import select_image_items
        from .background_references import storybook_background_plan_tool, storybook_background_references_tool
        report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
        affected = set()
        frame_findings = {}
        frame_entities = {}
        fully_localized = True
        for task in tasks:
            verdicts = report['tasks'][task['input_hash']]['verdicts']
            located = set()
            for verdict in verdicts:
                if verdict.get('passed'):
                    continue
                if verdict['uncertain'] or not verdict['violations']:
                    fully_localized = False
                for finding in verdict['violations']:
                    valid_finding = False
                    parts = str(finding.get('frame_key', '')).split(':')
                    if len(parts) == 3 and parts[2] in {'start', 'end'}:
                        key = f'{parts[0]}-{parts[1]}'
                        if key in task['id'].split('|'):
                            located.add(key)
                            if finding.get('observed') and finding.get('required'):
                                frame_findings.setdefault(finding['frame_key'], []).append(
                                    {'observed': finding['observed'], 'required': finding['required']})
                                if finding.get('entity_id'):
                                    frame_entities.setdefault(finding['frame_key'], set()).add(finding['entity_id'])
                                valid_finding = True
                    if not valid_finding:
                        fully_localized = False
            affected.update(task['id'].split('|') if not located or any(v['uncertain'] for v in verdicts) else located)
        shots = json.loads((base / '97_shots/shots.json').read_text())
        frames = select_image_items(base.name, shots['items'])
        while True:
            changed = {str(Path(item['output_path']).resolve()) for item in frames
                       if f"{item['scene_number']}-{item['shot_number']}" in affected}
            copied = {f"{item['scene_number']}-{item['shot_number']}" for item in frames
                      if item.get('copy_from_previous_end') and item.get('source_end_path')
                      and str(Path(item['source_end_path']).resolve()) in changed}
            if copied.issubset(affected):
                break
            affected.update(copied)
        keys = sorted(affected)
        by_frame = {f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}": item
                    for item in frames}
        direct_edit = (fully_localized and bool(frame_findings) and all(
            key in by_frame and not by_frame[key].get('copy_from_previous_end')
            and Path(by_frame[key]['output_path']).is_file() for key in frame_findings))
        if direct_edit:
            for key in frame_findings:
                Path(by_frame[key]['output_path']).with_suffix('.continuity.json').unlink(missing_ok=True)
            selected_paths = {str(Path(by_frame[key]['output_path']).resolve()) for key in frame_findings}
            while True:
                copies = {str(Path(item['output_path']).resolve()) for item in frames
                          if item.get('copy_from_previous_end') and item.get('source_end_path')
                          and str(Path(item['source_end_path']).resolve()) in selected_paths}
                if copies.issubset(selected_paths):
                    break
                selected_paths.update(copies)
            rendered = dict(shots, items=[dict(item) for item in frames
                            if str(Path(item['output_path']).resolve()) in selected_paths], scope_shot_keys=keys)
            rejected_paths = {str(Path(by_frame[key]['output_path']).resolve()) for key in frame_findings}
            plan = load_continuity_plan(base) or {}
            entity_refs = {entity['id']: entity.get('reference_path') for entity in plan.get('entities', [])}
            for item in rendered['items']:
                if str(Path(item['output_path']).resolve()) not in rejected_paths:
                    continue
                for field in ('reference_image_paths', 'references'):
                    refs = item.get(field)
                    if isinstance(refs, list):
                        item[field] = [path for path in refs if str(Path(path).resolve()) not in rejected_paths]
                    elif isinstance(refs, str) and str(Path(refs).resolve()) in rejected_paths:
                        item[field] = []
                frame_key = f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
                masters = [entity_refs[ident] for ident in sorted(frame_entities.get(frame_key, set()))
                           if entity_refs.get(ident) and Path(entity_refs[ident]).is_file()]
                refs = item.get('reference_image_paths') or item.get('references') or []
                if isinstance(refs, str):
                    refs = [refs]
                item['reference_image_paths'] = masters + [path for path in refs if path not in masters]
        else:
            for task in tasks:
                for item in task['group']:
                    if f"{item['scene_number']}-{item['shot_number']}" in affected:
                        Path(item['output_path']).with_suffix('.continuity.json').unlink(missing_ok=True)
            shots_prompt_qa_tool(session_id=session_id, project_id=base.name, shot_keys=keys,
                                 enable=True, force=True, global_max_repairs=0)
            shots = json.loads((base / '97_shots/shots.json').read_text())
            selected = [item for item in select_image_items(base.name, shots['items'])
                        if f"{item['scene_number']}-{item['shot_number']}" in keys]
            incoming = dict(shots, items=selected, scope_shot_keys=keys)
            planned = storybook_background_plan_tool(session_id, base.name, incoming, language=language)
            rendered = storybook_background_references_tool(session_id, base.name, planned, language=language)
        for item in rendered['items']:
            frame_key = f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
            if frame_key in frame_findings and not item.get('copy_from_previous_end') and Path(item['output_path']).is_file():
                item['_continuity_edit_findings'] = frame_findings[frame_key]
        result = artist_agent_batch_edit_tool(session_id=session_id, items=rendered, enable=True,
                                              language=language, max_concurrency=4)
        rows = json.loads(result)
        failed = [row for row in rows if not row.get('ok')]
        if failed:
            raise RuntimeError(f'Continuity image repair failed: {failed}')
        return
    from .video_contract import _active_video_tool_name
    import importlib
    tool_name = _active_video_tool_name()
    generate = getattr(importlib.import_module(f'custom_tools.storybook.{tool_name}'), tool_name)
    import inspect
    if 'shot_keys' not in inspect.signature(generate).parameters:
        raise ValueError('The selected video provider cannot retry scoped continuity repairs')
    shots_path = base / '97_shots/shots.json'
    shots = json.loads(shots_path.read_text())
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text()) if plan_path.is_file() else {'units': []}
    verdicts = json.loads((base / '97_shots/continuity_videos_review.json').read_text())['tasks']
    generation_keys = []
    for task in tasks:
        item = task['group'][0]
        key = f"{item['scene_number']}-{item['shot_number']}"
        generation_keys.append(key)
        if verdicts[task['input_hash']].get('passed'):
            continue
        if item.get('video_mode') == 'references':
            target = next(unit for unit in plan['units'] if unit['shot_keys'] == item['covered_shot_keys'])
        else:
            target = next(frame for frame in shots['items'] if f"{frame['scene_number']}-{frame['shot_number']}" == key
                          and frame.get('shot_type', 'start') == 'start')
        existing_prompt = target['video_prompt']
        response = call_openai_api(
            model=model_ultimate, temperature=0.1, max_tokens=4000, response_format={'type': 'json_object'},
            prompt=json.dumps({'existing_prompt': existing_prompt, 'expected_frames': task['contexts'],
                'reference_images': item.get('reference_image_paths'),
                'findings': verdicts[task['input_hash']]['verdicts']},
                ensure_ascii=False),
            system_prompt=(
                'Исправь промпт видеогенерации по фактическим нарушениям визуальной непрерывности. '
                'Перепиши коротко: убери повторы, противоречия и необязательные микродвижения, не дописывай длинные запреты. '
                'Сохрани исходное действие и сюжет, оптику, движение камеры, физику, длительность, порядок событий, все Image N и их роли. '
                'Не теряй обязательные физические условия ради сокращения. '
                'Не добавляй новых референсов или событий. Общие ориентиры сохраняют свою конструкцию; '
                'реквизит меняется только по заданным событиям. Отдельный предмет не следует за героем. '
                'Ограничения START/END относятся к своим фазам, не останавливай всё движение ради конечной позы. '
                'Уточни только причины перечисленных нарушений и способ их избежать. '
                'Сохрани язык и художественный стиль исходного промпта. Верни JSON {video_prompt:строка}.'),
        )
        prompt = parse_llm_json(response)['video_prompt'].strip()
        if not prompt or prompt == existing_prompt:
            raise ValueError('Continuity video repair did not change the rejected prompt')
        target['video_prompt'] = prompt
    _write(shots_path, shots)
    if plan_path.is_file():
        _write(plan_path, plan)
    generate(session_id=session_id, project_id=base.name, shot_keys=generation_keys, enable=True,
             max_concurrency=4, seed=shots.get('seed'), language=language, skip_prompt_enhancement=True)


def _video_sample_indices(events, planned_duration, duration, fps, frame_count):
    last = frame_count - 1
    count = max(2, math.ceil(duration))
    indices = {0, last}
    indices.update(min(last, int((index + 0.5) * duration / count * fps)) for index in range(count))
    for event in events:
        frame = min(last, max(0, int(event * duration / planned_duration * fps)))
        indices.update(min(last, max(0, frame + step)) for step in (-1, 0, 1))
    return sorted(indices)


def _review(session_id, project_id, kind, enable, repair=True, review_findings=None):
    if not enable:
        return {'status': 'skipped'}
    base = safe_storybook_project_dir(project_id)
    tasks = _tasks(base, kind)
    findings_by_task = {}
    if kind == 'images':
        frame_tasks = {f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}": task['input_hash']
                       for task in tasks for item in task['group']}
        for finding in review_findings or []:
            key = finding.get('frame_key')
            if key not in frame_tasks:
                raise ValueError(f'Unknown image review frame: {key}')
            findings_by_task.setdefault(frame_tasks[key], []).append(finding)
    path = base / f'97_shots/continuity_{kind}_review.json'
    report = json.loads(path.read_text()) if path.is_file() else {'tasks': {}}
    previous = dict(report['tasks'])

    def inspect(task):
        cached = previous.get(task['input_hash'])
        if cached is not None and (kind != 'images' or cached.get('passed')) and task['input_hash'] not in findings_by_task:
            return task['input_hash'], cached
        if kind == 'images':
            media_hashes = {p: _file_hash(p) for p in task['media']}
            prior = []
            for record in previous.values():
                if (record.get('id') == task['id'] and record.get('state_hash') == task['state_hash']
                        and record.get('media_hashes') == media_hashes):
                    for verdict in record.get('verdicts', []):
                        for finding in verdict.get('violations', []):
                            if finding not in prior:
                                prior.append(finding)
            for finding in findings_by_task.get(task['input_hash'], []):
                if finding not in prior:
                    prior.append(finding)
            expected_keys = {f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
                             for item in task['group']}
            if any(finding.get('frame_key') not in expected_keys for finding in prior):
                verdicts = [_audit(dict(task, prior_findings=prior), task['group'], 0)]
            else:
                pairs = {}
                for item in task['group']:
                    pairs.setdefault((item['scene_number'], item['shot_number']), []).append(item)
                verdicts = []
                previous_pair = None
                for pair_key, pair in pairs.items():
                    pair_keys = {f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
                                 for item in pair}
                    contexts = [context for context in task['contexts']
                                if f"{context['scene_number']}:{context['shot_number']}:{context['phase']}" in pair_keys]
                    masters = sorted({entity['reference_path'] for item in pair
                                      for context in item['_continuity'].values()
                                      for entity in context.get('entities', []) if entity.get('reference_path')})
                    pair_task = dict(task, group=pair, frames=pair, contexts=contexts, masters=masters,
                                     prior_findings=[finding for finding in prior if finding.get('frame_key') in pair_keys])
                    pictures = list(pair)
                    worlds = {context.get('continuity', {}).get('world_id') for context in contexts
                              if context.get('continuity')}
                    if previous_pair and previous_pair[0][0] == pair_key[0] and previous_pair[1]['passed']:
                        end = next((item for item in previous_pair[2] if item.get('shot_type', 'start') == 'end'), None)
                        end_world = ((end or {}).get('_continuity', {}).get('end') or {}).get('world_id')
                        if end and end_world and worlds == {end_world}:
                            pictures.append(dict(end, shot_type='CONTINUITY_REFERENCE'))
                    verdict = _audit(pair_task, pictures, len(verdicts))
                    verdicts.append(verdict)
                    previous_pair = (pair_key, verdict, pair)
        else:
            with tempfile.TemporaryDirectory(prefix='storybook_continuity_') as temporary:
                probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                    '-show_entries', 'stream=duration,avg_frame_rate,r_frame_rate,nb_frames:format=duration', '-of', 'json', task['media'][0]], text=True))
                stream = probe['streams'][0]
                duration = float(stream.get('duration') or probe['format']['duration'])
                rate = stream.get('avg_frame_rate')
                if not rate or rate in ('0/0', '0/1'):
                    rate = stream['r_frame_rate']
                fps = float(Fraction(rate))
                frame_count = int(stream['nb_frames']) if str(stream.get('nb_frames', '')).isdigit() else round(duration * fps)
                indices = _video_sample_indices(task.get('events', []), task['planned_duration'], duration, fps, frame_count)
                expression = '+'.join(f'eq(n\\,{index})' for index in indices)
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', task['media'][0], '-vf',
                                f'select={expression},scale=960:-1', '-vsync', '0', str(Path(temporary) / '%03d.jpg')],
                               capture_output=True, check=True)
                images = sorted(Path(temporary).glob('*.jpg'))
                if len(images) != len(indices):
                    raise ValueError('Video samples missing')
                audit_task = dict(task, video_duration_s=duration, final_sample_time_s=indices[-1] / fps,
                                  contexts=[dict(context, time_s=context['time_s'] * duration / task['planned_duration'])
                                                  for context in task['contexts']])
                samples = [{'scene_number': 'VIDEO', 'shot_number': index,
                            'shot_type': f't={frame / fps:.2f}s', 'output_path': str(path)}
                           for index, (frame, path) in enumerate(zip(indices, images))]
                verdicts = [_audit(audit_task, samples[index:index + 6], index // 6)
                            for index in range(0, len(samples), 6)]
        return task['input_hash'], {'id': task['id'], 'passed': all(v['passed'] for v in verdicts),
                                    'review_version': _REVIEW_VERSION,
                                    'state_hash': task['state_hash'],
                                    'media_hashes': {p: _file_hash(p) for p in task['media']},
                                    'verdicts': verdicts, 'session_id': session_id}

    with ThreadPoolExecutor(max_workers=4) as executor:
        for signature, record in executor.map(inspect, tasks):
            report['tasks'][signature] = record
            _write(path, report)
    if kind == 'images':
        from .continuity import record_continuity_image
        for task in tasks:
            if report['tasks'][task['input_hash']]['passed']:
                for item in task['group']:
                    record_continuity_image(item, basis='visual_review')
    check = continuity_acceptance(base, kind)
    if not check['passed'] and repair:
        rejected = [task for task in tasks if not report['tasks'][task['input_hash']]['passed']]
        if kind == 'videos':
            image_keys = {str(Path(item['output_path']).resolve()):
                          f"{item['scene_number']}:{item['shot_number']}:{item.get('shot_type', 'start')}"
                          for image_task in _tasks(base, 'images') for item in image_task['group']}
            hypotheses = [dict(finding, source='video', video_frame_key=finding.get('frame_key'),
                               frame_key=image_keys[anchor])
                          for task in rejected for anchor in task['anchors'] if anchor in image_keys
                          for verdict in report['tasks'][task['input_hash']]['verdicts']
                          for finding in verdict['violations']]
            if hypotheses:
                source_review = _review(session_id, project_id, 'images', True, review_findings=hypotheses)
                if source_review['status'] != 'success':
                    raise RuntimeError('Source image continuity repair failed; video generation was not started')
                current = {task['id']: task['input_hash'] for task in _tasks(base, 'videos')}
                rejected_ids = {task['id'] for task in rejected}
                rejected.extend(task for task in tasks if task['id'] not in rejected_ids
                                and current[task['id']] != task['input_hash'])
        _repair_media(base, rejected, kind, session_id)
        return _review(session_id, project_id, kind, enable, repair=False)
    return {'status': 'success' if check['passed'] else 'error', 'review_path': str(path), 'acceptance': check}


def storybook_continuity_images_review_tool(session_id: str, project_id: str, enable: bool = True, review_findings: list = None) -> dict:
    """Accept shared geometry and prop states in rendered storyboard images.

    Args:
        session_id: Pipeline execution identifier.
        project_id: Storybook project identifier.
        enable: Whether to inspect generated images.
        review_findings: Optional observations about current images, each with exact frame_key.
    """
    return _review(session_id, project_id, 'images', enable, review_findings=review_findings)


def storybook_continuity_videos_review_tool(session_id: str, project_id: str, enable: bool = True) -> dict:
    """Accept physical actions and object identity in downloaded video clips.

    Args:
        session_id: Pipeline execution identifier.
        project_id: Storybook project identifier.
        enable: Whether to inspect generated video clips.
    """
    return _review(session_id, project_id, 'videos', enable)
