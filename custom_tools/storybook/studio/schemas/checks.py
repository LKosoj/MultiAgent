"""Смысловые проверки артефактов (то, что схема выразить не может)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

_TIMING_RE = re.compile(r"^\d{1,2}:\d{2}$")
_JOB_STATUSES = {"prepared", "submitting", "submitted", "downloaded", "failed", "stale"}


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dict_items(data: Any, field: str | None = None):
    """(путь-префикс, индекс, элемент-словарь) для списка или списка в поле."""
    lst = data if field is None else (data.get(field) if isinstance(data, dict) else None)
    if not isinstance(lst, list):
        return
    prefix = [] if field is None else [field]
    for idx, element in enumerate(lst):
        if isinstance(element, dict):
            yield prefix, idx, element


def _duplicates(data, field, name, make, out, code="duplicate", label="Повтор") -> None:
    seen: dict[Any, int] = {}
    for prefix, idx, element in _dict_items(data, field):
        value = element.get(name)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            continue
        if value in seen:
            out.append(make("error", code, prefix + [idx, name],
                            f"{label}: значение «{value}» уже есть в элементе {seen[value] + 1}"))
        else:
            seen[value] = idx


def _path_missing(value: Any, ctx, bases: tuple[str, ...] = ("",)) -> bool:
    """True, если относительный путь внутри проекта точно указывает на несуществующий файл.

    Абсолютные и внешние пути пропускаются (путь шота бывает и абсолютным, и относительным)."""
    if ctx.project_dir is None or not isinstance(value, str) or not value.strip():
        return False
    if value.startswith("/") or "\0" in value or (len(value) > 1 and value[1] == ":"):
        return False
    root = ctx.project_dir.resolve()
    inside_any = False
    for base in bases:
        try:
            candidate = (root / base / value).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            continue
        inside_any = True
        if candidate.exists():
            return False
    # путь вида «plots/storybooks/<проект>/…» считается от рабочей папки процесса
    try:
        from_cwd = Path(value).resolve()
        from_cwd.relative_to(root)
        inside_any = True
        if from_cwd.exists():
            return False
    except (ValueError, OSError):
        pass
    return inside_any


def _consecutive(numbers: list[int], start: int | None = None) -> list[int]:
    """Пропущенные номера между наименьшим (или start) и наибольшим."""
    if not numbers:
        return []
    low = numbers[0] if start is None else start
    have = set(numbers)
    return [n for n in range(low, max(numbers) + 1) if n not in have]


def _check_brief(data, ctx, make, out):
    if not isinstance(data, dict):
        return
    for lo, hi, label in (("pages_min", "pages_max", "страниц"),
                          ("words_per_page_min", "words_per_page_max", "слов на странице")):
        if _is_num(data.get(lo)) and _is_num(data.get(hi)) and data[lo] > data[hi]:
            out.append(make("error", "range", [lo], f"Минимум {label} больше максимума"))
    for field in ("main_characters", "main_locations"):
        values = data.get(field)
        if isinstance(values, list):
            for idx, value in enumerate(values):
                if isinstance(value, str) and not value.strip():
                    out.append(make("warning", "custom", [field, idx], "Пустая строка в списке"))


def _check_beats(data, ctx, make, out):
    _duplicates(data, None, "page_number", make, out, label="Страница повторяется")
    pages = sorted({e["page_number"] for _, _, e in _dict_items(data)
                    if isinstance(e.get("page_number"), int) and not isinstance(e["page_number"], bool)})
    gaps = _consecutive(pages)
    if gaps:
        out.append(make("warning", "custom", [], "Пропущены номера страниц: " + ", ".join(map(str, gaps))))


def _check_names(data, ctx, make, out):
    _duplicates(data, None, "name", make, out, label="Имя повторяется")


def _check_story(data, ctx, make, out):
    _duplicates(data, "pages", "page", make, out, label="Номер страницы повторяется")
    pages = sorted({e["page"] for _, _, e in _dict_items(data, "pages")
                    if isinstance(e.get("page"), int) and not isinstance(e["page"], bool)})
    gaps = _consecutive(pages, 1)
    if gaps:
        out.append(make("warning", "custom", ["pages"], "Пропущены номера страниц: " + ", ".join(map(str, gaps))))
    brief = ctx.read("brief")
    count = len(data.get("pages") or []) if isinstance(data, dict) and isinstance(data.get("pages"), list) else None
    if count is not None and isinstance(brief, dict):
        lo, hi = brief.get("pages_min"), brief.get("pages_max")
        if _is_num(lo) and _is_num(hi) and not lo <= count <= hi:
            out.append(make("warning", "range", ["pages"],
                            f"Страниц {count}, а в брифе {lo}–{hi}"))


def _check_screenplay(data, ctx, make, out):
    _duplicates(data, "screenplay", "scene_number", make, out, label="Номер сцены повторяется")
    for prefix, idx, scene in _dict_items(data, "screenplay"):
        for sprefix, sidx, shot in _dict_items(scene, "storyboard"):
            timing = shot.get("timing")
            if isinstance(timing, str) and not _TIMING_RE.match(timing):
                out.append(make("warning", "pattern", prefix + [idx] + sprefix + [sidx, "timing"],
                                "Ожидается время вида 00:05"))


def _check_shots(data, ctx, make, out):
    seen: dict[tuple, int] = {}
    for prefix, idx, item in _dict_items(data, "items"):
        key = (item.get("scene_number"), item.get("shot_number"), item.get("shot_type"))
        if all(v is not None for v in key):
            if key in seen:
                out.append(make("error", "duplicate", prefix + [idx],
                                f"Шот (сцена {key[0]}, шот {key[1]}, {key[2]}) уже есть в элементе {seen[key] + 1}"))
            else:
                seen[key] = idx
        shot_type = item.get("shot_type")
        if isinstance(shot_type, str) and shot_type not in ("start", "end"):
            out.append(make("warning", "enum", prefix + [idx, "shot_type"], "Допустимо: start, end"))
        duration = item.get("duration_s")
        if duration is not None and not (_is_num(duration) and duration > 0):
            out.append(make("warning", "range", prefix + [idx, "duration_s"], "Длительность должна быть больше нуля"))
        timing = item.get("timing")
        if isinstance(timing, str) and not _TIMING_RE.match(timing):
            out.append(make("warning", "pattern", prefix + [idx, "timing"], "Ожидается время вида 00:05"))
        if _path_missing(item.get("image_path"), ctx):
            out.append(make("warning", "reference", prefix + [idx, "image_path"], "Файл картинки не найден"))


def _check_provider_jobs(data, ctx, make, out):
    for prefix, idx, job in _dict_items(data, "jobs"):
        status = job.get("status")
        if isinstance(status, str) and status not in _JOB_STATUSES:
            out.append(make("warning", "enum", prefix + [idx, "status"], "Допустимо: " + ", ".join(sorted(_JOB_STATUSES))))


def _check_chain_ids(data, ctx, make, out):
    _duplicates(data, "chains", "chain_id", make, out, label="Идентификатор повторяется")


def _check_asset_map(data, ctx, make, out):
    for field in ("characters", "locations"):
        for prefix, idx, entry in _dict_items(data, field):
            if "asset_id" in entry and not (isinstance(entry["asset_id"], str) and entry["asset_id"].strip()):
                out.append(make("warning", "custom", prefix + [idx, "asset_id"], "Ресурс не назначен"))


def _check_audio_manifest(data, ctx, make, out):
    for prefix, idx, track in _dict_items(data, "audio_tracks"):
        if _path_missing(track.get("path"), ctx, ("", "98_audio")):
            out.append(make("warning", "reference", prefix + [idx, "path"], "Файл дорожки не найден"))


def _check_music_manifest(data, ctx, make, out):
    if not isinstance(data, dict):
        return
    if "status" in data or "clip_ids" in data:
        if "status" in data and not isinstance(data["status"], str):
            out.append(make("error", "type", ["status"], "Ожидается строка"))
        clips = data.get("clip_ids")
        if "clip_ids" in data and not (isinstance(clips, list) and all(isinstance(c, str) for c in clips)):
            out.append(make("error", "type", ["clip_ids"], "Ожидается список строк"))
        return
    for name, value in data.items():
        if not isinstance(value, str):
            out.append(make("error", "type", [name], "Ожидается строка (путь к файлу)"))
        elif _path_missing(value, ctx, ("", "98_audio")):
            out.append(make("warning", "reference", [name], "Файл музыки не найден"))


def _check_edit_decisions(data, ctx, make, out):
    previous = None
    for prefix, idx, entry in _dict_items(data, "sequence"):
        start, end = entry.get("timeline_start_seconds"), entry.get("timeline_end_seconds")
        if _is_num(start) and _is_num(end) and end < start:
            out.append(make("error", "range", prefix + [idx, "timeline_end_seconds"], "Конец раньше начала"))
        number = entry.get("index")
        if isinstance(number, int) and not isinstance(number, bool):
            if previous is not None and number != previous + 1:
                out.append(make("warning", "custom", prefix + [idx, "index"], "Номера идут не подряд"))
            previous = number


_CHECKS: dict[str, Callable] = {
    "brief": _check_brief,
    "beats": _check_beats,
    "characters": _check_names,
    "locations": _check_names,
    "story": _check_story,
    "screenplay": _check_screenplay,
    "shots": _check_shots,
    "provider_jobs": _check_provider_jobs,
    "chains": _check_chain_ids,
    "scene_spec": _check_chain_ids,
    "asset_map": _check_asset_map,
    "audio_manifest": _check_audio_manifest,
    "music_manifest": _check_music_manifest,
    "edit_decisions": _check_edit_decisions,
}


def run_checks(key: str, data: Any, ctx, make) -> list[dict]:
    """Смысловые проверки артефакта; `make(severity, code, path, message)` строит находку."""
    check = _CHECKS.get(key)
    out: list[dict] = []
    if check is not None:
        check(data, ctx, make, out)
    return out
