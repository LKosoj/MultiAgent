"""Запуск прогона: Tk (поток внутри процесса) и React (менеджер воркфлоу бэкенда).

Бэкенд подключается лениво: модуль studio не должен тянуть FastAPI при импорте.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from typing import Any

from custom_tools.storybook import project_paths

from . import run_control, run_exec
from .errors import StudioError

logger = logging.getLogger(__name__)

_PACKAGE = "custom_tools.storybook.studio"
WORKFLOW_NAME = "storybook_pipeline"
LAUNCHERS = ("backend", "local")


def new_run_id(launcher: str) -> str:
    return f"tk-{uuid.uuid4().hex[:12]}" if launcher == "local" else f"run-{uuid.uuid4().hex}"


def reservation_record(run_id: str, plan: dict, launcher: str) -> dict:
    surface = "tk" if launcher == "local" else "react"
    return {"version": 1, "run_id": run_id, "workflow_id": plan["workflow_id"], "mode": plan["mode"],
            "surface": surface, "launcher": launcher, "started_at": run_control.iso(),
            "step_total": len(plan["steps_to_run"]), "steps": list(plan["steps_to_run"])}


def start_local(project_id: str, plan: dict, run_id: str) -> None:
    """Замок берётся сразу, а сам прогон идёт в фоновом потоке (kind=run вне общего семафора)."""
    jobs = importlib.import_module(f"{_PACKAGE}.jobs")
    root = project_paths.safe_storybook_project_dir(project_id, must_exist=True)
    engine = run_exec.get_engine()  # до замка: упавший движок не должен оставить замок
    handle = run_control.acquire_lock_with_retries(project_id)
    if handle is None:
        raise run_control.locked_error(root)

    def work(progress) -> dict:
        try:
            response = run_exec.execute_plan_sync(engine, plan, run_id=run_id, surface="tk",
                                                  launcher="local", lock_held=True)
        finally:
            run_control.release_lock(handle)
        return {"status": response.get("status"), "message": response.get("message")}

    try:
        jobs.start_job(project_paths.storybook_projects_root(), "run", project_id, work)
    except BaseException:
        run_control.release_lock(handle)
        raise


def system_owner() -> tuple[Any, Any]:
    """Владелец прогона: диспетчер действий не передаёт пользователя, поэтому системный admin."""
    auth = importlib.import_module("backend.fastapi_app.agui.auth")
    from workflow.streamlit_api import WorkflowOwner
    principal = auth.system_principal_for_disabled_mode()
    owner = WorkflowOwner(subject=principal.subject, tenant_id=principal.tenant_id, roles=principal.roles)
    return principal, owner


def start_backend(project_id: str, plan: dict, run_id: str) -> None:
    try:
        service = importlib.import_module("backend.fastapi_app.agui.service")
        principal, owner = system_owner()
    except ImportError as exc:
        raise StudioError("unavailable", "Бэкенд запуска недоступен") from exc
    session_id = f"storybook-{project_id}-{uuid.uuid4().hex[:8]}"
    service._agui_event_store().create_run(run_id, session_id, principal, run_kind="agui")
    service._wf_manager().start_workflow(
        workflow_name=WORKFLOW_NAME,
        parameters={"project_id": project_id, "studio_plan": plan, "studio_run_id": run_id},
        session_id=session_id, client_id=owner.quota_identity, use_enhanced=True,
        enable_telemetry=False, run_id=run_id, owner=owner)


def start(project_id: str, plan: dict, run_id: str, launcher: str) -> None:
    if launcher == "local":
        start_local(project_id, plan, run_id)
    elif launcher == "backend":
        start_backend(project_id, plan, run_id)
    else:
        raise StudioError("invalid_argument", "launcher: backend или local")


def cancel_backend(run_id: str) -> bool:
    """Жёсткая остановка через менеджер воркфлоу (убивает группу процессов)."""
    service = importlib.import_module("backend.fastapi_app.agui.service")
    return bool(service._wf_manager().cancel_workflow(run_id))
