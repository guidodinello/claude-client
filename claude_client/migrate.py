"""Migrate a project's content from one Claude.ai account/org to another."""

from logger import get_logger
from rich.progress import track

from .client import ClaudeClient
from .render import conversation_to_markdown

logger = get_logger(__name__)


def migrate_project(
    source: ClaudeClient,
    source_project_id: str,
    dest: ClaudeClient,
    dest_project_id: str,
    *,
    include_conversations: bool = True,
    include_memory: bool = True,
) -> dict[str, int]:
    """
    Copy a project's docs, metadata, conversations, and memory into another project.

    Conversations can't be recreated as live chats through the API, so they're
    uploaded as knowledge docs. Memory is auto-generated with no write endpoint, so
    it's likewise uploaded as a doc snapshot rather than restored as live memory.

    Uses upsert semantics throughout, so re-running is safe and won't duplicate docs.
    Returns a summary count per category.
    """
    project = source.get_project(source_project_id)
    description = project.get("description") or None
    instructions = project.get("prompt_template") or None
    if description is not None or instructions is not None:
        # The API rejects a PUT with no fields ("must update at least one field"),
        # so skip the call entirely when the source has nothing to copy.
        dest.update_project(dest_project_id, description=description, instructions=instructions)

    counts = {"docs": 0, "conversations": 0, "memory": 0}

    for meta in track(source.list_docs(source_project_id), description="Migrating docs…"):
        doc = source.get_doc(source_project_id, meta["uuid"])
        dest.upsert_content(dest_project_id, doc.get("content", ""), doc["file_name"])
        counts["docs"] += 1

    if include_conversations:
        convs = source.list_all_conversations(source_project_id)
        for conv_meta in track(convs, description="Migrating conversations…"):
            try:
                conv = source.get_conversation(conv_meta["uuid"])
            except Exception:
                logger.warning(
                    "Failed to fetch conversation %s, skipping", conv_meta.get("uuid", "unknown")
                )
                continue
            content = conversation_to_markdown(conv)
            file_name = f"Conversation - {conv.get('name', 'Untitled')}"
            dest.upsert_content(dest_project_id, content, file_name)
            counts["conversations"] += 1

    if include_memory:
        memory = source.get_memory(source_project_id).get("memory", "")
        if memory:
            dest.upsert_content(dest_project_id, memory, "Project Memory (migrated)")
            counts["memory"] += 1

    return counts
