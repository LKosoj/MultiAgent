import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_command import model_hard
from custom_tools.storybook.audio_subtitle import _safe_project_dir
from utils import call_openai_api, parse_llm_json

logger = logging.getLogger(__name__)

_DURATION_THRESHOLD_SEC = 600.0
_MAX_LEITMOTIFS_SHORT = 2
_MAX_LEITMOTIFS_LONG = 3
_FALLBACK_SCENE_SEC = 60.0

_SUNO_SUFFIX = "instrumental only, no vocals"
_MIN_PROMPT_LEN = 30
_MAX_PROMPT_LEN = 500
_PROMPT_FILLER = "warm cinematic instrumental theme, gentle orchestral texture, clear melody"
_VOCAL_TOKEN_RE = re.compile(r"\b(vocals?|vocalist\w*|singing|lyrics?|voices?)\b", re.IGNORECASE)

_SCENE_ACTION_CLIP = 400
_SCENE_SOUND_CLIP = 150
_CHAR_FIELD_CLIP = 220
_STORY_CLIP = 2000

_LANGUAGE_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish", "fr": "French", "de": "German"}


def music_planner_tool(
    base_dir: Optional[str] = None,
    language: str = "ru",
    *,
    session_id: str = "",
    project_id: str = "",
) -> Dict[str, Any]:
    # base_dir wins over project_id (test hook); otherwise resolve project_id
    # via the same safe helper used by the other storybook tools.
    if base_dir is None:
        if not project_id:
            return {
                "status": "skipped",
                "plan_path": "",
                "leitmotif_count": 0,
                "scene_count": 0,
                "budget_used": 0,
                "rationale": "music_planner_tool requires project_id or base_dir",
            }
        try:
            resolved = _safe_project_dir(project_id)
        except ValueError as exc:
            logger.warning("music_planner: cannot resolve project_id %r: %s", project_id, exc)
            return {
                "status": "skipped",
                "plan_path": "",
                "leitmotif_count": 0,
                "scene_count": 0,
                "budget_used": 0,
                "rationale": f"invalid project_id: {exc}",
            }
        base_dir = str(resolved)
    base_path = Path(base_dir)
    screenplay = _read_json(base_path / "91_screenplay" / "screenplay.json")
    story = _read_json(base_path / "20_story" / "story.json")
    brief = _read_json(base_path / "00_brief.json")
    shots = _read_json(base_path / "97_shots" / "shots.json")

    scenes = _screenplay_scenes(screenplay)
    scene_ids = [_scene_key(scene, idx) for idx, scene in enumerate(scenes)]

    duration_sec = _estimate_duration_sec(shots, screenplay, scenes)
    budget = _MAX_LEITMOTIFS_SHORT if duration_sec < _DURATION_THRESHOLD_SEC else _MAX_LEITMOTIFS_LONG

    if not scene_ids:
        logger.warning("music_planner: no scenes found under %s; writing neutral-only fallback plan", base_path)
        plan = _fallback_plan(scene_ids)
        status = "fallback"
    else:
        context = _build_llm_context(screenplay, scenes, scene_ids, story, brief, budget, language)
        raw_plan = _plan_via_llm(context, budget, language)
        if raw_plan is None:
            plan = _fallback_plan(scene_ids)
            status = "fallback"
        else:
            plan, status = _validate_and_fix(raw_plan, scene_ids, budget)

    plan_path = base_path / "98_audio" / "music_plan.json"
    _write_json(plan_path, plan)

    return {
        "status": status,
        "plan_path": str(plan_path),
        "leitmotif_count": len(plan.get("leitmotifs", {})),
        "scene_count": len(scene_ids),
        "budget_used": budget,
        "rationale": plan.get("rationale", ""),
    }


def _plan_via_llm(context: Dict[str, Any], budget: int, language: str) -> Optional[Dict[str, Any]]:
    try:
        response = call_openai_api(
            prompt=json.dumps(context, ensure_ascii=False, indent=2),
            system_prompt=_system_prompt(budget, language),
            model=model_hard,
            max_tokens=4000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        logger.warning("music_planner: LLM call failed: %s", exc)
        return None
    plan = parse_llm_json(response)
    if not isinstance(plan, dict) or not plan:
        logger.warning("music_planner: LLM returned empty or invalid JSON")
        return None
    return plan


def _system_prompt(budget: int, language: str) -> str:
    source_language = _LANGUAGE_NAMES.get(language, language)
    return (
        "You are a film music supervisor. Design a per-scene musical score for a short film "
        "using leitmotifs.\n\n"
        "TASK:\n"
        f"1. Pick AT MOST {budget} objects (priority: main character, antagonist, key location) "
        f"that deserve their own recognizable leitmotif. Pick fewer than {budget} if the story "
        "does not support that many distinct musical identities.\n"
        "2. For each leitmotif, invent a short lowercase snake_case id (e.g. \"hero\", \"villain\", \"castle\").\n"
        "3. Write a Suno music-generation prompt (English) for each leitmotif AND for one shared "
        "\"neutral\" theme used by scenes with no leitmotif. Format: \"[genre], [tempo/BPM], "
        "[key instruments], [mood adjectives], instrumental only, no vocals\". A leitmotif prompt "
        "must be recognizable (specific instrument/genre signature), not a generic \"orchestra\" description.\n"
        "4. Map EVERY scene_id from the input to exactly one of the leitmotif ids or \"neutral\":\n"
        "   - scene features a leitmotif object (character/location) -> that leitmotif;\n"
        "   - scene features more than one -> the dramatically dominant one;\n"
        "   - scene features none of them -> \"neutral\".\n\n"
        "Return ONLY strict JSON, no markdown, matching exactly this schema:\n"
        "{\n"
        '  "leitmotifs": {"<id>": {"suno_prompt": "string", "description": "string", '
        '"target": "character|location|mood"}},\n'
        '  "neutral": {"suno_prompt": "string", "description": "string"},\n'
        '  "scene_mapping": {"<scene_id>": "<leitmotif_id or neutral>"},\n'
        '  "rationale": "short string explaining the choices"\n'
        "}\n\n"
        "Every suno_prompt must end with \"instrumental only, no vocals\" and must never mention "
        "singing/vocals/lyrics/voice.\n"
        f"Scene/character/story context below is in {source_language} (project source language) - "
        "keep names as given, but write every suno_prompt, description and rationale in English."
    )


def _build_llm_context(
    screenplay: Any,
    scenes: List[Dict[str, Any]],
    scene_ids: List[str],
    story: Any,
    brief: Any,
    budget: int,
    language: str,
) -> Dict[str, Any]:
    concept = screenplay.get("concept") if isinstance(screenplay, dict) else None
    characters = screenplay.get("characters") if isinstance(screenplay, dict) else None

    scene_payload = []
    for scene_id, scene in zip(scene_ids, scenes):
        scene_payload.append({
            "scene_id": scene_id,
            "location": _clip(scene.get("location_time"), 150),
            "characters": scene.get("characters") if isinstance(scene.get("characters"), list) else [],
            "action": _clip(scene.get("action"), _SCENE_ACTION_CLIP),
            "sound": _clip(scene.get("sound"), _SCENE_SOUND_CLIP),
        })

    character_payload = []
    if isinstance(characters, list):
        for character in characters:
            if not isinstance(character, dict):
                continue
            character_payload.append({
                "name": character.get("name", ""),
                "appearance": _clip(character.get("appearance"), _CHAR_FIELD_CLIP),
                "character": _clip(character.get("character"), _CHAR_FIELD_CLIP),
            })

    brief_payload = {}
    if isinstance(brief, dict):
        for key in ("title", "genre", "target_age", "moral"):
            if brief.get(key):
                brief_payload[key] = brief[key]

    story_excerpt = ""
    if isinstance(story, dict) and isinstance(story.get("pages"), list):
        bodies = [str(page.get("body") or "") for page in story["pages"] if isinstance(page, dict)]
        story_excerpt = _clip(" ".join(bodies), _STORY_CLIP)

    return {
        "language": language,
        "leitmotif_budget": budget,
        "concept": concept if isinstance(concept, dict) else {},
        "brief": brief_payload,
        "characters": character_payload,
        "story_excerpt": story_excerpt,
        "scenes": scene_payload,
    }


def _validate_and_fix(plan: Dict[str, Any], scene_ids: List[str], budget: int) -> Tuple[Dict[str, Any], str]:
    status = "ok"

    raw_leitmotifs = plan.get("leitmotifs")
    leitmotifs = {
        str(lm_id).strip(): lm
        for lm_id, lm in (raw_leitmotifs.items() if isinstance(raw_leitmotifs, dict) else [])
        if str(lm_id).strip() and str(lm_id).strip() != "neutral" and isinstance(lm, dict)
    }

    if len(leitmotifs) > budget:
        logger.warning(
            "music_planner: LLM returned %d leitmotifs, exceeding budget %d; truncating",
            len(leitmotifs), budget,
        )
        leitmotifs = dict(list(leitmotifs.items())[:budget])
        status = "degraded"
    if not leitmotifs:
        logger.warning("music_planner: LLM returned no usable leitmotifs; all scenes fall back to neutral")
        status = "degraded"

    fixed_leitmotifs: Dict[str, Any] = {}
    for lm_id, leitmotif in leitmotifs.items():
        fixed_leitmotifs[lm_id] = {
            "suno_prompt": _sanitize_suno_prompt(leitmotif.get("suno_prompt")),
            "description": str(leitmotif.get("description") or "").strip(),
            "target": str(leitmotif.get("target") or "").strip(),
        }

    raw_neutral = plan.get("neutral")
    raw_neutral = raw_neutral if isinstance(raw_neutral, dict) else {}
    neutral = {
        "suno_prompt": _sanitize_suno_prompt(raw_neutral.get("suno_prompt")),
        "description": str(raw_neutral.get("description") or "").strip(),
    }

    valid_ids = set(fixed_leitmotifs) | {"neutral"}
    raw_mapping = plan.get("scene_mapping")
    raw_mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
    scene_mapping: Dict[str, str] = {}
    for scene_id in scene_ids:
        target = str(raw_mapping.get(scene_id) or "").strip()
        if target not in valid_ids:
            logger.warning(
                "music_planner: scene %s has no valid leitmotif mapping (got %r); defaulting to neutral",
                scene_id, target or None,
            )
            target = "neutral"
            status = "degraded"
        scene_mapping[scene_id] = target

    fixed_plan = {
        "leitmotifs": fixed_leitmotifs,
        "neutral": neutral,
        "scene_mapping": scene_mapping,
        "rationale": str(plan.get("rationale") or "").strip(),
    }
    return fixed_plan, status


def _sanitize_suno_prompt(raw: Any) -> str:
    text = str(raw or "").strip()
    # The mandatory suffix contains the word "vocals" - drop a pre-existing copy of it
    # before stripping vocal-related tokens, or the strip below would mangle it.
    if text.lower().endswith(_SUNO_SUFFIX):
        text = text[: len(text) - len(_SUNO_SUFFIX)].rstrip(" ,.-")
    text = _VOCAL_TOKEN_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,;])", r"\1", text)  # drop space left before punctuation by a stripped token
    text = re.sub(r"\s*[,;]\s*[,;]\s*", ", ", text)  # collapse repeated commas/semicolons
    text = re.sub(r"\s*,\s*-\s*", " - ", text)  # drop a comma orphaned next to a dash
    text = re.sub(r"\s*-\s*,\s*", " - ", text)
    text = re.sub(r"-{2,}", "-", text)  # collapse repeated dashes
    text = re.sub(r"\s{2,}", " ", text).strip(" ,.;-")
    if not text:
        text = _PROMPT_FILLER

    result = f"{text}, {_SUNO_SUFFIX}"
    if len(result) > _MAX_PROMPT_LEN:
        overflow = len(result) - _MAX_PROMPT_LEN
        text = text[: max(0, len(text) - overflow)].rstrip(" ,.-")
        result = f"{text}, {_SUNO_SUFFIX}"
    if len(result) < _MIN_PROMPT_LEN:
        result = f"{text} {_PROMPT_FILLER}, {_SUNO_SUFFIX}"
    return result


def _fallback_plan(scene_ids: List[str]) -> Dict[str, Any]:
    return {
        "leitmotifs": {},
        "neutral": {
            "suno_prompt": _sanitize_suno_prompt(None),
            "description": "Fallback neutral theme",
        },
        "scene_mapping": {scene_id: "neutral" for scene_id in scene_ids},
        "rationale": "LLM planning failed; fallback plan with a single neutral theme for all scenes.",
    }


def _screenplay_scenes(screenplay: Any) -> List[Dict[str, Any]]:
    if isinstance(screenplay, dict) and isinstance(screenplay.get("screenplay"), list):
        return [scene for scene in screenplay["screenplay"] if isinstance(scene, dict)]
    if isinstance(screenplay, dict) and isinstance(screenplay.get("scenes"), list):
        return [scene for scene in screenplay["scenes"] if isinstance(scene, dict)]
    if isinstance(screenplay, list):
        return [scene for scene in screenplay if isinstance(scene, dict)]
    return []


def _scene_key(scene: Dict[str, Any], index: int) -> str:
    for key in ("scene_number", "scene_id", "id", "number"):
        value = scene.get(key)
        if value not in (None, ""):
            return str(value)
    return str(index + 1)


def _shot_items(shots: Any) -> List[Dict[str, Any]]:
    if isinstance(shots, dict) and isinstance(shots.get("items"), list):
        return [item for item in shots["items"] if isinstance(item, dict)]
    if isinstance(shots, list):
        return [item for item in shots if isinstance(item, dict)]
    return []


def _estimate_duration_sec(shots: Any, screenplay: Any, scenes: List[Dict[str, Any]]) -> float:
    # Sources tried in order of preference; the first one with a positive total wins.
    total = sum(_to_float(item.get("duration_sec")) for item in _shot_items(shots))
    if total > 0:
        return total
    total = sum(_to_float(scene.get("estimated_duration_sec")) for scene in scenes)
    if total > 0:
        return total
    if isinstance(screenplay, dict):
        top_level = _to_float(screenplay.get("estimated_duration_sec"))
        if top_level > 0:
            return top_level
    return max(len(scenes), 1) * _FALLBACK_SCENE_SEC


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
