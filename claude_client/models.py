from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict


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
    """
    project_uuid and estimated_token_count are present on list()/get() responses (the
    docs list endpoint, verified live) but NotRequired because the create POST response
    (`DocsResource._create`) likely omits these server-computed fields.
    """

    uuid: str
    file_name: str
    content: str
    created_at: str
    project_uuid: NotRequired[str]
    estimated_token_count: NotRequired[int]


class ProjectImportDict(TypedDict):
    """Legacy-to-melange migration status of a project's memory."""

    status: str
    migrated_at: str | None
    withheld_reason: str | None


class MemoryDict(TypedDict):
    """
    Legacy memory endpoint response. Once a project's memory moved to melange
    (`project_import.status == "migrated"`, `project_memory_mode == "melange"`), the
    `memory`/`controls` fields are a frozen pre-migration snapshot; the live memory is
    the per-entry melange tree (see `MelangeEntryDict`).
    """

    memory: str
    controls: list[str]
    updated_at: str
    project_import: NotRequired[ProjectImportDict]
    project_memory_mode: NotRequired[str]


class MelangeEntryDict(TypedDict):
    """
    One memory file from `melange/list` (metadata only, no content). Melange memory is
    org-wide and path-addressed: `/profile.md`, `/topics/x.md`,
    `/projects/{project_uuid}/topics/x.md`, ... `version` changes when the content does.
    """

    path: str
    size_bytes: int
    updated_at: str
    memory_id: str
    version: str
    display_name: str
    category_id: str | None
    display_path_segments: list[str]
    description: str
    project_uuid: str | None


class MelangeFileDict(TypedDict):
    """
    `melange/read` response: the entry's metadata plus `content`, the raw markdown file
    (YAML frontmatter included). `memory_id`/`description` come back empty here, so take
    them from `melange/list` instead. The parsed/derived fields are not typed.
    """

    path: str
    version: str
    updated_at: str
    content: str


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


class ChatFileDict(TypedDict):
    """
    One entry of a message's `files` list (verified live on 2026-11-08). Uploaded images
    have file_kind "image" and a preview_url (a compressed webp preview, not the original
    upload); other uploads have file_kind "blob" and no preview_url — their text is
    already inlined in the conversation.
    """

    file_kind: str
    file_uuid: str
    file_name: str
    size_bytes: NotRequired[int]
    thumbnail_url: NotRequired[str]
    preview_url: NotRequired[str]


class ChatMessageDict(TypedDict):
    """
    `files_v2` and `attachments` are scanned for images too (deduped against `files` by
    file_uuid), but their shape hasn't been verified against a live conversation.
    """

    uuid: str
    sender: str
    content: list[MessageContentDict]
    parent_message_uuid: str
    index: int
    created_at: str
    updated_at: str
    files: NotRequired[list[ChatFileDict]]
    files_v2: NotRequired[list[ChatFileDict]]
    attachments: NotRequired[list[dict]]


class ConversationDetailDict(TypedDict):
    uuid: str
    name: str
    summary: str
    model: str
    created_at: str
    updated_at: str
    current_leaf_message_uuid: str
    chat_messages: list[ChatMessageDict]


@dataclass(slots=True)
class ProjectExport:
    """A Claude web project exported to a local representation."""

    uuid: str
    name: str
    description: str
    instructions: str
    memory: str = ""
    # True once the project's memory moved to melange: `memory` is then a frozen
    # pre-migration snapshot, not the live memory.
    memory_frozen: bool = False
    controls: list[str] = field(default_factory=list)
    docs: list[DocDict] = field(default_factory=list)
    conversations: list[ConversationDetailDict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProjectSyncResult:
    """Result of syncing a project's docs and conversations to a local directory."""

    path: Path
    docs: dict[str, str]
    conversations: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProjectPullResult:
    """Outcome of one project's pull inside `ProjectsResource.pull_all`.

    Truthy when the pull succeeded, so callers that treat the mapping's values as
    booleans keep working; `error` carries the exception so callers can make their own
    retry/backoff decision per project instead of the library absorbing it.
    """

    ok: bool
    error: Exception | None = None

    def __bool__(self) -> bool:
        return self.ok
