"""Event encoder for AG-UI SSE streams."""

from .events import BaseEvent


AGUI_MEDIA_TYPE = "application/vnd.ag-ui.event+proto"


class EventEncoder:
    def __init__(self, accept: str | None = None) -> None:
        self._accept = accept

    def get_content_type(self) -> str:
        return "text/event-stream"

    def encode(self, event: BaseEvent) -> str:
        return self._encode_sse(event)

    def _encode_sse(self, event: BaseEvent) -> str:
        payload = event.model_dump_json(by_alias=True, exclude_none=True)
        return f"data: {payload}\n\n"
