"""Адаптивное планирование для smolagents-агентов.

Мониторит ход выполнения плана и пропускает дорогостоящий replan,
когда агент и так идёт по плану.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Generator, Optional

from smolagents import CodeAgent, ToolCallingAgent
from smolagents.memory import ActionStep, PlanningStep
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing, TokenUsage

logger = logging.getLogger(__name__)

DEFAULT_ADAPTIVE_CADENCE = 2
DEFAULT_FORCE_REPLAN_EVERY = 4


@dataclass
class PlanningConfig:
    """Нормализованная конфигурация интервала планирования."""

    smol_interval: Optional[int]
    adaptive: bool
    check_cadence: Optional[int]
    force_every: int


def normalize_planning_interval(value) -> PlanningConfig:
    """Привести значение planning_interval к PlanningConfig.

    Поддерживает: None, int, целый float, str ("2", "adaptive", "adaptive:N").
    bool явно отклоняется (bool — подтип int).
    """
    if value is None:
        return PlanningConfig(None, False, None, DEFAULT_FORCE_REPLAN_EVERY)

    if isinstance(value, bool):
        raise ValueError(f"planning_interval не может быть bool: {value!r}")

    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
        else:
            raise ValueError(f"Нецелый float не поддерживается: {value!r}")

    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"planning_interval не может быть отрицательным: {value!r}")
        if value == 0:
            return PlanningConfig(None, False, None, DEFAULT_FORCE_REPLAN_EVERY)
        return PlanningConfig(value, False, None, DEFAULT_FORCE_REPLAN_EVERY)

    if isinstance(value, str):
        s = value.strip()
        if s == "adaptive":
            return PlanningConfig(
                DEFAULT_ADAPTIVE_CADENCE, True, DEFAULT_ADAPTIVE_CADENCE, DEFAULT_FORCE_REPLAN_EVERY
            )
        m = re.fullmatch(r"adaptive:(\d+)", s)
        if m:
            n = int(m.group(1))
            if n <= 0:
                raise ValueError(f"adaptive:N требует N>0, получено: {n!r}")
            return PlanningConfig(n, True, n, DEFAULT_FORCE_REPLAN_EVERY)
        # Числовая строка (в т.ч. "-1") маршрутизируется в int-ветку ради
        # корректной диагностики ("не может быть отрицательным" и т.п.).
        try:
            n = int(s)
        except ValueError:
            raise ValueError(f"Неподдерживаемый формат planning_interval: {value!r}")
        return normalize_planning_interval(n)

    raise ValueError(f"Неподдерживаемый тип planning_interval: {type(value).__name__}")


@dataclass
class Verdict:
    """Результат оценки монитора."""

    on_track: bool
    replan_needed: bool
    reason: str
    token_usage: Optional[TokenUsage] = None


def parse_verdict(text: str) -> Verdict:
    """Разобрать ответ монитора в Verdict.

    Пробует JSON, затем keyword-фолбэк, затем fail-safe.
    """
    if not text or not text.strip():
        logger.warning("parse_verdict: пустой ответ монитора — fail-safe replan")
        return Verdict(False, True, "empty monitor output")

    # Сканируем каждый '{' через raw_decode: корректно разбирает вложенные
    # объекты (нежадный regex рвал бы их) и пропускает посторонний JSON.
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            data, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        idx = end
        if not isinstance(data, dict):
            continue
        if "on_track" not in data and "replan_needed" not in data:
            # посторонний JSON-объект — ищем дальше
            continue
        on_track = bool(data.get("on_track", False))
        replan_needed = bool(data.get("replan_needed", not on_track))
        reason = str(data.get("reason", ""))
        return Verdict(on_track, replan_needed, reason)

    # Keyword-фолбэк: негатив/replan проверяем ПЕРВЫМ, иначе "on track"
    # внутри отрицания ложно трактуется как on_track. Разрыв "not ... on track"
    # ловим regex'ом (в пределах предложения), плюс явные маркеры дрейфа.
    lower = text.lower()
    negated_on_track = re.search(r"\bnot\b[^.]{0,30}on track", lower) is not None
    if (
        "replan" in lower
        or "off track" in lower
        or "not on track" in lower
        or negated_on_track
        or ("on_track" in lower and "false" in lower)
    ):
        return Verdict(False, True, "keyword:replan")
    if ("on_track" in lower and "true" in lower) or "on track" in lower:
        return Verdict(True, False, "keyword:on_track=true")

    logger.warning("parse_verdict: не удалось разобрать ответ монитора — fail-safe replan")
    return Verdict(False, True, f"parse_failed: {text[:100]}")


_MONITOR_PROMPT_TEMPLATE = (
    "You are a plan-adherence monitor.\n\n"
    "Task:\n{task}\n\n"
    "Current Plan:\n{current_plan}\n\n"
    "Recent Actions:\n{recent_context}\n\n"
    "Decide whether the agent is on track or has errored / looped / significantly deviated / is blocked.\n"
    'Respond with EXACTLY one line of JSON: {{"on_track": true/false, "reason": "<=20 words"}}.\n'
    "Set on_track=false if there is an error, loop, significant deviation, or blockage."
)


def evaluate_plan_adherence(model, task: str, current_plan: str, recent_context: str) -> Verdict:
    """Вызвать модель-монитор и вернуть Verdict.

    При любой ошибке возвращает fail-safe Verdict с replan_needed=True.
    """
    prompt = _MONITOR_PROMPT_TEMPLATE.format(
        task=task,
        current_plan=current_plan,
        recent_context=recent_context,
    )
    messages = [ChatMessage(role=MessageRole.USER, content=prompt)]
    try:
        resp = model.generate(messages)
        # content может быть str | list[dict] | None (зависит от модели).
        raw = getattr(resp, "content", None)
        if isinstance(raw, list):
            text = " ".join(item.get("text", "") for item in raw if isinstance(item, dict))
        elif isinstance(raw, str):
            text = raw
        else:
            text = ""
        v = parse_verdict(text)
        v.token_usage = getattr(resp, "token_usage", None)
        return v
    except Exception as e:
        logger.warning("evaluate_plan_adherence: ошибка монитора: %s", e)
        return Verdict(False, True, f"monitor_error: {e}")


class AdaptivePlanningMixin:
    """Mixin для адаптивного пропуска replan-шагов.

    Подмешивается перед ToolCallingAgent / CodeAgent.
    Использует модель-монитор (self._monitor_model) для оценки хода плана.
    """

    def __init__(self, *args, **kwargs):
        # Явный контракт адаптивного планирования; фабрика затем переопределяет
        # _monitor_model и _adaptive_force_every. Базовый агент инициализируется
        # дальше по MRO.
        self._adaptive_checks_since_replan = 0
        self._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
        self._monitor_model = None
        super().__init__(*args, **kwargs)

    def _generate_planning_step(
        self, task, is_first_step: bool, step: int
    ) -> Generator:
        """Генератор шага планирования с адаптивным пропуском replan."""
        start_time = time.time()
        monitor_model = getattr(self, "_monitor_model", None)
        force_every = getattr(self, "_adaptive_force_every", DEFAULT_FORCE_REPLAN_EVERY)
        checks = getattr(self, "_adaptive_checks_since_replan", 0)

        # Первый шаг — всегда полный план
        if is_first_step:
            self._adaptive_checks_since_replan = 0
            yield from super()._generate_planning_step(task, is_first_step, step)
            return

        # Уровень 0: если в окне есть ошибка — replan обязателен
        window = []
        for s in reversed(self.memory.steps):
            if isinstance(s, PlanningStep):
                break
            if isinstance(s, ActionStep):
                window.append(s)
        if any(getattr(a, "error", None) is not None for a in window):
            self._adaptive_checks_since_replan = 0
            yield from super()._generate_planning_step(task, is_first_step, step)
            return

        # Принудительный replan по счётчику
        if checks >= force_every:
            self._adaptive_checks_since_replan = 0
            yield from super()._generate_planning_step(task, is_first_step, step)
            return

        # Failsafe: нет модели монитора
        if monitor_model is None:
            self._adaptive_checks_since_replan = 0
            yield from super()._generate_planning_step(task, is_first_step, step)
            return

        # Уровень 1: монитор оценивает ход плана
        current_plan, recent_context = self._collect_adaptive_context()
        verdict = evaluate_plan_adherence(monitor_model, task, current_plan, recent_context)

        if verdict.replan_needed:
            self._adaptive_checks_since_replan = 0
            yield from super()._generate_planning_step(task, is_first_step, step)
            return

        # Агент идёт по плану — возвращаем noop-шаг без вызова модели
        self._adaptive_checks_since_replan = checks + 1
        yield self._make_noop_planning_step(current_plan, verdict, start_time)

    def _collect_adaptive_context(self) -> tuple[str, str]:
        """Собрать текущий план и контекст последних действий из памяти."""
        steps = list(getattr(self.memory, "steps", []) or [])
        current_plan = "(no plan yet)"
        window_actions: list[ActionStep] = []

        for s in reversed(steps):
            if isinstance(s, PlanningStep):
                current_plan = getattr(s, "plan", None) or "(no plan yet)"
                break
            if isinstance(s, ActionStep):
                window_actions.append(s)

        # Последние 2 действия в хронологическом порядке
        window_actions = list(reversed(window_actions))[-2:]

        parts = []
        for a in window_actions:
            p = f"--- Step {getattr(a, 'step_number', '?')} ---"
            obs = getattr(a, "observations", None)
            err = getattr(a, "error", None)
            if obs:
                p += f"\nObservation: {str(obs)[:500]}"
            if err is not None:
                p += f"\nError: {err}"
            if not obs and err is None:
                p += "\n(no observation)"
            parts.append(p)

        recent_context = "\n".join(parts) if parts else "(no actions yet)"
        return current_plan, recent_context

    def _make_noop_planning_step(self, current_plan: str, verdict: Verdict, start_time: float) -> PlanningStep:
        """Создать PlanningStep без реального replan (агент на верном пути)."""
        note = f"[adaptive-monitor] on_track. {getattr(verdict, 'reason', '')}"
        return PlanningStep(
            model_input_messages=[],
            model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content=note),
            plan=current_plan,
            token_usage=getattr(verdict, "token_usage", None),
            timing=Timing(start_time=start_time, end_time=time.time()),
        )


class AdaptivePlanningToolCallingAgent(AdaptivePlanningMixin, ToolCallingAgent):
    """ToolCallingAgent с адаптивным планированием."""


class AdaptivePlanningCodeAgent(AdaptivePlanningMixin, CodeAgent):
    """CodeAgent с адаптивным планированием."""
