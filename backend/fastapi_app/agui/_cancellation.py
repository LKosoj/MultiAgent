"""Task-local authorization for durable AG-UI workflow cancellation."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass
class RunCancellationIntent:
    request_id: Optional[str] = None
    provenance: Optional[str] = None

    def authorize(self, request_id: str, provenance: str) -> None:
        if self.request_id is None:
            self.request_id = request_id
            self.provenance = provenance

    @property
    def authorized(self) -> bool:
        return self.request_id is not None and self.provenance is not None


_RUN_CANCELLATION_INTENT: contextvars.ContextVar[
    Optional[RunCancellationIntent]
] = contextvars.ContextVar("agui_run_cancellation_intent", default=None)


@contextmanager
def bind_run_cancellation_intent(
    intent: RunCancellationIntent,
) -> Iterator[None]:
    token = _RUN_CANCELLATION_INTENT.set(intent)
    try:
        yield
    finally:
        _RUN_CANCELLATION_INTENT.reset(token)


def current_run_cancellation_intent() -> Optional[RunCancellationIntent]:
    intent = _RUN_CANCELLATION_INTENT.get()
    if intent is None or not intent.authorized:
        return None
    return intent


def run_cancellation_context_is_bound() -> bool:
    return _RUN_CANCELLATION_INTENT.get() is not None

