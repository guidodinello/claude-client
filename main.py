"""Quick usage demo — not part of the library itself."""

from pathlib import Path

from logger import init_logging

from claude_client import ClaudeClient


def main():
    init_logging()
    client = ClaudeClient()  # reads CLAUDE_SESSION_TOKEN from env

    projects = client.projects.list()
    print(f"Found {len(projects)} projects:")
    for org_id, p in projects:
        print(f"  {org_id}  {p['uuid']}  {p['name']}")

    if not projects:
        return

    # Demo: export the first project to a markdown file
    _, first = projects[0]
    markdown = client.projects.export(first["uuid"])
    out = Path(f"{first['name']}.md")
    out.write_text(markdown, encoding="utf-8")
    print(f"Exported to {out}")


if __name__ == "__main__":
    main()
