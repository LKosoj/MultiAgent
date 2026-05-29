"""AG-UI event models for the FastAPI gateway."""

from enum import Enum
from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import Field

from .models import ConfiguredBaseModel, Message, State, RunAgentInput


TextMessageRole = Literal["developer", "system", "assistant", "user"]


class EventType(str, Enum):
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    TEXT_MESSAGE_CHUNK = "TEXT_MESSAGE_CHUNK"
    THINKING_TEXT_MESSAGE_START = "THINKING_TEXT_MESSAGE_START"
    THINKING_TEXT_MESSAGE_CONTENT = "THINKING_TEXT_MESSAGE_CONTENT"
    THINKING_TEXT_MESSAGE_END = "THINKING_TEXT_MESSAGE_END"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_CALL_CHUNK = "TOOL_CALL_CHUNK"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    THINKING_START = "THINKING_START"
    THINKING_END = "THINKING_END"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"
    ACTIVITY_SNAPSHOT = "ACTIVITY_SNAPSHOT"
    ACTIVITY_DELTA = "ACTIVITY_DELTA"
    RAW = "RAW"
    CUSTOM = "CUSTOM"
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"


class BaseEvent(ConfiguredBaseModel):
    type: EventType
    timestamp: Optional[int] = None
    raw_event: Optional[Any] = None


class TextMessageStartEvent(BaseEvent):
    type: Literal[EventType.TEXT_MESSAGE_START] = EventType.TEXT_MESSAGE_START
    message_id: str
    role: TextMessageRole = "assistant"


class TextMessageContentEvent(BaseEvent):
    type: Literal[EventType.TEXT_MESSAGE_CONTENT] = EventType.TEXT_MESSAGE_CONTENT
    message_id: str
    delta: str = Field(min_length=1)


class TextMessageEndEvent(BaseEvent):
    type: Literal[EventType.TEXT_MESSAGE_END] = EventType.TEXT_MESSAGE_END
    message_id: str


class TextMessageChunkEvent(BaseEvent):
    type: Literal[EventType.TEXT_MESSAGE_CHUNK] = EventType.TEXT_MESSAGE_CHUNK
    message_id: Optional[str] = None
    role: Optional[TextMessageRole] = None
    delta: Optional[str] = None


class ThinkingTextMessageStartEvent(BaseEvent):
    type: Literal[EventType.THINKING_TEXT_MESSAGE_START] = EventType.THINKING_TEXT_MESSAGE_START


class ThinkingTextMessageContentEvent(BaseEvent):
    type: Literal[EventType.THINKING_TEXT_MESSAGE_CONTENT] = (
        EventType.THINKING_TEXT_MESSAGE_CONTENT
    )
    delta: str = Field(min_length=1)


class ThinkingTextMessageEndEvent(BaseEvent):
    type: Literal[EventType.THINKING_TEXT_MESSAGE_END] = EventType.THINKING_TEXT_MESSAGE_END


class ToolCallStartEvent(BaseEvent):
    type: Literal[EventType.TOOL_CALL_START] = EventType.TOOL_CALL_START
    tool_call_id: str
    tool_call_name: str
    parent_message_id: Optional[str] = None


class ToolCallArgsEvent(BaseEvent):
    type: Literal[EventType.TOOL_CALL_ARGS] = EventType.TOOL_CALL_ARGS
    tool_call_id: str
    delta: str


class ToolCallEndEvent(BaseEvent):
    type: Literal[EventType.TOOL_CALL_END] = EventType.TOOL_CALL_END
    tool_call_id: str


class ToolCallChunkEvent(BaseEvent):
    type: Literal[EventType.TOOL_CALL_CHUNK] = EventType.TOOL_CALL_CHUNK
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    parent_message_id: Optional[str] = None
    delta: Optional[str] = None


class ToolCallResultEvent(BaseEvent):
    message_id: str
    type: Literal[EventType.TOOL_CALL_RESULT] = EventType.TOOL_CALL_RESULT
    tool_call_id: str
    content: str
    role: Optional[Literal["tool"]] = None


class ThinkingStartEvent(BaseEvent):
    type: Literal[EventType.THINKING_START] = EventType.THINKING_START
    title: Optional[str] = None


class ThinkingEndEvent(BaseEvent):
    type: Literal[EventType.THINKING_END] = EventType.THINKING_END


class StateSnapshotEvent(BaseEvent):
    type: Literal[EventType.STATE_SNAPSHOT] = EventType.STATE_SNAPSHOT
    snapshot: State


class StateDeltaEvent(BaseEvent):
    type: Literal[EventType.STATE_DELTA] = EventType.STATE_DELTA
    delta: List[Any]


class MessagesSnapshotEvent(BaseEvent):
    type: Literal[EventType.MESSAGES_SNAPSHOT] = EventType.MESSAGES_SNAPSHOT
    messages: List[Message]


class ActivitySnapshotEvent(BaseEvent):
    type: Literal[EventType.ACTIVITY_SNAPSHOT] = EventType.ACTIVITY_SNAPSHOT
    message_id: str
    activity_type: str
    content: Any
    replace: bool = True


class ActivityDeltaEvent(BaseEvent):
    type: Literal[EventType.ACTIVITY_DELTA] = EventType.ACTIVITY_DELTA
    message_id: str
    activity_type: str
    patch: List[Any]


class RawEvent(BaseEvent):
    type: Literal[EventType.RAW] = EventType.RAW
    event: Any
    source: Optional[str] = None


class CustomEvent(BaseEvent):
    type: Literal[EventType.CUSTOM] = EventType.CUSTOM
    name: str
    value: Any


class RunStartedEvent(BaseEvent):
    type: Literal[EventType.RUN_STARTED] = EventType.RUN_STARTED
    thread_id: str
    run_id: str
    parent_run_id: Optional[str] = None
    input: Optional[RunAgentInput] = None


class RunFinishedEvent(BaseEvent):
    type: Literal[EventType.RUN_FINISHED] = EventType.RUN_FINISHED
    thread_id: str
    run_id: str
    result: Optional[Any] = None


class RunErrorEvent(BaseEvent):
    type: Literal[EventType.RUN_ERROR] = EventType.RUN_ERROR
    message: str
    code: Optional[str] = None


class StepStartedEvent(BaseEvent):
    type: Literal[EventType.STEP_STARTED] = EventType.STEP_STARTED
    step_name: str


class StepFinishedEvent(BaseEvent):
    type: Literal[EventType.STEP_FINISHED] = EventType.STEP_FINISHED
    step_name: str


Event = Annotated[
    Union[
        TextMessageStartEvent,
        TextMessageContentEvent,
        TextMessageEndEvent,
        TextMessageChunkEvent,
        ThinkingTextMessageStartEvent,
        ThinkingTextMessageContentEvent,
        ThinkingTextMessageEndEvent,
        ToolCallStartEvent,
        ToolCallArgsEvent,
        ToolCallEndEvent,
        ToolCallChunkEvent,
        ToolCallResultEvent,
        ThinkingStartEvent,
        ThinkingEndEvent,
        StateSnapshotEvent,
        StateDeltaEvent,
        MessagesSnapshotEvent,
        ActivitySnapshotEvent,
        ActivityDeltaEvent,
        RawEvent,
        CustomEvent,
        RunStartedEvent,
        RunFinishedEvent,
        RunErrorEvent,
        StepStartedEvent,
        StepFinishedEvent,
    ],
    Field(discriminator="type"),
]
