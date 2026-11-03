"""Правка картинки ИИ без tkinter: предпросмотр, запуск, опрос задания.

Зависимости передаются снаружи:
  bridge  - StudioBridge: call(capability_id, payload, on_success=, on_error=);
  runner  - JobRunner: start(...) запускает возможность и раз в секунду опрашивает projects_job_status;
  notify  - тосты: success, info, warning, error;
  get_project_id - id открытого проекта или None.
Ответы приходят в главном потоке (это делает мост), устаревшие предпросмотры отбрасываются по поколению.
"""

import logging
from typing import Callable, List, Optional

from StoryBookManager.gui.imagetools import edit_model as EM
from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT
from StoryBookManager.gui.screens.calls import CAP_IMAGE_EDIT_PREVIEW, CAP_IMAGE_EDIT_START, CAP_MEDIA_INVENTORY
from StoryBookManager.gui.shots.model import error_parts

logger = logging.getLogger(__name__)

SURFACE = "tk"
REFERENCES_LIMIT = 500


class EditController:
    def __init__(self, bridge, runner, notify, get_project_id: Callable[[], Optional[str]]):
        self.bridge = bridge
        self.runner = runner
        self.notify = notify
        self.get_project_id = get_project_id
        self.closed = False
        self._preview_gen = 0

    def is_running(self) -> bool:
        return self.runner.is_busy(self.get_project_id())

    def close(self) -> None:
        self.closed = True

    def _call(self, capability: str, payload: dict, ok: Callable, fail: Callable,
              project_id: Optional[str] = None) -> None:
        project = project_id or self.get_project_id()
        self.bridge.call(capability, {"project_id": project, **payload}, on_success=ok, on_error=fail)

    def load_references(self, exclude_path: str, on_ok: Callable[[List[dict]], None],
                        on_fail: Callable[[str], None], project_id: Optional[str] = None) -> None:
        """Картинки проекта, которые можно взять образцами (без правимой). project_id - проект, где открыли диалог."""
        self._call(CAP_MEDIA_INVENTORY, {"kinds": ["image"], "limit": REFERENCES_LIMIT, "probe_limit": 0},
                   lambda r: None if self.closed else on_ok(EM.reference_choices(r.get("items") or [], exclude_path)),
                   lambda r: None if self.closed else on_fail(error_parts(r)[2]), project_id)

    def preview(self, target: dict, refs: List[str], on_ok: Callable[[dict], None],
                on_fail: Callable[[str], None], project_id: Optional[str] = None) -> None:
        """Цена, проблемы и digest плана. Ответ на более раннее обращение отбрасывается."""
        self._preview_gen += 1
        gen = self._preview_gen
        payload = EM.target_payload(target)
        if refs:
            payload["reference_paths"] = list(refs)

        def ok(result):
            if gen == self._preview_gen and not self.closed:
                on_ok(result)

        def fail(result):
            if gen == self._preview_gen and not self.closed:
                on_fail(error_parts(result)[2])

        self._call(CAP_IMAGE_EDIT_PREVIEW, payload, ok, fail, project_id)

    def start(self, target: dict, prompt: str, refs: List[str], preview: dict, *,
              on_started: Callable[[], None], on_fail: Callable[[dict], None],
              on_done: Callable[[dict], None], on_status: Callable[[str], None],
              project_id: Optional[str] = None) -> None:
        """Запуск правки. Ошибка запуска -> on_fail({kind, text}); итог задания -> on_done / тост ошибки.

        project_id - проект, в котором открыли диалог: пока он открыт, пользователь мог переключить проект."""
        project = project_id or self.get_project_id()
        payload = {"project_id": project, **EM.start_payload(target, prompt, refs, preview, SURFACE)}

        def started(_result):
            on_status(IT["edit_running"])
            self.notify.info(IT["edit_running"])
            on_started()

        def finished(result):
            on_status("")
            self.notify.success(IT["edit_done"])
            if not self.closed:
                on_done(result)

        def failed_job(error):
            on_status("")
            self.notify.error(self._error_text(error))

        def failed_start(error):
            reason, unknown = self._reason(error)
            text_id = EM.edit_error_text(error.get("code"), reason, unknown)
            refresh = reason in ("plan_changed", "cost_changed", "cost_confirmation_required")
            if not self.closed:
                on_fail({"kind": "refresh" if refresh else "failed", "text": IT[text_id]})

        # одна и та же функция получает ошибки запуска и ошибки задания: различаем по тому, начался ли опрос
        state = {"started": False}

        def on_started_wrapper(result):
            state["started"] = True
            started(result)

        def on_error(error):
            (failed_job if state["started"] else failed_start)(error)

        if not self.runner.start(CAP_IMAGE_EDIT_START, payload, project_id=project, on_started=on_started_wrapper,
                                 on_done=finished, on_error=on_error):
            on_fail({"kind": "failed", "text": IT["edit_busy"]})

    @staticmethod
    def _reason(error: dict) -> tuple:
        details = error.get("details") or {}
        return details.get("reason"), details.get("unknown_cost") is True

    def _error_text(self, error: dict) -> str:
        return IT[EM.edit_error_text(error.get("code"), *self._reason(error))]
