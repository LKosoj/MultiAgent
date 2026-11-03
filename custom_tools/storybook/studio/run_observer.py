"""Наблюдатель за прогоном Tk (PipelineRunner): те же управляющие файлы и журнал, что у студии."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any, Optional

from custom_tools.storybook import project_paths

from . import run_control, run_exec

logger = logging.getLogger(__name__)


def _mode_for(workflow_id: str) -> str:
    if workflow_id.startswith("sbm_single_"):
        return "single_step"
    if workflow_id.startswith("sbm_partial_"):
        return "from_step"
    return "full"


class TkRunObserver:
    """Пишет active_run.json, control.json, step_runs.json и журнал; ничего не меняет в ходе прогона."""

    def __init__(self, engine: Any, project_id: str, workflow_id: str, step_ids: list[str]) -> None:
        root = project_paths.safe_storybook_project_dir(project_id, must_exist=True)
        self.project_id = project_id
        self.plan = {"version": 1, "mode": _mode_for(workflow_id), "project_id": project_id,
                     "workflow_id": workflow_id, "steps_to_run": list(step_ids), "skip_steps": []}
        self.session = run_exec.RunSession(engine, self.plan, root, run_id=f"tk-{uuid.uuid4().hex[:12]}",
                                           surface="tk", launcher="local")
        self.started_at = time.time()
        self.session.begin()
        self._monitor = asyncio.ensure_future(self.session.monitor())

    @property
    def run_id(self) -> str:
        return self.session.run_id

    def request(self, *, pause: Optional[bool] = None, cancel: bool = False) -> None:
        """Пауза/возобновление/остановка из Tk попадают в control.json, чтобы их видел React."""
        run_control.control_request(self.session.root, self.run_id, pause=pause, cancel=cancel or None, by="tk")
        if cancel:
            self.session.cancel_sent = True  # engine.cancel_workflow вызывает сам Tk
            run_control.control_patch(self.session.root, self.run_id, ack={"cancel": True})
            self.session._touch(state="stopping", message="Остановка прогона")

    async def finish(self, result: Any, error: Optional[BaseException] = None) -> None:
        self._monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._monitor
        status = run_exec._status_str(result)
        outcome = ("failed" if error is not None else
                   "cancelled" if (self.session.cancel_sent or status == "cancelled") else "completed")
        run_exec._finish(self.plan, self.project_id, {}, self.started_at, outcome)
        self.session.end(result, error)
