"""Rendering helpers for Claude project data."""

import re
from collections.abc import Mapping

from .models import (
    ChatFileDict,
    ChatMessageDict,
    ConversationDetailDict,
    MessageContentDict,
    ProjectExport,
)

ROOT_MESSAGE_UUID = "00000000-0000-4000-8000-000000000000"

_IMAGE_FILE_KIND = "image"
_MESSAGE_FILE_KEYS = ("files", "files_v2", "attachments")


def message_images(msg: ChatMessageDict) -> list[ChatFileDict]:
    """
    The downloadable images attached to a message, deduped by file_uuid across every key
    that may carry files. Non-image uploads ("blob") are skipped: their text is already
    part of the conversation.
    """
    seen: set[str] = set()
    images: list[ChatFileDict] = []
    for key in _MESSAGE_FILE_KEYS:
        for f in msg.get(key) or []:
            file_uuid = f.get("file_uuid")
            if (
                f.get("file_kind") != _IMAGE_FILE_KIND
                or not f.get("preview_url")
                or not file_uuid
                or file_uuid in seen
            ):
                continue
            seen.add(file_uuid)
            images.append(f)
    return images


def conversation_to_markdown(
    conv: ConversationDetailDict, attachments: Mapping[str, str] | None = None
) -> str:
    """
    Render a conversation detail dict as markdown, showing all branches.

    `attachments` maps an image's file_uuid to the relative path it was saved at; each
    image is then linked inline at the message it was attached to, or marked as not
    downloaded if its uuid is missing from the map. With `attachments=None` (e.g. a
    single-file export, which saves no images) images aren't mentioned at all.
    """
    lines: list[str] = []

    name = conv.get("name", "Untitled")
    lines.append(f"### {name}\n")

    model = conv.get("model", "")
    created = conv.get("created_at", "")
    if created:
        created = created.replace("T", " ").replace("Z", "")
    lines.append(f"**Model:** {model}  |  **Created:** {created}\n")

    messages = conv.get("chat_messages", [])
    if not messages:
        return "\n".join(lines)

    def find_roots() -> list[ChatMessageDict]:
        roots = []
        for m in messages:
            if m.get("parent_message_uuid") == ROOT_MESSAGE_UUID:
                roots.append(m)
        return roots

    def find_all_paths(start: ChatMessageDict) -> list[list[ChatMessageDict]]:
        paths: list[list[ChatMessageDict]] = []

        def dfs(node: ChatMessageDict, path: list[ChatMessageDict]) -> None:
            path.append(node)
            children = [m for m in messages if m.get("parent_message_uuid") == node["uuid"]]

            if not children:
                paths.append(path.copy())
            else:
                for child in children:
                    dfs(child, path.copy())

        dfs(start, [])
        return paths

    def format_content(content_list: list[MessageContentDict]) -> str:
        parts = []
        for c in content_list:
            match c.get("type"):
                case "text":
                    text = c.get("text", "")
                    if text:
                        parts.append(text)
                case "tool_use":
                    name = c.get("name", "")
                    inp = c.get("input", {})
                    parts.append(f"[Tool: {name}]\n{inp}")
                case "tool_result":
                    result = c.get("content", "")
                    if isinstance(result, list):
                        result = "\n".join(
                            item.get("text", "") for item in result if item.get("text")
                        )
                    if result:
                        parts.append(f"[Result]\n{result}")
                case "thinking":
                    thinking = c.get("thinking", "")
                    if thinking:
                        parts.append(f"[Thinking]\n{thinking}")
        return "\n\n".join(parts)

    def format_images(msg: ChatMessageDict) -> str:
        if attachments is None:
            return ""
        parts = []
        for image in message_images(msg):
            name = re.sub(r"([\[\]])", r"\\\1", image.get("file_name", "image"))
            path = attachments.get(image["file_uuid"])
            parts.append(f"![{name}]({path})" if path else f"[Image not downloaded: {name}]")
        return "\n\n".join(parts)

    lines.append("")
    roots = find_roots()
    if roots:
        thread_idx = 0
        for root in roots:
            paths = find_all_paths(root)
            for path in paths:
                thread_idx += 1
                if len(roots) > 1 or len(paths) > 1:
                    lines.append(f"#### Thread {thread_idx}\n")
                for msg in path:
                    sender = msg.get("sender", "unknown")
                    if sender == "human":
                        label = "Human"
                    elif sender == "assistant":
                        label = "Claude"
                    else:
                        label = sender.capitalize()
                    text = "\n\n".join(
                        part
                        for part in (format_content(msg.get("content", [])), format_images(msg))
                        if part
                    )
                    if text:
                        lines.append(f"**{label}**\n{text}\n")
                lines.append("")

    lines.append("")
    return "\n".join(lines)


def slugify(s: str, fallback: str = "untitled") -> str:
    """Sanitize a string into a lowercase, hyphen-separated slug safe for filenames."""
    s = re.sub(r"[^\w\-]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.lower().strip("-")
    return s[:50] or fallback


def conversation_filename(conv: ConversationDetailDict) -> str:
    """Generate a sanitized filename from conversation name."""
    name = conv.get("name", "Untitled")
    uuid = conv.get("uuid", "")
    return f"{slugify(name, fallback='conversation')}-{uuid[:8]}.md"


def render_project_metadata(export: ProjectExport) -> str:
    """Render the project header fields (name, description, instructions, memory, controls)."""
    sections: list[str] = []

    sections.append(f"# {export.name}\n")

    if export.description:
        sections.append(f"## Description\n\n{export.description}\n")

    if export.instructions:
        sections.append(f"## Instructions\n\n{export.instructions}\n")

    if export.memory:
        heading = "Memory (frozen pre-migration snapshot)" if export.memory_frozen else "Memory"
        sections.append(f"## {heading}\n\n{export.memory}\n")

    if export.controls:
        controls_md = "\n".join(f"- {c}" for c in export.controls)
        sections.append(f"## Controls\n\n{controls_md}\n")

    return "\n".join(sections)


def render_project(export: ProjectExport) -> str:
    """Render a ProjectExport as a single markdown document."""
    sections: list[str] = [render_project_metadata(export)]

    if export.docs:
        sections.append("## Knowledge Files\n")
        for doc in export.docs:
            name = doc.get("file_name", "untitled")
            content = doc.get("content", "")
            sections.append(f"### {name}\n\n```\n{content}\n```\n")

    if export.conversations:
        sections.append("## Conversations\n")
        for conv in export.conversations:
            conv_md = conversation_to_markdown(conv)
            sections.append(conv_md)

    return "\n".join(sections)
