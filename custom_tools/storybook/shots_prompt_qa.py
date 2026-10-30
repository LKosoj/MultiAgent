import json
import copy
import base64
import io
import hashlib
import logging
import os
import re
import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from agent_command import model_hard, model_mapping
from utils import call_openai_api, extract_json_from_markdown
from custom_tools.storybook.screenplay_shots_generator_utils.shared_utils import (
    _extract_shot_frame_spec_llm,
    _previous_shot_actions,
    _build_shot_frame_spec_cache_key,
    _SHOT_FRAME_SPEC_VERSION,
    _normalize_shot_frame_spec,
    _store_shot_frame_spec_to_disk_cache,
    _SHOT_FRAME_SPEC_CACHE,
    _shot_frame_spec_lock,
)
from custom_tools.storybook.screenplay_shots_generator_utils.technical import (
    _build_reference_roles_instruction,
)
from custom_tools.storybook.project_paths import safe_storybook_project_dir
from custom_tools.storybook.domain_glossary_config import (
    resolve_active_domain_profile,
    compose_anchor_glossary_note,
    compose_qa_domain_prompt,
)

logger = logging.getLogger(__name__)


def _scene_render_review_image(items: List[Dict[str, Any]]) -> Optional[str]:
    from PIL import Image, ImageDraw, ImageFont

    rendered = sorted(
        [item for item in items if os.path.isfile(str(item.get("output_path") or ""))],
        key=lambda item: (
            _safe_int(item.get("scene_number")), _safe_int(item.get("shot_number")),
            0 if str(item.get("shot_type") or "").lower() == "start" else 1,
        ),
    )
    if not rendered:
        return None
    sheet = Image.new("RGB", (1536, ((len(rendered) + 1) // 2) * 468), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for index, item in enumerate(rendered):
        x, y = (index % 2) * 768, (index // 2) * 468
        label = f"{item.get('scene_number')}.{item.get('shot_number')} {item.get('shot_type')}"
        draw.text((x + 8, y + 4), label, fill="black", font=font)
        with Image.open(item["output_path"]) as image:
            image = image.convert("RGB")
            image.thumbnail((760, 432))
            sheet.paste(image, (x + 4, y + 32))
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

def _safe_preview(text: Any, limit: int = 400) -> str:
    try:
        s = str(text) if text is not None else ""
    except Exception:
        return ""
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        return s[:limit] + "...(truncated)"
    return s


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# Fields this tool ever mutates on a shot item (mirrors `allowed_fields` in
# _apply_repairs_to_items and the fields touched by _apply_global_repairs_to_items).
# Used when merging the in-memory item back into the freshly re-read on-disk item:
# only these are overlaid, so any field written concurrently by another writer
# (blockout paths, duration_s/duration_source/duration_requested_s, etc.) between
# this tool's initial read and its write survives instead of being reverted to
# the stale in-memory snapshot.
_QA_REPAIR_ITEM_FIELDS = frozenset({
    "english_prompt", "video_prompt", "negative_prompt", "reference_image_paths",
    "characters", "locations", "_shot_frame_spec", "camera_position",
    "character_orientation", "final_camera_position", "final_character_orientation",
})

_QA_WRITABLE_ITEM_FIELDS = (
    "english_prompt",
    "video_prompt",
    "negative_prompt",
    "reference_image_paths",
    "characters",
    "locations",
    "camera_position",
    "character_orientation",
    "final_camera_position",
    "final_character_orientation",
    "style_id",
    "_shot_frame_spec",
    "_shot_frame_spec_cache_key",
    "copy_from_previous_end",
    "link_type",
    "should_link_as_next_start",
    "should_use_prev_end_as_reference",
)


def _safe_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_cam_plan(s: str) -> str:
    return (s or "").strip().lower()


def _requires_closeup_tokens(camera_plan: str) -> Tuple[bool, bool]:
    """
    Returns (needs_closeup, needs_extreme_closeup)
    """
    cp_l = _normalize_cam_plan(camera_plan)
    needs_extreme = ("extreme close" in cp_l) or ("крупнейш" in cp_l)
    needs_close = needs_extreme or ("close up" in cp_l) or ("close-up" in cp_l) or ("крупн" in cp_l)
    return needs_close, needs_extreme


_LLM_CACHE_MAXSIZE = 512

# Кэши пишутся из воркеров ThreadPoolExecutor (_process_scene_qa), поэтому
# неатомарная эвикция (len-check -> pop -> set) требует общей блокировки,
# иначе возможна двойная эвикция / StopIteration в гонке.
_CACHE_LOCK = threading.Lock()

def _cache_set(cache: dict, key: Any, value: Any, maxsize: int = _LLM_CACHE_MAXSIZE) -> None:
    """Insert into cache, evicting the oldest entry when maxsize is exceeded."""
    with _CACHE_LOCK:
        if len(cache) >= maxsize:
            try:
                cache.pop(next(iter(cache)))
            except StopIteration:
                pass
        cache[key] = value


_VIDEO_JUDGE_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_CLOSEUP_ROOM_CHECK_CACHE: Dict[Tuple[str, str, str], List[str]] = {}


def _llm_check_closeup_room_details(video_prompt: str, camera_plan: str, storyboard_description: str = "") -> List[str]:
    """
    LLM-классификатор: содержит ли video_prompt видимые room/venue/environment-детали,
    несовместимые с close-up framing (где фон должен быть размытием, а не описанием).
    Возвращает список конкретных фраз-нарушителей (или пустой список).

    Заменяет прежний хардкод room_markers (auditorium/seats/chairs/...), который по
    refactoring.md нельзя было держать как closed-world список.
    """
    vp = (video_prompt or "").strip()
    cp = (camera_plan or "").strip()
    if not vp:
        return []
    cache_key = (vp, cp, storyboard_description)
    cached = _CLOSEUP_ROOM_CHECK_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    sys = (
        "You are a strict close-up framing validator for video prompts.\n"
        "Source of truth: storyboard_description defines the subject and required visible details; camera_plan tells the framing tightness.\n"
        "Task: find visible room/venue/environment SETTING-DETAIL phrases that contradict close-up framing.\n"
        "The specified subject, its parts/support and details explicitly required by storyboard_description are allowed. "
        "Do not classify them as forbidden setting merely because they are furniture, architecture or ground. "
        "For unrelated setting only, close-up framing means background is acceptable as blur-hints "
        "(soft light, blurred shapes, bokeh, atmospheric haze). Naming furniture/architecture/audience/"
        "stage elements as visible parts of the frame breaks close-up framing.\n\n"
        "Allowed: the source subject and required visible details, blur-hints, atmospheric cues, light/shadow, color tones, generic background softness.\n"
        "Disallowed when unrelated to the source subject or required details (examples, NOT a closed list): named seats/chairs/podium/stage/screen/curtains/"
        "chandeliers/auditorium/hall/venue/architecture/audience rows when described as visible elements.\n\n"
        "Return STRICT JSON: {\"violations\": [\"<offending phrase 1>\", ...]}.\n"
        "If no violations, return {\"violations\": []}.\n"
        "Each violation MUST be a verbatim substring copied from video_prompt (lowercased exact slice)."
    )
    user = json.dumps({"camera_plan": cp, "storyboard_description": storyboard_description, "video_prompt": vp}, ensure_ascii=False)
    try:
        model_obj = model_mapping.get("model_lite") or model_hard
        resp = call_openai_api(
            prompt=user,
            system_prompt=sys,
            model=model_obj,
            max_tokens=400,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        cleaned = extract_json_from_markdown(resp)
        parsed = json.loads(cleaned) if cleaned else {}
        raw = parsed.get("violations") if isinstance(parsed, dict) else None
        result: List[str] = []
        if isinstance(raw, list):
            seen = set()
            vp_l = vp.lower()
            for item in raw:
                phrase = str(item or "").strip().lower()
                if not phrase or phrase in seen:
                    continue
                # Принимаем только то, что реально присутствует в исходном промпте,
                # чтобы LLM не выдумывал «нарушения», которых нет.
                if phrase not in vp_l:
                    continue
                seen.add(phrase)
                result.append(phrase)
        _cache_set(_CLOSEUP_ROOM_CHECK_CACHE, cache_key, list(result))
        return result
    except Exception as e:
        logger.warning("close-up room-details LLM check failed: %s; skipping check", e)
        # Без хардкод-fallback: если LLM недоступен, эту проверку пропускаем.
        # Основная валидация video_prompt всё равно делается _llm_judge_video_prompt_lite.
        return []


def _detect_closeup_video_prompt_violations(video_prompt: str, camera_plan: str, storyboard_description: str = "") -> List[str]:
    """
    FORMAL violations for START video_prompt in CLOSE UP / EXTREME CLOSE UP plans.
    Token-presence checks остаются детерминистичными (это про токены формата);
    проверка room/venue-деталей делегируется LLM (см. _llm_check_closeup_room_details),
    чтобы не держать closed-world список маркеров.
    """
    vp = (video_prompt or "")
    vp_l = vp.lower()
    needs_close, needs_extreme = _requires_closeup_tokens(camera_plan or "")
    if not needs_close:
        return []

    issues: List[str] = []
    if "close-up" not in vp_l and "close up" not in vp_l:
        issues.append('missing_required_token:"close-up"')
    if needs_extreme and not any(t in vp_l for t in ["extreme close-up", "extreme close up"]):
        issues.append('missing_required_token:"extreme close-up"')

    room_violations = _llm_check_closeup_room_details(vp, camera_plan or "", storyboard_description)
    if room_violations:
        issues.append("contains_room_details_markers:" + ", ".join(room_violations[:8]))
    return issues


def _has_any_closeup_room_markers(video_prompt: str, camera_plan: str = "", storyboard_description: str = "") -> bool:
    return bool(_llm_check_closeup_room_details(video_prompt, camera_plan, storyboard_description))


def _llm_judge_video_prompt_lite(
    *,
    before_video_prompt: str,
    candidate_video_prompt: Optional[str],
    storyboard_description: str,
    storyboard_camera_plan: str,
    model_override: Optional[Any] = None,
    frame_spec: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Universal judge/rewriter for START video_prompt vs storyboard.
    Returns:
      {
        "contradiction_before": bool,
        "candidate_ok": bool,
        "rewritten_candidate": str | null,
        "reason": str
      }
    """
    key = (
        str(before_video_prompt or "").strip(),
        str(candidate_video_prompt or "").strip(),
        str(storyboard_camera_plan or "").strip() + "||" + str(storyboard_description or "").strip()
        + "||" + json.dumps(frame_spec or {}, ensure_ascii=False, sort_keys=True),
    )
    # .get() — одна атомарная операция под GIL: не упадёт с KeyError, даже если
    # конкурентный _cache_set вытеснит ключ между проверкой и чтением (теперь, когда
    # эвикция реальна). Двухшаговый `if key in cache: cache[key]` тут гонкоопасен.
    cached = _VIDEO_JUDGE_CACHE.get(key)
    if cached is not None:
        return cached

    model_obj = model_override or model_mapping.get("model_lite") or model_hard
    needs_close, needs_extreme = _requires_closeup_tokens(storyboard_camera_plan)

    # --- Layer A: Role + validation scope + source of truth ---
    sys = (
        "You are a strict consistency judge for video prompts.\n"
        "Source of truth: storyboard_description + camera_plan.\n"
        "shot_frame_spec supplies the achieved START/END states and physical geometry. Preserve its compatible phase visibility and opaque-barrier occlusion; a costume list is not a requirement to reveal hidden body parts.\n"
        "Task: decide if BEFORE contradicts storyboard; evaluate CANDIDATE compliance; rewrite if non-compliant.\n"
        "Output: STRICT JSON object only. video_prompt = single-line English instructions; preserve source-visible text verbatim in its original script.\n\n"
        # --- Layer B: Step-by-step validation algorithm ---
        "## Evaluation algorithm\n"
        "1. Parse INPUT.mode:\n"
        "   - \"before_only\": evaluate BEFORE vs storyboard. Set contradiction_before. If true, output rewritten_candidate.\n"
        "   - \"candidate_eval\": evaluate both BEFORE and CANDIDATE. If candidate_ok=false, output rewritten_candidate.\n"
        "2. Extract 3-6 MUST_INCLUDE_FACTS from storyboard_description (identity, POV/anchor, key action, key objects, emotion/tone).\n"
        "3. BEFORE contradicts if it misses any must_include_fact OR contains incompatible facts (wrong place/anchor/roles/objects/action/timing, invented props/symbols/displays).\n"
        "Required end results must be explicitly achieved by END, not merely approached or intended.\n"
        "4. Candidate_ok=true only if all must_include_facts satisfied, no incompatible additions.\n"
        "5. Rewrite structure: camera clause + framing token + primary subject + key action/timing + (optional) one blur-hint + tempo.\n\n"
        # --- Layer C: Violation types table + constraints ---
        "## Violation locks\n"
        "| Lock | Rule |\n"
        "|------|------|\n"
        "| PRIMARY SUBJECT | Must match storyboard_description focus. No subject switching. |\n"
        "| ENTITY IDENTITY | storyboard_description = sole ground truth for WHO/WHAT. Correct wrong identity. No new entities. Transliterate character names in English prose, but preserve source-visible text verbatim; do not transliterate writing shown in the frame. No generic species labels unless storyboard uses them. No inventing species. |\n"
        "| DIRECTION | Preserve EXACT movement direction from storyboard. Downward cues -> downward verbs only. Upward cues -> upward verbs only. Never reverse. |\n"
        "| TIMING/MOMENT | Preserve EXACT timing. Contact cues (touches/makes contact) -> contact moment only. Pre-contact cues (above/hovering) -> pre-contact state only. No phase shifting. |\n"
        "| KEY ACTION | Reflect key action and timing from storyboard. No drift, no time shift. |\n"
        "| POV/ANCHOR PHRASE | Keep explicit POV/anchor phrases. Translate to English. No anchor substitution (do not swap one anchor location for another). |\n"
        "| CLOSE-UP FRAMING | Tight framing on specified subject. Preserve the subject, its parts/support and details explicitly required by storyboard_description; only unrelated setting must become blur-hints. |\n"
        "| REACTION/GAZE | No forced \"looking at camera\" for reactions unless storyboard says so. Prefer off-screen/sideways gaze. |\n\n"
        "## Additional rules\n"
        "- No placeholders (\"SUBJECT FACE\"/\"SUBJECT CLAW\"); use actual subject phrase from storyboard.\n"
        "- No absence-assertions (\"no X\"/\"without X\") unless storyboard explicitly requires absence.\n"
        "- Describe transition/micro-dynamics, not static restatement.\n"
        "- Preserve natural occlusion by opaque barriers: costume descriptions do not require hidden body parts to become visible through an opening. For quiet observation, retain subtle breathing and source-consistent gaze rather than replacing living reaction with a wardrobe reveal.\n"
        "- No invented story events. No raw newlines in strings.\n"
    )
    # M-25: \u0434\u043e\u043c\u0435\u043d\u043d\u044b\u0439 RU->EN glossary/anchor-pattern \u0432\u044b\u043d\u0435\u0441\u0435\u043d \u0432 domain_glossary_config.
    # \u0414\u0435\u0444\u043e\u043b\u0442\u043d\u044b\u0439 \u043f\u0440\u043e\u0444\u0438\u043b\u044c \u043f\u0443\u0441\u0442 => note == "" (\u043f\u0440\u043e\u043c\u043f\u0442 \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0443\u043d\u0438\u0432\u0435\u0440\u0441\u0430\u043b\u044c\u043d\u044b\u043c).
    sys += compose_anchor_glossary_note()
    if needs_close:
        sys += "- For CLOSE UP and EXTREME CLOSE UP: candidate MUST contain the words \"close-up\" or \"close up\".\n"
    if needs_extreme:
        sys += "- For EXTREME CLOSE UP: candidate MUST contain the words \"extreme close-up\" or \"extreme close up\".\n"
    if "split screen" in _normalize_cam_plan(storyboard_camera_plan) or "раздел" in _normalize_cam_plan(storyboard_camera_plan):
        sys += "- If camera_plan includes SPLIT SCREEN: candidate MUST contain \"split-screen\" or \"split screen\".\n"

    payload = {
        "mode": "before_only" if candidate_video_prompt is None else "candidate_eval",
        "storyboard_camera_plan": storyboard_camera_plan or "",
        "storyboard_description": storyboard_description or "",
        "before_video_prompt": before_video_prompt or "",
        "candidate_video_prompt": candidate_video_prompt or "",
        "shot_frame_spec": frame_spec or {},
    }
    resp = call_openai_api(
        prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
        system_prompt=sys,
        model=model_obj,
        max_tokens=6000,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    obj = _extract_json_object(resp) or {}
    out = {
        "contradiction_before": bool(obj.get("contradiction_before")),
        "candidate_ok": bool(obj.get("candidate_ok")),
        "rewritten_candidate": obj.get("rewritten_candidate") if isinstance(obj.get("rewritten_candidate"), str) else None,
        "reason": str(obj.get("reason") or ""),
    }
    if out["rewritten_candidate"] is not None:
        out["rewritten_candidate"] = out["rewritten_candidate"].replace("\n", " ").strip()
    _cache_set(_VIDEO_JUDGE_CACHE, key, out)
    return out


_ENGLISH_SET_JUDGE_CACHE: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}

_END_ENGLISH_JUDGE_CACHE: Dict[Tuple[str, str, str, str, str, str, str, str], Dict[str, Any]] = {}


def _llm_judge_end_english_prompt_candidate(
    *,
    before_english_prompt: str,
    candidate_english_prompt: str,
    storyboard_description: str,
    storyboard_camera_plan: str,
    transition_video_prompt: str,
    model_override: Any = None,
    rendered_image_url: Optional[str] = None,
    start_english_prompt: str = "",
    phase_state_spec: Optional[Dict[str, Any]] = None,
    scene_continuity_facts: Optional[Dict[str, Any]] = None,
    repair_reasons: Optional[List[str]] = None,
    image_style: str = "",
    before_negative_prompt: str = "",
    candidate_negative_prompt: str = "",
) -> Dict[str, Any]:
    """
    Safety+repair judge for END english_prompt edits.
    Prevents: semantic subject substitution, object invention, incorrect shot-size forcing.
    Allows: END framing changes ONLY when transition_video_prompt implies camera movement.

    Returns:
      {"candidate_ok": bool, "rewritten_candidate": str|None, "reason": str}
    """
    before_frame_core = _split_saved_image_style(before_english_prompt or "")[0]
    candidate_frame_core = _split_saved_image_style(candidate_english_prompt or "")[0]
    start_frame_core = _split_saved_image_style(start_english_prompt or "")[0]
    repair_context = {
        "phase_state_spec": phase_state_spec or {},
        "scene_continuity_facts": scene_continuity_facts or {},
        "repair_reasons": repair_reasons or [],
        "image_style": image_style or "",
        "before_negative_prompt": before_negative_prompt or "",
        "candidate_negative_prompt": candidate_negative_prompt or "",
    }
    rendered_image_digest = hashlib.sha256(
        str(rendered_image_url or "").encode("utf-8")
    ).hexdigest() if rendered_image_url else ""
    key = (
        before_frame_core.strip(),
        candidate_frame_core.strip(),
        (storyboard_camera_plan or "").strip(),
        (storyboard_description or "").strip(),
        (transition_video_prompt or "").strip(),
        rendered_image_digest,
        start_frame_core.strip(),
        json.dumps(repair_context, ensure_ascii=False, sort_keys=True),
    )
    # .get() атомарен под GIL — безопасно при конкурентной эвикции (см. _VIDEO_JUDGE_CACHE).
    cached = _END_ENGLISH_JUDGE_CACHE.get(key)
    if cached is not None:
        return cached

    model_obj = model_override or model_hard
    # --- Layer A: Role + validation scope ---
    sys = (
        "You are a strict validator+rewriter for END english_prompt.\n"
        "Source of truth: storyboard_description (primary subject, objects), camera_plan (base framing), transition_video_prompt (camera movement).\n"
        "phase_state_spec and scene_continuity_facts specify the current phase and retained states when compatible with that source. repair_reasons explain the correction being checked. Do not restore rejected BEFORE facts while rewriting a correction.\n"
        "Keep camera/actor sides and natural occlusion by opaque barriers. Costume inventory does not require all body parts to be visible through an opening; reject derived visibility requirements that contradict that geometry rather than restoring them in a corrected prompt.\n"
        "image_style describes the saved rendering medium; its grain or brushwork is not itself an in-scene object.\n"
        "Output: STRICT JSON only.\n\n"
        # --- Layer B: Validation steps ---
        "## Validation algorithm\n"
        "1. Identify the END-phase subject from the full storyboard sequence. Preserve an explicitly specified focus shift or exit of the initial subject; reject arbitrary subject substitution.\n"
        "2. Check NO-INVENTION: do not invent narrative subjects, props or symbols unsupported by the storyboard. Background landmarks already visible in attached renders or the before core are existing context, even when the concise storyboard does not enumerate them. Their new screen positions during camera travel are not new objects; any revealed continuation must fit that same environment and camera movement.\n"
        "3. Check framing: camera_plan is an ANCHOR (base), not absolute lock for END.\n"
        "   - END framing follows the planned camera movement (dolly/push-in/pull-back/zoom) when one is specified; without movement it must not contradict camera_plan.\n"
        "4. Keep the current-frame content in the language of storyboard_description, preserving technical terms and names. The caller appends the saved image-style and reference-roles blocks after this judge; do not require or rewrite those blocks here.\n"
        "5. Attached START/END renders establish existing objects, not the desired END truth: the candidate may correct their current projection according to the source.\n"
        "6. The current START prompt establishes the optics baseline. Keep its lens unless the source explicitly requires optical zoom or a focal-length change; camera movement changes framing, not the lens. Measured sizes in a bad render are observations, not targets: derive target sizes from the planned move using one coherent ratio, without competing ratios.\n"
        "7. Accept a compliant candidate unchanged. If it fails, rewrite only its contradictions while preserving the other corrections and phase constraints.\n\n"
        "Check the candidate image prompt and candidate_negative_prompt together against the END phase. Exclusions must not forbid its required state or retain a START-only prohibition. Preserve unrelated valid exclusions. Return rewritten_negative_prompt when an exclusion needs correction; an empty string may remove all invalid exclusions.\n\n"
        "8. Describe the complete target frame, starting with its required visible state (support, limb/prop arrangement or completed coverage). Do not address the image model as the auditor or refer to a failed render with 'fix/correct': its attached source image may be a valid START or a canonical reference, not that failed render. Keep canonical names and current wardrobe, camera, lighting and technical details; omit repeated reference appearance.\n\n"
        # --- Layer C: Output format ---
        "Return JSON: {\"candidate_ok\": true|false, \"rewritten_candidate\": \"...\"|null, \"rewritten_negative_prompt\": \"...\"|null, \"reason\": \"...\"}\n"
    )
    payload = {
        "storyboard_camera_plan": storyboard_camera_plan or "",
        "storyboard_description": storyboard_description or "",
        "transition_video_prompt": transition_video_prompt or "",
        "before_english_prompt": before_frame_core,
        "candidate_english_prompt": candidate_frame_core,
        "start_english_prompt": start_frame_core,
        **repair_context,
    }
    resp = call_openai_api(
        prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
        system_prompt=sys,
        model=model_obj,
        max_tokens=2500,
        temperature=0.0,
        response_format={"type": "json_object"},
        **({"image_url": rendered_image_url} if rendered_image_url else {}),
    )
    obj = _extract_json_object(resp) or {}
    out = {
        "candidate_ok": bool(obj.get("candidate_ok")),
        "rewritten_candidate": obj.get("rewritten_candidate") if isinstance(obj.get("rewritten_candidate"), str) else None,
        "rewritten_negative_prompt": obj.get("rewritten_negative_prompt") if isinstance(obj.get("rewritten_negative_prompt"), str) else None,
        "reason": str(obj.get("reason") or ""),
    }
    if out["rewritten_candidate"] is not None:
        out["rewritten_candidate"] = out["rewritten_candidate"].replace("\n", " ").strip()
    _cache_set(_END_ENGLISH_JUDGE_CACHE, key, out)
    return out


def _llm_judge_english_prompt_set_safe(
    *,
    before_english_prompt: str,
    after_english_prompt: str,
    storyboard_description: str,
    storyboard_camera_plan: str,
    shot_type: str,
    transition_video_prompt: str = "",
    model_override: Any = None,
) -> Dict[str, Any]:
    """
    LLM safety gate for allowing english_prompt.set in GLOBAL pass.
    Returns:
      {"ok": bool, "reason": str}
    """
    key = (
        (before_english_prompt or "").strip(),
        (after_english_prompt or "").strip(),
        (storyboard_camera_plan or "").strip(),
        (storyboard_description or "").strip(),
        (shot_type or "").strip().lower(),
        (transition_video_prompt or "").strip(),
    )
    # .get() атомарен под GIL — безопасно при конкурентной эвикции (см. _VIDEO_JUDGE_CACHE).
    cached = _ENGLISH_SET_JUDGE_CACHE.get(key)
    if cached is not None:
        return cached

    before_has_cyr = bool(re.search(r"[А-Яа-яЁё]", before_english_prompt or ""))
    after_has_cyr = bool(re.search(r"[А-Яа-яЁё]", after_english_prompt or ""))
    if before_has_cyr != after_has_cyr:
        out = {
            "ok": False,
            "reason": "language_changed_between_before_and_after_english_prompt",
        }
        _cache_set(_ENGLISH_SET_JUDGE_CACHE, key, out)
        return out

    model_obj = model_override or model_hard
    sys = (
        "You are a strict validator for a proposed full replacement of english_prompt.\n"
        "Decide whether AFTER is safe to apply given storyboard (camera_plan + description).\n"
        "Output STRICT JSON only.\n\n"
        "Rules:\n"
        "- Do NOT allow arbitrary PRIMARY SUBJECT changes. Infer the subject of this phase from the full storyboard sequence, preserving an explicitly specified focus shift or exit of the initial subject.\n"
        "- Do NOT allow introducing new story events not present in storyboard.description.\n"
        "- CAMERA_PLAN is an anchor, not an absolute lock for END:\n"
        "  - If shot_type='end': allow framing/shot-size changes ONLY if transition_video_prompt implies camera movement.\n"
        "  - If transition_video_prompt does NOT imply camera movement, AFTER must not contradict storyboard.camera_plan.\n"
        "  - If shot_type='start': AFTER must not contradict storyboard.camera_plan.\n"
        "- Be conservative: if unsure, return ok=false.\n"
        "- Return JSON: {\"ok\": true|false, \"reason\": \"...\"}\n"
    )
    payload = {
        "shot_type": (shot_type or ""),
        "storyboard_camera_plan": (storyboard_camera_plan or ""),
        "storyboard_description": (storyboard_description or ""),
        "transition_video_prompt": (transition_video_prompt or ""),
        "before_english_prompt": (before_english_prompt or ""),
        "after_english_prompt": (after_english_prompt or ""),
    }
    resp = call_openai_api(
        prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
        system_prompt=sys,
        model=model_obj,
        max_tokens=2000,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    obj = _extract_json_object(resp) or {}
    out = {"ok": bool(obj.get("ok")), "reason": str(obj.get("reason") or "")}
    _cache_set(_ENGLISH_SET_JUDGE_CACHE, key, out)
    return out


def _frame_visible_text_literals(frame_spec: Dict[str, Any]) -> List[str]:
    result = []
    for state in (frame_spec, frame_spec.get("start_state_spec") or {}, frame_spec.get("end_state_spec") or {}):
        for text in state.get("visible_readable_texts") or []:
            text = str(text)
            quoted = re.findall(r'«([^»]+)»|[„“"]([^"”]+)[”"]', text)
            literals = [part for match in quoted for part in match if part] or [text]
            for literal in literals:
                if literal and literal not in result:
                    result.append(literal)
    return result


def _video_prompt_has_unexpected_cyrillic(prompt: str, frame_spec: Dict[str, Any]) -> bool:
    prose = prompt
    for literal in _frame_visible_text_literals(frame_spec):
        prose = prose.replace(literal, "")
    return bool(re.search(r"[А-Яа-яЁё]", prose))


def _previous_end_paths(items):
    shots = {}
    ends = {}
    for item in items:
        scene, shot = _safe_int(item.get("scene_number")), _safe_int(item.get("shot_number"))
        shots.setdefault(scene, set()).add(shot)
        if str(item.get("shot_type") or "start").lower() == "end":
            ends[(scene, shot)] = str(item.get("output_path") or "")
    result = {}
    for scene, numbers in shots.items():
        ordered = sorted(numbers)
        for previous, current in zip(ordered, ordered[1:]):
            path = ends.get((scene, previous), "")
            if os.path.isfile(path):
                result[(scene, current)] = path
    return result


def _apply_repairs_to_items(
    items: List[Dict[str, Any]],
    repairs: List[Dict[str, Any]],
    storyboard_lookup: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
    image_styles: Optional[Dict[str, str]] = None,
    previous_end_paths: Optional[Dict[Tuple[int, int], str]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    Applies repairs in-place to items. Returns (applied_count, rejected_repairs)
    """
    rejected: List[Dict[str, Any]] = []
    applied = 0
    known_reference_paths = {
        str(entity["reference_image_path"]).strip()
        for item in items
        for field in ("characters", "locations")
        for entity in (item.get(field) or [])
        if isinstance(entity, dict) and entity.get("reference_image_path")
    }

    index: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
    for it in items:
        try:
            key = (int(it.get("scene_number")), int(it.get("shot_number")), str(it.get("shot_type")))
            index[key] = it
        except Exception:
            continue

    previous_end_paths = previous_end_paths if previous_end_paths is not None else _previous_end_paths(items)
    allowed_fields = _QA_REPAIR_ITEM_FIELDS

    def _video_prompt_contradiction(
        vp: str,
        storyboard_desc: str,
        storyboard_camera_plan: str,
        frame_spec: Dict[str, Any],
    ) -> bool:
        """
        Universal: allow START video_prompt edits only when it clearly contradicts storyboard.
        """
        verdict = _llm_judge_video_prompt_lite(
            before_video_prompt=vp or "",
            candidate_video_prompt=None,
            storyboard_description=storyboard_desc or "",
            storyboard_camera_plan=storyboard_camera_plan or "",
            frame_spec=frame_spec,
        )
        return bool(verdict.get("contradiction_before"))

    def _repair_apply_priority(rep: Dict[str, Any]) -> int:
        # START.video_prompt чинится первым, чтобы END.english_prompt того же шота
        # видел уже обновлённый transition_video_prompt в END judge (избегаем циркулярности
        # «END валидирован против устаревшего START.video_prompt»).
        if not isinstance(rep, dict):
            return 2
        set_obj = rep.get("set") or {}
        if not isinstance(set_obj, dict):
            return 2
        shot_type = str(rep.get("shot_type", "")).strip().lower()
        if shot_type == "start" and "video_prompt" in set_obj:
            return 0
        return 1

    ordered_repairs = sorted(repairs or [], key=_repair_apply_priority)
    # A shared spec may arrive in END while START's video is checked first.
    pending_frame_specs = {}
    for repair in ordered_repairs:
        if not isinstance(repair, dict):
            continue
        fields = repair.get("set") or {}
        repair_key = (_safe_int(repair.get("scene_number")), _safe_int(repair.get("shot_number")), str(repair.get("shot_type")))
        if repair_key in index and isinstance(fields, dict) and isinstance(fields.get("_shot_frame_spec"), dict) and fields["_shot_frame_spec"]:
            pending_frame_specs[repair_key[:2]] = fields["_shot_frame_spec"]

    for rep in ordered_repairs:
        try:
            scene_number = int(rep.get("scene_number"))
            shot_number = int(rep.get("shot_number"))
            shot_type = str(rep.get("shot_type"))
            key = (scene_number, shot_number, shot_type)
            target = index.get(key)
            if not target:
                rejected.append({**rep, "reject_reason": "shot_not_found"})
                continue

            set_obj = rep.get("set") or {}
            if not isinstance(set_obj, dict):
                rejected.append({**rep, "reject_reason": "set_is_not_object"})
                continue
            validation_frame_spec = pending_frame_specs.get(
                (scene_number, shot_number), target.get("_shot_frame_spec") or {},
            )
            rep_reasons = rep.get("reasons") or []
            has_reasons = isinstance(rep_reasons, list) and len(rep_reasons) > 0

            # Apply only allowed fields
            changed_any = False
            field_block_reasons: List[str] = []
            end_prompt_verdict = None
            # Ensure stable application order so that changes to characters/locations
            # can unlock reference_image_paths additions in the same repair.
            preferred_order = [
                "_shot_frame_spec",
                "characters",
                "locations",
                "reference_image_paths",
                "video_prompt",
                "english_prompt",
                "negative_prompt",
            ]
            ordered_keys = [k for k in preferred_order if k in set_obj] + [k for k in set_obj.keys() if k not in preferred_order]
            for k in ordered_keys:
                v = set_obj.get(k)
                if k not in allowed_fields:
                    continue
                if k == "_shot_frame_spec":
                    meta = storyboard_lookup.get((scene_number, shot_number), {}) if storyboard_lookup else {}
                    scene_characters = []
                    source_characters = meta.get("characters") if isinstance(meta, dict) and "characters" in meta else None
                    character_sources = (
                        [source_characters]
                        if source_characters is not None
                        else [
                            meta.get("visible_characters"),
                            (target.get("_shot_frame_spec") or {}).get("visible_characters"),
                            target.get("characters"),
                        ]
                    )
                    for source in character_sources:
                        if not isinstance(source, list):
                            continue
                        for entry in source:
                            name = entry.get("name") if isinstance(entry, dict) else entry
                            if str(name or "").strip() and str(name).strip() not in scene_characters:
                                scene_characters.append(str(name).strip())
                    normalized_spec = _normalize_shot_frame_spec(v, scene_characters)
                    if not normalized_spec:
                        field_block_reasons.append("shot_frame_spec_rejected:empty_normalized_spec")
                        continue
                    phase_items = [
                        candidate
                        for candidate in items
                        if _safe_int(candidate.get("scene_number")) == scene_number
                        and _safe_int(candidate.get("shot_number")) == shot_number
                        and str(candidate.get("shot_type", "")).strip().lower() in {"start", "end"}
                    ]
                    for phase_item in phase_items:
                        phase_item["_shot_frame_spec"] = normalized_spec
                    cache_key = str(
                        target.get("_shot_frame_spec_cache_key")
                        or next(
                            (candidate.get("_shot_frame_spec_cache_key") for candidate in phase_items if candidate.get("_shot_frame_spec_cache_key")),
                            "",
                        )
                    ).strip()
                    if cache_key:
                        with _shot_frame_spec_lock:
                            _SHOT_FRAME_SPEC_CACHE[cache_key] = normalized_spec
                        _store_shot_frame_spec_to_disk_cache(
                            cache_key=cache_key,
                            scene_number=scene_number,
                            shot_number=shot_number,
                            shot_frame_spec=normalized_spec,
                        )
                    changed_any = True
                    continue
                # ВАЖНО: video_prompt у END кадров НЕ редактируем (только START формирует/меняет video_prompt)
                if k == "video_prompt" and str(shot_type).strip().lower() == "end":
                    continue
                # END english_prompt guardrail: subject/objects/framing must align with storyboard + transition video_prompt.
                if k in {"english_prompt", "negative_prompt"} and str(shot_type).strip().lower() == "end" and end_prompt_verdict is None:
                    before_ep = target.get("english_prompt", "") or ""
                    desc = ""
                    cam_plan = ""
                    if storyboard_lookup is not None:
                        meta = storyboard_lookup.get((scene_number, shot_number)) or {}
                        desc = str(meta.get("description") or "")
                        cam_plan = str(meta.get("camera_plan") or "")
                    transition_vp = ""
                    start_it = index.get((scene_number, shot_number, "start"))
                    if start_it is not None:
                        transition_vp = str(start_it.get("video_prompt", "") or "")
                    rendered_image_url = _scene_render_review_image(
                        [item for item in (start_it, target) if item]
                    )
                    cand = str(set_obj.get("english_prompt", before_ep) or "")
                    if cand and desc:
                        verdict = _llm_judge_end_english_prompt_candidate(
                            before_english_prompt=before_ep,
                            candidate_english_prompt=cand,
                            storyboard_description=desc,
                            storyboard_camera_plan=cam_plan,
                            transition_video_prompt=transition_vp,
                            model_override=model_hard,
                            rendered_image_url=rendered_image_url,
                            start_english_prompt=str((start_it or {}).get("english_prompt") or ""),
                            phase_state_spec=(target.get("_shot_frame_spec") or {}).get("end_state_spec") or target.get("_shot_frame_spec") or {},
                            scene_continuity_facts=target.get("_scene_continuity_facts") or {},
                            repair_reasons=rep_reasons if isinstance(rep_reasons, list) else [],
                            image_style=(image_styles or {}).get(target.get("style_id"), ""),
                            before_negative_prompt=str(target.get("negative_prompt") or ""),
                            candidate_negative_prompt=str(set_obj.get("negative_prompt", target.get("negative_prompt")) or ""),
                        )
                        end_prompt_verdict = verdict
                        if verdict.get("candidate_ok") is True:
                            pass
                        else:
                            rewritten = verdict.get("rewritten_candidate")
                            rewritten_negative = verdict.get("rewritten_negative_prompt")
                            if isinstance(rewritten, str) and rewritten.strip():
                                set_obj["english_prompt"] = rewritten.strip()
                                if "english_prompt" not in ordered_keys:
                                    ordered_keys.append("english_prompt")
                            if isinstance(rewritten_negative, str):
                                set_obj["negative_prompt"] = rewritten_negative.strip()
                                if "negative_prompt" not in ordered_keys:
                                    ordered_keys.append("negative_prompt")
                            if not (isinstance(rewritten, str) and rewritten.strip()) and not isinstance(rewritten_negative, str):
                                field_block_reasons.append("english_prompt_end_blocked:judge_failed_no_rewrite")
                                continue
                        v = set_obj.get(k)
                elif k in {"english_prompt", "negative_prompt"} and end_prompt_verdict is not None:
                    if end_prompt_verdict.get("candidate_ok") is not True and not (
                        str(end_prompt_verdict.get("rewritten_candidate") or "").strip()
                        or isinstance(end_prompt_verdict.get("rewritten_negative_prompt"), str)
                    ):
                        continue
                    v = set_obj.get(k)
                # ВАЖНО: video_prompt у START редактируем ТОЛЬКО при явном противоречии storyboard (или если он пустой)
                if k == "video_prompt" and str(shot_type).strip().lower() == "start":
                    before_vp = target.get("video_prompt", "") or ""
                    if not str(before_vp).strip():
                        # allow filling empty, but validate candidate against camera_plan via LLM-judge
                        pass
                    else:
                        desc = ""
                        cam_plan = ""
                        if storyboard_lookup is not None:
                            meta = storyboard_lookup.get((scene_number, shot_number)) or {}
                            desc = meta.get("description") or ""
                            cam_plan = meta.get("camera_plan") or ""
                        # Если у нас нет описания — считаем, что нет явного противоречия (не трогаем)
                        if not desc:
                            field_block_reasons.append("video_prompt_blocked:no_storyboard_description")
                            continue
                        # Deterministic allowance for close-up plans: if formal violations exist (missing tokens / room-markers),
                        # allow the edit even if the LLM-judge contradiction check is flaky.
                        if _detect_closeup_video_prompt_violations(before_vp, cam_plan, desc):
                            pass
                        else:
                            if not _video_prompt_contradiction(before_vp, desc, cam_plan, validation_frame_spec):
                                field_block_reasons.append("video_prompt_blocked:not_contradictory")
                                continue

                    # Universal LLM validation of candidate; rewrite if needed
                    desc = ""
                    cam_plan = ""
                    if storyboard_lookup is not None:
                        meta = storyboard_lookup.get((scene_number, shot_number)) or {}
                        desc = meta.get("description") or ""
                        cam_plan = meta.get("camera_plan") or ""
                    judge = _llm_judge_video_prompt_lite(
                        before_video_prompt=before_vp or "",
                        candidate_video_prompt=str(v or ""),
                        storyboard_description=desc or "",
                        storyboard_camera_plan=cam_plan or "",
                        frame_spec=validation_frame_spec,
                    )
                    # Extra deterministic guard: enforce explicit framing tokens.
                    needs_close, needs_extreme = _requires_closeup_tokens(cam_plan or "")
                    cand_l = str(v or "").lower()
                    if needs_extreme and not any(t in cand_l for t in ["extreme close-up", "extreme close up"]):
                        judge["candidate_ok"] = False
                    if needs_close and ("close-up" not in cand_l and "close up" not in cand_l):
                        judge["candidate_ok"] = False
                    # If close-up plan: room markers are forbidden; use LLM-based check (in addition to LLM judge).
                    if needs_close and _has_any_closeup_room_markers(str(v or ""), cam_plan or "", desc or ""):
                        judge["candidate_ok"] = False
                    if judge.get("candidate_ok") is True:
                        pass
                    else:
                        rewritten = judge.get("rewritten_candidate")
                        if isinstance(rewritten, str) and rewritten.strip():
                            v = rewritten.strip()
                        else:
                            # If the candidate FIXES formal violations deterministically, accept even if judge is flaky.
                            cand2 = str(v or "")
                            cand2_l = cand2.lower()
                            ok_tokens = True
                            if needs_close and ("close-up" not in cand2_l and "close up" not in cand2_l):
                                ok_tokens = False
                            if needs_extreme and not any(t in cand2_l for t in ["extreme close-up", "extreme close up"]):
                                ok_tokens = False
                            if needs_close and _has_any_closeup_room_markers(cand2, cam_plan or "", desc or ""):
                                ok_tokens = False
                            if ok_tokens:
                                pass
                            else:
                                field_block_reasons.append("video_prompt_blocked:judge_failed_no_rewrite")
                                continue

                    # Note: we avoid deterministic "no X" scanners here. The LLM-judge prompt already enforces this rule
                    # and rewrites candidates when needed, which is more robust across languages/wording.

                    # Post-check after possible rewrite: still enforce explicit framing tokens.
                    cand_l2 = str(v or "").lower()
                    if needs_extreme and not any(t in cand_l2 for t in ["extreme close-up", "extreme close up"]):
                        field_block_reasons.append("video_prompt_blocked:missing_extreme_closeup_token")
                        continue
                    if needs_close and ("close-up" not in cand_l2 and "close up" not in cand_l2):
                        field_block_reasons.append("video_prompt_blocked:missing_closeup_token")
                        continue
                    if needs_close and _has_any_closeup_room_markers(str(v or ""), cam_plan or "", desc or ""):
                        field_block_reasons.append("video_prompt_blocked:contains_room_markers")
                        continue
                    # Instructions stay English; source-visible writing keeps its original script.
                    frame_spec = validation_frame_spec
                    if _video_prompt_has_unexpected_cyrillic(str(v or ""), frame_spec):
                        judge2 = _llm_judge_video_prompt_lite(
                            before_video_prompt=before_vp or "",
                            candidate_video_prompt=str(v or ""),
                            storyboard_description=desc or "",
                            storyboard_camera_plan=cam_plan or "",
                            frame_spec=frame_spec,
                        )
                        rewritten2 = judge2.get("rewritten_candidate")
                        if isinstance(rewritten2, str) and rewritten2.strip() and not _video_prompt_has_unexpected_cyrillic(rewritten2, frame_spec):
                            v = rewritten2.strip()
                        else:
                            field_block_reasons.append("video_prompt_blocked:contains_cyrillic")
                            continue
                if v is None:
                    continue
                if (
                    k == "english_prompt"
                    and str(shot_type).strip().lower() == "start"
                    and target.get("copy_from_previous_end") is True
                ):
                    target["copy_from_previous_end"] = False
                    target["link_type"] = "reference"
                    target["should_link_as_next_start"] = False
                    target["should_use_prev_end_as_reference"] = True
                    source_end_path = str(target.get("source_end_path") or "").strip()
                    existing_refs = target.get("reference_image_paths") or []
                    if isinstance(existing_refs, str):
                        existing_refs = [existing_refs]
                    if source_end_path and isinstance(existing_refs, list):
                        target["reference_image_paths"] = [
                            source_end_path,
                            *[ref for ref in existing_refs if ref != source_end_path],
                        ]
                # Guardrail: never drop continuity start image ref for END shots if it existed before.
                # Rationale: END кадр почти всегда должен редактировать continuity image1 (img_final_start_XX_YY.png),
                # иначе растёт риск дрейфа стиля/идентичности/фона.
                if k == "reference_image_paths" and str(shot_type).strip().lower() == "end":
                    before_refs = target.get("reference_image_paths") or []
                    if isinstance(before_refs, str):
                        before_refs = [before_refs]
                    after_refs = v or []
                    if isinstance(after_refs, str):
                        after_refs = [after_refs]
                    if isinstance(before_refs, list) and isinstance(after_refs, list):
                        continuity = None
                        for rp in before_refs:
                            s = str(rp or "")
                            if "/97_shots/" in s and "img_final_start_" in s and s.endswith(".png"):
                                continuity = rp
                                break
                        if continuity is not None and continuity not in after_refs:
                            # Prepend continuity to keep order: image1 first
                            after_refs = [continuity] + after_refs
                            v = after_refs

                # Guardrail: reference_image_paths edits are constrained.
                # - Allow deletions freely.
                # - Allow ONLY SAFE additions: known project character/location refs
                #   (characters[].reference_image_path / locations[].reference_image_path).
                # Rationale: at QA stage we must not invent new filesystem paths, but we CAN restore
                # canonical references from the same project catalog supplied to QA.
                if k == "reference_image_paths":
                    before_refs = target.get("reference_image_paths") or []
                    if isinstance(before_refs, str):
                        before_refs = [before_refs]
                    after_refs = v or []
                    if isinstance(after_refs, str):
                        after_refs = [after_refs]
                    if isinstance(before_refs, list) and isinstance(after_refs, list):
                        before_refs = [str(x) for x in before_refs if str(x)]
                        after_refs = [str(x) for x in after_refs if str(x)]
                        before_set = set(before_refs)

                        # Include metadata updated earlier in this repair as well.
                        allow_add: set = set(known_reference_paths)
                        previous_end = previous_end_paths.get((scene_number, shot_number)) if shot_type == "start" else None
                        if previous_end and os.path.isfile(previous_end):
                            allow_add.add(previous_end)
                        try:
                            for c in (target.get("characters") or []):
                                if isinstance(c, dict):
                                    rp = str(c.get("reference_image_path") or "").strip()
                                    if rp:
                                        allow_add.add(rp)
                            for loc in (target.get("locations") or []):
                                if isinstance(loc, dict):
                                    rp = str(loc.get("reference_image_path") or "").strip()
                                    if rp:
                                        allow_add.add(rp)
                        except Exception:
                            pass

                        # Identify additions requested by AFTER
                        requested_additions = [x for x in after_refs if x not in before_set]
                        # If there are additions outside allowlist -> forbid entire ref change (conservative)
                        if any(x not in allow_add for x in requested_additions):
                            logger.info(
                                "QA: forbid unsafe reference_image_paths add/rename for scene=%s shot=%s type=%s (unsafe_additions=%s)",
                                scene_number,
                                shot_number,
                                shot_type,
                                [x for x in requested_additions if x not in allow_add][:4],
                            )
                            continue

                        # Apply deletions (respect AFTER requested subset), preserving original order.
                        after_set = set(after_refs)
                        kept_ordered = [x for x in before_refs if x in after_set]

                        # Apply safe additions (preserve AFTER order), insert before first location ref if possible.
                        def _is_location_ref(p: str) -> bool:
                            return "/references/locations/" in p

                        insert_at = next((i for i, p in enumerate(kept_ordered) if _is_location_ref(p)), len(kept_ordered))
                        to_add = [x for x in requested_additions if x in allow_add and x not in kept_ordered]
                        for x in to_add:
                            kept_ordered.insert(insert_at, x)
                            insert_at += 1

                        v = kept_ordered
                target[k] = v
                changed_any = True

            if changed_any:
                applied += 1
            else:
                rr = "no_allowed_fields"
                if field_block_reasons:
                    rr += ":" + ";".join(sorted(set(field_block_reasons))[:6])
                rejected.append({**rep, "reject_reason": rr})
        except Exception as e:
            rejected.append({**rep, "reject_reason": f"exception:{e}"})

    return applied, rejected


def _extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    if text is None:
        return None
    if isinstance(text, dict):
        return text
    if isinstance(text, list):
        return {"repairs": text, "notes": "wrapped_list_response"}
    raw = str(text).strip()
    if not raw:
        return None
    try:
        cleaned = extract_json_from_markdown(raw)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"repairs": parsed, "notes": "wrapped_list_response"}
        return None
    except Exception:
        pass
    # fallback: ручная очистка code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", raw).strip()
        raw = raw.strip("`").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"repairs": parsed, "notes": "wrapped_list_response"}
        return None
    except Exception:
        return None


def _split_saved_image_style(prompt: str) -> Tuple[str, str]:
    """Keep frame content and reference roles separate from the ready style."""
    roles_match = re.search(r"(?:Опорные образы|Reference anchors|Reference images|Reference roles|References):", prompt)
    roles = prompt[roles_match.start():].strip() if roles_match else ""
    core = prompt[:roles_match.start()] if roles_match else prompt
    core = re.split(r"\b(?:Стиль изображения|Image style|Стиль|Style)\s*:", core, maxsplit=1)[0].strip()
    return core, roles


def _refresh_item_reference_roles(
    item: Dict[str, Any],
    location_index_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
    character_index_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    if "reference_image_paths" not in item:
        return
    if (
        "black screen" in _normalize_cam_plan(item.get("camera_plan") or "")
        or "#000000" in str(item.get("reference_roles_instruction") or "")
    ):
        return

    item["characters"] = [
        character_index_by_name[character.strip().lower()] if isinstance(character, str) else character
        for character in item.get("characters") or []
    ]
    item["locations"] = [
        location_index_by_name[location.strip().lower()] if isinstance(location, str) else location
        for location in item.get("locations") or []
    ]
    new_roles = _build_reference_roles_instruction(
        item.get("reference_image_paths") or [],
        item.get("characters") or [],
        item.get("locations") or [],
        location_index_by_name,
    )
    prompt = str(item.get("english_prompt") or "")
    _core, old_roles = _split_saved_image_style(prompt)
    if old_roles and old_roles in prompt:
        prompt = prompt[:prompt.find(old_roles)].rstrip()
    if new_roles:
        prompt = f"{prompt}\n\n{new_roles}".strip()
    item["english_prompt"] = prompt
    item["reference_roles_instruction"] = new_roles


def _split_roles_block(prompt: str) -> Tuple[str, str]:
    """
    Делит english_prompt на (core, suffix), где suffix включает блок ролей референсов,
    если он добавлен после двойного перевода строки.
    """
    p = (prompt or "")
    if "\n\n" not in p:
        return p, ""
    core, suffix = p.split("\n\n", 1)
    return core, "\n\n" + suffix


def _apply_text_patch(text: str, patch: Dict[str, Any]) -> str:
    """
    Применяет компактные patch-операции к строке.
    Поддерживаемые ключи:
      - set: str
      - prepend: str
      - append: str
      - replacements: [{find: str, replace: str}, ...]  (literal substring replace)
    """
    if not isinstance(patch, dict):
        return text
    if "set" in patch and isinstance(patch.get("set"), str):
        return patch["set"]
    out = text or ""
    reps = patch.get("replacements") or []
    if isinstance(reps, list):
        for r in reps:
            if not isinstance(r, dict):
                continue
            f = r.get("find")
            repl = r.get("replace")
            if isinstance(f, str) and isinstance(repl, str) and f and f in out:
                out = out.replace(f, repl)
    pre = patch.get("prepend")
    if isinstance(pre, str) and pre and not out.startswith(pre):
        out = pre + out
    app = patch.get("append")
    if isinstance(app, str) and app and app not in out:
        if out and not out.endswith(" "):
            out += " "
        out += app
    return out


def _apply_global_repairs_to_items(
    items: List[Dict[str, Any]],
    repairs: List[Dict[str, Any]],
    storyboard_lookup: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    Применяет глобальные (screenplay-level) repairs в формате компактных patch ops:
      repair.patch.english_prompt / video_prompt / negative_prompt
    """
    rejected: List[Dict[str, Any]] = []
    applied = 0

    index: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
    for it in items:
        try:
            key = (int(it.get("scene_number")), int(it.get("shot_number")), str(it.get("shot_type")))
            index[key] = it
        except Exception:
            continue

    def _global_repair_apply_priority(rep: Dict[str, Any]) -> int:
        # Те же причины, что и в _apply_repairs_to_items: START.video_prompt
        # должен примениться раньше END.english_prompt того же шота, иначе
        # END judge увидит устаревший transition_video_prompt.
        if not isinstance(rep, dict):
            return 2
        patch = rep.get("patch") or {}
        if not isinstance(patch, dict):
            return 2
        shot_type = str(rep.get("shot_type", "")).strip().lower()
        if shot_type == "start" and "video_prompt" in patch:
            return 0
        return 1

    ordered_repairs = sorted(repairs or [], key=_global_repair_apply_priority)

    for rep in ordered_repairs:
        try:
            scene_number = int(rep.get("scene_number"))
            shot_number = int(rep.get("shot_number"))
            shot_type = str(rep.get("shot_type"))
            key = (scene_number, shot_number, shot_type)
            target = index.get(key)
            if not target:
                rejected.append({**rep, "reject_reason": "shot_not_found"})
                continue
            patch = rep.get("patch") or {}
            if not isinstance(patch, dict):
                rejected.append({**rep, "reject_reason": "patch_is_not_object"})
                continue
            rep_reasons = rep.get("reasons") or []
            has_reasons = isinstance(rep_reasons, list) and len(rep_reasons) > 0
            if "[NEW]" in json.dumps(patch, ensure_ascii=False):
                rejected.append({**rep, "reject_reason": "contains_[NEW]"})
                continue

            changed_any = False

            if "english_prompt" in patch:
                before_ep = target.get("english_prompt", "") or ""
                ep_patch = patch.get("english_prompt") or {}
                # Narrowed rule:
                # - allow english_prompt.set ONLY when it is validated as storyboard-consistent and subject-preserving.
                if isinstance(ep_patch, dict) and isinstance(ep_patch.get("set"), str):
                    meta = (storyboard_lookup.get((scene_number, shot_number)) or {}) if storyboard_lookup else {}
                    cam_plan = meta.get("camera_plan") or ""
                    desc = meta.get("description") or ""
                    transition_vp = ""
                    try:
                        start_it = index.get((scene_number, shot_number, "start"))
                        if start_it is not None:
                            transition_vp = str(start_it.get("video_prompt", "") or "")
                    except Exception:
                        transition_vp = ""
                    verdict = _llm_judge_english_prompt_set_safe(
                        before_english_prompt=before_ep,
                        after_english_prompt=ep_patch.get("set") or "",
                        storyboard_description=desc or "",
                        storyboard_camera_plan=cam_plan or "",
                        shot_type=shot_type,
                        transition_video_prompt=transition_vp,
                        model_override=model_hard,
                    )
                    if not verdict.get("ok"):
                        rejected.append({**rep, "reject_reason": "english_prompt_set_rejected_by_judge"})
                        continue
                    after_ep = ep_patch.get("set") or ""
                else:
                    # micro-edits
                    after_ep = _apply_text_patch(before_ep, ep_patch if isinstance(ep_patch, dict) else {})
                if after_ep != before_ep:
                    target["english_prompt"] = after_ep
                    changed_any = True

            for field in ("video_prompt", "negative_prompt"):
                if field in patch:
                    # ВАЖНО: video_prompt у END кадров НЕ редактируем
                    if field == "video_prompt" and str(shot_type).strip().lower() == "end":
                        continue
                    # Global rule: for video_prompt we ONLY accept full replacement via set.
                    if field == "video_prompt":
                        vp_patch = patch.get("video_prompt")
                        # Auto-convert legacy/invalid shape: model may return a bare string instead of {"set": "..."}.
                        if isinstance(vp_patch, str) and vp_patch.strip():
                            vp_patch = {"set": vp_patch}
                            patch["video_prompt"] = vp_patch
                        if not isinstance(vp_patch, dict) or "set" not in vp_patch or not isinstance(vp_patch.get("set"), str):
                            rejected.append({**rep, "reject_reason": "video_prompt_non_set_forbidden_in_global_pass"})
                            continue
                    # ВАЖНО: video_prompt у START редактируем ТОЛЬКО при явном противоречии storyboard (или если он пустой)
                    if field == "video_prompt" and str(shot_type).strip().lower() == "start":
                        before_vp = target.get("video_prompt", "") or ""
                        desc = ""
                        cam_plan = ""
                        if storyboard_lookup is not None:
                            meta = storyboard_lookup.get((scene_number, shot_number)) or {}
                            desc = meta.get("description") or ""
                            cam_plan = meta.get("camera_plan") or ""
                        if not desc and str(before_vp).strip():
                            continue
                        # Allow editing if BEFORE contradicts storyboard OR if BEFORE is empty
                        if str(before_vp).strip():
                            # Narrowed gate: if model provided explicit reasons for change, allow,
                            # otherwise require contradiction_before.
                            verdict = _llm_judge_video_prompt_lite(
                                before_video_prompt=before_vp,
                                candidate_video_prompt=None,
                                storyboard_description=desc or "",
                                storyboard_camera_plan=cam_plan or "",
                                frame_spec=target.get("_shot_frame_spec") or {},
                            )
                            if not bool(verdict.get("contradiction_before")):
                                continue
                    before = target.get(field, "") or ""
                    after = _apply_text_patch(before, patch.get(field) or {})
                    # For START video_prompt edits, validate/possibly rewrite candidate to match camera_plan (incl. close-up bg rules)
                    if field == "video_prompt" and str(shot_type).strip().lower() == "start" and after != before:
                        needs_close, needs_extreme = _requires_closeup_tokens(cam_plan or "")
                        cand_l = (after or "").lower()
                        verdict2 = _llm_judge_video_prompt_lite(
                            before_video_prompt=before_vp or "",
                            candidate_video_prompt=after or "",
                            storyboard_description=desc or "",
                            storyboard_camera_plan=cam_plan or "",
                            frame_spec=target.get("_shot_frame_spec") or {},
                        )
                        if needs_extreme and not any(t in cand_l for t in ["extreme close-up", "extreme close up"]):
                            verdict2["candidate_ok"] = False
                        if needs_close and ("close-up" not in cand_l and "close up" not in cand_l):
                            verdict2["candidate_ok"] = False
                        if not verdict2.get("candidate_ok"):
                            rewritten = verdict2.get("rewritten_candidate")
                            if isinstance(rewritten, str) and rewritten.strip():
                                after = rewritten.strip()
                            else:
                                continue
                        # final token enforcement
                        cand_l2 = (after or "").lower()
                        if needs_extreme and not any(t in cand_l2 for t in ["extreme close-up", "extreme close up"]):
                            continue
                        if needs_close and ("close-up" not in cand_l2 and "close up" not in cand_l2):
                            continue
                    if after != before:
                        target[field] = after
                        changed_any = True

            if changed_any:
                applied += 1
            else:
                rejected.append({**rep, "reject_reason": "no_changes_after_patch"})
        except Exception as e:
            rejected.append({**rep, "reject_reason": f"exception:{e}"})

    return applied, rejected


def _global_screenplay_repair_pass(
    screenplay_scenes: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    *,
    model_obj: Any,
    temperature: float,
    max_repairs: int,
) -> List[Dict[str, Any]]:
    """
    Последний рубеж: LLM-сверка на уровне всего сценария.
    Возвращает компактные patch ops (не полные переписывания промптов).
    """
    # Компактный outline сценария (всё, но с агрессивным триммингом)
    outline: List[Dict[str, Any]] = []
    for sc in screenplay_scenes or []:
        try:
            sn = int(sc.get("scene_number", 0))
        except Exception:
            continue
        storyboard = []
        for sh in (sc.get("storyboard", []) or []):
            storyboard.append(
                {
                    "shot_number": sh.get("shot_number"),
                    "camera_plan": sh.get("camera_plan"),
                    "timing": sh.get("timing"),
                    "description": (sh.get("description", "") or ""),
                }
            )
        outline.append(
            {
                "scene_number": sn,
                "location_time": sc.get("location_time"),
                "characters": sc.get("characters"),
                "action": (sc.get("action", "") or ""),
                "storyboard": storyboard,
            }
        )

    # Компактный список промптов (ядро, без roles-хвоста)
    generated: List[Dict[str, Any]] = []
    for it in items:
        try:
            sn = int(it.get("scene_number"))
            shn = int(it.get("shot_number"))
        except Exception:
            continue
        st = str(it.get("shot_type"))
        core, _suffix = _split_roles_block(it.get("english_prompt", "") or "")
        generated.append(
            {
                "scene_number": sn,
                "shot_number": shn,
                "shot_type": st,
                "camera_plan": it.get("camera_plan"),
                "english_prompt_core": core,
                "video_prompt": (it.get("video_prompt", "") or ""),
            }
        )

    system_prompt = """You are the FINAL guardrail for screenplay alignment.

## Layer A: Role + scope + source of truth

You receive the FULL SCREENPLAY OUTLINE (all scenes + storyboard shot descriptions) and ALL generated prompts (core parts only).
Task: find remaining contradictions at the FULL SCREENPLAY level (cross-scene constraints, wrong POV, wrong staging).
Output: COMPACT PATCH OPS only. No full prompt rewrites. STRICT JSON object, no markdown.

## Layer B: Validation algorithm

1. For each generated prompt, verify alignment with its storyboard.description and camera_plan, including explicit optics and focal length. Shot size alone does not determine focal length.
2. Check cross-scene constraints: consistent POV, staging, character placement across scenes.
3. For any contradiction found, produce minimal patch ops (replacements/append/prepend for english_prompt; set for video_prompt).

## Layer C: Violation types + output format

### Hard rules
| Rule | Detail |
|------|--------|
| No "[NEW]" | Never include "[NEW]" in patches. |
| Minimal edits | Prefer replacements/append/prepend. |
| No raw newlines | Inside JSON strings. |
| END video_prompt | NEVER propose edits to END shot video_prompt. Only START video_prompt editable. |
| English video_prompt | English instructions; preserve source-visible text verbatim in its original script. Required end results must be explicitly achieved by END. |
| PRIMARY SUBJECT | Do NOT change the primary subject of any shot (from storyboard.description). |
| ACTION BOUNDARY | Do NOT move actions forward/backward across neighboring shots. No stealing, advancing, or rewinding actions. |
| START/END states | START = one T=0 frame according to t0_mode: frozen, early_motion or mid_action. END = the source-defined T=final stop-frame. No sequence of events in either image prompt. |
| OWN SHOT COVERAGE | Preserve the full action sequence of this storyboard shot. Its own later actions are not neighboring events; END follows its final action, including an explicit shift of attention or exit of the initial subject. |
| DIRECTION LOCK | Preserve EXACT movement direction from storyboard. Never reverse. |
| TIMING/MOMENT LOCK | Preserve EXACT timing. Contact = contact, not pre-contact. No phase shifting. |
| SCENE vs SHOT TRUTH | storyboard.description = ground truth. Do NOT spray scene.action props across shots. Remove foreign concrete objects. |
| REFERENCE APPEARANCE | The historical setting is established by canonical references. Remove historical-period labels, historical-accuracy demands and era-based object ban lists from english_prompt and negative_prompt. Preserve current-frame weather, season, states, staging and technical camera/light details. |
| SPATIAL LAYOUT | Keep the physical layout self-consistent with the screenplay. Do NOT invent venue/seating geometry absent from the storyboard. |
| CLOSE-UP FRAMING | MUST contain "close-up"/"extreme close-up" tokens. Remove room/venue establishing clauses. One blur-hint max. Keep primary subject unchanged. |
| SPLIT SCREEN | If camera_plan includes SPLIT SCREEN, video_prompt MUST contain "split-screen". |
| video_prompt set | When changing START video_prompt and cannot guarantee exact substring match, PREFER patch.video_prompt.set with FULL final string. |

### Output schema
{
  "repairs": [
    {
      "scene_number": <int>,
      "shot_number": <int>,
      "shot_type": "start"|"end",
      "reasons": [<string>, ...],
      "patch": {
        "english_prompt": { "replacements": [{"find":"...","replace":"..."}], "prepend":"...", "append":"..." },
        "video_prompt": { "set":"<FULL single-line English string>" },
        "negative_prompt": { "replacements": [{"find":"...","replace":"..."}], "prepend":"...", "append":"...", "set":"..." }
      }
    }
  ],
  "notes": "..."
}

### Constraints
- Maximum repairs: {MAX_REPAIRS}
- 'set' is FORBIDDEN for english_prompt. Only micro-edits allowed.
- For video_prompt: ONLY 'set' is allowed (no replacements/prepend/append). video_prompt must remain single-line English.
""".replace("{MAX_REPAIRS}", str(int(max_repairs)))

    # M-25: тот же доменный блок, что и в per-scene repair pass, чтобы кросс-сценовый
    # глобальный проход применял spatial/physics-локи активного профиля симметрично.
    # Дефолтный профиль пуст => ничего не добавляется, промпт остаётся универсальным.
    system_prompt += compose_qa_domain_prompt()

    payload = {"screenplay_outline": outline, "generated_prompts": generated}
    payload_str = json.dumps(payload, ensure_ascii=False)
    logger.info(f"🧪 shots_prompt_qa_tool: global payload_chars={len(payload_str)}")

    resp = call_openai_api(
        prompt=f"INPUT:\n{payload_str}",
        system_prompt=system_prompt,
        model=model_obj,
        max_tokens=40000,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    obj = _extract_json_object(resp) or {}
    repairs = obj.get("repairs") or []
    if not isinstance(repairs, list):
        return []
    return repairs[: int(max_repairs)]


def shots_prompt_qa_tool(
    session_id: str,
    project_id: str,
    shots_data: Optional[Dict[str, Any]] = None,
    enable: bool = True,
    model: str = "ultimate",
    temperature: float = 0.1,
    max_scenes: Optional[int] = None,
    scene_numbers: Optional[List[int]] = None,
    shot_keys: Optional[List[str]] = None,
    force: bool = False,
    global_max_repairs: int = 0,
    dry_run: bool = False,
    review_findings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    LLM validate+repair: compares generated prompts in shots.json vs screenplay.json scene-by-scene,
    and applies minimal JSON patches to fix contradictions BEFORE image generation.

    IMPORTANT:
    - No deterministic validator step is used for deciding correctness.
    - Does NOT generate images; only edits prompts/refs metadata.

    Args:
        session_id: Workflow/session identifier used for tracing and report metadata.
        project_id: Storybook project id (plots/storybooks/{project_id}/...).
        shots_data: Optional shots structure (output of screenplay_shots_generator_tool). If not provided,
            the tool reads plots/storybooks/{project_id}/97_shots/shots.json from disk.
        enable: If False, the tool is skipped and returns shots_data (or an empty items structure).
        model: LLM model name to use for validation/repair.
        temperature: Sampling temperature for the LLM repair step.
        max_scenes: Optional limit for number of scenes to process (for partial runs / debugging).
        scene_numbers: Optional explicit list of scene numbers to process (e.g., [1, 3, 5]). If provided,
            QA runs only on those scenes (intersection with available scenes). If max_scenes is also provided,
            it is applied after filtering by scene_numbers.
        shot_keys: Optional explicit list of scene-shot keys (e.g., ["3-2", "3-4"]). If provided,
            QA and repairs are restricted to those shots; the surrounding screenplay remains read-only context.
        force: If True, ignores prompts_validated flag and runs validation again.
        global_max_repairs: Max number of global screenplay-level patch repairs to apply at the end.
            По умолчанию отключено, чтобы не добавлять второй поздний LLM-rewrite поверх scene-level QA.
        dry_run: If True, runs validation/repair but does NOT write shots.json/report to disk.
        review_findings: Optional rendered-frame findings with scene_number, shot_number, shot_type, observed and required.

    Returns:
        Updated shots_data dict (items.json compatible). Also writes updated shots.json and a QA report to disk.
        If the shots.json write itself fails, raises RuntimeError so the next step cannot continue with stale
        on-disk data. A failure writing the QA report alone is logged, because shots.json was already
        persisted successfully.
    """
    if not enable:
        logger.info("🧪 shots_prompt_qa_tool: отключено (enable=False)")
        return shots_data or {"items": [], "consistency_rules": []}

    # Используем существующий model_mapping (строка -> объект модели). В output ничего не сохраняем.
    model_obj = None
    if isinstance(model, str):
        m = model.strip()
        model_obj = model_mapping.get(m)
        if model_obj is None:
            aliases = {
                "hard": "model_hard",
                "code": "model_code",
                "lite": "model_lite",
                "summary": "model_summary",
                "big": "model_big",
                "ultimate": "model_ultimate",
            }
            model_obj = model_mapping.get(aliases.get(m.lower(), ""))
    if model_obj is None:
        logger.warning(f"🧪 shots_prompt_qa_tool: модель '{model}' не найдена в model_mapping, используем model_hard")
        model_obj = model_hard

    base = str(safe_storybook_project_dir(project_id))
    image_style_catalog = _read_json(f"{base}/30_style/image_styles.json") or {}
    from .style_keeper import resolve_image_style_id
    project_brief = _read_json(f"{base}/00_brief.json") or {}
    project_language = str(project_brief.get("language") or "").lower()
    screenplay_path = f"{base}/91_screenplay/screenplay.json"
    shots_path = f"{base}/97_shots/shots.json"
    report_path = f"{base}/97_shots/shots_prompt_qa_report.json"

    if shots_data is None:
        shots_data = _read_json(shots_path) or {"items": [], "consistency_rules": []}

    saved_items = (_read_json(shots_path) or {}).get("items") or shots_data.get("items") or []
    previous_end_paths = _previous_end_paths(saved_items)

    # Skip повторную валидацию, если ЗАПРОШЕННЫЕ сцены уже провалидированы (если не force).
    # M-27: per-scene coverage. Новый prompts_validated=True выставляется только при полном
    # покрытии, поэтому флаг True => можно пропустить любой скоуп. Иначе пропуск только если
    # requested ⊆ prompts_validated_scenes. Легаси prompts_validated=True без scenes => scope=all.
    # A shot-level request must always inspect the requested shots, even when the
    # containing scene was previously marked as validated.
    if not force and shot_keys is None:
        on_disk_meta: Dict[str, Any] = {}
        if os.path.exists(shots_path):
            try:
                on_disk_meta = _read_json(shots_path) or {}
            except Exception:
                on_disk_meta = {}
        input_current = shots_data.get("prompts_validated_spec_version") == _SHOT_FRAME_SPEC_VERSION
        disk_current = on_disk_meta.get("prompts_validated_spec_version") == _SHOT_FRAME_SPEC_VERSION
        validated_flag = (
            (input_current and bool(shots_data.get("prompts_validated")))
            or (disk_current and bool(on_disk_meta.get("prompts_validated")))
        )
        validated_scenes = {
            _safe_int(x) for x in (shots_data.get("prompts_validated_scenes") or []) if input_current
        } | {
            _safe_int(x) for x in (on_disk_meta.get("prompts_validated_scenes") or []) if disk_current
        }
        validated_scenes.discard(-1)
        if scene_numbers is not None:
            try:
                requested = {int(x) for x in (scene_numbers or [])}
            except Exception:
                requested = set()
            can_skip = validated_flag or (bool(requested) and requested <= validated_scenes)
        else:
            # requested = все сцены; полное покрытие соответствует prompts_validated=True.
            can_skip = validated_flag
        if can_skip:
            logger.info("🧪 shots_prompt_qa_tool: пропуск — запрошенные сцены уже провалидированы")
            return shots_data if input_current else on_disk_meta

    if shots_data.get("prompts_validated_spec_version") != _SHOT_FRAME_SPEC_VERSION:
        shots_data["prompts_validated"] = False
        shots_data["prompts_validated_scenes"] = []

    screenplay_data = _read_json(screenplay_path) or {}
    screenplay_scenes: List[Dict[str, Any]] = screenplay_data.get("screenplay", []) or []
    if not screenplay_scenes:
        logger.warning("🧪 shots_prompt_qa_tool: screenplay пустой или не найден, пропускаем")
        return shots_data

    items: List[Dict[str, Any]] = shots_data.get("items", []) or []
    # Lookup storyboard meta by (scene_number, shot_number) for gating video_prompt edits
    storyboard_lookup: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for sc in screenplay_scenes or []:
        try:
            sn = int(sc.get("scene_number", 0))
        except Exception:
            continue
        for sh in (sc.get("storyboard", []) or []):
            try:
                shn = int(sh.get("shot_number", 0))
            except Exception:
                continue
            storyboard_lookup[(sn, shn)] = {
                "camera_plan": sh.get("camera_plan", "") or "",
                "description": sh.get("description", "") or "",
                "timing": sh.get("timing", "") or "",
                "characters": list(sc.get("characters") or []),
            }

    if not items:
        # H-5: пустой items — это провал генерации кадров, а НЕ «успешная» валидация.
        # Раньше здесь молча возвращался shots_data, из-за чего пустая генерация
        # помечалась как пройденная. Теперь падаем явно.
        raise RuntimeError(
            "shots_prompt_qa_tool: shots_data.items пустой — нечего валидировать "
            "(генерация кадров не дала ни одного шота)"
        )

    all_validatable_scenes = sorted(
        {_safe_int(it.get("scene_number")) for it in items}
        & {_safe_int(sc.get("scene_number")) for sc in screenplay_scenes}
    )
    scene_numbers_all = list(all_validatable_scenes)
    requested_shot_pairs: Optional[set[Tuple[int, int]]] = None
    if shot_keys is not None:
        requested_shot_pairs = set()
        for raw_key in shot_keys or []:
            parts = str(raw_key).strip().split("-", 1)
            if len(parts) != 2:
                continue
            try:
                requested_shot_pairs.add((int(parts[0]), int(parts[1])))
            except (TypeError, ValueError):
                continue
        scene_numbers_all = [
            sn for sn in scene_numbers_all
            if any(sn == pair[0] for pair in requested_shot_pairs)
        ]
    if scene_numbers is not None:
        try:
            allow = {int(x) for x in (scene_numbers or [])}
        except Exception:
            allow = set()
        scene_numbers_all = [sn for sn in scene_numbers_all if sn in allow]
    if max_scenes is not None:
        scene_numbers_all = scene_numbers_all[: max(0, int(max_scenes))]
    scope_scene_set = set(scene_numbers_all)

    def _item_in_requested_scope(item: Dict[str, Any]) -> bool:
        if _safe_int(item.get("scene_number")) not in scope_scene_set:
            return False
        if requested_shot_pairs is None:
            return True
        return (
            _safe_int(item.get("scene_number")),
            _safe_int(item.get("shot_number")),
        ) in requested_shot_pairs

    # World continuity belongs in the positive reference role, not a negated exclusion.
    frozen_background_tokens = {
        "без переноса фоновых якорей", "do not move background anchors",
        "sin mover anclas del fondo", "sans deplacer les ancrages de fond",
        "keine verschiebung der hintergrundanker",
        "без замены фоновой архитектуры", "без смены геометрии локации",
        "без другого задника", "без абстрактного фона",
        "do not replace background architecture", "do not change location geometry",
        "no alternate backdrop", "no abstract background",
        "sin reemplazar la arquitectura del fondo", "sin cambiar la geometria de la ubicacion",
        "sin fondo alternativo", "sin fondo abstracto",
        "sans remplacer l'architecture de fond", "sans changer la geometrie du lieu",
        "sans decor alternatif", "sans fond abstrait",
        "keine ersetzung der hintergrundarchitektur", "keine anderung der ortsgeometrie",
        "kein anderer hintergrund", "kein abstrakter hintergrund",
    }
    for item in items:
        if not _item_in_requested_scope(item):
            continue
        item["negative_prompt"] = ", ".join(
            token.strip() for token in str(item.get("negative_prompt") or "").split(",")
            if token.strip() and token.strip().casefold() not in frozen_background_tokens
        )

    scenes_by_number: Dict[int, Dict[str, Any]] = {}
    for sc in screenplay_scenes:
        try:
            scenes_by_number[int(sc.get("scene_number", 0))] = sc
        except Exception:
            continue

    items_by_scene: Dict[int, List[Dict[str, Any]]] = {}
    available_references: Dict[Tuple[str, str], Dict[str, str]] = {}
    locations_path = f"{base}/20_bible/locations.json"
    current_locations: Dict[str, Dict[str, Any]] = {}
    location_catalog = []
    if os.path.exists(locations_path):
        location_catalog = _read_json(locations_path)
        current_locations = {location.get("reference_image_path"): location for location in location_catalog}
        for it in items:
            if not _item_in_requested_scope(it):
                continue
            it["locations"] = [
                current_locations.get(location.get("reference_image_path"), location)
                if isinstance(location, dict) else location
                for location in it.get("locations") or []
            ]
    location_index_by_name = {
        str(location.get("name") or "").strip().lower(): location
        for location in location_catalog
        if isinstance(location, dict) and str(location.get("name") or "").strip()
    }
    character_index_by_name = {
        character["name"].strip().lower(): character
        for character in (_read_json(f"{base}/20_bible/characters.json") or [])
    }
    for item in items:
        if _item_in_requested_scope(item):
            _refresh_item_reference_roles(item, location_index_by_name, character_index_by_name)
    for it in items:
        try:
            sn = int(it.get("scene_number"))
        except Exception:
            continue
        if not _item_in_requested_scope(it):
            continue
        items_by_scene.setdefault(sn, []).append(it)
        for entity_field in ("characters", "locations"):
            for entity in it.get(entity_field) or []:
                if not isinstance(entity, dict):
                    continue
                name = str(entity.get("name") or "").strip()
                path = str(entity.get("reference_image_path") or "").strip()
                if name and path:
                    available_references[(name, path)] = {"name": name, "reference_image_path": path}

    system_prompt = """You are a strict validator and repair tool for STORYBOARD SHOT PROMPTS.

## Layer A: Role + scope + source of truth

You receive ONE SCENE from the screenplay + all GENERATED SHOTS (start/end) for that scene.
Task: detect contradictions between generated prompts and the screenplay, then REPAIR prompts to match.
Perform all three audits independently, in this order:
1. Compare the full shot_frame_specs against the original source sequence and retained object states from preceding storyboard descriptions; repair a conflicting derived spec.
2. Inspect EVERY START and END english_prompt and negative_prompt against the source and that item's phase_state_spec: visible states, pose/contact geometry, exclusions and explicitly required optics. Emit image/negative repairs for contradictions even when video repairs are also required.
3. Inspect video_prompt, including the detected formal video violations. Those violations are a minimum list, not the complete scope of this task.
Canonical identity is unchanged by a silhouette, back view or occlusion: use the canonical name in the phase cast, describe hidden features in pose/occlusion, and do not turn a ban on recognizable facial details into a ban on the character.
Use each frame's style_id and INPUT.image_styles to distinguish artistic rendering texture from material features inside the scene. Do not erase the bound medium's grain or brushwork merely because a depicted material disappears.

Source of truth hierarchy (highest to lowest):
0. INPUT.source_brief, when provided, contains the original user material. Preserve its required facts; derived screenplay elaborations must not override it. Do not treat incidental ambiguous details as new requirements absent from that source.
1. Original storyboard descriptions and camera plans: the current shot determines its action/framing, while earlier explicit object/part states persist until the source changes them. Silence about a part is not its destruction or removal.
2. shot_frame_spec (derived data, authoritative only when compatible with that complete source continuity)
3. scene.action + scene_continuity_facts (broader context, NOT a license to inject extra props into every shot)

## Layer B: Validation algorithm

### Step 1: Output format enforcement
- Output MUST be a single JSON object (no markdown). Strictly valid JSON.
- No raw newline characters inside strings (use "\n" if needed). Prefer single-line strings.
- If you propose a fix for english_prompt/video_prompt, return FULL FINAL CONTENT. No fragments, no ellipses, no truncation.
- Keep existing "roles of reference images" blocks in english_prompt unless directly contradictory.
- Repair scene-specific content without generating, choosing or appending an artistic style; code adds the saved bound style after validation.

### Step 2: Per-shot validation against shot_frame_spec
If labeled rendered images are attached, inspect their actual pixels against the source and their own phase, independently of whether the written prompt sounds correct. Labels identify scene.shot and START/END. Missing renders are not an image defect.
Describe the complete target frame, starting with the required visible state (support, limb/prop arrangement or completed coverage), before unchanged appearance. Do not write an audit-repair instruction such as "fix/correct the render": the image model receives canonical references or the valid adjacent frame, not necessarily the failed image being audited. Refer to canonical characters by name rather than repeating their reference appearance; preserve current wardrobe changes, camera, lighting and technical details. If the correct prompt already describes the required target state, preserve it verbatim for image-stage retry. Do not add requirements or change source facts to justify a faulty render. Avoid corrections based only on ambiguous incidental details.
Describe the required visible state concisely and positively, including its palette, lighting and framing. Preserve source-compatible surroundings; do not grow inventories of absent incidental background objects, landmarks or light fixtures.
For quantity checks, distinguish whole object instances from their ordinary parts, supports, shadows and outlines. Do not turn a suspected count error into a ban on normal parts or necessary supports of the required object; discard an unsupported derived restriction rather than enforcing it.
If shot_frame_spec exists for a shot, validate against it only when it agrees with the complete source continuity. A partial change does not imply complete absence; repair any derived exclusion that erases or bans surviving parts. Describe named surviving parts as visible parts of that same object, not as substitute landmarks. Source requirements outrank derived phase facts and exclusions: repair conflicting shared/phase specs and positive/negative prompts together in the same set, preserving legitimate crop limits. Negative tokens name unwanted states, not instructions with another negation inside an exclusion list.
- primary_subject defines the visual focus. Do NOT swap subjects.
- Visual focus does not determine physical depth: a primary subject may remain farther from the camera than a secondary character. Preserve the route order and camera-relative foreground/background relationship rather than moving the focal subject to the front.
  Foreground means physically nearer to the camera, not merely the main compositional accent. Audit the written prompts of copied START frames too; copying valid pixels does not excuse a contradictory saved prompt.
- must_show = mandatory visible facts; repair if missing from prompts.
- must_not_show = forbidden facts; repair if present in prompts.
- visible_characters = allowed visible cast.
- Each generated item's phase_state_spec is its own START or END state; validate its image and negative prompt separately. Full start_state_spec/end_state_spec/transition_spec are in shot_frame_specs; transition_spec applies to START video_prompt.
- Shared must_show/must_not_show constraints must hold in both phases. An END result required by the source must not remain forbidden globally or in END; move a START-only restriction into start_state_spec. Repair a conflicting shared restriction instead of weakening the required completed result. Check consistency across phase lists, pose and world_physics as well as the image prompt.
- A START with copy_from_previous_end=true reuses the previous END pixels instead of rendering its written prompt. Compare that previous END with the new START's subject, framing, camera and physical state. If they differ, emit set.english_prompt with the full correct START prompt (preserve it verbatim if already correct); this switches the existing full-copy link to reference-based generation. Do not force a copy just because the two shots share a location or a broad shot-size label.
- facial_expression / gaze_direction / pose_signature / character_pose_signatures: treat as authoritative. Do NOT reset to neutral or "looking at camera" unless spec says so.
- transition_spec.affect_delta: preserve facial/gaze/emotional delta in video_prompt.
- world_physics: respect support_state, surface_state, stability, contact_constraints, occlusion_constraints, forbidden_implications.

### Step 3: Per-shot validation against storyboard
For each shot, check:
1. Location/time matches scene (no wrong room/hall).
2. Spatial/zone consistency (physical placement of subjects/props per screenplay).
   Validate occlusion depth against the camera and physical boundary sides: a boundary component cannot occlude an actor positioned between that boundary and the camera. A divider separating figures on screen does not imply it covers either figure. Repair a derived occlusion that contradicts source placement in both the phase spec and image prompt; make the correct side visually readable through the actor's support and silhouette.
   Physical interactions must have compatible orientations and visible contact points; facial expression or a portrait pose must not replace the required contact. For covering/filling/blocking states, describe the occupied area and visible result, rather than placing the object merely nearby or on a support.
   For a source-assigned anatomical hand, make its visible screen position explicit when the viewpoint makes that projection unambiguous. Current source wardrobe, grip and prop ownership take priority over a reference pose or outfit.
   Track a removed garment as the same object through its current placement and use. A canonical outfit description must not put it back on the wearer while that garment is held, placed or used elsewhere; repair both the conflicting wardrobe phrase and its resulting prop state.
   If that hand assignment is ambiguous in the render, repeating "left/right hand" is insufficient: make its connection to the correct shoulder and its projected screen side unambiguous within the source crop.
   Preserve the scope of negative constraints: object part, owner, phase and degree. Repair exclusions broadened into a ban on the whole object or on required surviving parts; compare them against positive facts and the source.
For a view across a physical barrier, separate camera side, actor side and the visible opening. Retain natural occlusion and consistent actor scale; a wardrobe inventory does not justify enlarging an opening or showing body parts hidden by opaque structure. A source description of costume identifies the character; it does not require every body part to be visible simultaneously. Repair a derived must_show that turns that inventory into impossible visibility through the chosen framing. Rendered-image findings are observations to verify, not additional source requirements: if a finding demands a naturally occluded part, correct the derived visibility spec and prompts instead of forcing that part into view.
3. Camera plan consistency (shot size/framing and explicitly required optics/focal length not contradicted; shot size alone does not determine focal length).
   Body orientation relative to the camera is separate from gaze: facing_camera does not imply eye contact or direct address. Derive the visible front/back/side from the explicit camera position, body heading and route together. A camera ahead of forward-facing approaching subjects sees their fronts or front three-quarter views, even when their gaze passes the lens toward a goal. Actor-relative phrases such as "ahead of him" do not specify camera position. Repair contradictory orientation metadata and image prompts together without overriding the source camera plan.
   A changed pose, gaze or primary subject is not evidence that the camera physically moved. camera_position is relative to the current subject, so adjacent horizontal categories may describe the same oblique viewpoint. Derive the physical viewpoint from the source and preserve scene geometry; do not move the actors or camera merely to reconcile relative labels.
   Project the destination consistently too: on a straight approach toward a camera ahead of the actors, a destination farther along that same route is nearer the camera or outside its view, not behind the approaching actors. Do not inherit the canonical reference's foreground/background arrangement when the current camera requires a different projection.
   Measured sizes in a bad render are observations, not targets: derive target sizes from the planned camera move using one coherent ratio. With a fixed camera, START and END keep the same lens unless the source explicitly requires optical zoom or a focal-length change; movement changes framing, not lens.
   Express framing with the primary subject's visible scale and crop boundaries, not only a shot label or focal length. A location reference does not require showing the entire interior or its inventory; preserve geometry while respecting the source frame.
   Preserve world geometry, not fixed background screen coordinates. A camera following a traveling subject requires visibly advancing background landmarks and depth-dependent parallax consistent with travel; a frozen background with centered running limbs is not forward progress. With a fixed camera the subject travels across the unchanged background; a local gesture alone does not require parallax. Check this relationship in attached START/END renders and across continuous shots, without resetting travel to the canonical location view. Remove any prompt/reference-role/negative constraint that freezes required background projection changes.
   For traveling-camera shots whose environment persists, the END image prompt MUST specify concrete new screen positions/scale of existing visible landmarks relative to START, even before images are rendered. Saying only "slight parallax" is insufficient. Lateral tracking moves the background opposite to camera travel; preserve the same world rather than inventing new objects. Respect an explicitly transformed or replaced environment instead of retaining landmarks the source removes.
   Describe locomotion phases by visible configuration from this camera: stride width, limbs ahead of or behind the body, push-off, flight or landing, with near/far limbs where useful. Swapping anatomical left/right labels alone does not change the silhouette. A cyclic action may return to the same phase; do not force a different pose or prop position just to distinguish START/END. If derived intermediate poses obscure the source action, repair their phase facts together with the prompts while preserving source-assigned grips, supports and actions. Evaluate actual travel and background projection against the source camera movement.
4. Language: english_prompt and negative_prompt use INPUT.language when provided, otherwise the language of storyboard.description; preserve technical terms and reference labels. video_prompt uses English instructions; preserve source-visible text verbatim in its original script.
For a change of character visibility, distinguish source-required physical exits/entrances from revealing an already-present actor through framing or occlusion; preserve established positions and source order. Do not reduce a causal sequence to two actions or interpolate one identity into another. For quiet observation by a living subject, preserve the staged position while describing subtle breathing and source-consistent gaze/facial response; a wardrobe inventory and moving particles alone are insufficient.
5. For IMAGE prompts only: START = one T=0 frame according to start_state_spec.t0_mode (frozen before action, early_motion or mid_action); END = T=final stop-frame. An ongoing START pose is not a completed END result. START.video_prompt covers the whole shot from START to END, including its complete action sequence; required end results must be explicitly achieved by END, not merely approached or intended. END.video_prompt is intentionally empty and must not be filled or repaired.
6. Continuity: preserve unchanged object/part states and character posture, support and wardrobe from earlier source shots, including in negative exclusions. Do not force those parts into a crop where they are not visible; do not erase or prohibit them merely because the current description does not repeat them. When a visible character remains on a specific support, state that relation rather than leaving a generic static figure that could be placed on a different support.
   For cumulative actions, preserve already completed parts in place and add the missing parts of the same result; do not replace earlier marks with a shifted redraw unless the source explicitly erases or transforms them.
   Inspect adjacent END-to-START boundaries in the whole-scene render sheet, not only each pair in isolation. Follow physical sides of boundaries and actor order along the same path; a change of shot size or camera side does not relocate actors. Distinguish camera viewpoint from actor location, and apply clothing/prop states only to their own real or imagined episode.
   Where the source names a posture, "remains on the support" is insufficient: preserve that posture as well as the support location.
7. Multi-view location sheets: output ONE continuous frame (no collage/grid/multi-panel) unless storyboard explicitly requires split-screen.
8. reference_image_paths: change only when strictly necessary. For END shots, continuity start frame reference (img_final_start_*.png) MUST remain.
   When a source-required entity is missing, use its exact name/path descriptor from available_references for characters/locations and update the reference paths; do not guess file names. A listed previous END may be selected only for its specified START and only for object identity; current camera, pose and physical phase follow the current source.
9. Historical appearance comes from canonical references. Remove historical-period labels, historical-accuracy demands and era-based object ban lists from english_prompt and negative_prompt; preserve current-frame weather, season, states and camera/light details.
10. START is the beginning of this shot and END is the end of this same shot. Do not reject the shot's own later action as a neighboring event.
11. Change primary_subject only when this shot's storyboard explicitly changes attention or the hero leaves.

### Step 4: Cross-validation rules
Apply these violation checks in order. Each is a HARD CONTRADICTION requiring repair:

## Layer C: Violation types (compact reference)

### Identity & subject locks
| Lock | Rule |
|------|------|
| PRIMARY SUBJECT | Defined by storyboard.description. No swapping to another entity/object from another shot/scene. |
| ENTITY IDENTITY | storyboard.description = sole ground truth for WHO/WHAT. Correct wrong identity. No new named entities. Transliterate names in English video instructions, but preserve source-visible text verbatim in its original script. No generic species labels unless storyboard uses them. |
| ENTITY NATURE | Do NOT change entity type (human/anthropomorphic/animal/robot). Preserve visible non-human traits per character metadata (reptiles: scales/snout/tail; mammals: fur/muzzle; robots: mechanical parts). |

### Action & timing locks
| Lock | Rule |
|------|------|
| KEY ACTION/TIMING | video_prompt MUST reflect storyboard's key action and timing exactly. Contact = contact, not "approaching". Scraping = scraping, not hovering. Applause = applause. |
| DIRECTION | Preserve EXACT movement direction. Downward cues -> downward verbs only. Upward cues -> upward verbs only. Never reverse. |
| TIMING/MOMENT | Contact cues -> contact moment only. Pre-contact cues -> pre-contact state only. No phase shifting. |
| ACTION BOUNDARY | Do NOT move actions across neighboring shots. No stealing actions from prev/next storyboard shots. No advancing or rewinding actions. |

### Framing & camera locks
| Lock | Rule |
|------|------|
| CLOSE-UP FRAMING | If camera_plan is CLOSE UP/EXTREME CLOSE UP: video_prompt MUST contain "close-up"/"close up" (and "extreme close-up" if extreme). Preserve the source subject, its parts/support and explicitly required visible details; remove unrelated establishing descriptions. |
| SPLIT SCREEN | If camera_plan includes SPLIT SCREEN: video_prompt MUST mention "split-screen" explicitly. |
| END FRAMING vs CAMERA MOVEMENT | camera_plan = BASE/ANCHOR for START. END may differ ONLY if START video_prompt implies camera movement (dolly/zoom/push-in/pull-back). Validate END framing against both camera_plan and transition_video_prompt. |
| END CROP + SUPPORT | If END final_shot_size is close-up/medium close-up, do NOT describe full head-to-toe figure unless storyboard requires it. No inset panels, cutaway strips, or split-frame layouts for edge fragments. |

### POV & gaze locks
| Lock | Rule |
|------|------|
| POV/ANCHOR PHRASE | Keep explicit POV/anchor phrases from storyboard. Translate to English for video_prompt. No substitution (do not swap one anchor location for another). |
| POV CHARACTER REF | For POV shots: OK to show no body. If any body parts visible, they MUST match POV character identity. Add missing POV character reference_image_paths. |
| OVER-SHOULDER IDENTITY | Visible back/shoulder/head silhouette MUST match named character identity. |
| REACTION/GAZE | No forced "looking at camera" for reaction shots unless storyboard says so. Prefer off-screen/sideways gaze. |

### Content integrity locks
| Lock | Rule |
|------|------|
| SCENE CONTEXT vs SHOT TRUTH | Do NOT "spray" scene.action props/displays/events across multiple shots. Remove concrete objects not required by this shot's storyboard.description. |
| NO-INVENTION | No specific new objects/displays/holograms/symbols/props not in storyboard.description. Generic blur-hint only. No invented wardrobe, readable text, years, logos, signage, aurora unless storyboard says so. |
| LOCATION CANON NAME | If scene has location_canon_name, shots MUST use that exact canonical name. No variants with INT/EXT/time-of-day. |
| END CONTINUITY BACKGROUND | END edits of continuity frames MUST preserve at least one recognizable environment anchor. No replacement with abstract backdrop unless storyboard calls for it. |
| END UNLABELED FRAME | If visible_readable_texts is empty, no caption text/subtitles/overlays/title cards/credits. |
| END CONTINUITY COMPOSITION | If END is tighter than START, reference_roles_instruction must follow END framing, not START composition. |

### Object state locks (START/END split enforcement)
| Lock | Rule |
|------|------|
| THROW/HURL | START = pre-release (object in hand). END = post-release (object not in hand). Single object count unless storyboard says otherwise. |
| FALLING INTO HAND | START = pre-contact (mid-air, gap). END = contact/received (in hand). |
| FALLING ONTO BODY | START = pre-contact (mid-air, gap). END = post-contact (resting on body part). |
| CATCH/HANG-BY-THREAD | START = early fall (high in sky, no thread). END = post-catch (attached, hangs by thread). video_prompt = progression. Negative: START excludes thread; END requires attachment. |
| MATCH-CUT RECIPIENT | If instigator not in storyboard.description, do NOT add them as visible. |
| PROP HERO SHOT | Moving object = primary subject. Do not promote secondary elements to foreground. |
| ENVIRONMENT HERO | If shot is about environment/crowd state, do NOT turn into character-centric staging. |
| MOVING PROP | Match the source-defined final position; a cyclic movement may return to the same position. |
| CONTINUITY PROPS | Do NOT introduce new props/furniture not in Image 1 continuity reference. |

### Translation note
- video_prompt uses English instructions. Translate anchor nouns from Russian correctly, but preserve source-visible text verbatim in its original script.

### Video_prompt repair structure (semantic, not rigid)
When repairing START video_prompt:
1. camera clause (static/track/dolly/zoom)
2. framing token from camera_plan (close-up/extreme close-up/split-screen)
3. primary subject phrase (from storyboard.description, no placeholders like "SUBJECT FACE")
4. key action/timing (match storyboard exactly)
5. optional ONE short generic blur-hint for environment
6. tempo/micro-dynamics

### Detected violations (authoritative)
- If INPUT.detected_violations is non-empty, you MUST provide repairs for those entries.
- Do NOT return {"repairs": [], "notes": "ok"} when detected_violations is non-empty.

## Output schema
{
  "repairs": [
    {
      "scene_number": <int>,
      "shot_number": <int>,
      "shot_type": "start" | "end",
      "reasons": [<string>, ...],
      "set": {
        "_shot_frame_spec": {<full normalized spec, optional>},
        "english_prompt": <string, optional>,
        "video_prompt": <string, optional>,
        "negative_prompt": <string, optional>,
        "camera_position": <string, optional>,
        "character_orientation": <string, optional>,
        "final_camera_position": <string, optional>,
        "final_character_orientation": <string, optional>,
        "reference_image_paths": <array of strings, optional>,
        "characters": <array, optional>,
        "locations": <array, optional>
      }
    }
  ],
  "notes": <string>
}

If there are no issues, return {"repairs": [], "notes": "ok"}.
Each repair has only scene_number, shot_number, shot_type, reasons and set at the top level. Put every changed field, including video_prompt, inside that same set object, never after it. Combine image and video changes for the same frame into one repair.
For one logical shot, a full `_shot_frame_spec` repair is sufficient once; the code applies it to both START and END items. Do not invent a partial spec.
A prompt-only repair is insufficient when the derived spec still contradicts the source: include the full corrected `_shot_frame_spec`, and resolve ambiguous whole-object exclusions in its global, phase and transition fields before repairing the prompts.
"""
    # M-25: доменный блок (spatial/physics locks + RU->EN glossary + POV substitution)
    # вынесен в domain_glossary_config. Дефолтный профиль пуст => ничего не добавляется,
    # промпт остаётся универсальным. Активный проект выбирает профиль через
    # STORYBOOK_DOMAIN_PROFILE — качество не падает.
    system_prompt += compose_qa_domain_prompt()

    report: Dict[str, Any] = {
        "project_id": project_id,
        "session_id": session_id,
        "temperature": temperature,
        "scenes_processed": 0,
        "repairs_suggested": 0,
        "repairs_applied": 0,
        "repairs_rejected": 0,
        "rejected": [],
    }

    # Полный набор валидируемых сцен (пересечение сгенерированных и сценарных) —
    # нужен для M-27, чтобы отличать «покрыты все сцены» от частичного скоупа.
    # H-5: если в запрошенном скоупе не оказалось ни одного шота к валидации —
    # это пустая генерация в этом скоупе, а не успех. Считаем ожидаемые
    # (scene, shot) по storyboard vs присутствующие в items.
    items_in_scope = [
        it for it in items if _item_in_requested_scope(it)
    ]
    expected_pairs = {
        (sn, shn) for (sn, shn) in storyboard_lookup.keys()
        if sn in scope_scene_set
        and (requested_shot_pairs is None or (sn, shn) in requested_shot_pairs)
    }
    present_pairs = {
        (_safe_int(it.get("scene_number")), _safe_int(it.get("shot_number")))
        for it in items_in_scope
    }
    report["items_total"] = len(items_in_scope)
    report["missing_shots"] = [
        {"scene_number": sn, "shot_number": shn}
        for (sn, shn) in sorted(expected_pairs - present_pairs)
    ]
    if report["items_total"] == 0:
        raise RuntimeError(
            "shots_prompt_qa_tool: в запрошенном скоупе нет ни одного шота к "
            f"валидации (scenes={scene_numbers_all or 'all'}) — генерация кадров пуста"
        )

    # ------------------------------------------------------------------
    # Параллельная обработка сцен через ThreadPoolExecutor
    # ------------------------------------------------------------------
    _items_lock = threading.Lock()
    _report_lock = threading.Lock()
    # M-27: сцены, реально прошедшие валидацию (для per-scene coverage-статуса).
    _processed_scenes: set = set()

    def _process_scene_qa(scene_number: int) -> None:
        """Обрабатывает одну сцену: пре-валидация + LLM QA + применение ремонтов."""
        scene = scenes_by_number.get(scene_number) or {}
        scene_items = items_by_scene.get(scene_number) or []
        logger.info(
            f"🧪 shots_prompt_qa_tool: scene {scene_number} start "
            f"(storyboard_shots={len(scene.get('storyboard', []) or [])}, generated_items={len(scene_items)})"
        )

        storyboard = []
        for sh in (scene.get("storyboard", []) or []):
            storyboard.append(
                {
                    "shot_number": sh.get("shot_number"),
                    "timing": sh.get("timing"),
                    "camera_plan": sh.get("camera_plan"),
                    "description": sh.get("description"),
                    "characters": list(scene.get("characters") or []),
                }
            )

        scene_continuity_facts: Dict[str, Any] = {}
        for it0 in scene_items:
            candidate = it0.get("_scene_continuity_facts")
            if isinstance(candidate, dict) and candidate:
                scene_continuity_facts = candidate
                break
        if scene_continuity_facts:
            logger.info(
                "🧪 shots_prompt_qa_tool: scene %s reuse scene_continuity_facts from generated items",
                scene_number,
            )
        else:
            raw_scene_continuity_facts = scene.get("scene_continuity_facts")
            scene_continuity_facts = (
                dict(raw_scene_continuity_facts)
                if isinstance(raw_scene_continuity_facts, dict)
                else {}
            )
            logger.warning(
                "🧪 shots_prompt_qa_tool: scene %s missing embedded scene_continuity_facts; using screenplay input or empty dict",
                scene_number,
            )

        current_continuity = (scene.get("scene_continuity_facts") or {}).get("continuity")
        if isinstance(current_continuity, dict):
            scene_continuity_facts = dict(scene_continuity_facts, continuity=copy.deepcopy(current_continuity))

        shot_frame_specs: Dict[int, Dict[str, Any]] = {}
        for sh in (scene.get("storyboard", []) or []):
            try:
                shot_num_i = int(sh.get("shot_number", 0))
            except Exception:
                continue
            if requested_shot_pairs is not None and (scene_number, shot_num_i) not in requested_shot_pairs:
                continue
            cache_args = dict(
                scene_number=scene_number,
                shot_number=shot_num_i,
                shot_description=str(sh.get("description", "") or ""),
                camera_plan=str(sh.get("camera_plan", "") or ""),
                scene_characters=list(scene.get("characters") or []),
                location_time=str(scene.get("location_time", "") or ""),
                location_canon_name=str((sh.get("location_canon_name") or scene.get("location_canon_name") or "")).strip(),
                scene_action=_previous_shot_actions(scene.get("storyboard", []), shot_num_i),
                scene_continuity_facts=scene_continuity_facts,
            )
            cache_key = _build_shot_frame_spec_cache_key(**cache_args)
            phase_items = [it for it in scene_items if _safe_int(it.get("shot_number")) == shot_num_i]
            current = bool(phase_items) and all(
                it.get("_shot_frame_spec_cache_key") == cache_key
                and isinstance(it.get("_shot_frame_spec"), dict)
                and bool(it["_shot_frame_spec"])
                for it in phase_items
            )
            if current:
                shot_frame_specs[shot_num_i] = phase_items[0]["_shot_frame_spec"]
            else:
                logger.info("🧪 shots_prompt_qa_tool: refresh stale frame spec scene=%s shot=%s", scene_number, shot_num_i)
                shot_frame_spec = _extract_shot_frame_spec_llm(
                    **cache_args,
                    project_id=project_id,
                )
                for it in phase_items:
                    it["_shot_frame_spec"] = shot_frame_spec
                    it["_shot_frame_spec_cache_key"] = cache_key
                if shot_frame_spec:
                    shot_frame_specs[shot_num_i] = shot_frame_spec

        # Build a quick lookup of START video_prompts for transition grounding of END shots.
        start_vp_by_shot: Dict[int, str] = {}
        for it0 in scene_items:
            try:
                if str(it0.get("shot_type", "")).strip().lower() != "start":
                    continue
                shn0 = int(it0.get("shot_number", 0))
                start_vp_by_shot[shn0] = str(it0.get("video_prompt", "") or "")
            except Exception:
                continue

        generated_shots = []
        detected_violations: List[Dict[str, Any]] = []
        for it in sorted(scene_items, key=lambda x: (
            _safe_int(x.get("shot_number")),
            0 if str(x.get("shot_type") or "").lower() == "start" else 1,
        )):
            # Deterministic formal violations for START close-up video_prompt (forces LLM to actually output repairs)
            try:
                st_l = str(it.get("shot_type", "")).strip().lower()
                if st_l == "start":
                    # camera_plan is authoritative from storyboard, not from items (items may omit it)
                    cp = ""
                    desc_i = ""
                    try:
                        sn_i = int(it.get("scene_number"))
                        shn_i = int(it.get("shot_number"))
                        meta_i = storyboard_lookup.get((sn_i, shn_i)) or {}
                        cp = str(meta_i.get("camera_plan", "") or "")
                        desc_i = str(meta_i.get("description", "") or "")
                    except Exception:
                        cp = ""
                        desc_i = ""
                    vp = str(it.get("video_prompt", "") or "")
                    issues = _detect_closeup_video_prompt_violations(vp, cp, desc_i)
                    if issues:
                        detected_violations.append(
                            {
                                "shot_number": it.get("shot_number"),
                                "shot_type": it.get("shot_type"),
                                "camera_plan": cp,
                                "field": "video_prompt",
                                "violations": issues,
                            }
                        )

                    # Universal LLM-based trigger: if current START video_prompt contradicts storyboard, force a repair.
                    # No keyword lists here — let the judge decide semantically.
                    if desc_i and vp.strip():
                        try:
                            verdict = _llm_judge_video_prompt_lite(
                                before_video_prompt=vp,
                                candidate_video_prompt=None,
                                storyboard_description=desc_i,
                                storyboard_camera_plan=cp,
                                model_override=model_obj,
                                frame_spec=it.get("_shot_frame_spec") or {},
                            )
                            if verdict.get("contradiction_before") is True:
                                reason = str(verdict.get("reason") or "").strip()
                                violations = ["storyboard_contradiction"]
                                if reason:
                                    # keep it short to avoid ballooning payload
                                    violations.append("judge_reason:" + _safe_preview(reason, limit=240))
                                detected_violations.append(
                                    {
                                        "shot_number": it.get("shot_number"),
                                        "shot_type": it.get("shot_type"),
                                        "camera_plan": cp,
                                        "field": "video_prompt",
                                        "violations": violations,
                                    }
                                )
                        except Exception:
                            pass
            except Exception:
                pass
            # Authoritative storyboard meta (helps model avoid using stale item.camera_plan)
            sb_cam_plan = ""
            sb_desc = ""
            shn_i = 0
            try:
                sn_i = int(it.get("scene_number"))
                shn_i = int(it.get("shot_number"))
                meta_i = storyboard_lookup.get((sn_i, shn_i)) or {}
                sb_cam_plan = str(meta_i.get("camera_plan", "") or "")
                sb_desc = str(meta_i.get("description", "") or "")
            except Exception:
                sb_cam_plan = ""
                sb_desc = ""

            st_l = str(it.get("shot_type", "")).strip().lower()
            transition_vp = ""
            if st_l == "end":
                try:
                    transition_vp = start_vp_by_shot.get(int(it.get("shot_number", 0)), "") or ""
                except Exception:
                    transition_vp = ""

            item_frame_spec = it.get("_shot_frame_spec") or shot_frame_specs.get(shn_i, {})
            phase_state_spec = item_frame_spec.get(f"{st_l}_state_spec") or item_frame_spec
            visible_characters = phase_state_spec.get("visible_characters", item_frame_spec.get("visible_characters"))
            if isinstance(visible_characters, list):
                visible_names = {str(name).strip().casefold() for name in visible_characters}
                excluded_refs = {
                    character.get("reference_image_path")
                    for name, character in character_index_by_name.items()
                    if name.casefold() not in visible_names
                }
                it["characters"] = [
                    character for character in it.get("characters") or []
                    if str(character.get("name") or "").strip().casefold() in visible_names
                ]
                it["reference_image_paths"] = [
                    path for path in it.get("reference_image_paths") or [] if path not in excluded_refs
                ]
                _refresh_item_reference_roles(it, location_index_by_name, character_index_by_name)
            image_prompt = it.get("english_prompt") or ""
            style_id = None
            if image_style_catalog:
                frame_key = f"{scene_number}:{shn_i}:{st_l}"
                style_id = resolve_image_style_id(
                    image_style_catalog, frame_key, it.get("locations") or [], it.get("style_id")
                )
                it["style_id"] = style_id
                core, roles = _split_saved_image_style(image_prompt)
                image_prompt = core + (f"\n\n{roles}" if roles else "")
            generated_shots.append(
                {
                    "shot_number": it.get("shot_number"),
                    "shot_type": it.get("shot_type"),
                    "style_id": style_id,
                    "copy_from_previous_end": it.get("copy_from_previous_end", False),
                    "source_end_path": it.get("source_end_path"),
                    "camera_plan": it.get("camera_plan"),
                    "camera_position": it.get("camera_position"),
                    "character_orientation": it.get("character_orientation"),
                    "final_camera_position": it.get("final_camera_position"),
                    "final_character_orientation": it.get("final_character_orientation"),
                    "storyboard_camera_plan": sb_cam_plan,
                    "storyboard_description": sb_desc,
                    "phase_state_spec": phase_state_spec,
                    "transition_video_prompt": transition_vp,
                    "timing": it.get("timing"),
                    "english_prompt": image_prompt,
                    "video_prompt": it.get("video_prompt"),
                    "negative_prompt": it.get("negative_prompt"),
                    "reference_image_paths": it.get("reference_image_paths"),
                }
            )

        bound_image_styles = {
            shot["style_id"]: image_style_catalog["styles"][shot["style_id"]]["prompt"]
            for shot in generated_shots if shot["style_id"]
        }
        render_review_image = _scene_render_review_image(scene_items)
        rendered_image_findings: List[Dict[str, Any]] = []
        for shot_number in sorted({
            _safe_int(item.get("shot_number"))
            for item in scene_items
            if item.get("shot_number") is not None
        }):
            pair_items = [
                item for item in scene_items
                if _safe_int(item.get("shot_number")) == shot_number
                and str(item.get("shot_type") or "").strip().lower() in {"start", "end"}
                and os.path.isfile(str(item.get("output_path") or ""))
            ]
            review_items = list(pair_items)
            previous_end = previous_end_paths.get((scene_number, shot_number))
            if previous_end and any(item.get("shot_type") == "start" for item in scene_items
                                    if _safe_int(item.get("shot_number")) == shot_number):
                review_items.append(dict(scene_number=scene_number, shot_number=shot_number,
                                         shot_type="PREVIOUS_END_IDENTITY", output_path=previous_end))
            canonical_locations = []
            seen_location_paths = set()
            for item in pair_items:
                for location in item.get("locations") or []:
                    if not isinstance(location, dict):
                        continue
                    reference_path = str(location.get("reference_image_path") or "")
                    if reference_path.lstrip("/").startswith("references/"):
                        reference_path = os.path.join(base, "20_bible", reference_path.lstrip("/"))
                    if reference_path in seen_location_paths or not os.path.isfile(reference_path):
                        continue
                    seen_location_paths.add(reference_path)
                    label = f"CANONICAL_LOCATION_{len(canonical_locations) + 1}"
                    canonical_locations.append({"label": label, "name": location.get("name", "")})
                    review_items.append({
                        "scene_number": scene_number, "shot_number": shot_number,
                        "shot_type": label, "output_path": reference_path,
                    })
            pair_render_review_image = _scene_render_review_image(review_items)
            from .continuity import frame_continuity
            continuity_contexts = {item.get('shot_type', 'start'): frame_continuity(
                base, scene_number, shot_number, item.get('shot_type', 'start')) for item in pair_items}
            landmark_paths = {path for context in continuity_contexts.values()
                              for path in context.get('landmark_reference_paths', [])}
            if landmark_paths:
                review_items += [dict(scene_number=scene_number, shot_number=shot_number,
                    shot_type='SHARED_LANDMARK_GEOMETRY', output_path=path) for path in sorted(landmark_paths)]
                pair_render_review_image = _scene_render_review_image(review_items)
            if not pair_render_review_image:
                continue
            pair_storyboard = [shot for shot in storyboard if _safe_int(shot.get("shot_number")) == shot_number]
            pair_generated_shots = [shot for shot in generated_shots if _safe_int(shot.get("shot_number")) == shot_number]
            visual_audit_input = {
                "scene_number": scene_number,
                "scene_continuity_facts": scene_continuity_facts,
                "canonical_locations": canonical_locations,
                "continuity": continuity_contexts,
                "image_styles": {
                    shot["style_id"]: bound_image_styles[shot["style_id"]]
                    for shot in pair_generated_shots if shot["style_id"]
                },
                "storyboard": pair_storyboard,
                "generated_shots": [
                    {
                        key: shot.get(key)
                        for key in ("shot_number", "shot_type", "style_id", "camera_plan", "phase_state_spec", "english_prompt", "negative_prompt")
                    }
                    for shot in pair_generated_shots
                ],
            }
            logger.info(
                "🧪 shots_prompt_qa_tool: scene %s shot %s rendered-frame visual audit payload_chars=%s",
                scene_number,
                shot_number,
                len(json.dumps(visual_audit_input, ensure_ascii=False)),
            )
            visual_audit_response = call_openai_api(
                prompt="AUDIT_INPUT:\n" + json.dumps(visual_audit_input, ensure_ascii=False),
                system_prompt=(
                    "You are a rendered-frame visual audit. Inspect actual labeled pixels before written promises. "
                    "PREVIOUS_END_IDENTITY is only for object identity; current camera, pose and physical phase follow the current source. "
                    "Continuity identifies shared physical objects across scenes. SHARED_LANDMARK_GEOMETRY and its "
                    "fixed geometry override conflicting legacy CANONICAL_LOCATION designs. Current phase states "
                    "govern damage, support, possession and visibility; an offscreen prop must not follow the actor. "
                    "CANONICAL_LOCATION images identify the architecture and parts of the same place. Compare required visible surviving parts with those references; do not accept an unrelated landmark as their substitute. The current source defines damage, transformations, framing and occlusion: do not copy the canonical state or require its whole inventory in the shot. "
                    "Use each frame's style_id and the saved image_styles to distinguish rendering medium from in-scene materials. Paper grain, brushwork or film grain belonging to that style is not itself a surviving story object; require identifiable object geometry or features before flagging a failed material transformation. "
                    "Compare START/END background landmark positions and scales where visible against camera travel; "
                    "first locate two matching visible landmarks rather than inferring motion from the text. "
                    "Evaluate background projection against the actual camera movement and depth; same actor screen position does not freeze the world, but a small physically coherent shift is not a violation. "
                    "State the compared landmarks in observed, and separate limb changes. Inspect source-assigned hand/shoulder, posture/support, clothing, crop, count, contact and occlusion. "
                    "Compare visible body/action phase rather than written anatomical labels; cyclic locomotion may return to the same phase, so an equal silhouette alone is not a failure. Check source continuity facts and positive/negative conflicts as well as pixels. Respect physical boundary sides, surviving object parts and current wardrobe rather than inheriting a canonical pose or outfit. "
                    "For views through openings, locate the camera, actor and opaque barrier before judging visibility. Costume inventory does not require every body part to be visible: natural occlusion is correct even when a derived must_show demands the hidden part. Flag that derived requirement as the contradiction; never require the opening to grow or the opaque barrier to become transparent. "
                    "Count distinct whole objects, not their ordinary parts, supports, shadows or outlines; unclear boundaries do not establish an extra instance. Flag only clear contradictions, not ambiguous incidental details "
                    "or additional scene rules. Return JSON only: {\"violations\":[{\"shot_number\":1,\"shot_type\":\"end\",\"observed\":\"...\",\"required\":\"...\"}]}."
                ),
                model=model_obj,
                max_tokens=6000,
                temperature=0.1,
                response_format={"type": "json_object"},
                image_url=pair_render_review_image,
            )
            visual_audit_obj = _extract_json_object(visual_audit_response)
            if not isinstance(visual_audit_obj, dict) or not isinstance(visual_audit_obj.get("violations"), list):
                raise RuntimeError(f"QA rendered-frame visual audit returned invalid response for scene {scene_number} shot {shot_number}")
            for violation in visual_audit_obj["violations"]:
                if not isinstance(violation, dict):
                    raise RuntimeError(f"QA rendered-frame visual audit returned invalid violation for scene {scene_number} shot {shot_number}")
                try:
                    finding_shot_number = int(violation["shot_number"])
                    shot_type = str(violation["shot_type"]).strip()
                    observed = str(violation["observed"]).strip()
                    required = str(violation["required"]).strip()
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError(f"QA rendered-frame visual audit returned incomplete violation for scene {scene_number} shot {shot_number}")
                if not shot_type or not observed or not required:
                    raise RuntimeError(f"QA rendered-frame visual audit returned incomplete violation for scene {scene_number} shot {shot_number}")
                rendered_image_findings.append({
                    "scene_number": scene_number,
                    "shot_number": finding_shot_number,
                    "shot_type": shot_type,
                    "observed": observed,
                    "required": required,
                })
            logger.info(
                "🧪 shots_prompt_qa_tool: scene %s shot %s rendered-frame findings=%s",
                scene_number,
                shot_number,
                len(rendered_image_findings),
            )
        from .continuity_review import pending_image_findings
        selected_keys = {f"{item['scene_number']}-{item['shot_number']}" for item in scene_items}
        rendered_image_findings.extend(pending_image_findings(base, selected_keys))
        rendered_image_findings.extend(
            finding for finding in (review_findings or [])
            if f"{finding.get('scene_number')}-{finding.get('shot_number')}" in selected_keys
        )
        if rendered_image_findings:
            with _report_lock:
                report.setdefault("rendered_image_findings", []).extend(rendered_image_findings)

        payload = {
            "scene_number": scene_number,
            "language": project_language,
            "source_brief": str(project_brief.get("storybook_prompt") or ""),
            "location_time": scene.get("location_time"),
            "location_canon_name": scene.get("location_canon_name"),
            "action": scene.get("action"),
            "characters": scene.get("characters"),
            "available_references": list(available_references.values()) + [
                {"name": f"Previous END for {scene_number}:{shot_number}:start (object identity only)",
                 "reference_image_path": path}
                for (scene, shot_number), path in previous_end_paths.items()
                if scene == scene_number and any(_safe_int(item.get("shot_number")) == shot_number
                                                 and item.get("shot_type") == "start" for item in scene_items)
            ],
            "scene_continuity_facts": scene_continuity_facts,
            "shot_frame_specs": shot_frame_specs,
            "storyboard": storyboard,
            "generated_shots": generated_shots,
            "image_styles": bound_image_styles,
            "detected_violations": detected_violations,
            "rendered_image_findings": rendered_image_findings,
        }
        payload_str = json.dumps(payload, ensure_ascii=False)
        logger.info(f"🧪 shots_prompt_qa_tool: scene {scene_number} payload_chars={len(payload_str)}")

        violations_note = ""
        if detected_violations or rendered_image_findings:
            violations_note = (
                "IMPORTANT: INPUT.detected_violations is authoritative.\n"
                "- If detected_violations is non-empty, you MUST return repairs entries for those shots.\n"
                "- You are NOT allowed to return repairs=[] / notes='ok' while detected_violations is non-empty.\n"
                "- For each detected_violations entry, you MUST output set.video_prompt with the FULL corrected final single-line English video_prompt.\n"
                "- For every INPUT.rendered_image_findings entry, provide a matching FULL set.english_prompt repair for that shot (or a full set._shot_frame_spec repair when the derived spec conflicts with the primary source). Unrelated or video-only repairs do not close a rendered image finding.\n"
                "\n"
            )
        user_prompt = (
            "Validate and repair generated shots prompts for this scene.\n"
            "Return JSON patches ONLY where contradictions exist.\n\n"
            f"{violations_note}"
            "Audit every image and negative prompt independently before video prompts. "
            "The detected_violations list covers video checks only and does not replace the image/spec audit.\n\n"
            f"INPUT:\n{payload_str}"
        )

        obj = None
        last_resp_preview = ""

        def _rendered_findings_covered(repairs: List[Dict[str, Any]]) -> bool:
            for finding in rendered_image_findings:
                covered = False
                for repair in repairs:
                    if not isinstance(repair, dict):
                        continue
                    if _safe_int(repair.get("shot_number", -1)) != finding["shot_number"]:
                        continue
                    set_obj = repair.get("set") or {}
                    if not isinstance(set_obj, dict):
                        continue
                    if isinstance(set_obj.get("_shot_frame_spec"), dict) and set_obj["_shot_frame_spec"]:
                        covered = True
                        break
                    if (
                        str(repair.get("shot_type") or "").strip().lower() == finding["shot_type"].lower()
                        and isinstance(set_obj.get("english_prompt"), str)
                        and set_obj["english_prompt"].strip()
                    ):
                        covered = True
                        break
                if not covered:
                    return False
            return True

        for attempt in range(1, 4):
            missing_video_keys = []
            unsupported_set_fields = []
            resp = call_openai_api(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=model_obj,
                max_tokens=40000,
                temperature=temperature,
                response_format={"type": "json_object"},
                **({"image_url": render_review_image} if render_review_image else {}),
            )
            last_resp_preview = _safe_preview(resp, limit=600)
            obj = _extract_json_object(resp)
            if obj is not None:
                candidate_repairs = obj.get("repairs")
                if isinstance(candidate_repairs, list):
                    unsupported_set_fields = sorted({
                        field
                        for repair in candidate_repairs
                        if isinstance(repair, dict) and isinstance(repair.get("set"), dict)
                        for field in set(repair["set"]) - _QA_REPAIR_ITEM_FIELDS
                    })
                if not isinstance(candidate_repairs, list) or any(
                    not isinstance(repair, dict)
                    or set(repair) - {"scene_number", "shot_number", "shot_type", "reasons", "set"}
                    or not isinstance(repair.get("set"), dict)
                    or not repair["set"]
                    or set(repair["set"]) - _QA_REPAIR_ITEM_FIELDS
                    for repair in candidate_repairs
                ):
                    obj = None
            if obj:
                # If we provided detected_violations but model returned no repairs, force a retry with stricter wording.
                repairs_try = obj.get("repairs") or []
                required_video_keys = {
                    (_safe_int(violation.get("shot_number")), str(violation.get("shot_type") or "").lower())
                    for violation in detected_violations
                }
                repaired_video_keys = {
                    (_safe_int(repair.get("shot_number")), str(repair.get("shot_type") or "").lower())
                    for repair in repairs_try
                    if isinstance(repair["set"].get("video_prompt"), str)
                    and repair["set"]["video_prompt"].strip()
                }
                missing_video_keys = sorted(required_video_keys - repaired_video_keys)
                required_repairs_missing = rendered_image_findings and (
                    not isinstance(repairs_try, list) or not _rendered_findings_covered(repairs_try)
                )
                if (
                    missing_video_keys
                    or required_repairs_missing
                ):
                    logger.warning(
                        f"🧪 shots_prompt_qa_tool: scene {scene_number} required repairs missing on attempt {attempt}/3; retrying"
                    )
                    obj = None
                else:
                    break
            logger.warning(
                f"🧪 shots_prompt_qa_tool: scene {scene_number} invalid QA response on attempt {attempt}/3; "
                f"resp_preview='{last_resp_preview}'"
            )
            # tighten prompt for retries
            user_prompt = (
                "Return STRICT JSON ONLY (no markdown, no prose). "
                "All strings must be single-line (no raw newlines). "
                "Every repairs entry must be an object with a nonempty supported set; no placeholders or mutation fields outside set. "
                "If you need a line break, use \\n.\n\n"
                "CRITICAL: If INPUT.detected_violations is non-empty, you MUST return repairs for those shots. "
                "Cover EVERY detected entry with its full set.video_prompt; partial coverage and repairs=[] / ok are invalid.\n\n"
                f"Unresolved required video repairs: {json.dumps(missing_video_keys)}. Return the COMPLETE repair list, including previously covered entries.\n\n"
                f"Unsupported set fields: {json.dumps(unsupported_set_fields)}. Phase specs belong inside set._shot_frame_spec, not directly inside set.\n\n"
                "CRITICAL: Every INPUT.rendered_image_findings entry must have a matching FULL set.english_prompt repair for that shot, or a full set._shot_frame_spec repair when the derived spec conflicts with the source. Unrelated/video-only repairs do not close the finding.\n\n"
                f"{violations_note}"
                "Audit every image and negative prompt independently before video prompts; detected video violations do not replace that audit.\n\n"
                f"INPUT:\n{payload_str}"
            )

        if not obj:
            logger.warning(f"🧪 shots_prompt_qa_tool: scene {scene_number} invalid QA response after 3 attempts")
            return

        repairs = obj.get("repairs") or []
        if not isinstance(repairs, list):
            repairs = []
        # Since this is a per-scene pass, normalize any model mistakes in scene_number to the current scene_number.
        # This is universal (not content-specific) and prevents shot_not_found due to wrong scene id.
        normalized_repairs = []
        for r in repairs:
            if not isinstance(r, dict):
                continue
            rr = dict(r)
            rr["scene_number"] = scene_number
            normalized_repairs.append(rr)
        repairs = normalized_repairs
        if requested_shot_pairs is not None:
            repairs = [
                repair for repair in repairs
                if (
                    _safe_int(repair.get("scene_number")),
                    _safe_int(repair.get("shot_number")),
                ) in requested_shot_pairs
            ]

        # Thread-safe: применяем ремонты с блокировкой
        with _items_lock:
            applied_count, rejected = _apply_repairs_to_items(
                items, repairs, storyboard_lookup=storyboard_lookup, image_styles=bound_image_styles,
                previous_end_paths=previous_end_paths,
            )

        with _report_lock:
            report["scenes_processed"] += 1
            _processed_scenes.add(int(scene_number))
            report["repairs_suggested"] += len(repairs)
            report["repairs_applied"] += applied_count
            report["repairs_rejected"] += len(rejected)
            if rejected:
                report["rejected"].extend(rejected)

        logger.info(
            f"🧪 shots_prompt_qa_tool: scene {scene_number} done "
            f"(suggested={len(repairs)}, applied={applied_count}, rejected={len(rejected)})"
        )

    # Параллельный запуск обработки сцен
    QA_MAX_WORKERS = min(4, len(scene_numbers_all))  # ограничиваем параллелизм для API rate limits
    if QA_MAX_WORKERS <= 1:
        # Одна сцена — без overhead на пул
        for sn in scene_numbers_all:
            _process_scene_qa(sn)
    else:
        logger.info(f"🧪 shots_prompt_qa_tool: параллельная обработка {len(scene_numbers_all)} сцен (workers={QA_MAX_WORKERS})")
        with ThreadPoolExecutor(max_workers=QA_MAX_WORKERS) as executor:
            futures = {executor.submit(_process_scene_qa, sn): sn for sn in scene_numbers_all}
            for future in as_completed(futures):
                sn = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"🧪 shots_prompt_qa_tool: scene {sn} failed: {e}")

    unprocessed_scenes = set(scene_numbers_all) - _processed_scenes
    if unprocessed_scenes:
        raise RuntimeError(f"QA not completed for scenes: {sorted(unprocessed_scenes)}")

    # ------------------------------------------------------------
    # FINAL GUARDRAIL: screenplay-level pass across ALL prompts
    # ------------------------------------------------------------
    try:
        max_repairs = int(global_max_repairs) if global_max_repairs is not None else 25
        max_repairs = max(0, min(max_repairs, 100))
    except Exception:
        max_repairs = 25

    if max_repairs > 0 and report.get("scenes_processed", 0) > 0 and requested_shot_pairs is None:
        logger.info(f"🧪 shots_prompt_qa_tool: global screenplay pass start (max_repairs={max_repairs})")
        global_repairs = _global_screenplay_repair_pass(
            screenplay_scenes=screenplay_scenes,
            items=items,
            model_obj=model_obj,
            temperature=temperature,
            max_repairs=max_repairs,
        )
        report["global_repairs_suggested"] = len(global_repairs) if isinstance(global_repairs, list) else 0
        if global_repairs:
            g_applied, g_rejected = _apply_global_repairs_to_items(items, global_repairs, storyboard_lookup=storyboard_lookup)
            report["global_repairs_applied"] = g_applied
            report["global_repairs_rejected"] = len(g_rejected)
            if g_rejected:
                report["global_rejected"] = g_rejected
            logger.info(
                f"🧪 shots_prompt_qa_tool: global screenplay pass done "
                f"(suggested={len(global_repairs)}, applied={g_applied}, rejected={len(g_rejected)})"
            )
        else:
            report["global_repairs_applied"] = 0
            report["global_repairs_rejected"] = 0
            logger.info("🧪 shots_prompt_qa_tool: global screenplay pass done (no repairs)")

    # QA rewrites scene content; refresh reference roles after any reference repairs.
    for item in items_in_scope:
        _refresh_item_reference_roles(item, location_index_by_name, character_index_by_name)

    # QA rewrites scene content; code restores the saved style after all model passes.
    if image_style_catalog:
        for item in items_in_scope:
            if "black screen" in _normalize_cam_plan(item.get("camera_plan") or ""):
                continue
            frame_key = f"{item['scene_number']}:{item['shot_number']}:{item['shot_type']}"
            style_id = resolve_image_style_id(
                image_style_catalog, frame_key, item.get("locations") or [], item.get("style_id")
            )
            style_prompt = image_style_catalog["styles"][style_id]["prompt"].strip()
            core, roles = _split_saved_image_style(item.get("english_prompt") or "")
            literals = _frame_visible_text_literals(item.get("_shot_frame_spec") or {})
            prose = core
            for literal in literals:
                prose = prose.replace(literal, "")
            if project_language == "ru" and re.search(r"[A-Za-z]", prose) and not re.search(r"[А-Яа-яЁё]", prose):
                from utils import translate_prompts_in_items
                core = translate_prompts_in_items(
                    {"english_prompt": core}, project_language,
                    fields_to_translate=["english_prompt"],
                    locked_fragments_by_field={"english_prompt": literals},
                )["english_prompt"]
            label = "Стиль изображения" if re.search(r"[А-Яа-яЁё]", style_prompt) else "Image style"
            item["style_id"] = style_id
            item["english_prompt"] = f"{core}\n\n{label}: {style_prompt}" + (f"\n\n{roles}" if roles else "")

    # Ставим флаг только если реально обработали хотя бы одну сцену
    # M-27: per-scene coverage. Ведём prompts_validated_scenes (union уже валидированных
    # и только что обработанных). prompts_validated=True — ТОЛЬКО когда покрыты все
    # валидируемые сцены. prompts_validated_scope сохраняем как раньше.
    if report.get("scenes_processed", 0) > 0:
        from datetime import datetime, timezone

        existing_scenes = {
            _safe_int(x) for x in (shots_data.get("prompts_validated_scenes") or [])
        }
        existing_scenes.discard(-1)
        # Легаси: prompts_validated=True без scenes => считаем покрытыми все валидируемые.
        if shots_data.get("prompts_validated") and not shots_data.get("prompts_validated_scenes"):
            existing_scenes |= set(all_validatable_scenes)
        processed_for_coverage = set() if requested_shot_pairs is not None else {int(s) for s in _processed_scenes}
        validated_scenes = sorted(existing_scenes | processed_for_coverage)
        shots_data["prompts_validated_scenes"] = validated_scenes
        shots_data["prompts_validated"] = (
            shot_keys is None
            and bool(all_validatable_scenes)
            and set(all_validatable_scenes) <= set(validated_scenes)
        )
        shots_data["prompts_validated_at"] = datetime.now(timezone.utc).isoformat()
        shots_data["prompts_validated_tool"] = "shots_prompt_qa_tool"
        shots_data["prompts_validated_spec_version"] = _SHOT_FRAME_SPEC_VERSION
        shots_data["prompts_validated_temperature"] = temperature
        if shot_keys is not None:
            shots_data["prompts_validated_scope"] = "shot_keys:" + ",".join(
                str(x).strip() for x in (shot_keys or [])
            )
        elif scene_numbers is not None:
            try:
                shots_data["prompts_validated_scope"] = "scenes:" + ",".join(str(int(x)) for x in scene_numbers)
            except Exception:
                shots_data["prompts_validated_scope"] = "scenes:<invalid>"
        elif max_scenes is not None:
            shots_data["prompts_validated_scope"] = f"partial:{int(max_scenes)}"
        else:
            shots_data["prompts_validated_scope"] = "all"

    # Persist back to shots.json with a lock (to avoid race with other steps)
    if dry_run:
        # Возвращаем изменения в памяти, без записи на диск
        shots_data["items"] = items
        report["dry_run"] = True
        # For debugging/inspection in dry-run, include the report in the returned object.
        # Must remain JSON-serializable (no model objects).
        shots_data["_qa_report"] = report
        logger.info("🧪 shots_prompt_qa_tool: dry_run=True — пропускаем сохранение shots.json/report на диск")
    else:
        try:
            os.makedirs(os.path.dirname(shots_path), exist_ok=True)
            lock_path = f"{shots_path}.lock"
            with open(lock_path, "a+", encoding="utf-8") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    # Reload latest file content (if any) and merge items by key to avoid stomping
                    on_disk = _read_json(shots_path) or {}
                    # M-27: union per-scene coverage с тем, что уже на диске — не затираем
                    # сцены, провалидированные параллельными скоупами.
                    if (
                        shots_data.get("prompts_validated_scenes") is not None
                        or on_disk.get("prompts_validated_scenes")
                    ):
                        merged_scenes = {
                            _safe_int(x) for x in (shots_data.get("prompts_validated_scenes") or [])
                        } | {
                            _safe_int(x) for x in (on_disk.get("prompts_validated_scenes") or [])
                            if on_disk.get("prompts_validated_spec_version") == _SHOT_FRAME_SPEC_VERSION
                        }
                        merged_scenes.discard(-1)
                        shots_data["prompts_validated_scenes"] = sorted(merged_scenes)
                        shots_data["prompts_validated"] = (
                            bool(all_validatable_scenes)
                            and set(all_validatable_scenes) <= merged_scenes
                        )
                    on_disk_items = on_disk.get("items") or []
                    if on_disk_items:
                        # Build index from updated in-memory items
                        updated_index: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
                        for it in items:
                            try:
                                key = (int(it.get("scene_number")), int(it.get("shot_number")), str(it.get("shot_type")))
                                updated_index[key] = it
                            except Exception:
                                continue
                        merged_items = []
                        for it in on_disk_items:
                            try:
                                key = (int(it.get("scene_number")), int(it.get("shot_number")), str(it.get("shot_type")))
                            except Exception:
                                merged_items.append(it)
                                continue
                            in_memory_it = updated_index.get(key)
                            if in_memory_it is None:
                                merged_items.append(it)
                            else:
                                # Merge by field (not wholesale replace): start from the
                                # on-disk item so fields written concurrently by other
                                # steps (e.g. blockout paths) survive, then overlay ONLY
                                # the fields this tool itself can mutate
                                # (_QA_WRITABLE_ITEM_FIELDS). Overlaying the whole
                                # in-memory item would resurrect its stale copy of any
                                # field a concurrent writer updated after this tool's
                                # initial read (see docstring of _merge_write_shots in
                                # screenplay_shots_generator.py for the protocol).
                                merged = dict(it)
                                for field in _QA_WRITABLE_ITEM_FIELDS:
                                    if field in in_memory_it:
                                        merged[field] = in_memory_it[field]
                                merged_items.append(merged)
                        shots_data["items"] = merged_items
                    else:
                        shots_data["items"] = items

                    _write_json_atomic(shots_path, shots_data)
                    try:
                        _write_json_atomic(report_path, report)
                    except Exception as report_exc:
                        # shots.json (the critical write) already succeeded above;
                        # the report is a secondary artifact, so log and move on
                        # instead of feeding this into the shots.json failure path below.
                        logger.error(f"🧪 shots_prompt_qa_tool: не удалось сохранить report: {report_exc}")
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"🧪 shots_prompt_qa_tool: не удалось сохранить shots.json: {e}")
            raise RuntimeError(f"shots_prompt_qa_tool: не удалось сохранить shots.json: {e}") from e

    # NOTE: We intentionally do NOT validate reference_image_paths against the filesystem here.
    # At this stage there may be zero generated images on disk; existence-based filtering is invalid and destructive.

    logger.info(
        f"🧪 shots_prompt_qa_tool: scenes={report['scenes_processed']}, "
        f"suggested={report['repairs_suggested']}, applied={report['repairs_applied']}, rejected={report['repairs_rejected']}"
    )
    return shots_data
