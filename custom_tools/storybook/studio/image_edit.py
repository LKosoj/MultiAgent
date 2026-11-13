"""Правка одной картинки ИИ (R3): предпросмотр и фоновый запуск.

Цель правки: картинка «Начало»/«Конец» шота или иллюстрация из 50_images. Инструмент
`edit_image_vse_tool` пишет результат во временный файл, и только готовый, проверенный
файл ставится на место цели. Перед правкой прежняя картинка сохраняется: для шота это
версия (её возвращает `versions_restore`), для иллюстрации копия в `.studio/image_edit_backups`.
Права: только admin, как у остальных storybook.*.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from custom_tools.storybook.project_paths import storybook_projects_root

from . import jobs
from . import regeneration as rg
from . import review_state as rs
from . import shot_versions as sv
from .capabilities import capability
from .errors import StudioError, success
from .paths import project_dir, project_relpath, resolve_in_project
from .reports import parse_shot_key

logger = logging.getLogger(__name__)

JOB_KIND = "image_edit"
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
PROMPT_MIN = 5
PROMPT_MAX = 2000
REFERENCES_MAX = 3
PARTS = ("start", "end")
ILLUSTRATIONS_DIR = "50_images"
BACKUPS_REL = ".studio/image_edit_backups"
TMP_REL = ".studio/tmp"
MONEY_TOLERANCE_RUB = 0.5
FAILED_TEXT = "Не удалось исправить картинку. Прежняя осталась на месте"

_mod = sv._mod


# ------------------------------------------------------------------ цель и образцы

def _check_extension(path: Path) -> None:
    if path.suffix.lower() not in EXTENSIONS:
        raise StudioError("invalid_argument", "Править можно только PNG, JPG и WEBP")


def _resolve_target(project_id: str, shot_key: str, part: str, path: str) -> dict:
    by_shot = bool(shot_key) or bool(part)
    by_path = bool(path)
    if by_shot == by_path:
        raise StudioError("invalid_argument", "Укажите либо шот и часть (shot_key, part), либо путь (path)")
    root = project_dir(project_id).resolve()
    if by_shot:
        key = parse_shot_key(shot_key)
        if key is None:
            raise StudioError("invalid_argument", "shot_key: ключ вида 2-3")
        if part not in PARTS:
            raise StudioError("invalid_argument", "part: start или end")
        absolute = _mod("shots").shot_files(project_id, key).get(part)
        if absolute is None:
            raise StudioError("not_found", "Файла этой части шота нет")
        absolute = Path(absolute)
        kind = "shot"
    else:
        absolute = resolve_in_project(project_id, path)
        key, part, kind = "", "", "illustration"
        # shared/references серии лежит выше корня серии: править его нельзя, только брать образцом
        inside = absolute.relative_to(root).parts if absolute.is_relative_to(root) else ()
        if len(inside) < 2 or inside[0] != ILLUSTRATIONS_DIR:
            raise StudioError("invalid_argument", "Править можно иллюстрации из папки 50_images")
    _check_extension(absolute)
    return {"kind": kind, "abs": absolute, "path": project_relpath(project_id, absolute), "name": absolute.name,
            "shot_key": key, "part": part}


def _resolve_references(project_id: str, target: dict, reference_paths: Any) -> list[dict]:
    if reference_paths is None:
        return []
    if not isinstance(reference_paths, (list, tuple)) or any(not isinstance(p, str) for p in reference_paths):
        raise StudioError("invalid_argument", "reference_paths: список путей к картинкам")
    if len(reference_paths) > REFERENCES_MAX:
        raise StudioError("invalid_argument", f"Образцов не больше {REFERENCES_MAX}")
    out: list[dict] = []
    seen = {target["path"]}
    for raw in reference_paths:
        absolute = resolve_in_project(project_id, raw)
        rel = project_relpath(project_id, absolute)
        if rel.startswith(".studio/"):
            raise StudioError("invalid_argument", "Служебные файлы нельзя брать образцом")
        _check_extension(absolute)
        if rel in seen:  # дубль и сама правимая картинка образцом не считаются
            continue
        seen.add(rel)
        out.append({"path": rel, "name": absolute.name, "abs": absolute})
    return out


# ------------------------------------------------------------------ предпросмотр

def _price() -> Optional[float]:
    from . import cost_estimate
    return cost_estimate._flat_price("images", "per_image_rub")


def _target_view(target: dict) -> dict:
    st = target["abs"].stat()
    return {"kind": target["kind"], "path": target["path"], "name": target["name"],
            "shot_key": target["shot_key"], "part": target["part"], "size": st.st_size,
            "modified_ts": st.st_mtime_ns / 1e9}


def _is_running(project_id: str) -> bool:
    return project_id in jobs.active_project_ids(storybook_projects_root(), JOB_KIND)


def _shot_problems(project_id: str, target: dict) -> list[dict]:
    key, part = target["shot_key"], target["part"]
    out: list[dict] = []
    root = project_dir(project_id)
    try:
        if part == "start" and rg.pending_paid_jobs(root, [key]):
            out.append({"level": "warning", "code": "paid_job_pending",
                        "message": "По шоту уже отправлена оплаченная видеозадача. Правка стартовой картинки "
                                   "сделает её результат устаревшим"})
    except Exception:  # noqa: BLE001
        logger.exception("image_edit: paid jobs")
    try:
        _, frames = rg.image_frames(project_id)
        if part == "end":
            linked = rg.linked_closure(frames, [key], ("end",))
            if linked:
                out.append({"level": "warning", "code": "linked_part",
                            "message": "Конец этого шота копируется как начало шота "
                                       + ", ".join(linked) + ". Копии правка не затронет"})
        elif any(rg._frame_key(f) == key and str(f.get("shot_type") or "start").lower() == "start"
                 and f.get("copy_from_previous_end") for f in frames):
            out.append({"level": "warning", "code": "linked_part",
                        "message": "Начало этого шота скопировано из конца предыдущего. Исходный конец правка "
                                   "не затронет"})
    except StudioError:
        pass
    return out


def _preview(project_id: str, shot_key: str, part: str, path: str, reference_paths: Any) -> dict:
    target = _resolve_target(project_id, shot_key, part, path)
    refs = _resolve_references(project_id, target, reference_paths)
    price = _price()
    estimate = {"cost_rub": float(price) if price is not None else 0.0, "unknown_cost": price is None,
                "price_per_image_rub": price}
    problems: list[dict] = []
    if _is_running(project_id):
        problems.append({"level": "error", "code": "edit_running",
                         "message": "В этом проекте уже идёт правка картинки"})
    elif rg.lock_held(project_id):
        problems.append({"level": "error", "code": "project_busy",
                         "message": "Проект занят: идёт прогон или другая правка"})
    if rg.has_live_journal(project_id):
        problems.append({"level": "error", "code": "regeneration_pending",
                         "message": "Предыдущая перегенерация ещё не завершена"})
    if target["kind"] == "shot":
        problems += _shot_problems(project_id, target)
    token = sv.file_token(target["abs"])
    body = {"target": target["path"], "token": token, "refs": [[r["path"], sv.file_token(r["abs"])] for r in refs],
            "cost": estimate["cost_rub"], "unknown": estimate["unknown_cost"]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "target": _target_view(target),
        "references": [{"path": r["path"], "name": r["name"]} for r in refs],
        "limits": {"prompt_min": PROMPT_MIN, "prompt_max": PROMPT_MAX, "references_max": REFERENCES_MAX,
                   "extensions": list(EXTENSIONS)},
        "estimate": estimate,
        "problems": problems,
        "can_start": not any(p["level"] == "error" for p in problems),
        "plan_digest": digest,
        "_target": target, "_refs": refs, "_token": token,
    }


@capability("image_edit_preview", title="Предпросмотр правки картинки", access="read", category="media")
def image_edit_preview(project_id: str, shot_key: str = "", part: str = "", path: str = "",
                       reference_paths: Optional[list] = None, prompt: str = "") -> dict:
    if not isinstance(prompt, str):
        raise StudioError("invalid_argument", "prompt: строка")
    data = _preview(project_id, shot_key, part, path, reference_paths)
    return success(**{k: v for k, v in data.items() if not k.startswith("_")})


# ------------------------------------------------------------------ запуск

def _check_prompt(prompt: Any) -> str:
    text = prompt.strip() if isinstance(prompt, str) else ""
    if len(text) < PROMPT_MIN:
        raise StudioError("invalid_argument", f"Опишите изменение: не меньше {PROMPT_MIN} символов")
    if len(text) > PROMPT_MAX:
        raise StudioError("invalid_argument", f"Описание не длиннее {PROMPT_MAX} символов")
    return text


def _check_confirm(value: Any) -> None:
    """confirm_cost_rub: None или конечное число; NaN и бесконечность - ошибка запроса."""
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return  # нечисловое подтверждение даст cost_changed
    if isinstance(value, bool) or not math.isfinite(number):
        raise StudioError("invalid_argument", "confirm_cost_rub: конечное число")


def _money_changed(known: float, confirmed: Any) -> bool:
    try:
        return float(confirmed) < known - MONEY_TOLERANCE_RUB
    except (TypeError, ValueError):
        return True


def _check_start(data: dict, expected_plan_digest: str, confirm_cost_rub: Any, confirm_unknown_cost: bool) -> None:
    if expected_plan_digest != data["plan_digest"]:
        raise StudioError("conflict", "Картинка или цена изменились. Проверьте ещё раз",
                          reason="plan_changed", plan_digest=data["plan_digest"])
    errors = {p["code"]: p for p in data["problems"] if p["level"] == "error"}
    for code in ("edit_running", "regeneration_pending"):
        if code in errors:
            raise StudioError("conflict", errors[code]["message"], reason=code)
    if "project_busy" in errors:
        raise StudioError("locked", errors["project_busy"]["message"])
    estimate = data["estimate"]
    if estimate["unknown_cost"]:
        if not confirm_unknown_cost:
            raise StudioError("conflict", "Подтвердите правку без оценки цены",
                              reason="cost_confirmation_required", unknown_cost=True)
        return
    known = estimate["cost_rub"]
    if known > 0:
        if confirm_cost_rub is None:
            raise StudioError("conflict", "Подтвердите сумму правки",
                              reason="cost_confirmation_required", cost_rub=known)
        if _money_changed(known, confirm_cost_rub):
            raise StudioError("conflict", "Стоимость изменилась. Проверьте сумму ещё раз",
                              reason="cost_changed", cost_rub=known)


@capability("image_edit_start", title="Запуск правки картинки", access="run", category="media")
def image_edit_start(project_id: str, prompt: str, expected_plan_digest: str, shot_key: str = "", part: str = "",
                     path: str = "", reference_paths: Optional[list] = None, confirm_cost_rub: Optional[float] = None,
                     confirm_unknown_cost: bool = False, surface: str = "") -> dict:
    text = _check_prompt(prompt)
    _check_confirm(confirm_cost_rub)
    data = _preview(project_id, shot_key, part, path, reference_paths)
    _check_start(data, expected_plan_digest, confirm_cost_rub, bool(confirm_unknown_cost))
    handle = rg.acquire_lock(project_id)  # занято -> locked сразу; отпускает фоновая работа
    try:
        if sv.file_token(data["_target"]["abs"]) != data["_token"]:
            raise StudioError("conflict", "Картинка или цена изменились. Проверьте ещё раз", reason="plan_changed")
        work = _make_work(project_id, text, data["_target"], data["_refs"], rs.surface_of(surface), handle)
        job_id = jobs.start_job(storybook_projects_root(), JOB_KIND, project_id, work)
    except BaseException:
        rg.release_lock(handle)
        raise
    return success(job_id=job_id)


# ------------------------------------------------------------------ фоновая работа

def _translate(prompt: str) -> str:
    """Промпт на английский; сбой перевода не фатален."""
    try:
        from utils import _translate_texts_batch
        translated = _translate_texts_batch([prompt], "en")
        return translated[0] if translated and isinstance(translated[0], str) and translated[0].strip() else prompt
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_edit: перевод промпта не удался, берём исходный текст: %s", exc)
        return prompt


def _backup_illustration(project_id: str, absolute: Path) -> str:
    root = project_dir(project_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = root / BACKUPS_REL / f"{stamp}_{absolute.name}"
    if dst.exists():
        dst = dst.with_name(f"{stamp}_{uuid.uuid4().hex[:6]}_{absolute.name}")
    sv._copy_atomic(absolute, dst)
    return dst.relative_to(root).as_posix()


def _verify_image(path: Path) -> None:
    from PIL import Image
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("файл не создан")
    with Image.open(path) as img:
        img.verify()


def _run_tool(english: str, sources: list[str], output: Path) -> None:
    """Вызвать инструмент и проверить файл; любая беда -> исключение (текст наружу не идёт)."""
    image_tools = importlib.import_module("custom_tools.image_tools")
    answer = image_tools.edit_image_vse_tool(prompt=english, image_paths=sources, session_id=str(uuid.uuid4()),
                                             output_path=str(output))
    if isinstance(answer, str) and answer.lstrip().lower().startswith("ошибка"):
        raise RuntimeError(answer)
    _verify_image(output)


def _make_work(project_id: str, prompt: str, target: dict, refs: list[dict], surface: str, handle):
    def work(progress) -> dict:
        absolute: Path = target["abs"]
        tmp: Optional[Path] = None
        started = time.time()
        try:
            root = project_dir(project_id)  # внутри try: любая ошибка отпускает замок
            tmp = root / TMP_REL / f"{uuid.uuid4().hex}{absolute.suffix.lower()}"
            rg.recover_pending(project_id, lock_held=True)
            if not absolute.is_file():
                raise StudioError("not_found", "Файл картинки пропал")
            progress(0, 3, "Сохраняем прежнюю картинку")
            version_id = None
            if target["kind"] == "shot":
                snap = sv.snapshot_copy(project_id, target["shot_key"], [target["part"]], reason="image_edit",
                                        surface=surface)
                # дубль последней версии копию не создаёт: откат идёт к уже существующей
                version_id = (snap or {}).get("version_id") or _latest_version_id(
                    project_id, target["shot_key"], target["part"])
                backup = None
            else:
                backup = _backup_illustration(project_id, absolute)
            progress(1, 3, "Правим картинку")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            english = _translate(prompt)
            try:
                _run_tool(english, [str(absolute)] + [str(r["abs"]) for r in refs], tmp)
            except Exception:  # noqa: BLE001
                logger.exception("image_edit: инструмент не справился (%s)", target["path"])
                raise StudioError("unavailable", FAILED_TEXT, reason="edit_failed") from None
            progress(2, 3, "Ставим результат на место")
            shutil.copymode(absolute, tmp)
            os.replace(tmp, absolute)
            if target["kind"] == "shot":
                _after_shot_edit(project_id, target, started)
            st = absolute.stat()
            progress(3, 3, "Готово")
            return {"path": target["path"], "name": target["name"], "modified_ts": st.st_mtime_ns / 1e9,
                    "size": st.st_size, "backup": backup, "version_id": version_id,
                    "shot_key": target["shot_key"] or None}
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            rg.release_lock(handle)

    return work


def _latest_version_id(project_id: str, key: str, part: str) -> Optional[str]:
    for version in reversed(sv.iter_versions(project_id, key)):
        if part in (version["meta"].get("parts") or {}):
            return version["id"]
    return None


def _after_shot_edit(project_id: str, target: dict, started: float) -> None:
    """Правка не должна делать картинку «устаревшей» сама по себе; ревью сбрасывается в «Ожидает»."""
    key = target["shot_key"]
    shots = _mod("shots")
    try:
        shots.reconcile_after_run(project_id, started, [key])
    except Exception:  # noqa: BLE001
        logger.exception("image_edit: отпечаток после правки")
    try:
        rs.mark_content_changed(project_id, [key], "image_edited")
    except Exception:  # noqa: BLE001
        logger.exception("image_edit: сброс ревью")
