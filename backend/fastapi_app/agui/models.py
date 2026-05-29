"""AG-UI core types used by the FastAPI gateway."""

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        alias_generator=to_camel,
        populate_by_name=True,
    )


class FunctionCall(ConfiguredBaseModel):
    name: str
    arguments: str


class ToolCall(ConfiguredBaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class BaseMessage(ConfiguredBaseModel):
    id: str
    role: str
    content: Optional[str] = None
    name: Optional[str] = None


class DeveloperMessage(BaseMessage):
    role: Literal["developer"] = "developer"
    content: str


class SystemMessage(BaseMessage):
    role: Literal["system"] = "system"
    content: str


class AssistantMessage(BaseMessage):
    role: Literal["assistant"] = "assistant"
    tool_calls: Optional[List[ToolCall]] = None


class TextInputContent(ConfiguredBaseModel):
    type: Literal["text"] = "text"
    text: str


class BinaryInputContent(ConfiguredBaseModel):
    type: Literal["binary"] = "binary"
    mime_type: str
    id: Optional[str] = None
    url: Optional[str] = None
    data: Optional[str] = None
    filename: Optional[str] = None

    @model_validator(mode="after")
    def validate_source(self) -> "BinaryInputContent":
        if not any([self.id, self.url, self.data]):
            raise ValueError("BinaryInputContent requires id, url, or data to be provided.")
        return self


InputContent = Annotated[
    Union[TextInputContent, BinaryInputContent],
    Field(discriminator="type"),
]


class UserMessage(BaseMessage):
    role: Literal["user"] = "user"
    content: Union[str, List[InputContent]]


class ToolMessage(ConfiguredBaseModel):
    id: str
    role: Literal["tool"] = "tool"
    content: str
    tool_call_id: str
    error: Optional[str] = None


class ActivityMessage(ConfiguredBaseModel):
    id: str
    role: Literal["activity"] = "activity"
    activity_type: str
    content: Dict[str, Any]


Message = Annotated[
    Union[
        DeveloperMessage,
        SystemMessage,
        AssistantMessage,
        UserMessage,
        ToolMessage,
        ActivityMessage,
    ],
    Field(discriminator="role"),
]

Role = Literal["developer", "system", "assistant", "user", "tool", "activity"]


class Context(ConfiguredBaseModel):
    description: str
    value: str


class Tool(ConfiguredBaseModel):
    name: str
    description: str
    parameters: Any


class RunAgentInput(ConfiguredBaseModel):
    thread_id: str
    run_id: str
    parent_run_id: Optional[str] = None
    state: Any
    messages: List[Message]
    tools: List[Tool]
    context: List[Context]
    forwarded_props: Any


State = Any
