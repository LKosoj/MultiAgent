import os
import re
import json
import logging
import glob
import shutil
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import random
from custom_tools.image_tools import edit_image_vse_tool
from utils import log_smolagents_panel
from custom_tools.storybook.entity_generator_utils import build_canon_image_prompt
from custom_tools.storybook.screenplay_shots_generator_utils.shared_utils import (
    black_screen_storyboard_shot,
)

# Локальные импорты
from agent_factory import AgentFactory
from PIL import Image

logger = logging.getLogger(__name__)

_QUOTED_FRAGMENT_RE = re.compile(r"\^([^^\n]{1,80})\^|«([^»\n]{1,80})»|\"([^\"]{1,80})\"|'([^'\n]{1,80})'")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

def _write_solid_color_png(path: str, width: int, height: int, rgb: Tuple[int, int, int] = (0, 0, 0)) -> str:
    """Сохраняет однотонный RGB PNG (для BLACK SCREEN без вызова image API)."""
    _ensure_parent_dir(path)
    img = Image.new("RGB", (max(1, int(width)), max(1, int(height))), color=rgb)
    img.save(path, format="PNG")
    return os.path.abspath(path)


def _ensure_parent_dir(path: Optional[str]) -> None:
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception as e:
        logger.warning(f"Не удалось создать директорию для {path}: {e}")


def _get_source_language_name(language: str) -> str:
    names = {
        "ru": "Russian",
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
    }
    return names.get(language, language)


def _build_reference_index_lookup(item: Dict[str, Any]) -> Dict[str, int]:
    lookup: Dict[str, int] = {}
    for idx, path in enumerate(item.get("reference_image_paths") or [], start=1):
        if isinstance(path, str) and path.strip():
            lookup[path.strip()] = idx
    return lookup


def _strip_text_lock_markers(text: str) -> str:
    if not text:
        return text
    return re.sub(r"\^([^^\n]{1,120})\^", r"\1", text)


def _sanitize_anchor_literal_text(text: str, allowed_texts: List[str], language: str) -> str:
    if not text:
        return ""

    allowed = {str(entry).strip() for entry in (allowed_texts or []) if str(entry).strip()}

    def _replace(match: re.Match[str]) -> str:
        fragment = next((group for group in match.groups() if group), "").strip()
        if fragment in allowed:
            return match.group(0)
        return ""

    sanitized = _QUOTED_FRAGMENT_RE.sub(_replace, text)
    sanitized = re.sub(r"\s{2,}", " ", sanitized)
    sanitized = re.sub(r"\s+,", ",", sanitized)
    sanitized = re.sub(r"\s+([.;:])", r"\1", sanitized)
    sanitized = re.sub(r"\(\s*\)", "", sanitized)
    return sanitized.strip(" ,;.-")


def _build_character_identity_block(
    item: Dict[str, Any],
    language: str,
    allowed_texts: Optional[List[str]] = None,
) -> str:
    characters = item.get("characters") or []
    if not characters:
        return ""

    reference_index_lookup = _build_reference_index_lookup(item)
    entries: List[Dict[str, Any]] = []

    for character in characters:
        if not isinstance(character, dict):
            continue

        ref_path = str(character.get("reference_image_path") or "").strip()
        reference_index = reference_index_lookup.get(ref_path)
        if not reference_index:
            continue

        immutable = character.get("immutable_attributes") or {}
        variable = character.get("variable_attributes") or {}
        accessories = [
            accessory
            for accessory in (variable.get("accessories") or [])
            if isinstance(accessory, str) and accessory.strip()
        ]
        anchor_parts = [
            str(character.get("role") or "").strip(),
            str(immutable.get("face_shape") or "").strip(),
            str(immutable.get("skin_tone") or "").strip(),
            _sanitize_anchor_literal_text(str(variable.get("base_clothing") or "").strip(), allowed_texts or [], language),
            _sanitize_anchor_literal_text(str(accessories[0] or "").strip(), allowed_texts or [], language) if accessories else "",
            str(immutable.get("eye_color") or "").strip(),
            str(immutable.get("body_proportions") or "").strip(),
        ]
        anchor_parts = [part for part in anchor_parts if part]
        if not anchor_parts:
            continue

        anchor_text = "; ".join(anchor_parts[:5]).rstrip(".")
        entries.append(
            {
                "name": str(character.get("name") or f"character_{reference_index}").strip(),
                "reference_index": reference_index,
                "anchor_text": anchor_text,
            }
        )

    if not entries:
        return ""

    lines: List[str] = []
    for entry in entries:
        labels = {
            "ru": f'- Референс {entry["reference_index"]} / {entry["name"]}: сохрани эту каноническую идентичность — {entry["anchor_text"]}.',
            "es": f'- Referencia {entry["reference_index"]} / {entry["name"]}: conserva esta identidad canonica exacta — {entry["anchor_text"]}.',
            "fr": f'- Reference {entry["reference_index"]} / {entry["name"]}: conserve cette identite canonique exacte — {entry["anchor_text"]}.',
            "de": f'- Referenzbild {entry["reference_index"]} / {entry["name"]}: bewahre diese exakte kanonische Identitat — {entry["anchor_text"]}.',
            "en": f'- Reference image {entry["reference_index"]} / {entry["name"]}: keep this exact canonical identity — {entry["anchor_text"]}.',
        }
        lines.append(labels.get(language, labels["en"]))

    if not lines:
        return ""

    templates = {
        "ru": "Обязательные identity-anchors персонажей:\n{lines}\nНе humanize не-человеческих персонажей. Не меняй местами тела, лица, костюмы, аксессуары, реквизит и позиции персонажей между референсами.",
        "es": "Anclas obligatorias de identidad de personajes:\n{lines}\nNo humanices personajes no humanos. No intercambies cuerpos, rostros, vestuario, accesorios, utileria ni posiciones entre referencias.",
        "fr": "Ancrages d'identite obligatoires des personnages :\n{lines}\nN'humanise pas les personnages non humains. N'echange pas corps, visages, tenues, accessoires, accessoires de scene ni positions entre references.",
        "de": "Verbindliche Identitatsanker der Figuren:\n{lines}\nVermenschliche keine nichtmenschlichen Figuren. Vertausche keine Korper, Gesichter, Kleidung, Accessoires, Requisiten oder Positionen zwischen Referenzen.",
        "en": "Mandatory character identity anchors:\n{lines}\nDo not humanize non-human characters. Do not swap bodies, faces, outfits, accessories, props, or scene positions between references.",
    }
    return templates.get(language, templates["en"]).format(lines="\n".join(lines)).strip()


def _iter_visible_text_sources(item: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    sources: List[Tuple[str, str, str]] = []

    for character in item.get("characters") or []:
        if not isinstance(character, dict):
            continue
        owner_name = str(character.get("name") or character.get("role") or "character").strip()
        variable = character.get("variable_attributes") or {}
        base_clothing = variable.get("base_clothing")
        if isinstance(base_clothing, str) and base_clothing.strip():
            sources.append((base_clothing, owner_name, "character"))
        for accessory in variable.get("accessories") or []:
            if isinstance(accessory, str) and accessory.strip():
                sources.append((accessory, owner_name, "character"))

    for location in item.get("locations") or []:
        if not isinstance(location, dict):
            continue
        owner_name = str(location.get("name") or "location").strip()
        description = location.get("description")
        if isinstance(description, str) and description.strip():
            sources.append((description, owner_name, "location"))
        for key_object in location.get("key_objects") or []:
            if isinstance(key_object, str) and key_object.strip():
                sources.append((key_object, owner_name, "location"))

    return sources


def _build_reference_fidelity_block() -> str:
    return "Match all characters, props, and locations exactly to provided reference images. No generic substitutes, no extra background characters."


def _build_single_illustration_block() -> str:
    return "Single continuous illustration. No split panels, no comic layout, no panel borders."


def _build_ensemble_balance_block(item: Dict[str, Any], scene_prompt: str) -> str:
    characters = item.get("characters") or []
    if len(characters) < 3:
        return ""

    prompt_text = " ".join(str(scene_prompt or "").lower().split())
    ensemble_markers = ("ensemble", "ансамбл", "общий хаос", "shared catastrophe", "collective chaos")
    if not any(marker in prompt_text for marker in ensemble_markers):
        return ""

    character_names = [
        " ".join(str(character.get("name") or "").split()).strip()
        for character in characters
        if " ".join(str(character.get("name") or "").split()).strip()
    ]
    if not character_names:
        return ""

    return (
        "This is a multi-character ensemble scene, not a hero portrait. "
        "The shared catastrophe cluster and the involved cast must read more strongly than any one individual. "
        "Keep the named cast visually distinguishable in the same frame: "
        f"{', '.join(character_names)}. "
        "Do not let a single side character become an oversized foreground figure. "
        "If one character is a late observer or enters from the side, keep that character smaller and at the frame edge or midground so the central event stays dominant. "
        "A late observer must not be the closest figure to the camera and must not dominate the frame width."
    )


def _load_visible_text_story_context(item: Dict[str, Any]) -> Dict[str, Any]:
    project_id = item.get("project_id")
    page_number = item.get("page_number")
    if not project_id or not page_number:
        return {}

    base_dir = f"plots/storybooks/{project_id}"
    story_path = f"{base_dir}/20_story/story.json"
    beats_path = f"{base_dir}/10_synopsis/beats.json"
    context: Dict[str, Any] = {}

    try:
        if os.path.exists(story_path):
            with open(story_path, "r", encoding="utf-8") as f:
                story = json.load(f)
            story_page = next((page for page in story.get("pages", []) if page.get("page") == page_number), None)
            if isinstance(story_page, dict):
                context["story_page"] = {
                    "title": story_page.get("title", ""),
                    "body": story_page.get("body", ""),
                }
    except Exception as e:
        logger.warning(f"Не удалось загрузить story.json для visible text page {page_number}: {e}")

    try:
        if os.path.exists(beats_path):
            with open(beats_path, "r", encoding="utf-8") as f:
                beats = json.load(f)
            beat = next((entry for entry in beats if entry.get("page_number") == page_number), None)
            if isinstance(beat, dict):
                context["beat"] = {
                    "key_object": beat.get("key_object", ""),
                    "must_have_details": beat.get("must_have_details", ""),
                    "character_appearance_changes": beat.get("character_appearance_changes", ""),
                    "location_hint": beat.get("location_hint", ""),
                }
    except Exception as e:
        logger.warning(f"Не удалось загрузить beats.json для visible text page {page_number}: {e}")

    return context


def _normalize_visible_text_bindings_language(bindings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not bindings:
        return bindings

    from utils import needs_translation_to_english, translate_prompts_in_items

    indices_to_translate: List[int] = []
    translation_items: List[Dict[str, str]] = []

    for idx, binding in enumerate(bindings):
        translation_candidate = {
            "english_prompt": binding["carrier"],
            "negative_prompt": binding["visibility"],
        }
        if not needs_translation_to_english(translation_candidate):
            continue
        indices_to_translate.append(idx)
        translation_items.append(translation_candidate)

    if translation_items:
        translated_items = translate_prompts_in_items(
            translation_items,
            "en",
            max_workers=min(5, len(translation_items)),
        )
        if isinstance(translated_items, dict):
            translated_items = [translated_items]
        for idx, translated in zip(indices_to_translate, translated_items):
            bindings[idx]["carrier"] = " ".join(str(translated.get("english_prompt", "")).split()).strip()
            bindings[idx]["visibility"] = " ".join(str(translated.get("negative_prompt", "")).split()).strip()

    for binding in bindings:
        if _CYRILLIC_RE.search(binding["carrier"]) or _CYRILLIC_RE.search(binding["visibility"]):
            raise ValueError(
                "LLM не смог вернуть carrier/visibility на английском для обязательной надписи "
                f"'{binding['text']}'"
            )

    return bindings


def _collect_visible_text_bindings(
    item: Dict[str, Any],
    scene_prompt: str,
    language: str,
) -> List[Dict[str, str]]:
    cached = item.get("_visible_text_bindings")
    if isinstance(cached, list):
        return cached

    from utils import call_openai_api, parse_llm_json

    source_entries = _iter_visible_text_sources(item)
    story_context = _load_visible_text_story_context(item)
    candidate_pool: List[str] = [source_text for source_text, _, _ in source_entries]

    candidate_texts = []
    seen_candidates = set()
    for source_text in candidate_pool:
        for match in _QUOTED_FRAGMENT_RE.finditer(source_text):
            fragment = next((group for group in match.groups() if group), "").strip()
            normalized = " ".join(fragment.split()).strip()
            if normalized and normalized not in seen_candidates:
                seen_candidates.add(normalized)
                candidate_texts.append(normalized)

    llm_payload = {
        "scene_language": language,
        "scene_prompt": scene_prompt,
        "characters": [
            {
                "name": owner_name,
                "source_kind": owner_kind,
                "source_text": source_text,
            }
            for source_text, owner_name, owner_kind in source_entries
            if owner_kind == "character"
        ],
        "locations": [
            {
                "name": owner_name,
                "source_kind": owner_kind,
                "source_text": source_text,
            }
            for source_text, owner_name, owner_kind in source_entries
            if owner_kind == "location"
        ],
        "story_context": story_context,
        "candidate_texts": candidate_texts,
    }
    system_prompt = (
        "You extract mandatory readable in-scene text bindings for image generation.\n"
        "Return JSON only with shape "
        "{\"bindings\": [{\"text\": str, \"carrier\": str, \"visibility\": str}], "
        "\"ignored_texts\": [{\"text\": str, \"reason\": str}], "
        "\"rejected_candidates\": [{\"text\": str, \"reason\": str}]}.\n"
        "Rules:\n"
        "- Every candidate text from `candidate_texts` must be accounted for exactly once: either in `bindings`, in `ignored_texts`, or in `rejected_candidates`.\n"
        "- Decide binding at the LLM level from scene, bible, story, and beat context. Do not rely on a fixed carrier taxonomy.\n"
        "- Infer the carrier from the provided context itself, not from a predefined list of supported surface types.\n"
        "- Exact literal texts for characters must come only from `characters` source texts. Exact literal texts for locations/props must come only from `locations` source texts.\n"
        "- Use `rejected_candidates` only when a quoted fragment from source text is not actually an in-scene readable inscription/slogan/label/badge/sign text source.\n"
        "- Use `story_context` only to decide current-frame visibility, prominence, and which of the authoritative texts should be readable now.\n"
        "- Exclude dialogue and narration unless the same literal text is also present in authoritative character/location source data as in-scene text.\n"
        "- `carrier` must be a precise English description of the exact surface that owns the text in this frame.\n"
        "- `visibility` must be an explicit English requirement that the text stays readable on that exact carrier in the current frame.\n"
        "- If a text should not be readable in the current frame, put it into `ignored_texts` with a short English reason instead of omitting it silently.\n"
        "- Do not invent new texts, carriers, or visibility claims."
    )
    response = call_openai_api(
        prompt="Source data:\n" + json.dumps(llm_payload, ensure_ascii=False, indent=2),
        system_prompt=system_prompt,
        max_tokens=1400,
        temperature=0.1,
        response_format={"type": "json_object"},
        max_retries=2,
    )
    parsed = parse_llm_json(response, fallback_list_key="bindings")

    bindings: List[Dict[str, str]] = []
    seen = set()
    for raw_binding in parsed.get("bindings") or []:
        if not isinstance(raw_binding, dict):
            continue
        text = " ".join(str(raw_binding.get("text", "")).split()).strip()
        carrier = " ".join(str(raw_binding.get("carrier", "")).split()).strip()
        visibility = " ".join(str(raw_binding.get("visibility", "")).split()).strip()
        if not text or not carrier or not visibility:
            continue
        key = (text, carrier)
        if key in seen:
            continue
        seen.add(key)
        bindings.append({
            "text": text,
            "carrier": carrier,
            "visibility": visibility,
        })

    ignored_texts: Dict[str, str] = {}
    for raw_ignored in parsed.get("ignored_texts") or []:
        if not isinstance(raw_ignored, dict):
            continue
        text = " ".join(str(raw_ignored.get("text", "")).split()).strip()
        reason = " ".join(str(raw_ignored.get("reason", "")).split()).strip()
        if not text or not reason:
            continue
        ignored_texts[text] = reason

    rejected_candidates: Dict[str, str] = {}
    for raw_rejected in parsed.get("rejected_candidates") or []:
        if not isinstance(raw_rejected, dict):
            continue
        text = " ".join(str(raw_rejected.get("text", "")).split()).strip()
        reason = " ".join(str(raw_rejected.get("reason", "")).split()).strip()
        if not text or not reason:
            continue
        rejected_candidates[text] = reason

    bound_texts = {binding["text"] for binding in bindings}
    overlap = sorted(bound_texts.intersection(ignored_texts) | bound_texts.intersection(rejected_candidates) | set(ignored_texts).intersection(rejected_candidates))
    if overlap:
        raise ValueError(
            "LLM пометил candidate texts сразу в нескольких взаимоисключающих категориях: "
            f"{overlap}"
        )

    missing_texts = [
        text
        for text in candidate_texts
        if text not in bound_texts and text not in ignored_texts and text not in rejected_candidates
    ]
    if missing_texts:
        raise ValueError(
            "LLM не отчитался по всем authoritative candidate texts. Missing: "
            f"{missing_texts}"
        )

    if candidate_texts and not bindings and not ignored_texts and not rejected_candidates:
        raise ValueError(
            f"LLM не смог привязать обязательные надписи к носителю и видимости. Кандидаты: {candidate_texts}"
        )

    item["_visible_text_bindings"] = _normalize_visible_text_bindings_language(bindings)
    item["_ignored_visible_texts"] = [
        {"text": text, "reason": reason}
        for text, reason in sorted(ignored_texts.items())
    ]
    return item["_visible_text_bindings"]


def _render_visible_text_bindings(bindings: List[Dict[str, str]], language: str) -> str:
    if not bindings:
        return ""

    binding_lines = [
        f'- "{binding["text"]}" on {binding["carrier"]}, {binding["visibility"]}'
        for binding in bindings
    ]
    return (
        f"Readable text (exact {_get_source_language_name(language)}, no translation/paraphrase, each appears once only):\n"
        + "\n".join(binding_lines)
    ).strip()


def _render_hidden_authoritative_texts(ignored_texts: List[Dict[str, str]]) -> str:
    if not ignored_texts:
        return ""

    lines = []
    for entry in ignored_texts:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        lines.append(f'- "{text}" — NOT readable in this frame (hidden/occluded/cropped)')

    if not lines:
        return ""

    return ("Canonical texts that must stay unreadable here:\n" + "\n".join(lines)).strip()


def _render_readable_text_exclusivity(bindings: List[Dict[str, str]]) -> str:
    if not bindings:
        return "No readable text anywhere in the image."

    allowed_texts = "; ".join(f'"{binding["text"]}"' for binding in bindings)
    return f"Only allowed readable text: {allowed_texts}. No other text, labels, or letters anywhere."


def _augment_negative_prompt_for_text_fidelity(
    negative_prompt: str,
    bindings: List[Dict[str, str]],
    ignored_texts: Optional[List[Dict[str, str]]] = None,
) -> str:
    extras = ["unapproved readable text", "gibberish lettering", "extra labels"]
    if not bindings:
        extras.extend([
            "readable text",
            "subtitle overlay",
            "title card",
            "caption text",
            "credit text",
            "character name overlay",
            "lower-third label",
        ])

    # Скрытые canonical тексты — добавляем в negative вместо positive prompt,
    # чтобы не вводить их в контекст image-генератора
    for entry in (ignored_texts or []):
        text = str(entry.get("text") or "").strip()
        if text:
            extras.append(f'readable text "{text}"')

    parts: List[str] = []
    seen = set()
    for raw_part in str(negative_prompt or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = part.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(part)

    for extra in extras:
        normalized = extra.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(extra)

    return ", ".join(parts)


def _augment_negative_prompt_for_single_illustration(negative_prompt: str) -> str:
    # Компактный набор — 4 самых эффективных термина вместо 13 синонимов
    extras = [
        "split panel",
        "comic page layout",
        "panel borders",
        "diptych",
    ]
    # Семантические группы — если LLM уже добавил любой из синонимов, не добавляем extras из этой группы
    _collage_synonyms = {"split", "panel", "diptych", "triptych", "comic", "collage", "grid",
                         "сплит", "панел", "коллаж", "сетка", "триптих", "диптих", "разделен", "разделён"}

    parts: List[str] = []
    seen = set()
    has_collage_term = False
    for raw_part in str(negative_prompt or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = part.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(part)
        if any(syn in normalized for syn in _collage_synonyms):
            has_collage_term = True
    # Если LLM уже добавил anti-collage термины (на любом языке) — не дублируем
    if not has_collage_term:
        for extra in extras:
            normalized = extra.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            parts.append(extra)
    return ", ".join(parts)


def _augment_negative_prompt_for_ensemble_balance(negative_prompt: str, scene_prompt: str) -> str:
    prompt_text = " ".join(str(scene_prompt or "").lower().split())
    ensemble_markers = ("ensemble", "ансамбл", "общий хаос", "shared catastrophe", "collective chaos")
    if not any(marker in prompt_text for marker in ensemble_markers):
        return negative_prompt

    extras = [
        "single character portrait",
        "oversized foreground figure",
        "hero close-up",
        "anonymous background silhouettes",
        "cast hidden behind one character",
    ]
    parts: List[str] = []
    seen = set()
    for raw_part in str(negative_prompt or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = part.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(part)
    for extra in extras:
        normalized = extra.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(extra)
    return ", ".join(parts)


def _augment_negative_prompt_for_text_uniqueness(negative_prompt: str) -> str:
    extras = [
        "duplicate text",
        "extra copy of approved text",
    ]
    parts: List[str] = []
    seen = set()
    for raw_part in str(negative_prompt or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        normalized = part.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(part)
    for extra in extras:
        normalized = extra.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(extra)
    return ", ".join(parts)


def _build_image_generation_prompts(
    item: Dict[str, Any],
    scene_prompt: str,
    language: str,
) -> Tuple[str, str]:
    from utils import needs_translation_to_english, translate_prompts_in_items

    base_negative_prompt = item.get("negative_prompt") or (
        "watermark, text, logo, nsfw, distorted hands, extra fingers, extra limbs, lowres, deformed face"
    )
    visible_text_bindings = _collect_visible_text_bindings(item, scene_prompt, language)
    visible_text_fragments = [binding["text"] for binding in visible_text_bindings]
    ignored_texts = item.get("_ignored_visible_texts") or []
    identity_block = _build_character_identity_block(
        item,
        language,
        visible_text_fragments,
    )
    scene_prompt_for_translation = scene_prompt
    if identity_block:
        scene_prompt_for_translation = f"{scene_prompt}\n\n{identity_block}".strip()
    prompt_item = {
        "english_prompt": scene_prompt_for_translation,
        "negative_prompt": base_negative_prompt,
    }
    needs_translation = language != "en" or needs_translation_to_english(prompt_item)

    if needs_translation:
        translated_item = translate_prompts_in_items(
            prompt_item,
            "en",
            locked_fragments_by_field={"english_prompt": visible_text_fragments},
        )
        english_prompt = translated_item.get("english_prompt", scene_prompt)
        negative_prompt = translated_item.get("negative_prompt", base_negative_prompt)
    else:
        english_prompt = scene_prompt
        negative_prompt = base_negative_prompt

    english_prompt = _strip_text_lock_markers(english_prompt)
    negative_prompt = _strip_text_lock_markers(negative_prompt)

    bindings_block = _render_visible_text_bindings(visible_text_bindings, language)
    hidden_texts_block = _render_hidden_authoritative_texts(ignored_texts)
    text_exclusivity_block = _render_readable_text_exclusivity(visible_text_bindings)
    reference_fidelity_block = _build_reference_fidelity_block()
    single_illustration_block = _build_single_illustration_block()
    ensemble_balance_block = _build_ensemble_balance_block(item, scene_prompt)
    negative_prompt = _augment_negative_prompt_for_text_fidelity(negative_prompt, visible_text_bindings, ignored_texts)
    negative_prompt = _augment_negative_prompt_for_single_illustration(negative_prompt)
    negative_prompt = _augment_negative_prompt_for_text_uniqueness(negative_prompt)
    negative_prompt = _augment_negative_prompt_for_ensemble_balance(negative_prompt, scene_prompt)
    if bindings_block:
        english_prompt = f"{english_prompt}\n\n{bindings_block}".strip()
    if hidden_texts_block:
        english_prompt = f"{english_prompt}\n\n{hidden_texts_block}".strip()
    english_prompt = f"{english_prompt}\n\n{text_exclusivity_block}".strip()
    english_prompt = f"{english_prompt}\n\n{reference_fidelity_block}".strip()
    english_prompt = f"{english_prompt}\n\n{single_illustration_block}".strip()
    if ensemble_balance_block:
        english_prompt = f"{english_prompt}\n\n{ensemble_balance_block}".strip()

    return english_prompt, negative_prompt


def _handle_linked_shot(item: Dict[str, Any]) -> bool:
    """
    Обрабатывает кадр, связанный с предыдущим end кадром.
    Проверяет временные метки и копирует файл при необходимости.
    
    Returns:
        True - если файл обработан (скопирован или актуален), пропускаем генерацию
        False - нужна генерация изображения
    """
    source_path = item.get("source_end_path")
    target_path = item.get("output_path")
    
    if not source_path or not target_path:
        logger.warning("⚠️ Отсутствует source_end_path или output_path для связанного кадра")
        return False
    
    # Проверяем существование source файла
    if not os.path.exists(source_path):
        logger.warning(f"⚠️ Source файл не найден: {source_path}")
        return False
    
    # Проверяем, нужно ли копировать
    copy_needed = False
    if not os.path.exists(target_path):
        logger.info(f"📁 Target файл отсутствует, копируем: {os.path.basename(target_path)}")
        copy_needed = True
    else:
        source_mtime = os.path.getmtime(source_path)
        target_mtime = os.path.getmtime(target_path)
        
        if source_mtime > target_mtime:
            logger.info(f"🔄 Source файл новее, обновляем: {os.path.basename(target_path)}")
            copy_needed = True
        else:
            logger.info(f"✅ Target файл актуален: {os.path.basename(target_path)}")
            item["image_path"] = target_path
            return True  # файл актуален, пропускаем генерацию
    
    # Выполняем копирование
    if copy_needed:
        try:
            _ensure_parent_dir(target_path)
            shutil.copy2(source_path, target_path)
            item["image_path"] = target_path
            logger.info(f"✅ Файл успешно скопирован: {os.path.basename(source_path)} → {os.path.basename(target_path)}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка копирования {source_path} → {target_path}: {e}")
            return False
    
    return False


def _build_edit_instruction(
    session_id: str,
    item: Dict[str, Any],
    seed: Optional[int] = None,
    language: str = 'en'
) -> Tuple[str, List[str], str, str]:
    """
    Формирует задачу для агента artist_agent, принуждая использовать edit_image_vse_tool
    с заданными параметрами. Агент – ToolCallingAgent, поэтому передаём точные указания.
    Поддерживает редактирование как одиночного изображения, так и множественных изображений.
    """
    # Поддержка как одиночного изображения, так и массива
    base_image = item.get("image_path") or item.get("base_image_path")
    image_paths = item.get("image_paths") or item.get("base_image_paths") or []
    
    # Если есть одиночное изображение, добавляем его в массив
    if base_image and not image_paths:
        image_paths = [base_image]
    # Если нет базового изображения, работаем только с референсными (проверка будет позже)
    
    # Ограничиваем до 10 изображений (лимит edit_image_vse_tool)
    if len(image_paths) > 10:
        image_paths = image_paths[:10]

    # Создаем промпт для сцены
    scene_prompt = _build_scene_prompt(item, seed)
    english_prompt, negative_prompt = _build_image_generation_prompts(item, scene_prompt, language)
    #english_prompt += "\n\n То, чего не должно быть в результате редактирования: " + negative_prompt
    width = int(item.get("width", 1920))
    height = int(item.get("height", 1080))
    true_cfg_scale = item.get("true_cfg_scale", 7.0)
    steps = item.get("num_inference_steps", 65)
    # seed = item.get("seed", None)
    number = int(item.get("number", item.get("index", 1)))
    output_path = item.get("output_path")
    
    # Получаем project_id для формирования правильных путей к референсам
    project_id = item.get("project_id", "")
    reference_base = f"plots/storybooks/{project_id}/20_bible/references" if project_id else "plots/references"
    
    reference_paths = item.get("reference_image_paths") or item.get("references") or []
    
    # Добавляем детальное логирование для отладки
    logger.info(f"🔍 Исходные reference_paths (тип: {type(reference_paths)}): {reference_paths}")
    
    # Проверяем, если это строка, которая выглядит как список Python
    if isinstance(reference_paths, str):
        # Пытаемся распарсить строку как список Python
        if reference_paths.startswith('[') and reference_paths.endswith(']'):
            try:
                import ast
                reference_paths = ast.literal_eval(reference_paths)
                logger.info(f"🔧 Распарсили строку как список: {reference_paths}")
            except (ValueError, SyntaxError) as e:
                logger.warning(f"⚠️ Не удалось распарсить строку как список: {e}")
                reference_paths = [reference_paths]
        else:
            reference_paths = [reference_paths]
    
    # Исправляем пути к референсным изображениям, добавляя reference_base
    corrected_reference_paths = []
    for ref_path in reference_paths:
        if ref_path.startswith("/references/"):
            # Убираем ведущий слеш и заменяем "/references/" на reference_base
            corrected_path = os.path.join(reference_base, ref_path[12:])  # 12 = len("/references/")
            corrected_reference_paths.append(corrected_path)
        elif ref_path.startswith("references/"):
            # Убираем "references/" и заменяем на reference_base
            corrected_path = os.path.join(reference_base, ref_path[11:])  # 11 = len("references/")
            corrected_reference_paths.append(corrected_path)
        elif ref_path.startswith("plots/storybooks/") and "/97_shots/" in ref_path:
            # Это путь к start изображению - оставляем как есть
            corrected_reference_paths.append(ref_path)
        else:
            # Оставляем путь как есть, если он уже корректный
            corrected_reference_paths.append(ref_path)
    
    # Преобразуем в абсолютные пути
    abs_reference_paths = []
    for ref_path in corrected_reference_paths:
        if ref_path and not os.path.isabs(ref_path):
            abs_reference_paths.append(os.path.abspath(ref_path))
        else:
            abs_reference_paths.append(ref_path)
    
    reference_paths = abs_reference_paths
    
    # Логируем исправленные пути к референсам для отладки
    if reference_paths:
        logger.info(f"📚 Найдено {len(reference_paths)} референсных изображений:")
        for i, ref_path in enumerate(reference_paths, 1):
            exists = "✅" if os.path.exists(ref_path) else "❌"
            logger.info(f"  {i}. {exists} {ref_path}")

    # Жёсткая проверка: если референсы указаны, но каких-то файлов нет —
    # лучше упасть с понятной ошибкой, чем генерировать "не по референсам".
    # (пример: /references/characters/anesthesiologist.png отсутствует → модель импровизирует и стиль/лица плывут)
    if reference_paths:
        missing_refs = [p for p in reference_paths if p and not os.path.exists(p)]
        if missing_refs:
            raise FileNotFoundError(
                "Отсутствуют референсные изображения, генерация будет неконсистентной. "
                f"Missing ({len(missing_refs)}): {missing_refs}"
            )

    # Если не задан output_path — сформируем дефолтный путь с учетом количества изображений
    if not output_path:
        project_id = item.get("project_id")
        page_num = item.get("page_number") or item.get("page") or item.get("number") or 1
        try:
            page_num = int(page_num)
        except Exception:
            page_num = 1
        base_dir = (
            f"plots/storybooks/{project_id}/50_images/page_{page_num:02d}"
            if project_id and page_num else "plots"
        )
        os.makedirs(base_dir, exist_ok=True)
        # Единое имя финального файла редактирования
        output_path = os.path.join(base_dir, "img_final.png")

    # Гарантируем наличие директории и абсолютные пути для MCP
    if output_path and not os.path.isabs(output_path):
        output_path = os.path.abspath(output_path)
    _ensure_parent_dir(output_path)

    # Преобразуем относительные пути к изображениям в абсолютные для MCP
    abs_image_paths = []
    for img_path in image_paths:
        if img_path and not os.path.isabs(img_path):
            abs_image_paths.append(os.path.abspath(img_path))
        else:
            abs_image_paths.append(img_path)
    
    # Используем ТОЛЬКО существующие референсные изображения (без базового!)
    reference_only_paths = []
    for ref_path in reference_paths:
        if os.path.exists(ref_path):
            reference_only_paths.append(ref_path)
    
    # Применяем умный отбор референсов с учетом ограничения в 10 изображений
    # Определяем тип генерируемой сущности из контекста
    entity_type = "unknown"
    entity_data = {"name": "Scene"}
    
    # Пытаемся определить тип по контексту сцены
    characters = [c for c in (item.get("characters", []) or []) if isinstance(c, dict)]
    locations = [l for l in (item.get("locations", []) or []) if isinstance(l, dict)]
    
    if locations and len(locations) > 0:
        # Если есть локации в сцене, то скорее всего генерируем локацию
        entity_type = "location" 
        entity_data = locations[0] if locations else {"name": "Location"}
    elif characters and len(characters) > 0:
        # Если только персонажи, то генерируем персонажа или общую сцену
        entity_type = "character"
        entity_data = characters[0] if characters else {"name": "Character"}
    
    # Применяем умный отбор
    if len(reference_only_paths) > 10:
        logger.info(f"🎯 Применяем умный отбор из {len(reference_only_paths)} референсов для типа '{entity_type}'")
        reference_only_paths = _smart_select_references_for_generation(
            entity_type, entity_data, reference_only_paths, max_count=10
        )
    
    paths_list = reference_only_paths
    images_count = len(paths_list)
    ref_count = len(reference_only_paths)
    
    # Подсказка по референсам (агент перенесет стиль/идентику в prompt)
    ref_text = ""

    if reference_paths:
        ref_text = "\n\nREFERENCE IMAGES (для стилистической и персонажной консистентности):"
        for ref_path in paths_list:
            ref_text += f"\n- {ref_path}"
        ref_text += "\n\nОбязательно изучи референсные изображения и поддерживай идентичность персонажей и стиль локаций!"

    if not paths_list:
        raise ValueError("Не найдено существующих референсных изображений!")
    
    logger.info(f"🎨 Создаем изображение используя ТОЛЬКО {ref_count} референсных изображений (без базового):")
    
    # Используем negative_prompt из upstream/LLM-пайплайна без добавления захардкоженных semantic bans.
    scene_negative = negative_prompt

    # ВНИМАНИЕ: агенту явно укажем ВЫЗВАТЬ edit_image_vse_tool с параметрами ниже
    # Промпт строго на английском.
    # Агенту: НЕ ИЗМЕНЯТЬ session_id, ВСТАВИТЬ РЕАЛЬНЫЙ.
    
    # Форматируем список путей для передачи агенту
    image_paths_str = str(paths_list)
    
    instruction = f"""
Ты — художник-иллюстратор (artist_agent). Твоя задача — ОТРЕДАКТИРОВАТЬ референсные изображения согласно промпту:

СТРОГО СЛЕДУЙ ИНСТРУКЦИЯМ НИЖЕ:
1) Используй инструмент edit_image_vse_tool (а не generate_image_tool).
2) Обязательно укажи негативный промпт для исключения нежелательных элементов.
3) Вызови edit_image_vse_tool с аргументами prompt="{english_prompt}", image_paths={image_paths_str}, negative_prompt="{scene_negative}", session_id="{session_id}", output_path="{output_path}", seed={seed}

ВАЖНО - РАБОТАЕМ ТОЛЬКО С РЕФЕРЕНСНЫМИ ИЗОБРАЖЕНИЯМИ:
- В image_paths передаются {ref_count} референсных изображения персонажей и локаций
- edit_image_vse_tool объединит все референсы и отредактирует изображения согласно промпту
- Результат будет сохранен как одно финальное изображение

КРИТИЧЕСКИ ВАЖНО:
- Язык редактирования изображения (prompt) — ТОЛЬКО английский. Проверь, что переведены все слова, это очень важно!
- Передавай image_paths как список (массив) путей к референсным изображениям, сохраняя исходный порядок.
- ИСПОЛЬЗУЙ ТОЧНЫЕ ИМЕНА АРГУМЕНТОВ БЕЗ ПРОБЕЛОВ: prompt, image_paths, negative_prompt, session_id, output_path, seed
- В ответе НЕ выводи ничего лишнего, кроме результата вызова инструмента и финального пути к файлу.
"""
    return instruction.strip(), paths_list, scene_negative, english_prompt


def _parse_output_path(agent_output: str, session_id: str = None) -> Optional[str]:
    """Пытается извлечь путь к файлу PNG из ответа агента или найти по session_id."""
    if not agent_output:
        return None
    
    # Ищем явные пути к .png в выводе агента
    candidates = re.findall(r"[\w\-/\\\.]+\.png", agent_output, flags=re.IGNORECASE)
    if candidates:
        # Возвращаем первый существующий
        for p in candidates:
            if os.path.exists(p):
                return p
    
    # Если не нашли в выводе, ищем по шаблону имени файла с session_id
    if session_id:
        import glob
        pattern = f"plots/image*{session_id}.png"
        matching_files = glob.glob(pattern)
        if matching_files:
            # Возвращаем самый свежий файл
            latest_file = max(matching_files, key=os.path.getctime)
            return latest_file
    
    return None


def _analyze_reference_usage(items_data: List[Dict[str, Any]]) -> Dict[str, int]:
    """Анализирует использование референсных путей и возвращает счётчик повторов."""
    ref_counter = defaultdict(int)
    
    for item in items_data:
        ref_paths = item.get("reference_image_paths", [])
        if isinstance(ref_paths, str):
            ref_paths = [ref_paths]
        
        for ref_path in ref_paths:
            if ref_path:
                ref_counter[ref_path] += 1
    
    return dict(ref_counter)


def _find_entity_by_reference_path(items_data: List[Dict[str, Any]], ref_path: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Находит сущность по reference_image_path в данных всех сцен."""
    # Нормализуем путь до вида /references/...
    normalized_path = ref_path
    if "/20_bible/references/" in ref_path:
        normalized_path = "/references/" + ref_path.split("/20_bible/references/", 1)[1]
    
    # Ищем в characters и locations всех сцен
    for item in items_data:
        # Поиск в персонажах
        for character in item.get("characters", []):
            if not isinstance(character, dict):
                continue
            if character.get("reference_image_path") == normalized_path:
                return ("character", character)
        
        # Поиск в локациях
        for location in item.get("locations", []):
            if not isinstance(location, dict):
                continue
            if location.get("reference_image_path") == normalized_path:
                return ("location", location)
    
    return None


def _collect_project_references(project_id: str, entity_type: str, max_count: int = 10) -> List[str]:
    """Собирает до max_count существующих референсных файлов из проектных каталогов с приоритетом."""
    references_dir = f"plots/storybooks/{project_id}/20_bible/references"
    
    # Определяем приоритетный каталог
    primary_dir = f"{references_dir}/{entity_type}s/"  # characters/ или locations/
    secondary_dir = f"{references_dir}/{'locations' if entity_type == 'character' else 'characters'}/"
    
    collected = []
    
    # Сначала собираем из приоритетного каталога
    if os.path.exists(primary_dir):
        files = sorted(glob.glob(f"{primary_dir}*.png"))
        collected.extend(files[:max_count])
    
    # Если нужно, добираем из вторичного
    if len(collected) < max_count and os.path.exists(secondary_dir):
        files = sorted(glob.glob(f"{secondary_dir}*.png"))
        needed = max_count - len(collected)
        collected.extend(files[:needed])
    
    # Возвращаем только существующие файлы
    return [f for f in collected if os.path.exists(f)]


def _smart_select_references_for_generation(
    entity_type: str, 
    entity_data: Dict[str, Any], 
    available_references: List[str], 
    max_count: int = 10
) -> List[str]:
    """
    Умно выбирает референсы для генерации с учетом типа создаваемой сущности и ограничения в 10 изображений.
    
    Правила:
    - Для локации: обязательно включать существующую локацию (если есть), остальное - персонажи
    - Для персонажа: 4 персонажа (или сколько есть)
    
    Args:
        entity_type: 'character' или 'location' 
        entity_data: данные создаваемой сущности
        available_references: список доступных путей к референсам
        max_count: максимальное количество (по умолчанию 10)
    
    Returns:
        список выбранных путей к референсам (до max_count элементов)
    """
    if not available_references:
        return []
    
    # Фильтруем только существующие файлы
    existing_refs = [ref for ref in available_references if os.path.exists(ref)]
    
    if not existing_refs:
        logger.warning("Нет существующих референсных файлов")
        return []
    
    # Классифицируем референсы по типам на основе путей
    characters_refs = []
    locations_refs = []
    other_refs = []
    
    for ref_path in existing_refs:
        if '/characters/' in ref_path:
            characters_refs.append(ref_path)
        elif '/locations/' in ref_path:
            locations_refs.append(ref_path)
        else:
            other_refs.append(ref_path)
    
    logger.info(f"🔍 Классификация референсов: {len(characters_refs)} персонажей, {len(locations_refs)} локаций, {len(other_refs)} других")
    
    selected = []
    
    if entity_type == "location":
        # Для локации: сначала существующие локации, потом персонажи
        logger.info("📍 Генерация локации: приоритет существующим локациям")
        
        # Добавляем все доступные локации (но не более max_count)
        if locations_refs:
            selected.extend(locations_refs[:max_count])
            logger.info(f"  📍 Добавлено локаций: {len(selected)}")
        
        # Добираем персонажами до max_count
        remaining_slots = max_count - len(selected)
        if remaining_slots > 0 and characters_refs:
            selected.extend(characters_refs[:remaining_slots])
            logger.info(f"  👥 Добавлено персонажей: {min(remaining_slots, len(characters_refs))}")
        
        # Если еще есть место и есть другие референсы
        remaining_slots = max_count - len(selected)
        if remaining_slots > 0 and other_refs:
            selected.extend(other_refs[:remaining_slots])
            logger.info(f"  📁 Добавлено других: {min(remaining_slots, len(other_refs))}")
    
    elif entity_type == "character":
        # Для персонажа: максимум персонажей, потом локации если есть место
        logger.info("👤 Генерация персонажа: приоритет персонажам")
        
        # Добавляем всех доступных персонажей (но не более max_count)
        if characters_refs:
            selected.extend(characters_refs[:max_count])
            logger.info(f"  👥 Добавлено персонажей: {len(selected)}")
        
        # Добираем локациями до max_count
        remaining_slots = max_count - len(selected)
        if remaining_slots > 0 and locations_refs:
            selected.extend(locations_refs[:remaining_slots])
            logger.info(f"  📍 Добавлено локаций: {min(remaining_slots, len(locations_refs))}")
        
        # Если еще есть место и есть другие референсы
        remaining_slots = max_count - len(selected)
        if remaining_slots > 0 and other_refs:
            selected.extend(other_refs[:remaining_slots])
            logger.info(f"  📁 Добавлено других: {min(remaining_slots, len(other_refs))}")
    
    else:
        # Для неизвестного типа: берем все подряд до max_count
        logger.info(f"❓ Неизвестный тип '{entity_type}': берем все подряд")
        selected = existing_refs[:max_count]
    
    # Финальная проверка и логирование
    final_selected = selected[:max_count]  # на всякий случай обрезаем
    logger.info(f"✅ Финальный отбор для {entity_data.get('name', 'Unknown')} ({entity_type}): {len(final_selected)}/{max_count} референсов")
    
    for i, ref_path in enumerate(final_selected, 1):
        ref_type = "👤" if '/characters/' in ref_path else "📍" if '/locations/' in ref_path else "📁"
        logger.info(f"  {i}. {ref_type} {os.path.basename(ref_path)}")
    
    return final_selected


def _build_scene_prompt(item: Dict[str, Any], seed: Optional[int] = None) -> str:
    """Создаёт полный русскоязычный промпт для генерации изображения сцены."""
    base_prompt = item.get("english_prompt") or item.get("prompt_en") or ""
    
    # Если промпт уже есть, используем его как основу
    if base_prompt:
        # Переводим на русский для единообразия
        scene_prompt = base_prompt
    else:
        scene_prompt = "Создай изображение сцены. "
    
    #scene_prompt += "\n\nИнформация ниже дана как дополнительная, если она противоречит основному описанию сцены, за основу возьми основное описание:\n"

    # Добавляем информацию о персонажах
    #characters = item.get("characters", [])
    characters = []
    if characters:
        scene_prompt += "Персонажи в сцене: "
        for char in characters:
            char_name = char.get("name", "")
            if char_name:
                scene_prompt += f"{char_name}"
                # Добавляем ключевые характеристики персонажа
                if "age" in char:
                    scene_prompt += f" ({char['age']})"
                if "role" in char:
                    scene_prompt += f" - {char['role']}"
                
                # Добавляем внешность из immutable_attributes
                if "immutable_attributes" in char:
                    attrs = char["immutable_attributes"]
                    features = []
                    if "unique_features" in attrs and attrs["unique_features"]:
                        features.extend(attrs["unique_features"][:2])  # Первые 2 особенности
                    if features:
                        scene_prompt += f" (особенности: {', '.join(features)})"
                
                # Добавляем одежду из variable_attributes
                if "variable_attributes" in char and "base_clothing" in char["variable_attributes"]:
                    clothing = char["variable_attributes"]["base_clothing"]
                    scene_prompt += f" в {clothing}"
                
                scene_prompt += "; "
        scene_prompt = scene_prompt.rstrip("; ") + ". "
    
    # Добавляем информацию о локациях
    #locations = item.get("locations", [])
    locations = []
    if locations:
        scene_prompt += "Локация: "
        for loc in locations:
            loc_name = loc.get("name", "")
            if loc_name:
                scene_prompt += f"{loc_name}"
                
                # Добавляем описание локации
                if "description" in loc:
                    scene_prompt += f" - {loc['description']}"
                
                # Добавляем ключевые объекты
                if "key_objects" in loc and loc["key_objects"]:
                    objects = ", ".join(loc["key_objects"][:3])  # Первые 3 объекта
                    scene_prompt += f" (объекты: {objects})"
                
                # Добавляем атмосферу и освещение
                details = []
                if "atmosphere" in loc:
                    details.append(f"атмосфера: {loc['atmosphere']}")
                if "lighting" in loc:
                    details.append(f"освещение: {loc['lighting']}")
                if "color_palette" in loc and loc["color_palette"]:
                    colors = ", ".join(loc["color_palette"][:3])
                    details.append(f"цвета: {colors}")
                
                if details:
                    scene_prompt += f" ({', '.join(details)})"
                
                scene_prompt += "; "
        scene_prompt = scene_prompt.rstrip("; ") + ". "
    
    # Добавляем правила консистентности
    consistency_rules = item.get("consistency_rules", [])
    if consistency_rules:
        # Находим релевантные правила для этой сцены
        relevant_rules = []
        all_entity_names = set()
        
        # Собираем имена всех сущностей в сцене
        for char in characters:
            if char.get("name"):
                all_entity_names.add(char["name"])
        for loc in locations:
            if loc.get("name"):
                all_entity_names.add(loc["name"])
        
        # Ищем релевантные правила
        for rule in consistency_rules:
            applies_to = rule.get("applies_to", [])
            if any(entity in applies_to for entity in all_entity_names):
                relevant_rules.append(rule["rule"])
        
        if relevant_rules:
            scene_prompt += f"Важные правила: {'; '.join(relevant_rules[:2])}. "  # Максимум 2 правила
    
    #scene_prompt += "Сохрани визуальный стиль из референсных изображений: точно воспроизведите линии, цветовую палитру, технику освещения и общую эстетику."
    
    return scene_prompt


def _create_canon_reference(
    session_id: str,
    entity_type: str,
    entity_data: Dict[str, Any],
    reference_paths: List[str],
    output_path: str,
    consistency_rules: List[Dict[str, Any]],
    pipeline_type: str = "workflow",
    seed: Optional[int] = None
) -> bool:
    """Создаёт канонический референс через artist_agent."""
    try:
        # Создаём директорию если нужно
        _ensure_parent_dir(output_path)
        
        # Умная фильтрация референсов с учетом лимита в 10 изображений
        selected_references = _smart_select_references_for_generation(
            entity_type, entity_data, reference_paths, max_count=10
        )
        
        # ХАК: Исправляем опечатку в путях (storybook -> storybooks), если она есть
        fixed_references = []
        for ref in selected_references:
            if "plots/storybook/" in ref and "plots/storybooks/" not in ref:
                 fixed = ref.replace("plots/storybook/", "plots/storybooks/")
                 logger.warning(f"🔧 Исправлен путь референса: {ref} -> {fixed}")
                 fixed_references.append(fixed)
            else:
                 fixed_references.append(ref)
        selected_references = fixed_references
        
        if len(selected_references) == 0:
            logger.error(f"Нет подходящих референсных изображений для создания {entity_data.get('name', 'Unknown')}")
            return False
        
        logger.info(f"📚 Отобрано {len(selected_references)} референсов из {len(reference_paths)} для генерации {entity_data.get('name', 'Unknown')} ({entity_type})")
        
        # Формируем промпт через новый модуль
        prompt = build_canon_image_prompt(entity_type, entity_data, consistency_rules)

        # --- Подмешиваем единый визуальный стиль проекта (style_images.json) ---
        # Важно: для канон-референсов локаций иначе стиль может "плавать" (т.к. style_images — текстовые правила).
        # Мы добавляем это ДО перевода, чтобы translate_prompts_in_items перевёл и стиль тоже.
        def _infer_base_dir_from_output(p: str) -> Optional[str]:
            try:
                norm = (p or "").replace("\\", "/")
                marker = "/plots/storybooks/"
                if marker not in norm:
                    return None
                prefix, rest = norm.split(marker, 1)
                parts = rest.split("/")
                if not parts:
                    return None
                project_id_local = parts[0]
                return f"{prefix}{marker}{project_id_local}"
            except Exception:
                return None

        def _pick_style_variant_key(style_images: Dict[str, Any], entity_data_local: Dict[str, Any], output_path_local: str) -> Optional[str]:
            try:
                cp = (entity_data_local.get("reference_image_path") or "").lower()
                nm = (entity_data_local.get("name") or "").lower()
                op = (output_path_local or "").lower()
                candidates = []
                color_palette = style_images.get("color_palette")
                if isinstance(color_palette, dict):
                    candidates.extend([str(k) for k in color_palette.keys()])
                lighting = style_images.get("lighting")
                if isinstance(lighting, dict):
                    candidates.extend([str(k) for k in lighting.keys()])
                for key in candidates:
                    k = key.lower().strip()
                    if not k:
                        continue
                    if (k in cp) or (k in nm) or (k in op):
                        return key
            except Exception:
                return None
            return None

        base_dir = _infer_base_dir_from_output(output_path)
        if base_dir:
            style_images_path = f"{base_dir}/30_style/style_images.json"
            try:
                if os.path.exists(style_images_path):
                    with open(style_images_path, "r", encoding="utf-8") as f:
                        style_images = json.load(f) or {}
                else:
                    style_images = {}
            except Exception:
                style_images = {}

            if isinstance(style_images, dict) and style_images:
                style_chunks = []
                if style_images.get("art_style"):
                    style_chunks.append(f"Art style: {style_images['art_style']}.")
                # для локаций часто важно подтянуть конкретную палитру/свет из style_images по ключу
                variant_key = _pick_style_variant_key(style_images, entity_data, output_path)
                color_palette = style_images.get("color_palette")
                lighting = style_images.get("lighting")
                if variant_key and isinstance(color_palette, dict) and color_palette.get(variant_key):
                    style_chunks.append(f"Color palette hint ({variant_key}): {color_palette.get(variant_key)}.")
                elif style_images.get("color_palette"):
                    style_chunks.append(f"Color palette: {style_images['color_palette']}.")
                if variant_key and isinstance(lighting, dict) and lighting.get(variant_key):
                    style_chunks.append(f"Lighting hint ({variant_key}): {lighting.get(variant_key)}.")
                elif style_images.get("lighting"):
                    style_chunks.append(f"Lighting: {style_images['lighting']}.")
                if style_images.get("texture"):
                    style_chunks.append(f"Texture: {style_images['texture']}.")
                if style_images.get("detail_density"):
                    style_chunks.append(f"Detail level: {style_images['detail_density']}.")
                if style_images.get("model"):
                    style_chunks.append(f"Model/style notes: {style_images['model']}.")
                dni = style_images.get("do_not_include")
                if isinstance(dni, list) and dni:
                    dni_text = "; ".join([str(x) for x in dni if str(x).strip()])
                    if dni_text:
                        style_chunks.append(f"Do not include: {dni_text}.")

                if style_chunks:
                    prompt = f"{prompt}\n\nPROJECT VISUAL STYLE:\n" + " ".join(style_chunks)
        
        # Формируем негативный промпт в зависимости от типа сущности
        if entity_type == "location":
            negative_prompt = "people, person, character, human, man, woman, child, figure, watermark, text, logo"
        elif entity_type == "character":  
            negative_prompt = "complex background, detailed background, scenery, landscape, watermark, text, logo"
            # Универсально: запрещаем дрейф типа сущности (human/anthro/animal/robot)
            try:
                from custom_tools.storybook.entity_generator_utils.prompt_templates import get_character_nature
                nature = get_character_nature(entity_data)
                if nature == "human":
                    negative_prompt += (
                        ", animal, anthropomorphic, furry, fur, muzzle, snout, tail, paws, animal ears, "
                        "robot, android, cyborg, mechanical body, metal skin, screen face"
                    )
                elif nature == "anthropomorphic_animal":
                    negative_prompt += (
                        ", normal human, realistic human skin, robot, android, cyborg, mechanical body, metal skin, screen face"
                    )
                elif nature == "animal":
                    negative_prompt += (
                        ", human, person, biped, anthropomorphic, furry humanoid, robot, android, cyborg, mechanical body, metal skin, screen face"
                    )
                elif nature == "robot":
                    negative_prompt += (
                        ", animal, anthropomorphic, furry, fur, muzzle, snout, tail, paws, animal ears, "
                        "human skin, organic flesh, realistic human face"
                    )
            except Exception:
                # fail-safe: не ломаем генерацию, если импорт недоступен
                pass
        else:
            negative_prompt = "watermark, text, logo"
        
        # Переводим промпт на английский
        from utils import translate_prompts_in_items
        translated_item = translate_prompts_in_items({"prompt": prompt}, 'en')
        english_prompt = translated_item.get('english_prompt', prompt)
        
        # ОБХОДИМ ПРОБЛЕМУ С АГЕНТОМ: вызываем edit_image_vse_tool напрямую
        logger.info(f"🎨 Создаём канонический референс для {entity_data.get('name', 'Unknown')} ({entity_type}) НАПРЯМУЮ через edit_image_vse_tool")
        logger.info(f"📝 Промпт: {english_prompt[:200]}...")
        logger.info(f"🖼️ Референсов: {len(selected_references)}")
        logger.info(f"🚫 Негативный промпт: {negative_prompt}")
        
        try:
            result = edit_image_vse_tool(
                prompt=english_prompt,
                image_paths=selected_references,
                negative_prompt=negative_prompt,
                session_id=session_id,
                output_path=output_path,
                seed=seed
            )
            
            logger.info(f"📊 Результат вызова edit_image_vse_tool: {result}")
            
            # Проверяем результат
            if os.path.exists(output_path):
                logger.info(f"✅ Канонический референс создан: {output_path}")
                return True
            else:
                logger.warning(f"⚠️ Файл не создан: {output_path}")
                return False
        except Exception as tool_error:
            logger.error(f"❌ Ошибка вызова edit_image_vse_tool: {tool_error}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка создания канонического референса: {e}")
        return False


def _preprocess_canon_references(
    items_data: List[Dict[str, Any]],
    consistency_rules: List[Dict[str, Any]],
    session_id: str,
    max_concurrency: int,
    pipeline_type: str = "workflow",
    seed: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Предобрабатывает referencer и создаёт недостающие канонические только для элементов с output_path содержащим 20_bible/references/."""
    
    # Анализируем использование референсов
    ref_usage = _analyze_reference_usage(items_data)
    repeated_refs = {path: count for path, count in ref_usage.items() if count >= 2}
    
    if not repeated_refs:
        logger.info("📋 Повторно используемых референсов не найдено")
        return []
    
    logger.info(f"📋 Найдено {len(repeated_refs)} повторно используемых referencer:")
    for path, count in repeated_refs.items():
        logger.info(f"  • {path} (используется {count} раз)")
    
    # Определяем project_id из первого элемента
    project_id = items_data[0].get("project_id") if items_data else None
    if not project_id:
        logger.warning("⚠️ project_id не найден, пропускаем создание канонов")
        return []
    
    canon_tasks = []
    
    # Анализируем каждый повторный референс
    for ref_path in repeated_refs.keys():
        # Проверяем, находится ли уже в проектном каталоге И существует ли физически
        if f"/20_bible/references/" in ref_path:
            if os.path.exists(ref_path):
                logger.info(f"📁 Референс уже существует в проектном каталоге: {ref_path}")
                continue
            else:
                logger.info(f"🔍 Референс в проектном каталоге, но файл отсутствует: {ref_path}")
                # Продолжаем обработку для создания отсутствующего файла
        
        # Ищем соответствующую сущность
        entity_info = _find_entity_by_reference_path(items_data, ref_path)
        if not entity_info:
            logger.warning(f"⚠️ Не удалось найти сущность для {ref_path}")
            continue
        
        entity_type, entity_data = entity_info
        entity_name = entity_data.get("name", "Unknown")
        
        # Определяем целевой путь канона
        if f"/20_bible/references/" in ref_path:
            # Если файл уже должен быть в проектном каталоге, используем этот путь
            canon_path = os.path.abspath(ref_path)
        else:
            # Если файл внешний, строим путь в проектном каталоге
            canon_path = f"plots/storybooks/{project_id}/20_bible{entity_data.get('reference_image_path', '')}"
            canon_path = os.path.abspath(canon_path)
        
        # Проверяем, существует ли уже канон
        if os.path.exists(canon_path):
            logger.info(f"✅ Канонический референс уже существует: {canon_path}")
            continue
        
        # Собираем входные референсы
        all_available_refs = _collect_project_references(project_id, entity_type, max_count=10)  # Собираем больше для умного отбора
        if len(all_available_refs) < 1:
            logger.warning(f"⚠️ Недостаточно входных референсов для {entity_name} ({entity_type})")
            continue
        
        # Применяем умный отбор для соблюдения лимита в 10 изображений
        input_refs = _smart_select_references_for_generation(
            entity_type, entity_data, all_available_refs, max_count=10
        )
        
        # Исключаем целевой файл из входных
        input_refs = [r for r in input_refs if os.path.abspath(r) != canon_path]
        
        if len(input_refs) < 1:
            logger.warning(f"⚠️ После исключения целевого файла не осталось входных референсов для {entity_name}")
            continue
        
        logger.info(f"📝 Запланировано создание канона для {entity_name} ({entity_type}): {canon_path}")
        logger.info(f"    Входных референсов: {len(input_refs)}")
        
        canon_tasks.append({
            "entity_type": entity_type,
            "entity_data": entity_data,
            "reference_paths": input_refs,
            "output_path": canon_path,
            "original_ref_path": ref_path
        })
    
    if not canon_tasks:
        logger.info("📋 Нет задач для создания канонических референсов")
        return []
    
    logger.info(f"🚀 Создаём {len(canon_tasks)} канонических референсов с concurrency={max_concurrency}")
    
    # Выполняем создание канонов
    canon_results = []
    
    def _canon_worker(task: Dict[str, Any]) -> Dict[str, Any]:
        """Воркер для создания одного канонического референса."""
        success = _create_canon_reference(
            session_id=session_id,
            entity_type=task["entity_type"],
            entity_data=task["entity_data"],
            reference_paths=task["reference_paths"],
            output_path=task["output_path"],
            consistency_rules=consistency_rules,
            pipeline_type=pipeline_type,
            seed=seed
        )
        
        return {
            "task": task,
            "success": success,
            "output_path": task["output_path"] if success else None
        }
    
    # Параллельное выполнение задач канонов
    try:
        if max_concurrency <= 1:
            # Последовательное выполнение
            for task in canon_tasks:
                result = _canon_worker(task)
                canon_results.append(result)
        else:
            # Параллельное выполнение
            with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
                future_to_task = {executor.submit(_canon_worker, task): task for task in canon_tasks}
                
                for future in as_completed(future_to_task):
                    try:
                        result = future.result()
                        canon_results.append(result)
                    except Exception as e:
                        task = future_to_task[future]
                        logger.error(f"❌ Ошибка создания канона для {task['entity_data'].get('name', 'Unknown')}: {e}")
                        canon_results.append({
                            "task": task,
                            "success": False,
                            "error": str(e)
                        })
    
    except Exception as e:
        logger.error(f"❌ Ошибка параллельного создания канонов: {e}")
        # Fallback: последовательное выполнение
        canon_results.clear()
        for task in canon_tasks:
            result = _canon_worker(task)
            canon_results.append(result)
    
    # Логируем результаты
    successful_canons = [r for r in canon_results if r.get("success")]
    logger.info(f"📊 Создано канонических референсов: {len(successful_canons)}/{len(canon_tasks)}")
    
    for result in successful_canons:
        task = result["task"]
        entity_name = task["entity_data"].get("name", "Unknown")
        logger.info(f"  ✅ {entity_name} ({task['entity_type']}): {result['output_path']}")
    
    return canon_results


def _is_video_batch(items_obj: Dict[str, Any]) -> bool:
    """Определяет, являются ли элементы кадрами видео по наличию video_prompt у самих элементов.

    Поддерживаются обе структуры входа:
    - { "items": [ ... ] }
    - [ ... ]
    """
    try:
        # Вариант 1: объект с ключом items
        if isinstance(items_obj, dict):
            items_list = items_obj.get("items")
            if isinstance(items_list, list):
                for it in items_list:
                    if isinstance(it, dict) and str(it.get("video_prompt", "")).strip():
                        return True
                return False
            # Вариант 2: items_obj сам является массивом
            if isinstance(items_obj, list):
                for it in items_obj:
                    if isinstance(it, dict) and str(it.get("video_prompt", "")).strip():
                        return True
                return False
            # Фолбек: проверка верхнего уровня (на случай обратной совместимости)
            return bool(str(items_obj.get("video_prompt", "")).strip())
        # Если это сразу список элементов
        if isinstance(items_obj, list):
            for it in items_obj:
                if isinstance(it, dict) and str(it.get("video_prompt", "")).strip():
                    return True
            return False
    except Exception:
        return False
    return False


def _group_items_by_scenes(items_data: List[Dict[str, Any]], is_video: bool = False) -> Tuple[List[List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """
    Группирует элементы по сценам для видео-кадров и отдельно возвращает иллюстрации.
    
    Args:
        items_data: Список элементов для обработки
        is_video: Если True, все элементы считаются кадрами видео
    
    Returns:
        Tuple: (video_scenes, illustration_items)
        - video_scenes: Список сцен, где каждая сцена - список кадров
        - illustration_items: Список отдельных иллюстраций
    """
    if not is_video:
        # Все элементы - иллюстрации, добавляем индексы и возвращаем
        illustrations = []
        for i, item in enumerate(items_data):
            item_with_index = dict(item)
            item_with_index["original_index"] = i
            illustrations.append(item_with_index)
        return [], illustrations
    
    # Все элементы - видео-кадры, группируем по сценам
    video_frames = []
    for i, item in enumerate(items_data):
        # Добавляем индекс для сохранения порядка в результатах
        item_with_index = dict(item)
        item_with_index["original_index"] = i
        video_frames.append(item_with_index)
    
    # Группируем видео-кадры по сценам
    scenes = []
    if video_frames:
        # Группируем кадры по scene_number используя словарь
        scenes_dict = {}
        
        for frame in video_frames:
            scene_number = frame.get("scene_number")
            if scene_number not in scenes_dict:
                scenes_dict[scene_number] = []
            scenes_dict[scene_number].append(frame)
        
        # Преобразуем словарь в список сцен, отсортированный по номерам сцен
        for scene_number in sorted(scenes_dict.keys()):
            scene_frames = scenes_dict[scene_number]
            # Сортируем кадры внутри сцены по shot_number
            scene_frames.sort(key=lambda frame: frame.get("shot_number", 0))
            scenes.append(scene_frames)
    
    logger.info(f"📊 Группировка завершена: {len(scenes)} видео-сцен, 0 иллюстраций")
    for scene in scenes:
        scene_number = scene[0].get("scene_number", "unknown")
        shot_numbers = [frame.get("shot_number", "?") for frame in scene]
        logger.info(f"  📹 Сцена {scene_number}: {len(scene)} кадров (shots: {shot_numbers})")
    
    return scenes, []


def artist_agent_batch_edit_tool(
    session_id: str,
    items: Any,
    max_concurrency: int = 5,
    items_to_edit: Dict[int, Any] = {},
    pipeline_type: str = "workflow",
    enable: bool = True,
    language: str = 'en'
) -> str:
    """
    Пакетное редактирование изображений с использованием агента artist_agent в параллельном режиме.
    Поддерживает как одиночные, так и множественные изображения (до 10) через edit_image_vse_tool.
    Автоматически создаёт канонические референсы для повторно используемых изображений.
    
    Для видео-кадров (определяются по наличию video_prompt) применяется особая логика:
    - Кадры группируются по сценам
    - Сцены обрабатываются параллельно
    - Внутри каждой сцены кадры обрабатываются последовательно (для наследования)

    Args:
        session_id: Идентификатор сессии (будет передан агенту).
        items: Объект с данными или JSON-строка. Ожидаемая структура:
            {
                "items": [список сцен для обработки],
                "consistency_rules": [общие правила проекта]
            }
            Каждая сцена содержит поля:
            - image_path (одиночное изображение) ИЛИ image_paths (массив до 10 изображений)
            - english_prompt (или prompt_en)
            - video_prompt (опционально, определяет кадры видео)
            - negative_prompt
            - width, height, true_cfg_scale, number, output_path
            - reference_image_paths | references (str | List[str])
            - characters: [данные персонажей с reference_image_path]
            - locations: [данные локаций с reference_image_path]
            - project_id, page_number (для генерации базового изображения)
            - scene_number (для группировки видео-кадров)
            - shot_number (для сортировки кадров внутри сцены)
        max_concurrency: Максимальное число параллельных запусков агента (по умолчанию 5).
                         Используется как для создания канонов, так и для основного батча.
        items_to_edit: Список индексов элементов для редактирования.
        pipeline_type: Тип пайплайна для фабрики агентов.
        enable: Если True, выполняет генерацию изображений. Если False, пропускает выполнение и возвращает пустой результат.
        language: Язык генерации из пайплайна (для правильного перевода промптов).

    Returns:
        JSON-строка со списком результатов по каждому элементу.
    """

    if not session_id:
        raise ValueError("session_id обязателен")
    
    # Проверка параметра enable
    if not enable:
        logger.info("🚫 Генерация изображений отключена (enable=False), пропускаем выполнение")
        return json.dumps({
            "status": "skipped",
            "message": "Генерация изображений отключена параметром enable=False",
            "results": []
        }, ensure_ascii=False, indent=2)
    
    # Поддержка входа как JSON-строка
    if isinstance(items, str):
        try:
            items_obj = json.loads(items)
        except Exception as e:
            raise ValueError(f"items невалидный JSON: {e}")
    else:
        items_obj = items
    
    # Получаем project_id для чтения seed из brief.json
    temp_items_data = items_obj.get("items", []) if isinstance(items_obj, dict) else items_obj
    project_id_for_seed = temp_items_data[0].get("project_id") if temp_items_data else None
    
    # Читаем seed из brief.json или items_obj
    seed = random.randint(1, 1000000)  # Значение по умолчанию
    if project_id_for_seed:
        brief_path = f"plots/storybooks/{project_id_for_seed}/00_brief.json"
        if os.path.exists(brief_path):
            try:
                with open(brief_path, "r", encoding="utf-8") as f:
                    brief_data = json.load(f)
                seed = brief_data.get("seed", seed)
                logger.info(f"🎲 Используем seed из brief.json: {seed}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось прочитать seed из brief.json: {e}")
    
    # Проверяем новую структуру
    if isinstance(items_obj, dict) and "items" in items_obj:
        # Новая структура с каталогами
        items_data = items_obj.get("items", [])
        consistency_rules = items_obj.get("consistency_rules", [])
        # Если seed передан в items_obj, он имеет приоритет (для обратной совместимости)
        seed = items_obj.get("seed", seed)
        logger.info(f"📋 Получена новая структура данных: {len(items_data)} сцен, {len(consistency_rules)} правил")
        
        # Предобрабатываем канонические референсы
        logger.info("🔍 Запуск предобработки канонических референсов...")
        canon_results = _preprocess_canon_references(
            items_data=items_data,
            consistency_rules=consistency_rules,
            session_id=session_id,
            max_concurrency=1,
            pipeline_type=pipeline_type,
            seed=seed
        )
        logger.info(f"📝 Предобработка завершена, создано канонов: {len([r for r in canon_results if r.get('success')])}")
        
    elif isinstance(items_obj, list):
        # Старая структура - просто список
        items_data = items_obj
        consistency_rules = []
        logger.warning("⚠️ Получена старая структура данных (список), предобработка канонов отключена")
    else:
        raise ValueError("items должен быть объектом с ключом 'items' или списком")
    
    if not items_data:
        raise ValueError("items_data не может быть пустым")

    project_id = items_data[0].get("project_id") if items_data else None
    if project_id:
        logger.info("🔍 Проверка и генерация отсутствующих референсных изображений...")
        _ensure_references_exist(
            session_id=session_id,
            project_id=project_id,
            items_data=items_data,
            consistency_rules=consistency_rules,
            pipeline_type=pipeline_type,
            seed=seed
        )

    results: List[Dict[str, Any]] = []

    def _generate_base_image(
        session_id: str, 
        project_id: Optional[str], 
        page_number: Optional[int], 
        item: Dict[str, Any], 
        pipeline_type: str = "workflow",
        seed: Optional[int] = None
    ) -> str:
        """Генерирует базовое изображение через artist_agent, если не указано image_path."""
        spec = item.get("_shot_frame_spec") or item.get("shot_frame_spec")
        if black_screen_storyboard_shot(str(item.get("camera_plan") or ""), spec if isinstance(spec, dict) else None):
            base_dir = f"plots/storybooks/{project_id}/50_images/page_{page_number:02d}" if project_id and page_number else "plots"
            os.makedirs(base_dir, exist_ok=True)
            base_path = os.path.join(base_dir, "base.png")
            w = int(item.get("width", 1920))
            h = int(item.get("height", 1080))
            _write_solid_color_png(base_path, w, h, (0, 0, 0))
            logger.info(f"⬛ BLACK SCREEN: базовый кадр без API (локальный #000000) -> {base_path}")
            return os.path.abspath(base_path)
        # Путь для сохранения
        base_dir = f"plots/storybooks/{project_id}/50_images/page_{page_number:02d}" if project_id and page_number else "plots"
        os.makedirs(base_dir, exist_ok=True)
        base_path = os.path.join(base_dir, "base.png")
                
        # Получаем параметры для генерации
        # Создаем полный русскоязычный промпт для сцены
        scene_prompt = _build_scene_prompt(item)
        english_prompt = scene_prompt  # Будет переведен агентом на английский
        negative_prompt = item.get("negative_prompt") or (
            "watermark, text, logo, nsfw, distorted hands, extra fingers, extra limbs, lowres, deformed face"
        )
        width = int(item.get("width", 1920))
        height = int(item.get("height", 1080))
        true_cfg_scale = item.get("true_cfg_scale", 4.0)
        # Убираем неиспользуемые параметры
        # steps = item.get("num_inference_steps", 50)
        # seed = item.get("seed", None)
        number = int(item.get("number", item.get("index", 1)))
        
        # Формируем задачу для генерации базового изображения
        generation_task = f"""
Ты — художник-иллюстратор (artist_agent). Твоя задача — СОЗДАТЬ новое изображение.

СТРОГО СЛЕДУЙ ИНСТРУКЦИЯМ НИЖЕ:
1) Используй инструмент generate_image_tool (НЕ edit_image_tool).
2) Используй эту информацию для создания промпта для изображения. Переведи её на английский (проверь, что все слова переведены): "{english_prompt}"
3) Обязательно укажи negative_prompt (английский, не пустой).
4) Параметры вызова generate_image_tool должны быть ПЕРЕДАНЫ ЯВНО как именованные аргументы:
   - prompt: "prompt_on_english"
   - session_id: "{session_id}"
   - number: {number}
   - negative_prompt: "{negative_prompt}"
   - width: {width}
   - height: {height}
   - true_cfg_scale: {float(true_cfg_scale)}
   - output_path: "{base_path}"
   - seed: {seed}

КРИТИЧЕСКИ ВАЖНО:
- Язык описания изображения (prompt) — ТОЛЬКО английский.
- В ответе НЕ выводи ничего лишнего, кроме результата вызова инструмента и финального пути к файлу.
"""
        
        try:
            # Создаем агента для генерации базового изображения
            factory = AgentFactory()
            agent = factory.create_agent(
                profile_type='artist_agent',
                session_id=session_id,
                task=generation_task.strip(),
                pipeline_type=pipeline_type
            )
            if agent is None:
                raise RuntimeError("Не удалось создать агента 'artist_agent' для генерации базового изображения")

            output = agent.run(generation_task.strip(), stream=False)
            generated_path = _parse_output_path(str(output), session_id)
            
            logger.info(f"Агент сгенерировал: {generated_path}")
            
            if generated_path and os.path.exists(generated_path):
                # Перемещаем изображение на правильный путь
                import shutil
                try:
                    shutil.move(generated_path, base_path)
                    logger.info(f"Изображение перемещено: {generated_path} -> {base_path}")
                    return os.path.abspath(base_path)  # Возвращаем абсолютный путь
                except Exception as e:
                    logger.warning(f"Не удалось переместить изображение {generated_path} -> {base_path}: {e}")
                    return os.path.abspath(generated_path)  # Возвращаем абсолютный путь
            else:
                raise RuntimeError("Агент не сгенерировал базовое изображение")
                
        except Exception as e:
            logger.error(f"Ошибка генерации базового изображения: {e}")
            raise

    def _worker(index: int, item: Dict[str, Any], seed: Optional[int] = None) -> Dict[str, Any]:
        # Используем language из замыкания (переменная доступна из artist_agent_batch_edit_tool)
        try:
            # Проверяем, является ли это связанным кадром (из предыдущего end кадра)
            if item.get("copy_from_previous_end", False):
                logger.info(f"🔗 Элемент {index} связан с предыдущим end кадром, проверяем копирование")
                if _handle_linked_shot(item):
                    # Файл успешно скопирован или актуален
                    return {
                        "index": index,
                        "ok": True,
                        "output_path": item.get("output_path", ""),
                        "raw_output": f"Файл скопирован из предыдущего end кадра: {item.get('source_end_path', '')}"
                    }
                # Если копирование не удалось, продолжаем с обычной генерацией
                logger.warning(f"⚠️ Fallback на генерацию для связанного кадра {index}")
            
            # Проверяем, существует ли уже финальное изображение
            output_path = item.get("output_path", "")
            if output_path and os.path.exists(output_path):
                logger.info(f"🖼️ Изображение уже существует: {output_path}, пропускаем генерацию")
                return {
                    "index": index,
                    "ok": True,
                    "output_path": output_path,
                    "raw_output": f"Файл уже существует: {output_path}"
                }

            spec_bs = item.get("_shot_frame_spec") or item.get("shot_frame_spec")
            if black_screen_storyboard_shot(str(item.get("camera_plan") or ""), spec_bs if isinstance(spec_bs, dict) else None):
                out_bs = (item.get("output_path") or "").strip()
                if not out_bs:
                    logger.error("⬛ BLACK SCREEN: отсутствует output_path, не могу записать локальный кадр")
                    raise ValueError("BLACK SCREEN item requires output_path")
                if not os.path.isabs(out_bs):
                    out_bs = os.path.abspath(out_bs)
                w_bs = int(item.get("width", 1920))
                h_bs = int(item.get("height", 1080))
                _write_solid_color_png(out_bs, w_bs, h_bs, (0, 0, 0))
                logger.info(f"⬛ BLACK SCREEN: финальный кадр без API (локальный #000000) -> {out_bs}")
                return {
                    "index": index,
                    "ok": True,
                    "output_path": out_bs,
                    "raw_output": "black_screen_local_png",
                }
            
            # Проверяем, есть ли референсные изображения для работы
            reference_paths = item.get("reference_image_paths") or item.get("references") or []
            if reference_paths:
                logger.info(f"🎨 Найдены референсные изображения, используем ТОЛЬКО их (без базового изображения)")
                # Убираем base image из item, чтобы _build_edit_instruction работал только с референсами
                item = dict(item)
                item.pop("image_path", None)
                item.pop("base_image_path", None)
            else:
                # Если нет референсов и нет base image — сгенерируем базовое изображение
                if not item.get("image_path") and not item.get("base_image_path"):
                    project_id = item.get("project_id")
                    page_num = item.get("page_number") or item.get("page") or item.get("number") or 1
                    try:
                        page_num = int(page_num)
                    except Exception:
                        page_num = 1
                    
                    logger.info(f"Генерируем базовое изображение для страницы {page_num}")
                    base_image = _generate_base_image(session_id, project_id, page_num, item, pipeline_type, seed)
                    item = dict(item)
                    item["image_path"] = base_image

            # Механизм повторных попыток (до 3 раз)
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    task, paths_list, scene_negative, english_prompt = _build_edit_instruction(session_id=session_id, item=item, seed=seed, language=language)
                    
                    log_data = {
                        "🎨 Попытка": f"{attempt}/{max_attempts} - Элемент #{index}",
                        "📝 Промпт": english_prompt,
                        "🖼️  Изображений": len(paths_list),
                        "📁 Файлы": [paths_list],
                        "💾 Выходный путь": item.get("output_path")
                    }
                    if scene_negative.strip():
                        log_data["🚫 Негативный"] = scene_negative
                    
                    log_smolagents_panel(
                        content=log_data,
                        title="🎨 Artist Generation Process (attempt)",
                        title_style="bold blue",
                        border_style="blue"
                    )

                    result = edit_image_vse_tool(
                        prompt=english_prompt,
                        image_paths=paths_list,
                        session_id=session_id,
                        output_path=item.get("output_path"),
                        seed=seed,
                        width=int(item.get("width", 1920)),
                        height=int(item.get("height", 1080)),
                    )
                    log_data = {
                        "🎨 Результат": result,
                        "💾 Выходный путь": item.get("output_path")
                    }
                    log_smolagents_panel(
                        content=log_data,
                        title="🎨 Artist Generation Process (result)",
                        title_style="bold blue",
                        border_style="blue"
                    )

                    file_path = item.get("output_path")
                    
                    # Проверяем, что файл действительно создан
                    if file_path and os.path.exists(file_path):
                        logger.info(f"✅ Файл успешно создан на попытке {attempt}: {file_path}")
                        return {
                            "index": index,
                            "ok": True,
                            "output_path": file_path,
                            "raw_output": str(result)[:4000],
                            "attempts": attempt
                        }
                    else:
                        logger.warning(f"⚠️ Попытка {attempt}/{max_attempts} не создала файл: {file_path}")
                        if attempt < max_attempts:
                            continue  # Повторяем попытку
                        else:
                            return {
                                "index": index,
                                "ok": False,
                                "output_path": file_path,
                                "raw_output": str(result)[:4000],
                                "error": f"Файл не создан после {max_attempts} попыток",
                                "attempts": attempt
                            }
                            
                except Exception as e:
                    logger.error(f"❌ Ошибка на попытке {attempt}/{max_attempts} для элемента {index}: {e}")
                    if attempt < max_attempts:
                        continue  # Повторяем попытку
                    else:
                        return {
                            "index": index, 
                            "ok": False, 
                            "error": f"Все {max_attempts} попытки неудачны. Последняя ошибка: {str(e)}",
                            "attempts": attempt
                        }
                        
        except Exception as e:
            logger.error(f"Критическая ошибка обработки элемента {index}: {e}")
            return {"index": index, "ok": False, "error": str(e)}

    # Определяем, является ли это батчем видео-кадров
    is_video_batch = _is_video_batch(items_obj)
    logger.info(f"🎭 Тип батча: {'📹 Видео-кадры' if is_video_batch else '🖼️ Иллюстрации'}")
    
    # Группируем элементы по сценам (если это видео) или оставляем как иллюстрации
    video_scenes, illustration_items = _group_items_by_scenes(items_data, is_video_batch)
    
    # Определяем параллельность
    parallel_generation = items_obj.get("parallel_generation", True)
    seed = items_obj.get("seed", None)
    # Для видео-сцен: сцены обрабатываются параллельно независимо от parallel_generation
    scenes_max_concurrency = max_concurrency if is_video_batch else (max_concurrency if parallel_generation else 1)
    # Для иллюстраций: уважаем parallel_generation
    illustrations_max_concurrency = max_concurrency if parallel_generation else 1
    
    def _process_scene_sequentially(scene_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Обрабатывает кадры одной сцены последовательно."""
        scene_results = []
        scene_number = scene_items[0].get("scene_number", "unknown")
        logger.info(f"🎬 Обработка сцены {scene_number}: {len(scene_items)} кадров (последовательно)")
        
        for item in scene_items:
            original_index = item["original_index"]
            result = _worker(original_index, item, seed)
            scene_results.append(result)
            
            if result.get("ok"):
                logger.info(f"  ✅ Кадр {original_index+1} обработан: {result.get('output_path')}")
            else:
                logger.warning(f"  ❌ Кадр {original_index+1} не обработан: {result.get('error', 'unknown error')}")
        
        return scene_results
    
    # Основной батч обработки
    logger.info(f"🎬 Запуск основного батча обработки...")
    
    try:
        if is_video_batch and video_scenes:
            # Обрабатываем видео-кадры по сценам
            logger.info(f"📹 Обработка {len(video_scenes)} видео-сцен")
            
            if scenes_max_concurrency <= 1:
                # Последовательная обработка сцен
                logger.info(f"🔄 Последовательная обработка сцен (concurrency=1)")
                for scene in video_scenes:
                    scene_results = _process_scene_sequentially(scene)
                    results.extend(scene_results)
            else:
                # Параллельная обработка сцен
                logger.info(f"🚀 Параллельная обработка {len(video_scenes)} сцен с concurrency={scenes_max_concurrency}")
                
                with ThreadPoolExecutor(max_workers=scenes_max_concurrency) as executor:
                    # Отправляем каждую сцену как отдельную задачу
                    future_to_scene = {
                        executor.submit(_process_scene_sequentially, scene): i 
                        for i, scene in enumerate(video_scenes)
                    }
                    
                    # Собираем результаты по мере завершения
                    for future in as_completed(future_to_scene):
                        scene_index = future_to_scene[future]
                        try:
                            scene_results = future.result()
                            results.extend(scene_results)
                            logger.info(f"✅ Сцена {scene_index+1} завершена: {len(scene_results)} кадров")
                        except Exception as e:
                            logger.error(f"❌ Ошибка обработки сцены {scene_index+1}: {e}")
                            # Добавляем ошибки для всех кадров сцены
                            scene = video_scenes[scene_index]
                            for item in scene:
                                results.append({
                                    "index": item["original_index"],
                                    "ok": False,
                                    "error": f"Ошибка обработки сцены: {str(e)}"
                                })
        
        elif illustration_items:
            # Обрабатываем иллюстрации как раньше
            logger.info(f"🖼️ Обработка {len(illustration_items)} иллюстраций")
            
            if illustrations_max_concurrency <= 1:
                # Последовательная обработка иллюстраций
                logger.info(f"🔄 Последовательная обработка иллюстраций (concurrency=1)")
                for item in illustration_items:
                    original_index = item["original_index"]
                    logger.info(f"🎨 Обрабатываем иллюстрацию {original_index+1}/{len(illustration_items)}")
                    result = _worker(original_index, item, seed)
                    results.append(result)
                    if result.get("ok"):
                        logger.info(f"✅ Иллюстрация {original_index+1} обработана: {result.get('output_path')}")
                    else:
                        logger.warning(f"❌ Иллюстрация {original_index+1} не обработана: {result.get('error', 'unknown error')}")
            else:
                # Параллельная обработка иллюстраций
                logger.info(f"🚀 Параллельная обработка {len(illustration_items)} иллюстраций с concurrency={illustrations_max_concurrency}")
                
                with ThreadPoolExecutor(max_workers=illustrations_max_concurrency) as executor:
                    # Отправляем все иллюстрации в очередь
                    future_to_item = {
                        executor.submit(_worker, item["original_index"], item, seed): item["original_index"]
                        for item in illustration_items
                    }
                    
                    # Собираем результаты по мере завершения
                    for future in as_completed(future_to_item):
                        original_index = future_to_item[future]
                        try:
                            result = future.result()
                            results.append(result)
                            
                            if result.get("ok"):
                                logger.info(f"✅ Иллюстрация {original_index+1} обработана: {result.get('output_path')}")
                            else:
                                logger.warning(f"❌ Иллюстрация {original_index+1} не обработана: {result.get('error', 'unknown error')}")
                                
                        except Exception as e:
                            logger.error(f"❌ Ошибка в future для иллюстрации {original_index+1}: {e}")
                            results.append({
                                "index": original_index,
                                "ok": False,
                                "error": f"Future ошибка: {str(e)}"
                            })
        
        else:
            logger.warning("⚠️ Нет элементов для обработки")
                        
    except Exception as parallel_error:
        logger.warning(f"⚠️ Ошибка параллельного выполнения: {parallel_error}")
        logger.info("🔄 Переключаемся на последовательное выполнение...")
        
        # Fallback: последовательное выполнение всех элементов
        results.clear()  # Очищаем частичные результаты
        all_items = illustration_items if not is_video_batch else [item for scene in video_scenes for item in scene]
        
        for item in all_items:
            original_index = item["original_index"]
            logger.info(f"🎨 Обрабатываем элемент {original_index+1}/{len(all_items)} (fallback)")
            result = _worker(original_index, item)
            results.append(result)
            if result.get("ok"):
                logger.info(f"✅ Элемент {original_index+1} обработан: {result.get('output_path')}")
            else:
                logger.warning(f"❌ Элемент {original_index+1} не обработан: {result.get('error', 'unknown error')}")

    # Сортируем по index для стабильности
    results.sort(key=lambda x: x.get("index", 0))
    return json.dumps(results, ensure_ascii=False, indent=2)


def _generate_image_from_scratch(
    session_id: str,
    project_id: str,
    protagonist_data: Dict[str, Any],
    output_path: str,
    pipeline_type: str
) -> bool:
    """Генерирует изображение протагониста с нуля, используя логику из protagonist_initializer."""
    base_dir = f"plots/storybooks/{project_id}"
    
    # Собираем промпт на основе данных о персонаже
    name = protagonist_data.get("name") or "Hero"
    imm = protagonist_data.get("immutable_attributes", {})
    var = protagonist_data.get("variable_attributes", {})
    prompt = (
        f"Full-body portrait of {name} as main protagonist, neutral pose, neutral background, "
        f"face_shape: {imm.get('face_shape','')}, eye_color: {imm.get('eye_color','')}, "
        f"skin_tone: {imm.get('skin_tone','')}, body_proportions: {imm.get('body_proportions','')}, "
        f"unique_features: {', '.join(imm.get('unique_features', []))}. "
        f"base_clothing: {var.get('base_clothing','')}, base_hairstyle: {var.get('base_hairstyle','')}, "
        f"accessories: {', '.join(var.get('accessories', []))}. "
    )

    # Добавляем информацию о стиле из файлов
    style_images_path = f"{base_dir}/30_style/style_images.json"
    negative_list_path = f"{base_dir}/30_style/negative_prompt_list.txt"
    style_images: Dict[str, Any] = {}
    try:
        if os.path.exists(style_images_path):
            with open(style_images_path, "r", encoding="utf-8") as f:
                style_images = json.load(f) or {}
    except Exception:
        style_images = {}

    # Собираем негативный промпт
    negative_prompt = "watermark, text, logo, nsfw, lowres, extra limbs, complex background, detailed background, scenery, landscape"
    try:
        if os.path.exists(negative_list_path):
            with open(negative_list_path, "r", encoding="utf-8") as f:
                nl = (f.read() or "").strip()
                if nl:
                    negative_prompt = nl
    except Exception:
        pass
    
    try:
        dni = style_images.get("do_not_include")
        if isinstance(dni, list) and dni:
            extra_neg = ", ".join([str(x) for x in dni if str(x).strip()])
            if extra_neg:
                negative_prompt = f"{negative_prompt}, {extra_neg}" if negative_prompt.strip() else extra_neg
    except Exception:
        pass

    # Добавляем стили в основной промпт
    style_chunks = []
    if style_images.get("art_style"): style_chunks.append(f"Art style: {style_images['art_style']}.")
    if style_images.get("color_palette"): style_chunks.append(f"Color palette: {style_images['color_palette']}.")
    if style_images.get("composition_rules"): style_chunks.append(f"Composition: {style_images['composition_rules']}.")
    if style_images.get("lighting"): style_chunks.append(f"Lighting: {style_images['lighting']}.")
    if style_images.get("texture"): style_chunks.append(f"Texture: {style_images['texture']}.")
    if style_images.get("detail_density"): style_chunks.append(f"Detail level: {style_images['detail_density']}.")
    if style_images.get("model"): style_chunks.append(f"Prefer model: {style_images['model']}.")
    if style_chunks:
        prompt = f"{prompt} {' '.join(style_chunks)}"
    
    # Формируем задачу для агента
    task = f"""
Ты — художник-иллюстратор (artist_agent). Сгенерируй изображение протагониста.
ВАЖНО: Перед генерацией усиль негативный промпт, добавив диаметрально противоположную стилистику к основному промпту.
Анализируй основной промпт: {prompt}
Если в основном промпте:
- реалистичное изображение → добавь в негативный промпт: cartoon, anime, stylized, illustrated, drawing
- мультяшный стиль → добавь: photorealistic, realistic, photography, hyperrealistic  
- детская иллюстрация → добавь: mature, adult, serious, dark, gritty
- темная атмосфера → добавь: bright, cheerful, colorful, happy, light
- минималистичный стиль → добавь: busy, cluttered, complex, detailed, ornate
- детализированный стиль → добавь: simple, minimal, basic, plain, undetailed
Базовый негативный промпт: "{negative_prompt}"
Используй generate_image_tool c параметрами:
  - prompt: "english_prompt" - строго на английском языке, его нужно сгенерировать на основе {prompt}
  - session_id: "{session_id}"
  - number: 1
  - negative_prompt: "усиленный_негативный_промпт"
  - width: 1920
  - height: 1080
  - true_cfg_scale: 5.0
  - num_inference_steps: 50
  - output_path: "{output_path}"
В ответе верни только финальный путь к файлу.
"""
    try:
        factory = AgentFactory()
        agent = factory.create_agent(
            profile_type='artist_agent',
            session_id=session_id,
            task=task.strip(),
            pipeline_type=pipeline_type
        )
        output = agent.run(task.strip(), stream=False)
        gen_path = _parse_output_path(str(output), session_id)
        if gen_path and os.path.exists(gen_path):
            if os.path.abspath(gen_path) != os.path.abspath(output_path):
                _ensure_parent_dir(output_path)
                shutil.move(gen_path, output_path)
            return True
        return os.path.exists(output_path)
    except Exception as e:
        logger.error(f"Ошибка генерации изображения с нуля для {name}: {e}")
        return False


def _ensure_references_exist(
    session_id: str,
    project_id: str,
    items_data: List[Dict[str, Any]],
    consistency_rules: List[Dict[str, Any]],
    pipeline_type: str,
    seed: Optional[int] = None
):
    """Проверяет наличие всех изображений в image_paths содержащих 20_bible/references/ и генерирует недостающие."""
    
    base_dir = f"plots/storybooks/{project_id}/20_bible"

    # Загружаем bible-данные, чтобы при генерации недостающих канонов
    # использовать ПОЛНОЕ описание персонажа/локации (а не фиктивные заглушки).
    # Иначе теряется признак "человек" / entity_nature и появляются антро-животные/роботы.
    bible_characters: List[Dict[str, Any]] = []
    bible_locations: List[Dict[str, Any]] = []
    try:
        from .items_builder import load_bible_data
        bible_characters, bible_locations, _ = load_bible_data(project_id)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить bible данные для {project_id}: {e}")
        bible_characters, bible_locations = [], []

    def _to_reference_path(abs_or_rel_path: str) -> str:
        """Преобразует абсолютный путь .../20_bible/references/... в '/references/...'. """
        p = (abs_or_rel_path or "").replace("\\", "/")
        marker = "/20_bible/references/"
        if marker in p:
            tail = p.split(marker, 1)[1].lstrip("/")
            return "/references/" + tail
        # если уже /references/...
        if p.startswith("/references/"):
            return p
        if p.startswith("references/"):
            return "/" + p
        return p
    
    def resolve_path(ref_path):
        if not ref_path:
            return ""
        if ref_path.startswith('/references/'):
            return os.path.abspath(os.path.join(base_dir, ref_path.lstrip('/')))
        if ref_path.startswith('references/'):
            return os.path.abspath(os.path.join(base_dir, ref_path))
        return os.path.abspath(ref_path)

    # 1. Сбор всех отсутствующих файлов из image_paths и reference_image_paths, которые содержат 20_bible/references/
    missing_canonical_files = set()
    
    for item in items_data:
        # Проверяем image_paths
        image_paths = item.get("image_paths", []) or item.get("base_image_paths", []) or []
        if isinstance(image_paths, str):
            image_paths = [image_paths]
            
        # Проверяем reference_image_paths  
        ref_paths = item.get("reference_image_paths", []) or item.get("references", []) or []
        if isinstance(ref_paths, str):
            ref_paths = [ref_paths]
        
        # Объединяем все пути
        all_paths = list(image_paths) + list(ref_paths)
        
        for path in all_paths:
            if not path:
                continue
                
            resolved = resolve_path(path)
            
            # Проверяем, относится ли путь к 20_bible/references/
            if "20_bible/references/" in resolved:
                if not os.path.exists(resolved):
                    missing_canonical_files.add(resolved)
                    logger.info(f"🔍 Найден отсутствующий канонический файл: {resolved} (исходный: {path})")
    
    if not missing_canonical_files:
        logger.info("📋 Отсутствующих канонических файлов не найдено (все файлы в 20_bible/references/ существуют)")
        return
    
    logger.info(f"📋 Найдено {len(missing_canonical_files)} отсутствующих канонических файлов для создания")
    
    # Преобразуем в формат для совместимости с остальной логикой
    all_entities = {}
    for file_path in missing_canonical_files:
        # Определяем тип сущности по пути
        if "/characters/" in file_path:
            entity_type = "character"
        elif "/locations/" in file_path:
            entity_type = "location"
        else:
            entity_type = "unknown"

        ref_path_norm = _to_reference_path(file_path)

        # Пытаемся найти сущность в bible по reference_image_path
        entity_data: Dict[str, Any] = {}
        try:
            if entity_type == "character" and bible_characters:
                found = next((c for c in bible_characters if (c.get("reference_image_path") or "").strip() == ref_path_norm), None)
                if isinstance(found, dict):
                    entity_data = dict(found)
            elif entity_type == "location" and bible_locations:
                found = next((l for l in bible_locations if (l.get("reference_image_path") or "").strip() == ref_path_norm), None)
                if isinstance(found, dict):
                    entity_data = dict(found)
        except Exception:
            entity_data = {}

        # Fallback: создаём минимальную сущность, но reference_image_path держим в /references/ формате
        if not entity_data:
            entity_data = {
                "name": os.path.splitext(os.path.basename(file_path))[0],
                "reference_image_path": ref_path_norm
            }

        all_entities[file_path] = (entity_type, entity_data)
    
    if not all_entities:
        logger.info("Отсутствующих канонических файлов не найдено после обработки.")
        return

    # 2. Проверка существования файлов (base_dir и resolve_path уже определены выше)

    # Все файлы уже отфильтрованы как отсутствующие
    missing_paths = list(missing_canonical_files)
    entities_by_full_path = {}
    for file_path, (entity_type, entity_data) in all_entities.items():
        entities_by_full_path[file_path] = (entity_type, entity_data)

    logger.info(f"Обнаружено {len(missing_paths)} отсутствующих канонических файлов: {missing_paths}")
    
    # Собираем все существующие референсы проекта для использования в генерации
    existing_paths = []
    project_refs_dir = f"plots/storybooks/{project_id}/20_bible/references"
    if os.path.exists(project_refs_dir):
        for root, dirs, files in os.walk(project_refs_dir):
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                    existing_paths.append(os.path.join(root, file))

    # 3. Генерация протагониста, если он отсутствует и других референсов нет
    try:
        from .items_builder import load_bible_data
        all_characters, _, _ = load_bible_data(project_id)
        protagonist = all_characters[0] if all_characters else None
        protagonist_ref_path = resolve_path(protagonist.get("reference_image_path")) if protagonist else None

        if protagonist_ref_path and protagonist_ref_path in missing_paths and not existing_paths:
            logger.info("Протагонист отсутствует и нет других референсов. Генерируем с нуля...")
            if _generate_image_from_scratch(session_id, project_id, protagonist, protagonist_ref_path, pipeline_type):
                logger.info(f"Изображение протагониста успешно создано: {protagonist_ref_path}")
                existing_paths.append(protagonist_ref_path)
                missing_paths.remove(protagonist_ref_path)
            else:
                logger.error("Не удалось сгенерировать изображение протагониста. Продолжаем с остальными.")
    except ImportError:
        logger.error("Не удалось импортировать load_bible_data, пропуск генерации протагониста.")
    except Exception as e:
        logger.error(f"Ошибка при генерации протагониста: {e}")

    # 4. Последовательная генерация остальных недостающих референсов
    for path_to_create in missing_paths:
        if not existing_paths:
            logger.warning(f"Нет существующих референсов для создания {os.path.basename(path_to_create)}. Пропуск.")
            continue
        
        entity_info = entities_by_full_path.get(path_to_create)
        if not entity_info:
            logger.warning(f"Не найдена информация о сущности для {os.path.basename(path_to_create)}. Пропуск.")
            continue

        entity_type, entity_data = entity_info
        logger.info(f"Генерируем '{entity_data.get('name')}' используя {len(existing_paths)} существующих референсов...")
        
        # Применяем умный отбор для соблюдения лимита
        selected_refs = _smart_select_references_for_generation(
            entity_type, entity_data, existing_paths, max_count=10
        )
        
        logger.info(f"📚 После умного отбора: {len(selected_refs)} из {len(existing_paths)} референсов")

        if _create_canon_reference(
            session_id=session_id,
            entity_type=entity_type,
            entity_data=entity_data,
            reference_paths=selected_refs,
            output_path=path_to_create,
            consistency_rules=consistency_rules,
            pipeline_type=pipeline_type,
            seed=seed
        ):
            logger.info(f"Референс успешно создан: {path_to_create}")
            existing_paths.append(path_to_create)
        else:
            logger.error(f"Не удалось создать референс: {path_to_create}")
