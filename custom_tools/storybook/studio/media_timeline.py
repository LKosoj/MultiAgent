"""Таймлайн фильма и субтитры для studio (вызывается из media.py).

Позиции звука считаются по правилам монтажа (custom_tools/storybook/timeline_media.py,
_audio_segments), а не читаются из готового файла: если монтаж поменяет правила,
расчёт здесь надо обновить. Поле `placement` у клипа говорит, откуда взято начало.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .media import MediaEntry

_MEDIA = "custom_tools.storybook.studio.media"


def _media():
    """Модуль media на момент вызова (не на момент импорта этого файла)."""
    return importlib.import_module(_MEDIA)


def DurationCache(root, probe_limit):
    return _media().DurationCache(root, probe_limit)


def norm_key(raw):
    return _media().norm_key(raw)


def parse_srt(text):
    return _media().parse_srt(text)


def read_json_cached(path, root):
    return _media().read_json_cached(path, root)


def scan_media(root):
    return _media().scan_media(root)


_KNOWN_DIRS = ("97_shots", "98_audio", "99_final", "93_blockout", "50_images", "20_bible")
_SRT_LIMIT = 2 * 1024 * 1024
_DEFAULT_SHOT_S = 5.0
_NARRATION_ROLES = ("narration", "audio", "voice")


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_timing(value: object, default: float = _DEFAULT_SHOT_S) -> float:
    """'00:05', '1:02:03', '5s', '5', 5 -> секунды; мусор -> default."""
    number = _num(value)
    if number is not None:
        return number if number > 0 else default
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        hours, minutes, seconds = match.groups()
        total = int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
        return total if total > 0 else default
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|с|сек)?", text)
    if match and float(match.group(1)) > 0:
        return float(match.group(1))
    return default


class _Ctx:
    def __init__(self, root: Path, project_id: str, probe_limit: int = 0):
        self.root = root
        self.project_id = project_id
        self.warnings: list[dict] = []
        self.cache = DurationCache(root, probe_limit)
        self.entries: list[MediaEntry] = scan_media(root)

    def warn(self, code: str, message: str, **extra) -> None:
        item = {"code": code, "message": message, **extra}
        if item not in self.warnings:
            self.warnings.append(item)

    def load(self, rel: str, bad_code: str | None = None, bad_message: str = "") -> object:
        state, data = read_json_cached(self.root / rel, self.root)
        if state == "bad" and bad_code:
            self.warn(bad_code, bad_message or f"Файл {rel} не читается")
        return data if state == "ok" else None

    def exists(self, rel: str | None) -> bool:
        return bool(rel) and (self.root / rel).is_file()


def to_rel(root: Path, project_id: str, raw: object, default_base: str = "") -> str | None:
    """Путь из манифеста -> относительный путь проекта (или None, если сопоставить нельзя)."""
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        return None
    text = raw.strip().replace("\\", "/")
    absolute = text.startswith("/") or bool(re.match(r"^[A-Za-z]:/", text))
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if ".." in parts or not parts:
        return None

    def after_project(items: list[str]) -> list[str] | None:
        if project_id in items:
            tail = items[items.index(project_id) + 1:]
            return tail or None
        return None

    def from_known(items: list[str]) -> list[str] | None:
        for i, part in enumerate(items):
            if part in _KNOWN_DIRS:
                return items[i:]
        return None

    candidate: list[str] | None = None
    if absolute:
        try:
            inside = PurePosixPath(os.path.normpath(text)).relative_to(root.as_posix())
            candidate = list(inside.parts) or None
        except ValueError:
            candidate = after_project(parts) or from_known(parts)
    else:
        options = [parts]
        if default_base:
            options.append([default_base, *parts])
        tail = after_project(parts)
        if tail:
            options.append(tail)
        for option in options:
            if (root.joinpath(*option)).is_file():
                candidate = option
                break
        if candidate is None:
            candidate = tail or (parts if parts[0] in _KNOWN_DIRS else None) or (
                [default_base, *parts] if default_base else None)
    if not candidate or ".." in candidate:
        return None
    full = root.joinpath(*candidate)
    if full.exists():
        try:
            full.resolve().relative_to(root)
        except ValueError:
            return None
    return "/".join(candidate)


# --- шоты -------------------------------------------------------------------------------


def _split_key(key: str) -> tuple[int, int]:
    scene, shot = key.split("-")
    return int(scene), int(shot)


def _new_shot(key: str) -> dict:
    scene, shot = _split_key(key)
    return {
        "key": key, "scene": scene, "shot": shot, "index": None,
        "start_s": None, "end_s": None, "duration_s": None,
        "planned_duration_s": None, "actual_duration_s": None,
        "in_cut": False, "approximate": False, "covered_by": None, "unit_span": False,
        "unit": None, "video": None, "start_image": None, "end_image": None,
        "thumb_path": None, "warnings": [],
    }


def _shot_assets(ctx: _Ctx) -> tuple[dict[str, dict], list[MediaEntry]]:
    assets: dict[str, dict] = {}
    units: list[MediaEntry] = []
    for e in sorted(ctx.entries, key=lambda x: x.path):
        if e.type == "shot" and e.key:
            slot = {"start_frame": "start_image", "end_frame": "end_image"}.get(e.category)
            if slot:
                assets.setdefault(e.key, {}).setdefault(slot, e.path)
        elif e.type == "shot_video" and e.category == "shot_clip" and e.key:
            assets.setdefault(e.key, {}).setdefault("video", e.path)
        elif e.category == "video_unit" and e.keys:
            units.append(e)
    return assets, units


def _plan_durations(ctx: _Ctx) -> tuple[dict[str, float | None], bool]:
    """Плановые длительности шотов из shots.json; (словарь, файл_читается)."""
    state, data = read_json_cached(ctx.root / "97_shots" / "shots.json", ctx.root)
    if state == "bad":
        ctx.warn("shots_unreadable", "Файл 97_shots/shots.json не читается")
    items = data.get("items") if isinstance(data, dict) else data
    out: dict[str, float | None] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        scene, shot = _int(item.get("scene_number")), _int(item.get("shot_number"))
        if scene is None or shot is None:
            continue
        key = f"{scene}-{shot}"
        duration = _num(item.get("duration_s"))
        if not duration or duration <= 0:
            timing = item.get("timing")
            duration = parse_timing(timing) if timing not in (None, "") else None
        if out.get(key) is None:
            out[key] = duration
    return out, state == "ok"


def _clip_durations(ctx: _Ctx) -> tuple[dict[str, float], dict[str, float]]:
    """(плановая, фактическая) длительность по ключу шота из asset_manifest/render_report."""
    planned: dict[str, float] = {}
    actual: dict[str, float] = {}
    sources = [
        (ctx.load("99_final/asset_manifest.json"), "clips"),
        (ctx.load("99_final/render_report.json"), "clips_actual_vs_planned"),
    ]
    for data, field in sources:
        clips = data.get(field) if isinstance(data, dict) else None
        for clip in clips if isinstance(clips, list) else []:
            if not isinstance(clip, dict):
                continue
            scene, shot = _int(clip.get("scene_number")), _int(clip.get("shot_number"))
            if scene is None or shot is None:
                continue
            key = f"{scene}-{shot}"
            p, a = _num(clip.get("planned_duration_seconds")), _num(clip.get("actual_duration_seconds"))
            if p is not None:
                planned.setdefault(key, p)
            if a is not None:
                actual.setdefault(key, a)
    return planned, actual


def _collect_shots(ctx: _Ctx) -> tuple[str | None, list[dict], dict]:
    """(источник, шоты, служебные данные). Без звука, финала и открытия видео."""
    assets, units = _shot_assets(ctx)
    plan_durations, _ = _plan_durations(ctx)
    clip_planned, clip_actual = _clip_durations(ctx)
    state, _plan = read_json_cached(ctx.root / "97_shots" / "video_plan.json", ctx.root)
    if state == "bad":
        ctx.warn("video_plan_unreadable", "Файл 97_shots/video_plan.json не читается")
    unit_of: dict[str, MediaEntry] = {}
    for unit in units:
        for k in unit.keys:
            unit_of.setdefault(k, unit)

    def planned_of(key: str) -> float | None:
        return clip_planned.get(key) if clip_planned.get(key) is not None else plan_durations.get(key)

    shots: dict[str, dict] = {}
    source: str | None = None

    edit = ctx.load("99_final/edit_decisions.json", "edit_decisions_unreadable",
                    "Файл 99_final/edit_decisions.json не читается")
    sequence = edit.get("sequence") if isinstance(edit, dict) else None
    if isinstance(sequence, list) and sequence:
        source = "edit_decisions"
        for position, rec in enumerate(sequence, 1):
            if not isinstance(rec, dict):
                continue
            scene, shot = _int(rec.get("scene_number")), _int(rec.get("shot_number"))
            start, end = _num(rec.get("timeline_start_seconds")), _num(rec.get("timeline_end_seconds"))
            if scene is None or shot is None or start is None or end is None:
                continue
            key = f"{scene}-{shot}"
            if key in shots:
                continue
            item = _new_shot(key)
            item.update(index=_int(rec.get("index")) or position, start_s=start, end_s=end, in_cut=True)
            item["_source_path"] = rec.get("source_path")
            shots[key] = item
        # шоты видео-блока без собственной записи: делим отрезок блока по плановым длительностям
        for unit in units:
            if len(unit.keys) < 2:
                continue
            lead = next((k for k in unit.keys if k in shots and shots[k]["in_cut"]), None)
            absent = [k for k in unit.keys if k not in shots]
            if lead is None or not absent:
                continue
            weights = [planned_of(k) for k in unit.keys]
            known = [w for w in weights if w]
            fallback = (sum(known) / len(known)) if known else 1.0
            weights = [w if w else fallback for w in weights]
            total = sum(weights)
            begin, finish = shots[lead]["start_s"], shots[lead]["end_s"]
            cursor = begin
            for k, w in zip(unit.keys, weights):
                span = (finish - begin) * w / total
                if k in absent:
                    item = _new_shot(k)
                    item.update(start_s=round(cursor, 3), end_s=round(cursor + span, 3), in_cut=True,
                                approximate=True, covered_by=lead)
                    item["warnings"].append("unit_member_approximate")
                    ctx.warn("unit_member_approximate",
                             f"Время шота {k} приблизительное: он смонтирован в одном видео-блоке с {lead}", key=k)
                    shots[k] = item
                cursor += span
            shots[lead]["unit_span"] = True
    else:
        if edit is None and not (ctx.root / "99_final" / "edit_decisions.json").exists():
            ctx.warn("no_edit_decisions", "Монтаж ещё не делали: время шотов плановое")
        keys = sorted((k for k in plan_durations if norm_key(k)), key=_split_key)
        if keys:
            source = "shots"
            cursor = 0.0
            for position, key in enumerate(keys, 1):
                duration = plan_durations.get(key) or _DEFAULT_SHOT_S
                item = _new_shot(key)
                item.update(index=position, start_s=cursor, end_s=cursor + duration, in_cut=True)
                shots[key] = item
                cursor += duration
        else:
            cues = ctx.load("98_audio/cue_sheet.json")
            rows = cues.get("cues") if isinstance(cues, dict) else None
            grouped: dict[str, list[float]] = {}
            for cue in rows if isinstance(rows, list) else []:
                if not isinstance(cue, dict):
                    continue
                scene, shot = _int(cue.get("scene_number")), _int(cue.get("shot_number"))
                start, end = _num(cue.get("start_seconds")), _num(cue.get("end_seconds"))
                if scene is None or shot is None or start is None or end is None:
                    continue
                span = grouped.setdefault(f"{scene}-{shot}", [start, end])
                span[0], span[1] = min(span[0], start), max(span[1], end)
            if grouped:
                source = "cue_sheet"
                for position, (key, (start, end)) in enumerate(
                        sorted(grouped.items(), key=lambda kv: (kv[1][0], _split_key(kv[0]))), 1):
                    item = _new_shot(key)
                    item.update(index=position, start_s=start, end_s=end, in_cut=True)
                    shots[key] = item

    # остальные известные шоты — вне монтажа
    known_keys = set(plan_durations) | set(assets)
    for key in sorted((k for k in known_keys if norm_key(k) and k not in shots), key=_split_key):
        shots[key] = _new_shot(key)
        if source == "edit_decisions":
            shots[key]["warnings"].append("shot_not_in_cut")
            ctx.warn("shot_not_in_cut", f"Шот {key} не попал в монтаж", key=key)

    for key, item in shots.items():
        asset = assets.get(key, {})
        unit = unit_of.get(key)
        video = asset.get("video") or (unit.path if unit else None)
        if video is None and item.get("_source_path"):
            rel = to_rel(ctx.root, ctx.project_id, item["_source_path"])
            if rel and ctx.exists(rel):
                video = rel
            else:
                ctx.warn("path_unresolved", f"Не удалось найти видео шота {key}: "
                         f"{str(item['_source_path']).replace(chr(92), '/').rsplit('/', 1)[-1]}", key=key)
        item.pop("_source_path", None)
        item["video"] = video
        item["start_image"], item["end_image"] = asset.get("start_image"), asset.get("end_image")
        item["thumb_path"] = item["start_image"] or item["end_image"] or video
        if unit and len(unit.keys) > 1 and unit.category == "video_unit":
            item["unit"] = {"keys": list(unit.keys), "path": unit.path}
        item["planned_duration_s"] = planned_of(key)
        if item["start_s"] is not None:
            item["start_s"], item["end_s"] = round(item["start_s"], 3), round(item["end_s"], 3)
            item["duration_s"] = round(item["end_s"] - item["start_s"], 3)

    ordered = sorted((s for s in shots.values() if s["in_cut"]),
                     key=lambda s: (s["start_s"], s["covered_by"] is not None, s["scene"], s["shot"]))
    ordered += sorted((s for s in shots.values() if not s["in_cut"]), key=lambda s: (s["scene"], s["shot"]))
    return source, ordered, {"actual": clip_actual}


def _scenes(shots: list[dict]) -> list[dict]:
    grouped: dict[int, list[dict]] = {}
    for shot in shots:
        if shot["in_cut"]:
            grouped.setdefault(shot["scene"], []).append(shot)
    out = []
    for scene in sorted(grouped):
        items = grouped[scene]
        start, end = min(s["start_s"] for s in items), max(s["end_s"] for s in items)
        out.append({"scene": scene, "start_s": start, "end_s": end, "duration_s": round(end - start, 3),
                    "shot_keys": [s["key"] for s in items]})
    return out


# --- звук -------------------------------------------------------------------------------


def _manifest_path(value: object) -> str | None:
    path = value.get("path") if isinstance(value, dict) else value
    if isinstance(path, str) and path.strip() and _media()._kind_of(Path(path).suffix) == "audio":
        return path
    return None


def _audio_tracks(ctx: _Ctx, scenes: dict[int, dict], film_end: float | None) -> dict:
    root, pid = ctx.root, ctx.project_id
    audio = ctx.load("98_audio/audio_manifest.json", "audio_manifest_unreadable",
                     "Файл 98_audio/audio_manifest.json не читается")
    sfx = ctx.load("98_audio/sfx_manifest.json", "audio_manifest_unreadable",
                   "Файл 98_audio/sfx_manifest.json не читается")
    music_manifest = ctx.load("98_audio/music_manifest.json", "audio_manifest_unreadable",
                              "Файл 98_audio/music_manifest.json не читается")
    music_plan = ctx.load("98_audio/music_plan.json", "audio_manifest_unreadable",
                          "Файл 98_audio/music_plan.json не читается")
    tts_status = audio.get("tts_status") if isinstance(audio, dict) else None

    def source_duration(rel: str, exists: bool) -> float | None:
        if not exists:
            return None
        value = ctx.cache.get(rel, "audio")
        if value is None:
            ctx.warn("audio_unknown_duration", "Длительность звукового файла неизвестна", path=rel)
        return value

    def place(offset, given, scene, src, *, scene_len: bool) -> tuple[float, float | None, str]:
        info = scenes.get(scene) if scene is not None else None
        if offset is not None:
            start, placement = offset, "manifest"
        elif info:
            start, placement = info["start_s"], "scene"
        else:
            start, placement = 0.0, "film"
        if given:
            length = given
        elif scene_len and info:
            length = info["duration_s"]
        elif film_end is not None:
            length = max(film_end - start, 0.0)
        else:
            length = src
        if src is not None and length is not None:
            length = min(length, src)
        return start, length, placement

    def clip(role: str, label: str, rel: str, scene, music_id, start, length, src, loop, placement) -> dict:
        exists = ctx.exists(rel)
        return {
            "id": f"{role}:{label}:{scene if scene is not None else 0}", "role": role, "label": label,
            "path": rel, "exists": exists, "scene": scene, "music_id": music_id,
            "start_s": round(start, 3),
            "end_s": round(start + length, 3) if length is not None else None,
            "duration_s": round(length, 3) if length is not None else None,
            "source_duration_s": src, "loop": loop, "placement": placement,
        }

    narration: list[dict] = []
    effects: list[dict] = []
    manifest_music: list[dict] = []
    seen: set[tuple] = set()
    for data in (audio, sfx):
        tracks = data.get("audio_tracks") if isinstance(data, dict) else None
        for track in tracks if isinstance(tracks, list) else []:
            if not isinstance(track, dict):
                continue
            role = "narration" if track.get("role") in _NARRATION_ROLES else track.get("role")
            if role not in ("narration", "music", "sfx"):
                continue
            raw = track.get("path")
            rel = to_rel(root, pid, raw, "98_audio")
            if rel is None:
                name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
                ctx.warn("path_unresolved", f"Не удалось сопоставить путь звука с проектом: {name}", name=name)
                continue
            scene = _int(track.get("scene_number"))
            sig = (role, scene, rel)
            if sig in seen:
                continue
            seen.add(sig)
            exists = ctx.exists(rel)
            src = source_duration(rel, exists)
            start, length, placement = place(_num(track.get("offset_seconds")), _num(track.get("duration_seconds")),
                                             scene, src, scene_len=(role == "sfx"))
            label = Path(rel).stem
            item = clip(role, label, rel, scene, label if role == "music" else None, start, length, src,
                        False, placement)
            {"narration": narration, "sfx": effects, "music": manifest_music}[role].append(item)

    if not narration:
        for entry in ctx.entries:
            if entry.type == "narration":
                src = source_duration(entry.path, True)
                start, length, placement = place(None, None, None, src, scene_len=False)
                narration.append(clip("narration", Path(entry.path).stem, entry.path, None, None,
                                      start, length, src, False, placement))
    for entry in ctx.entries:  # эффекты, которых нет в манифестах, но лежат на диске
        if entry.type == "sfx" and not any(c["path"] == entry.path for c in effects):
            src = source_duration(entry.path, True)
            start, length, placement = place(None, None, entry.scene, src, scene_len=True)
            effects.append(clip("sfx", Path(entry.path).stem, entry.path, entry.scene, None,
                                start, length, src, False, placement))

    # музыка: по плану (на каждую сцену) или из audio_manifest
    pairs = {}
    service_keys = False
    if isinstance(music_manifest, dict):
        for mid, value in music_manifest.items():
            path = _manifest_path(value)
            if path:
                pairs[str(mid)] = path
            else:
                service_keys = True
    mapping = music_plan.get("scene_mapping") if isinstance(music_plan, dict) else None
    music: list[dict] = []
    music_source = "none"
    if isinstance(mapping, dict) and mapping and pairs:
        music_source = "plan"
        for scene_raw in sorted(mapping, key=lambda s: (not str(s).isdigit(), int(s) if str(s).isdigit() else 0)):
            scene = int(scene_raw) if str(scene_raw).isdigit() else None
            mid = mapping[scene_raw]
            mid = mid if isinstance(mid, str) else (mid.get("id") if isinstance(mid, dict) else None)
            info = scenes.get(scene) if scene is not None else None
            if not mid or not info:
                continue
            rel = to_rel(root, pid, pairs.get(mid), "98_audio") if mid in pairs else None
            if rel is None:
                ctx.warn("missing_file", f"Для музыки «{mid}» сцены {scene} нет файла", key=str(scene))
                continue
            exists = ctx.exists(rel)
            src = source_duration(rel, exists)
            length = info["duration_s"]
            loop = None if src is None else src < info["duration_s"]
            music.append(clip("music", mid, rel, scene, mid, info["start_s"], length, src, loop, "scene"))
    elif manifest_music:
        music_source = "manifest"
        music = manifest_music
    if service_keys:
        ctx.warn("music_pending", "Музыка ещё генерируется или дорожки не готовы")

    def status_of(clips: list[dict]) -> str:
        return "present" if any(c["exists"] for c in clips) else "missing"

    result_tracks: dict[str, dict] = {}
    if not any(c["exists"] for c in narration) and tts_status == "unavailable":
        narration = []
        result_tracks["narration"] = {"status": "unavailable", "note": "Озвучка не подключена (TTS недоступен)",
                                      "clips": []}
    else:
        result_tracks["narration"] = {"status": status_of(narration), "note": "", "clips": narration}
    music_note = "Музыка ещё генерируется" if service_keys and not any(c["exists"] for c in music) else ""
    result_tracks["music"] = {"status": status_of(music), "source": music_source, "note": music_note,
                              "clips": music}
    result_tracks["sfx"] = {"status": status_of(effects), "note": "", "clips": effects}
    for track in result_tracks.values():
        track["clips"].sort(key=lambda c: (c["start_s"], c["id"]))
        for c in track["clips"]:
            if not c["exists"]:
                ctx.warn("missing_file", "Файл звука потерян", path=c["path"])
    return result_tracks


# --- субтитры в сводке и отдельно ---------------------------------------------------------

_SRT_FILES = {"final": ("99_final/subtitles.srt", "final"), "planned": ("98_audio/subtitles.srt", "planned")}


def _pick_srt(root: Path, source: str) -> tuple[str | None, str | None]:
    order = ["final", "planned"] if source == "auto" else [source]
    for name in order:
        rel = _SRT_FILES[name][0]
        if _media().inside_root(root, root / rel):
            return rel, name
    return None, None


def _read_srt(root: Path, rel: str) -> tuple[list[dict], int, bool]:
    if not _media().inside_root(root, root / rel):  # симлинк наружу = файла нет
        return [], 0, False
    with open(root / rel, "rb") as handle:
        data = handle.read(_SRT_LIMIT + 1)
    truncated = len(data) > _SRT_LIMIT
    if truncated:
        data = data[:_SRT_LIMIT]
        cut = data.rfind(b"\n")
        data = data[:cut] if cut > 0 else data
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    cues, skipped = parse_srt(text)
    return cues, skipped, truncated


def build_subtitles(root: Path, source: str) -> dict:
    rel, name = _pick_srt(root, source)
    if rel is None:
        return {"source": None, "path": None, "exists": False, "count": 0, "skipped": 0,
                "duration_s": None, "truncated": False, "cues": []}
    cues, skipped, truncated = _read_srt(root, rel)
    ctx = _Ctx(root, root.name)
    _, shots, _ = _collect_shots(ctx)
    spans = [s for s in shots if s["in_cut"]]
    spans.sort(key=lambda s: not s["approximate"])  # точные участки блока важнее всего блока
    for cue in cues:
        mid = (cue["start_s"] + cue["end_s"]) / 2
        cue["shot_key"] = next((s["key"] for s in spans if s["start_s"] <= mid < s["end_s"]), None)
    return {"source": name, "path": rel, "exists": True, "count": len(cues), "skipped": skipped,
            "duration_s": max((c["end_s"] for c in cues), default=None), "truncated": truncated, "cues": cues}


# --- таймлайн ---------------------------------------------------------------------------


def _final_block(ctx: _Ctx, shots: list[dict]) -> tuple[dict, dict | None, dict | None]:
    clip_mtime = max((e.mtime_ns for e in ctx.entries
                      if e.type == "shot_video" and e.category in ("shot_clip", "video_unit")), default=None)

    def describe(entry: MediaEntry | None) -> dict | None:
        if entry is None:
            return None
        stem = entry.path.rsplit(".", 1)[0]
        data = ctx.load(f"{stem}.render.json")
        duration = _num(data.get("duration_seconds")) if isinstance(data, dict) else None
        if duration is None:
            duration = ctx.cache.get(entry.path, "video")
        return {"path": entry.path, "type": entry.type, "size": entry.size,
                "modified": _media()._iso(entry.mtime_ns), "duration_s": duration, "exists": True,
                "older_than_shots": clip_mtime is not None and clip_mtime > entry.mtime_ns}

    finals = [e for e in ctx.entries if e.type == "final_video"]
    final = next((e for e in finals if Path(e.path).stem == "final_video"), finals[0] if finals else None)
    photo = next((e for e in ctx.entries if e.type == "photo_video"), None)
    video, photo_video = describe(final), describe(photo)
    review = ctx.load("99_final/final_review.json")
    block = {
        "video": video, "photo_video": photo_video,
        "render_report_path": "99_final/render_report.json" if ctx.exists("99_final/render_report.json") else None,
        "final_review_path": "99_final/final_review.json" if ctx.exists("99_final/final_review.json") else None,
        "review_passed": review.get("passed") if isinstance(review, dict) and isinstance(review.get("passed"), bool) else None,
        "stale_hint": bool(video and video["older_than_shots"]),
    }
    return block, video, photo_video


def build_timeline(root: Path, project_id: str, probe_limit: int) -> dict:
    ctx = _Ctx(root, project_id, probe_limit)
    try:
        return _build_timeline(ctx, root)
    finally:
        ctx.cache.flush()


def _build_timeline(ctx: _Ctx, root: Path) -> dict:
    source, shots, extra = _collect_shots(ctx)
    for shot in shots:
        if not shot["in_cut"] and shot["start_s"] is None:
            continue
        actual = extra["actual"].get(shot["key"])
        if actual is None and shot["video"] and shot["covered_by"] is None:
            actual = ctx.cache.get(shot["video"], "video")
        shot["actual_duration_s"] = actual
    scenes = _scenes(shots)
    ends = [s["end_s"] for s in shots if s["in_cut"]]
    duration = max(ends) if ends else None

    final, video, _photo = _final_block(ctx, shots)
    if duration is not None and video and video["duration_s"] is not None \
            and abs(video["duration_s"] - duration) > 1.0:
        ctx.warn("final_duration_mismatch",
                 f"Длина фильма {video['duration_s']:.1f} с не совпадает с таймлайном {duration:.1f} с")
    film_end = duration if duration is not None else (video["duration_s"] if video else None)
    tracks = _audio_tracks(ctx, {s["scene"]: s for s in scenes}, film_end)

    rel, name = _pick_srt(root, "auto")
    subtitles = {"source": name, "path": rel, "count": 0, "duration_s": None}
    if rel:
        cues, _skipped, _trunc = _read_srt(root, rel)
        subtitles.update(count=len(cues), duration_s=max((c["end_s"] for c in cues), default=None))

    missing = []
    if not shots:
        missing.append("shots")
    if video is None:
        missing.append("final_video")
    for track in ("narration", "music", "sfx"):
        if tracks[track]["status"] != "present":
            missing.append(track)
    if rel is None:
        missing.append("subtitles")
    return {
        "source": source, "duration_s": duration, "shots": shots, "scenes": scenes, "tracks": tracks,
        "subtitles": subtitles, "final": final, "missing": missing, "warnings": ctx.warnings,
        "durations_pending": ctx.cache.pending,
        "labels": _media().media_labels(),
    }
