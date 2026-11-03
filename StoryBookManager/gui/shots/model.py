"""Чистая логика экрана «Кадры» (без tkinter): фильтры, выбор соседа, перегенерация, слияние страниц.

Те же правила реализованы в lib/storybook/shotsView.ts; общие случаи проверяются
векторами tests/fixtures/storybook_shots_vectors.json.
"""

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional

from StoryBookManager.gui.shots import texts as T

PAGE_SIZE = 100
BUSY_RUN_STATES = ("running", "pause_requested", "paused", "stopping")
CLIENT_PREDICATES = ("missing_images", "missing_videos")
KEY_STATUS = {"a": "approved", "r": "redo", "x": "rejected"}
EDIT_FIELDS = ("start_prompt", "end_prompt", "video_prompt", "start_negative_prompt",
               "end_negative_prompt", "duration_s")
REVIEW_ORDER = ("pending", "approved", "redo", "rejected")
STATE_ORDER = ("ready", "stale", "error", "generating", "missing", "partial")


def shot_sort_key(key: str):
    """Числовой порядок ключей: 9-6 раньше 10-1."""
    head, _, tail = str(key).partition("-")
    return (int(head) if head.isdigit() else 0, int(tail) if tail.isdigit() else 0, str(key))


# --- Фильтры ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ListFilters:
    scene: Optional[int] = None
    review: str = ""
    state: str = ""
    stale_only: bool = False
    with_findings: bool = False
    query: str = ""
    predicate: Optional[str] = None  # missing_images / missing_videos: считается на клиенте

    def to_params(self) -> dict:
        """Параметры shots_list: только непустые."""
        out: dict = {}
        if self.scene is not None:
            out["scene"] = self.scene
        if self.review:
            out["review"] = self.review
        if self.state:
            out["state"] = self.state
        if self.stale_only:
            out["stale_only"] = True
        if self.with_findings:
            out["with_findings"] = True
        query = self.query.strip()
        if query:
            out["query"] = query
        return out

    def is_active(self) -> bool:
        return bool(self.to_params()) or self.predicate is not None

    def with_(self, **changes) -> "ListFilters":
        return replace(self, **changes)


def filter_from_intent(name: Optional[str]) -> dict:
    """Фильтр «Обзора» -> параметры сервера; клиентские (missing_*) дают пустой набор."""
    if name == "stale":
        return {"stale_only": True}
    if name == "errors":
        return {"state": "error"}
    if name == "ready":
        return {"state": "ready"}
    if name in ("approved", "pending", "redo"):
        return {"review": name}
    return {}


def client_predicate(name: Optional[str]) -> Optional[str]:
    return name if name in CLIENT_PREDICATES else None


def filters_from_intent(name: Optional[str]) -> ListFilters:
    params = filter_from_intent(name)
    return ListFilters(review=params.get("review", ""), state=params.get("state", ""),
                       stale_only=bool(params.get("stale_only")), predicate=client_predicate(name))


def matches_predicate(row: dict, predicate: Optional[str]) -> bool:
    if not predicate:
        return True
    parts = row.get("parts") or {}
    start, end, video = (parts.get(n) or {} for n in ("start", "end", "video"))
    if predicate == "missing_images":
        return not start.get("exists") or (end.get("state") != "not_applicable" and not end.get("exists"))
    return video.get("state") != "not_applicable" and not video.get("exists") and row.get("state") != "generating"


def apply_predicate(rows: Iterable[dict], predicate: Optional[str]) -> List[dict]:
    return [r for r in rows if matches_predicate(r, predicate)]


# --- Страницы списка -------------------------------------------------------------------

def merge_pages(items: List[dict], incoming: List[dict], offset: int, total: int) -> List[dict]:
    """Страница встаёт на своё место; повторная загрузка той же страницы заменяет её строки."""
    at = min(max(0, offset), len(items))
    merged = items[:at] + list(incoming) + items[at + len(incoming):]
    return merged[:max(0, total)]


def has_more_pages(loaded: int, total: int) -> bool:
    return loaded < total


# --- Ревью и выбор ---------------------------------------------------------------------

def next_status_on_key(current: str, letter: str) -> Optional[str]:
    """Статус после A/R/X: повтор той же клавиши возвращает «Ожидает»."""
    target = KEY_STATUS.get(str(letter).lower())
    if target is None:
        return None
    return "pending" if current == target else target


def neighbor_key(keys: List[str], current: Optional[str], delta: int) -> Optional[str]:
    """Сосед в списке: в конце остаётся последний; без текущего - первый; пустой список даёт None."""
    if not keys:
        return None
    index = keys.index(current) if current in keys else -1
    if index == -1:
        return keys[0]
    return keys[min(len(keys) - 1, max(0, index + delta))]


def bulk_target_keys(order: List[str], marked: Iterable[str], current: Optional[str]) -> List[str]:
    """Над кем работают A/R/X и массовые кнопки: отмеченные (в порядке списка), иначе текущий."""
    marked_set = set(marked)
    if not marked_set:
        return [current] if current else []
    known = set(order)
    rest = sorted((k for k in marked_set if k not in known), key=shot_sort_key)
    return [k for k in order if k in marked_set] + rest


def apply_review_status(rows: List[dict], keys: Iterable[str], status: str, titles: Dict[str, str]) -> List[dict]:
    wanted = set(keys)
    out = []
    for row in rows:
        if row.get("shot_key") in wanted:
            review = dict(row.get("review") or {})
            review.update(status=status, status_title=titles.get(status, status))
            row = {**row, "review": review}
        out.append(row)
    return out


def toast_for_status(status: str, keys: List[str]) -> str:
    """Текст тоста после смены статуса ревью."""
    if len(keys) == 1:
        ids = {"approved": "toast_approved", "redo": "toast_redo", "rejected": "toast_rejected",
               "pending": "toast_pending"}
        return T.fmt(ids[status], key=keys[0])
    if status == "approved":
        return T.fmt("toast_bulk_approved", n=len(keys))
    if status == "redo":
        return T.fmt("toast_bulk_redo", n=len(keys))
    return T.fmt("toast_bulk_rejected" if status == "rejected" else "toast_bulk_pending", n=len(keys))


# --- Карточка --------------------------------------------------------------------------

def form_from_card(card: dict) -> dict:
    start, end, video = card.get("start") or {}, card.get("end") or {}, card.get("video") or {}
    return {
        "start_prompt": start.get("prompt") or "",
        "end_prompt": end.get("prompt") or "",
        "video_prompt": video.get("prompt") or "",
        "start_negative_prompt": start.get("negative_prompt") or "",
        "end_negative_prompt": end.get("negative_prompt") or "",
        "duration_s": video.get("duration_s"),
    }


def dirty_changes(card: dict, form: dict) -> dict:
    """Только реально изменённые и разрешённые поля формы; пустой словарь - сохранять нечего."""
    original = form_from_card(card)
    allowed = set((card.get("editable") or {}).get("fields") or [])
    out = {}
    for name in EDIT_FIELDS:
        value = form.get(name)
        if value is None or name not in allowed:
            continue
        if value != original[name]:
            out[name] = value
    return out


def can_edit(card: dict) -> bool:
    editable = card.get("editable") or {}
    return not editable.get("locked") and bool(editable.get("fields"))


def reapply_draft(fresh_card: dict, mine: dict) -> dict:
    """«Оставить мою правку»: мои изменённые поля поверх свежей карточки."""
    return {**form_from_card(fresh_card), **mine}


def blockout_rel(scene: int, shot: int, part: str) -> str:
    """Файлы болванки лежат по соглашению об именах (сервер отдаёт только признаки наличия)."""
    name = {"start": "ref_start.png", "end": "ref_end.png", "video": "blockout_ref.mp4"}[part]
    return f"93_blockout/scene_{int(scene):02d}_shot_{int(shot):02d}/{name}"


def is_generating(row: dict) -> bool:
    return row.get("state") == "generating" or any(
        (p or {}).get("state") == "generating" for p in (row.get("parts") or {}).values())


def shared_video_keys(card_or_row: dict) -> List[str]:
    """Ключи шотов, на которые рассчитан один общий ролик (пусто, если ролик свой)."""
    unit = (((card_or_row.get("parts") or {}).get("video")) or {}).get("unit") or {}
    keys = unit.get("keys") or []
    return list(keys) if len(keys) > 1 else []


def stale_lines(card: dict) -> List[str]:
    """Строки блока «устарел»: причины по частям, каскад, зависимость от соседей."""
    lines: List[str] = []
    for part, reasons in (card.get("stale_reasons") or {}).items():
        titles = ", ".join(T.reason_title(r) for r in reasons) or T.SHOT_TEXTS["stale_title"].lower()
        lines.append(f"{T.part_title(part)}: {titles}")
    after = ((card.get("cascade") or {}).get("stale_after_regeneration")) or []
    if after:
        lines.append(T.fmt("stale_after", parts=", ".join(T.part_title(p) for p in after)))
    upstream = card.get("upstream_stale_keys") or []
    if upstream:
        lines.append(T.fmt("upstream_stale", keys=", ".join(upstream)))
    return lines


# --- Прогон и блокировки ---------------------------------------------------------------

def is_run_busy(state: Optional[str]) -> bool:
    return bool(state) and state in BUSY_RUN_STATES


def regen_block_reason(run_state: Optional[str], row: Optional[dict]) -> str:
    """Почему «Перегенерировать…» отключена; пустая строка - можно."""
    if is_run_busy(run_state):
        return T.SHOT_TEXTS["busy_run"]
    if row is not None and row.get("state") == "generating":
        return T.SHOT_TEXTS["busy_shot"]
    return ""


# --- Перегенерация ---------------------------------------------------------------------

def default_what(stale_parts: List[str], missing_parts: List[str]) -> str:
    bad = set(stale_parts) | set(missing_parts)
    if "start" in bad or "end" in bad:
        return "both"
    if "video" in bad:
        return "video"
    return "both"


def default_what_for_rows(rows: List[dict]) -> str:
    stale: List[str] = []
    missing: List[str] = []
    for row in rows:
        stale += list(row.get("stale_parts") or [])
        missing += [n for n, p in (row.get("parts") or {}).items() if (p or {}).get("state") == "missing"]
    return default_what(stale, missing)


def regen_button(cost_rub: float, unknown_cost: bool) -> dict:
    """Подпись кнопки запуска и пояснение под ней (id текста или None)."""
    if cost_rub > 0:
        return {"label": T.fmt("regen_go_paid", cost=T.format_rub_amount(cost_rub)),
                "note": "regen_cost_partial" if unknown_cost else None}
    if unknown_cost:
        return {"label": T.SHOT_TEXTS["regen_go_unknown"], "note": "regen_cost_unknown"}
    return {"label": T.SHOT_TEXTS["regen_go_free"], "note": None}


def regen_view(preview: dict) -> dict:
    """Всё, что нужно окну «Перегенерировать шот», из ответа regenerate_preview."""
    confirm = preview.get("confirm_required") or {}
    cost = float(confirm.get("cost_rub") or 0)
    unknown = bool(confirm.get("unknown_cost"))
    pending = preview.get("pending_paid_jobs") or []
    pending_keys = sorted({str(p.get("shot_key")) for p in pending if p.get("shot_key")}, key=shot_sort_key)
    disk = preview.get("disk_needed_bytes") or 0
    return {
        "button": regen_button(cost, unknown),
        "cost_rub": cost,
        "unknown_cost": unknown,
        "can_start": bool(preview.get("can_start")),
        "needs_abandon": bool(pending),
        "pending_keys": pending_keys,
        "shot_count": len(preview.get("expanded_keys") or []),
        "linked_keys": list(preview.get("linked_keys") or []),
        "disk_needed": disk if disk > 0 else 0,
        "problems": [{"level": p.get("level"), "message": p.get("message")} for p in preview.get("problems") or []],
    }


def start_payload_from_preview(base: dict, preview: dict, confirmed: bool) -> dict:
    payload = {**base, "expected_plan_digest": preview.get("plan_digest")}
    confirm = preview.get("confirm_required") or {}
    if confirmed and (confirm.get("cost_rub") or 0) > 0:
        payload["confirm_cost_rub"] = confirm["cost_rub"]
    if confirmed and confirm.get("unknown_cost"):
        payload["confirm_unknown_cost"] = True
    return payload


def classify_regen_error(code: str, reason: Optional[str], message: str, details: Optional[dict] = None) -> dict:
    """Реакция окна на отказ regenerate_start (таблица 7.4 плана).

    kind: refresh (обновить предпросмотр, ничего не запускать), need_abandon (показать ключи и
    включить флажок), pending, blocked (показать problems), locked, other.
    """
    details = details or {}
    text = T.error_text(code, reason, message)
    if code == "locked":
        return {"kind": "locked", "text": text}
    if code == "conflict":
        if reason in ("plan_changed", "cost_changed", "cost_confirmation_required"):
            return {"kind": "refresh", "text": text}
        if reason == "paid_job_pending":
            keys = sorted((str(k) for k in details.get("keys") or []), key=shot_sort_key)
            return {"kind": "need_abandon", "text": message, "keys": keys}
        if reason == "regeneration_pending":
            return {"kind": "pending", "text": T.SHOT_TEXTS["regen_pending"]}
        if reason == "plan_blocked":
            problems = [{"level": p.get("level"), "message": p.get("message")} for p in details.get("problems") or []]
            return {"kind": "blocked", "text": message, "problems": problems}
    return {"kind": "other", "text": text}


# --- Версии ----------------------------------------------------------------------------

def version_label(version: dict) -> str:
    """Подпись версии: номер, дата, провайдер, стоимость с копейками («6,36 ₽»)."""
    if version.get("id") == "current":
        return T.SHOT_TEXTS["compare_current"]
    pieces = [f"Версия {version.get('number', '')}".strip()]
    created = str(version.get("created_at") or "")
    if created:
        pieces.append(created[:16].replace("T", " "))
    if version.get("provider"):
        pieces.append(str(version["provider"]))
    if isinstance(version.get("cost_rub"), (int, float)):
        pieces.append(T.format_rub_exact(version["cost_rub"]))
    if version.get("is_selected"):
        pieces.append(T.SHOT_TEXTS["version_selected"])
    return " · ".join(pieces)


def error_parts(result: dict) -> tuple:
    """(code, reason, message, details) из ответа-ошибки studio."""
    error = (result or {}).get("error") or {}
    details = error.get("details") or {}
    return (str(error.get("code") or "internal"), details.get("reason"),
            str(error.get("message") or "Внутренняя ошибка"), details)


def result_error_text(result: dict) -> str:
    code, reason, message, _ = error_parts(result)
    return T.error_text(code, reason, message)
