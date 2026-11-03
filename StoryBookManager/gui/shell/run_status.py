"""Что показывать в верхней строке: состояние прогона, траты, доступность кнопок (без tkinter)."""

from dataclasses import dataclass
from typing import Optional

NBSP = " "

ACTIVE_STATES = ("running", "pause_requested", "paused", "stopping")

# Тон = токен цвета из theme_tokens.COLORS
TONE_BY_STATE = {
    "idle": "text_muted",
    "running": "running",
    "pause_requested": "warning",
    "paused": "warning",
    "stopping": "warning",
    "failed": "error",
    "interrupted": "warning",
    "completed": "success",
}

NOT_RUNNING_TEXT = "Прогон не запущен"
UNAVAILABLE_TEXT = "Состояние прогона недоступно"
NO_REMOTE_HINT = "Управление прогоном из другого приложения станет доступно после обновления"
STOPPING_HINT = "Остановка уже запрошена"

OWNER_NAMES = {"tk": "Tk", "react": "React"}


@dataclass(frozen=True)
class RunView:
    state: str = "idle"
    text: str = NOT_RUNNING_TEXT
    tone: str = "text_muted"
    owner: str = "none"          # none | remote
    owner_name: str = ""         # "Tk" / "React" / ""
    pause_label: str = "Пауза"
    pause_enabled: bool = False
    pause_action: Optional[str] = None   # remote | None
    pause_target: bool = True            # remote: True - поставить на паузу, False - снять
    pause_hint: str = NOT_RUNNING_TEXT
    stop_enabled: bool = False
    stop_action: Optional[str] = None
    stop_hint: str = NOT_RUNNING_TEXT
    step_id: str = ""            # внутренний id шага - только для подсказки
    rerun_enabled: bool = False  # «Запустить заново»: открывает «Генерацию» с нужным режимом
    rerun_hint: str = ""
    rerun_mode: Optional[str] = None


def format_money(value, currency: Optional[str] = "RUB") -> str:
    """«1 204 ₽» с неразрывными пробелами; чужая валюта - кодом."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    amount = int(round(value))
    digits = f"{abs(amount):,}".replace(",", NBSP)
    sign = "-" if amount < 0 else ""
    code = (currency or "RUB").upper()
    unit = "₽" if code == "RUB" else code
    return f"{sign}{digits}{NBSP}{unit}"


def format_rub(value) -> str:
    return format_money(value, "RUB")


def spend_text(spend) -> str:
    """«Траты 412 ₽ из 1 500 ₽»; без суммы траты не показываем."""
    if not isinstance(spend, dict):
        return ""
    currency = spend.get("currency") or "RUB"
    total = format_money(spend.get("total"), currency)
    if not total:
        return ""
    text = f"Траты {total}"
    budget = spend.get("budget_rub")
    if currency.upper() == "RUB" and isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
        text += f" из {format_rub(budget)}"
    return text


def poll_interval_ms(state: Optional[str]) -> int:
    """2 с, пока прогон идёт (контракт Ф3), иначе 10 с."""
    return 2000 if state in ACTIVE_STATES else 10000


def owner_phrase(owner_name: str) -> str:
    return f"в приложении {owner_name}" if owner_name else "в другом приложении"


def rerun_view(state: Optional[str], resumable: bool = True) -> dict:
    """Кнопка «Запустить заново» в верхней полосе: куда вести и что подсказать.

    Тексты совпадают с rerun_* из tests/fixtures/storybook_generation_parity.json (сверяет тест паритета)."""
    if state in ACTIVE_STATES:
        return {"enabled": False, "hint": "Недоступно, пока идёт прогон", "mode": None}
    if state in ("failed", "interrupted"):
        if resumable:
            return {"enabled": True, "hint": "Открыть «Генерацию»: продолжить прогон после сбоя", "mode": "resume"}
        return {"enabled": True, "hint": "Открыть «Генерацию»: запустить пайплайн заново", "mode": "full"}
    if state == "completed":
        return {"enabled": True, "hint": "Открыть «Генерацию»: запустить пайплайн заново", "mode": "full"}
    return {"enabled": False, "hint": "Прогон ещё не запускался", "mode": None}


def build_run_view(brief: Optional[dict], caps=()) -> RunView:
    """Собрать состояние верхней строки из ответа project_status_brief.

    caps: id доступных возможностей реестра.
    """
    caps = set(caps or ())
    run = (brief or {}).get("run") if isinstance((brief or {}).get("run"), dict) else {}
    state = run.get("state") if run.get("state") in TONE_BY_STATE else None
    text = run.get("text") or ""
    surface = run.get("surface") if run.get("surface") in OWNER_NAMES else ""
    step = run.get("step") if isinstance(run.get("step"), dict) else {}

    if state is None:
        state, text = "idle", NOT_RUNNING_TEXT
    elif not text:
        text = NOT_RUNNING_TEXT if state == "idle" else ""

    owner = "none"
    owner_name = ""
    if state in ACTIVE_STATES:
        owner, owner_name = "remote", OWNER_NAMES.get(surface, "")

    rerun = rerun_view(state, state == "interrupted" or bool(run.get("failed_step")))
    paused_now = state in ("paused", "pause_requested")
    view = dict(state=state, text=text, tone=TONE_BY_STATE[state], owner=owner, owner_name=owner_name,
                pause_label="Продолжить" if paused_now else "Пауза",
                pause_target=not paused_now, step_id=str(step.get("id") or ""),
                rerun_enabled=rerun["enabled"], rerun_hint=rerun["hint"], rerun_mode=rerun["mode"])

    if owner == "none":
        return RunView(**view)

    stopping = state == "stopping"
    pause_ok = not stopping and "run_set_pause" in caps
    stop_ok = not stopping and "run_cancel" in caps
    pause_action = "remote" if pause_ok else None
    stop_action = "remote" if stop_ok else None
    pause_hint = STOPPING_HINT if stopping else ("" if pause_ok else NO_REMOTE_HINT)
    stop_hint = STOPPING_HINT if stopping else ("" if stop_ok else NO_REMOTE_HINT)
    view.update(pause_enabled=pause_ok, pause_action=pause_action, pause_hint=pause_hint,
                stop_enabled=stop_ok, stop_action=stop_action, stop_hint=stop_hint)
    return RunView(**view)
