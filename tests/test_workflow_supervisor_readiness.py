from __future__ import annotations

from workflow.process_supervisor import WorkflowProcessSupervisor


def _entrypoint(_run_id, _work_spec, _claim) -> None:
    return None


class _SchedulerThread:
    def __init__(self, **_kwargs) -> None:
        self.alive = False

    def start(self) -> None:
        self.alive = True

    def join(self, timeout=None) -> None:
        del timeout
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


def test_supervisor_public_readiness_requires_live_accepting_scheduler() -> None:
    supervisor = WorkflowProcessSupervisor(
        object(),
        process_entrypoint=_entrypoint,
        thread_factory=_SchedulerThread,
    )

    assert supervisor.is_accepting() is False
    assert supervisor.start() is True
    assert supervisor.is_accepting() is True

    supervisor._scheduler_thread.alive = False
    assert supervisor.is_accepting() is False
    supervisor._scheduler_thread.alive = True
    supervisor._stop_event.set()
    assert supervisor.is_accepting() is False
    supervisor.stop()
