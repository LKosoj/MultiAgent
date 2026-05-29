import os
import json
import re
import logging
from typing import Dict, Any, List
from utils import call_openai_api, parse_llm_json
from agent_command import model_code

logger = logging.getLogger(__name__)

_QUOTED_TEXT_RE = re.compile(r'«[^»]+»|"[^"]+"|\'[^\']+\'')
_CARET_TEXT_RE = re.compile(r"\^[^^\n]{1,120}\^")
_FOCUS_PREFIX_PATTERNS = {
    "ru": re.compile(r"Сфокусировать кадр на одном замороженном моменте: .*? Главный субъект кадра: .*?\.\s*", re.IGNORECASE),
    "es": re.compile(r"Enfocar el encuadre en un unico momento congelado: .*? Sujeto principal del cuadro: .*?\.\s*", re.IGNORECASE),
    "fr": re.compile(r"Concentrer l'image sur un seul instant fige: .*? Sujet principal du cadre: .*?\.\s*", re.IGNORECASE),
    "de": re.compile(r"Das Bild auf einen einzigen eingefrorenen Moment fokussieren: .*? Hauptmotiv des Bildes: .*?\.\s*", re.IGNORECASE),
    "en": re.compile(r"Focus the frame on one frozen moment: .*? Primary subject of the frame: .*?\.\s*", re.IGNORECASE),
}
_REFERENCE_ROLES_PATTERNS = {
    "ru": re.compile(r"Роли референсов: .*?(?:избегай смешения стилей\.)", re.IGNORECASE),
    "es": re.compile(r"Roles de referencia: .*?(?:evita la deriva de estilo\.)", re.IGNORECASE),
    "fr": re.compile(r"Roles des references: .*?(?:evite la derive de style\.)", re.IGNORECASE),
    "de": re.compile(r"Referenzrollen: .*?(?:vermeide Stilabweichungen\.)", re.IGNORECASE),
    "en": re.compile(r"Reference roles: .*?(?:avoid style drift\.)", re.IGNORECASE),
}
def _get_prompt_language_label(language: str) -> str:
    language_map = {
        "ru": "русском языке",
        "en": "английском языке",
        "es": "испанском языке",
        "fr": "французском языке",
        "de": "немецком языке",
    }
    return language_map.get(language, f"языке с кодом {language}")


def _build_reference_roles_instruction(language: str, role_entries: List[str]) -> str:
    joined = "; ".join(role_entries)
    templates = {
        "ru": (
            "Роли референсов: {entries}. "
            "Сохрани идентичность, перспективу, освещение и естественные тени; избегай смешения стилей."
        ),
        "es": (
            "Roles de referencia: {entries}. "
            "Conserva la identidad, la perspectiva, la iluminacion y las sombras naturales; evita la deriva de estilo."
        ),
        "fr": (
            "Roles des references: {entries}. "
            "Conserve l'identite, la perspective, l'eclairage et les ombres naturelles; evite la derive de style."
        ),
        "de": (
            "Referenzrollen: {entries}. "
            "Bewahre Identitat, Perspektive, Beleuchtung und naturliche Schatten; vermeide Stilabweichungen."
        ),
        "en": (
            "Reference roles: {entries}. "
            "Preserve identity, perspective, lighting, and natural shadows; avoid style drift."
        ),
    }
    template = templates.get(language, templates["en"])
    return template.format(entries=joined)


def _build_story_page_lookup(story: Dict[str, Any]) -> Dict[int, Dict[str, str]]:
    pages: Dict[int, Dict[str, str]] = {}
    for page in story.get("pages", []):
        page_number = page.get("page")
        if not isinstance(page_number, int):
            continue
        pages[page_number] = {
            "title": (page.get("title") or "").strip(),
            "body": (page.get("body") or "").strip(),
        }
    return pages


def _build_scene_packets(
    beats: List[Dict[str, Any]],
    story_pages: Dict[int, Dict[str, str]],
) -> List[Dict[str, Any]]:
    packets: List[Dict[str, Any]] = []
    for beat in beats:
        page_context = story_pages.get(beat.get("page_number"), {})
        packets.append(
            {
                "page_number": beat.get("page_number"),
                "page_title": page_context.get("title", ""),
                "page_body": page_context.get("body", ""),
                "beat": beat,
            }
        )
    return packets


def _annotate_scene_packets(
    scene_packets: List[Dict[str, Any]],
    characters: List[Dict[str, Any]],
    authoritative_readable_texts: List[str],
) -> List[Dict[str, Any]]:
    annotated_packets: List[Dict[str, Any]] = []
    for packet in scene_packets:
        beat = packet.get("beat") or {}
        haystacks: List[str] = [
            str(packet.get("page_title") or ""),
            str(packet.get("page_body") or ""),
        ]
        if isinstance(beat, dict):
            for value in beat.values():
                if isinstance(value, str):
                    haystacks.append(value)

        lowered_haystacks = [text.casefold() for text in haystacks if text]

        explicit_character_mentions: List[str] = []
        for character in characters:
            name = str(character.get("name") or "").strip()
            if not name:
                continue
            name_key = name.casefold()
            if any(name_key in text for text in lowered_haystacks):
                explicit_character_mentions.append(name)

        explicit_readable_text_mentions: List[str] = []
        for text in authoritative_readable_texts:
            candidate = str(text or "").strip()
            if not candidate:
                continue
            candidate_key = candidate.casefold()
            if any(candidate_key in text for text in lowered_haystacks):
                explicit_readable_text_mentions.append(candidate)

        annotated_packet = dict(packet)
        annotated_packet["explicit_character_mentions"] = explicit_character_mentions
        annotated_packet["explicit_readable_text_mentions"] = explicit_readable_text_mentions
        annotated_packets.append(annotated_packet)
    return annotated_packets


def _extract_authoritative_readable_texts(
    characters: List[Dict[str, Any]],
    locations: List[Dict[str, Any]],
) -> List[str]:
    texts: List[str] = []
    seen = set()

    candidate_fields: List[str] = []
    for character in characters:
        variable = character.get("variable_attributes") or {}
        candidate_fields.append(str(variable.get("base_clothing") or ""))
        candidate_fields.extend(str(entry or "") for entry in (variable.get("accessories") or []))
    for location in locations:
        candidate_fields.append(str(location.get("description") or ""))
        candidate_fields.extend(str(entry or "") for entry in (location.get("key_objects") or []))

    for field_text in candidate_fields:
        for match in _QUOTED_TEXT_RE.finditer(field_text):
            fragment = match.group(0).strip("«»\"'").strip()
            if not fragment or fragment in seen:
                continue
            seen.add(fragment)
            texts.append(fragment)

    return texts


def _validate_frame_specs(
    frame_specs: List[Dict[str, Any]],
    expected_count: int,
    *,
    strict_semantics: bool = True,
) -> None:
    if len(frame_specs) != expected_count:
        raise ValueError(
            f"frame_spec planner вернул {len(frame_specs)} specs при ожидании {expected_count}"
        )

    required_string_fields = ("selected_moment", "primary_subject", "scene_mode", "camera_anchor")
    required_list_fields = (
        "must_show",
        "must_not_show",
        "visible_characters",
        "observer_characters",
        "visible_readable_texts",
        "hidden_readable_texts",
        "prop_states",
    )
    for idx, frame_spec in enumerate(frame_specs, start=1):
        missing = []
        for field in required_string_fields:
            if not str(frame_spec.get(field) or "").strip():
                missing.append(field)
        for field in required_list_fields:
            if not isinstance(frame_spec.get(field), list):
                missing.append(field)
        if missing:
            raise ValueError(
                f"frame_spec для page_{idx:02d} не содержит обязательные поля: {', '.join(missing)}"
            )
        scene_mode = str(frame_spec.get("scene_mode") or "").strip()
        if scene_mode not in {"single_subject", "ensemble"}:
            raise ValueError(
                f"frame_spec для page_{idx:02d} содержит недопустимый scene_mode: {scene_mode}"
            )

        if not strict_semantics:
            continue

        visible_texts = {str(text).strip() for text in frame_spec.get("visible_readable_texts") or [] if str(text).strip()}
        hidden_texts = {str(text).strip() for text in frame_spec.get("hidden_readable_texts") or [] if str(text).strip()}
        overlap = visible_texts & hidden_texts
        if overlap:
            raise ValueError(
                f"frame_spec для page_{idx:02d} содержит один и тот же readable text как visible и hidden: {', '.join(sorted(overlap))}"
            )

        visible_characters = [str(name).strip() for name in frame_spec.get("visible_characters") or [] if str(name).strip()]
        observer_characters = [str(name).strip() for name in frame_spec.get("observer_characters") or [] if str(name).strip()]
        primary_subject = str(frame_spec.get("primary_subject") or "").strip().casefold()
        if scene_mode == "ensemble" and len(visible_characters) < 2:
            raise ValueError(
                f"frame_spec для page_{idx:02d} помечен как ensemble, но visible_characters содержит меньше двух персонажей"
            )
        if not set(observer_characters).issubset(set(visible_characters)):
            raise ValueError(
                f"frame_spec для page_{idx:02d} содержит observer_characters, которых нет в visible_characters"
            )
        if scene_mode == "ensemble" and any(observer.casefold() in primary_subject for observer in observer_characters):
            raise ValueError(
                f"frame_spec для page_{idx:02d} делает observer-персонажа главным субъектом ensemble-сцены"
            )


def _canonicalize_character_name(raw_name: str, canonical_names: List[str]) -> str:
    candidate = str(raw_name or "").strip()
    if not candidate:
        return ""
    candidate_key = candidate.casefold()
    for canonical_name in canonical_names:
        canonical_key = canonical_name.casefold()
        if candidate_key == canonical_key or canonical_key in candidate_key:
            return canonical_name
    return candidate


def _normalize_frame_specs(
    frame_specs: List[Dict[str, Any]],
    characters: List[Dict[str, Any]],
    scene_packets: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    canonical_names = [
        str(character.get("name") or "").strip()
        for character in characters
        if str(character.get("name") or "").strip()
    ]
    normalized_specs: List[Dict[str, Any]] = []
    for idx, frame_spec in enumerate(frame_specs):
        normalized = dict(frame_spec)
        for field in ("visible_characters", "observer_characters"):
            normalized_names: List[str] = []
            seen_names = set()
            for raw_name in frame_spec.get(field) or []:
                canonical_name = _canonicalize_character_name(str(raw_name or ""), canonical_names)
                if not canonical_name or canonical_name in seen_names:
                    continue
                normalized_names.append(canonical_name)
                seen_names.add(canonical_name)
            normalized[field] = normalized_names
        visible_names = list(normalized.get("visible_characters") or [])
        visible_seen = set(visible_names)
        for observer_name in normalized.get("observer_characters") or []:
            if observer_name not in visible_seen:
                visible_names.append(observer_name)
                visible_seen.add(observer_name)
        if (
            str(normalized.get("scene_mode") or "").strip() == "ensemble"
            and len(visible_names) < 2
            and scene_packets
            and idx < len(scene_packets)
        ):
            explicit_mentions = [
                _canonicalize_character_name(name, canonical_names)
                for name in (scene_packets[idx].get("explicit_character_mentions") or [])
            ]
            for explicit_name in explicit_mentions:
                if not explicit_name or explicit_name in visible_seen:
                    continue
                visible_names.append(explicit_name)
                visible_seen.add(explicit_name)
        normalized["visible_characters"] = visible_names
        normalized_specs.append(normalized)
    return normalized_specs


def _select_frame_specs(
    story_title: str,
    scene_packets: List[Dict[str, Any]],
    beats: List[Dict[str, Any]],
    characters: List[Dict[str, Any]],
    locations: List[Dict[str, Any]],
    style_images: List[Dict[str, Any]],
    negative_list: str,
    authoritative_readable_texts: List[str],
    language: str,
) -> List[Dict[str, Any]]:
    system = f"""
Ты — визуальный режиссер-постановщик. Твоя задача: до генерации image prompts выбрать для каждой сцены один
строго определенный still frame и описать его как структурированный frame spec.

Верни JSON только вида:
{{
  "frame_specs": [
    {{
      "selected_moment": "один конкретный замороженный момент на {_get_prompt_language_label(language)}",
      "primary_subject": "главный субъект этого кадра на {_get_prompt_language_label(language)}",
      "scene_mode": "single_subject или ensemble",
      "camera_anchor": "что является композиционным и ракурсным якорем кадра на {_get_prompt_language_label(language)}",
      "must_show": ["что обязательно видно в одном кадре T=0"],
      "must_not_show": ["что НЕ должно попадать в этот кадр, потому что относится к соседнему моменту или конфликтует с ним"],
      "visible_characters": ["точные канонические имена персонажей, которые реально видимы в этом кадре"],
      "observer_characters": ["точные канонические имена поздних наблюдателей или вторичных участников"],
      "visible_readable_texts": ["точные буквальные readable texts, реально видимые в кадре"],
      "hidden_readable_texts": ["точные буквальные readable texts, которые существуют в каноне, но в этом кадре не должны быть видимы"],
      "prop_states": ["состояния реквизита и аксессуаров, которые должны быть одновременно правдивы в этом кадре"]
    }}
  ]
}}

Правила:
- Для каждой страницы сначала смотри на final `page_title` и `page_body`. Это главный source of truth.
- Затем используй `beat` как источник must-have деталей, атмосферы и состава сцены, но не позволяй ему тащить в кадр целую последовательность действий.
- В `scene_packets[i].explicit_character_mentions` уже собраны канонические персонажи, явно названные в финальном тексте страницы и beat. Если они совместимы с выбранным кадром, не выкидывай их из ensemble без причины.
- В `scene_packets[i].explicit_readable_text_mentions` уже собраны авторитетные буквальные надписи, которые финальный текст страницы явно поднимает. Используй это как сильную подсказку для visible/hidden readable texts.
- Выбери один visually strongest freeze-frame, который лучше всего соответствует финальному тексту страницы.
- Если страница описывает последствия события или уже свершившееся событие, выбирай кадр с этими последствиями, а не момент за секунду до них.
- Если страница описывает коллективную катастрофу, общий событийный узел или последствия для нескольких персонажей, не своди кадр к одному позднему наблюдателю, даже если он визуально прост.
- `must_show` должен содержать только факты, которые можно одновременно увидеть в одном still frame.
- В ensemble-сцене `must_show` должен явно удерживать событийное ядро кадра и различимый состав сцены, а `observer_characters` — только вторичную периферию, но не замену основному cast.
- `must_not_show` должен перечислять соседние во времени или физически противоречащие состояния, которые нельзя тащить в этот кадр.
- Если аксессуар уже лежит отдельно в кадре, зафиксируй это в `prop_states` и укажи в `must_not_show`, что он не должен оставаться на персонаже.
- `visible_readable_texts` и `hidden_readable_texts` разрешены только как exact строки из `authoritative_readable_texts`; ничего не сокращай, не расширяй и не выдумывай.
- Если readable text существует в каноне, но в выбранном кадре он не должен читаться или не должен быть видим, отправляй его в `hidden_readable_texts`, а не в `visible_readable_texts`.
- В ensemble-сцене late observer может оставаться в `visible_characters`, но не должен быть единственным visually dominant персонажем, единственным character reference и единственным композиционным якорем.
- Если персонаж относится к событийному ядру, но виден лишь частично (например, ноги из люка, падающая фигура, участник паники), он всё равно остаётся в `visible_characters`, а не превращается в observer.
- В `visible_characters` и `observer_characters` используй только точные канонические имена из `characters.json`, без пояснений в скобках, состояний и описаний видимости. Детали вида "частично виден" выноси в `must_show` или `prop_states`.
- Не пиши narrative summary. Определи только визуально наблюдаемый кадр T=0 и его инварианты.
- Не упоминай будущие или прошлые события как текущие состояния кадра.
"""
    payload = {
        "story_title": story_title,
        "scene_packets": scene_packets,
        "beats": beats,
        "characters": characters,
        "locations": locations,
        "style_images": style_images,
        "negative_list": negative_list,
        "authoritative_readable_texts": authoritative_readable_texts,
    }
    resp = call_openai_api(
        prompt=json.dumps(payload, ensure_ascii=False),
        system_prompt=system,
        model=model_code,
        max_tokens=_estimate_prompt_response_max_tokens(len(scene_packets)),
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = parse_llm_json(resp, fallback_list_key="frame_specs")
    frame_specs: List[Dict[str, Any]] = data.get("frame_specs", [])
    _validate_frame_specs(frame_specs, len(scene_packets), strict_semantics=False)
    return _repair_frame_specs(
        story_title=story_title,
        scene_packets=scene_packets,
        beats=beats,
        characters=characters,
        locations=locations,
        style_images=style_images,
        negative_list=negative_list,
        authoritative_readable_texts=authoritative_readable_texts,
        language=language,
        frame_specs=frame_specs,
    )


def _repair_frame_specs(
    story_title: str,
    scene_packets: List[Dict[str, Any]],
    beats: List[Dict[str, Any]],
    characters: List[Dict[str, Any]],
    locations: List[Dict[str, Any]],
    style_images: List[Dict[str, Any]],
    negative_list: str,
    authoritative_readable_texts: List[str],
    language: str,
    frame_specs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    system = f"""
Ты — QA-редактор frame specs для still-image иллюстраций.
Верни JSON только вида:
{{
  "frame_specs": [
    {{
      "selected_moment": "...",
      "primary_subject": "...",
      "scene_mode": "single_subject или ensemble",
      "camera_anchor": "...",
      "must_show": ["..."],
      "must_not_show": ["..."],
      "visible_characters": ["точные канонические имена"],
      "observer_characters": ["точные канонические имена"],
      "visible_readable_texts": ["..."],
      "hidden_readable_texts": ["..."],
      "prop_states": ["..."]
    }}
  ]
}}

Все текстовые поля пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: починить frame specs так, чтобы они соответствовали финальному тексту страниц и не уводили prompt generation в неверный кадр.

Правила:
- `scene_packets[].page_title` и `scene_packets[].page_body` — главный source of truth.
- `scene_packets[].explicit_character_mentions` и `scene_packets[].explicit_readable_text_mentions` — сильные input-driven подсказки о том, что финальный текст страницы реально поднимает как видимое/важное.
- `current_frame_specs` можно исправлять, если они выбрали не тот момент, не тот primary subject, не тот scene_mode или конфликтные visible/hidden facts.
- Если финальный текст страницы описывает коллективное событие, цепочку последствий или общий событийный узел, а один персонаж появляется поздно как наблюдатель или вторичный участник, такой персонаж не должен становиться `primary_subject` ensemble-сцены.
- В ensemble-сцене `primary_subject` должен описывать событийное ядро кадра, а `observer_characters` — только поздних наблюдателей, боковые входы и вторичные фигуры у края кадра.
- В ensemble-сцене `visible_characters` должен удерживать персонажей, явно присутствующих в финальном тексте и реально видимых в выбранном моменте; late observer не должен вытеснять основной cast из `visible_characters`.
- `observer_characters` — это только поздние наблюдатели, боковые входы и периферийные свидетели. Не переноси туда персонажей из событийного ядра только потому, что они видны частично, падают или читаются через последствия.
- Если страница уже описывает последствия катастрофы, выбранный кадр должен фиксировать эти последствия, а не сцену до них.
- `must_show` должен фиксировать обязательные видимые последствия и composition anchors.
- Если страница строится вокруг общего события, `must_show` должен явно удерживать событийное ядро и различимый состав сцены, а не только late observer и фон.
- `must_not_show` должен выносить соседние моменты, pre-event состояния и физически несовместимые состояния.
- `camera_anchor` должен быть привязан к событийному ядру кадра, а не к late observer, если сцена ensemble.
- `visible_readable_texts` и `hidden_readable_texts` разрешены только как exact строки из `authoritative_readable_texts`.
- Не придумывай новые readable texts, не сокращай их и не меняй словоформу.
- `prop_states` должны описывать только одновременно истинные состояния реквизита и аксессуаров.
- `visible_characters` и `observer_characters` должны содержать только точные канонические имена из `characters.json`, без пояснений в скобках и без описаний состояния.
- Не пиши narrative summary. Исправляй только визуальные инварианты кадра.
"""
    payload = {
        "story_title": story_title,
        "scene_packets": scene_packets,
        "beats": beats,
        "characters": characters,
        "locations": locations,
        "style_images": style_images,
        "negative_list": negative_list,
        "authoritative_readable_texts": authoritative_readable_texts,
        "current_frame_specs": frame_specs,
    }
    resp = call_openai_api(
        prompt=json.dumps(payload, ensure_ascii=False),
        system_prompt=system,
        model=model_code,
        max_tokens=_estimate_prompt_response_max_tokens(len(scene_packets)),
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = parse_llm_json(resp, fallback_list_key="frame_specs")
    repaired_specs: List[Dict[str, Any]] = data.get("frame_specs", [])
    repaired_specs = _normalize_frame_specs(repaired_specs, characters, scene_packets)
    _validate_frame_specs(repaired_specs, len(scene_packets))
    return repaired_specs


def _sync_prompt_references_with_frame_spec(
    prompt: Dict[str, Any],
    frame_spec: Dict[str, Any],
    characters: List[Dict[str, Any]],
) -> None:
    references = prompt.get("references")
    if not isinstance(references, dict):
        references = {}
        prompt["references"] = references

    reference_path_by_name: Dict[str, str] = {}
    for character in characters:
        name = str(character.get("name") or "").strip()
        path = str(character.get("reference_image_path") or "").strip()
        if name and path:
            reference_path_by_name[name] = path

    visible_characters = [
        str(name).strip()
        for name in (frame_spec.get("visible_characters") or [])
        if str(name).strip()
    ]
    observer_characters = {
        str(name).strip()
        for name in (frame_spec.get("observer_characters") or [])
        if str(name).strip()
    }

    prioritized_names = [
        *[name for name in visible_characters if name not in observer_characters],
        *[name for name in visible_characters if name in observer_characters],
    ]
    synced_paths: List[str] = []
    seen_paths = set()
    for name in prioritized_names:
        path = reference_path_by_name.get(name)
        if not path or path in seen_paths:
            continue
        synced_paths.append(path)
        seen_paths.add(path)

    if synced_paths:
        references["character_paths"] = synced_paths[:10]


def _apply_frame_plan(prompt: Dict[str, Any], language: str) -> None:
    prompt.pop("_frame_plan", None)
    prompt_text = (prompt.get("english_prompt") or "").strip()
    if not prompt_text:
        return

    prompt["english_prompt"] = _FOCUS_PREFIX_PATTERNS.get(language, _FOCUS_PREFIX_PATTERNS["en"]).sub("", prompt_text, count=1).strip()


def _estimate_prompt_response_max_tokens(scene_count: int) -> int:
    scene_count = max(1, int(scene_count or 1))
    return min(12000, max(2600, 1800 + scene_count * 1800))


def _validate_prompts(prompts: List[Dict[str, Any]], expected_count: int) -> None:
    if len(prompts) != expected_count:
        raise ValueError(
            f"prompt_engineer вернул {len(prompts)} prompts при ожидании {expected_count}"
        )

    required_fields = ("english_prompt", "negative_prompt", "references", "technical")
    for idx, prompt in enumerate(prompts, start=1):
        missing = []
        for field in required_fields:
            value = prompt.get(field)
            if isinstance(value, str):
                if not value.strip():
                    missing.append(field)
            elif not value:
                missing.append(field)
        if missing:
            raise ValueError(
                f"page_{idx:02d}_prompt не содержит обязательные поля: {', '.join(missing)}"
            )


def _normalize_negative_prompt(prompt: Dict[str, Any], language: str) -> None:
    prompt_text_raw = prompt.get("english_prompt") or ""
    prompt_text = prompt_text_raw.lower()
    negative_prompt = (prompt.get("negative_prompt") or "").strip()
    if not negative_prompt:
        return

    readable_text_prompt_markers = {
        "ru": ("читаем", "разборчив"),
        "es": ("texto legible", "legible"),
        "fr": ("texte lisible", "lisible"),
        "de": ("lesbarer text", "lesbar"),
        "en": ("readable text", "legible"),
    }
    has_explicit_readable_text = bool(_CARET_TEXT_RE.search(prompt_text_raw))
    if not has_explicit_readable_text and not any(marker in prompt_text for marker in readable_text_prompt_markers.get(language, readable_text_prompt_markers["en"])):
        return

    generic_text_bans = {
        "ru": {"текст"},
        "es": {"texto"},
        "fr": {"texte"},
        "de": {"text"},
        "en": {"text"},
    }
    readable_text_ban_markers = {
        "ru": ("читаем", "разборчив"),
        "es": ("texto legible", "legible"),
        "fr": ("texte lisible", "lisible"),
        "de": ("lesbarer", "lesbar"),
        "en": ("readable", "legible"),
    }
    overlay_text_bans = {
        "ru": (
            "накладной текст",
            "речевые пузыри",
            "текст диалога",
            "субтитры",
        ),
        "es": (
            "texto superpuesto",
            "globos de dialogo",
            "texto de dialogo",
            "subtitulos",
        ),
        "fr": (
            "texte superpose",
            "bulles de dialogue",
            "texte de dialogue",
            "sous-titres",
        ),
        "de": (
            "overlay-text",
            "sprechblasen",
            "dialogtext",
            "untertitel",
        ),
        "en": (
            "overlay text",
            "speech bubbles",
            "dialogue text",
            "subtitles",
        ),
    }

    seen = set()
    filtered_parts: List[str] = []
    for raw_part in negative_prompt.split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = part.lower()
        if normalized in generic_text_bans.get(language, generic_text_bans["en"]):
            continue
        if any(marker in normalized for marker in readable_text_ban_markers.get(language, readable_text_ban_markers["en"])):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered_parts.append(part)

    for extra_part in overlay_text_bans.get(language, overlay_text_bans["en"]):
        normalized = extra_part.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered_parts.append(extra_part)

    prompt["negative_prompt"] = ", ".join(filtered_parts)


def _sanitize_prompt_text(prompt_text: str, allowed_readable_texts: List[str]) -> str:
    cleaned = (prompt_text or "").strip()
    if not cleaned:
        return cleaned

    allowed = {text.strip() for text in (allowed_readable_texts or []) if str(text).strip()}
    allowed_by_lower = {text.casefold(): text for text in allowed}

    def _normalize_authoritative_fragment(fragment: str) -> str:
        normalized_fragment = fragment.strip()
        if not normalized_fragment:
            return ""
        direct = allowed_by_lower.get(normalized_fragment.casefold())
        if direct:
            return direct

        matches = [
            text for text in allowed
            if text.casefold() in normalized_fragment.casefold()
            or normalized_fragment.casefold() in text.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        return ""

    while True:
        updated = cleaned
        for match in _CARET_TEXT_RE.finditer(cleaned):
            fragment = match.group(0).strip("^").strip()
            normalized = _normalize_authoritative_fragment(fragment)
            replacement = f"^{normalized}^" if normalized else fragment
            candidate = cleaned[:match.start()] + replacement + cleaned[match.end():]
            if candidate != cleaned:
                updated = candidate
                break
        if updated == cleaned:
            break
        cleaned = updated

    while True:
        updated = cleaned
        for match in _QUOTED_TEXT_RE.finditer(cleaned):
            fragment = match.group(0).strip("«»\"'")
            normalized = _normalize_authoritative_fragment(fragment)
            replacement = f"^{normalized}^" if normalized else fragment
            updated = (
                cleaned[:match.start()]
                + f" {replacement}"
                + cleaned[match.end():]
            )
            break
        if updated == cleaned:
            break
        cleaned = updated

    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    cleaned = re.sub(r'\s+([,.;:])', r'\1', cleaned)
    cleaned = re.sub(r"\bВизуальные последствия катастрофы:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\^", " ^", cleaned)
    return cleaned.strip()


def _repair_prompt_state_conflicts(
    prompt: Dict[str, Any],
    scene_packet: Dict[str, Any],
    language: str,
) -> None:
    prompt_text = (prompt.get("english_prompt") or "").strip()
    frame_plan = prompt.get("_frame_plan") or {}
    frame_spec = prompt.get("_frame_spec") or {}
    if not prompt_text or not isinstance(frame_plan, dict) or not isinstance(frame_spec, dict):
        return

    payload = {
        "scene_packet": scene_packet,
        "current_frame_plan": frame_plan,
        "current_frame_spec": frame_spec,
        "current_prompt": prompt_text,
    }
    system = f"""
Ты — QA-редактор prompt'ов для одной still-image иллюстрации.
Верни JSON только вида {{"english_prompt": "..."}}.
Поле `english_prompt` пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: исправить физические и временные противоречия в prompt, не меняя уже выбранный кадр.

Правила:
- `current_frame_spec` и `current_frame_plan` — source of truth. Не переопределяй выбранный момент.
- В кадре существует только один момент времени T=0.
- Один и тот же предмет или аксессуар не может одновременно быть в двух взаимоисключающих состояниях: в руке и на земле, на персонаже и на земле, надетым и потерянным, направляющим жест и уже выпавшим.
- Если предмет уже лежит/упал/оказался на земле, персонаж больше не держит и не использует его в жесте.
- Если аксессуар уже на земле, явно не описывай его как надетый на персонаже.
- Если текущий prompt смешивает соседние моменты последовательности, оставь только то, что совместимо с `current_frame_spec.must_show` и `current_frame_spec.prop_states`.
- Сохрани главного субъекта и общую композиционную идею, но убери взаимоисключающие состояния.
- Не меняй канонические identity-маркеры персонажей из `characters`: вид, анатомию, базовую одежду, ключевые аксессуары и распределение ролей в кадре.
- Не humanize не-человеческих персонажей. Если персонаж в каноне не-человек, это должно явно читаться и в repaired prompt.
- Не меняй владельца одежды, аксессуаров, жестов, позиций и читаемых надписей между персонажами.
- Не добавляй диалоги и реплики персонажей.
- Если в кадре обязателен readable text из `current_frame_spec.visible_readable_texts`, сохраняй только точный буквальный текст и оборачивай его в `^...^`.
- Не показывай readable texts из `current_frame_spec.hidden_readable_texts`.
- Не придумывай новые надписи и не заменяй авторитетный текст generic placeholders.
- Не добавляй новые readable texts на поверхностях, если такие тексты не поддержаны authoritative source data сцены.
- Не упоминай `_frame_plan` и `_frame_spec` в `english_prompt`.
"""
    repaired = parse_llm_json(
        call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=2200,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    )

    repaired_prompt = " ".join(str(repaired.get("english_prompt") or "").split()).strip()
    if not repaired_prompt:
        raise ValueError("QA-ремонт prompt'а не вернул english_prompt")
    prompt["english_prompt"] = repaired_prompt


def _repair_prompt_scene_alignment(
    prompt: Dict[str, Any],
    scene_packet: Dict[str, Any],
    language: str,
    authoritative_readable_texts: List[str],
) -> None:
    prompt_text = (prompt.get("english_prompt") or "").strip()
    frame_plan = prompt.get("_frame_plan") or {}
    frame_spec = prompt.get("_frame_spec") or {}
    if not prompt_text or not isinstance(frame_plan, dict) or not isinstance(frame_spec, dict):
        return

    payload = {
        "scene_packet": scene_packet,
        "current_frame_plan": frame_plan,
        "current_frame_spec": frame_spec,
        "authoritative_readable_texts": authoritative_readable_texts,
        "current_prompt": prompt_text,
        "current_negative_prompt": prompt.get("negative_prompt") or "",
    }
    system = f"""
Ты — QA-редактор prompt'ов для одной still-image иллюстрации.
Верни JSON только вида {{"english_prompt": "...", "negative_prompt": "..."}}.
Поля `english_prompt` и `negative_prompt` пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: переписать prompt и negative_prompt так, чтобы они строго соответствовали уже выбранному freeze-frame.

Правила:
- `current_frame_spec` и `current_frame_plan` — source of truth для кадра.
- Держись максимально близко к финальному тексту страницы и beat для этого кадра; не упрощай scene facts без необходимости.
- Если текущий prompt описывает более ранний или более поздний момент сцены, перепиши его под выбранный момент времени T=0 из `current_frame_spec.selected_moment`.
- Не пересказывай действия и последовательности. Описывай только то, что одновременно видно в одном кадре.
- Переписывай процессные и причинно-следственные формулировки в статичные, одновременно наблюдаемые состояния still-frame. Не используй конструкции вроде "после того как", "сразу после", "just after", "after", если их можно заменить описанием текущей позы, положения и результата.
- `current_frame_spec.must_show` — обязательные факты кадра. Они должны явно поддерживаться body prompt.
- `current_frame_spec.must_not_show` — запрещённые факты соседних моментов. Они не должны попадать ни в body prompt, ни в negative как обязательные элементы кадра.
- Для ensemble-сцены сохраняй различимость обязательного cast, observer-policy и центр события, но не превращай prompt в список сюжетных фраз про каждого персонажа.
- Если `current_frame_spec.scene_mode == "ensemble"`, late observers из `current_frame_spec.observer_characters` не должны становиться главным субъектом кадра, композиционным якорем или доминирующим foreground-планом.
- Для ракурса и композиции следуй `current_frame_spec.camera_anchor`.
- Не добавляй диалоги и narrative summary.
- Readable texts: только точные буквальные строки из `authoritative_readable_texts` и `current_frame_spec.visible_readable_texts`, всегда обёрнутые в `^...^`. Не сокращай, не расширяй, не дописывай инициалы, не склеивай два текста, не меняй словоформу, не заменяй generic placeholders.
- `current_frame_spec.hidden_readable_texts` не должны становиться видимыми или читаемыми в body prompt.
- Сохраняй identity anchors персонажей и reference-fidelity, но только в том состоянии, которое совместимо с selected moment.
- Не описывай персонажей в жестах, позах и состояниях, которые уже противоречат текущему freeze-frame.
- Состояния реквизита и аксессуаров должны следовать `current_frame_spec.prop_states`.
- Не упоминай `_frame_plan` и `_frame_spec` в `english_prompt`.
- Не используй meta-labels вроде "Визуальные последствия катастрофы:"; сразу описывай текущее видимое состояние кадра.
- Исправляй орфографию, грамматику и императивные формулировки на естественный {_get_prompt_language_label(language)}. Не оставляй случайные англоязычные хвосты в `english_prompt` и `negative_prompt`, кроме защищённых readable texts внутри `^...^`.
- `negative_prompt` должен быть визуальным и композиционным.
- Не запрещай в `negative_prompt` обязательные элементы, состояния и последствия выбранного кадра из `current_frame_spec.must_show`, `current_frame_spec.prop_states` и `current_frame_spec.visible_readable_texts`.
- Если в кадре обязателен readable text, запрещай только extra/unapproved/overlay text, но не общий `text`.
- Убирай из `negative_prompt` narrative-мусор, semantic-банальности и запреты, конфликтующие с body prompt.
"""
    repaired = parse_llm_json(
        call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=2200,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    )

    repaired_prompt = " ".join(str(repaired.get("english_prompt") or "").split()).strip()
    repaired_negative = " ".join(str(repaired.get("negative_prompt") or "").split()).strip()
    if not repaired_prompt or not repaired_negative:
        raise ValueError("QA-ремонт scene alignment не вернул english_prompt/negative_prompt")
    prompt["english_prompt"] = repaired_prompt
    prompt["negative_prompt"] = repaired_negative


def _polish_prompt_surface(
    prompt: Dict[str, Any],
    scene_packet: Dict[str, Any],
    language: str,
    authoritative_readable_texts: List[str],
) -> None:
    prompt_text = (prompt.get("english_prompt") or "").strip()
    negative_prompt = (prompt.get("negative_prompt") or "").strip()
    frame_plan = prompt.get("_frame_plan") or {}
    frame_spec = prompt.get("_frame_spec") or {}
    if not prompt_text or not negative_prompt or not isinstance(frame_plan, dict) or not isinstance(frame_spec, dict):
        return

    payload = {
        "scene_packet": scene_packet,
        "current_frame_plan": frame_plan,
        "current_frame_spec": frame_spec,
        "authoritative_readable_texts": authoritative_readable_texts,
        "current_prompt": prompt_text,
        "current_negative_prompt": negative_prompt,
    }
    system = f"""
Ты — surface-редактор image prompt'ов.
Верни JSON только вида {{"english_prompt": "...", "negative_prompt": "..."}}.
Оба поля пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: не менять факты кадра, а довести body prompt и negative prompt до чистого, статичного и грамматически корректного вида.

Правила:
- `current_frame_spec` и `current_frame_plan` — source of truth. Не меняй выбранный момент, primary subject, must_show, must_not_show, prop_states и readable texts.
- Не добавляй и не убирай сущности кадра. Меняй только формулировку.
- Переписывай процессные, причинно-следственные и временные формулировки в описание одновременно видимого still frame.
- Предпочитай статичные видимые состояния: позы, положения, результат, spatial blocking.
- Не начинай prompt с meta-фраз вроде "Сфокусировать кадр...", "Главный субъект кадра...", "Момент сразу после...". Начинай сразу с содержательного описания композиции и состояния сцены.
- Если формулировка описывает процесс ("спотыкается", "проваливается", "отступает после того как"), перепиши её в статичное наблюдаемое состояние этого же кадра.
- Исправляй орфографию, грамматику, стиль команд и неестественные формулировки.
- В `negative_prompt` не оставляй случайные англоязычные хвосты, если они не являются защищённым readable text.
- Readable texts сохраняй только как exact строки из `authoritative_readable_texts` и только внутри `^...^`.
- Не меняй смысл negative prompt: сохраняй запреты, но делай их краткими, визуальными и на целевом языке.
"""
    repaired = parse_llm_json(
        call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=1800,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    )
    repaired_prompt = " ".join(str(repaired.get("english_prompt") or "").split()).strip()
    repaired_negative = " ".join(str(repaired.get("negative_prompt") or "").split()).strip()
    if not repaired_prompt or not repaired_negative:
        raise ValueError("surface-polish не вернул english_prompt/negative_prompt")
    prompt["english_prompt"] = repaired_prompt
    prompt["negative_prompt"] = repaired_negative


def _freeze_frame_surface_rewrite(
    prompt: Dict[str, Any],
    scene_packet: Dict[str, Any],
    language: str,
    authoritative_readable_texts: List[str],
) -> None:
    prompt_text = (prompt.get("english_prompt") or "").strip()
    negative_prompt = (prompt.get("negative_prompt") or "").strip()
    frame_plan = prompt.get("_frame_plan") or {}
    frame_spec = prompt.get("_frame_spec") or {}
    if not prompt_text or not negative_prompt or not isinstance(frame_plan, dict) or not isinstance(frame_spec, dict):
        return

    payload = {
        "scene_packet": scene_packet,
        "current_frame_plan": frame_plan,
        "current_frame_spec": frame_spec,
        "authoritative_readable_texts": authoritative_readable_texts,
        "current_prompt": prompt_text,
        "current_negative_prompt": negative_prompt,
    }
    system = f"""
Ты — финальный freeze-frame редактор image prompt'ов.
Верни JSON только вида {{"english_prompt": "...", "negative_prompt": "..."}}.
Оба поля пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: не менять факты сцены, а удалить из body prompt остаточные формулировки процесса, перехода и движения.

Правила:
- `current_frame_spec` и `current_frame_plan` — source of truth. Не меняй selected moment, primary subject, readable texts, cast, реквизит и композиционный центр.
- Не добавляй и не убирай сущности кадра. Меняй только формулировку.
- Каждое предложение в `english_prompt` должно описывать только одновременно наблюдаемое состояние still frame T=0.
- Не оставляй глаголы процесса, перехода и причинно-следственной динамики, если их можно заменить статичным результатом.
- Вместо динамики описывай позу, положение тела, ориентацию, степень наклона, выражение лица, spatial blocking, уже наступивший результат.
- Не используй формулировки, которые звучат как действие в процессе: шаг, падение, бег, спотыкание, отступание, поворот, зевок, вход, выход, начало/конец действия. Перепиши их как статичное состояние кадра.
- Не меняй literal readable texts внутри `^...^`.
- Не порти negative prompt; при необходимости только слегка подчисти его язык, не меняя смысл.
"""
    repaired = parse_llm_json(
        call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=1600,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    )
    repaired_prompt = " ".join(str(repaired.get("english_prompt") or "").split()).strip()
    repaired_negative = " ".join(str(repaired.get("negative_prompt") or "").split()).strip()
    if not repaired_prompt or not repaired_negative:
        raise ValueError("freeze-frame surface rewrite не вернул english_prompt/negative_prompt")
    prompt["english_prompt"] = repaired_prompt
    prompt["negative_prompt"] = repaired_negative


def _finalize_prompt_from_frame_spec(
    prompt: Dict[str, Any],
    scene_packet: Dict[str, Any],
    language: str,
    authoritative_readable_texts: List[str],
) -> None:
    prompt_text = (prompt.get("english_prompt") or "").strip()
    negative_prompt = (prompt.get("negative_prompt") or "").strip()
    frame_plan = prompt.get("_frame_plan") or {}
    frame_spec = prompt.get("_frame_spec") or {}
    if not prompt_text or not negative_prompt or not isinstance(frame_plan, dict) or not isinstance(frame_spec, dict):
        return

    payload = {
        "scene_packet": scene_packet,
        "current_frame_plan": frame_plan,
        "current_frame_spec": frame_spec,
        "authoritative_readable_texts": authoritative_readable_texts,
        "current_prompt": prompt_text,
        "current_negative_prompt": negative_prompt,
    }
    system = f"""
Ты — единственный финальный validator-rewriter image prompt'а.
Верни JSON только вида {{"english_prompt": "...", "negative_prompt": "..."}}.
Оба поля пиши строго на {_get_prompt_language_label(language)}.

Твоя задача: выпустить финальную версию prompt-а строго по `current_frame_spec`, без дополнительных поздних постпроцессов.

Правила:
- `current_frame_spec` и `current_frame_plan` — единственный source of truth.
- Сохраняй все обязательные факты из `current_frame_spec.must_show` и `current_frame_spec.prop_states`. Нельзя удалять обязательную сущность только ради более гладкой формулировки.
- Если в `must_show` есть частично видимый персонаж, последствия ловушки, ноги из люка, свисающая камера, лежащие очки или трость, они должны остаться явно видимыми в `english_prompt`.
- Переписывай процессные формулировки в статичные состояния, но не удаляй их субъектов, объектов и результатов.
- Не используй meta-фразы вроде "Сфокусировать кадр...", "Главный субъект кадра..." и не пересказывай сюжет.
- Не добавляй и не убирай сущности кадра. Меняй только формулировку до финальной чистой версии.
- Для ensemble-сцены не схлопывай cast до одного наблюдателя и фона.
- Readable texts разрешены только как exact строки из `authoritative_readable_texts` и `current_frame_spec.visible_readable_texts`, только внутри `^...^`.
- Readable texts из `current_frame_spec.hidden_readable_texts` не делай видимыми и не замещай переводами.
- `negative_prompt` должен оставаться кратким, визуальным и не должен запрещать ни один обязательный факт из `must_show`, `prop_states` и `visible_readable_texts`.
- Если в кадре есть разрешённые readable texts, запрещай только лишние/неавторизованные надписи, но не сам факт readable text.
- Удали случайные narrative- и prose-хвосты. Оставь один чистый still-frame prompt.
"""
    repaired = parse_llm_json(
        call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=2200,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    )
    repaired_prompt = " ".join(str(repaired.get("english_prompt") or "").split()).strip()
    repaired_negative = " ".join(str(repaired.get("negative_prompt") or "").split()).strip()
    if not repaired_prompt or not repaired_negative:
        raise ValueError("final validator-rewriter не вернул english_prompt/negative_prompt")
    prompt["english_prompt"] = repaired_prompt
    prompt["negative_prompt"] = repaired_negative


def prompt_engineer_tool(session_id: str, project_id: str, language: str = 'en') -> str:
    """Создаёт 40_prompts/page_{NN}_prompt.json на основе канона, стилей и битов.
    english_prompt, negative_prompt, references, technical.

    Args:
        project_id (str): Идентификатор проекта. Используется для построения путей
            вида `plots/storybooks/{project_id}` и поиска исходных файлов (beats,
            characters, locations, style, negative list).
        session_id (str): Идентификатор сессии.
        language (str): Язык генерации для локализации промптов (по умолчанию 'en').

    Returns:
        str: Путь к каталогу сгенерированных промптов (`40_prompts`).
    """
    base = f"plots/storybooks/{project_id}"
    prompts_dir = f"{base}/40_prompts"
    
    # Проверяем целостность: существуют ли ВСЕ промпты (не только первый)
    with open(f"{base}/10_synopsis/beats.json", "r", encoding="utf-8") as f:
        _beats_check = json.load(f)
    expected_count = len(_beats_check)
    if expected_count > 0 and os.path.isdir(prompts_dir):
        existing = [
            name for name in os.listdir(prompts_dir)
            if name.startswith("page_") and name.endswith("_prompt.json")
        ]
        if len(existing) >= expected_count:
            logger.info(
                f"✏️ Промпты уже существуют ({len(existing)}/{expected_count} файлов в {prompts_dir}), пропускаем генерацию"
            )
            return prompts_dir
        elif existing:
            logger.warning(
                f"⚠️ Неполный набор промптов ({len(existing)}/{expected_count}), перегенерируем"
            )
    
    with open(f"{base}/10_synopsis/beats.json", "r", encoding="utf-8") as f:
        beats = json.load(f)
    with open(f"{base}/20_bible/characters.json", "r", encoding="utf-8") as f:
        characters = json.load(f)
    with open(f"{base}/20_bible/locations.json", "r", encoding="utf-8") as f:
        locations = json.load(f)
    with open(f"{base}/30_style/style_images.json", "r", encoding="utf-8") as f:
        style_images = json.load(f)
    with open(f"{base}/30_style/negative_prompt_list.txt", "r", encoding="utf-8") as f:
        negative_list = f.read().strip()
    with open(f"{base}/20_story/story.json", "r", encoding="utf-8") as f:
        story = json.load(f)

    prompt_language_label = _get_prompt_language_label(language)
    story_pages = _build_story_page_lookup(story)
    authoritative_readable_texts = _extract_authoritative_readable_texts(characters, locations)
    scene_packets = _annotate_scene_packets(
        _build_scene_packets(beats, story_pages),
        characters,
        authoritative_readable_texts,
    )
    frame_specs = _select_frame_specs(
        story_title=(story.get("title") or "").strip(),
        scene_packets=scene_packets,
        beats=beats,
        characters=characters,
        locations=locations,
        style_images=style_images,
        negative_list=negative_list,
        authoritative_readable_texts=authoritative_readable_texts,
        language=language,
    )

    system = """
=== LAYER A: ROLE + CONTRACT ===

Ты -- эксперт промпт-инженер и цифровой режиссер-постановщик для генерации иллюстраций в книге.

Source of truth: `frame_specs[i]` -- уже выбранная freeze-frame спецификация. Кадр заново НЕ выбирай.
Языковой контракт: содержимое `english_prompt` и `negative_prompt` пиши строго на __PROMPT_LANGUAGE_LABEL__. Поле `english_prompt` сохраняет имя по контракту, даже если содержимое не на английском. Не локализуй имена файлов и пути в `references`.
Входные данные: `characters`, `locations`, `scene_packets` (page_title, page_body, beat), `negative_list`, `beats`, `style_images`, `frame_specs`.

Выходной формат -- JSON:
```json
{"prompts": [{
  "_frame_plan": {"selected_moment": "...", "primary_subject": "...", "excluded_sequence_parts": ["..."]},
  "english_prompt": "...", "negative_prompt": "...",
  "references": {"character_paths": ["..."], "location_path": "..."},
  "technical": {"size": "1920x1080", "aspect": "16:9", "guidance": 7.5, "steps": 30, "sampler": "DPM++ 2M Karras", "seed_policy": "random"}
}]}
```
Генерируй ровно столько промптов, сколько `scene_packets`.

=== LAYER B: STEP-BY-STEP ALGORITHM (для каждой страницы i) ===

**Шаг 1 -- FRAME PLAN:**
- `_frame_plan.selected_moment` и `primary_subject` дословно из `frame_specs[i]`.
- Остальные части последовательности -- только в `excluded_sequence_parts`, не в `english_prompt`.
- Не переопределяй `must_show`, `must_not_show`, `prop_states`, `visible_readable_texts`, `hidden_readable_texts` из `frame_specs[i]`.

**Шаг 2 -- ENGLISH_PROMPT (единая команда редактирования):**
- Шаблон: Действие + Объект + Позиция + Стиль + Освещение + Перспектива.
- Один still frame T=0: только одновременно видимые в кадре элементы. Процессные фразы переводи в наблюдаемое состояние.
- Один главный субъект (primary_subject); остальные -- supporting elements.
- Ensemble-режим (`scene_mode == "ensemble"`): главный субъект -- событийное ядро, не observer. `observer_characters` вторичны. `visible_characters` остаются различимыми, не схлопывай ensemble до одного наблюдателя.
- Композиция/ракурс по `frame_specs[i].camera_anchor`.
- Identity anchors: 1-2 канонических маркера на каждого видимого персонажа. Не humanize не-людей. No-swap rule для костюмов/аксессуаров/ownership-bound элементов.
- Физическая консистентность: один предмет -- одно состояние. Нельзя одновременно "удерживаемый" и "упавший".
- Readable text only by authority из `characters`/`locations`; оборачивай в `^...^`. `hidden_readable_texts` не делай видимыми.
- Без реплик, цитат, диалогов, speech bubbles, пересказа сюжетных фраз.
- Формулируй команды редактирования референсных изображений (см. Layer C).
- Роли референсов указывай в порядке `reference_image_paths`.
- Персонажи и локации строго из текущего beat. Не переноси элементы из других beat'ов.
- Грамматически чисто на целевом языке; без случайных англоязычных хвостов кроме `^...^`.

**Шаг 3 -- REFERENCES:**
- `character_paths`: канонические референсы всех `frame_specs[i].visible_characters` с `reference_image_path`. В ensemble event-core cast раньше observer.
- `location_path`: из текущего beat.

**Шаг 4 -- NEGATIVE_PROMPT:**
- Базовые исключения из `negative_list` + технические запреты (blurry, low quality, distorted, watermark, text).
- Сценоспецифичные: лишние персонажи, неподходящие объекты/стиль.
- Если есть обязательный readable text -- запрещай только extra/unapproved, не сам факт readable text.
- Пространственные запреты: взгляд на объект -> исключай facing_camera/straight-on; over-shoulder -> исключай front-facing.
- Не запрещай присутствующих персонажей, обязательные атрибуты и референсные стили.

**Шаг 5 -- TECHNICAL:** size, aspect, guidance, steps, sampler, seed_policy (значения по умолчанию в JSON-схеме выше).

**Шаг 6 -- SELF-CHECK:**
- `_frame_plan` дословно совпадает с `frame_specs[i]`?
- Один T=0 момент, нет временных цепочек?
- Identity anchors и no-swap соблюдены?
- Readable texts авторизованы и обёрнуты в `^...^`?
- Фокус на текущем beat, нет элементов из других beat'ов?
- Консистентность с референсами, нет identity drift?

=== LAYER C: REFERENCE TABLES ===

**Команды пространственного маппинга (используй буквально, если соответствует beat):**
- смотрит в/на/к/за окно → "Position camera at [side/behind] to show character looking toward [object]"
- профиль → "Position camera at side angle"
- over-shoulder → "Frame over-shoulder view with character's back/shoulder in foreground, target ahead"
- субъективный взгляд → "Set point of view to subjective (POV)"

**Запрещённые формулировки (временные цепочки):**
"сначала", "потом", "после этого", "затем", "в этот момент", "наконец", "финальный кадр", "тем временем", "later", "then", "after that", "finally", "после того как", "just after". Вместо них описывай наблюдаемый результат в текущей позе и композиции.

**Шаблоны команд english_prompt (унифицированный формат):**
- Композиция: "Set up [shot type] composition; Arrange [left/center/right, fore/mid/background]"
- Позиционирование: "Place character [position] facing [direction]; Position [facing_camera/away/profile]"
- Освещение: "Apply [style] from [direction], [soft/hard] shadows, [temp]K" — НЕ смешивай K и warm/cool.
- Мультимодальность: "Combine character and location images; роли референсов в порядке reference_image_paths".

Противоречия: "facing camera" + "looking at [object]" запрещено одновременно.
    """
    system = system.replace("__PROMPT_LANGUAGE_LABEL__", prompt_language_label)

    # Батчинг: разбиваем на группы по BATCH_SIZE страниц для стабильного качества
    BATCH_SIZE = 5
    prompts: List[Dict[str, Any]] = []
    total = len(scene_packets)
    for batch_start in range(0, total, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total)
        batch_packets = scene_packets[batch_start:batch_end]
        batch_specs = frame_specs[batch_start:batch_end]
        batch_beats = beats[batch_start:batch_end] if batch_start < len(beats) else []

        payload = {
            "story_title": (story.get("title") or "").strip(),
            "scene_packets": batch_packets,
            "frame_specs": batch_specs,
            "beats": batch_beats,
            "characters": characters,
            "locations": locations,
            "style_images": style_images,
            "negative_list": negative_list,
            "references_root": f"{base}/20_bible/references",
            "batch_info": f"pages {batch_start + 1}-{batch_end} of {total}",
        }
        logger.info(
            "prompt_engineer: генерируем батч %d-%d из %d страниц",
            batch_start + 1, batch_end, total,
        )
        resp = call_openai_api(
            prompt=json.dumps(payload, ensure_ascii=False),
            system_prompt=system,
            model=model_code,
            max_tokens=_estimate_prompt_response_max_tokens(len(batch_packets)),
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        data = parse_llm_json(resp, fallback_list_key="prompts")
        batch_prompts: List[Dict[str, Any]] = data.get("prompts", [])
        _validate_prompts(batch_prompts, len(batch_packets))
        prompts.extend(batch_prompts)

    for prompt, scene_packet, frame_spec in zip(prompts, scene_packets, frame_specs):
        prompt["_frame_spec"] = frame_spec
        prompt["_frame_plan"] = {
            "selected_moment": str(frame_spec.get("selected_moment") or "").strip(),
            "primary_subject": str(frame_spec.get("primary_subject") or "").strip(),
            "excluded_sequence_parts": list(frame_spec.get("must_not_show") or []),
        }
        _sync_prompt_references_with_frame_spec(prompt, frame_spec, characters)
        logger.info(
            "prompt_engineer: запускаем финальный validator-rewriter для page_%02d",
            int(scene_packet.get("page_number") or 0),
        )
        _finalize_prompt_from_frame_spec(
            prompt,
            scene_packet,
            language,
            authoritative_readable_texts,
        )
        prompt.pop("_frame_spec", None)

    # Пост-обогащение: добавляем явные роли референсов (background/characters) в english_prompt
    try:
        # Индексы пути → имя сущности для быстрого поиска
        char_path_to_name = {}
        for ch in characters:
            path = (ch.get("reference_image_path") or "").strip()
            if path:
                char_path_to_name[path] = (ch.get("name") or "").strip()
        loc_path_to_name = {}
        for loc in locations:
            path = (loc.get("reference_image_path") or "").strip()
            if path:
                loc_path_to_name[path] = (loc.get("name") or "").strip()

        for p in prompts or []:
            refs = p.get("references") or {}
            if not isinstance(refs, dict):
                continue
            char_paths = refs.get("character_paths") or []
            loc_path = refs.get("location_path") or ""

            ordered_paths: List[str] = []
            if isinstance(char_paths, list):
                for cp in char_paths:
                    if isinstance(cp, str) and cp.strip():
                        ordered_paths.append(cp.strip())
            if isinstance(loc_path, str) and loc_path.strip():
                ordered_paths.append(loc_path.strip())

            if not ordered_paths:
                continue

            role_entries: List[str] = []
            for idx, rp in enumerate(ordered_paths, start=1):
                label = ""
                if rp in loc_path_to_name and loc_path_to_name[rp]:
                    labels = {
                        "ru": f"изображение {idx} как фон — {loc_path_to_name[rp]}",
                        "es": f"imagen {idx} como fondo — {loc_path_to_name[rp]}",
                        "fr": f"image {idx} comme decor — {loc_path_to_name[rp]}",
                        "de": f"Bild {idx} als Hintergrund — {loc_path_to_name[rp]}",
                        "en": f"image {idx} as background — {loc_path_to_name[rp]}",
                    }
                    label = labels.get(language, labels["en"])
                elif rp in char_path_to_name and char_path_to_name[rp]:
                    labels = {
                        "ru": f"изображение {idx} как персонаж — {char_path_to_name[rp]}",
                        "es": f"imagen {idx} como personaje — {char_path_to_name[rp]}",
                        "fr": f"image {idx} comme personnage — {char_path_to_name[rp]}",
                        "de": f"Bild {idx} als Figur — {char_path_to_name[rp]}",
                        "en": f"image {idx} as character — {char_path_to_name[rp]}",
                    }
                    label = labels.get(language, labels["en"])
                else:
                    labels = {
                        "ru": f"изображение {idx} как референс",
                        "es": f"imagen {idx} como referencia",
                        "fr": f"image {idx} comme reference",
                        "de": f"Bild {idx} als Referenz",
                        "en": f"image {idx} as reference",
                    }
                    label = labels.get(language, labels["en"])
                role_entries.append(label)

            if role_entries:
                roles_instruction = _build_reference_roles_instruction(language, role_entries)
                ep = (p.get("english_prompt") or "").strip()
                ep = _REFERENCE_ROLES_PATTERNS.get(language, _REFERENCE_ROLES_PATTERNS["en"]).sub("", ep).strip()
                ep = (ep + ("\n\n" + roles_instruction)).strip()
                p["english_prompt"] = ep
                p["reference_roles_instruction"] = roles_instruction
    except Exception as _:
        # Не блокируем пайплайн при ошибке обогащения
        pass

    out_dir = f"{base}/40_prompts"
    os.makedirs(out_dir, exist_ok=True)
    for idx, p in enumerate(prompts, start=1):
        with open(f"{out_dir}/page_{idx:02d}_prompt.json", "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
    return out_dir
