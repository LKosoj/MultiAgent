"""Общие правила видеопромпта: длина, одно событие, камера, темп и якоря формы предметов."""
import re
from typing import Any, Dict, Iterable, List, Optional

# Длинные промпты (250+ слов) провоцируют модель добавлять лишние предметы и события.
VIDEO_PROMPT_MAX_WORDS = 150
STATIC_CAMERA_SENTENCE = "Locked-off tripod shot, fixed frame."
NATURAL_TEMPO = "real-time, natural speed"

VIDEO_PROMPT_RULES = (
    f"- Length: 80-{VIDEO_PROMPT_MAX_WORDS} words; a simple static insert may be shorter. "
    f"Never exceed {VIDEO_PROMPT_MAX_WORDS} words.\n"
    "- Write instructions in English and character names with Latin letters. Preserve source-visible writing verbatim; "
    "spoken dialogue is not visible writing, so describe the speech action in English instead of quoting its source-language line.\n"
    "- One clip shows the complete source-authored causal sequence of this shot, including its required reactions. Do not add unrelated events, objects or characters.\n"
    "- START image restrictions and t0_mode apply only at T=0. The video must reach the required END state within the clip; shortening prose must preserve all authored events.\n"
    "- Give the timeline in seconds within the clip duration, e.g. \"0-2s: ...; 2-4s: ...\".\n"
    "- Supplied duration_s is the actual clip duration; nominal screenplay timing and stale timestamps do not extend it. "
    "Achieve every authored event and the required END state within that duration.\n"
    f"- State the speed in words: \"{NATURAL_TEMPO}\", unless slow motion or a time-lapse is required.\n"
    f"- The first sentence is the camera. For a stationary camera write exactly \"{STATIC_CAMERA_SENTENCE}\" "
    "(in English) as the first sentence. Describe the camera positively: never as a negation "
    "such as \"camera does not move\" or \"no camera movement\".\n"
)

_STATIC_CAMERA = re.compile(
    r"\b(static|stationary|locked[- ]off|locks off|fixed (?:camera|frame)|tripod)\b", re.IGNORECASE)
_NEGATED_CAMERA = re.compile(
    r"\bcamera\s+(?:does\s+not|doesn't|does\s+n't|never|will\s+not|won't)\s+(?:move|pan|tilt|shake|zoom|drift)"
    r"|\b(?:no|without)\s+camera\s+(?:movement|motion|moves|shake)\b",
    re.IGNORECASE,
)


def video_prompt_word_count(prompt: str) -> int:
    return len(str(prompt or "").split())


def video_prompt_violations(prompt: str, duration_s: Optional[float] = None) -> List[str]:
    """Формальные нарушения правил видеопромпта для ремонта в QA."""
    issues = []
    words = video_prompt_word_count(prompt)
    if words > VIDEO_PROMPT_MAX_WORDS:
        issues.append(f"video_prompt_too_long:{words}_words_max_{VIDEO_PROMPT_MAX_WORDS}")
    if _NEGATED_CAMERA.search(str(prompt or "")):
        issues.append("camera_phrased_as_negation")
    if duration_s is not None:
        ends = re.findall(r"\b\d+(?:\.\d+)?\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:s|seconds?)\b",
                          str(prompt or ""), re.IGNORECASE)
        last_mark = max((float(value) for value in ends), default=0.0)
        # Rounding a timestamp to hundredths can add up to half a centisecond.
        if last_mark > duration_s + 0.005:
            issues.append(f"video_prompt_time_exceeds_duration:{last_mark:g}s_max_{duration_s:g}s")
    return issues


def is_static_camera(prompt: str) -> bool:
    """Статичность определяется только по первой фразе, где по правилам описана камера."""
    camera_clause = re.split(r"[.;:]", str(prompt or "").strip(), maxsplit=1)[0]
    return bool(_STATIC_CAMERA.search(camera_clause))


def shape_anchors(items: Iterable[Dict[str, Any]]) -> List[str]:
    """Канонические формы видимых в кадре предметов из плана непрерывности."""
    anchors: Dict[str, str] = {}
    for item in items:
        for context in ((item or {}).get("_continuity") or {}).values():
            states = (context or {}).get("states") or {}
            for entity in (context or {}).get("entities") or []:
                shape = str(entity.get("shape") or "").strip().rstrip(".")
                if shape and states.get(entity.get("id"), {}).get("visible", True):
                    anchors.setdefault(entity["id"], shape)
    return list(anchors.values())


def finalize_video_prompt(prompt: str, items: Iterable[Dict[str, Any]] = ()) -> str:
    """Детерминированно ставит фразу статичной камеры первой и дописывает якоря формы."""
    text = str(prompt or "").strip()
    if not text:
        return text
    if is_static_camera(text) and not text.startswith(STATIC_CAMERA_SENTENCE):
        text = f"{STATIC_CAMERA_SENTENCE} {text}"
    anchors = [shape for shape in shape_anchors(items) if shape.lower() not in text.lower()]
    if anchors:
        text = f"{text.rstrip()} Shape lock: {'; '.join(anchors)}."
    return text
