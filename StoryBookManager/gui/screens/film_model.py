"""Чистая модель экрана «Фильм»: масштаб, линейка, позиция плеера (без tkinter)."""

import math

from .common_model import format_bytes, format_created_at, format_timecode
from .film_texts import FILM_TEXTS

TICK_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300)
TICK_MIN_PX = 60
MAX_SCALE_PX_PER_S = 64
MAX_TOTAL_WIDTH_PX = 30000
ZOOM_FACTOR = 1.5
BUSY_RUN_STATES = ("running", "pause_requested", "paused", "stopping")


def _busy(state) -> bool:
    return bool(state) and state in BUSY_RUN_STATES


def tick_step(px_per_s):
    for step in TICK_STEPS:
        if step * px_per_s >= TICK_MIN_PX:
            return step
    return TICK_STEPS[-1]


def fit_scale(view_width_px, duration_s):
    return view_width_px / duration_s if duration_s > 0 and view_width_px > 0 else 1


def clamp_scale(px_per_s, fit, duration_s):
    cap = MAX_TOTAL_WIDTH_PX / duration_s if duration_s > 0 else MAX_SCALE_PX_PER_S
    hi = max(fit, min(MAX_SCALE_PX_PER_S, cap))
    return min(hi, max(fit, px_per_s))


def zoom_in(px_per_s, fit, duration_s):
    return clamp_scale(px_per_s * ZOOM_FACTOR, fit, duration_s)


def zoom_out(px_per_s, fit, duration_s):
    return clamp_scale(px_per_s / ZOOM_FACTOR, fit, duration_s)


def ruler_ticks(duration_s, px_per_s):
    step = tick_step(px_per_s)
    out, t = [], 0
    while t <= duration_s:
        out.append({"t": t, "label": format_timecode(t)})
        t += step
    return out


def run_transition(prev, next_state):
    """Перечитать экран, когда прогон закончился; тост только после успешного конца."""
    if not _busy(prev) or _busy(next_state):
        return {"reload": False, "toast": None}
    return {"reload": True, "toast": "done" if next_state == "completed" else None}


def clamp_time(t, duration):
    top = duration if duration > 0 else 0
    try:
        value = float(t)
    except (TypeError, ValueError):
        value = 0.0
    return min(top, max(0, value))


def time_from_x(x, scroll_left, px_per_s, duration):
    return clamp_time((x + scroll_left) / px_per_s, duration) if px_per_s > 0 else 0


def format_position(current, duration):
    return f"{format_timecode(current)} / {format_timecode(duration)}"


# ---- раскладка дорожек (те же правила, что в filmView.ts) ----

TRACK_ORDER = ("shots", "narration", "music", "sfx", "subtitles")
TRACK_TITLES = {
    "shots": FILM_TEXTS["track_shots"], "narration": FILM_TEXTS["track_narration"], "music": FILM_TEXTS["track_music"],
    "sfx": FILM_TEXTS["track_sfx"], "subtitles": FILM_TEXTS["track_subtitles"],
}
LABEL_MIN_PX = 28
_PLACEMENT_TEXT = {"manifest": "src_manifest", "scene": "src_scene", "film": "src_film"}


def _round3(n):
    return math.floor(n * 1000 + 0.5) / 1000


def _tc(a, b):
    return f"{format_timecode(a)} – {format_timecode(b)}"


def placement_text(placement):
    return FILM_TEXTS[_PLACEMENT_TEXT[placement]]


def film_duration(timeline, cues=()):
    cut = timeline.get("duration_s")
    if cut is not None and cut > 0:
        return cut
    video = (timeline.get("final") or {}).get("video") or {}
    from_file = video.get("duration_s")
    if from_file is not None and from_file > 0:
        return from_file
    return max([0, *(c["end_s"] for c in cues)])


def _shot_blocks(timeline):
    warn_by_key = {}
    for w in timeline.get("warnings") or []:
        if w.get("key"):
            warn_by_key.setdefault(w["key"], []).append(w["message"])
    blocks, out_of_cut = [], 0
    for s in timeline["shots"]:
        if not s.get("in_cut") or s.get("start_s") is None or s.get("end_s") is None:
            out_of_cut += 1
            continue
        label = f"Шот {s['scene']}-{s['shot']}"
        nested = bool(s.get("covered_by")) or bool(s.get("unit_span"))
        lines = [f"{label} · {_tc(s['start_s'], s['end_s'])}"]
        if s.get("approximate"):
            lines.append(FILM_TEXTS["approximate"])
        texts = warn_by_key.get(s["key"], [])
        if texts:
            lines.append(f"{FILM_TEXTS['warning_word']}: {'; '.join(texts)}")
        blocks.append({
            "track": "shots", "start_s": _round3(s["start_s"]), "end_s": _round3(s["end_s"]), "label": label,
            "tone": "unit" if s.get("unit_span") else "covered" if s.get("covered_by") else "shot",
            "key": s["key"], "tooltip": "\n".join(lines), "approximate": bool(s.get("approximate")),
            "warning": len(s.get("warnings") or []) > 0, "nested": nested,
        })
    return blocks, out_of_cut


def _clip_blocks(track, data, film_end):
    out = []
    for c in data["clips"]:
        end = c.get("end_s") if c.get("end_s") is not None else film_end
        missing = not c.get("exists")
        extras = [x for x in (FILM_TEXTS["loop_word"] if c.get("loop") else "", FILM_TEXTS["no_file"] if missing else "") if x]
        label = f"{c['label']} ({', '.join(extras)})" if extras else c["label"]
        out.append({
            "track": track, "start_s": _round3(c["start_s"]), "end_s": _round3(max(end, c["start_s"])), "label": label,
            "tone": "missing" if missing else "clip", "key": c["id"],
            "tooltip": f"{label} · {_tc(c['start_s'], end)} · {placement_text(c['placement'])}",
            "approximate": False, "warning": False, "nested": False,
        })
    return out


def _cue_blocks(cues):
    return [{
        "track": "subtitles", "start_s": _round3(c["start_s"]), "end_s": _round3(c["end_s"]), "label": c["text"],
        "tone": "cue", "key": str(c["index"]), "tooltip": f"{_tc(c['start_s'], c['end_s'])}\n{c['text']}",
        "approximate": False, "warning": False, "nested": False,
    } for c in cues]


def build_layout(timeline, cues=()):
    duration = film_duration(timeline, cues)
    shots, out_of_cut = _shot_blocks(timeline)
    tracks = timeline["tracks"]
    blocks = [
        *shots, *_clip_blocks("narration", tracks["narration"], duration), *_clip_blocks("music", tracks["music"], duration),
        *_clip_blocks("sfx", tracks["sfx"], duration), *_cue_blocks(cues),
    ]
    end = max([0, *(b["end_s"] for b in blocks)])
    return {"blocks": blocks, "duration_s": _round3(max(duration, end)), "out_of_cut": out_of_cut}


def merge_narrow(blocks, px_per_s, min_px=LABEL_MIN_PX):
    """Мелкие блоки без места под подпись сливаются в одну полосу."""
    out = []
    for b in sorted(blocks, key=lambda x: x["start_s"]):
        narrow = (b["end_s"] - b["start_s"]) * px_per_s < min_px
        prev = out[-1] if out else None
        prev_narrow = prev is not None and (prev["end_s"] - prev["start_s"]) * px_per_s < min_px
        if narrow and prev is not None and prev_narrow and (b["start_s"] - prev["end_s"]) * px_per_s < min_px:
            prev["end_s"] = max(prev["end_s"], b["end_s"])
            prev["label"] = ""
            prev["key"] = None
            prev["tooltip"] = _tc(prev["start_s"], prev["end_s"])
        else:
            out.append(dict(b))
    return out


def block_at(blocks, track, t):
    hit = [b for b in blocks if b["track"] == track and b["start_s"] <= t < b["end_s"]]
    if not hit:
        return None
    hit.sort(key=lambda b: (-int(b["nested"]), -b["start_s"]))
    return hit[0]


def cue_at(cues, t):
    found = None
    for c in cues:
        if c["start_s"] <= t:
            found = c
        else:
            break
    return found if found is not None and t < found["end_s"] else None


# ---- состояние экрана ----

def has_film_file(timeline):
    final = (timeline or {}).get("final") or {}
    return bool((final.get("video") or {}).get("exists") or (final.get("photo_video") or {}).get("exists"))


def resolve_state(timeline, error):
    if not timeline:
        return "error" if error else "loading"
    if has_film_file(timeline):
        return "ready"
    tracks = timeline["tracks"]
    return "partial" if sum(len(tracks[k]["clips"]) for k in ("narration", "music", "sfx")) > 0 else "empty"


MONTAGE_STEP_ID = "montage_assembler"


def is_montage_running(run_state, steps):
    return _busy(run_state) and bool(steps) and MONTAGE_STEP_ID in steps


def banners(stale_hint, older_than_shots, source, montage_running):
    out = []
    if montage_running:
        out.append({"id": "building", "tone": "warn", "text": FILM_TEXTS["building"]})
    if stale_hint or older_than_shots:
        out.append({"id": "stale", "tone": "warn", "text": FILM_TEXTS["stale"]})
    if source is not None and source != "edit_decisions":
        out.append({"id": "approximate", "tone": "info", "text": FILM_TEXTS["approximate"]})
    return out


def banner_ids(*args):
    return [b["id"] for b in banners(*args)]


def music_rows(clips):
    groups = {}
    for c in clips:
        groups.setdefault(c.get("music_id") or c["id"], []).append(c)
    rows = []
    for mid, items in groups.items():
        whole = any(c["placement"] == "film" or c.get("scene") is None for c in items)
        scenes = sorted({c["scene"] for c in items if c.get("scene") is not None})
        first = items[0]
        rows.append({
            "id": mid, "name": first["label"],
            "scenes": FILM_TEXTS["music_all_scenes"] if whole else ", ".join(str(x) for x in scenes),
            "source": placement_text(first["placement"]), "path": first["path"],
            "exists": any(c.get("exists") for c in items),
        })
    return rows


def file_rows(video, resolution):
    dur = (video or {}).get("duration_s")
    return [
        {"id": "size", "label": FILM_TEXTS["file_size"], "value": format_bytes(video["size"]) if video else "—"},
        {"id": "duration", "label": FILM_TEXTS["file_duration"], "value": format_timecode(dur) if dur is not None else "—"},
        {"id": "resolution", "label": FILM_TEXTS["file_resolution"],
         "value": f"{resolution['width']}×{resolution['height']}" if resolution and resolution["width"] > 0 else "—"},
        {"id": "created", "label": FILM_TEXTS["file_created"], "value": format_created_at(video.get("modified")) if video else "—"},
    ]


def source_video(timeline, source):
    final = timeline["final"]
    v = final.get("video") if source == "film" else final.get("photo_video")
    return v if v and v.get("exists") else None


def default_source(exists):
    """exists: {"video": bool, "photo": bool}; фильм первым, иначе фотомонтаж."""
    if exists.get("video"):
        return "film"
    return "photo" if exists.get("photo") else "film"


# ---- клавиши плеера ----

SEEK_STEP_S = 5
SEEK_FINE_STEP_S = 1
_KEYS = {
    " ": {"type": "toggle"}, "Spacebar": {"type": "toggle"},
    "m": {"type": "mute"}, "M": {"type": "mute"}, "ь": {"type": "mute"}, "Ь": {"type": "mute"},
    "f": {"type": "fullscreen"}, "F": {"type": "fullscreen"}, "а": {"type": "fullscreen"}, "А": {"type": "fullscreen"},
    "+": {"type": "zoom", "direction": "in"}, "=": {"type": "zoom", "direction": "in"},
    "-": {"type": "zoom", "direction": "out"}, "_": {"type": "zoom", "direction": "out"},
}


def key_action(key, shift, in_field, mod=False):
    if in_field or mod:
        return None
    step = SEEK_FINE_STEP_S if shift else SEEK_STEP_S
    if key == "ArrowLeft":
        return {"type": "seek_by", "delta": -step}
    if key == "ArrowRight":
        return {"type": "seek_by", "delta": step}
    action = _KEYS.get(key)
    return dict(action) if action else None
