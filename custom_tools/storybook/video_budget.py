"""Денежный бюджет видео: проверка перед платной отправкой и отчёт при остановке.

Проверка стоит рядом с проверкой по времени (`_require_budget_for_new_video_submission`)
в двух местах платной отправки routerai/aitunnel. Бюджет читается заново при
каждой отправке: изменение во время прогона подхватывается без перезапуска.
Бюджет 0 или «не задан» ничего не читает и ничего не считает — поведение прежнее.

Проверка и запись резерва идут под одним замком: иначе четыре потока
(`max_concurrency`) одновременно увидят «ещё хватает» и оплатят все.
Межпроцессный замок не нужен: проект защищён `acquire_storybook_project_lock`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from custom_tools.storybook import video_pricing, video_run_config

logger = logging.getLogger(__name__)

REPORT_NAME = "budget_report.json"
_REPORT_VERSION = 1
# записи, по которым платная отправка не уходила
_NOT_SENT_STATUSES = {"prepared", "pending"}
_DONE_STATUSES = {"completed", "downloading", "downloaded", "download_failed"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class BudgetExceededError(RuntimeError):
    """Платную отправку нельзя делать: бюджет исчерпан или цена ролика неизвестна."""

    def __init__(self, code: str, message: str, report: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.report = report or {}


def _project_lock(project_dir: Path) -> threading.Lock:
    key = str(project_dir.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _read_jobs(project_dir: Path) -> list:
    path = project_dir / "97_shots" / "provider_jobs.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("provider_jobs.json не прочитан при проверке бюджета: %s", exc)
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _job_amount(job: dict) -> tuple[float, str]:
    """(сумма, вид): фактическая цена в рублях предпочтительнее резерва."""
    cost = _num(job.get("cost"))
    if cost is not None and cost >= 0 and str(job.get("currency") or "").upper() == "RUB":
        return cost, "actual"
    reserved = _num(job.get("reserved_cost_rub"))
    if reserved is not None and reserved > 0:
        return reserved, "reserved"
    return 0.0, "reserved"


def spent_rub(jobs: list) -> dict:
    """Сколько потрачено по журналу задач: `{"total", "actual", "reserved"}`.

    Не считаются записи «не отправлено» (`prepared`/`pending`, а также `failed`
    без task_id и без submitting_at). `submitting` без task_id считается:
    запрос мог уйти и быть оплачен — осторожнее вместо занижения. Записи старых
    версий шотов тоже считаются: деньги уже ушли. Повторные записи одного
    запроса без task_id (повтор после сбоя) считаются один раз.
    """
    groups: dict[tuple, tuple[float, str]] = {}
    for job in jobs:
        status = job.get("status")
        timestamps = job.get("timestamps") if isinstance(job.get("timestamps"), dict) else {}
        task_id = job.get("task_id")
        if status in _NOT_SENT_STATUSES:
            continue
        if status == "failed" and not task_id and not timestamps.get("submitting_at"):
            continue
        amount, kind = _job_amount(job)
        if amount <= 0:
            continue
        key = ("task", task_id) if task_id else ("job", job.get("shot_key"), job.get("input_hash"))
        if key not in groups or amount > groups[key][0]:
            groups[key] = (amount, kind)
    actual = sum(a for a, k in groups.values() if k == "actual")
    reserved = sum(a for a, k in groups.values() if k == "reserved")
    return {"total": actual + reserved, "actual": actual, "reserved": reserved}


def current_budget_rub(project_dir: Path, fallback_budget_rub: float = 0.0) -> float:
    """Бюджет сейчас: файл проекта -> параметр -> запасное значение диспетчера -> 0."""
    stored = video_run_config.stored_budget_rub_at(project_dir)
    if stored is not None:
        return max(stored, 0.0)
    try:
        fallback = float(fallback_budget_rub or 0)
    except (TypeError, ValueError):
        fallback = 0.0
    if fallback > 0:
        return fallback
    from .project_paths import current_series_scope, safe_storybook_project_dir
    scope = current_series_scope()
    project_id = scope[0] if scope and safe_storybook_project_dir(scope[0]) == project_dir.resolve() else project_dir.name
    return video_run_config.budget_fallback(project_id)


def _money(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _report_path(project_dir: Path) -> Path:
    return project_dir / "96_video_contract" / REPORT_NAME


def _write_report(project_dir: Path, *, code: str, provider: str, model: str, budget: float,
                  spent: dict, next_price: Optional[float], price_source: str,
                  shot_key: str, jobs: list, message: str) -> dict:
    path = _report_path(project_dir)
    previous: dict = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        previous = {}
    same_stop = (isinstance(previous, dict) and previous.get("code") == code
                 and previous.get("budget_rub") == budget)
    remaining = list(previous.get("remaining_shot_keys") or []) if same_stop else []
    if shot_key not in remaining:
        remaining.append(shot_key)
    done_shots = sum(1 for job in jobs if job.get("status") in _DONE_STATUSES
                     and (job.get("task_id") or job.get("output_path")))
    report = {
        "version": _REPORT_VERSION,
        "stopped_at": previous.get("stopped_at") if same_stop else
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code": code,
        "provider": provider,
        "model": model,
        "budget_rub": budget,
        "spent_rub": spent["total"],
        "spent_basis": {"actual": spent["actual"], "reserved": spent["reserved"]},
        "next_price_rub": next_price,
        "price_source": price_source,
        "shot_key": shot_key,
        "done_shots": done_shots,
        "remaining_shot_keys": remaining,
        "skipped_after_stop": (int(previous.get("skipped_after_stop") or 0) + 1) if same_stop else 1,
        "message": message,
    }
    try:
        _atomic_write(path, report)
    except OSError as exc:
        logger.warning("budget_report.json не записан: %s", exc)
    return report


def read_report(project_dir: Path) -> Optional[dict]:
    path = _report_path(Path(project_dir))
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _archive_stale_report(project_dir: Path) -> None:
    """Отправка прошла: прежний отчёт об остановке устарел (переносим, не удаляем)."""
    path = _report_path(project_dir)
    if path.is_file():
        try:
            os.replace(path, path.with_name("budget_report.resolved.json"))
        except OSError as exc:
            logger.warning("budget_report.json не перенесён: %s", exc)


@contextmanager
def reserve_budget(project_dir: Any, provider: str, model: str, duration_s: Any, resolution: str,
                   shot_key: str, fallback_budget_rub: float = 0.0) -> Iterator[dict]:
    """Под замком: прочитать бюджет и траты, решить, зарезервировать цену ролика.

    Тело `with` обязано записать резерв в запись задачи (`**reservation`) — замок
    держится до его выхода. Бюджет 0: ничего не читается, `{}`.
    """
    base = Path(project_dir)
    budget = current_budget_rub(base, fallback_budget_rub)
    if budget <= 0:
        yield {}
        return
    with _project_lock(base):
        jobs = _read_jobs(base)
        spent = spent_rub(jobs)
        price = video_pricing.unit_price(provider, model, resolution, jobs)
        clip = video_pricing.clip_price(price, duration_s)
        if clip is None:
            message = (f"Для {provider}/{model} не задана цена за видео: бюджет в рублях проверить нельзя. "
                       "Укажите цену в config/storybook/video_pricing.yaml или поставьте бюджет 0.")
            report = _write_report(base, code="budget_price_unknown", provider=provider, model=model,
                                   budget=budget, spent=spent, next_price=None, price_source=price["source"],
                                   shot_key=shot_key, jobs=jobs, message=message)
            raise BudgetExceededError("budget_price_unknown", message, report)
        if spent["total"] + clip > budget + 1e-9:
            message = (f"Бюджет {_money(budget)} ₽ исчерпан: потрачено {_money(spent['total'])} ₽. "
                       "Новые платные отправки остановлены. Увеличьте бюджет и продолжите.")
            report = _write_report(base, code="budget_exceeded", provider=provider, model=model,
                                   budget=budget, spent=spent, next_price=clip, price_source=price["source"],
                                   shot_key=shot_key, jobs=jobs, message=message)
            raise BudgetExceededError("budget_exceeded", message, report)
        _archive_stale_report(base)
        yield {"reserved_cost_rub": clip, "cost_price_source": price["source"]}
