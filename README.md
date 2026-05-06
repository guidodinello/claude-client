# claude-client

Python client for the Claude.ai web API — manage projects, sync files, and export project knowledge.

> **Unofficial.** Uses the same endpoints the claude.ai browser app uses. Session tokens expire; see [Authentication](#authentication).

## Features

- List projects and knowledge docs
- Upload, download, and delete docs
- Upsert (upload or replace by name)
- Sync docs from the web to a local folder
- Export a full project to a single Markdown file (title, description, instructions, memory, docs)
- CLI for all operations

## Installation

```bash
uv pip install -e .
```

Requires Python 3.13+ and [`curl_cffi`](https://github.com/yifeikong/curl_cffi) (installed automatically).

## Authentication

Get your session token from claude.ai:

1. Open claude.ai in a browser, log in.
2. Open DevTools → Application → Cookies → `sessionKey`.
3. Copy the value (starts with `sk-ant-sid01-...`).

Set it as an env var:

```bash
export CLAUDE_SESSION_TOKEN=sk-ant-sid01-...
```

Or pass it directly via `--token` (CLI) or the `session_token` argument (Python).

## CLI usage

```bash
# List all projects
claude-client projects list

# List docs in a project
claude-client docs list <project-id>

# Upload a file
claude-client docs upload <project-id> path/to/file.md

# Download all docs to a local folder
claude-client docs download <project-id> ./output/

# Sync web → local (web wins, skips unchanged files)
claude-client docs sync <project-id> ./local-docs/

# Export full project to a single markdown file
claude-client export <project-id> export.md
```

## Python usage

```python
from claude_client import ClaudeClient

client = ClaudeClient()  # reads CLAUDE_SESSION_TOKEN from env

# List projects
projects = client.list_projects()

# Upload a file
client.upload_file(project_id, "notes.md")

# Upsert (replace if exists, upload if not)
client.upsert_file(project_id, "notes.md")

# Sync multiple files
client.sync_files(project_id, ["a.md", "b.md"], name_prefix="MyProject__")

# Export project to markdown
client.export_project_to_file(project_id, "export.md")
```

## Development

```bash
uv run ruff check --fix .   # lint
uv run pytest tests/ -v     # tests
```
