import json
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
)

logger = logging.getLogger(__name__)

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
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


_VIDEO_JUDGE_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_CLOSEUP_ROOM_CHECK_CACHE: Dict[Tuple[str, str], List[str]] = {}


def _llm_check_closeup_room_details(video_prompt: str, camera_plan: str) -> List[str]:
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
    cache_key = (vp, cp)
    cached = _CLOSEUP_ROOM_CHECK_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    sys = (
        "You are a strict close-up framing validator for video prompts.\n"
        "Source of truth: camera_plan tells the framing tightness; video_prompt is the candidate text.\n"
        "Task: find visible room/venue/environment SETTING-DETAIL phrases that contradict close-up framing.\n"
        "Close-up framing means: subject fills the frame; background is acceptable ONLY as blur-hints "
        "(soft light, blurred shapes, bokeh, atmospheric haze). Naming furniture/architecture/audience/"
        "stage elements as visible parts of the frame breaks close-up framing.\n\n"
        "Allowed: blur-hints, atmospheric cues, light/shadow, color tones, generic background softness.\n"
        "Disallowed (examples, NOT a closed list): named seats/chairs/podium/stage/screen/curtains/"
        "chandeliers/auditorium/hall/venue/architecture/audience rows when described as visible elements.\n\n"
        "Return STRICT JSON: {\"violations\": [\"<offending phrase 1>\", ...]}.\n"
        "If no violations, return {\"violations\": []}.\n"
        "Each violation MUST be a verbatim substring copied from video_prompt (lowercased exact slice)."
    )
    user = json.dumps({"camera_plan": cp, "video_prompt": vp}, ensure_ascii=False)
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
        _CLOSEUP_ROOM_CHECK_CACHE[cache_key] = list(result)
        return result
    except Exception as e:
        logger.warning("close-up room-details LLM check failed: %s; skipping check", e)
        # Без хардкод-fallback: если LLM недоступен, эту проверку пропускаем.
        # Основная валидация video_prompt всё равно делается _llm_judge_video_prompt_lite.
        return []


def _detect_closeup_video_prompt_violations(video_prompt: str, camera_plan: str) -> List[str]:
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

    room_violations = _llm_check_closeup_room_details(vp, camera_plan or "")
    if room_violations:
        issues.append("contains_room_details_markers:" + ", ".join(room_violations[:8]))
    return issues


def _has_any_closeup_room_markers(video_prompt: str, camera_plan: str = "") -> bool:
    return bool(_llm_check_closeup_room_details(video_prompt, camera_plan))


def _llm_judge_video_prompt_lite(
    *,
    before_video_prompt: str,
    candidate_video_prompt: Optional[str],
    storyboard_description: str,
    storyboard_camera_plan: str,
    model_override: Optional[Any] = None,
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
        str(storyboard_camera_plan or "").strip() + "||" + str(storyboard_description or "").strip(),
    )
    if key in _VIDEO_JUDGE_CACHE:
        return _VIDEO_JUDGE_CACHE[key]

    model_obj = model_override or model_mapping.get("model_lite") or model_hard
    needs_close, needs_extreme = _requires_closeup_tokens(storyboard_camera_plan)

    # --- Layer A: Role + validation scope + source of truth ---
    sys = (
        "You are a strict consistency judge for video prompts.\n"
        "Source of truth: storyboard_description + camera_plan.\n"
        "Task: decide if BEFORE contradicts storyboard; evaluate CANDIDATE compliance; rewrite if non-compliant.\n"
        "Output: STRICT JSON object only. video_prompt = single-line English, no Cyrillic.\n\n"
        # --- Layer B: Step-by-step validation algorithm ---
        "## Evaluation algorithm\n"
        "1. Parse INPUT.mode:\n"
        "   - \"before_only\": evaluate BEFORE vs storyboard. Set contradiction_before. If true, output rewritten_candidate.\n"
        "   - \"candidate_eval\": evaluate both BEFORE and CANDIDATE. If candidate_ok=false, output rewritten_candidate.\n"
        "2. Extract 3-6 MUST_INCLUDE_FACTS from storyboard_description (identity, POV/anchor, key action, key objects, emotion/tone).\n"
        "3. BEFORE contradicts if it misses any must_include_fact OR contains incompatible facts (wrong place/anchor/roles/objects/action/timing, invented props/symbols/displays).\n"
        "4. Candidate_ok=true only if all must_include_facts satisfied, no incompatible additions.\n"
        "5. Rewrite structure: camera clause + framing token + primary subject + key action/timing + (optional) one blur-hint + tempo.\n\n"
        # --- Layer C: Violation types table + constraints ---
        "## Violation locks\n"
        "| Lock | Rule |\n"
        "|------|------|\n"
        "| PRIMARY SUBJECT | Must match storyboard_description focus. No subject switching. |\n"
        "| ENTITY IDENTITY | storyboard_description = sole ground truth for WHO/WHAT. Correct wrong identity. No new entities. Transliterate Cyrillic names to Latin. No generic species labels unless storyboard uses them. No inventing species. |\n"
        "| DIRECTION | Preserve EXACT movement direction from storyboard. Downward cues -> downward verbs only. Upward cues -> upward verbs only. Never reverse. |\n"
        "| TIMING/MOMENT | Preserve EXACT timing. Contact cues (touches/makes contact) -> contact moment only. Pre-contact cues (above/hovering) -> pre-contact state only. No phase shifting. |\n"
        "| KEY ACTION | Reflect key action and timing from storyboard. No drift, no time shift. |\n"
        "| POV/ANCHOR PHRASE | Keep explicit POV/anchor phrases. Translate to English. No anchor substitution (e.g., bleachers!=podium). |\n"
        "| CLOSE-UP FRAMING | Tight framing on specified subject. Environment = blurred light/shapes only. No room/venue detail. |\n"
        "| REACTION/GAZE | No forced \"looking at camera\" for reactions unless storyboard says so. Prefer off-screen/sideways gaze. |\n\n"
        "## Additional rules\n"
        "- No placeholders (\"SUBJECT FACE\"/\"SUBJECT CLAW\"); use actual subject phrase from storyboard.\n"
        "- No absence-assertions (\"no X\"/\"without X\") unless storyboard explicitly requires absence.\n"
        "- Describe transition/micro-dynamics, not static restatement.\n"
        "- No invented story events. No raw newlines in strings.\n"
        "- Anchor glossary: \u0442\u0440\u0438\u0431\u0443\u043d\u0430->podium/lectern, \u043c\u0438\u043a\u0440\u043e\u0444\u043e\u043d->microphone, \u043c\u043e\u043d\u0438\u0442\u043e\u0440->monitor, \u0430\u043d\u0435\u0441\u0442\u0435\u0437\u0438\u043e\u043b\u043e\u0433->anesthesiologist, \u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043d\u0442->assistant.\n"
        "- Pattern \"\u0432\u0438\u0434 \u0438\u0437-\u0437\u0430 <ANCHOR>\"/\"\u0443 <ANCHOR>\"/\"\u0437\u0430 <ANCHOR>\" -> translate <ANCHOR> to English and include it.\n"
    )
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
    _VIDEO_JUDGE_CACHE[key] = out
    return out


_ENGLISH_SET_JUDGE_CACHE: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}

_END_ENGLISH_JUDGE_CACHE: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}


def _llm_judge_end_english_prompt_candidate(
    *,
    before_english_prompt: str,
    candidate_english_prompt: str,
    storyboard_description: str,
    storyboard_camera_plan: str,
    transition_video_prompt: str,
    model_override: Any = None,
) -> Dict[str, Any]:
    """
    Safety+repair judge for END english_prompt edits.
    Prevents: semantic subject substitution, object invention, incorrect shot-size forcing.
    Allows: END framing changes ONLY when transition_video_prompt implies camera movement.

    Returns:
      {"candidate_ok": bool, "rewritten_candidate": str|None, "reason": str}
    """
    key = (
        (before_english_prompt or "").strip(),
        (candidate_english_prompt or "").strip(),
        (storyboard_camera_plan or "").strip(),
        (storyboard_description or "").strip(),
        (transition_video_prompt or "").strip(),
    )
    if key in _END_ENGLISH_JUDGE_CACHE:
        return _END_ENGLISH_JUDGE_CACHE[key]

    model_obj = model_override or model_hard
    # --- Layer A: Role + validation scope ---
    sys = (
        "You are a strict validator+rewriter for END english_prompt.\n"
        "Source of truth: storyboard_description (primary subject, objects), camera_plan (base framing), transition_video_prompt (camera movement).\n"
        "Output: STRICT JSON only.\n\n"
        # --- Layer B: Validation steps ---
        "## Validation algorithm\n"
        "1. Identify PRIMARY SUBJECT from storyboard_description. Candidate must not substitute it.\n"
        "2. Check NO-INVENTION: no new props/objects/symbols not in storyboard_description. Generic background extras allowed only if framing widens due to camera movement.\n"
        "3. Check framing: camera_plan is an ANCHOR (base), not absolute lock for END.\n"
        "   - END may change framing ONLY if transition_video_prompt implies camera movement (zoom/dolly/push-in/pull-back).\n"
        "   - If no camera movement implied, candidate must not contradict camera_plan.\n"
        "4. Keep english_prompt as single line (no raw newlines). Preserve reference roles block if present.\n"
        "5. If candidate fails, provide rewritten_candidate.\n\n"
        # --- Layer C: Output format ---
        "Return JSON: {\"candidate_ok\": true|false, \"rewritten_candidate\": \"...\"|null, \"reason\": \"...\"}\n"
    )
    payload = {
        "storyboard_camera_plan": storyboard_camera_plan or "",
        "storyboard_description": storyboard_description or "",
        "transition_video_prompt": transition_video_prompt or "",
        "before_english_prompt": before_english_prompt or "",
        "candidate_english_prompt": candidate_english_prompt or "",
    }
    resp = call_openai_api(
        prompt="INPUT:\n" + json.dumps(payload, ensure_ascii=False),
        system_prompt=sys,
        model=model_obj,
        max_tokens=2500,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    obj = _extract_json_object(resp) or {}
    out = {
        "candidate_ok": bool(obj.get("candidate_ok")),
        "rewritten_candidate": obj.get("rewritten_candidate") if isinstance(obj.get("rewritten_candidate"), str) else None,
        "reason": str(obj.get("reason") or ""),
    }
    if out["rewritten_candidate"] is not None:
        out["rewritten_candidate"] = out["rewritten_candidate"].replace("\n", " ").strip()
    _END_ENGLISH_JUDGE_CACHE[key] = out
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
    if key in _ENGLISH_SET_JUDGE_CACHE:
        return _ENGLISH_SET_JUDGE_CACHE[key]

    before_has_cyr = bool(re.search(r"[А-Яа-яЁё]", before_english_prompt or ""))
    after_has_cyr = bool(re.search(r"[А-Яа-яЁё]", after_english_prompt or ""))
    if before_has_cyr != after_has_cyr:
        out = {
            "ok": False,
            "reason": "language_changed_between_before_and_after_english_prompt",
        }
        _ENGLISH_SET_JUDGE_CACHE[key] = out
        return out

    model_obj = model_override or model_hard
    sys = (
        "You are a strict validator for a proposed full replacement of english_prompt.\n"
        "Decide whether AFTER is safe to apply given storyboard (camera_plan + description).\n"
        "Output STRICT JSON only.\n\n"
        "Rules:\n"
        "- Do NOT allow changing the PRIMARY SUBJECT of the shot (infer from storyboard.description).\n"
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
    _ENGLISH_SET_JUDGE_CACHE[key] = out
    return out


def _apply_repairs_to_items(
    items: List[Dict[str, Any]],
    repairs: List[Dict[str, Any]],
    storyboard_lookup: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    Applies repairs in-place to items. Returns (applied_count, rejected_repairs)
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

    allowed_fields = {
        "english_prompt",
        "video_prompt",
        "negative_prompt",
        "reference_image_paths",
        "characters",
        "locations",
    }

    def _video_prompt_contradiction(
        vp: str,
        storyboard_desc: str,
        storyboard_camera_plan: str,
    ) -> bool:
        """
        Universal: allow START video_prompt edits only when it clearly contradicts storyboard.
        """
        verdict = _llm_judge_video_prompt_lite(
            before_video_prompt=vp or "",
            candidate_video_prompt=None,
            storyboard_description=storyboard_desc or "",
            storyboard_camera_plan=storyboard_camera_plan or "",
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
            rep_reasons = rep.get("reasons") or []
            has_reasons = isinstance(rep_reasons, list) and len(rep_reasons) > 0

            # Apply only allowed fields
            changed_any = False
            field_block_reasons: List[str] = []
            # Ensure stable application order so that changes to characters/locations
            # can unlock reference_image_paths additions in the same repair.
            preferred_order = [
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
                # ВАЖНО: video_prompt у END кадров НЕ редактируем (только START формирует/меняет video_prompt)
                if k == "video_prompt" and str(shot_type).strip().lower() == "end":
                    continue
                # END english_prompt guardrail: subject/objects/framing must align with storyboard + transition video_prompt.
                if k == "english_prompt" and str(shot_type).strip().lower() == "end":
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
                    cand = str(v or "")
                    if cand and desc:
                        verdict = _llm_judge_end_english_prompt_candidate(
                            before_english_prompt=before_ep,
                            candidate_english_prompt=cand,
                            storyboard_description=desc,
                            storyboard_camera_plan=cam_plan,
                            transition_video_prompt=transition_vp,
                            model_override=model_hard,
                        )
                        if verdict.get("candidate_ok") is True:
                            pass
                        else:
                            rewritten = verdict.get("rewritten_candidate")
                            if isinstance(rewritten, str) and rewritten.strip():
                                v = rewritten.strip()
                            else:
                                field_block_reasons.append("english_prompt_end_blocked:judge_failed_no_rewrite")
                                continue
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
                        if _detect_closeup_video_prompt_violations(before_vp, cam_plan):
                            pass
                        else:
                            if not _video_prompt_contradiction(before_vp, desc, cam_plan):
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
                    )
                    # Extra deterministic guard: enforce explicit framing tokens.
                    needs_close, needs_extreme = _requires_closeup_tokens(cam_plan or "")
                    cand_l = str(v or "").lower()
                    if needs_extreme and not any(t in cand_l for t in ["extreme close-up", "extreme close up"]):
                        judge["candidate_ok"] = False
                    if needs_close and ("close-up" not in cand_l and "close up" not in cand_l):
                        judge["candidate_ok"] = False
                    # If close-up plan: room markers are forbidden; use LLM-based check (in addition to LLM judge).
                    if needs_close and _has_any_closeup_room_markers(str(v or ""), cam_plan or ""):
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
                            if needs_close and _has_any_closeup_room_markers(cand2, cam_plan or ""):
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
                    if needs_close and _has_any_closeup_room_markers(str(v or ""), cam_plan or ""):
                        field_block_reasons.append("video_prompt_blocked:contains_room_markers")
                        continue
                    # Universal hard rule: video_prompt must not contain Cyrillic. If it does, try to rewrite via judge.
                    if re.search(r"[А-Яа-яЁё]", str(v or "")):
                        judge2 = _llm_judge_video_prompt_lite(
                            before_video_prompt=before_vp or "",
                            candidate_video_prompt=str(v or ""),
                            storyboard_description=desc or "",
                            storyboard_camera_plan=cam_plan or "",
                        )
                        rewritten2 = judge2.get("rewritten_candidate")
                        if isinstance(rewritten2, str) and rewritten2.strip() and not re.search(r"[А-Яа-яЁё]", rewritten2):
                            v = rewritten2.strip()
                        else:
                            field_block_reasons.append("video_prompt_blocked:contains_cyrillic")
                            continue
                if v is None:
                    continue
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
                # - Allow ONLY SAFE additions: character/location refs already declared in this item
                #   (characters[].reference_image_path / locations[].reference_image_path).
                # Rationale: at QA stage we must not invent new filesystem paths, but we CAN restore
                # canonical references that are part of the shot metadata.
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

                        # Build allowlist of safe refs we can re-add (from declared shot metadata)
                        allow_add: set = set()
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

1. For each generated prompt, verify alignment with its storyboard.description and camera_plan.
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
| English video_prompt | START video_prompt must be English-only, no Cyrillic. |
| PRIMARY SUBJECT | Do NOT change the primary subject of any shot (from storyboard.description). |
| ACTION BOUNDARY | Do NOT move actions forward/backward across neighboring shots. No stealing, advancing, or rewinding actions. |
| START/END states | START = T=0 static (no dynamic verbs). END = T=final stop-frame (no process verbs). |
| DIRECTION LOCK | Preserve EXACT movement direction from storyboard. Never reverse. |
| TIMING/MOMENT LOCK | Preserve EXACT timing. Contact = contact, not pre-contact. No phase shifting. |
| SCENE vs SHOT TRUTH | storyboard.description = ground truth. Do NOT spray scene.action props across shots. Remove foreign concrete objects. |
| STAGE GEOMETRY | Seats face stage. Podium on stage side. Physically consistent layout. |
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
    force: bool = False,
    global_max_repairs: int = 0,
    dry_run: bool = False,
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
        force: If True, ignores prompts_validated flag and runs validation again.
        global_max_repairs: Max number of global screenplay-level patch repairs to apply at the end.
            По умолчанию отключено, чтобы не добавлять второй поздний LLM-rewrite поверх scene-level QA.
        dry_run: If True, runs validation/repair but does NOT write shots.json/report to disk.

    Returns:
        Updated shots_data dict (items.json compatible). Also writes updated shots.json and a QA report to disk.
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

    screenplay_path = f"plots/storybooks/{project_id}/91_screenplay/screenplay.json"
    shots_path = f"plots/storybooks/{project_id}/97_shots/shots.json"
    report_path = f"plots/storybooks/{project_id}/97_shots/shots_prompt_qa_report.json"

    if shots_data is None:
        shots_data = _read_json(shots_path) or {"items": [], "consistency_rules": []}

    # Skip повторную валидацию, если уже провалидировано (если не force)
    if not force:
        already_validated = bool(shots_data.get("prompts_validated"))
        if not already_validated and os.path.exists(shots_path):
            try:
                on_disk_meta = _read_json(shots_path) or {}
                already_validated = bool(on_disk_meta.get("prompts_validated"))
            except Exception:
                already_validated = False

        if already_validated:
            logger.info("🧪 shots_prompt_qa_tool: пропуск — shots.json уже помечен как prompts_validated=true")
            return shots_data

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
            }

    if not items:
        logger.info("🧪 shots_prompt_qa_tool: shots_data.items пустой, нечего валидировать")
        return shots_data

    scenes_by_number: Dict[int, Dict[str, Any]] = {}
    for sc in screenplay_scenes:
        try:
            scenes_by_number[int(sc.get("scene_number", 0))] = sc
        except Exception:
            continue

    items_by_scene: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        try:
            sn = int(it.get("scene_number"))
        except Exception:
            continue
        items_by_scene.setdefault(sn, []).append(it)

    system_prompt = """You are a strict validator and repair tool for STORYBOARD SHOT PROMPTS.

## Layer A: Role + scope + source of truth

You receive ONE SCENE from the screenplay + all GENERATED SHOTS (start/end) for that scene.
Task: detect contradictions between generated prompts and the screenplay, then REPAIR prompts to match.

Source of truth hierarchy (highest to lowest):
1. shot_frame_spec (per-shot authoritative spec: primary_subject, must_show, must_not_show, visible_characters, start/end/transition specs)
2. storyboard.description (what must be visible in this shot)
3. storyboard.camera_plan (base framing anchor)
4. scene.action + scene_continuity_facts (broader context, NOT a license to inject extra props into every shot)

## Layer B: Validation algorithm

### Step 1: Output format enforcement
- Output MUST be a single JSON object (no markdown). Strictly valid JSON.
- No raw newline characters inside strings (use "\n" if needed). Prefer single-line strings.
- If you propose a fix for english_prompt/video_prompt, return FULL FINAL CONTENT. No fragments, no ellipses, no truncation.
- Keep existing "roles of reference images" blocks in english_prompt unless directly contradictory.

### Step 2: Per-shot validation against shot_frame_spec
If shot_frame_spec exists for a shot, validate against it:
- primary_subject defines the visual focus. Do NOT swap subjects.
- must_show = mandatory visible facts; repair if missing from prompts.
- must_not_show = forbidden facts; repair if present in prompts.
- visible_characters = allowed visible cast.
- start_state_spec -> validate START prompt only. end_state_spec -> validate END prompt only. transition_spec -> validate START video_prompt.
- facial_expression / gaze_direction / pose_signature / character_pose_signatures: treat as authoritative. Do NOT reset to neutral or "looking at camera" unless spec says so.
- transition_spec.affect_delta: preserve facial/gaze/emotional delta in video_prompt.
- world_physics: respect support_state, surface_state, stability, contact_constraints, occlusion_constraints, forbidden_implications.

### Step 3: Per-shot validation against storyboard
For each shot, check:
1. Location/time matches scene (no wrong room/hall).
2. Staging/zone consistency (stage/podium/aisle placement per screenplay).
3. Camera plan consistency (shot size/framing not contradicted).
4. Language: english_prompt stays in its current language; video_prompt = English only, no Cyrillic; negative_prompt stays in its current language.
5. START = T=0 static state (no dynamic verbs). END = T=final stop-frame (no process verbs like "being", "moving", "starts to").
6. Continuity: avoid changing extras/decor unless screenplay explicitly changes them.
7. Multi-view location sheets: output ONE continuous frame (no collage/grid/multi-panel) unless storyboard explicitly requires split-screen.
8. reference_image_paths: change only when strictly necessary. For END shots, continuity start frame reference (img_final_start_*.png) MUST remain.

### Step 4: Cross-validation rules
Apply these violation checks in order. Each is a HARD CONTRADICTION requiring repair:

## Layer C: Violation types (compact reference)

### Identity & subject locks
| Lock | Rule |
|------|------|
| PRIMARY SUBJECT | Defined by storyboard.description. No swapping to another entity/object from another shot/scene. |
| ENTITY IDENTITY | storyboard.description = sole ground truth for WHO/WHAT. Correct wrong identity. No new named entities. Transliterate Cyrillic to Latin for video_prompt. No generic species labels unless storyboard uses them. |
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
| CLOSE-UP FRAMING | If camera_plan is CLOSE UP/EXTREME CLOSE UP: video_prompt MUST contain "close-up"/"close up" (and "extreme close-up" if extreme). Tight framing on primary subject only. Remove room/venue establishing descriptions. One short blur-hint max. |
| SPLIT SCREEN | If camera_plan includes SPLIT SCREEN: video_prompt MUST mention "split-screen" explicitly. |
| END FRAMING vs CAMERA MOVEMENT | camera_plan = BASE/ANCHOR for START. END may differ ONLY if START video_prompt implies camera movement (dolly/zoom/push-in/pull-back). Validate END framing against both camera_plan and transition_video_prompt. |
| END CROP + SUPPORT | If END final_shot_size is close-up/medium close-up, do NOT describe full head-to-toe figure unless storyboard requires it. No inset panels, cutaway strips, or split-frame layouts for edge fragments. |

### POV & gaze locks
| Lock | Rule |
|------|------|
| POV/ANCHOR PHRASE | Keep explicit POV/anchor phrases from storyboard. Translate to English for video_prompt. No substitution (bleachers != podium). |
| POV CHARACTER REF | For POV shots: OK to show no body. If any body parts visible, they MUST match POV character identity. Add missing POV character reference_image_paths. |
| OVER-SHOULDER IDENTITY | Visible back/shoulder/head silhouette MUST match named character identity. |
| REACTION/GAZE | No forced "looking at camera" for reaction shots unless storyboard says so. Prefer off-screen/sideways gaze. |

### Spatial & physics locks
| Lock | Rule |
|------|------|
| STAGE/AUDITORIUM GEOMETRY | Seats face stage. Podium on stage side. Do NOT place rows behind speaker when camera faces stage. Consistent physical layout. |
| MICROPHONE/PODIUM ANCHOR | Hand/claw touching mic = podium/lectern mic on stage side. Primary focus on contact. Environment = generic blur hint. |
| AUDIENCE REACTION ZONE | Audience members positioned in seating area, NOT on stage (unless storyboard says so). Podium/mic = distant background anchor only. |

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
| MOVING PROP | START and END must show DIFFERENT positions for moving objects. |
| CONTINUITY PROPS | Do NOT introduce new props/furniture not in Image 1 continuity reference. |

### Translation note
- video_prompt = English only. Translate anchor nouns from Russian correctly.
- Glossary: трибуна->podium/lectern, микрофон->microphone, монитор->monitor, анестезиолог->anesthesiologist, ассистент->assistant.

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
        "english_prompt": <string, optional>,
        "video_prompt": <string, optional>,
        "negative_prompt": <string, optional>,
        "reference_image_paths": <array of strings, optional>,
        "characters": <array, optional>,
        "locations": <array, optional>
      }
    }
  ],
  "notes": <string>
}

If there are no issues, return {"repairs": [], "notes": "ok"}.
"""

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

    scene_numbers_all = sorted(set(items_by_scene.keys()) & set(scenes_by_number.keys()))
    if scene_numbers is not None:
        try:
            allow = {int(x) for x in (scene_numbers or [])}
        except Exception:
            allow = set()
        scene_numbers_all = [sn for sn in scene_numbers_all if sn in allow]
    if max_scenes is not None:
        scene_numbers_all = scene_numbers_all[: max(0, int(max_scenes))]

    # ------------------------------------------------------------------
    # Параллельная обработка сцен через ThreadPoolExecutor
    # ------------------------------------------------------------------
    _items_lock = threading.Lock()
    _report_lock = threading.Lock()

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

        shot_frame_specs: Dict[int, Dict[str, Any]] = {}
        for it0 in scene_items:
            try:
                shot_num_i = int(it0.get("shot_number", 0))
            except Exception:
                continue
            embedded_spec = it0.get("_shot_frame_spec")
            if isinstance(embedded_spec, dict) and embedded_spec:
                shot_frame_specs.setdefault(shot_num_i, embedded_spec)
        if not shot_frame_specs:
            logger.warning(
                "🧪 shots_prompt_qa_tool: scene %s missing embedded shot_frame_spec; rebuilding for legacy artifact",
                scene_number,
            )
            for sh in (scene.get("storyboard", []) or []):
                try:
                    shot_num_i = int(sh.get("shot_number", 0))
                except Exception:
                    continue
                shot_frame_spec = _extract_shot_frame_spec_llm(
                    project_id=project_id,
                    scene_number=scene_number,
                    shot_number=shot_num_i,
                    shot_description=str(sh.get("description", "") or ""),
                    camera_plan=str(sh.get("camera_plan", "") or ""),
                    scene_action=str(scene.get("action", "") or ""),
                    scene_characters=list(scene.get("characters") or []),
                    location_time=str(scene.get("location_time", "") or ""),
                    location_canon_name=str((sh.get("location_canon_name") or scene.get("location_canon_name") or "")).strip(),
                    scene_continuity_facts=scene_continuity_facts,
                )
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
        for it in sorted(scene_items, key=lambda x: (int(x.get("shot_number", 0)), str(x.get("shot_type", "")))):
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
                    issues = _detect_closeup_video_prompt_violations(vp, cp)
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

            generated_shots.append(
                {
                    "shot_number": it.get("shot_number"),
                    "shot_type": it.get("shot_type"),
                    "camera_plan": it.get("camera_plan"),
                    "storyboard_camera_plan": sb_cam_plan,
                    "storyboard_description": sb_desc,
                    "shot_frame_spec": shot_frame_specs.get(shn_i, {}),
                    "transition_video_prompt": transition_vp,
                    "timing": it.get("timing"),
                    "english_prompt": it.get("english_prompt"),
                    "video_prompt": it.get("video_prompt"),
                    "negative_prompt": it.get("negative_prompt"),
                    "reference_image_paths": it.get("reference_image_paths"),
                }
            )

        payload = {
            "scene_number": scene_number,
            "location_time": scene.get("location_time"),
            "location_canon_name": scene.get("location_canon_name"),
            "action": scene.get("action"),
            "characters": scene.get("characters"),
            "scene_continuity_facts": scene_continuity_facts,
            "shot_frame_specs": shot_frame_specs,
            "storyboard": storyboard,
            "generated_shots": generated_shots,
            "detected_violations": detected_violations,
        }
        payload_str = json.dumps(payload, ensure_ascii=False)
        logger.info(f"🧪 shots_prompt_qa_tool: scene {scene_number} payload_chars={len(payload_str)}")

        violations_note = ""
        if detected_violations:
            violations_note = (
                "IMPORTANT: INPUT.detected_violations is authoritative.\n"
                "- If detected_violations is non-empty, you MUST return repairs entries for those shots.\n"
                "- You are NOT allowed to return repairs=[] / notes='ok' while detected_violations is non-empty.\n"
                "- For each detected_violations entry, you MUST output set.video_prompt with the FULL corrected final single-line English video_prompt.\n"
                "\n"
            )
        user_prompt = (
            "Validate and repair generated shots prompts for this scene.\n"
            "Return JSON patches ONLY where contradictions exist.\n\n"
            f"{violations_note}"
            f"INPUT:\n{payload_str}"
        )

        obj = None
        last_resp_preview = ""
        for attempt in range(1, 4):
            resp = call_openai_api(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=model_obj,
                max_tokens=40000,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            last_resp_preview = _safe_preview(resp, limit=600)
            obj = _extract_json_object(resp)
            if obj:
                # If we provided detected_violations but model returned no repairs, force a retry with stricter wording.
                repairs_try = obj.get("repairs") or []
                if detected_violations and (not isinstance(repairs_try, list) or len(repairs_try) == 0):
                    logger.warning(
                        f"🧪 shots_prompt_qa_tool: scene {scene_number} violations_present but repairs=[] on attempt {attempt}/3; retrying"
                    )
                    obj = None
                else:
                    break
            logger.warning(
                f"🧪 shots_prompt_qa_tool: scene {scene_number} invalid JSON on attempt {attempt}/3; "
                f"resp_preview='{last_resp_preview}'"
            )
            # tighten prompt for retries
            user_prompt = (
                "Return STRICT JSON ONLY (no markdown, no prose). "
                "All strings must be single-line (no raw newlines). "
                "If you need a line break, use \\n.\n\n"
                "CRITICAL: If INPUT.detected_violations is non-empty, you MUST return repairs for those shots. "
                "Do NOT return repairs=[] / ok.\n\n"
                f"{violations_note}"
                f"INPUT:\n{payload_str}"
            )

        if not obj:
            logger.warning(f"🧪 shots_prompt_qa_tool: scene {scene_number} skip (still invalid JSON)")
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

        # Thread-safe: применяем ремонты с блокировкой
        with _items_lock:
            applied_count, rejected = _apply_repairs_to_items(items, repairs, storyboard_lookup=storyboard_lookup)

        with _report_lock:
            report["scenes_processed"] += 1
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

    # ------------------------------------------------------------
    # FINAL GUARDRAIL: screenplay-level pass across ALL prompts
    # ------------------------------------------------------------
    try:
        max_repairs = int(global_max_repairs) if global_max_repairs is not None else 25
        max_repairs = max(0, min(max_repairs, 100))
    except Exception:
        max_repairs = 25

    if max_repairs > 0 and report.get("scenes_processed", 0) > 0:
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

    # Ставим флаг только если реально обработали хотя бы одну сцену
    if report.get("scenes_processed", 0) > 0:
        from datetime import datetime, timezone

        shots_data["prompts_validated"] = True
        shots_data["prompts_validated_at"] = datetime.now(timezone.utc).isoformat()
        shots_data["prompts_validated_tool"] = "shots_prompt_qa_tool"
        shots_data["prompts_validated_temperature"] = temperature
        if scene_numbers is not None:
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
            with open(shots_path, "a+", encoding="utf-8") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                # Reload latest file content (if any) and merge items by key to avoid stomping
                on_disk = _read_json(shots_path) or {}
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
                        merged_items.append(updated_index.get(key, it))
                    shots_data["items"] = merged_items
                else:
                    shots_data["items"] = items

                _write_json_atomic(shots_path, shots_data)
                _write_json_atomic(report_path, report)
                fcntl.flock(lock_f, fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"🧪 shots_prompt_qa_tool: не удалось сохранить shots.json/report: {e}")

    # NOTE: We intentionally do NOT validate reference_image_paths against the filesystem here.
    # At this stage there may be zero generated images on disk; existence-based filtering is invalid and destructive.

    logger.info(
        f"🧪 shots_prompt_qa_tool: scenes={report['scenes_processed']}, "
        f"suggested={report['repairs_suggested']}, applied={report['repairs_applied']}, rejected={report['repairs_rejected']}"
    )
    return shots_data
