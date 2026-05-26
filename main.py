"""Quick usage demo — not part of the library itself."""

from logger import init_logging

from claude_client import ClaudeClient


def main():
    init_logging()
    client = ClaudeClient()  # reads CLAUDE_SESSION_TOKEN from env

    projects = client.list_projects()
    print(f"Found {len(projects)} projects:")
    for p in projects:
        print(f"  {p['uuid']}  {p['name']}")

    if not projects:
        return

    # Demo: export the first project to a markdown file
    project_id = projects[0]["uuid"]
    out = client.export_project_to_file(project_id, f"{projects[0]['name']}.md")
    print(f"Exported to {out}")


if __name__ == "__main__":
    main()
