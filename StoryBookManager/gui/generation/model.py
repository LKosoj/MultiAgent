"""Модель экрана «Генерация»: режимы, кнопка запуска, баннеры, уведомления (без tkinter).

Повторяет frontend/.../lib/storybook/generationModel.ts; общие векторы - tests/fixtures/storybook_generation_parity.json.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from StoryBookManager.gui.generation import costs, log_model
from StoryBookManager.gui.shell.run_status import rerun_view as _shell_rerun_view
from StoryBookManager.gui.generation.texts import fmt
from StoryBookManager.gui.shots.texts import format_rub_amount

RUN_MODES = ("full", "from_step", "single_step", "resume", "regenerate_shots")
_MODE_TEXT = {"full": "mode_full", "from_step": "mode_from_step", "single_step": "mode_single_step",
              "resume": "mode_resume", "regenerate_shots": "mode_regenerate_shots"}
BUSY_STATES = ("running", "pause_requested", "paused", "stopping")
REASON_LIMIT = 500


def mode_label(mode: str) -> str:
    return fmt(_MODE_TEXT[mode])


def is_busy_state(state: Optional[str]) -> bool:
    return state in BUSY_STATES


def title_of(titles, step_id) -> str:
    """Название шага: titles - словарь id -> название или функция."""
    if callable(titles):
        return titles(step_id)
    return (titles or {}).get(step_id) or str(step_id)


def make_titles(catalog) -> Callable[[str], str]:
    mapping = {s.get("id"): s.get("title") for s in catalog or [] if isinstance(s, dict) and s.get("title")}
    return lambda step_id: mapping.get(step_id) or str(step_id)


# ---- режимы и выбор

def mode_options(resumable: bool, shot_count: int) -> List[dict]:
    out = []
    for mode in RUN_MODES:
        enabled, hint = True, None
        if mode == "resume" and not resumable:
            enabled, hint = False, fmt("mode_resume_none")
        elif mode == "regenerate_shots" and shot_count == 0:
            enabled, hint = False, fmt("mode_shots_none")
        out.append({"mode": mode, "label": mode_label(mode), "enabled": enabled, "hint": hint})
    return out


def resolve_mode(mode: str, resumable: bool, shot_count: int) -> str:
    """«Продолжать нечего» - полный запуск, а не пустая кнопка."""
    option = next((o for o in mode_options(resumable, shot_count) if o["mode"] == mode), None)
    if option and option["enabled"]:
        return mode
    return "full" if mode in ("regenerate_shots", "resume") else mode


@dataclass(frozen=True)
class Selection:
    mode: str = "full"
    step_id: Optional[str] = None
    shot_keys: tuple = ()


def selection_from_intent(mode, step_id, shot_keys, fallback: Selection, resumable: bool) -> Selection:
    """Что выбрать на экране по переходу из других разделов."""
    keys = tuple(k for k in (shot_keys or []) if isinstance(k, str) and k)
    chosen = mode if mode in RUN_MODES else fallback.mode
    if keys:
        chosen = "regenerate_shots"
    elif step_id and chosen == "full":
        chosen = "single_step"
    step = step_id if step_id is not None else fallback.step_id
    all_keys = keys or fallback.shot_keys
    return Selection(resolve_mode(chosen, resumable, len(all_keys)), step, all_keys)


def plan_params(sel: Selection, inputs: Dict, task: str) -> dict:
    out: dict = {"mode": sel.mode}
    if sel.mode == "from_step" and sel.step_id:
        out["start_step"] = sel.step_id
    if sel.mode == "single_step" and sel.step_id:
        out["step_id"] = sel.step_id
    if sel.mode == "regenerate_shots":
        out["shot_keys"] = list(sel.shot_keys)
    if inputs:
        out["inputs"] = dict(inputs)
    if (task or "").strip():
        out["task"] = task.strip()
    return out


# ---- замечания

def problem_text(problem: dict, titles) -> str:
    """missing_artifact: «нужны результаты шагов: a, b;» превращаем в русские названия."""
    import re
    if problem.get("code") == "missing_artifact" and problem.get("step_id"):
        match = re.search(r":\s*([^;]+);", problem.get("message") or "")
        if match:
            needed = ", ".join(title_of(titles, s.strip()) for s in match.group(1).split(",") if s.strip())
            return fmt("step_block", step=title_of(titles, problem["step_id"]), needed=needed)
    return problem.get("message") or ""


_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def collect_notes(plan: Optional[dict], issues, readiness_blocking, readiness_warnings, titles) -> List[dict]:
    """Все замечания одним списком: ошибки, затем предупреждения и сведения; повторы убираются.

    readiness только предупреждает, запуск он не блокирует (решение В4).
    """
    notes = [{"level": p.get("level"), "text": problem_text(p, titles)} for p in (plan or {}).get("problems") or []]
    notes += [{"level": i.get("level"), "text": i.get("message")} for i in issues or []]
    notes += [{"level": "warning", "text": t} for t in list(readiness_blocking or []) + list(readiness_warnings or [])]
    seen, out = set(), []
    for n in notes:
        if not n["text"] or n["text"] in seen:
            continue
        seen.add(n["text"])
        out.append(n)
    return sorted(out, key=lambda n: _LEVEL_ORDER.get(n["level"], 3))


def blocking_issues(issues) -> List[dict]:
    return [i for i in issues or [] if i.get("level") == "error"]


def inflight_warning(plan: Optional[dict]) -> Optional[str]:
    for p in (plan or {}).get("problems") or []:
        if p.get("code") == "video_jobs_inflight":
            return p.get("message")
    return None


# ---- кнопка запуска

@dataclass(frozen=True)
class StartButton:
    label: str
    enabled: bool
    reason: Optional[str]
    no_estimate: bool = False


def _off(label, reason):
    return StartButton(label, False, reason)


def start_button(s: dict, titles) -> StartButton:
    """Подпись и доступность кнопки запуска. s - словарь с ключами как в общей фикстуре."""
    sel = s.get("selection") or {}
    mode, step_id, keys = sel.get("mode", "full"), sel.get("stepId"), sel.get("shotKeys") or []
    regen = mode == "regenerate_shots"
    idle = fmt("go_regen") if regen else fmt("go")
    if s.get("loading") or s.get("planBusy"):
        return _off(idle, fmt("why_loading"))
    if s.get("engineError") is not None:
        return _off(idle, fmt("why_engine", reason=s["engineError"]))
    if not s.get("isAdmin", True):
        return _off(idle, fmt("why_no_rights"))
    if is_busy_state(s.get("runState")):
        return _off(fmt("go_running"), None)
    if s.get("locked"):
        return _off(idle, fmt("why_busy"))
    if mode in ("from_step", "single_step") and not step_id:
        return _off(idle, fmt("why_pick_step"))
    if regen and not keys:
        return _off(idle, fmt("why_no_shots"))
    if mode == "full" and not s.get("hasBrief") and not (s.get("task") or "").strip():
        return _off(idle, fmt("why_no_brief"))
    errors = blocking_issues(s.get("issues"))
    if errors:
        return _off(idle, fmt("why_issues", reason=errors[0].get("message")))
    provider = s.get("provider")
    if provider and not provider.get("credentials_found"):
        return _off(idle, fmt("why_no_key", name=provider.get("title")))

    if regen:
        r = s.get("regen")
        if not r:
            return _off(idle, fmt("why_loading"))
        if not r.get("can_start"):
            return _off(idle, fmt("why_plan", reason=r.get("problem") or ""))
        if (r.get("cost_rub") or 0) > 0:
            return StartButton(fmt("go_regen_cost", cost=format_rub_amount(r["cost_rub"])), True, None)
        if r.get("unknown_cost"):
            return StartButton(fmt("go_no_estimate"), True, None, True)
        return StartButton(fmt("go_regen"), True, None)

    if s.get("planError") is not None:
        return _off(idle, fmt("why_plan", reason=s["planError"]))
    plan = s.get("plan")
    if not plan:
        return _off(idle, fmt("why_loading"))
    if not plan.get("can_start"):
        first = next((p for p in plan.get("problems") or [] if p.get("level") == "error"), None)
        return _off(idle, problem_text(first, titles) if first else fmt("why_plan", reason=""))
    est = s.get("estimate")
    if s.get("estimateError") is not None or not est:
        return StartButton(fmt("go_no_estimate"), True, s.get("estimateError"), True)
    total = costs.estimate_total(est)
    if total > 0:
        return StartButton(fmt("go_cost", cost=format_rub_amount(total)), True, None, not est.get("complete"))
    if not est.get("complete"):
        return StartButton(fmt("go_no_estimate"), True, None, True)
    return StartButton(fmt("go"), True, None)


def confirm_kind(estimate: Optional[dict], estimate_error: Optional[str]) -> str:
    """start | no_estimate | over_budget."""
    if estimate_error is not None or not estimate:
        return "no_estimate"
    if estimate.get("budget_verdict") == "short":
        return "over_budget"
    if not estimate.get("complete"):
        return "no_estimate"
    return "start"


# ---- время

def format_duration(seconds) -> str:
    """«42 мин», «2 ч 5 мин»; меньше минуты - в секундах."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0 or seconds != seconds:
        return "—"
    if seconds < 60:
        return f"{int(round(seconds))} с"
    total = int(round(seconds / 60))
    hours, minutes = divmod(total, 60)
    if hours == 0:
        return f"{minutes} мин"
    return f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(start, end) -> Optional[int]:
    a, b = _parse_time(start), _parse_time(end)
    if a is None or b is None or (a.tzinfo is None) != (b.tzinfo is None) or b < a:
        return None
    return int(round((b - a).total_seconds()))


def run_duration_text(run) -> str:
    run = run if isinstance(run, dict) else {}
    secs = seconds_between(run.get("started_at"), run.get("finished_at"))
    return "—" if secs is None else format_duration(secs)


# ---- баннеры и тосты

def banner_step_id(status: dict) -> Optional[str]:
    failure = status.get("failure") or {}
    if failure.get("step_id"):
        return failure["step_id"]
    for row in reversed(status.get("steps") or []):
        if row.get("status") in ("running", "failed"):
            return row.get("id")
    return None


def _failure_reason(status: dict) -> str:
    return ((status.get("failure") or {}).get("message") or "")[:REASON_LIMIT] or "причина неизвестна"


def banner_for(status: Optional[dict], titles) -> Optional[dict]:
    """{kind, tone, text, action} или None."""
    if not status:
        return None
    state = status.get("state")
    engine = status.get("engine") or {}
    if engine.get("available") is False:
        return {"kind": "engine", "tone": "error", "action": None,
                "text": fmt("banner_engine", reason=engine.get("error") or "причина неизвестна")}
    step_id = banner_step_id(status)
    step = title_of(titles, step_id) if step_id else "—"
    # «не отвечает» важнее «запущено в другом приложении»: только оно даёт кнопку «Пометить прерванным»
    if status.get("unresponsive") and is_busy_state(state):
        return {"kind": "unresponsive", "tone": "warn", "action": "mark_interrupted", "text": fmt("banner_unresponsive")}
    if status.get("other_surface"):
        return {"kind": "other_surface", "tone": "info", "action": None, "text": status["other_surface"].get("text") or ""}
    if state == "interrupted":
        return {"kind": "interrupted", "tone": "warn", "action": "resume", "text": fmt("banner_interrupted", step=step)}
    if state == "failed":
        return {"kind": "failed", "tone": "error", "action": "resume",
                "text": fmt("banner_failed", step=step, reason=_failure_reason(status))}
    if status.get("stale_control"):
        return {"kind": "stale_control", "tone": "warn", "action": "refresh", "text": fmt("banner_stale")}
    if state == "completed":
        return {"kind": "completed", "tone": "ok", "action": "restart",
                "text": fmt("banner_completed", time=run_duration_text(status.get("run")))}
    return None


def transition_toast(prev: Optional[str], status: Optional[dict], titles) -> Optional[dict]:
    """Тост при переходе из «идёт» в итоговое состояние: один раз."""
    if not status or not is_busy_state(prev) or is_busy_state(status.get("state")):
        return None
    state = status.get("state")
    if state == "completed":
        return {"type": "success", "text": fmt("finished", time=run_duration_text(status.get("run")))}
    if state == "failed":
        step_id = banner_step_id(status)
        return {"type": "error", "text": fmt("failed_toast", step=title_of(titles, step_id) if step_id else "—",
                                             reason=_failure_reason(status))}
    if status.get("outcome") == "cancelled" or state == "idle":
        return {"type": "info", "text": fmt("cancelled_toast")}
    return None


def settings_locked(state: Optional[str], locked: bool) -> bool:
    return is_busy_state(state) or bool(locked)


def step_line(status: dict, titles) -> str:
    p = status.get("progress") or {}
    if not p.get("step_id"):
        return fmt("progress_starting")
    return fmt("step_n_of", n=max(p.get("step_index") or 1, 1), total=p.get("step_total") or 0,
               name=title_of(titles, p["step_id"]))


def restart_dialog(mode: str, state: Optional[str], resumable: bool) -> Optional[str]:
    """Окно перед полным запуском: restart после завершённого прогона, discard при неоконченном."""
    if mode != "full":
        return None
    if state == "completed":
        return "restart"
    return "discard" if resumable else None


def should_discard(mode: str, resumable: bool) -> bool:
    """Неоконченный прогон стирается только при запуске «сначала»; продолжение его не трогает."""
    return mode == "full" and resumable


# Имена, под которыми общий тест паритета с React ищет функции модели
estimate_headline = costs.estimate_headline
parse_log_line = log_model.parse_line
rerun_view = _shell_rerun_view
