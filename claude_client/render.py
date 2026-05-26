"""Rendering helpers for Claude project data."""

import re

from .models import ProjectExport

ROOT_MESSAGE_UUID = "00000000-0000-4000-8000-000000000000"


def conversation_to_markdown(conv: dict) -> str:
    """Render a conversation detail dict as markdown, showing all branches."""
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

    def find_roots() -> list[dict]:
        roots = []
        for m in messages:
            if m.get("parent_message_uuid") == ROOT_MESSAGE_UUID:
                roots.append(m)
        return roots

    def find_all_paths(start: dict) -> list[list[dict]]:
        paths: list[list[dict]] = []

        def dfs(node: dict, path: list[dict]) -> None:
            path.append(node)
            children = [m for m in messages if m.get("parent_message_uuid") == node["uuid"]]

            if not children:
                paths.append(path.copy())
            else:
                for child in children:
                    dfs(child, path.copy())

        dfs(start, [])
        return paths

    def format_content(content_list: list[dict]) -> str:
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
                    text = format_content(msg.get("content", []))
                    if text:
                        lines.append(f"**{label}**\n{text}\n")
                lines.append("")

    lines.append("")
    return "\n".join(lines)


def conversation_filename(conv: dict) -> str:
    """Generate a sanitized filename from conversation name."""

    def simplify(s: str) -> str:
        s = re.sub(r"[^\w\-]", "-", s)
        s = re.sub(r"-+", "-", s)
        s = s.lower().strip("-")
        return s[:50] or "conversation"

    name = conv.get("name", "Untitled")
    uuid = conv.get("uuid", "")
    return f"{simplify(name)}-{uuid[:8]}.md"


def render_project(export: ProjectExport) -> str:
    """Render a ProjectExport as a single markdown document."""
    sections: list[str] = []

    sections.append(f"# {export.name}\n")

    if export.description:
        sections.append(f"## Description\n\n{export.description}\n")

    if export.instructions:
        sections.append(f"## Instructions\n\n{export.instructions}\n")

    if export.memory:
        sections.append(f"## Memory\n\n{export.memory}\n")

    if export.controls:
        controls_md = "\n".join(f"- {c}" for c in export.controls)
        sections.append(f"## Controls\n\n{controls_md}\n")

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
