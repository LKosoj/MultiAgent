"""Машина состояний экрана «Генерация» (без tkinter).

Зависимости передаются снаружи:
  bridge         - StudioBridge: call(capability_id, payload, on_success=, on_error=, latest_only_key=);
  view           - render(*частей), ask_confirm(spec) -> bool, ask_choice(title, body, buttons, default) -> id,
                   show_tab(имя), focus_budget(), log_wanted() -> bool;
  notify         - тосты: success, info, warning, error;
  schedule/cancel - таймер главного потока: schedule(ms, fn) -> handle;
  get_project_id - id открытого проекта или None;
  on_run_changed - сообщить оболочке, что прогон запущен или остановлен (обновить верхнюю строку).
Ответы мост возвращает в главном потоке; устаревшие отбрасываются по номеру поколения.
"""

import logging
import math
from typing import Callable, Dict, List, Optional

from StoryBookManager.gui.generation import costs, form, log_model, model, texts as T
from StoryBookManager.gui.generation.history_model import history_rows
from StoryBookManager.gui.generation.poller import RunPoller
from StoryBookManager.gui.shell.dialog_texts import ConfirmSpec, stop_run_spec
from StoryBookManager.gui.shots import model as SM
from StoryBookManager.gui.shots.texts import format_rub_amount, plural_shots

logger = logging.getLogger(__name__)

SUMMARY_DELAY_MS = 500
LOG_FIRST_LINES = log_model.LOG_LINES_CAP
LAUNCHER = "local"
REGEN_WHATS = ("images", "video", "both", "music", "film")


def _first_error(problems) -> str:
    for p in problems or []:
        if p.get("level") == "error":
            return p.get("message") or ""
    return ""


class GenerationController:
    def __init__(self, bridge, view, notify, schedule: Callable, cancel: Callable,
                 get_project_id: Callable[[], Optional[str]],
                 on_run_changed: Optional[Callable[[], None]] = None):
        self.bridge, self.view, self.notify = bridge, view, notify
        self.schedule, self.cancel = schedule, cancel
        self.get_project_id = get_project_id
        self.on_run_changed = on_run_changed or (lambda: None)
        self.closed = False
        self.poller = RunPoller(bridge, schedule, cancel, self._on_status, self._on_unavailable)

        self.project_id: Optional[str] = None
        self.load_state = "idle"          # idle, loading, ready, error
        self.load_error = ""
        self.unavailable = False
        # настройки
        self.saved: Optional[dict] = None
        self.draft: Optional[dict] = None
        self.digest: Optional[str] = None
        self.effective: dict = {}
        self.defaults: Optional[dict] = None
        self.issues: List[dict] = []
        self.budget_text: Optional[str] = None
        self.budget_error: Optional[str] = None
        self.saving = False
        # справочники
        self.providers: Optional[dict] = None
        self.styles: Optional[dict] = None
        self.steps_catalog: List[dict] = []
        self.titles = model.make_titles([])
        # выбор
        self.selection = model.Selection()
        self.task = ""
        self.brief: Dict = {}
        self.regen_what = "both"
        # план и сводка
        self.plan: Optional[dict] = None
        self.plan_error: Optional[str] = None
        self.summary: Optional[dict] = None
        self.estimate: Optional[dict] = None
        self.estimate_error: Optional[str] = None
        self.regen: Optional[dict] = None       # ответ regenerate_preview
        self.paid_pending: Optional[List[str]] = None
        self.plan_busy = False
        # прогон
        self.status: Optional[dict] = None
        self.prev_state: Optional[str] = None
        self.starting = False
        self.discard_next = False
        self.history: List[dict] = []
        self.log = log_model.EMPTY_LOG
        self._log_busy = False
        self._log_gen = 0
        self._defaulted: Optional[str] = None
        self.stop_busy = False
        self._plan_gen = 0
        self._load_gen = 0
        self._plan_timer = None
        self._after_plan: Optional[Callable[[], None]] = None   # что сделать, когда план пересчитан
        self._pending_intent = None                              # переход из другого раздела до первого статуса
        self.pause_busy = False
        self.viewing_run: Optional[str] = None   # журнал выбранного прошлого прогона вместо текущего

    # ------------------------------------------------------------------ вспомогательное

    @property
    def run_state(self) -> str:
        return (self.status or {}).get("state") or "idle"

    @property
    def run_id(self) -> Optional[str]:
        run = (self.status or {}).get("run") or {}
        return run.get("run_id") or (self.status or {}).get("run_id")

    @property
    def resumable(self) -> bool:
        return bool((self.status or {}).get("resumable", False))

    def settings_dirty(self) -> bool:
        return form.is_dirty(self.saved, self.draft)

    def render(self, *what: str) -> None:
        if not self.closed:
            self.view.render(*(what or ("all",)))

    def _call(self, cap: str, payload: dict, ok: Callable, fail: Optional[Callable] = None, key: Optional[str] = None):
        self.bridge.call(cap, payload, on_success=ok, on_error=fail or self._report, latest_only_key=key)

    def _report(self, result: dict) -> None:
        self.notify.error(SM.result_error_text(result))

    # ------------------------------------------------------------------ загрузка

    def set_project(self, project_id: Optional[str]) -> None:
        """Смена проекта: всё сбрасывается; загрузка идёт при показе раздела."""
        self.project_id = project_id
        self._load_gen += 1
        self._plan_gen += 1
        self.poller.stop()
        self._cancel_plan_timer()
        self._reset_state()
        self.render()

    def _reset_state(self) -> None:
        self.load_state = "idle"
        self.load_error = ""
        self.unavailable = False
        self.saved = self.draft = self.digest = self.defaults = None
        self.effective, self.issues = {}, []
        self.budget_text = self.budget_error = None
        self.providers = self.styles = None
        self.steps_catalog, self.titles = [], model.make_titles([])
        self.selection, self.task, self.brief = model.Selection(), "", {}
        self.regen_what = "both"
        self.plan = self.plan_error = self.summary = self.estimate = self.estimate_error = None
        self.regen = None
        self.paid_pending = None
        self.status = self.prev_state = None
        self.starting = self.discard_next = False
        self.history = []
        self.log = log_model.EMPTY_LOG
        self._pending_intent = None
        self.plan_busy = self.pause_busy = self.stop_busy = False
        self.viewing_run = None
        self._log_busy = False
        self._log_gen += 1

    def on_show(self) -> None:
        pid = self.get_project_id()
        if pid != self.project_id:
            self.set_project(pid)
        if not self.project_id:
            return
        if self.load_state in ("idle", "error"):
            self.load()
        else:
            self.poller.refresh()

    def load(self) -> None:
        if not self.project_id:
            return
        self._load_gen += 1
        gen, pid = self._load_gen, self.project_id
        self.load_state, self.load_error = "loading", ""
        self.render()

        def alive():
            return gen == self._load_gen and not self.closed

        def settings_ok(res):
            if not alive():
                return
            self.saved, self.draft = form.clone(res["settings"]), form.clone(res["settings"])
            self.digest, self.effective = res.get("digest"), res.get("effective") or {}
            self.defaults, self.issues = res.get("defaults"), res.get("issues") or []
            self.budget_text, self.budget_error = form.budget_text(self.draft.get("budget_rub")), None
            self.load_state = "ready"
            self.render()
            self.refresh_plan(now=True)

        def settings_fail(res):
            if alive():
                self.load_state, self.load_error = "error", SM.result_error_text(res)
                self.render()

        def soft(setter):
            def ok(res):
                if alive():
                    setter(res)
                    self.render()
            return ok

        def quiet(_res):
            logger.debug("Справочник экрана «Генерация» не загрузился")

        def set_providers(res):
            self.providers = res

        def set_styles(res):
            self.styles = res

        def set_steps(res):
            self.steps_catalog = res.get("steps") or []
            self.titles = model.make_titles(self.steps_catalog)

        def set_history(res):
            self.history = history_rows(res.get("runs"))

        self._call("run_settings_get", {"project_id": pid}, settings_ok, settings_fail)
        self._call("provider_catalog", {"for_project_id": pid}, soft(set_providers), quiet)
        self._call("style_catalog", {}, soft(set_styles), quiet)
        self._call("run_steps_get", {}, soft(set_steps), quiet)
        self._call("run_history", {"project_id": pid, "limit": 20}, soft(set_history), quiet)
        self.poller.start(pid)

    def on_refresh(self) -> None:
        if self.project_id:
            self.load_state = "idle"
            self.load()

    # ------------------------------------------------------------------ статус прогона

    def _on_unavailable(self) -> None:
        self.unavailable = True
        self._apply_pending()
        self.render("run", "button")

    def _on_status(self, status: dict) -> None:
        if self.closed:
            return
        self.unavailable = False
        prev = self.prev_state
        self.status, self.prev_state = status, status.get("state")
        applied = self._apply_pending()
        if self._defaulted != self.project_id:
            # после сбоя по умолчанию предлагаем продолжить; один раз на проект
            self._defaulted = self.project_id
            if not applied and status.get("state") in ("failed", "interrupted") and self.resumable:
                self.set_mode("resume")
        toast = model.transition_toast(prev, status, self.titles)
        if toast:
            {"success": self.notify.success, "error": self.notify.error}.get(toast["type"], self.notify.info)(toast["text"])
            self._reload_history()
            self.on_run_changed()
            self.schedule_summary()
        rid = self.run_id
        if self.viewing_run:
            self.render("run", "steps", "banner", "button")
            return
        if rid and rid != self.log.run_id:
            self.log = log_model.reset_for(self.log, rid)
            self._log_busy = False
            self._log_gen += 1
            self.render("log")
        self.render("run", "steps", "banner", "button")
        if rid and (model.is_busy_state(self.run_state) or self.view.log_wanted()):
            self.poll_log()

    def _apply_pending(self) -> bool:
        if not self._pending_intent:
            return False
        pending, self._pending_intent = self._pending_intent, None
        self.apply_intent(*pending)
        return True

    def _reload_history(self) -> None:
        if not self.project_id:
            return

        def ok(res):
            self.history = history_rows(res.get("runs"))
            self.render("history")
        self._call("run_history", {"project_id": self.project_id, "limit": 20}, ok, lambda _r: None)

    def poll_log(self) -> None:
        rid = self.log.run_id
        if self._log_busy or not rid or not self.project_id:
            return
        self._log_busy = True
        gen = self._log_gen
        payload = {"project_id": self.project_id, "run_id": rid, "lines": LOG_FIRST_LINES}
        if self.log.offset > 0:
            payload["after_offset"] = self.log.offset

        def done():
            if gen == self._log_gen:
                self._log_busy = False

        def ok(res):
            if gen != self._log_gen:
                return
            done()
            self.log = log_model.apply_tail(self.log, rid, res)
            self.render("log")

        def fail(res):
            if gen != self._log_gen:
                return
            done()
            code, _reason, message, _d = SM.error_parts(res)
            self.log = log_model.apply_error(self.log, rid, code, message)
            self.render("log")

        self._call("run_log_tail", payload, ok, fail)

    def open_run_log(self, run_id: Optional[str]) -> None:
        """Журнал прошлого прогона из истории: один запрос, без опроса."""
        if not run_id:
            return
        self.viewing_run = run_id
        self._log_busy = False
        self._log_gen += 1
        self.log = log_model.LogState(run_id=run_id)
        self.render("log")
        self.view.show_tab("log")
        self.poll_log()

    def back_to_current_log(self) -> None:
        self.viewing_run = None
        self._log_busy = False
        self._log_gen += 1
        self.log = log_model.LogState(run_id=self.run_id)
        self.render("log")
        if self.run_id:
            self.poll_log()

    def pick_step(self, mode: str, step_id: Optional[str]) -> None:
        """«С этого шага» / «Только этот шаг»: готовим запуск, сам запуск по кнопке."""
        if model.is_busy_state(self.run_state) or mode not in ("from_step", "single_step"):
            return
        self.apply_intent(mode=mode, step_id=step_id)
        self.view.show_tab("settings")

    def clear_log(self) -> None:
        self.log = log_model.cleared(self.log)
        self.render("log")

    # ------------------------------------------------------------------ выбор режима и поля

    def apply_intent(self, mode=None, step_id=None, shot_keys=None) -> None:
        """Переход из других разделов: режим, шаг, отмеченные шоты."""
        if self.status is None and mode == "resume":
            # пока статус прогона неизвестен, «продолжить» нельзя решить: применим после первого ответа
            self._pending_intent = (mode, step_id, shot_keys)
            return
        self.selection = model.selection_from_intent(mode, step_id, shot_keys, self.selection, self.resumable)
        self.paid_pending = None
        self.render("mode", "button")
        self.refresh_plan()

    def set_mode(self, mode: str) -> None:
        if mode != "full":
            self.discard_next = False
        self.selection = model.Selection(model.resolve_mode(mode, self.resumable, len(self.selection.shot_keys)),
                                         self.selection.step_id, self.selection.shot_keys)
        self.paid_pending = None
        self.render("mode", "button")
        self.refresh_plan()

    def set_step(self, step_id: Optional[str]) -> None:
        self.selection = model.Selection(self.selection.mode, step_id, self.selection.shot_keys)
        self.render("button")
        self.refresh_plan()

    def set_task(self, text: str) -> None:
        self.task = text
        self.refresh_plan()

    def set_brief(self, key: str, value) -> None:
        self.brief = {**self.brief, key: value}
        self.refresh_plan()

    def set_regen_what(self, what: str) -> None:
        self.regen_what = what if what in REGEN_WHATS else "both"
        self.paid_pending = None
        self.refresh_plan()

    # ------------------------------------------------------------------ настройки

    def edit(self, section: str, key: str, value) -> None:
        if self.draft is None:
            return
        self.draft = form.patched(self.draft, section, key, value)
        self.render("settings", "button")
        self.schedule_summary()

    def edit_provider(self, provider: Optional[str]) -> None:
        if self.draft is not None:
            self.draft = form.change_provider(self.draft, provider)
            self.render("settings", "button")
            self.schedule_summary()

    def edit_style(self, value: str) -> None:
        if self.draft is not None:
            self.draft = form.with_style(self.draft, value)
            self.render("settings")
            self.schedule_summary()

    def edit_budget(self, text: str) -> None:
        self.budget_text = text
        value, error = form.parse_budget(text)
        self.budget_error = error
        if self.draft is not None and error is None:
            self.draft = form.patch_budget(self.draft, value)
        self.render("settings", "button")
        self.schedule_summary()

    def reset_defaults(self) -> None:
        if self.defaults is None or self.draft is None:
            return
        if not self.view.ask_confirm(ConfirmSpec(T.TK_TEXTS["tk_reset_title"], T.TK_TEXTS["tk_reset_body"],
                                                 T.TK_TEXTS["tk_reset_ok"], T.TK_TEXTS["tk_cancel"])):
            return
        self.draft = form.clone(self.defaults)
        self.budget_text, self.budget_error = form.budget_text(self.draft.get("budget_rub")), None
        self.render("settings", "button")
        self.schedule_summary()

    def locked_settings(self) -> bool:
        return model.settings_locked(self.run_state, bool((self.status or {}).get("locked")))

    def save_settings(self, then: Optional[Callable[[], None]] = None, on_fail: Optional[Callable[[], None]] = None) -> None:
        if self.draft is None or self.saving or not self.project_id:
            return
        if self.budget_error:
            self.notify.error(self.budget_error)
            if on_fail:
                on_fail()
            return
        self.saving = True
        pid = self.project_id
        payload = {"project_id": pid, "settings": self.draft, "expected_digest": self.digest}
        if self.locked_settings():
            # во время прогона меняется только бюджет: остальное берём из сохранённого
            payload["settings"] = form.patch_budget(self.saved or self.draft, self.draft.get("budget_rub"))

        def ok(res):
            self.saving = False
            if pid != self.project_id:
                return
            self.saved, self.digest = form.clone(res["settings"]), res.get("digest")
            self.draft = form.clone(res["settings"])
            self.issues = res.get("issues") or []
            self.budget_text = form.budget_text(self.draft.get("budget_rub"))
            self.notify.success(T.fmt("saved"))
            self.render("settings", "button")
            # продолжаем только когда план перестроен уже с новыми настройками
            self._after_plan = then
            self.refresh_plan(now=True)

        def fail(res):
            self.saving = False
            code, reason, message, _d = SM.error_parts(res)
            if code == "conflict":
                self.notify.warning(T.fmt("settings_conflict"))
            elif code == "locked":
                self.notify.warning(T.fmt("settings_locked"))
            else:
                self.notify.error(T.fmt("tk_settings_error", reason=message))
            if on_fail:
                on_fail()

        self._call("run_settings_save", payload, ok, fail)

    # ------------------------------------------------------------------ план и сводка

    def _cancel_plan_timer(self) -> None:
        if self._plan_timer is not None:
            try:
                self.cancel(self._plan_timer)
            except Exception:
                pass
            self._plan_timer = None

    def schedule_summary(self) -> None:
        self.refresh_plan()

    def refresh_plan(self, now: bool = False) -> None:
        """Пересчёт плана и сводки; с задержкой 500 мс, чтобы не считать на каждую букву."""
        if self.load_state != "ready":
            return
        self._cancel_plan_timer()
        self.plan_busy = True
        self.render("summary", "button")
        if now:
            self._recompute()
        else:
            self._plan_timer = self.schedule(SUMMARY_DELAY_MS, self._recompute)

    def _inputs(self):
        return form.collect_inputs(self.brief, (self.plan or {}).get("inputs"))

    def _plan_done(self) -> None:
        self.plan_busy = False
        after, self._after_plan = self._after_plan, None
        if after:
            after()

    def _recompute(self) -> None:
        self._plan_timer = None
        if self.closed or not self.project_id or self.load_state != "ready":
            return
        self._plan_gen += 1
        gen, pid = self._plan_gen, self.project_id
        if self.selection.mode == "regenerate_shots":
            self._recompute_regen(gen, pid)
            return
        inputs, error = self._inputs()
        if error:
            self.plan_error = error
            self._plan_done()
            self.render("summary", "button")
            return
        params = model.plan_params(self.selection, inputs, self.task)
        if self.selection.mode in ("from_step", "single_step") and not self.selection.step_id:
            self.plan = self.summary = self.estimate = None
            self.plan_busy, self.plan_error = False, None
            self.render("summary", "button")
            return

        def plan_ok(res):
            if gen != self._plan_gen:
                return
            self.plan, self.plan_error = res["plan"], None
            self.regen = None
            self._load_summary(gen, pid)

        def plan_fail(res):
            if gen != self._plan_gen:
                return
            self.plan, self.summary, self.estimate = None, None, None
            self.plan_error = SM.result_error_text(res)
            self._plan_done()
            self.render("summary", "button")

        self._call("run_plan", {"project_id": pid, **params}, plan_ok, plan_fail, key="generation.plan")

    def _load_summary(self, gen: int, pid: str) -> None:
        payload = {"project_id": pid, "step_ids": (self.plan or {}).get("steps_to_run") or []}
        if self.settings_dirty():
            payload["settings"] = self.draft

        def ok(res):
            if gen != self._plan_gen:
                return
            self.summary = res
            plan_est = (self.plan or {}).get("estimate")
            self.estimate = plan_est if costs.is_cost_estimate(plan_est) else res.get("estimate")
            self.estimate_error = None if self.estimate else T.fmt("estimate_unknown")
            self._plan_done()
            self.render("summary", "button")

        def fail(res):
            if gen != self._plan_gen:
                return
            self.summary = None
            plan_est = (self.plan or {}).get("estimate")
            self.estimate = plan_est if costs.is_cost_estimate(plan_est) else None
            self.estimate_error = None if self.estimate else SM.result_error_text(res)
            self._plan_done()
            self.render("summary", "button")

        self._call("run_preflight_summary", payload, ok, fail, key="generation.summary")

    def _recompute_regen(self, gen: int, pid: str) -> None:
        keys = list(self.selection.shot_keys)
        if not keys:
            self.regen, self.plan_error = None, None
            self._plan_done()
            self.render("summary", "button")
            return

        def ok(res):
            if gen != self._plan_gen:
                return
            self.regen, self.plan_error = res, None
            self._plan_done()
            self.render("summary", "button")

        def fail(res):
            if gen != self._plan_gen:
                return
            self.regen = None
            self.plan_error = SM.result_error_text(res)
            self._plan_done()
            self.render("summary", "button")

        self._call("regenerate_preview", {"project_id": pid, "what": self.regen_what, "shot_keys": keys},
                   ok, fail, key="generation.regen")

    # ------------------------------------------------------------------ кнопка запуска

    def regen_state(self) -> Optional[dict]:
        if not self.regen:
            return None
        view = SM.regen_view(self.regen)
        return {"can_start": view["can_start"], "cost_rub": view["cost_rub"], "unknown_cost": view["unknown_cost"],
                "problem": _first_error(self.regen.get("problems"))}

    def provider(self) -> Optional[dict]:
        return form.active_provider(self.providers, self.draft or self.saved)

    def button_input(self) -> dict:
        status = self.status or {}
        engine = status.get("engine") or {}
        provider = self.provider()
        return {
            "loading": self.load_state != "ready",
            "planBusy": self.plan_busy,
            "engineError": (engine.get("error") or "причина неизвестна") if engine.get("available") is False else None,
            "isAdmin": True,
            "runState": self.run_state,
            "locked": bool(status.get("locked")) and not model.is_busy_state(self.run_state),
            "selection": {"mode": self.selection.mode, "stepId": self.selection.step_id,
                          "shotKeys": list(self.selection.shot_keys)},
            "hasBrief": not any(p.get("code") == "invalid_task" for p in (self.plan or {}).get("problems") or []),
            "task": self.task, "plan": self.plan, "planError": self.plan_error, "issues": self.issues,
            "provider": {"title": provider.get("title"), "credentials_found": provider.get("credentials_found")} if provider else None,
            "estimate": self.estimate, "estimateError": self.estimate_error, "regen": self.regen_state(),
        }

    def start_button(self) -> model.StartButton:
        if self.starting:
            return model.StartButton(T.fmt("go"), False, None)
        return model.start_button(self.button_input(), self.titles)

    def banner(self) -> Optional[dict]:
        status = self.status
        if status and (status.get("other_surface") or {}).get("surface") == "tk":
            # сервер пишет «запущен в Tk» и про наш собственный прогон: у себя этого не показываем
            status = {**status, "other_surface": None}
        return model.banner_for(status, self.titles)

    # ------------------------------------------------------------------ запуск

    def request_run(self, mode: Optional[str] = None, skip_confirm: bool = False) -> None:
        """Нажатие «Запустить»: подтверждения, свежий план, старт. skip_confirm - только после «Поднять бюджет и запустить»."""
        if mode:
            self.set_mode(mode)
        button = self.start_button()
        if not button.enabled or not self.project_id:
            return
        if self.settings_dirty():
            self.save_settings(then=lambda: self.request_run(skip_confirm=skip_confirm))
            return
        if self.selection.mode == "regenerate_shots":
            self._run_regen(abandon=False)
            return
        if not skip_confirm:
            self.discard_next = False
            first = model.restart_dialog(self.selection.mode, self.run_state, self.resumable)
            if first:
                body = T.fmt("confirm_restart_body") if first == "restart" else T.fmt("confirm_discard_body")
                spec = ConfirmSpec(T.fmt("confirm_restart_title"), body, T.fmt("confirm_restart_ok"))
                if not self.view.ask_confirm(spec):
                    return
            self.discard_next = model.should_discard(self.selection.mode, self.resumable)
            choice = self._confirm_start()
            if choice == "raise":
                self._raise_budget_and_start()
                return
            if choice != "go":
                self.discard_next = False
                return
        self._start_run()

    def _money(self, value) -> str:
        return format_rub_amount(value)

    def _confirm_start(self) -> str:
        """go | raise | cancel. Окно выбирается по оценке: без оценки, выше бюджета или обычное."""
        est = self.estimate
        kind = model.confirm_kind(est, self.estimate_error)
        if kind == "over_budget" and est:
            return self.view.ask_choice(
                T.fmt("confirm_over_title"),
                T.fmt("confirm_over_body", cost=self._money(est.get("total_known_rub")),
                      sum=self._money(est.get("short_by_rub") or 0)),
                [("go", T.fmt("confirm_over_go")), ("raise", T.fmt("confirm_over_raise")),
                 ("cancel", T.fmt("confirm_cancel"))], "cancel") or "cancel"
        if kind == "no_estimate":
            spec = ConfirmSpec(T.fmt("confirm_none_title"), T.fmt("confirm_none_body"), T.fmt("confirm_none_ok"))
        else:
            spec = ConfirmSpec(T.fmt("confirm_start_title"),
                               T.fmt("confirm_start_body", mode=model.mode_label(self.selection.mode),
                                     cost=self._money(costs.estimate_total(est))),
                               T.fmt("confirm_start_ok"))
        return "go" if self.view.ask_confirm(spec) else "cancel"

    def _raise_budget_and_start(self) -> None:
        """«Поднять бюджет и запустить»: бюджет = оценка вверх, сохраняем, строим план заново, стартуем без второго окна."""
        total = math.ceil(costs.estimate_total(self.estimate))
        self.edit_budget(str(total))
        if self.budget_error:
            self.notify.error(self.budget_error)
            self.discard_next = False
            return

        def failed():
            self.discard_next = False

        self.save_settings(then=lambda: self.request_run(skip_confirm=True), on_fail=failed)

    def _run_params(self) -> Optional[dict]:
        inputs, error = self._inputs()
        if error:
            self.notify.error(error)
            return None
        return model.plan_params(self.selection, inputs, self.task)

    def _start_run(self) -> None:
        params = self._run_params()
        if params is None or not self.project_id or not self.plan:
            return
        pid = self.project_id
        shown_digest, shown_total = self.plan.get("plan_digest"), (
            costs.estimate_total(self.plan["estimate"]) if costs.is_cost_estimate(self.plan.get("estimate")) else None)
        # флаг «бросить неоконченный» только для полного запуска; продолжение его никогда не получает
        discard = self.discard_next and self.selection.mode == "full"
        self.starting = True
        self.render("button")

        def finish(text=None, level="warning", refresh=False):
            self.starting = False
            if text:
                getattr(self.notify, level)(text)
            self.render("button")
            if refresh:
                self.refresh_plan(now=True)

        def fresh_ok(res):
            fresh = res["plan"]
            if not fresh.get("can_start"):
                finish(_first_error(fresh.get("problems")) or T.fmt("changed_plan"), "warning", True)
                return
            if fresh.get("plan_digest") != shown_digest:
                finish(T.fmt("changed_plan"), "warning", True)
                return
            total = costs.estimate_total(fresh["estimate"]) if costs.is_cost_estimate(fresh.get("estimate")) else None
            if total is not None and shown_total is not None and total != shown_total:
                finish(T.fmt("changed_cost"), "warning", True)
                return
            payload = {"project_id": pid, **params, "launcher": LAUNCHER, "expected_plan_digest": fresh.get("plan_digest")}
            if discard:
                payload["discard_unfinished"] = True
            self._call("run_start", payload, started, start_fail)

        def started(res):
            self.discard_next = False
            # после успешного старта черновик брифа очищается: он уже ушёл в прогон
            self.task, self.brief = "", {}
            self.viewing_run = None
            finish()
            self.render("mode")
            self.notify.success(T.fmt("started"))
            self.log = log_model.EMPTY_LOG
            self.view.show_tab("progress")
            self.on_run_changed()
            self.poller.refresh()

        def start_fail(res):
            code, reason, message, details = SM.error_parts(res)
            if code == "locked":
                finish(SM.result_error_text(res), "error")
            elif code == "conflict" and reason == "plan_changed":
                finish(T.fmt("changed_plan"), "warning", True)
            elif code == "conflict" and reason == "plan_blocked":
                finish(_first_error(details.get("problems")) or message, "error", True)
            else:
                finish(SM.result_error_text(res), "error")

        def fresh_fail(res):
            finish(SM.result_error_text(res), "error")

        self._call("run_plan", {"project_id": pid, **params}, fresh_ok, fresh_fail)

    # ------------------------------------------------------------------ перегенерация шотов

    def _run_regen(self, abandon: bool) -> None:
        view = SM.regen_view(self.regen or {})
        cost = view["cost_rub"]
        if view["unknown_cost"]:
            spec = ConfirmSpec(T.fmt("confirm_none_title"), T.fmt("confirm_none_body"), T.fmt("confirm_none_ok"))
        else:
            spec = ConfirmSpec(T.fmt("confirm_start_title"),
                               T.fmt("confirm_start_body", mode=model.mode_label("regenerate_shots"), cost=self._money(cost)),
                               T.fmt("confirm_start_ok"))
        if abandon:
            # одно окно: сумма и предупреждение, что оплаченные задачи будут брошены
            spec = ConfirmSpec(T.fmt("inflight_abandon"), f"{T.fmt('inflight_abandon_hint')}. {spec.body}",
                               T.fmt("inflight_abandon"))
        if not self.view.ask_confirm(spec):
            return
        shown = self.regen or {}
        pid, keys, what = self.project_id, list(self.selection.shot_keys), self.regen_what
        self.starting = True
        self.render("button")

        def finish(text=None, level="warning", refresh=False):
            self.starting = False
            if text:
                getattr(self.notify, level)(text)
            self.render("button", "banner")
            if refresh:
                self.refresh_plan(now=True)

        def fresh_ok(fresh):
            if fresh.get("plan_digest") != shown.get("plan_digest"):
                finish(T.fmt("changed_plan"), "warning", True)
                return
            if (fresh.get("confirm_required") or {}).get("cost_rub") != (shown.get("confirm_required") or {}).get("cost_rub"):
                finish(T.fmt("changed_cost"), "warning", True)
                return
            base = {"project_id": pid, "what": what, "launcher": LAUNCHER, "shot_keys": keys}
            if abandon:
                base["abandon_pending_jobs"] = True
            self._call("regenerate_start", SM.start_payload_from_preview(base, fresh, True), started, start_fail)

        def started(_res):
            finish()
            self.paid_pending = None
            self.viewing_run = None
            self.notify.success(T.fmt("started"))
            self.log = log_model.EMPTY_LOG
            self.view.show_tab("progress")
            self.on_run_changed()
            self.poller.refresh()

        def start_fail(res):
            code, reason, message, details = SM.error_parts(res)
            info = SM.classify_regen_error(code, reason, message, details)
            kind = info["kind"]
            if kind == "need_abandon":
                self.paid_pending = info.get("keys") or []
                finish(None)
                self.view.show_tab("settings")
            elif kind == "refresh":
                finish(info["text"], "warning", True)
            elif kind == "blocked":
                problems = info.get("problems") or []
                finish((problems[0].get("message") if problems else None) or info["text"], "error", True)
            else:
                finish(info["text"], "error")

        self._call("regenerate_preview", {"project_id": pid, "what": what, "shot_keys": keys}, fresh_ok,
                   lambda res: finish(SM.result_error_text(res), "error"))

    def wait_for_pending(self) -> None:
        """Баннер K, «Дождаться»: ничего не запускаем."""
        self.paid_pending = None
        self.render("banner", "button")

    def abandon_and_regenerate(self) -> None:
        """Баннер K, «Бросить задания и перегенерировать»: отдельное подтверждение, затем повтор."""
        self._run_regen(abandon=True)

    # ------------------------------------------------------------------ пауза, остановка, баннеры

    def toggle_pause(self) -> None:
        if not self.project_id:
            return
        paused = self.run_state not in ("paused", "pause_requested")
        if self.pause_busy:
            return
        payload = {"project_id": self.project_id, "paused": paused}
        if self.run_id:
            payload["run_id"] = self.run_id
        self.pause_busy = True
        self.render("run")

        def done():
            self.pause_busy = False
            self.render("run")

        def ok(_res):
            done()
            self.notify.info(T.fmt("pause_requested") if paused else T.fmt("resumed"))
            self.poller.refresh()
            self.on_run_changed()

        def fail(res):
            done()
            self._report(res)

        self._call("run_set_pause", payload, ok, fail)

    def stop(self) -> None:
        if not self.project_id or self.stop_busy or self.run_state == "stopping" or not self.view.ask_confirm(stop_run_spec()):
            return
        payload = {"project_id": self.project_id}
        if self.run_id:
            payload["run_id"] = self.run_id
        self.stop_busy = True
        self.render("run")

        def done():
            self.stop_busy = False
            self.render("run")

        def ok(_res):
            done()
            self.notify.info(T.fmt("stop_requested"))
            self.poller.refresh()
            self.on_run_changed()

        def fail(res):
            done()
            self._report(res)

        self._call("run_cancel", payload, ok, fail)

    def mark_interrupted(self) -> None:
        if not self.project_id or not self.run_id:
            return
        spec = ConfirmSpec(T.fmt("confirm_mark_title"), T.fmt("confirm_mark_body"), T.fmt("confirm_mark_ok"))
        if not self.view.ask_confirm(spec):
            return

        def ok(_res):
            self.notify.info(T.fmt("marked"))
            self.poller.refresh()
            self.on_run_changed()

        def fail(res):
            code, _r, _m, _d = SM.error_parts(res)
            self.notify.warning(T.fmt("mark_locked")) if code == "locked" else self._report(res)

        self._call("run_mark_interrupted", {"project_id": self.project_id, "run_id": self.run_id}, ok, fail)

    def banner_action(self) -> None:
        """Кнопка баннера: продолжить (режим и план, без запуска), начать сначала, пометить, обновить."""
        banner = self.banner()
        action = banner and banner.get("action")
        if action == "mark_interrupted":
            self.mark_interrupted()
        elif action == "resume":
            self.set_mode("resume")
            self.view.show_tab("settings")
        elif action == "restart":
            # после успешного прогона неоконченного нет: просто готовим полный запуск
            self.set_mode("full")
            self.view.show_tab("settings")
        elif action == "refresh":
            self.poller.refresh()

    def shutdown(self) -> None:
        self.closed = True
        self.poller.stop()
        self._cancel_plan_timer()
