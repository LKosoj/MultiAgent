"""Ревью шотов, версии и перегенерация (Ф7): возможности storybook.* и хуки для соседних задач.

Статусы ревью информационные (пайплайн их не читает). Версии неизменяемы и не удаляются.
Перегенерация делается подготовкой перед запуском (prepare_regeneration), сами инструменты
генерации не меняются. Права: только admin (О1).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import shutil
from typing import Any, Optional

from . import regeneration as rg
from . import review_state as rs
from . import shot_versions as sv
from .capabilities import capability
from .errors import StudioError, success
from .paths import project_dir

logger = logging.getLogger(__name__)

STATUS_TITLES = dict(rs.TITLES)
CONTINUITY_PLAN = "20_bible/continuity_plan.json"
BUDGET_STEP_IGNORED = frozenset({"text_llm"})


_mod = sv._mod
_optional = sv._optional


def status_title(status: Any) -> str:
    return rs.status_title(status)


def _film_review_state(project_id: str, series_id: str) -> dict:
    from custom_tools.storybook.project_paths import series_scope
    from .run_plan import stale_upstream_for_step
    from .series import series_dir

    root = series_dir(project_id, series_id)
    digest = hashlib.sha256()
    for name in ("final_video.mp4", "final_review.json"):
        path = root / "99_final" / name
        digest.update(name.encode())
        if path.is_file():
            with path.open("rb") as source:
                digest.update(hashlib.file_digest(source, "sha256").digest())
    final_available = (root / "99_final/final_video.mp4").is_file()
    series = json.loads((root / "series.json").read_text())
    stale_outputs = [name for name in series.get("stale_results", [])
                     if name == "99_final" or name.startswith("99_final/")]
    with series_scope(project_id, series_id):
        stale_outputs = sorted(set(stale_outputs + stale_upstream_for_step(project_id, "montage_assembler")))
    digest.update(json.dumps(stale_outputs, sort_keys=True).encode())
    report_path = root / "99_final/final_review.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    saved_path = root / ".studio/film_review.json"
    saved = json.loads(saved_path.read_text()) if saved_path.is_file() else {}
    input_digest = digest.hexdigest()
    stale = bool(stale_outputs or (saved and saved.get("input_digest") != input_digest))
    return success(project_id=project_id, series_id=series_id, final_available=final_available,
                   technical_passed=final_available and report.get("passed") is True and not stale_outputs,
                   review={"status": "pending" if stale else saved.get("status", "pending"),
                           "note": saved.get("note", ""), "input_digest": input_digest, "stale": stale})


@capability("film_review_get", title="Принятие фильма", access="read")
def film_review_get(project_id: str, series_id: str) -> dict:
    """Technical render success never grants artistic approval."""
    return _film_review_state(project_id, series_id)


@capability("film_review_set", title="Сохранить решение по фильму", access="write")
def film_review_set(project_id: str, series_id: str, status: str, expected_digest: str, note: str = "") -> dict:
    from custom_tools.storybook.project_paths import series_scope
    from .project_store import write_json_atomic
    from .series import series_dir

    if status not in ("pending", "approved", "redo") or not isinstance(note, str):
        raise StudioError("invalid_argument", "Проверьте решение и заметку по фильму")
    with series_scope(project_id, series_id), rs._locked(project_id):
        current = _film_review_state(project_id, series_id)
        if current["review"]["input_digest"] != expected_digest:
            raise StudioError("conflict", "Фильм изменился. Просмотрите актуальную версию")
        if status == "approved" and not current["technical_passed"]:
            raise StudioError("invalid_argument", "Принять можно готовый фильм с успешной технической проверкой")
        path = series_dir(project_id, series_id) / ".studio/film_review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"status": status, "note": note, "input_digest": expected_digest})
        return _film_review_state(project_id, series_id)


# ------------------------------------------------------------------ хуки для соседей

def review_counts(project_id: str) -> Optional[dict]:
    """{"approved","pending","redo","rejected"} по шотам Ф6; нет шотов -> None."""
    keys = rs._known_keys(project_id)
    if not keys:
        return None
    return rs.counts_for(rs.read_for_hooks(project_id), keys)


def shot_review_map(project_id: str) -> Optional[dict]:
    """{"2-3": {"status","status_title","note"}} для карточек Ф6; нет файла -> None."""
    shots = rs.read_for_hooks(project_id)
    if not shots:
        return None
    return {k: {"status": e["status"], "status_title": status_title(e["status"]), "note": e.get("note") or ""}
            for k, e in shots.items()}


def prepare_regeneration(project_id: str, shot_keys: Any, what: str, *, steps_to_run: Optional[list] = None,
                         abandon_pending_jobs: bool = False, image_parts: Any = ("start", "end"),
                         surface: Optional[str] = None) -> dict:
    """Хук Ф4: вызывается под замком проекта перед движком (F7 §5.4)."""
    return rg.prepare_regeneration(project_id, shot_keys, what, steps_to_run=steps_to_run,
                                   abandon_pending_jobs=abandon_pending_jobs, image_parts=image_parts,
                                   surface=surface)


def finish_regeneration(project_id: str, regen_id: str, *, since_ts: Optional[float] = None,
                        outcome: str = "") -> dict:
    """Хук Ф4: в finally под замком, после shots.reconcile_after_run."""
    return rg.finish_regeneration(project_id, regen_id, since_ts=since_ts, outcome=outcome)


def protect_before_run(project_id: str, steps_to_run: Optional[list] = None, *,
                       surface: Optional[str] = None) -> dict:
    """Хук Ф4 (P2): перед обычным запуском копирует устаревшие части шотов в версии."""
    return rg.protect_before_run(project_id, steps_to_run, surface=surface)


# ------------------------------------------------------------------ ревью

def _known_key(project_id: str, key: Any) -> str:
    return rs.check_keys(project_id, [key])[0]


@capability("review_get", title="Ревью шотов", access="read", category="review")
def review_get(project_id: str, key: str = "") -> dict:
    doc, corrupt = rs.load(project_id)
    known = rs._known_keys(project_id)
    shots = doc["shots"]
    base = {"digest": rs.doc_digest(doc), "counts": rs.counts_for(shots, known), "titles": dict(STATUS_TITLES),
            "corrupt": corrupt}
    if key:
        one = _known_key(project_id, key)
        return success(**base, key=one, shots={one: rs.view(shots.get(one), history=True)},
                       history=list((shots.get(one) or {}).get("history") or []))
    return success(**base, shots={k: rs.view(shots.get(k)) for k in known})


@capability("review_set_status", title="Статус ревью шотов", access="write", category="review")
def review_set_status(project_id: str, keys: list, status: str, expected_digests: Optional[dict] = None,
                      note: Optional[str] = None, surface: str = "") -> dict:
    return success(**rs.set_status(project_id, keys, status, expected_digests=expected_digests, note=note,
                                   by=rs.surface_of(surface)))


@capability("review_set_note", title="Заметка к шоту", access="write", category="review")
def review_set_note(project_id: str, key: str, note: str, expected_digest: str, surface: str = "") -> dict:
    return success(**rs.set_note(project_id, key, note, expected_digest, by=rs.surface_of(surface)))


# ------------------------------------------------------------------ версии

def _selected_take(project_id: str, key: str) -> Optional[str]:
    return (rs.read_for_hooks(project_id).get(key) or {}).get("selected_take")


@capability("versions_list", title="Версии шотов", access="read", category="review")
def versions_list(project_id: str, key: str = "", kind: str = "shot") -> dict:
    if kind not in ("shot", "music", "film"):
        raise StudioError("invalid_argument", "kind: shot, music или film")
    recovery = None
    try:
        res = rg.recover_pending(project_id)
        recovery = res if res.get("recovered") else None
    except Exception:  # noqa: BLE001 - чтение не должно падать из-за чистки
        logger.exception("recover_pending")
    if kind != "shot":
        return success(kind=kind, versions=sv.area_versions(project_id, kind), usage=sv.usage(project_id)["usage"],
                       **({"recovery": recovery} if recovery else {}))
    if not key:
        info = sv.usage(project_id)
        return success(shots=info["shots"], music={"count": info["music"]["count"]},
                       film={"count": info["film"]["count"]}, usage=info["usage"],
                       **({"recovery": recovery} if recovery else {}))
    one = _known_key(project_id, key)
    selected = _selected_take(project_id, one)
    versions = sv.iter_versions(project_id, one)
    cur = sv.current_prompts(project_id, one)  # один раз на весь список
    views = [sv.version_view(project_id, one, v, n, selected, cur) for n, v in enumerate(versions, start=1)]
    views.reverse()  # новые сверху; номер остаётся порядковым по времени
    return success(key=one, versions=views, current=sv.current_view(project_id, one), selected=selected,
                   usage=sv.usage(project_id)["usage"], **({"recovery": recovery} if recovery else {}))


def _side(project_id: str, key: str, ident: str, numbers: dict, selected: Optional[str]) -> dict:
    if ident == "current":
        cur = sv.current_view(project_id, key)
        return {"id": "current", "parts": cur["parts"], "tokens": cur["tokens"], "prompts": cur["prompts"],
                "prompts_trusted": True}
    version = sv.find_version(project_id, key, ident)
    return sv.version_view(project_id, key, version, numbers[ident], selected)


@capability("versions_compare", title="Сравнение версий шота", access="read", category="review")
def versions_compare(project_id: str, key: str, left: str, right: str) -> dict:
    one = _known_key(project_id, key)
    if left == right:
        raise StudioError("invalid_argument", "Выберите две разные версии")
    numbers = {v["id"]: n for n, v in enumerate(sv.iter_versions(project_id, one), start=1)}
    selected = _selected_take(project_id, one)
    a, b = (_side(project_id, one, x, numbers, selected) for x in (left, right))
    pa, pb = a["parts"], b["parts"]
    exists = lambda side, part: bool((side.get(part) or {}).get("exists"))  # noqa: E731
    prompt_changed = {}
    names = {"start": ("start", "negative_start"), "end": ("end", "negative_end"), "video": ("video",)}
    for part in sv.PARTS:
        prompt_changed[part] = any((a["prompts"].get(n) or "") != (b["prompts"].get(n) or "") for n in names[part])
    size_delta = {part: (pb.get(part) or {}).get("size", 0) - (pa.get(part) or {}).get("size", 0)
                  for part in sv.PARTS if exists(pa, part) and exists(pb, part)}
    diff = {"prompt_changed": prompt_changed, "size_delta": {k: v or 0 for k, v in size_delta.items()},
            "parts_only_left": [p for p in sv.PARTS if exists(pa, p) and not exists(pb, p)],
            "parts_only_right": [p for p in sv.PARTS if exists(pb, p) and not exists(pa, p)]}
    return success(key=one, left=a, right=b, diff=diff)


def _check_token(project_id: str, version: dict, parts: list, expected: Any) -> None:
    if expected in (None, "", {}):
        return
    root = project_dir(project_id)
    for index, part in enumerate(parts):
        want = expected.get(part) if isinstance(expected, dict) else (expected if index == 0 else None)
        if want is None:
            continue
        path = sv.safe_origin(root, version["meta"]["parts"][part])
        if not path.is_file() or sv.file_token(path) != want:
            raise StudioError("conflict", "Текущий файл изменился после сравнения. Обновите список версий",
                              reason="current_changed", part=part)


def _restore_warnings(project_id: str, key: str, version: dict, parts: list) -> list[dict]:
    root = project_dir(project_id)
    meta = version["meta"]
    out: list[dict] = []
    shots = _optional("shots")
    if any(p in parts for p in ("start", "end")) and "video" not in parts:
        video = (shots.shot_files(project_id, key).get("video") if shots else None)
        record = shots.get_baseline_record(project_id, key, "video") if (shots and video) else None
        fp = _mod("_shots_fp")
        new_start = fp.h16(sv.file_token(root / meta["parts"]["start"]["origin"])) if "start" in parts else None
        if video and (record is None or (new_start is not None and
                                          (record.get("components") or {}).get("start_img") != new_start)):
            out.append({"code": "video_made_from_other_start",
                        "message": "Видео сделано по другой картинке начала"})
    if any(v is False for v in sv.matches_current(project_id, key, meta.get("prompts") or {}, parts).values()):
        out.append({"code": "prompt_differs", "message": "Промпты версии отличаются от текущих"})
    if "video" in parts:
        job = sv.last_job(root, sv.unit_keys_for(root, key))
        stored = (meta.get("job") or {}).get("input_hash")
        if job and stored and job.get("input_hash") != stored:
            out.append({"code": "provider_job_mismatch",
                        "message": "Запись задачи провайдера относится к другому ролику"})
    if (root / CONTINUITY_PLAN).is_file():
        for part in parts:
            info = meta["parts"][part]
            if part != "video" and len(info.get("files") or []) < 2:
                out.append({"code": "regeneration_may_overwrite",
                            "message": "Для картинки нет подтверждения непрерывности: обычный запуск может "
                                       "нарисовать её заново (копия сохранится)"})
                break
    return out


@capability("versions_restore", title="Вернуть версию шота", access="write", category="review")
def versions_restore(project_id: str, key: str, version_id: str, parts: Optional[list] = None,
                     expected_token: Any = None, surface: str = "") -> dict:
    one = _known_key(project_id, key)
    if version_id == "current":
        raise StudioError("invalid_argument", "Текущая версия уже на месте")
    version = sv.find_version(project_id, one, version_id)
    available = [p for p in sv.PARTS if p in (version["meta"].get("parts") or {})]
    if parts is None:
        chosen = available
    else:
        if not isinstance(parts, list) or not parts or any(p not in available for p in parts):
            raise StudioError("invalid_argument", "parts: части, которые есть в версии: " + ", ".join(available))
        chosen = [p for p in sv.PARTS if p in parts]
    root = project_dir(project_id)
    handle = rg.acquire_lock(project_id)
    try:
        rg.recover_pending(project_id, lock_held=True)
        sv.verify_version(version, chosen)
        current = sv.collect_by_origin(project_id, version, chosen)  # снимок по всем выбранным частям
        need = 2 * (sum(sum(p.stat().st_size for p in i["files"]) for i in current.values())
                    + sum(version["meta"]["parts"][p].get("size") or 0 for p in chosen))
        sv.check_space(root, need)
        _check_token(project_id, version, chosen, expected_token)
        ops = sv.plan_shot_ops(project_id, one, current, mode="copy", reason="restore", restored_from=version_id,
                               surface=rs.surface_of(surface))
        for op in ops:
            sv.apply_op(project_id, op)
        snapshot_id = ops[0]["version_id"] if ops else None
        done: list[str] = []
        part = None
        try:
            for part in chosen:  # фиксированный порядок start, end, video
                sv.replace_from_version(project_id, version, [part])
                done.append(part)
        except Exception as exc:  # noqa: BLE001
            failed = part
            for op in ops:  # вернуть уже заменённые части из снимка текущего (только те, что в снимке)
                back = [p for p in done if p in op["parts"]]
                if back:
                    try:
                        sv.replace_from_version(project_id, {"dir": root / op["version_rel"], "meta": op["meta"]},
                                                back)
                    except Exception:  # noqa: BLE001
                        logger.exception("откат возврата версии")
            raise StudioError("unavailable", f"Не удалось вернуть часть «{failed}»: {exc}. Изменения отменены",
                              reason="restore_failed", part=failed) from exc
        shots = _optional("shots")
        fpm = _mod("_shots_fp")
        for part in chosen:
            record = (version["meta"].get("baseline") or {}).get(part)
            if shots is not None and isinstance(record, dict):
                shots.set_baseline_record(project_id, one, part, record, source="restored")
            elif shots is not None:  # у версии нет отпечатка: старый отпечаток описывал другой файл
                fpm.update_records(project_id, lambda recs, p=part: (recs.get(one) or {}).pop(p, None))
        keys = sorted({k for p in chosen for k in (version["meta"]["parts"][p].get("unit_keys") or [one])})
        reset = rs.mark_content_changed(project_id, keys, "restored:" + "+".join(chosen), take=version_id)
        freshness: dict = {}
        if shots is not None:
            try:
                card = shots.shot_get(project_id, one)["shot"]
                freshness = {p: (card["parts"].get(p) or {}).get("freshness") for p in chosen}
            except Exception:  # noqa: BLE001
                logger.exception("freshness after restore")
        warnings = _restore_warnings(project_id, one, version, chosen)
    finally:
        rg.release_lock(handle)
    return success(restored=chosen, snapshot_version_id=snapshot_id, review_reset=reset, freshness=freshness,
                   warnings=warnings)


# ------------------------------------------------------------------ перегенерация: предпросмотр

KIND_BY_WHAT = {"images": ["images"], "video": ["video"], "both": ["images", "video"]}
SINGLE_STEP = {"music": rg.MUSIC_STEP, "film": rg.FILM_STEP}


def _runs():
    runs = _optional("runs")
    if runs is None or not hasattr(runs, "run_plan_get") or not hasattr(runs, "run_start"):
        raise StudioError("unavailable", "Запуск прогона недоступен")
    return runs


def _estimate(project_id: str, plan: dict, keys: list) -> Optional[dict]:
    try:
        settings = _mod("run_settings")
        res = settings.cost_estimate_get(project_id, step_ids=list(plan.get("steps_to_run") or []),
                                         shot_keys=keys or None)
    except Exception as exc:  # noqa: BLE001
        logger.info("Оценка стоимости недоступна: %s", exc)
        return None
    return {k: v for k, v in res.items() if k != "status"} if isinstance(res, dict) else None


def _confirm(what: str, estimate: Optional[dict]) -> dict:
    if what == "film":
        return {"cost_rub": 0.0, "unknown_cost": False}
    if estimate is None:
        return {"cost_rub": 0.0, "unknown_cost": True}
    unknown = [u for u in estimate.get("unknown_components") or [] if u not in BUDGET_STEP_IGNORED]
    return {"cost_rub": float(estimate.get("total_known_rub") or 0.0), "unknown_cost": bool(unknown)}


def _canonical_digest(body: dict) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _supports_force_regenerate(runs) -> bool:
    return "force_regenerate" in inspect.signature(runs.run_start).parameters


def _preview(project_id: str, what: str, shot_keys: Any, rebuild_film: bool, image_parts: Any) -> dict:
    what = rg.check_what(what)
    parts = rg.check_image_parts(image_parts)
    runs = _runs()
    try:
        rg.recover_pending(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("recover_pending")
    keys = rg.check_keys(project_id, shot_keys) if what in ("images", "video", "both") else []
    film = bool(rebuild_film) and what in ("images", "video", "both")
    probe = rg.dry_run(project_id, keys, what, image_parts=parts, with_film=film)
    expanded = probe["expanded_keys"]
    if what in ("music", "film"):
        run_args = {"mode": "single_step", "step_id": SINGLE_STEP[what]}
    else:
        run_args = {"mode": "regenerate_shots", "kinds": KIND_BY_WHAT[what], "shot_keys": expanded,
                    "rebuild_film": film}
    plan = runs.run_plan_get(project_id, **run_args)["plan"]
    problems = [dict(p) for p in plan.get("problems") or []]
    pending = probe["pending_paid_jobs"]
    if pending:
        problems.append({"level": "warning", "code": "paid_job_pending",
                         "message": "По части шотов уже отправлены оплаченные задачи. Дождитесь их результата, "
                                    "иначе вы заплатите дважды"})
    if rg.has_live_journal(project_id):
        problems.append({"level": "error", "code": "regeneration_pending",
                         "message": "Предыдущая перегенерация ещё не завершена"})
    if what == "music" and not _supports_force_regenerate(runs):
        problems.append({"level": "warning", "code": "force_regenerate_unavailable",
                         "message": "С тем же промптом музыка может вернуться из кэша: запуск пока не передаёт "
                                    "параметр принудительной генерации"})
    estimate = None if what == "film" else _estimate(project_id, plan, expanded)
    if estimate and estimate.get("budget_verdict") == "short":
        problems.append({"level": "warning", "code": "budget_short",
                         "message": "Бюджет прогона меньше оценки; отправку остановит охранник бюджета"})
    confirm = _confirm(what, estimate)
    steps = list(plan.get("steps_to_run") or [])
    pending_keys = sorted({str(p["shot_key"]) for p in pending})
    can_start = bool(plan.get("can_start")) and not any(p.get("level") == "error" for p in problems)
    digest = _canonical_digest({"what": what, "expanded_keys": expanded, "steps": steps,
                                "run_plan": plan.get("plan_digest"), "cost": confirm["cost_rub"],
                                "unknown": confirm["unknown_cost"], "pending": pending_keys})
    return {
        "plan_digest": digest, "can_start": can_start, "problems": problems, "expanded_keys": expanded,
        "linked_keys": probe["linked_keys"], "steps": steps, "will_snapshot": probe["will_snapshot"],
        "disk_needed_bytes": probe["disk_needed_bytes"], "pending_paid_jobs": pending, "estimate": estimate,
        "confirm_required": confirm,
        "run_plan": {k: plan.get(k) for k in ("mode", "steps_to_run", "plan_digest", "can_start", "scope",
                                              "rebuild_film")},
        "_run_args": run_args, "_parts": parts, "_keys": keys,
    }


@capability("regenerate_preview", title="Предпросмотр перегенерации", access="read", category="review")
def regenerate_preview(project_id: str, what: str, shot_keys: Optional[list] = None, rebuild_film: bool = False,
                       image_parts: Optional[list] = None) -> dict:
    data = _preview(project_id, what, shot_keys, rebuild_film, image_parts)
    return success(**{k: v for k, v in data.items() if not k.startswith("_")})


# ------------------------------------------------------------------ перегенерация: старт

def _money_changed(known: float, confirmed: Any) -> bool:
    try:
        return float(confirmed) < known - 0.5
    except (TypeError, ValueError):
        return True


@capability("regenerate_start", title="Запуск перегенерации", access="run", category="review")
def regenerate_start(project_id: str, what: str, launcher: str, expected_plan_digest: str,
                     shot_keys: Optional[list] = None, rebuild_film: bool = False,
                     image_parts: Optional[list] = None, confirm_cost_rub: Optional[float] = None,
                     confirm_unknown_cost: bool = False, abandon_pending_jobs: bool = False) -> dict:
    if launcher not in ("backend", "local"):
        raise StudioError("invalid_argument", "launcher: backend или local")
    if confirm_cost_rub is not None:
        try:
            finite = not isinstance(confirm_cost_rub, bool) and math.isfinite(float(confirm_cost_rub))
        except (TypeError, ValueError):
            finite = True  # нечисловое подтверждение даст cost_changed ниже
        if not finite:
            raise StudioError("invalid_argument", "confirm_cost_rub: конечное число")
    data = _preview(project_id, what, shot_keys, rebuild_film, image_parts)
    if expected_plan_digest != data["plan_digest"]:
        raise StudioError("conflict", "План изменился с момента показа. Проверьте его ещё раз",
                          reason="plan_changed", plan_digest=data["plan_digest"])
    errors = [p for p in data["problems"] if p.get("level") == "error"]
    if any(p.get("code") == "regeneration_pending" for p in errors):
        raise StudioError("conflict", "Предыдущая перегенерация ещё не завершена", reason="regeneration_pending")
    for p in errors:
        if p.get("code") == "project_busy":
            raise StudioError("locked", p.get("message") or "Проект занят прогоном")
    if errors:
        raise StudioError("conflict", errors[0].get("message") or "Запуск невозможен", reason="plan_blocked",
                          problems=errors)
    known = data["confirm_required"]["cost_rub"]
    if known > 0:
        if confirm_cost_rub is None:
            raise StudioError("conflict", "Подтвердите сумму перегенерации",
                              reason="cost_confirmation_required", cost_rub=known)
        if _money_changed(known, confirm_cost_rub):
            raise StudioError("conflict", "Стоимость изменилась. Проверьте сумму ещё раз", reason="cost_changed",
                              cost_rub=known)
    if data["confirm_required"]["unknown_cost"] and not confirm_unknown_cost:
        raise StudioError("conflict", "Цена части работ не оценена: подтвердите, что согласны",
                          reason="cost_confirmation_required", unknown_cost=True)
    if data["pending_paid_jobs"] and not abandon_pending_jobs:
        raise StudioError("conflict", "По шоту уже отправлена оплаченная задача. Сначала дождитесь её результата, "
                          "иначе вы заплатите дважды", reason="paid_job_pending",
                          keys=sorted({str(p["shot_key"]) for p in data["pending_paid_jobs"]}))
    runs = _runs()
    surface = "tk" if launcher == "local" else "react"
    start_args: dict = {**data["_run_args"], "launcher": launcher, "expected_plan_digest": data["run_plan"]["plan_digest"]}
    # необязательные аргументы отдаём, только если run_start их принимает (Ф4 добавляет по договору)
    optional = {"abandon_pending_jobs": bool(abandon_pending_jobs), "image_parts": list(data["_parts"])}
    accepted = inspect.signature(runs.run_start).parameters
    start_args.update({k: v for k, v in optional.items() if k in accepted and what in ("images", "video", "both")})
    if abandon_pending_jobs and "abandon_pending_jobs" not in accepted:
        raise StudioError("unavailable", "Запуск пока не умеет бросать оплаченные задачи: дождитесь их результата")
    if (what in ("images", "both") and tuple(data["_parts"]) != rg.ALL_IMAGE_PARTS
            and "image_parts" not in accepted):
        raise StudioError("unavailable", "Запуск пока не умеет перерисовывать только часть кадров: "
                          "выберите оба кадра (начало и конец)")
    if what not in ("images", "video", "both"):
        if what == "music" and "force_regenerate" in accepted:
            start_args["force_regenerate"] = True
    created = _snapshot_single_step(project_id, what, surface) if what in SINGLE_STEP else []
    try:
        result = runs.run_start(project_id, **start_args)
    except BaseException:
        _drop_snapshots(project_id, created)
        raise
    if isinstance(result, dict) and result.get("status") == "error":
        _drop_snapshots(project_id, created)
        err = result.get("error") if isinstance(result.get("error"), dict) else result
        raise StudioError(err.get("code") or "internal", err.get("message") or "Запуск не удался",
                          **(err.get("details") or {}))
    return success(run_id=result.get("run_id"), workflow_id=result.get("workflow_id"),
                   regen_pending=what not in SINGLE_STEP)


def _drop_snapshots(project_id: str, rels: list) -> None:
    """Запуск не состоялся: убираем только что созданные копии (оригиналы на месте)."""
    root = project_dir(project_id)
    for rel in rels:
        shutil.rmtree(root / rel, ignore_errors=True)


def _snapshot_single_step(project_id: str, what: str, surface: str) -> list:
    """Музыка и фильм: копия в версии под замком, пока запуск single_step сам хук не вызывает."""
    handle = rg.acquire_lock(project_id)
    try:
        res = prepare_regeneration(project_id, [], what, surface=surface)
        journal = rg.read_journal(project_id) if res.get("status") != "error" else None
        created = [op["version_rel"] for op in (journal or {}).get("ops") or []]
        if journal is not None:
            rg.drop_journal(project_id)  # копии откатывать не нужно: версии остаются, журнал лишний
    finally:
        rg.release_lock(handle)
    if res.get("status") == "error":
        err = res.get("error") or {}
        raise StudioError(err.get("code", "internal"), err.get("message", "Подготовка не удалась"),
                          **(err.get("details") or {}))
    return created
