from dataclasses import dataclass, field
from typing import TypedDict


class OrgDict(TypedDict):
    uuid: str
    name: str
    capabilities: list[str]


class ProjectDict(TypedDict):
    uuid: str
    name: str
    description: str
    prompt_template: str
    created_at: str
    updated_at: str


class DocDict(TypedDict):
    uuid: str
    file_name: str
    content: str
    created_at: str


class MemoryDict(TypedDict):
    memory: str
    controls: list[str]
    updated_at: str


class PaginationDict(TypedDict):
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass
class Page[T]:
    data: list[T]
    pagination: PaginationDict


class ConversationDict(TypedDict):
    uuid: str
    name: str
    summary: str
    model: str
    created_at: str
    updated_at: str
    is_starred: bool
    is_temporary: bool
    project_uuid: str
    current_leaf_message_uuid: str


class MessageContentDict(TypedDict):
    type: str
    text: str


class ChatMessageDict(TypedDict):
    uuid: str
    sender: str
    content: list[MessageContentDict]
    parent_message_uuid: str
    index: int
    created_at: str
    updated_at: str


class ConversationDetailDict(TypedDict):
    uuid: str
    name: str
    summary: str
    model: str
    created_at: str
    updated_at: str
    current_leaf_message_uuid: str
    chat_messages: list[ChatMessageDict]


@dataclass
class ProjectExport:
    """A Claude web project exported to a local representation."""

    uuid: str
    name: str
    description: str
    instructions: str
    memory: str = ""
    controls: list[str] = field(default_factory=list)
    docs: list[DocDict] = field(default_factory=list)
    conversations: list[ConversationDetailDict] = field(default_factory=list)
