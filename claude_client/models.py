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


@dataclass
class ProjectExport:
    """A Claude web project exported to a local representation."""

    uuid: str
    name: str
    description: str
    instructions: str
    memory: str = ""
    controls: list[str] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)

    def to_markdown(self) -> str:
        sections: list[str] = []

        sections.append(f"# {self.name}\n")

        if self.description:
            sections.append(f"## Description\n\n{self.description}\n")

        if self.instructions:
            sections.append(f"## Instructions\n\n{self.instructions}\n")

        if self.memory:
            sections.append(f"## Memory\n\n{self.memory}\n")

        if self.controls:
            controls_md = "\n".join(f"- {c}" for c in self.controls)
            sections.append(f"## Controls\n\n{controls_md}\n")

        if self.docs:
            sections.append("## Knowledge Files\n")
            for doc in self.docs:
                name = doc.get("file_name", "untitled")
                content = doc.get("content", "")
                sections.append(f"### {name}\n\n```\n{content}\n```\n")

        return "\n".join(sections)
