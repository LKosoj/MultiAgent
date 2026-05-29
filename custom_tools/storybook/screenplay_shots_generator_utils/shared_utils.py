"""
Модуль общих функций для генерации кадров
"""

from typing import List, Dict, Any, Optional
from .timing_utils import _time_str_to_seconds
import copy
import os
import json
import hashlib
import re
from agent_command import model_code, model_hard, model_ultimate, model_lite
from utils import call_openai_api, parse_llm_json
import logging

import threading

logger = logging.getLogger(__name__)

# Глобальная блокировка для записи в locations.json
_locations_file_lock = threading.Lock()

_shot_frame_spec_lock = threading.Lock()
_SHOT_FRAME_SPEC_CACHE: Dict[str, Dict[str, Any]] = {}


def _sanitize_visual_feature(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if lowered.startswith(("привычка ", "манера ", "склонность ", "обычай ", "обыкновение ")):
        return ""
    text = re.sub(r"«[^»]*»", "", text)
    text = re.sub(r'"[^"]*"', "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    return text


def _build_character_visual_profiles(characters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    profiles: List[Dict[str, Any]] = []
    for char in characters:
        name = str(char.get("name") or "").strip()
        if not name:
            continue

        immutable = char.get("immutable_attributes") if isinstance(char.get("immutable_attributes"), dict) else {}
        variable = char.get("variable_attributes") if isinstance(char.get("variable_attributes"), dict) else {}

        visible_anchor_features: List[str] = []
        seen_features = set()
        for raw_feature in immutable.get("unique_features", []) or []:
            feature = _sanitize_visual_feature(raw_feature)
            if not feature:
                continue
            key = feature.casefold()
            if key in seen_features:
                continue
            seen_features.add(key)
            visible_anchor_features.append(feature)
            if len(visible_anchor_features) >= 3:
                break

        profile = {
            "name": name,
            "face_shape": str(immutable.get("face_shape") or "").strip(),
            "eyes": str(immutable.get("eye_color") or "").strip(),
            "skin_tone": str(immutable.get("skin_tone") or "").strip(),
            "body_proportions": str(immutable.get("body_proportions") or "").strip(),
            "hairstyle": str(variable.get("base_hairstyle") or "").strip(),
            "clothing": str(variable.get("base_clothing") or "").strip(),
            "visible_accessories": [str(x).strip() for x in (variable.get("accessories") or []) if str(x).strip()][:4],
            "visible_anchor_features": visible_anchor_features,
            "reference_image_path": str(char.get("reference_image_path") or "").strip(),
        }
        profiles.append({k: v for k, v in profile.items() if v})
    return profiles


def _build_shot_character_visual_profiles(
    character_visual_profiles: List[Dict[str, Any]],
    shot_frame_spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not isinstance(character_visual_profiles, list) or not character_visual_profiles:
        return []

    profile_index = {}
    for profile in character_visual_profiles:
        name = str((profile or {}).get("name") or "").strip()
        if not name:
            continue
        profile_index[name.casefold()] = profile

    if not isinstance(shot_frame_spec, dict) or not shot_frame_spec:
        return list(character_visual_profiles)

    primary_subject = str(shot_frame_spec.get("primary_subject") or "").strip()
    visible_characters = [
        str(name or "").strip()
        for name in (shot_frame_spec.get("visible_characters") or [])
        if str(name or "").strip()
    ]
    if primary_subject and primary_subject.casefold() not in {name.casefold() for name in visible_characters}:
        visible_characters = [primary_subject] + visible_characters
    if not visible_characters:
        return list(character_visual_profiles)

    shot_profiles: List[Dict[str, Any]] = []
    for order, name in enumerate(visible_characters, start=1):
        base_profile = dict(profile_index.get(name.casefold(), {"name": name}))
        role = "primary_subject" if primary_subject and name.casefold() == primary_subject.casefold() else "secondary_visible_character"
        distinct_from = [other for other in visible_characters if other.casefold() != name.casefold()]
        if role:
            base_profile["shot_role"] = role
        base_profile["shot_order"] = order
        if distinct_from:
            base_profile["must_remain_distinct_from"] = distinct_from
        shot_profiles.append(base_profile)
    return shot_profiles


def _coerce_str_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for v in values:
        s = str(v or "").strip()
        if s:
            out.append(s)
    return out


def enrich_shot_frame_spec_environment_delta_via_llm(
    shot_frame_spec: Dict[str, Any],
    *,
    video_prompt: str = "",
) -> Dict[str, Any]:
    """
    Если `transition_spec.environment_delta` пуст, один вызов model_lite решает,
    нужно ли явно описать изменение видимого окружения между START и END (без эвристик).

    Не подменяет уже заполненный extraction-LLM `environment_delta`.
    При ошибке API возвращает исходный spec без изменений.
    """
    if not isinstance(shot_frame_spec, dict) or not shot_frame_spec:
        return dict(shot_frame_spec) if isinstance(shot_frame_spec, dict) else {}
    out = copy.deepcopy(shot_frame_spec)
    trans = out.get("transition_spec")
    if not isinstance(trans, dict):
        trans = {}
        out["transition_spec"] = trans
    env_existing = _coerce_str_list(trans.get("environment_delta"))
    if env_existing:
        return out

    system_prompt = """Ты аналитик одного storyboard-шота (пара кадров START→END).

По переданному shot_frame_spec и (если есть) video_prompt определи:
нужно ли в transition_spec.environment_delta явно описать изменение **видимого окружения**
между START и END при **той же локации**.

МОЖНО (изменения уже видимых элементов окружения):
- параллакс/сдвиг деталей фона из-за движения камеры;
- изменение глубины резкости, бокэ, световых пятен, отражений (особенно в крупном плане);
- плотность дыма/тумана/пыли/осадков;
- направление, температура или интенсивность УЖЕ присутствующего источника света;
- частицы, искры, листья, поднятые движением субъекта или ветром, который уже описан в шоте.

НЕЛЬЗЯ (это придумывание, а не delta):
- новые объекты, мебель, персонажи, ориентиры, которых нет в shot_description / shot_frame_spec / video_prompt;
- новые источники света (свеча, окно, фонарь), если их нет на входе;
- новое погодное явление, которого нет в shot_description;
- собственные сюжетные события, не подразумеваемые входом.

Если кадр явно заявлен как полностью статичный (камера и среда без относительного сдвига) — needs_environment_delta=false.

Верни СТРОГО JSON:
{"needs_environment_delta": true|false, "environment_delta": ["короткая строка на русском", ...]}

При needs_environment_delta=false поле environment_delta должно быть [].
При true — 1–2 строки, только следствия из входа."""

    payload = {
        "shot_frame_spec": out,
        "video_prompt": (video_prompt or "").strip(),
    }
    try:
        response = call_openai_api(
            prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            model=model_lite,
            max_tokens=900,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        obj = parse_llm_json(response) or {}
        if not bool(obj.get("needs_environment_delta")):
            return out
        delta = obj.get("environment_delta")
        if not isinstance(delta, list):
            return out
        lines = [str(x).strip() for x in delta if str(x).strip()]
        if lines:
            trans["environment_delta"] = lines
    except Exception as e:
        logger.warning("enrich_shot_frame_spec_environment_delta_via_llm: %s", e)
    return out


def _patch_value_is_meaningful(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return len(v) > 0
    if isinstance(v, dict):
        return len(v) > 0
    return True


def _deep_merge_shot_frame_spec_patch(base: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """Накладывает patch на base: непустые значения из patch дополняют/заменяют пустые в base; dict — рекурсивно."""
    if not isinstance(patch, dict):
        return
    for key, pv in patch.items():
        if key not in base:
            if _patch_value_is_meaningful(pv):
                base[key] = copy.deepcopy(pv)
            continue
        bv = base[key]
        if isinstance(pv, dict) and isinstance(bv, dict):
            _deep_merge_shot_frame_spec_patch(bv, pv)
        elif isinstance(pv, list) and isinstance(bv, list):
            if _patch_value_is_meaningful(pv):
                base[key] = list(pv)
        elif isinstance(pv, str) and isinstance(bv, str):
            if (not bv.strip()) and pv.strip():
                base[key] = pv
        elif _patch_value_is_meaningful(pv) and not _patch_value_is_meaningful(bv):
            base[key] = copy.deepcopy(pv)


_SHOT_FRAME_SPEC_COMPLETE_SYSTEM = """Ты дополняешь нормализованный shot_frame_spec (после парсинга JSON извлечения).

Ты заменяешь бывшую эвристическую логику: заполнение пустых полей, согласование START/END и transition.

Правила:
- Не придумывай новые объекты, персонажей, реквизит и события вне уже переданного spec.
- Если scene_mode = object_focus или environment и лицо не должно читаться — не заполняй facial_expression/gaze пустыми шаблонами; оставь пусто или сохрани «не применимо», если так во входе.
- Если transition_spec описывает смену опоры/физики между START и END — при необходимости уточни start_state_spec.world_physics так, чтобы START не противоречил END и transition (как раньше делала гармонизация).
- Заполни пустые pose_signature / facial_expression / gaze_direction только там, где это следует из must_show, world_physics и phase (start/end).
- Если affect_delta пуст — заполни только если логически следует из уже заданных выражений/взглядов/физики между START и END; иначе оставь [].
- При необходимости добавь character_pose_signatures для primary_subject, если заполняешь pose_signature.

Верни СТРОГО JSON — объект PATCH: только ветки и поля, которые нужно установить или заменить.
Ключи верхнего уровня допускаются: start_state_spec, end_state_spec, transition_spec, pose_signature, facial_expression, gaze_direction.
Внутри — только непустые поля для записи. Не дублируй весь spec."""


def _complete_shot_frame_spec_fields_via_llm(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Заменяет бывшие эвристики _infer_* и _harmonize_phase_physics одним вызовом model_lite.
    При ошибке API возвращает spec без изменений.
    """
    if not isinstance(spec, dict) or not spec:
        return spec
    out = copy.deepcopy(spec)
    try:
        response = call_openai_api(
            prompt="NORMALIZED_SHOT_FRAME_SPEC:\n" + json.dumps(out, ensure_ascii=False),
            system_prompt=_SHOT_FRAME_SPEC_COMPLETE_SYSTEM,
            model=model_lite,
            max_tokens=4000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        patch = parse_llm_json(response)
        if isinstance(patch, dict) and patch:
            _deep_merge_shot_frame_spec_patch(out, patch)
    except Exception as e:
        logger.warning("_complete_shot_frame_spec_fields_via_llm: %s", e)
    return out


def _sanitize_readable_texts_via_llm(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Санитизирует readable_text поля:
    - в visible_readable_texts допускаются только визуально читаемые надписи в кадре (вывески, шильдики, UI и т.п.)
    - реплики/междометия/звуки персонажей (например, "Moment...", "А-а-а!", "Хмм") не должны попадать в visible;
      их нужно переносить в hidden_readable_texts.
    """
    if not isinstance(spec, dict) or not spec:
        return spec

    out = copy.deepcopy(spec)
    try:
        response = call_openai_api(
            prompt="SHOT_FRAME_SPEC:\n" + json.dumps(out, ensure_ascii=False),
            system_prompt=(
                "Ты санитайзер readable_text полей в storyboard shot_frame_spec.\n"
                "Критерий visible_readable_texts: только тексты, которые физически существуют в мире кадра\n"
                "как письменный элемент (вывеска, табличка, шильдик, дорожный знак, экранный UI, надпись на форме,\n"
                "наклейка, граффити, обложка книги, светящееся табло). Эмоциональная окраска самого текста\n"
                "(STOP!, АВАРИЯ, DANGER) НЕ делает надпись устной — если это надпись на объекте, оставь её visible.\n"
                "Запрещено держать в visible_readable_texts: устные реплики персонажей, междометия, крики,\n"
                "звукоподражания, мысли (например: 'Moment...', 'А-а-а!', 'Эй!', 'Хмм', 'wooosh'). Такие строки\n"
                "перенеси в hidden_readable_texts.\n"
                "Примеры:\n"
                "  RIGHT visible: 'вывеска „Кафе Восток\"', 'знак „STOP\"', 'нашивка „SECURITY\" на куртке',\n"
                "                 'табло „GATE 12\"', 'граффити „NO FUTURE\" на стене'.\n"
                "  WRONG visible (перенести в hidden): 'персонаж кричит „А-а!\"', 'герой бормочет „Moment...\"',\n"
                "                 'звук удара „БАМ!\"', 'внутренний голос „надо бежать\"'.\n"
                "Ничего не выдумывай и не добавляй новых текстов. Только переразложи уже существующие значения.\n"
                "Верни СТРОГО JSON вида:\n"
                "{\n"
                "  \"visible_readable_texts\": [\"...\"],\n"
                "  \"hidden_readable_texts\": [\"...\"],\n"
                "  \"start_state_spec\": {\"visible_readable_texts\": [\"...\"], \"hidden_readable_texts\": [\"...\"]},\n"
                "  \"end_state_spec\": {\"visible_readable_texts\": [\"...\"], \"hidden_readable_texts\": [\"...\"]}\n"
                "}"
            ),
            model=model_lite,
            max_tokens=900,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        obj = parse_llm_json(response) or {}

        def _apply_lists(target: Dict[str, Any], patch: Dict[str, Any]) -> None:
            if not isinstance(target, dict) or not isinstance(patch, dict):
                return
            vis = patch.get("visible_readable_texts")
            hid = patch.get("hidden_readable_texts")
            if isinstance(vis, list):
                target["visible_readable_texts"] = [str(x).strip() for x in vis if str(x).strip()]
            if isinstance(hid, list):
                target["hidden_readable_texts"] = [str(x).strip() for x in hid if str(x).strip()]

        _apply_lists(out, obj if isinstance(obj, dict) else {})
        if isinstance(out.get("start_state_spec"), dict):
            _apply_lists(out["start_state_spec"], obj.get("start_state_spec") if isinstance(obj, dict) else {})
        if isinstance(out.get("end_state_spec"), dict):
            _apply_lists(out["end_state_spec"], obj.get("end_state_spec") if isinstance(obj, dict) else {})
    except Exception as e:
        logger.warning("_sanitize_readable_texts_via_llm: %s", e)
    return out


def _build_phase_shot_character_visual_profiles(
    shot_character_visual_profiles: List[Dict[str, Any]],
    shot_frame_spec: Dict[str, Any],
    *,
    state_key: str,
) -> List[Dict[str, Any]]:
    if not isinstance(shot_character_visual_profiles, list) or not shot_character_visual_profiles:
        return []
    if not isinstance(shot_frame_spec, dict) or not shot_frame_spec:
        return [dict(profile or {}) for profile in shot_character_visual_profiles]

    state = shot_frame_spec.get(state_key) or {}
    if not isinstance(state, dict):
        state = {}

    primary_subject = str(state.get("primary_subject") or shot_frame_spec.get("primary_subject") or "").strip()
    fallback_pose = str(state.get("pose_signature") or "").strip()
    raw_pose_map = state.get("character_pose_signatures") or {}
    pose_map = {
        str(name or "").casefold(): str(signature or "").strip()
        for name, signature in raw_pose_map.items()
        if str(name or "").strip() and str(signature or "").strip()
    }

    result: List[Dict[str, Any]] = []
    for profile in shot_character_visual_profiles:
        item = dict(profile or {})
        name = str(item.get("name") or "").strip()
        phase_pose = pose_map.get(name.casefold(), "")
        if not phase_pose and primary_subject and name.casefold() == primary_subject.casefold():
            phase_pose = fallback_pose
        if phase_pose:
            item["phase_pose_signature"] = phase_pose
        result.append(item)
    return result


def _build_shot_frame_spec_cache_key(
    *,
    scene_number: int,
    shot_number: int,
    shot_description: str,
    camera_plan: str,
    scene_characters: List[str],
    location_time: str,
    location_canon_name: str,
) -> str:
    cache_key_payload = json.dumps(
        {
            "scene_number": int(scene_number),
            "shot_number": int(shot_number),
            "shot_description": str(shot_description or "").strip(),
            "camera_plan": camera_plan or "",
            "scene_characters": scene_characters or [],
            "location_time": location_time or "",
            "location_canon_name": location_canon_name or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(cache_key_payload.encode("utf-8")).hexdigest()


def _shot_frame_spec_cache_path(cache_key: str) -> str:
    return os.path.join(
        "plots",
        "storybooks",
        "_shared_internal",
        "shot_frame_spec_cache",
        f"{cache_key}.json",
    )


def _load_shot_frame_spec_from_disk_cache(
    *,
    cache_key: str,
    scene_characters: List[str],
) -> Dict[str, Any]:
    cache_path = _shot_frame_spec_cache_path(cache_key)
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        spec = _normalize_shot_frame_spec(payload.get("shot_frame_spec"), scene_characters)
        if spec:
            logger.info("♻️ shot_frame_spec: reused persisted cache %s", cache_path)
        return spec
    except Exception as e:
        logger.warning("⚠️ shot_frame_spec: failed to load disk cache %s: %s", cache_path, e)
        return {}


def _store_shot_frame_spec_to_disk_cache(
    *,
    cache_key: str,
    scene_number: int,
    shot_number: int,
    shot_frame_spec: Dict[str, Any],
) -> None:
    cache_path = _shot_frame_spec_cache_path(cache_key)
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "scene_number": int(scene_number),
                    "shot_number": int(shot_number),
                    "cache_key": cache_key,
                    "shot_frame_spec": shot_frame_spec,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.warning("⚠️ shot_frame_spec: failed to persist disk cache %s: %s", cache_path, e)


def _load_shot_frame_spec_from_existing_shots(
    *,
    project_id: str,
    scene_number: int,
    shot_number: int,
    camera_plan: str,
    cache_key: str,
    scene_characters: List[str],
) -> Dict[str, Any]:
    shots_path = os.path.join("plots", "storybooks", str(project_id), "97_shots", "shots.json")
    if not os.path.exists(shots_path):
        return {}
    try:
        with open(shots_path, "r", encoding="utf-8") as f:
            shots_data = json.load(f)
    except Exception as e:
        logger.warning("⚠️ shot_frame_spec: failed to read existing shots.json %s: %s", shots_path, e)
        return {}

    for item in shots_data.get("items") or []:
        try:
            if int(item.get("scene_number", -1)) != int(scene_number):
                continue
            if int(item.get("shot_number", -1)) != int(shot_number):
                continue
        except Exception:
            continue
        raw_spec = item.get("_shot_frame_spec")
        if not isinstance(raw_spec, dict) or not raw_spec:
            continue
        stored_key = str(item.get("_shot_frame_spec_cache_key") or "").strip()
        if stored_key and stored_key != cache_key:
            continue
        if not stored_key and str(item.get("camera_plan") or "").strip() != str(camera_plan or "").strip():
            continue
        spec = _normalize_shot_frame_spec(raw_spec, scene_characters)
        if not spec:
            continue
        logger.info(
            "♻️ shot_frame_spec: reused from existing shots.json scene=%s shot=%s%s",
            scene_number,
            shot_number,
            " (legacy match)" if not stored_key else "",
        )
        return spec
    return {}


def _normalize_shot_frame_spec(
    raw_spec: Dict[str, Any],
    scene_characters: List[str],
) -> Dict[str, Any]:
    if not isinstance(raw_spec, dict):
        logger.warning(
            "shot_frame_spec rejected: raw_spec is not a dict (got %s)",
            type(raw_spec).__name__,
        )
        return {}

    char_index = {
        str(name or "").strip().casefold(): str(name or "").strip()
        for name in (scene_characters or [])
        if str(name or "").strip()
    }

    def _normalize_mode(value: Any) -> str:
        mode = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {
            "single": "single_subject",
            "single_subject": "single_subject",
            "single_character": "single_subject",
            "single_focus": "single_subject",
            "ensemble": "ensemble",
            "group": "ensemble",
            "object": "object_focus",
            "object_focus": "object_focus",
            "prop_focus": "object_focus",
            "environment": "environment",
            "environment_focus": "environment",
            "establishing": "environment",
        }
        return aliases.get(mode, "")

    def _normalize_list(values: Any) -> List[str]:
        if not isinstance(values, list):
            return []
        result: List[str] = []
        seen = set()
        for value in values:
            item = str(value or "").strip()
            if not item:
                continue
            if item.casefold() in seen:
                continue
            seen.add(item.casefold())
            result.append(item)
        return result

    def _normalize_characters(values: Any) -> List[str]:
        normalized: List[str] = []
        seen = set()
        for value in _normalize_list(values):
            key = value.casefold()
            exact = char_index.get(key)
            if not exact:
                continue
            if exact.casefold() in seen:
                continue
            seen.add(exact.casefold())
            normalized.append(exact)
        return normalized

    def _normalize_world_physics(values: Any) -> Dict[str, Any]:
        if not isinstance(values, dict):
            values = {}
        physics = {
            "support_state": str(values.get("support_state") or "").strip(),
            "surface_state": str(values.get("surface_state") or "").strip(),
            "stability": str(values.get("stability") or "").strip(),
            "body_relation": str(values.get("body_relation") or "").strip(),
            "contact_constraints": _normalize_list(values.get("contact_constraints")),
            "occlusion_constraints": _normalize_list(values.get("occlusion_constraints")),
            "forbidden_implications": _normalize_list(values.get("forbidden_implications")),
        }
        if not any(physics.values()):
            return {}
        return physics

    def _normalize_state_spec(raw_state: Any, fallback: Dict[str, Any], *, is_start: bool = False) -> Dict[str, Any]:
        if not isinstance(raw_state, dict):
            raw_state = {}

        def _normalize_character_pose_signatures(values: Any) -> Dict[str, str]:
            if not isinstance(values, dict):
                values = {}
            normalized: Dict[str, str] = {}
            for raw_name, raw_signature in values.items():
                name = str(raw_name or "").strip()
                signature = str(raw_signature or "").strip()
                if not name or not signature:
                    continue
                canonical = char_index.get(name.casefold())
                if not canonical:
                    continue
                normalized[canonical] = signature
            return normalized

        state = {
            "scene_mode": _normalize_mode(raw_state.get("scene_mode")) or fallback.get("scene_mode", ""),
            "primary_subject": str(raw_state.get("primary_subject") or fallback.get("primary_subject") or "").strip(),
            "camera_anchor": str(raw_state.get("camera_anchor") or fallback.get("camera_anchor") or "").strip(),
            "pose_signature": str(raw_state.get("pose_signature") or fallback.get("pose_signature") or "").strip(),
            "character_pose_signatures": _normalize_character_pose_signatures(raw_state.get("character_pose_signatures")),
            "facial_expression": str(raw_state.get("facial_expression") or fallback.get("facial_expression") or "").strip(),
            "gaze_direction": str(raw_state.get("gaze_direction") or fallback.get("gaze_direction") or "").strip(),
            "visible_characters": _normalize_characters(raw_state.get("visible_characters")) or list(fallback.get("visible_characters", [])),
            "must_show": _normalize_list(raw_state.get("must_show")) or list(fallback.get("must_show", [])),
            "must_not_show": _normalize_list(raw_state.get("must_not_show")) or list(fallback.get("must_not_show", [])),
            "visible_readable_texts": _normalize_list(raw_state.get("visible_readable_texts")) or list(fallback.get("visible_readable_texts", [])),
            "hidden_readable_texts": _normalize_list(raw_state.get("hidden_readable_texts")) or list(fallback.get("hidden_readable_texts", [])),
            "world_physics": _normalize_world_physics(raw_state.get("world_physics")) or dict(fallback.get("world_physics", {})),
        }
        if is_start:
            raw_t0 = str(raw_state.get("t0_mode") or "").strip().lower()
            if raw_t0 not in {"frozen", "early_motion", "mid_action"}:
                if raw_t0:
                    logger.warning(
                        "start_state_spec.t0_mode unrecognized value %r; defaulting to 'frozen'",
                        raw_t0,
                    )
                raw_t0 = "frozen"
            state["t0_mode"] = raw_t0
        if not state["primary_subject"] or not state["camera_anchor"] or not state["must_show"]:
            missing = [
                name
                for name, value in (
                    ("primary_subject", state["primary_subject"]),
                    ("camera_anchor", state["camera_anchor"]),
                    ("must_show", state["must_show"]),
                )
                if not value
            ]
            logger.warning(
                "state_spec rejected: missing required fields %s",
                ", ".join(missing),
            )
            return {}
        return state

    def _normalize_transition_spec(raw_transition: Any) -> Dict[str, Any]:
        if not isinstance(raw_transition, dict):
            raw_transition = {}
        return {
            "camera_delta": str(raw_transition.get("camera_delta") or "").strip(),
            "subject_delta": _normalize_list(raw_transition.get("subject_delta")),
            "affect_delta": _normalize_list(raw_transition.get("affect_delta")),
            "environment_delta": _normalize_list(raw_transition.get("environment_delta")),
            "physics_delta": _normalize_list(raw_transition.get("physics_delta")),
            "tempo": str(raw_transition.get("tempo") or "").strip(),
            "must_not_introduce": _normalize_list(raw_transition.get("must_not_introduce")),
        }

    spec = {
        "scene_mode": _normalize_mode(raw_spec.get("scene_mode")),
        "primary_subject": str(raw_spec.get("primary_subject") or "").strip(),
        "camera_anchor": str(raw_spec.get("camera_anchor") or "").strip(),
        "pose_signature": str(raw_spec.get("pose_signature") or "").strip(),
        "facial_expression": str(raw_spec.get("facial_expression") or "").strip(),
        "gaze_direction": str(raw_spec.get("gaze_direction") or "").strip(),
        "visible_characters": _normalize_characters(raw_spec.get("visible_characters")),
        "must_show": _normalize_list(raw_spec.get("must_show")),
        "must_not_show": _normalize_list(raw_spec.get("must_not_show")),
        "visible_readable_texts": _normalize_list(raw_spec.get("visible_readable_texts")),
        "hidden_readable_texts": _normalize_list(raw_spec.get("hidden_readable_texts")),
        "world_physics": _normalize_world_physics(raw_spec.get("world_physics")),
    }

    if not spec["scene_mode"]:
        logger.warning("shot_frame_spec rejected: scene_mode is empty after normalization")
        return {}
    if not spec["primary_subject"] or not spec["camera_anchor"]:
        missing = [
            name
            for name, value in (
                ("primary_subject", spec["primary_subject"]),
                ("camera_anchor", spec["camera_anchor"]),
            )
            if not value
        ]
        logger.warning(
            "shot_frame_spec rejected: missing required fields %s",
            ", ".join(missing),
        )
        return {}
    if not spec["must_show"]:
        logger.warning("shot_frame_spec rejected: must_show is empty")
        return {}
    spec["start_state_spec"] = _normalize_state_spec(raw_spec.get("start_state_spec"), spec, is_start=True) or dict(spec)
    if "t0_mode" not in spec["start_state_spec"]:
        spec["start_state_spec"]["t0_mode"] = "frozen"
    spec["end_state_spec"] = _normalize_state_spec(raw_spec.get("end_state_spec"), spec) or dict(spec)
    spec["transition_spec"] = _normalize_transition_spec(raw_spec.get("transition_spec"))
    spec = _complete_shot_frame_spec_fields_via_llm(spec)
    spec = _sanitize_readable_texts_via_llm(spec)
    if not spec["facial_expression"]:
        spec["facial_expression"] = spec["end_state_spec"].get("facial_expression") or spec["start_state_spec"].get("facial_expression") or ""
    if not spec["pose_signature"]:
        spec["pose_signature"] = spec["end_state_spec"].get("pose_signature") or spec["start_state_spec"].get("pose_signature") or ""
    if not spec["gaze_direction"]:
        spec["gaze_direction"] = spec["end_state_spec"].get("gaze_direction") or spec["start_state_spec"].get("gaze_direction") or ""
    return spec


def _extract_shot_frame_spec_llm(
    *,
    project_id: str,
    scene_number: int,
    shot_number: int,
    shot_description: str,
    camera_plan: str,
    scene_action: str,
    scene_characters: List[str],
    location_time: str,
    location_canon_name: str,
    scene_continuity_facts: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract an internal shot_frame_spec that becomes the per-shot source of truth.

    The spec is not persisted to on-disk shots.json; it is used only to ground prompt generation.
    """
    description = str(shot_description or "").strip()
    if not description:
        logger.warning(
            "_extract_shot_frame_spec_llm: empty shot_description for scene=%s shot=%s; returning empty spec",
            scene_number,
            shot_number,
        )
        return {}

    cache_key = _build_shot_frame_spec_cache_key(
        scene_number=int(scene_number),
        shot_number=int(shot_number),
        shot_description=description,
        camera_plan=camera_plan or "",
        scene_characters=scene_characters or [],
        location_time=location_time or "",
        location_canon_name=location_canon_name or "",
    )
    with _shot_frame_spec_lock:
        cached = _SHOT_FRAME_SPEC_CACHE.get(cache_key)
    if isinstance(cached, dict) and cached:
        return cached
    cached = _load_shot_frame_spec_from_disk_cache(
        cache_key=cache_key,
        scene_characters=scene_characters,
    )
    if cached:
        with _shot_frame_spec_lock:
            _SHOT_FRAME_SPEC_CACHE[cache_key] = cached
        return cached
    cached = _load_shot_frame_spec_from_existing_shots(
        project_id=project_id,
        scene_number=int(scene_number),
        shot_number=int(shot_number),
        camera_plan=camera_plan or "",
        cache_key=cache_key,
        scene_characters=scene_characters,
    )
    if cached:
        with _shot_frame_spec_lock:
            _SHOT_FRAME_SPEC_CACHE[cache_key] = cached
        _store_shot_frame_spec_to_disk_cache(
            cache_key=cache_key,
            scene_number=int(scene_number),
            shot_number=int(shot_number),
            shot_frame_spec=cached,
        )
        return cached

    system_prompt = """
Ты — shot frame spec planner для storyboard-шотов.
Выбери один конкретный внутренний frame spec для ОДНОГО шота, без narrative summary и без выдумывания новых объектов.

Верни СТРОГО JSON-объект такого вида:
{
  "scene_mode": "single_subject | ensemble | object_focus | environment",
  "primary_subject": "главный субъект этого шота",
  "camera_anchor": "краткая формулировка, от чего должен якориться ракурс/композиция",
  "pose_signature": "краткое описание читаемой позы/жеста/отношения корпуса, рук и реквизита; обязательно непусто, если поза важна для узнаваемости момента",
  "facial_expression": "короткое описание видимого выражения лица/микроэмоции; обязательно непусто, если лицо субъекта читается в кадре",
  "gaze_direction": "куда направлен взгляд субъекта в этом кадре; обязательно непусто, если глаза читаются; не 'в камеру' без явного основания",
  "visible_characters": ["точные канонические имена персонажей, реально видимых в кадре"],
  "must_show": ["список обязательных одновременно видимых фактов этого шота"],
  "must_not_show": ["что нельзя показывать, потому что это относится к другим моментам/шотам"],
  "visible_readable_texts": ["только тексты, явно видимые именно в этом шоте"],
  "hidden_readable_texts": ["тексты, которые существуют в сцене/каноне, но не должны читаться в этом шоте"],
  "world_physics": {
    "support_state": "на чём/как субъект держится в этом кадре",
    "surface_state": "что происходит с поверхностью/опорой под субъектом",
    "stability": "stable | precarious | unsupported | suspended | partially_occluded",
    "body_relation": "как тело расположено относительно опоры/разрыва/объекта",
    "contact_constraints": ["какие физические контакты обязаны быть или отсутствовать"],
    "occlusion_constraints": ["какие части тела/опоры должны быть перекрыты или обрезаны"],
    "forbidden_implications": ["какие неверные физические выводы запрещены"]
  },
  "start_state_spec": {
    "t0_mode": "frozen | early_motion | mid_action — насколько T=0 неподвижен. frozen = абсолютный покой ДО начала события. early_motion = самое начало читаемой фазы движения, без полного финального результата. mid_action = середина процесса, кадр уже внутри события, но ещё не финал.",
    "primary_subject": "главный субъект START кадра",
    "camera_anchor": "якорь композиции START",
    "pose_signature": "видимая поза/жест START; как расположены корпус, руки, голова и важный реквизит",
    "character_pose_signatures": {"Имя персонажа": "если в START несколько видимых персонажей и их позы важны, краткая видимая поза/жест каждого"},
    "facial_expression": "выражение лица/микроэмоция START; обязательно непусто, если лицо видно",
    "gaze_direction": "направление взгляда START; обязательно непусто, если глаза читаются",
    "visible_characters": ["кто реально видим в START"],
    "must_show": ["что обязательно видно в START как T=0"],
    "must_not_show": ["что нельзя показывать в START, потому что это позднее состояние этого же шота"],
    "visible_readable_texts": ["только тексты, видимые в START"],
    "hidden_readable_texts": ["тексты, которые существуют, но не читаются в START"],
    "world_physics": {
      "support_state": "физическая опора START",
      "surface_state": "состояние поверхности/опоры в START",
      "stability": "устойчивость START",
      "body_relation": "положение тела относительно опоры/объекта в START",
      "contact_constraints": ["физические контакты START"],
      "occlusion_constraints": ["видимость/перекрытия START"],
      "forbidden_implications": ["какие неверные физические выводы нельзя допускать в START"]
    }
  },
  "end_state_spec": {
    "primary_subject": "главный субъект END кадра",
    "camera_anchor": "якорь композиции END",
    "pose_signature": "видимая поза/жест END; как расположены корпус, руки, голова и важный реквизит",
    "character_pose_signatures": {"Имя персонажа": "если в END несколько видимых персонажей и их позы важны, краткая видимая поза/жест каждого"},
    "facial_expression": "выражение лица/микроэмоция END как видимый результат события; обязательно непусто, если лицо видно",
    "gaze_direction": "направление взгляда END; обязательно непусто, если глаза читаются",
    "visible_characters": ["кто реально видим в END"],
    "must_show": ["что обязательно видно в END как финальный стоп-кадр"],
    "must_not_show": ["что нельзя показывать в END, потому что это раннее состояние или чужой шот"],
    "visible_readable_texts": ["только тексты, видимые в END"],
    "hidden_readable_texts": ["тексты, которые существуют, но не читаются в END"],
    "world_physics": {
      "support_state": "физическая опора END",
      "surface_state": "состояние поверхности/опоры в END",
      "stability": "устойчивость END",
      "body_relation": "положение тела относительно опоры/объекта в END",
      "contact_constraints": ["физические контакты END"],
      "occlusion_constraints": ["видимость/перекрытия END"],
      "forbidden_implications": ["какие неверные физические выводы нельзя допускать в END"]
    }
  },
  "transition_spec": {
    "camera_delta": "краткое описание перехода камеры от START к END",
    "subject_delta": ["что меняется у главного субъекта между START и END"],
    "affect_delta": ["как меняется выражение лица/взгляд/микроэмоция между START и END"],
    "environment_delta": ["что меняется в окружении между START и END"],
    "physics_delta": ["что меняется в опоре/контактах/целостности среды между START и END"],
    "tempo": "темп перехода",
    "must_not_introduce": ["что video_prompt не должен привносить сверх START/END"]
  }
}

Правила:
- Ground truth = storyboard shot_description этого шота.
- camera_plan = framing anchor, а не повод подменить содержание.
- location_time/location_canon_name = только контекст локации и не повод тащить в кадр новые объекты.
- Не тащи в spec конкретные дисплеи, экраны, подиумы, микрофоны, вывески, реквизит или персонажей, если их нет в shot_description этого шота.
- Не превращай соседние события сцены в содержимое текущего шота.
- Если шот про одного персонажа или один объект, не делай ensemble только потому, что в сцене есть ещё персонажи.
- visible_characters можно брать только из scene_characters и только точными каноническими именами.
- must_show должен перечислять только то, что можно одновременно увидеть в одном конкретном кадре.
- must_not_show используй для явных запретов на чужие события/чужие объекты/чужие зоны сцены, которые часто галлюцинируются.
- readable texts можно включать только если сам shot_description требует или явно показывает их.
- `visible_readable_texts` — только тексты, физически существующие в мире кадра как письменный элемент:
  вывеска, табличка, шильдик, дорожный знак, экранный UI, надпись на форме, наклейка, граффити, обложка, табло.
  Эмоциональная окраска самой надписи (STOP!, АВАРИЯ, DANGER) НЕ делает её устной, если это надпись на объекте.
  Примеры RIGHT: «вывеска „Кафе"», «знак „STOP"», «нашивка „SECURITY"», «табло „GATE 12"», «граффити „NO FUTURE"».
- В `visible_readable_texts` НЕЛЬЗЯ класть устные реплики/крики/звукоподражания/мысли персонажей
  (например: «Moment...», «А-а-а!», «Эй!», «Хмм», «БАМ!», «wooosh», «надо бежать»).
  Такие вещи относятся к аудио/артикуляции/внутреннему монологу и должны быть в `hidden_readable_texts` либо отсутствовать.
- Если в шоте важны опора, целостность поверхности, контакт, удержание, зависание, падение, разрыв среды, перекрытие или удар,
  ОБЯЗАТЕЛЬНО опиши это в `world_physics` как состояние мира, а не как литературный пересказ процесса.
- Если лицо/взгляд читаются в кадре, опиши `facial_expression` и `gaze_direction` как часть видимого состояния кадра, а не как абстрактный эмоциональный комментарий.
- Если для узнаваемости момента важны жест, положение корпуса, поднятая/вытянутая рука, направление реквизита, открытый рот на полуслове или другая выразительная поза, заполни `pose_signature` и удерживай её в START/END как часть видимого состояния кадра.
- Если в кадре несколько персонажей и их позы одновременно важны для читаемости шота, заполни `character_pose_signatures` внутри `start_state_spec` / `end_state_spec` по точным каноническим именам.
- Одиночный `pose_signature` оставляй как fallback для `primary_subject`, а не как замену многоперсонажной карты поз.
- Не схлопывай активную или прерванную позу в generic "стоит", "сидит", "нейтральная стойка", если в shot_description или must_show есть более характерный жест/поза.
- Не default'и к нейтральному лицу, blank stare или взгляду в камеру, если shot_description и world_physics подразумевают конкретную реакцию на событие.
- Если в shot_description есть внезапное прерывание действия, потеря опоры, удар, угроза, резкое раскрытие/обрушение среды или другой физически значимый сдвиг, а лицо читается в кадре, `facial_expression` не может оставаться пустым или нейтральным по умолчанию: зафиксируй правдоподобную микро-реакцию на этот сдвиг.
- Если shot_description формулирует лицо как "застыло", "прервалось", "в начале паузы", трактуй это как прерванное видимое состояние лица, а не как эмоционально пустую маску.
- `gaze_direction` должен следовать внутриигровой причине внимания: объекту, партнёру, источнику угрозы, разрыву поверхности, траектории падения или другому событию шота. Взгляд в камеру допустим только при явном direct address / lens look / viewer-facing framing.
- Пустые `facial_expression` и `gaze_direction` допустимы только если лицо или глаза реально не читаются в кадре.
- Выводи выражение лица и взгляд из физики мира, shot_description и camera framing: внезапная потеря опоры, напряжение, близкий контакт, реакция на объект, пауза перед действием.
- `start_state_spec.facial_expression` = самый ранний читаемый facial state этого шота; `end_state_spec.facial_expression` = итоговая видимая micro-reaction того же шота.
- `transition_spec.affect_delta` описывает только изменение facial/gaze state между START и END; без театральных exaggeration и без новых событий.
- Формулируй физику через одновременно видимые факты кадра: на чём стоит/висит субъект, цела ли поверхность, есть ли контакт/зазор, что перекрыто, что нельзя имплицировать.
- Если физика между START и END различается, `start_state_spec.world_physics`, `end_state_spec.world_physics` и `transition_spec.physics_delta` должны это явно отражать.
- `start_state_spec` = самый ранний читаемый кадр этого шота (T=0), не усредняй его с END.
- Если шот целиком построен на резком физическом сдвиге внутри кадра, `start_state_spec` не должен превращаться в отдельный безопасный establishing-пролог; это должна быть ранняя фаза того же самого события.
- В таком случае START может сохранять контакт/опору, но не должен описывать мир как полностью безопасный и никак не затронутый происходящим переходом.
- `start_state_spec.t0_mode` выбирай по тому, как именно shot_description описывает начальную фазу события:
  * `frozen` — если shot_description описывает исходную позу/композицию ДО начала действия или явно указывает на статичную сцену (стоит, смотрит, сидит, ожидание перед событием).
  * `early_motion` — если сам shot уже начинается внутри читаемой фазы движения, но без финального результата (только что начал жест, тело уже в наклоне, нога уже оторвалась от опоры, удар уже начался, но ещё не достиг цели).
  * `mid_action` — если shot целиком разворачивается внутри уже идущего события и START должен показать середину процесса, а не его начало (середина прыжка, середина падения, середина удара, тело уже в воздухе).
- Не выбирай `early_motion`/`mid_action` ради театральности: переключайся туда только когда сам shot_description действительно начинается уже внутри события. По умолчанию — `frozen`.
- При `early_motion`/`mid_action` `start_state_spec.world_physics`, `pose_signature` и `must_show` обязаны явно отражать соответствующую фазу (например — оторванная от опоры стопа, корпус уже в наклоне, рука уже на полпути), а не противоречить ей описанием полного покоя.
- `end_state_spec` = финальный стоп-кадр этого же шота (T=final).
- `transition_spec` = только дельта между START и END; без новых объектов и без пересказа всей сцены.
- Если в шоте есть любое относительное движение субъекта и/или камеры (ходьба, бег, жест, наклон, поворот, толчок камеры, панорама, наезд/отъезд, трекинг, кран и т.д.), `transition_spec.environment_delta` ОБЯЗАН описывать, как меняется **видимое окружение** между START и END: параллакс фона, сдвиг деталей среды, или в крупном плане — сдвиг боке/световых акцентов/отражений. Это та же локация и материалы, не новая комната и не смена места без оснований в shot_description.
- Никаких объяснений, только JSON.
"""

    payload = {
        "scene_number": int(scene_number),
        "shot_number": int(shot_number),
        "camera_plan": camera_plan or "",
        "shot_description": description,
        "scene_characters": scene_characters or [],
        "location_time": location_time or "",
        "location_canon_name": location_canon_name or "",
    }

    try:
        response = call_openai_api(
            prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            model=model_hard,
            max_tokens=3500,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw_obj = parse_llm_json(response)
    except Exception as e:
        logger.error(
            "❌ shot_frame_spec: ошибка извлечения для scene=%s shot=%s: %s",
            scene_number,
            shot_number,
            e,
        )
        return {}

    normalized = _normalize_shot_frame_spec(raw_obj, scene_characters)
    if not normalized:
        logger.error(
            "❌ shot_frame_spec: невалидный результат для scene=%s shot=%s: %s",
            scene_number,
            shot_number,
            raw_obj,
        )
        return {}

    with _shot_frame_spec_lock:
        _SHOT_FRAME_SPEC_CACHE[cache_key] = normalized
    _store_shot_frame_spec_to_disk_cache(
        cache_key=cache_key,
        scene_number=int(scene_number),
        shot_number=int(shot_number),
        shot_frame_spec=normalized,
    )
    return normalized


def _get_previous_shot_info(storyboard: List[Dict[str, Any]], current_shot_number: int) -> Optional[Dict[str, str]]:
    """Возвращает краткую информацию о предыдущем кадре для контекста"""
    if current_shot_number <= 1 or not storyboard:
        return None
    
    try:
        prev_shot = storyboard[current_shot_number - 2]  # -2 потому что нумерация с 1
        return {
            "shot_number": str(prev_shot.get("shot_number", current_shot_number - 1)),
            "description": prev_shot.get("description", "")[:200],  # Первые 200 символов
            "camera_plan": prev_shot.get("camera_plan", ""),
            "timing": prev_shot.get("timing", "")
        }
    except (IndexError, KeyError):
        return None


def _get_next_shot_info(storyboard: List[Dict[str, Any]], current_shot_number: int) -> Optional[Dict[str, str]]:
    """Возвращает краткую информацию о следующем кадре для планирования переходов"""
    if current_shot_number >= len(storyboard) or not storyboard:
        return None
    
    try:
        next_shot = storyboard[current_shot_number]  # current_shot_number это уже индекс следующего
        return {
            "shot_number": str(next_shot.get("shot_number", current_shot_number + 1)),
            "description": next_shot.get("description", "")[:200],  # Первые 200 символов
            "camera_plan": next_shot.get("camera_plan", ""),
            "timing": next_shot.get("timing", "")
        }
    except (IndexError, KeyError):
        return None

def _extract_scene_mood(scene: Dict[str, Any]) -> str:
    """Извлекает настроение из action, sound, dialogue"""
    mood_indicators = []
    
    # Из действия сцены
    action = scene.get("action", "").lower()
    if any(word in action for word in ["напряжен", "тревож", "страх", "паник"]):
        mood_indicators.append("напряженное")
    elif any(word in action for word in ["спокой", "мирн", "тих"]):
        mood_indicators.append("спокойное")
    elif any(word in action for word in ["торжеств", "радост", "триумф"]):
        mood_indicators.append("торжественное")
    elif any(word in action for word in ["мрачн", "темн", "зловещ"]):
        mood_indicators.append("мрачное")
    
    # Из звукового сопровождения
    sound = scene.get("sound", "").lower()
    if "тревож" in sound or "напряж" in sound:
        mood_indicators.append("тревожное")
    elif "торжеств" in sound or "величеств" in sound:
        mood_indicators.append("величественное")
    elif "тиш" in sound:
        mood_indicators.append("напряженно-тихое")
    
    return ", ".join(mood_indicators) if mood_indicators else "нейтральное"


_BLACK_SCREEN_DETECTION_CACHE: Dict[str, bool] = {}
_BLACK_SCREEN_DETECTION_LOCK = threading.Lock()


def black_screen_storyboard_shot(
    camera_plan: str,
    shot_frame_spec: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    True для шотов вида BLACK SCREEN / чёрный экран. Решение принимает LLM-классификатор
    по сути полей `camera_plan`, `camera_anchor` и `must_show` (без хардкод-словарей
    ключевых слов). Результаты кэшируются по нормализованным входам в рамках процесса.
    """
    cp = (camera_plan or "").strip()
    spec = shot_frame_spec if isinstance(shot_frame_spec, dict) else {}
    anchor = str(spec.get("camera_anchor") or "").strip()
    raw_must = spec.get("must_show") or []
    if not isinstance(raw_must, list):
        raw_must = []
    must = [str(x).strip() for x in raw_must if str(x).strip()]

    if not cp and not anchor and not must:
        return False

    cache_payload = json.dumps(
        {"camera_plan": cp, "camera_anchor": anchor, "must_show": must},
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()

    with _BLACK_SCREEN_DETECTION_LOCK:
        cached = _BLACK_SCREEN_DETECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    system_prompt = (
        "Ты классификатор storyboard-шотов. Определи, является ли шот «чёрным экраном» — "
        "то есть кадр, в котором визуально нет НИЧЕГО, кроме сплошной черноты (#000000): "
        "ни силуэтов, ни источников света, ни текста, ни объектов, ни среды, ни контраста.\n"
        "Вход: JSON с полями `camera_plan`, `camera_anchor`, `must_show` (что обязано быть видно).\n"
        "Принцип решения по СУТИ полей, не по ключевым словам:\n"
        "- true ТОЛЬКО если совокупность полей описывает полное отсутствие визуальной информации "
        "  (пустой чёрный кадр / абсолютная тьма / void / blackout) как явное намерение режиссёра;\n"
        "- false, если в must_show, camera_plan или camera_anchor есть хотя бы один конкретный "
        "  визуальный элемент: объект, персонаж, действие, окружение, ТЕКСТ В КАДРЕ, ИСТОЧНИК СВЕТА "
        "  (свеча, факел, фонарь, экран, луна, окно), СИЛУЭТ против света, контраст света и тьмы, "
        "  отражение, бликование. Тёмная или ночная сцена с любой видимой деталью — это НЕ black screen;\n"
        "- false при двусмысленности или недостатке данных.\n"
        "Верни СТРОГО JSON: {\"is_black_screen\": true|false}."
    )

    response = call_openai_api(
        prompt=cache_payload,
        system_prompt=system_prompt,
        model=model_lite,
        max_tokens=40,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    obj = parse_llm_json(response)
    if not isinstance(obj, dict) or "is_black_screen" not in obj:
        raise ValueError(
            f"black_screen_storyboard_shot: LLM вернула невалидный JSON без поля is_black_screen: {response!r}"
        )
    result = bool(obj.get("is_black_screen"))

    with _BLACK_SCREEN_DETECTION_LOCK:
        _BLACK_SCREEN_DETECTION_CACHE[cache_key] = result
    return result


def merge_style_do_not_into_negative(negative_prompt: str, style_do_not_include: Any) -> str:
    """Добавляет в negative_prompt токены из style_images.do_not_include без дубликатов."""
    parts = [p.strip() for p in (negative_prompt or "").split(",") if p.strip()]
    seen = {p.lower() for p in parts}
    extra: List[str] = []
    if isinstance(style_do_not_include, str):
        if style_do_not_include.strip():
            extra.append(style_do_not_include.strip())
    elif isinstance(style_do_not_include, list):
        extra.extend(str(x).strip() for x in style_do_not_include if str(x).strip())
    for e in extra:
        if e.lower() not in seen:
            parts.append(e)
            seen.add(e.lower())
    return ", ".join(parts)


def _validate_negative_prompt_consistency(
    negative_prompt: str, 
    prop_continuity: Dict[str, Any]
) -> str:
    """
    Валидирует и исправляет negative_prompt на основе prop_continuity.
    Устраняет противоречия между запретами и фактическим состоянием реквизита.
    """
    if not prop_continuity:
        return negative_prompt
    
    corrected_prompt = negative_prompt
    
    # Обрабатываем удаленные предметы
    removed_items = prop_continuity.get("removed", [])
    for item in removed_items:
        if "gloves" in item.lower():
            if "no gloves on hands" not in corrected_prompt:
                corrected_prompt += ", no gloves on hands"
    
    # Обрабатываем сохраненные предметы с промежуточными состояниями
    kept_items = prop_continuity.get("kept", [])
    for item in kept_items:
        if "partially removed" in item.lower() or "hanging" in item.lower() or "being taken off" in item.lower():
            # Убираем запреты на предметы в промежуточном состоянии
            if "gloves" in item.lower():
                corrected_prompt = corrected_prompt.replace("no gloves on hands", "")
                corrected_prompt = corrected_prompt.replace("no gloves", "")
    
    # Убираем лишние запятые и пробелы
    corrected_prompt = ", ".join([part.strip() for part in corrected_prompt.split(",") if part.strip()])
    
    return corrected_prompt

def _determine_narrative_position(scene_number: int, shot_number: int, total_shots_in_scene: int) -> str:
    """Определяет позицию в повествовании"""
    if shot_number == 1:
        return "начало сцены"
    elif shot_number == total_shots_in_scene:
        return "завершение сцены"
    elif shot_number <= total_shots_in_scene * 0.3:
        return "начальное развитие"
    elif shot_number >= total_shots_in_scene * 0.7:
        return "кульминация/развязка"
    else:
        return "основное развитие"

def _extract_lighting_from_location_time(location_time: str) -> str:
    """Извлекает информацию об освещении из location_time"""
    location_time_lower = location_time.lower()
    
    if "ночь" in location_time_lower or "night" in location_time_lower:
        return "ночное освещение, низкий ключ, глубокие тени"
    elif "день" in location_time_lower or "day" in location_time_lower:
        return "дневное освещение, высокий ключ, естественный свет"
    elif "рассвет" in location_time_lower or "dawn" in location_time_lower:
        return "рассветное освещение, мягкий свет, теплые тона"
    elif "закат" in location_time_lower or "sunset" in location_time_lower:
        return "закатное освещение, теплый свет, длинные тени"
    elif "сумерки" in location_time_lower or "twilight" in location_time_lower:
        return "сумеречное освещение, переходный свет, голубой час"
    else:
        return "освещение не определено из времени суток"


def _analyze_scene_pacing(storyboard: List[Dict[str, Any]]) -> str:
    """Анализирует темпоритм сцены на основе timing кадров"""
    if not storyboard:
        return "неопределенная"
    
    try:
        # Подсчитываем среднюю длительность кадров
        total_duration = 0
        valid_timings = 0
        
        for shot in storyboard:
            timing = shot.get("timing", "")
            if " - " in timing:
                try:
                    start_str, end_str = timing.split(" - ")
                    start_seconds = _time_str_to_seconds(start_str.strip())
                    end_seconds = _time_str_to_seconds(end_str.strip())
                    duration = end_seconds - start_seconds
                    if duration > 0:
                        total_duration += duration
                        valid_timings += 1
                except:
                    continue
        
        if valid_timings == 0:
            return "средняя"
        
        avg_duration = total_duration / valid_timings
        
        if avg_duration < 3:
            return "быстрая"
        elif avg_duration > 6:
            return "медленная" 
        else:
            return "средняя"
            
    except Exception:
        return "средняя"

def _create_missing_location_llm(
    location_name: str, 
    scene_number: int, 
    project_id: str, 
    existing_locations: List[Dict[str, Any]],
    location_time: str = "",
    scene_action: str = "",
    shot_description: str = "",
    english_prompt: str = ""
) -> Optional[Dict[str, Any]]:
    """Создает новую локацию через LLM и добавляет в библию проекта"""
    
    # КРИТИЧЕСКИ ВАЖНО: Блокируем весь процесс создания локации
    with _locations_file_lock:
        logger.info(f"🔒 ЗАБЛОКИРОВАН ПРОЦЕСС СОЗДАНИЯ: '{location_name}' для сцены {scene_number}")
        
        # ШАГ 1: Перечитываем файл для проверки, не создал ли другой поток эту локацию
        locations_path = f"plots/storybooks/{project_id}/20_bible/locations.json"
        current_locations = []
        
        if os.path.exists(locations_path):
            try:
                with open(locations_path, "r", encoding="utf-8") as f:
                    current_locations = json.load(f)
            except Exception as e:
                logger.error(f"❌ Ошибка чтения {locations_path}: {e}")
                return None
        
        # ШАГ 2: Проверяем, нет ли уже такой локации (защита от дублирования)
        location_key = location_name.strip().lower()
        for existing_loc in current_locations:
            existing_key = (existing_loc.get("name") or "").strip().lower()
            if existing_key == location_key:
                logger.info(f"🔍 ЛОКАЦИЯ УЖЕ СУЩЕСТВУЕТ: '{location_name}' найдена как '{existing_loc.get('name', '')}'")
                return existing_loc
        
        # ШАГ 3: Нормализация названия через LLM для избежания дублирования
        normalization_prompt = f"""Нормализуй название локации для избежания дублирования.

ИСХОДНАЯ ИНФОРМАЦИЯ:
- LLM определил локацию как: "{location_name}"
- location_time из сценария: "{location_time}"
- СУЩЕСТВУЮЩИЕ ЛОКАЦИИ: {[loc.get('name', '') for loc in current_locations]}

ЗАДАЧА: 
1. Проанализируй location_time из сценария
2. Извлеки ОСНОВНОЕ название локации (без времени/интерьера)
3. Верни каноническое название, которое не дублирует существующие

ПРИМЕРЫ:
- location_time: "ИНТ. ДЕТСКАЯ КОМНАТА - ДЕНЬ" → "детская комната"
- location_time: "ИНТ. КУХНЯ / ПРИХОЖАЯ - ВЕЧЕР" → "кухня"
- location_time: "ИНТ. КОРИДОР КВАРТИРЫ - ДЕНЬ" → "коридор"
- location_time: "ЭКС. УЛИЦЫ ГОРОДА - ДЕНЬ" → "улицы города"

ПРИОРИТЕТ: location_time > LLM определение

ВЕРНИ ТОЛЬКО НАЗВАНИЕ ЛОКАЦИИ (без кавычек, времени, ИНТ/ЭКС):"""

        try:
            normalized_resp = call_openai_api(
                prompt=normalization_prompt,
                system_prompt="Ты эксперт по обработке текста. Нормализуй название локации согласно инструкциям.",
                model=model_ultimate,
                max_tokens=50,
                temperature=0.1
            )
            
            normalized_name = normalized_resp.strip().strip('"\'')
            logger.info(f"📝 НОРМАЛИЗОВАННОЕ НАЗВАНИЕ: '{location_name}' → '{normalized_name}'")
            
        except Exception as e:
            logger.error(f"❌ Ошибка нормализации названия: {e}")
            normalized_name = location_name
        
        # ШАГ 4: Повторная проверка после нормализации
        normalized_key = normalized_name.strip().lower()
        for existing_loc in current_locations:
            existing_key = (existing_loc.get("name") or "").strip().lower()
            if existing_key == normalized_key:
                logger.info(f"🔍 НОРМАЛИЗОВАННАЯ ЛОКАЦИЯ УЖЕ СУЩЕСТВУЕТ: '{normalized_name}'")
                return existing_loc
        
        # ШАГ 5: Создаем новую локацию через LLM с полным контекстом
        system_prompt = f"""Ты - дизайнер локаций. Создай описание новой локации для анимационного проекта.

КОНТЕКСТ ИЗ СЦЕНАРИЯ:
- location_time: "{location_time}"
- Нормализованное название: "{normalized_name}"
- Сцена: {scene_number}
- Действие сцены: "{scene_action}"
- Описание кадра: "{shot_description}"

ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ (english_prompt для понимания стиля):
"{english_prompt}"

ЗАДАЧА: Создай описание локации НА ОСНОВЕ ВСЕГО КОНТЕКСТА СЦЕНАРИЯ.

КРИТИЧЕСКИ ВАЖНО:
- Анализируй english_prompt для понимания СТИЛЯ и АТМОСФЕРЫ (стерильная/уютная, футуристичная/традиционная)
- НО название бери из location_time, НЕ из english_prompt
- Создавай описание, СООТВЕТСТВУЮЩЕЕ стилю проекта

ТРЕБОВАНИЯ:
- name: используй нормализованное название "{normalized_name}"
- description: 2-3 предложения, описывающие локацию в СООТВЕТСТВИИ со стилем из english_prompt
- key_objects: список из 3-5 объектов, СООТВЕТСТВУЮЩИХ стилю и контексту
- atmosphere: настроение, СООТВЕТСТВУЮЩЕЕ english_prompt
- lighting: освещение соответствующее времени из location_time (ДЕНЬ/ВЕЧЕР/НОЧЬ)
- color_palette: 4 hex-цвета, подходящие к СТИЛЮ проекта
- reference_image_path: создай английский путь типа "/references/locations/english_name.png"

ПРИМЕРЫ reference_image_path:
- "детская комната" → "/references/locations/nursery_room.png"
- "кухня" → "/references/locations/kitchen.png"
- "коридор" → "/references/locations/corridor.png"
- "офис директора" → "/references/locations/director_office.png"

АНАЛИЗИРУЙ СТИЛЬ:
- Если english_prompt упоминает "sterile", "white", "minimalistic", "future" → создавай ФУТУРИСТИЧНУЮ локацию
- Если упоминает "warm", "cozy", "traditional" → создавай ТРАДИЦИОННУЮ локацию

ВЕРНИ JSON:
{{
  "name": "{normalized_name}",
  "description": "...",
  "key_objects": ["объект1", "объект2", "объект3"],
  "atmosphere": "...",
  "lighting": "...",
  "color_palette": ["#hex1", "#hex2", "#hex3", "#hex4"],
  "reference_image_path": "/references/locations/english_name.png"
}}"""

        try:
            resp = call_openai_api(
                prompt=system_prompt,
                system_prompt="Ты эксперт по созданию описаний локаций для кинематографа. Создай детальное описание новой локации в JSON формате.",
                model=model_ultimate,
                max_tokens=1000,
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            new_location = parse_llm_json(resp)
            
            # ШАГ 6: Атомарная запись в файл
            current_locations.append(new_location)
            
            # Создаем временный файл для атомарной записи
            temp_path = locations_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(current_locations, f, ensure_ascii=False, indent=2)
            
            # Атомарно переименовываем
            os.rename(temp_path, locations_path)
            
            logger.info(f"✅ НОВАЯ ЛОКАЦИЯ СОЗДАНА И СОХРАНЕНА: '{new_location.get('name', '')}' → {locations_path}")
            
            return new_location
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания новой локации: {e}")
            return None

def _build_extended_context(
    project_id: str,
    scene: Dict[str, Any],
    storyboard: List[Dict[str, Any]], 
    shot_number: int,
    scene_action: str,
    shot_description: str,
    camera_plan: str,
    scene_characters: List[str],
    screenplay_data: Dict[str, Any],
    characters_data: List[Dict[str, Any]],
    locations_data: List[Dict[str, Any]],
    scene_continuity_facts: Optional[Dict[str, Any]] = None,
    continuity_reference_path: Optional[str] = None,
    style_images: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Строит расширенный контекст для генерации промптов"""

    # --- Location canon (Variant A): scene-level + optional per-shot override ---
    scene_loc_canon = (scene.get("location_canon_name") or "").strip() if isinstance(scene, dict) else ""
    shot_loc_canon = ""
    try:
        for sb in (storyboard or []):
            if not isinstance(sb, dict):
                continue
            if int(sb.get("shot_number", -1)) == int(shot_number):
                shot_loc_canon = (sb.get("location_canon_name") or "").strip()
                break
    except Exception:
        shot_loc_canon = ""
    effective_loc_canon = shot_loc_canon or scene_loc_canon
    
    # One source of truth for the whole one-shot cycle: if continuity facts were already extracted
    # at scene level, reuse them for generation and downstream QA instead of recomputing later.
    scene_continuity_facts = dict(scene_continuity_facts or {})

    shot_frame_spec = _extract_shot_frame_spec_llm(
        project_id=project_id,
        scene_number=int(scene.get("scene_number", 1) or 1),
        shot_number=int(shot_number or 1),
        shot_description=shot_description or "",
        camera_plan=camera_plan or "",
        scene_action=scene_action or "",
        scene_characters=scene_characters or [],
        location_time=scene.get("location_time", "") or "",
        location_canon_name=effective_loc_canon,
        scene_continuity_facts=scene_continuity_facts,
    )

    character_visual_profiles = _build_character_visual_profiles(characters_data)
    shot_character_visual_profiles = _build_shot_character_visual_profiles(
        character_visual_profiles,
        shot_frame_spec,
    )
    start_shot_character_visual_profiles = _build_phase_shot_character_visual_profiles(
        shot_character_visual_profiles,
        shot_frame_spec,
        state_key="start_state_spec",
    )
    end_shot_character_visual_profiles = _build_phase_shot_character_visual_profiles(
        shot_character_visual_profiles,
        shot_frame_spec,
        state_key="end_state_spec",
    )

    # Базовый контекст (уже был)
    base_context = {
        "scene_action": scene_action,
        "shot_description": shot_description,
        "camera_plan": camera_plan,
        "scene_characters": scene_characters,
        # Устойчивые факты сцены (continuity) из scene.action
        # ВАЖНО: применять только если это уместно для конкретного кадра и видимо в его camera_plan.
        "scene_continuity_facts": scene_continuity_facts,
        "shot_frame_spec": shot_frame_spec,
        "shot_frame_spec_cache_key": _build_shot_frame_spec_cache_key(
            scene_number=int(scene.get("scene_number", 1) or 1),
            shot_number=int(shot_number or 1),
            shot_description=shot_description or "",
            camera_plan=camera_plan or "",
            scene_characters=scene_characters or [],
            location_time=scene.get("location_time", "") or "",
            location_canon_name=effective_loc_canon,
        ),
        "available_characters": [
            f"{char.get('name', '')} ({char.get('role', 'character')}; {', '.join(char.get('immutable_attributes', {}).get('unique_features', [])[:2])})"
            for char in characters_data if char.get("name")
        ],
        "character_visual_profiles": character_visual_profiles,
        "shot_character_visual_profiles": shot_character_visual_profiles,
        "start_shot_character_visual_profiles": start_shot_character_visual_profiles,
        "end_shot_character_visual_profiles": end_shot_character_visual_profiles,
        "available_locations": [loc.get("name", "") for loc in locations_data if loc.get("name")]
    }
    
    # Расширенный контекст сцены
    scene_context = {
        "location_time": scene.get("location_time", ""),
        # Provide effective canonical location for the current shot (override -> else scene)
        "location_canon_name": effective_loc_canon,
        # Debug-only (not required by prompts, but useful downstream)
        "scene_location_canon_name": scene_loc_canon,
        "shot_location_canon_name": shot_loc_canon,
        "scene_dialogue": scene.get("dialogue", []),
        "scene_camera_notes": scene.get("camera", ""),
        "scene_sound": scene.get("sound", ""),
        "scene_transition": scene.get("transition", "")
    }
    
    # Контекст соседних кадров
    neighbor_context = {
        "previous_shot": _get_previous_shot_info(storyboard, shot_number),
        "next_shot": _get_next_shot_info(storyboard, shot_number),
        "total_shots_in_scene": len(storyboard),
        "current_shot_position": f"{shot_number}/{len(storyboard)}"
    }
    
    # Режиссерские заметки и анализ
    def _build_visual_style_from_style_images(si: Optional[Dict[str, Any]]) -> str:
        if not isinstance(si, dict) or not si:
            return ""
        chunks: List[str] = []
        for k in ["art_style", "color_palette", "composition_rules", "lighting", "texture", "detail_density", "model"]:
            v = si.get(k)
            if v:
                chunks.append(str(v))
        out = " ".join(chunks).strip()
        dni = si.get("do_not_include")
        if dni:
            out = (out + f". Не используй {str(dni)}").strip()
        return out

    creative_context = {
        # ВАЖНО: visual_style может приходить из screenplay.json (сгенерен на основе 30_style/style_images.json).
        # Если он пуст — достраиваем из style_images напрямую, чтобы все кадры получали стиль на вход.
        "visual_style": (screenplay_data.get("visual_style", "") or _build_visual_style_from_style_images(style_images)),
        "style_images": style_images or {},
        "style_do_not_include": (style_images.get("do_not_include") if isinstance(style_images, dict) else []),
        "scene_mood": _extract_scene_mood(scene),
        "lighting_context": _extract_lighting_from_location_time(scene.get("location_time", "")),
        "scene_pacing": _analyze_scene_pacing(storyboard),
        "narrative_position": _determine_narrative_position(scene.get("scene_number", 1), shot_number, len(storyboard))
    }
    
    # Добавляем continuity reference если есть
    continuity_context = {}
    if continuity_reference_path:
        continuity_context = {
            "continuity_reference_path": continuity_reference_path,
            "has_continuity_reference": True
        }
    
    # Объединяем все контексты
    return {**base_context, **scene_context, **neighbor_context, **creative_context, **continuity_context}
