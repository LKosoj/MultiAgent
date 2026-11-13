"""Prepare spatial background cards before rendering storyboard frames."""
import contextvars
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from agent_command import model_ultimate
from custom_tools.image_tools import edit_image_vse_tool, generate_image_tool
from custom_tools.storybook.project_paths import existing_reference_path, safe_storybook_project_dir, reference_project_dir
from custom_tools.storybook.reference_catalog import reuse_reference, store_reference
from utils import call_openai_api, parse_llm_json

_BACKGROUND_REFERENCE_PLAN_VERSION = 22


def _validate_background_plan_content(plan, frames):
    props = {entity['id'] for frame in frames.values()
             for entity in (frame.get('continuity') or {}).get('entities', [])
             if entity.get('kind') == 'prop'}
    errors = []
    for space in plan.get('spaces', []):
        texts = [('layout', space.get('layout', ''))] + [
            (card.get('frame_key', 'card'), card.get('description', '')) for card in space.get('cards', [])]
        for key, text in texts:
            for ident in sorted(props):
                if re.search(r'(?<![\w-])' + re.escape(ident) + r'(?![\w-])', text):
                    errors.append(f'{key}: portable prop {ident} cannot be part of a background')
    if errors:
        raise ValueError('; '.join(errors))


def _inherit_spatial_layout(plan, payload):
    for space in plan.get('spaces', []):
        names = sorted({name for card in space.get('cards', [])
                        for name in payload['frames'].get(card['frame_key'], {}).get('locations', [])})
        layouts = [(name, payload['locations'][name].get('spatial_layout')) for name in names]
        if layouts and all(layout for _, layout in layouts):
            space['layout'] = '\n\n'.join(f'{name}: {layout}' for name, layout in layouts)


def _review_background_plan(plan, payload):
    response = call_openai_api(
        prompt=json.dumps({'source': payload, 'candidate': plan}, ensure_ascii=False),
        system_prompt=(
            'Проверь готовый план фонов по исходным данным. Не переписывай план. '
            'Верни JSON {"issues": ["ключ кадра/пространство: конкретное противоречие исходным данным"]}. '
            'Если подтверждённых ошибок нет, issues пуст. '
            'Проверяй: одно физическое место не разделено на пространства только из-за смены крупности, '
            'участка или состояния; разные реальные места не объединены. '
            'В описаниях фона нет персонажей, их частей тела, одежды и переносимого реквизита, '
            'даже если автор не написал их id. Сопоставь упомянутые предметы с continuity.entities: '
            'kind=prop нельзя описывать даже для объяснения кадрирования, масштаба или источника света. '
            'Отдельные ориентиры kind=landmark разрешены. '
            'styles задаёт актуальную художественную манеру. Её применяют отдельно при рендере: '
            'в описаниях фона не должно быть указаний художественной манеры, особенно противоречащих styles. '
            'Макро основного объекта без самостоятельного окружения должно быть в skipped. '
            'Координаты объектов в карточках совместимы с locations.spatial_layout и выбранной камерой; '
            'сценарий может явно переместить только названный объект, но не остальные. '
            'Проверь соответствие фазам, видимую конструкцию и неизменность проекции неподвижной камеры. '
            'Не требуй показывать скрытое или находящееся за границами кадра. '
            'Указывай только доказуемые ошибки, не предпочтения и не новые требования.'
        ),
        model=model_ultimate, max_tokens=4000, temperature=0.1,
        response_format={'type': 'json_object'},
    )
    review = parse_llm_json(response)
    if not isinstance(review.get('issues'), list):
        raise ValueError('Background plan review did not return issues')
    if review['issues']:
        raise ValueError('Background plan review: ' + '; '.join(map(str, review['issues'])))


def _landmark_state_signature(frame):
    continuity = frame.get("continuity") or {}
    states = continuity.get("states") or {}
    return tuple(sorted((entity["id"], states[entity["id"]]["id"])
                        for entity in continuity.get("entities", [])
                        if entity.get("kind") == "landmark"))


def _group_identical_background_views(spaces, frames):
    result = []
    for space in spaces:
        cards = {}
        for card in space.get("cards") or []:
            description = card["description"]
            key = (description, frames.get(card["frame_key"], {}).get("style_id"),
                   _landmark_state_signature(frames.get(card["frame_key"], {})))
            if key not in cards:
                cards[key] = {"frame_keys": [], "description": description}
            cards[key]["frame_keys"].append(card["frame_key"])
        result.append(dict(space, cards=list(cards.values())))
    return result


def _location_planning_inputs(payload):
    groups = []
    for key, frame in payload["frames"].items():
        names = set(frame["locations"])
        matches = [group for group in groups if names & group["locations"] or not names and not group["locations"]]
        if matches:
            group = matches[0]
            for other in matches[1:]:
                group["frames"].update(other["frames"])
                group["locations"].update(other["locations"])
                groups.remove(other)
            group["frames"][key] = frame
            group["locations"].update(names)
        else:
            groups.append({"frames": {key: frame}, "locations": names})
    inputs = []
    for group in groups:
        requested = {tuple(map(int, key.split(":")[:2])) for key in group["frames"]}
        scenes = [dict(scene, storyboard=[shot for shot in scene.get("storyboard") or []
                  if (scene["scene_number"], shot["shot_number"]) in requested])
                  for scene in payload["screenplay"] if any(number == scene["scene_number"] for number, _ in requested)]
        inputs.append({"screenplay": scenes, "frames": group["frames"],
                       "locations": {name: location for name, location in payload["locations"].items() if name in group["locations"]},
                       "styles": {key: value for key, value in payload.get('styles', {}).items()
                                  if key in {frame['style_id'] for frame in group['frames'].values() if 'style_id' in frame}},
                       "language": payload["language"]})
    return inputs


def _keep_accepted_cards(plan, candidate, error):
    """Модель переписывает план целиком и ломает принятые карточки: без замечаний они остаются прежними."""
    accepted = {card.get("frame_key"): card.get("description")
                for space in candidate.get("spaces") or [] if isinstance(space, dict)
                for card in space.get("cards") or [] if isinstance(card, dict)}
    flagged = {key for key in accepted if key and re.search(rf"(?<![\w:]){re.escape(key)}(?!\w)", error)}
    if not flagged:
        return
    for space in plan["spaces"]:
        for card in space.get("cards") or [] if isinstance(space, dict) else []:
            key = card.get("frame_key") if isinstance(card, dict) else None
            if key in accepted and key not in flagged:
                card["description"] = accepted[key]


def _frame_key(item):
    return f"{item['scene_number']}:{item['shot_number']}:{item['shot_type']}"


def _reference_candidate(base, value):
    """(путь-кандидат, разрешённый путь): кандидат — что было бы до переноса в общий каталог."""
    value = str(value or "")
    from .reference_catalog import _enabled, _sid, bootstrap_series_references, resolve_reference
    bootstrap_series_references(base)
    if value.lstrip("/").startswith("references/"):
        common = reference_project_dir(base) / "20_bible" / value.lstrip("/")
        legacy = base / "20_bible" / value.lstrip("/")
        if _enabled(base) and _sid(base):
            relative = Path(value.lstrip('/')).relative_to('references')
            output = reference_project_dir(base) / '20_bible/references/_series' / _sid(base) / relative
            for candidate in (output, common, legacy):
                try:
                    resolved = resolve_reference(base, candidate)
                except ValueError:
                    continue
                if resolved.is_file():
                    return candidate, resolved
            # A different series' reference with this filename is not an input.
            return output, output
        for candidate in (common, legacy):
            resolved = resolve_reference(base, candidate)
            if resolved.is_file():
                return candidate, resolved
        path = common
    else:
        path = Path(value)
    return path, resolve_reference(base, path)


def _reference_path(base, value):
    return _reference_candidate(base, value)[1]


def _build_background_sheets(spaces, frames, styles, output_dir, skipped=None):
    covered = set()
    for space in spaces:
        for card in space.get("cards") or []:
            keys = card.get("frame_keys") or []
            if not keys or any(key not in frames or key in covered for key in keys) or len(keys) != len(set(keys)):
                # Без конкретных ключей планировщик исправляет привязку вслепую и повторяет ошибку.
                unknown = sorted({key for key in keys if key not in frames})
                repeated = sorted({key for key in keys if key in covered or keys.count(key) > 1})
                raise ValueError("Background card must bind existing frames exactly once: "
                                 f"not in INPUT.frames {unknown}, bound more than once {repeated}")
            covered.update(keys)
    for entry in skipped or []:
        key = entry["frame_key"]
        if key not in frames or key in covered or not str(entry.get("reason") or "").strip():
            raise ValueError("Background skip must identify an uncovered frame and its reason")
        covered.add(key)
    if covered != set(frames):
        raise ValueError(f"Background plan coverage is incomplete: {sorted(set(frames) - covered)}")
    sheets, bindings = [], {}
    for space_number, space in enumerate(spaces, 1):
        groups = {}
        for card in space.get("cards") or []:
            split = {}
            for key in card["frame_keys"]:
                signature = _landmark_state_signature(frames[key])
                split.setdefault(signature, []).append(key)
            for signature, keys in split.items():
                groups.setdefault(signature, []).append(dict(card, frame_keys=keys))
        chunks = [(signature, cards[offset:offset + 12]) for signature, cards in groups.items()
                  for offset in range(0, len(cards), 12)]
        space_id = f"space_{space_number:02d}"
        previous_sheet = None
        previous_signature = None
        for sheet_number, (signature, chunk) in enumerate(chunks, 1):
            if signature != previous_signature:
                previous_sheet = None
            previous_signature = signature
            path = str(output_dir / f"{space_id}_sheet_{sheet_number:02d}.png")
            descriptions, used_styles, locations = [], {}, []
            for card_number, card in enumerate(chunk, 1):
                keys = card["frame_keys"]
                first_frame = frames[keys[0]]
                style_id = first_frame["style_id"]
                used_styles[style_id] = styles[style_id]["prompt"]
                descriptions.append(f"Карточка {card_number}; стиль {style_id}: {card['description']}")
                for key in keys:
                    bindings[key] = {"sheet_path": path, "card_number": card_number}
                    for location in frames[key]["locations"]:
                        if location not in locations:
                            locations.append(location)
            columns = min(4, math.ceil(math.sqrt(len(chunk))))
            rows = math.ceil(len(chunk) / columns)
            frame_width, frame_height = first_frame['width'], first_frame['height']
            divisor = math.gcd(frame_width, frame_height)
            aspect = f'{frame_width // divisor}:{frame_height // divisor}'
            canvas_width, canvas_height = columns * frame_width, rows * frame_height
            scale = max(frame_width, frame_height) / max(canvas_width, canvas_height)
            canvas_width, canvas_height = round(canvas_width * scale), round(canvas_height * scale)
            landmarks = {entity['id']: entity for card in chunk for key in card['frame_keys']
                         for entity in (frames[key].get('continuity') or {}).get('entities', [])
                         if entity['kind'] == 'landmark'}
            prompt = (
                f"Создай лист из {len(chunk)} пронумерованных карточек фона: {columns} столбца, {rows} ряда, "
                "в порядке слева направо, сверху вниз. Незаполненные ячейки последнего ряда оставь пустыми. "
                f"Внутри каждой карточки один кинематографический вид {aspect}, "
                f"общий лист {canvas_width}×{canvas_height}. Формат отдельной карточки не является форматом всего листа; "
                "номер расположен на внешнем поле. Только фоны, без персонажей и переносимого ими реквизита. "
                "Лист показывает только ОДНО конкретное физическое пространство. Сохрани его устройство; "
                "каждой карточке соответствует своя проекция камеры, свет и состояние среды. "
                "Референсы задают устройство места, но не отменяют состояние, указанное в каждой карточке. "
                "Текстовый пространственный план задаёт взаимное расположение ориентиров. Не копируй случайные "
                "соседние объекты с фона эталона отдельного ориентира и не меняй взаимное расположение ориентиров. "
                "Описание карточки задаёт кадрирование и масштаб. Сохраняй состав, число и взаимное расположение "
                "постоянных частей конструкции, попадающих в её поле зрения; не расширяй вид ради показа всего места. "
                "Повреждение одной части не удаляет её опоры, разделители или соседние секции; изменяй только затронутую часть. "
                "При неизменной камере состояние среды не меняет масштаб, кадрирование или устройство окружения. "
                "Не переноси позднее состояние среды в ранний кадр или наоборот. Внешние служебные надписи — только номера карточек. "
                f"Место: {space.get('name', space_id)}. Пространственный контекст (не перечень видимого): {space.get('layout', '')}\n\n"
                + "\n".join(descriptions)
                + "\n\nГотовые стили (не надписи на изображении):\n"
                + "\n".join(f"{key}: {value}" for key, value in used_styles.items())
            )
            if landmarks:
                prompt += '\n\nОбщие ориентиры (одни и те же физические объекты во всех ракурсах):\n' + '\n'.join(
                    f"{ident}: {entity['geometry']}" for ident, entity in landmarks.items())
                prompt += ('\nЭталоны этих ориентиров задают неизменную конструкцию; старые общие виды '
                           'локации дополняют окружение и не заменяют ориентир другим похожим объектом. '
                           'Меняй только состояние материала, явно заданное конкретной карточкой.')
            if previous_sheet:
                prompt += (
                    "\n\nImage 1 — ПРЕДЫДУЩИЙ лист этого же пространства. Создай продолжение его каталога видов, "
                    "а не повтор карточек; сохраняй общую пространственную структуру. "
                    "Текущее состояние среды берётся из описания каждой новой карточки, а не наследуется с прошлого листа. "
                    "На новом листе номера снова начинаются с 1. Остальные референсы показывают исходное устройство мест."
                )
            sheets.append({
                "space_id": space_id, "space_name": space.get("name", space_id),
                "sheet_number": sheet_number, "path": path,
                "card_count": len(chunk), "prompt": prompt, "locations": locations,
                "previous_sheet": previous_sheet,
                "width": canvas_width, "height": canvas_height,
                "negative_prompt": "персонажи, люди, водяные знаки, логотипы",
                "landmark_reference_paths": list(dict.fromkeys(entity['reference_path'] for entity in landmarks.values()
                                                               if entity.get('reference_path'))),
                "landmark_reference_roles": [dict(id=ident, name=entity.get('name', ident),
                                                   path=entity['reference_path'])
                                             for ident, entity in landmarks.items() if entity.get('reference_path')],
            })
            previous_sheet = path
    return sheets, bindings


def storybook_background_plan_tool(session_id: str, project_id: str, shots_data: Optional[dict] = None, language: str = "ru", enable: bool = True) -> dict:
    """Plan reusable background views and deterministic frame/card bindings without rendering images.

    Args:
        session_id: Execution trace identifier.
        project_id: Storybook project identifier.
        shots_data: Checked frame tasks; defaults to the saved shots.
        language: Project language code.
        enable: Whether to prepare background references.
    """
    base = safe_storybook_project_dir(project_id)
    shots_path = base / "97_shots" / "shots.json"
    if not enable:
        return shots_data or {}
    if shots_data is None:
        shots_data = json.loads(shots_path.read_text(encoding="utf-8"))
    elif isinstance(shots_data, str):
        shots_data = json.loads(shots_data)
    items = sorted(shots_data.get("items") or [], key=lambda item: (
        int(item["scene_number"]), int(item["shot_number"]), item["shot_type"] != "start",
    ))
    if not items:
        return shots_data
    screenplay = json.loads((base / "91_screenplay" / "screenplay.json").read_text(encoding="utf-8"))
    catalog = json.loads((base / "30_style" / "image_styles.json").read_text(encoding="utf-8"))
    from .style_keeper import resolve_image_style_id
    from .continuity import frame_continuity
    locations = json.loads((base / "20_bible" / "locations.json").read_text(encoding="utf-8"))
    locations_by_name = {location["name"]: location for location in locations}
    frames = {}
    for item in items:
        phase = (item.get("_shot_frame_spec") or {}).get(f"{item['shot_type']}_state_spec") or {}
        transition = (item.get("_shot_frame_spec") or {}).get("transition_spec") or {}
        frames[_frame_key(item)] = {
            "camera_plan": item.get("camera_plan"), "camera_anchor": phase.get("camera_anchor"),
            "camera_position": item.get("camera_position"),
            "final_camera_position": item.get("final_camera_position"),
            "final_shot_size": item.get("final_shot_size"),
            "framing_delta_percent": item.get("framing_delta_percent"),
            "subject_scale_ratio": item.get("subject_scale_ratio"),
            "environment_state": (phase.get("world_physics") or {}).get("surface_state"),
            "support": (phase.get("world_physics") or {}).get("support_state"),
            "visibility_constraints": (phase.get("world_physics") or {}).get("occlusion_constraints") or [],
            "camera_delta": transition.get("camera_delta"),
            "environment_delta": transition.get("environment_delta") or [],
            "locations": [location["name"] for location in item.get("locations") or []],
            "style_id": resolve_image_style_id(catalog, _frame_key(item), item.get("locations"), item.get("style_id")),
            "width": int(item.get("width") or 1920), "height": int(item.get("height") or 1088),
            "continuity": frame_continuity(base, item['scene_number'], item['shot_number'], item['shot_type']),
        }
    source = [{key: scene.get(key) for key in (
        "scene_number", "location_time", "action", "scene_continuity_facts", "storyboard",
    )} for scene in screenplay["screenplay"]]
    used_locations = {name: locations_by_name[name] for frame in frames.values() for name in frame["locations"]}
    used_styles = {frame["style_id"]: catalog["styles"][frame["style_id"]] for frame in frames.values()}
    reference_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for name, location in used_locations.items()
        for path in [_reference_path(base, location.get("reference_image_path"))]
    }
    for frame in frames.values():
        for path in frame['continuity'].get('landmark_reference_paths', []):
            reference_hashes[path] = hashlib.sha256(Path(existing_reference_path(base, path)).read_bytes()).hexdigest()
    payload = {"screenplay": source, "frames": frames, "locations": used_locations,
               "styles": used_styles, "language": language}
    input_hash = hashlib.sha256(json.dumps(
        [_BACKGROUND_REFERENCE_PLAN_VERSION, payload, used_styles, reference_hashes], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    manifest_path = base / "20_bible" / "references" / "background_references.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    if manifest.get("input_hash") != input_hash:
        output_dir = reference_project_dir(base) / "20_bible" / "references" / "background_references" / input_hash[:12]
        planning_inputs = _location_planning_inputs(payload)

        def plan_location(index_and_payload):
            index, local_payload = index_and_payload
            plan_path = output_dir / "plans" / f"location_{index:02d}.json"
            if plan_path.is_file():
                return json.loads(plan_path.read_text(encoding="utf-8"))
            from .video_generator_aitunnel_media import _normalize_image_reference
            reference_items = [
                {"scene_number": "REFERENCE", "shot_number": number, "shot_type": name,
                 "output_path": str(_reference_path(base, location.get("reference_image_path")))}
                for number, (name, location) in enumerate(local_payload["locations"].items(), 1)
                if _reference_path(base, location.get("reference_image_path")).is_file()
            ]
            landmarks = {entity['id']: entity for frame in local_payload['frames'].values()
                         for entity in (frame.get('continuity') or {}).get('entities', [])
                         if entity['kind'] == 'landmark' and entity.get('reference_path')}
            reference_items += [dict(scene_number='LANDMARK', shot_number=ident,
                                     shot_type='only this named object construction; surrounding objects are not a spatial layout',
                                     output_path=entity['reference_path']) for ident, entity in landmarks.items()]
            request = dict(local_payload)
            errors = []
            image_url = (_normalize_image_reference(reference_items[0]["output_path"])
                         if len(reference_items) == 1 else None)
            for attempt in range(3):
                request_content = json.dumps(request, ensure_ascii=False)
                if len(reference_items) > 1:
                    request_content = [{"type": "text", "text": request_content}]
                    for reference in reference_items:
                        label = f"{reference['scene_number']} {reference['shot_number']} {reference['shot_type']}"
                        request_content.extend([
                            {"type": "text", "text": label},
                            {"type": "image_url", "image_url": {"url": _normalize_image_reference(reference['output_path'])}},
                        ])
                response = call_openai_api(
                    prompt=request_content,
                    system_prompt=(
                        "Если locations.spatial_layout задан, наследуй его дословно в layout, без дополнений из фотографий. "
                        "В карточках описывай его проекцию для выбранной камеры, не меняя координат объектов. "
                        "Если пространственный план не задан, восстанови его по приложенным эталонам. "
                        "locations.spatial_layout и исходный план задают единую систему координат пространства. "
                        "Указания слева/справа из описаний отдельных ракурсов относятся к их камере: переведи их в "
                        "систему исходного плана, прежде чем включать в общий layout. Не склеивай противоречащие "
                        "положения одного элемента; при конфликте с общим описанием сохраняй явно заданный spatial_layout. "
                        "Проверь layout и все карточки на взаимную непротиворечивость до ответа. "
                        "continuity содержит общие идентификаторы и фиксированное устройство ориентиров. "
                        "Эталоны LANDMARK и их geometry задают конструкцию и явно заданную сценарием расстановку "
                        "своего объекта, но не меняют устройство остального пространства из locations.spatial_layout. "
                        "При расхождении изображения LANDMARK с его geometry следуй geometry. "
                        "Каждый LANDMARK задаёт только конструкцию своего именованного объекта; другие объекты "
                        "на его фоне не являются эталонами. Содержимое и изменяемое состояние задаёт текущая фаза. "
                        "Один id обозначает один физический объект с любой стороны границы; не придумывай другую конструкцию. "
                        "Для составных постоянных элементов укажи в layout, на сколько частей они разделены, какими опорами и разделителями, как части расположены. "
                        "Для видимого деления явно укажи число частей; общая фраза «конструкция сохранена» не заменяет описание. "
                        "Подготовь каталог уникальных осмысленных видов окружения для переданной локации и всех её появлений в сценарии, с привязками кадров. "
                        "Группируй виды по конкретному физическому пространству, "
                        "а не по порядку шотов и не по общим категориям интерьер/экстерьер. "
                        "Смена функции участка, точки камеры или крупности не создаёт нового пространства; "
                        "разделяй только действительно отдельные места, явно следующие из исходных данных. "
                        "Зоны и участки из описания одной локации остаются одним пространством. "
                        "Одно место сохраняет одну группу при повторных визитах и продолжении сквозного движения "
                        "после несоседних сцен, фантазий или перебивок; разные места не смешиваются. "
                        "Внутри пространства располагай карточки в связном пространственном порядке видов, а не хронологии фильма. "
                        "Сформируй отдельную запись для КАЖДОГО входного кадра. Если вид действительно не изменился, "
                        "повтори его описание БУКВАЛЬНО: код сам объединит совпадения внутри одного пространства. "
                        "Новый вид нужен при существенной смене проекции, геометрии, материала или освещения самой среды. "
                        "Изменения только позы, видимости или освещения героя, частицы и следы движения не создают новый вид: "
                        "повтори описание общих устойчивых фактов обеих фаз БУКВАЛЬНО, не фиксируя такие фазовые детали в фоне. "
                        "camera_delta и environment_delta задают переход между START и END: при изменении проекции фона "
                        "нужны разные карточки двух фаз, даже если масштаб и экранные позиции героев не меняются. "
                        "Проверенные camera_anchor каждой фазы и camera_delta определяют проекцию; производные final_shot_size, "
                        "framing_delta_percent и subject_scale_ratio не могут отменять эти указания. Если камера неподвижна, "
                        "сохрани одинаковые ракурс, масштаб и положение ориентиров START и END; изменение состояния среды само по себе не означает зум или смену крупности. "
                        "Крупность camera_plan считай относительно главного объекта camera_anchor и его опоры support, "
                        "а не относительно всей локации: фон показывает окружение этого места на указанном масштабе. "
                        "Сам camera_anchor и support определяют точку и масштаб съёмки, но не становятся фоном, "
                        "если принадлежат персонажу или kind=prop. Не упоминай их в описании карточки даже "
                        "для объяснения кадрирования, масштаба или источника света. "
                        "support задаёт физический уровень и масштаб кадрирования, но не требует рисовать персонажа. "
                        "Нельзя назначать одну карточку двум различным видам, описав внутри неё само движение или параллакс: "
                        "опиши отдельный неподвижный вид каждой фазы с её положением ориентиров. "
                        "Каждый входной кадр либо имеет одну запись с frame_key, либо явно указан в skipped с причиной: "
                        "например, предметный макроплан или абстракция без осмысленного отдельного окружения. "
                        "Макроплан основного объекта без самостоятельно различимого окружения отправляй в skipped: "
                        "поверхность самого предмета и его надписи не являются отдельным фоном. "
                        "Опиши только вид фона: положение камеры, перспективу и видимые ориентиры, свет и состояние среды. "
                        "Не добавляй персонажей, переносимый ими реквизит, сюжетную экспликацию или художественный стиль. "
                        "Актуальная художественная манера задаётся styles по style_id кадра и применяется отдельно "
                        "при рендере; не переноси указания манеры из старого screenplay в описания карточек. "
                        "Не описывай даже частичные фигуры, силуэты или части тела. Если visibility_constraints задают "
                        "перекрытие персонажем, опиши сам фон без этого персонажа; устойчивые перекрытия объектами среды сохрани. "
                        "Каждая карточка фиксирует одно неподвижное состояние выбранного момента, а не цепочку действий. "
                        "В layout опиши только постоянное устройство места и заданную locations эпоху; "
                        "не помещай туда итоговые события или меняющиеся состояния. "
                        "Приложенные изображения — эталоны локаций; при нескольких локациях они подписаны REFERENCE и именами. "
                        "При отсутствии locations.spatial_layout перечисли по ним в layout видимые постоянные части, "
                        "их число и взаимное расположение. Общие виды задают полную конструкцию, крупные фрагменты её дополняют: "
                        "обрезка не означает отсутствия остальных частей. Повреждение материала не открывает, не сдвигает и не удаляет "
                        "конструкцию, опоры или разделители без отдельного события в сценарии. В КАЖДОЙ карточке назови видимые сохраняемые "
                        "части из layout и их неизменное положение рядом с описанием повреждённого материала. "
                        "Не заменяй конкретное состояние материала словами «открытый проём» или «объект исчез», "
                        "если такие операции не заданы сценарием. Неоднозначный environment_state уточняй по исходному storyboard. "
                        "Состояние каждой карточки бери из environment_state/visibility_constraints именно её фазы, "
                        "согласуя со сценарием; не переносить финал действия в START или поздний вид на ранние кадры. "
                        "Привязывай ТОЛЬКО ключи из INPUT.frames; остальные сцены являются контекстом. "
                        "Не разбивай пространства на листы и не назначай номера: это сделает код. "
                        "Количество видов одного пространства не ограничено. Крупные планы его участков остаются в том же пространстве. "
                        "Описание карточки — краткое задание одного фона, не более 50 слов. "
                        "Верни JSON: {\"spaces\":[{\"name\":\"...\",\"layout\":\"...\","
                        "\"cards\":[{\"frame_key\":\"1:1:start\",\"description\":\"...\"}]}],"
                        "\"skipped\":[{\"frame_key\":\"1:2:start\",\"reason\":\"...\"}]}. "
                        "Если переданы candidate и validation_error, исправь ошибку и верни полный план. "
                        "previous_validation_errors — замечания прошлых попыток: ни одно из них не должно повториться. "
                        "Исправь КАЖДОЕ замечание во ВСЕХ названных кадрах: фразы про персонажей, части тела, "
                        "одежду или реквизит удали целиком, не перефразируя их присутствие. Если замечание требует "
                        "skipped, перенеси кадр из cards в skipped с причиной. Остальной корректный план сохрани. "
                        "При ошибке переносимого реквизита исключи сам предмет и части тела из фона, а не только "
                        "его идентификатор; при отсутствии самостоятельного фона отправь кадр в skipped. "
                        "Пиши описания на языке проекта."
                    ),
                    model=model_ultimate, max_tokens=24000, temperature=0.1,
                    response_format={"type": "json_object"},
                    image_url=image_url,
                )
                plan = parse_llm_json(response)
                if errors and isinstance(plan.get("spaces"), list):
                    _keep_accepted_cards(plan, request["candidate"], errors[-1])
                try:
                    if not isinstance(plan.get("spaces"), list) or not isinstance(plan.get("skipped"), list):
                        raise ValueError("Background planner did not return spaces and skipped frames")
                    _inherit_spatial_layout(plan, local_payload)
                    _validate_background_plan_content(plan, local_payload["frames"])
                    spaces = _group_identical_background_views(plan["spaces"], local_payload["frames"])
                    _build_background_sheets(spaces, local_payload["frames"], catalog["styles"], output_dir, plan["skipped"])
                    _review_background_plan(plan, local_payload)
                except ValueError as exc:
                    logging.getLogger(__name__).warning('Background plan attempt %s rejected: %s', attempt + 1, exc)
                    if attempt == 2:
                        raise
                    # План пишется заново целиком: без прошлых замечаний исправленные ошибки возвращаются.
                    request = dict(local_payload, candidate=plan, validation_error=str(exc))
                    if errors:
                        request['previous_validation_errors'] = list(errors)
                    errors.append(str(exc))
                    continue
                plan["spaces"] = spaces
                break
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            return plan

        with ThreadPoolExecutor(max_workers=4) as executor:
            plans = list((future.result() for future in [executor.submit(contextvars.copy_context().run, plan_location, value) for value in enumerate(planning_inputs, 1)]))
        plan = {"spaces": [space for result in plans for space in result["spaces"]],
                "skipped": [entry for result in plans for entry in result["skipped"]]}
        sheets, bindings = _build_background_sheets(plan["spaces"], frames, catalog["styles"], output_dir, plan["skipped"])
        manifest = {"version": _BACKGROUND_REFERENCE_PLAN_VERSION, "input_hash": input_hash,
                    "spaces": plan["spaces"], "skipped": plan["skipped"], "sheets": sheets, "frame_bindings": bindings}
        previous = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        retained = {key: value for key, value in previous.get('frame_bindings', {}).items() if key not in frames}
        retained.update({_frame_key(item): item['background_reference']
                         for item in json.loads(shots_path.read_text()).get('items', [])
                         if _frame_key(item) not in frames and item.get('background_reference')})
        manifest['frame_bindings'].update(retained)
        retained_paths = {value['sheet_path'] for value in retained.values()}
        manifest['sheets'] += [sheet for sheet in previous.get('sheets', []) if sheet['path'] in retained_paths]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(shots_data, _background_plan_hash=input_hash)


def storybook_background_references_tool(session_id: str, project_id: str, shots_data: dict, language: str = "ru", enable: bool = True) -> dict:
    """Render planned background sheets and attach their frame/card bindings.

    Args:
        session_id: Execution trace identifier.
        project_id: Storybook project identifier.
        shots_data: Result of the background planning step.
        language: Project language code.
        enable: Whether to render background references.
    """
    if not enable or not shots_data.get("items"):
        return shots_data
    base = safe_storybook_project_dir(project_id)
    shots_path = base / "97_shots" / "shots.json"
    manifest_path = base / "20_bible" / "references" / "background_references.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if shots_data.get("_background_plan_hash") != manifest.get("input_hash"):
        raise ValueError("Background images require the matching planning-step result")
    frames = {_frame_key(item) for item in shots_data["items"]}
    locations = json.loads((base / "20_bible" / "locations.json").read_text(encoding="utf-8"))
    locations_by_name = {location["name"]: location for location in locations}
    space_sheets = {}
    for sheet in manifest["sheets"]:
        space_sheets.setdefault(sheet["space_id"], []).append(sheet)

    def render_space(space_sheets):
        for sheet in space_sheets:
            output = Path(existing_reference_path(base, sheet["path"]))  # файл мог быть перенесён
            output.parent.mkdir(parents=True, exist_ok=True)
            references = [existing_reference_path(base, sheet["previous_sheet"])] if sheet["previous_sheet"] else []
            landmarks = [existing_reference_path(base, path) for path in sheet.get('landmark_reference_paths', [])]
            references += [path for path in landmarks if path not in references]
            if len(references) > 4:
                raise ValueError('Shared landmark references exceed the image reference limit')
            references += [str(path) for name in sheet["locations"]
                           for path in [_reference_path(base, locations_by_name[name].get("reference_image_path"))]
                           if path.is_file() and str(path) not in references]
            sheet["reference_image_paths"] = references[:4]
            prompt = sheet['prompt']
            for role in sheet.get('landmark_reference_roles', []):
                role_path = existing_reference_path(base, role['path'])
                if role_path in references[:4]:
                    prompt += (f"\nImage {references.index(role_path) + 1}: эталон только конструкции "
                               f"{role['id']} ({role['name']}). Другие объекты на фоне не задают канон; "
                               "используй их собственные эталоны. Изменяемое состояние и содержимое берутся из карточки.")
            key = hashlib.sha256(json.dumps(
                [prompt, sheet["negative_prompt"], sheet["width"], sheet["height"],
                 [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in references[:4]],
                 shots_data.get("seed")], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cached = reuse_reference(base, "background", key, description=prompt,
                                     metadata={"space_id": sheet["space_id"], "source_hashes": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in references[:4]]})
            old_path = sheet["path"]
            if cached:
                output = Path(cached)
                sheet["path"] = str(output)
                for binding in manifest["frame_bindings"].values():
                    if binding.get("sheet_path") == old_path:
                        binding["sheet_path"] = str(output)
                for other in space_sheets:
                    if other.get("previous_sheet") == old_path:
                        other["previous_sheet"] = str(output)
                continue
            if output.is_file():
                result = output
            elif references:
                result = edit_image_vse_tool(
                    session_id=session_id, prompt=prompt, image_paths=references[:4],
                    output_path=str(output), width=sheet["width"], height=sheet["height"],
                    negative_prompt=sheet["negative_prompt"],
                    seed=shots_data.get("seed"),
                )
            else:
                result = generate_image_tool(
                    session_id=session_id, prompt=sheet["prompt"], number=sheet["sheet_number"],
                    width=sheet["width"], height=sheet["height"],
                    negative_prompt=sheet["negative_prompt"],
                    seed=shots_data.get("seed"),
                )
                if os.path.isfile(str(result)):
                    shutil.move(str(result), output)
            if not output.is_file():
                raise RuntimeError(f"Background reference was not generated: {output}; {str(result)[:500]}")
            canonical = store_reference(base, output, "background", key,
                                        description=prompt,
                                        metadata={"space_id": sheet["space_id"], "source_hashes": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in references[:4]]},
                                        sources=references[:4])
            sheet["path"] = canonical
            for binding in manifest["frame_bindings"].values():
                if binding.get("sheet_path") == old_path:
                    binding["sheet_path"] = canonical
            for other in space_sheets:
                if other.get("previous_sheet") == old_path:
                    other["previous_sheet"] = canonical
    with ThreadPoolExecutor(max_workers=4) as executor:
        list((future.result() for future in [executor.submit(contextvars.copy_context().run, render_space, value) for value in space_sheets.values()]))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(str(shots_path) + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        latest = json.loads(shots_path.read_text(encoding="utf-8"))
        for item in latest["items"]:
            key = _frame_key(item)
            if key not in frames:
                continue
            item["background_reference"] = manifest["frame_bindings"].get(key)
        temporary = shots_path.with_suffix(".background.tmp")
        temporary.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, shots_path)
        fcntl.flock(lock, fcntl.LOCK_UN)
    from .video_plan import select_image_items
    selected = select_image_items(project_id, latest['items'])
    scope = shots_data.get('scope_shot_keys')
    if scope is not None:
        selected = [item for item in selected if f"{item['scene_number']}-{item['shot_number']}" in scope]
    return dict(latest, items=selected)
