"""Production process wrapper for the durable workflow supervisor."""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path

from backend.fastapi_app.agui.store import EventStore
from workflow.state_files import (
    LegacyDatabaseUnrecoverable,
    default_state_database_path,
)
from workflow.streamlit_api import configure_workflow_process_supervisor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_AGUI_EVENTS_DB_PATH = PROJECT_ROOT / "data" / "agui_events.db"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop_requested = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        db_path = default_state_database_path(
            PROJECT_ROOT,
            "agui_events.db",
            legacy_path=_LEGACY_AGUI_EVENTS_DB_PATH,
        )
    except LegacyDatabaseUnrecoverable as exc:
        logging.getLogger(__name__).critical(
            "Legacy AGUI event store database is unrecoverable "
            "(reason_code=%s): %s",
            exc.reason_code,
            _LEGACY_AGUI_EVENTS_DB_PATH,
        )
        raise
    store = EventStore(str(db_path))
    supervisor = configure_workflow_process_supervisor(store)
    try:
        supervisor.start()
        stop_requested.wait()
    finally:
        try:
            supervisor.stop()
        finally:
            store.close()


if __name__ == "__main__":
    main()

