"""Цепочка «план -> решение -> запуск» для действий болванки (без tkinter, вызовы через StudioCalls)."""

from .blockout_model import BLOCKOUT_TEXTS
from .calls import CAP_BLOCKOUT_PLAN, CAP_BLOCKOUT_RUN

# Для этих действий отпечаток плана обязателен: без него сервер запуск не примет
DIGEST_REQUIRED = ("redraw_chain", "rebuild_scene", "regenerate_shots")


def needs_confirm(plan):
    return bool((plan.get("confirm") or {}).get("required"))


class BlockoutFlow:
    def __init__(self, calls, get_project_id, launcher="local"):
        self.calls = calls
        self.get_project_id = get_project_id
        self.launcher = launcher
        self.pid = None  # проект, для которого запущен план: поздний ответ не должен уйти в другой проект

    def _run(self, action, target, plan, on_done, on_error):
        payload = {"project_id": self.pid, "action": action, "launcher": self.launcher, **target}
        if plan.get("plan_digest"):
            payload["expected_plan_digest"] = plan["plan_digest"]
        self.calls.call(CAP_BLOCKOUT_RUN, payload, on_success=on_done, on_error=on_error)

    def _decide(self, action, target, plan, on_step, on_error):
        if not plan.get("can_start"):
            on_step({"kind": "blocked", "plan": plan})
        elif action in DIGEST_REQUIRED and not plan.get("plan_digest"):
            problem = {"level": "error", "code": "no_digest", "message": BLOCKOUT_TEXTS["no_digest"], "step_id": None, "artifact": None}
            on_step({"kind": "blocked", "plan": {**plan, "can_start": False, "problems": [*plan["problems"], problem]}})
        elif needs_confirm(plan):
            on_step({"kind": "confirm", "plan": plan})
        else:
            self._run(action, target, plan, lambda r: on_step({"kind": "started", "run": r}), on_error)

    def start(self, action, target, on_step, on_error):
        self.pid = self.get_project_id()
        self.calls.call(CAP_BLOCKOUT_PLAN, {"project_id": self.pid, "action": action, **target},
                        on_success=lambda plan: self._decide(action, target, plan, on_step, on_error), on_error=on_error)

    def confirm(self, action, target, shown, on_outcome, on_error):
        """Пользователь подтвердил показанный план. conflict при запуске: если план закрыт блокером, показываем причину, иначе строим новый."""

        def failed(result):
            error = result.get("error") or {}
            if error.get("code") != "conflict":
                on_error(result)
                return
            reason = (error.get("details") or {}).get("reason")
            if reason == "plan_blocked":
                on_outcome({"kind": "blocked", "text": (error.get("message") or "").strip() or BLOCKOUT_TEXTS["plan_blocked"]})
                return

            def replanned(fresh):
                step = {"kind": "confirm" if fresh.get("can_start") else "blocked", "plan": fresh}
                on_outcome({"kind": "replan", "text": BLOCKOUT_TEXTS["plan_changed"], "step": step})

            self.calls.call(CAP_BLOCKOUT_PLAN, {"project_id": self.pid, "action": action, **target},
                            on_success=replanned, on_error=on_error)

        self._run(action, target, shown, lambda r: on_outcome({"kind": "started", "run": r}), failed)
