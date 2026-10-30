"""Shared story objects, their physical states and cross-scene frame bindings."""
import copy
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from agent_command import model_ultimate
from utils import call_openai_api, parse_llm_json
from .project_paths import safe_storybook_project_dir

_PLAN_VERSION = 5
_BOUNDARY_INSTRUCTION = (
    'START отражает начало шота, END — последнее состояние после всех заданных в нём событий, не промежуточную фазу. '
    'На один предмет в одной фазе допустим один переход: обобщи START → итоговый END; промежуточным фазам не нужны отдельные ключи или два перехода на одной границе. '
    'Явное превращение или изменение состояния представь итоговым состоянием и одним переходом с точной цитатой из description. '
    'Для изменения только видимости или полного перекрытия используй visible=false, не выдумывай физическое событие. '
    'Не закрепляй случайные позы, направления и предосторожности из производных описаний как сквозные физические ограничения: для этого требуется явное авторское условие либо необходимость в причинной цепочке событий. '
    'При протяжённом приближении оставляй в начале запас пути, правдоподобный для времени движения: персонажи ещё не у цели; неподвижные цель и предметы не переноси и не показывай принудительно, используй visible=false или дальний план. '
    'Сохраняй исходные id, geometry, reference_path и текст; не переписывай верный исходник ради ошибочного плана. '
)
logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _source(base):
    screenplay = json.loads((base / '91_screenplay/screenplay.json').read_text())
    for scene in screenplay['screenplay']:
        scene.get('scene_continuity_facts', {}).pop('continuity', None)
    return {
        'screenplay': screenplay,
        'characters': json.loads((base / '20_bible/characters.json').read_text()),
        'locations': json.loads((base / '20_bible/locations.json').read_text()),
        'author_materials': {p.name: p.read_text() for p in sorted((base / 'inputs').glob('*'))
                             if p.suffix in {'.txt', '.md'}},
    }


def _frame_sources(screenplay):
    return {f"{scene['scene_number']}:{shot['shot_number']}:{phase}":
            {'scene': scene, 'shot': shot}
            for scene in screenplay['screenplay'] for shot in scene['storyboard']
            for phase in ('start', 'end')}


def _order(key):
    scene, shot, phase = key.split(':')
    return int(scene), int(shot), phase != 'start'


def _normalized(text):
    return re.sub(r'\s+', ' ', str(text)).strip().casefold()


def _normalize_frame_keys(plan):
    """Normalize phase labels and fill omitted state IDs from the declared event chain."""
    def key(value):
        return re.sub(r'^scene:(\d+:\d+:(?:start|end))$', r'\1', value)
    normalized = {key(name): frame for name, frame in plan['frames'].items()}
    if len(normalized) != len(plan['frames']):
        raise ValueError('Duplicate normalized continuity frame')
    plan['frames'] = normalized
    for entity in plan['entities']:
        for transition in entity.get('transitions', []):
            transition['at'] = key(transition['at'])
    current = {entity['id']: entity['initial_state'] for entity in plan['entities']}
    events = {}
    for entity in plan['entities']:
        for transition in entity.get('transitions', []):
            events.setdefault(transition['at'], []).append((entity['id'], transition['to']))
    for phase in sorted(plan['frames'], key=_order):
        current.update(events.get(phase, []))
        for binding in plan['frames'][phase].get('entities', []):
            if 'state_id' not in binding and binding.get('id') in current:
                binding['state_id'] = current[binding['id']]
    return plan


def _planning_source(source):
    """Keep physical story facts; omit duplicated technical and presentation fields."""
    screenplay = source['screenplay']
    result = dict(source, screenplay={
        'concept': screenplay.get('concept'), 'director_notes': screenplay.get('director_notes'),
        'screenplay': [{key: scene.get(key) for key in ('scene_number', 'location_time', 'action', 'scene_continuity_facts')}
                      | {'storyboard': [{key: shot.get(key) for key in (
                          'shot_number', 'description', 'camera_plan', 'location_canon_name')}
                                        for shot in scene['storyboard']]}
                      for scene in screenplay['screenplay']],
    })
    result['characters'] = [{key: character.get(key) for key in ('name', 'role', 'immutable_attributes', 'variable_attributes')}
                            for character in source['characters']]
    materials = source['author_materials']
    result['author_materials'] = {name: text for name, text in materials.items()
                                 if not any(len(other) > len(text) and text.strip() in other
                                            for other in materials.values())}
    return result


def validate_continuity_plan(plan, screenplay):
    """Check coverage, object identity and every declared physical transition."""
    frames = _frame_sources(screenplay)
    if set(plan.get('frames', {})) != set(frames):
        raise ValueError('Continuity frame coverage is incomplete')
    entities = {entity['id']: entity for entity in plan.get('entities', [])}
    if len(entities) != len(plan.get('entities', [])):
        raise ValueError('Duplicate continuity entity')
    states, current, transitions = {}, {}, {}
    for ident, entity in entities.items():
        if (not re.fullmatch(r'[\w-]+', ident)
                or entity.get('kind') not in {'landmark', 'prop'} or not entity.get('world_id')):
            raise ValueError(f'Invalid continuity entity: {ident}')
        if entity['kind'] == 'landmark' and not entity.get('geometry'):
            raise ValueError(f'Landmark has no fixed geometry: {ident}')
        states[ident] = {state['id']: state for state in entity['states']}
        if len(states[ident]) != len(entity['states']) or entity['initial_state'] not in states[ident]:
            raise ValueError(f'Invalid continuity states: {ident}')
        for state in states[ident].values():
            if not state.get('description') or not state.get('space'):
                raise ValueError(f'Incomplete physical state: {ident}/{state.get("id")}')
            if state.get('motion') not in {'stationary', 'moving'}:
                raise ValueError(f'Invalid motion for {ident}/{state.get("id")}: {state.get("motion")!r}; allowed: stationary|moving')
        current[ident] = entity['initial_state']
        for event in entity.get('transitions', []):
            key = event['at']
            if key not in frames or (key, ident) in transitions:
                raise ValueError(f'Invalid continuity event binding: {ident}/{key}')
            source = frames[key]
            evidence = _normalized(event.get('evidence', '')).rstrip('.!?…')
            text = _normalized(source['shot']['description'])
            if len(evidence) < 12 or evidence not in text:
                raise ValueError(f'Continuity change has no source event: {ident}/{key}')
            transitions[(key, ident)] = event
    for key in sorted(frames, key=_order):
        frame = plan['frames'][key]
        if not frame.get('world_id'):
            raise ValueError(f'Continuity frame has no world: {key}')
        for ident in entities:
            event = transitions.get((key, ident))
            if event:
                if (event['from'] != current[ident] or event['to'] not in states[ident]
                        or entities[ident]['world_id'] != frame['world_id']):
                    raise ValueError(f'Invalid continuity transition: {ident}/{key}')
                current[ident] = event['to']
        seen = set()
        for binding in frame.get('entities', []):
            ident = binding['id']
            if ident not in entities or ident in seen or not isinstance(binding.get('visible'), bool):
                raise ValueError(f'Invalid continuity frame entity: {key}/{ident}')
            seen.add(ident)
            if entities[ident]['world_id'] != frame['world_id']:
                raise ValueError(f'Continuity object crossed worlds: {key}/{ident}')
            if binding['state_id'] != current[ident]:
                raise ValueError(f'Continuity state changed without an event: {key}/{ident}')
    return plan


def _apply_repairs(source, plan):
    result = copy.deepcopy(source)
    scenes = {scene['scene_number']: scene for scene in result['screenplay']['screenplay']}
    repairs = []
    for repair in plan.get('repairs', []):
        if 'set' not in repair and set(repair).issubset({'scene_number', 'action', 'storyboard', 'reason'}):
            reason = repair.get('reason') or 'Согласование постановки по результату планировщика непрерывности.'
            if 'action' in repair:
                repairs.append(dict(scene_number=repair['scene_number'], shot_number=None,
                                    set={'action': repair['action']}, reason=reason))
            for shot in repair.get('storyboard', []):
                if set(shot) != {'shot_number', 'description'}:
                    raise ValueError('Continuity repair may only reconcile staging descriptions')
                repairs.append(dict(scene_number=repair['scene_number'], shot_number=shot['shot_number'],
                                    set={'description': shot['description']}, reason=reason))
        else:
            repairs.append(repair)
    plan['repairs'] = repairs
    for repair in repairs:
        scene = scenes[repair['scene_number']]
        target = scene if repair.get('shot_number') is None else next(
            shot for shot in scene['storyboard'] if shot['shot_number'] == repair['shot_number'])
        allowed = {'action'} if target is scene else {'description'}
        if not repair.get('reason') or not repair.get('set') or not set(repair['set']).issubset(allowed):
            raise ValueError('Continuity repair may only reconcile staging descriptions')
        target.update(repair['set'])
    characters = {character['name']: character for character in result['characters']}
    for name, identity in plan.get('character_identity', {}).items():
        if set(identity) != {'unique_features', 'accessories', 'excluded_entity_ids'}:
            raise ValueError('Invalid character identity repair')
        if not all(isinstance(identity[field], list) for field in identity):
            raise ValueError('Invalid character identity lists')
        if any(ident not in {e['id'] for e in plan['entities'] if e['kind'] == 'prop'}
               for ident in identity['excluded_entity_ids']):
            raise ValueError('Unknown temporary character prop')
        characters[name]['immutable_attributes']['unique_features'] = identity['unique_features']
        characters[name]['variable_attributes']['accessories'] = identity['accessories']
    return result


def _validate_reference_locations(plan, source):
    locations = {location['name']: location for location in source['locations']}
    for entity in plan['entities']:
        if not set(entity.get('locations', [])).issubset(locations):
            raise ValueError(f"Unknown continuity location: {entity['id']}")
        if entity['kind'] == 'landmark':
            location = locations.get(entity.get('reference_location'))
            if not location or not location.get('reference_image_path'):
                raise ValueError(f"Landmark has no canonical reference location: {entity['id']}")


def _attach_scene_facts(screenplay, plan):
    for scene in screenplay['screenplay']:
        prefix = f"{scene['scene_number']}:"
        frames = {key: value for key, value in plan['frames'].items() if key.startswith(prefix)}
        ids = {entry['id'] for frame in frames.values() for entry in frame.get('entities', [])}
        if ids:
            scene.setdefault('scene_continuity_facts', {})['continuity'] = {
                'entities': [{key: entity[key] for key in ('id', 'name', 'kind', 'world_id', 'geometry') if key in entity}
                             for entity in plan['entities'] if entity['id'] in ids],
                'frames': {key: dict(frame, entities=[
                    dict(binding, state=next(state for entity in plan['entities'] if entity['id'] == binding['id']
                                             for state in entity['states'] if state['id'] == binding['state_id']))
                    for binding in frame.get('entities', [])]) for key, frame in frames.items()},
            }


def _review_staging_repairs(base, source, plan):
    scenes = {scene['scene_number']: scene for scene in source['screenplay']['screenplay']}
    changes = []
    for repair in plan.get('repairs', []):
        original = scenes[repair['scene_number']]
        if repair.get('shot_number') is not None:
            original = next(shot for shot in original['storyboard'] if shot['shot_number'] == repair['shot_number'])
        if any(original.get(field) != value for field, value in repair['set'].items()):
            changes.append(dict(repair, before={field: original.get(field) for field in repair['set']}))
    if not changes:
        return
    selected = {repair['scene_number'] for repair in changes}
    context = _planning_source(source)
    context['screenplay']['screenplay'] = [scene for scene in context['screenplay']['screenplay']
                                         if scene['scene_number'] in selected]
    payload = dict(context, proposed_changes=changes, entities=plan['entities'],
                   frames={key: value for key, value in plan['frames'].items() if int(key.split(':')[0]) in selected})
    signature = _digest(payload)
    review = plan.get('_staging_review') or {}
    if review.get('input_hash') != signature:
        logger.info('Review staging repairs: scenes=%s changes=%s payload_chars=%s',
                    sorted(selected), len(changes), len(json.dumps(payload, ensure_ascii=False)))
        review = parse_llm_json(call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False), model=model_ultimate, temperature=0.1,
            max_tokens=2000, response_format={'type': 'json_object'},
            system_prompt=(_BOUNDARY_INSTRUCTION + 'Независимо проверь только предложенные изменения постановки до их применения. '
                'Первичные авторские материалы приоритетнее производного before: само наличие детали в before не делает её авторским требованием. '
                'Разрешены минимальные исправления, необходимые для физических состояний, общей геометрии '
                'и последних авторских уточнений. Сопоставляй before, исходные соседние шоты и результат. '
                'Не разрешай переносить монтажные переходы, добавлять места действия внутри шота, менять '
                'последовательность событий, раскрывать скрываемую личность или терять заданные сюжетные '
                'детали ради упрощения. Не начинай заново выполнять историю старых правок, если текущая '
                'постановка уже их учитывает. Сохраняй крупность и ракурс из camera_plan: наличие предмета '
                'в локации или плане состояний не требует показывать его в текущем кадре. Не разрешай '
                'добавлять предметы в кадр ради фиксации начальных состояний; исправлять надо видимость '
                'в плане, а не расширять исходную композицию. Если ошибочен план, не узаконивай его '
                'переписыванием уже согласованного исходника. '
                'Уточнение физической части объекта или необходимого события '
                'допустимо, но не разрешает соседнюю переработку сцены. Верни JSON '
                '{passed:bool,issues:[строки с точными номерами сцен/шотов и причиной]}. '
                'Пропускай только необходимые согласованные правки. Пиши по-русски.')))
        if not isinstance(review.get('passed'), bool) or not isinstance(review.get('issues'), list):
            raise ValueError('Invalid staging repair review')
        review['input_hash'] = signature
        plan['_staging_review'] = review
        _write(base / '91_screenplay/continuity_candidate.json', plan)
    if not review['passed'] or review['issues']:
        raise ValueError('Staging review rejected: ' + json.dumps(review['issues'], ensure_ascii=False))


def _repair_candidate(base, source, plan, fingerprint, review_findings=None):
    """Repair a saved model result through the pipeline, without repeating its full generation."""
    last_error = ''
    pending_feedback = bool(review_findings)
    verified_input = plan.get('_input_hash') == fingerprint or fingerprint in plan.get('_publication_sources', [])
    for attempt in range(4):
        corrected = source
        try:
            plan = _normalize_frame_keys(plan)
            corrected = _apply_repairs(source, plan)
            if verified_input:
                _review_staging_repairs(base, source, plan)
            validate_continuity_plan(plan, corrected['screenplay'])
            _validate_reference_locations(plan, corrected)
            if verified_input and not pending_feedback:
                return plan
        except (ValueError, KeyError, TypeError) as exc:
            last_error = str(exc)
        if attempt == 3:
            raise ValueError(f'Continuity candidate still invalid: {last_error}')
        staging_rejected = last_error.startswith('Staging review rejected:')
        request_error = last_error
        if pending_feedback:
            request_error += '\nReview findings are hypotheses; check them against the source: ' + json.dumps(review_findings, ensure_ascii=False)
        planning_source = _planning_source(source if staging_rejected else corrected)
        candidate_context = plan
        proposed_frames = _frame_sources(corrected['screenplay'])
        stale_events = {entity['id'] for entity in plan['entities'] for event in entity.get('transitions', [])
                        if event.get('at') in proposed_frames and
                        _normalized(event.get('evidence', '')).rstrip('.!?…') not in
                        _normalized(proposed_frames[event['at']]['shot']['description'])}
        focused = [entity for entity in plan['entities'] if re.search(
            r'(?<![\w-])' + re.escape(entity['id']) + r'(?![\w-])', request_error)
                   or entity['id'] in stale_events] if verified_input and not staging_rejected else []
        if focused:
            ids = {entity['id'] for entity in focused}
            keys = {key for key, frame in plan['frames'].items()
                    if any(binding['id'] in ids for binding in frame.get('entities', []))}
            keys.update(event['at'] for entity in focused for event in entity.get('transitions', []))
            candidate_context = dict(plan, entities=focused, frames={key: plan['frames'][key] for key in keys})
            requested = {tuple(map(int, key.split(':')[:2])) for key in keys}
            planning_source['screenplay']['screenplay'] = [dict(scene, storyboard=[
                shot for shot in scene['storyboard'] if (scene['scene_number'], shot['shot_number']) in requested])
                for scene in planning_source['screenplay']['screenplay']
                if any(scene['scene_number'] == number for number, _ in requested)]
        payload = {'source': planning_source, 'candidate': candidate_context,
                   'validation_error': request_error or 'Verify this saved candidate against current source'}
        logger.info('Continuity repair %s: entities=%s payload_chars=%s error=%s',
                    attempt + 1, len(candidate_context['entities']), len(json.dumps(payload, ensure_ascii=False)), last_error)
        response = call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            model=model_ultimate, temperature=0.1, max_tokens=8000, response_format={'type': 'json_object'},
            system_prompt=(
                _BOUNDARY_INSTRUCTION +
                'Проверь сохранённый план физической непрерывности и исправь его через минимальный JSON-патч. '
                'Последние авторские уточнения и правки приоритетнее исходного текста, производного сценария '
                'и сохранённого плана. Исправь ВСЕ ошибки цепочек состояний, не только первую ошибку валидатора. '
                'Видимость, камера, поза и иное описание той же опоры не создают нового физического состояния предмета; различие движения может требовать разных states ID даже при той же опоре. '
                'states.motion допускает только stationary или moving; описательный текст помещай в description, переходы задавай по существующим правилам. '
                'Сохраняй исходные camera_plan и композицию. Не добавляй в кадр предметы из паспорта локации '
                'или плана состояний ради их первого показа: при несовместимости с ракурсом меняй visible '
                'в плане. Исправляй ошибочный план по исходнику, а не исходник ради оправдания плана. '
                'Любой настоящий перенос или изменение состояния требует transitions с точной цитатой события из '
                'соответствующего description после repairs. Не выдумывай события ради оправдания ошибочного состояния. '
                'Если авторская правка меняет постановку, согласуй с ней и action сцены, и затронутые description '
                'шотов через repairs; одного изменения entities недостаточно. Цитата авторского требования не '
                'заменяет событие в description. '
                'Фраза, только показывающая уже достигнутое место или состояние, не обосновывает перенос '
                'между неподвижными опорами: требуется событие, которое привело к этому результату. '
                'Начальное состояние должно соответствовать первому появлению, а не предполагаемому более раннему действию. '
                'geometry описывает только постоянную конструкцию, число частей и место относительно ориентиров. '
                'Если изменяется отдельная часть составного ориентира, дай постоянные названия его частям '
                'в geometry, определи их относительно одного явно заданного опорного вида и используй те же '
                'названия в states. Повреждение, закрытие или восстановление должно относиться к одной '
                'определённой части во всех ракурсах; «одна из секций» недостаточно. Физическая ориентация '
                'опорного вида сохраняется, даже когда в обратном ракурсе меняется экранная сторона. '
                'Не помещай в geometry разбитые/уцелевшие части, осколки и другие итоги действия: это states. '
                'Согласуй физическое устройство одного ориентира с обеих сторон, без привязки к левому/правому краю экрана. '
                'Не дублируй целую базовую локацию как landmark, если уже есть её канонический референс; '
                'оставь конкретные повторяющиеся сюжетно важные части и ориентиры. '
                'Разные миры и отделимый реквизит не смешиваются с внешностью. Сохрани id, номера, хронометраж, '
                'закадровый и видимый текст; постановочные action/description согласуй при необходимости. '
                'Верни {entity_replacements:[полные исправленные entities],frame_updates:{ключ:{world_id:необязательно,'
                'entities:[{id,state_id:необязательно,visible:необязательно}]}},'
                'remove_frame_entities:{ключ:[id удаляемых из этой фазы привязок]},remove_entity_ids:[id избыточных записей]}. '
                'frame_updates обновляет только переданные поля; остальные привязки сохраняются кодом. '
                'Неизменные entities/frames НЕ повторяй. Можно дополнительно вернуть полный раздел '
                'repairs:[{scene_number,shot_number:null|номер,set:{action:строка}|{description:строка},reason:строка}] '
                'и полный character_identity, если их действительно надо изменить. '
                'Пустой патч допустим только если план и постановочные описания согласованы с последними '
                'авторскими правками и содержат события, обосновывающие все изменения состояния. '
                'Ключи фаз только вида 1:1:start или 1:1:end.'),
        )
        patch = parse_llm_json(response)
        entities = {entity['id']: entity for entity in plan['entities']}
        entities.update({entity['id']: entity for entity in patch.get('entity_replacements', [])})
        removed = set(patch.get('remove_entity_ids', []))
        entities = {ident: entity for ident, entity in entities.items() if ident not in removed}
        plan['entities'] = list(entities.values())
        for key, update in patch.get('frame_updates', {}).items():
            frame = plan['frames'].setdefault(key, {})
            bindings = {binding['id']: binding for binding in frame.get('entities', [])}
            for binding in update.get('entities', []):
                bindings[binding['id']] = dict(bindings.get(binding['id'], {}), **binding)
            frame.update({field: value for field, value in update.items() if field != 'entities'})
            frame['entities'] = list(bindings.values())
        for key, ids in patch.get('remove_frame_entities', {}).items():
            plan['frames'][key]['entities'] = [binding for binding in plan['frames'][key].get('entities', [])
                                             if binding['id'] not in ids]
        for frame in plan['frames'].values():
            frame['entities'] = [binding for binding in frame.get('entities', []) if binding['id'] not in removed]
        for field in ('repairs', 'character_identity'):
            if field in patch:
                plan[field] = patch[field]
        plan['_input_hash'] = fingerprint
        verified_input = True
        pending_feedback = False
        _write(base / '91_screenplay/continuity_candidate.json', plan)


def _publish_plan(base, source, plan):
    corrected = _apply_repairs(source, plan)
    validate_continuity_plan(plan, corrected['screenplay'])
    _validate_reference_locations(plan, corrected)
    plan.update(version=_PLAN_VERSION, affected_shot_keys=sorted({
        ':'.join(key.split(':')[:2]).replace(':', '-')
        for key, frame in plan['frames'].items() if frame.get('entities')},
        key=lambda key: tuple(map(int, key.split('-')))))
    _attach_scene_facts(corrected['screenplay'], plan)
    middle = dict(source, screenplay=copy.deepcopy(corrected['screenplay']))
    after = copy.deepcopy(corrected)
    for document in (middle, after):
        for scene in document['screenplay']['screenplay']:
            scene.get('scene_continuity_facts', {}).pop('continuity', None)
    plan['_publication_sources'] = [_digest([_PLAN_VERSION, value]) for value in (source, middle, after)]
    _write(base / '91_screenplay/continuity_candidate.json', plan)
    _write(base / '91_screenplay/screenplay.json', corrected['screenplay'])
    _write(base / '20_bible/characters.json', corrected['characters'])
    plan['input_hash'] = _digest([_PLAN_VERSION, _source(base)])
    _write(base / '20_bible/continuity_plan.json', plan)
    return {'status': 'success', 'plan_path': str(base / '20_bible/continuity_plan.json'), 'cached': False,
            'affected_shot_keys': plan['affected_shot_keys']}


def storybook_continuity_plan_tool(session_id: str, project_id: str, enable: bool = True, review_findings: list = None) -> dict:
    """Reconcile recurring physical objects before generating any storyboard frames.

    Args:
        session_id: Pipeline execution identifier.
        project_id: Storybook project identifier.
        enable: Whether screenplay generation is enabled.
        review_findings: Actual quality findings about the current published plan; include exact entity_id for focused repair. Findings are hypotheses and the source takes priority.
    """
    if not enable:
        return {'status': 'skipped'}
    base = safe_storybook_project_dir(project_id)
    path = base / '20_bible/continuity_plan.json'
    source = _source(base)
    fingerprint = _digest([_PLAN_VERSION, source])
    existing = json.loads(path.read_text()) if path.is_file() else {}
    if existing and review_findings:
        return _publish_plan(base, source, _repair_candidate(base, source, existing, fingerprint, review_findings))
    if existing:
        if existing.get('input_hash') == fingerprint:
            validate_continuity_plan(existing, source['screenplay'])
            _validate_reference_locations(existing, source)
            screenplay = json.loads((base / '91_screenplay/screenplay.json').read_text())
            restored = copy.deepcopy(screenplay)
            _attach_scene_facts(restored, existing)
            if restored != screenplay:
                _write(base / '91_screenplay/screenplay.json', restored)
            return {'status': 'success', 'plan_path': str(path), 'cached': True,
                    'affected_shot_keys': existing['affected_shot_keys']}
    candidate_path = base / '91_screenplay/continuity_candidate.json'
    if candidate_path.is_file():
        candidate = json.loads(candidate_path.read_text())
        if (candidate.get('_input_hash') in (None, fingerprint, _digest([_PLAN_VERSION - 1, source]))
                or fingerprint in candidate.get('_publication_sources', [])):
            return _publish_plan(base, source, _repair_candidate(base, source, candidate, fingerprint))
    if existing:
        return _publish_plan(base, source, _repair_candidate(base, source, existing, fingerprint))
    response = call_openai_api(
        prompt=json.dumps(dict(_planning_source(source), existing_entities=[
            {key: entity.get(key) for key in ('id', 'name', 'kind', 'world_id', 'geometry')}
            for entity in existing.get('entities', [])]), ensure_ascii=False), model=model_ultimate, temperature=0.1,
        max_tokens=24000, response_format={'type': 'json_object'},
        system_prompt=(
            _BOUNDARY_INSTRUCTION +
            'Согласуй физическую непрерывность ВСЕГО фильма. Авторские материалы и последние авторские правки '
            'приоритетнее производных описаний. Не меняй сюжет, текст, номера, длительности, монтажные перебивки '
            'или внешность. Сохраняй id уже известных existing_entities для тех же предметов; новое состояние '
            'не создаёт новый предмет. Выдели только повторяющиеся сюжетно важные ориентиры и переносимый реквизит. '
            'Не каталогизируй весь антураж и не дублируй целые базовые локации отдельными landmarks. '
            'Выделяй конкретные повторяющиеся сюжетные ориентиры внутри них. Пиши кратко: geometry до 50 слов, '
            'description состояния до 20 слов. Один предмет имеет один id; разные виды из помещения и снаружи '
            'используют один landmark. Его geometry задаёт постоянное устройство, части и их взаимное положение, '
            'а также его точное место относительно устойчивых частей локации, а не экранные лево/право '
            'или итоговое повреждение. Состояния описывают место, опору, владельца, '
            'целостность и движение. При изменении отдельной части составного ориентира дай его постоянным '
            'частям названия в geometry относительно явно заданного опорного вида; states используют эти '
            'же названия, чтобы повреждение или закрытие не перескакивало на другую часть в обратном ракурсе. '
            'Кадрирование/исчезновение из поля зрения не перемещает предмет. '
            'При возврате после перебивок продолжай последнее состояние этого же предмета. Разделяй миры '
            'реальности, воспоминаний и фантазий: визуальное сходство не делает два предмета одним. '
            'Молчание сцены не разрешает предмету следовать за персонажем. Если производные сцены противоречат '
            'друг другу, минимально согласуй action/description с авторским замыслом и явными физическими событиями. '
            'Нельзя молча узаконивать противоречие или добавлять ключевое событие. Необходимое простое физическое '
            'следствие уже заданного действия можно уточнить в description с объяснением в repairs. '
            'Переносимый и временный реквизит не является неизменной внешностью персонажа. '
            'В character_identity дай очищенные unique_features/accessories, сохранив одежду и личность; '
            'excluded_entity_ids перечисляет только отделимый реквизит, который не должен попасть в эталон личности. '
            'Верни JSON {entities:[{id,name,kind:"landmark|prop",world_id,locations:[точные имена локаций],'
            'geometry:строка,reference_location:точное имя локации для landmark,initial_state:state_id,'
            'states:[{id,description,space,support,holder:null|имя,motion:"stationary|moving"}],'
            'transitions:[{at:"1:1:start",from:state_id,to:state_id,evidence:ТОЧНАЯ цитата '
            'из соответствующего description ПОСЛЕ repairs}]}],'
            'frames:{"1:1:start":{world_id,entities:[{id,state_id,visible:true|false}]}},'
            'repairs:[{scene_number,shot_number:null|номер,set:{action:строка}|{description:строка},reason:строка}],'
            'character_identity:{имя:{unique_features:[строки],accessories:[строки],excluded_entity_ids:[id]}}}. '
            'Ключ фазы имеет три части номер_сцены:номер_шота:start или end, например 2:3:end; без буквального '
            'префикса scene. Покрой START и END КАЖДОГО шота, даже если entities пуст. entities кадра содержит только важные '
            'для него объекты: visible=false сохраняет существенное отсутствие/удалённое положение без требования '
            'рисовать предмет в кадре. Каждый переход состояния задаётся transitions в точной фазе. Состояние '
            'сохраняется между переходами независимо от видимости. Пиши на языке исходника.'),
    )
    plan = parse_llm_json(response)
    plan['_input_hash'] = fingerprint
    _write(base / '91_screenplay/continuity_candidate.json', plan)
    return _publish_plan(base, source, _repair_candidate(base, source, plan, fingerprint))


def load_continuity_plan(base):
    path = Path(base) / '20_bible/continuity_plan.json'
    return json.loads(path.read_text()) if path.is_file() else None


def ensure_landmark_references(base, session_id, seed):
    """Use the same physical landmark master for all connected location views."""
    from concurrent.futures import ThreadPoolExecutor
    from custom_tools.image_tools import edit_image_vse_tool
    from .background_references import _reference_path

    plan = load_continuity_plan(base)
    if not plan:
        return
    locations = {entry['name']: entry for entry in json.loads((base / '20_bible/locations.json').read_text())}

    def render(entity):
        source = _reference_path(base, locations[entity['reference_location']]['reference_image_path'])
        views = ('Если объект виден из разных помещений/снаружи, покажи два согласованных вида с противоположных '
                 'сторон; это один объект с одними частями, а не две вариации дизайна. Экранный порядок '
                 'частей в обратном виде зеркален опорному; названия обозначают физические части, а не '
                 'экранные позиции. В остальном один вид. '
                 if len(entity.get('locations', [])) > 1 else
                 'Покажи ровно один согласованный вид в указанной локации. Не добавляй второй ракурс '
                 'или другую локацию. ')
        prompt = ('Создай единый нейтральный архитектурный эталон ОДНОГО физического ориентира. '
                  'Референс задаёт материалы, фактуру и художественную обработку. Постоянное устройство '
                  'из следующего описания имеет приоритет над несовместимыми деталями старого референса. '
                  'Покажи целый исходный объект, без сюжетных повреждений, персонажей и переносимого реквизита. '
                  'Включи ближайшее неизменное окружение, по которому узнаётся его точное место в локации, '
                  'а не изолированную деталь: взаимное положение этих ориентиров сохраняется во всех видах. '
                  'Ограничь кадр самим ориентиром и его ближайшими опорами; не воспроизводи общий обзор '
                  'всей локации и другие самостоятельные сюжетные ориентиры из старого референса. '
                  + views +
                  'Никаких альтернатив конструкции или декоративных добавлений. Имя: ' + entity['name'] +
                  '. Устройство: ' + entity['geometry'] +
                  '. Названия частей служат описанием геометрии; не наноси на рисунок буквы, метки и подписи.')
        related = [other for other in plan['entities'] if other['id'] != entity['id']
                   and other['world_id'] == entity['world_id']
                   and set(other.get('locations', [])) & set(entity.get('locations', []))]
        other_landmarks = [other['name'] for other in related if other['kind'] == 'landmark']
        props = [other['name'] for other in related if other['kind'] == 'prop']
        if other_landmarks:
            prompt += ('. Подбери кадрирование так, чтобы другие самостоятельные ориентиры оставались '
                       'за кадром без изменения их реального положения: ' + ', '.join(other_landmarks))
        if props:
            prompt += ('. Покажи только конструкцию без переносимого и расходуемого наполнения, в том числе: '
                       + ', '.join(props))
        if locations[entity['reference_location']].get('historical_period'):
            prompt += '. Эпоха: ' + str(locations[entity['reference_location']]['historical_period'])
            prompt += '. Согласуй все детали с указанной эпохой, даже если старый референс содержит анахронизмы.'
        digest = _digest([prompt, hashlib.sha256(source.read_bytes()).hexdigest(), seed,
                          os.getenv('IMG2IMG_MODEL') or os.getenv('VSEGPT_IMG2IMG_MODEL')])
        target = base / '20_bible/references/landmarks' / f"{entity['id']}_{digest[:12]}.png"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            edit_image_vse_tool(session_id=session_id, prompt=prompt, image_paths=[str(source)],
                                output_path=str(target), width=1920, height=1088, seed=seed)
            if not target.is_file():
                raise RuntimeError(f'Landmark reference missing: {target}')
        entity['reference_path'] = str(target.resolve())

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(render, [entity for entity in plan['entities'] if entity['kind'] == 'landmark']))
    _write(base / '20_bible/continuity_plan.json', plan)


def identity_reference_path(base, character, session_id, seed):
    """Normalize a legacy identity sheet once, keeping temporary props out of it."""
    from custom_tools.image_tools import edit_image_vse_tool
    from .background_references import _reference_path

    plan = load_continuity_plan(base)
    identity = (plan or {}).get('character_identity', {}).get(character['name'], {})
    excluded = set(identity.get('excluded_entity_ids', []))
    # Cleaned text can omit the old sheet's props on a later planning run.
    # The physical registry still identifies which props this character handles.
    excluded.update(entity['id'] for entity in (plan or {}).get('entities', [])
                    if entity['kind'] == 'prop' and any(
                        state.get('holder') == character['name'] for state in entity['states']))
    source = _reference_path(base, character['reference_image_path'])
    if not excluded:
        return str(source)
    names = [entity['name'] for entity in plan['entities'] if entity['id'] in excluded]
    prompt = ('Создай нейтральный эталон только внешности этого персонажа: лицо, пропорции, '
              'причёска и надетая на тело одежда сохраняются. Спокойная стойка, свободные руки, '
              'однотонная бумажная основа. Без сюжетных действий или игровых поз '
              'из исходного листа. Переносимые предметы вокруг героя и в его руках исключены: ' +
              ', '.join(names) + '. Надетую одежду и естественные части тела не удаляй. '
              'Сохрани художественную технику исходного изображения.')
    digest = _digest([prompt, hashlib.sha256(source.read_bytes()).hexdigest(), seed,
                      os.getenv('IMG2IMG_MODEL') or os.getenv('VSEGPT_IMG2IMG_MODEL')])
    target = base / '20_bible/references/characters' / f'identity_{digest[:16]}.png'
    if not target.is_file():
        edit_image_vse_tool(session_id=session_id, prompt=prompt, image_paths=[str(source)],
                            output_path=str(target), width=1920, height=1088, seed=seed)
        if not target.is_file():
            raise RuntimeError(f'Clean character identity missing: {target}')
    return str(target.resolve())


def frame_continuity(base, scene, shot, phase):
    plan = load_continuity_plan(base)
    if not plan:
        return {}
    key = f'{scene}:{shot}:{phase}'
    frame = plan['frames'][key]
    if not frame.get('entities'):
        return {}
    entities = {entity['id']: entity for entity in plan['entities']}
    selected, states = [], {}
    for binding in frame['entities']:
        entity = entities[binding['id']]
        selected.append({key: entity[key] for key in ('id', 'name', 'kind', 'world_id', 'geometry', 'reference_path') if key in entity})
        if entity['kind'] != 'landmark':
            selected[-1].pop('reference_path', None)
        state = next(state for state in entity['states'] if state['id'] == binding['state_id'])
        states[entity['id']] = dict(state, visible=binding['visible'])
    return {'world_id': frame['world_id'], 'entities': selected, 'states': states,
            'landmark_reference_paths': [entity['reference_path'] for entity in selected
                                         if entity['kind'] == 'landmark' and entity.get('reference_path')]}


def bind_continuity_to_items(base, items):
    if not load_continuity_plan(base):
        return items
    return [dict(item, _continuity={phase: frame_continuity(base, item['scene_number'], item['shot_number'], phase)
                                   for phase in ('start', 'end')}) for item in items]


def unit_continuity_hash(base, shot_keys):
    contexts = {f'{key}:{phase}': frame_continuity(base, *map(int, key.split('-')), phase)
                for key in shot_keys for phase in ('start', 'end')}
    contexts = {key: context for key, context in contexts.items() if context}
    if not contexts:
        return None
    refs = {path for context in contexts.values() for path in context['landmark_reference_paths']}
    return _digest([contexts, {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in sorted(refs)}])


def image_continuity_hash(item):
    if not item.get('project_id') or item.get('scene_number') is None:
        return None
    base = safe_storybook_project_dir(item['project_id'])
    context = frame_continuity(base, item['scene_number'], item['shot_number'], item.get('shot_type', 'start'))
    if not context:
        return None
    from .background_references import _reference_path
    paths = list(item.get('reference_image_paths') or []) + context['landmark_reference_paths']
    if item.get('background_reference'):
        paths.append(item['background_reference']['sheet_path'])
    paths += [item[key] for key in ('source_end_path', 'image_path', 'base_image_path') if item.get(key)]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for value in paths
              for path in [_reference_path(base, value)] if path.is_file()}
    return _digest([context, item.get('english_prompt'), item.get('negative_prompt'),
                    item.get('background_reference'), hashes])


def current_continuity_image(item):
    fingerprint = image_continuity_hash(item)
    if fingerprint is None:
        return True
    path = Path(item['output_path']).with_suffix('.continuity.json')
    return path.is_file() and json.loads(path.read_text()).get('input_hash') == fingerprint


def record_continuity_image(item, basis='generation'):
    fingerprint = image_continuity_hash(item)
    if fingerprint is not None:
        _write(Path(item['output_path']).with_suffix('.continuity.json'), {'input_hash': fingerprint, 'basis': basis})
