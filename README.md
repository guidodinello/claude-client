# claude-client

Python client for the Claude.ai web API — manage projects, sync files, and export project knowledge.

> **Unofficial.** Uses the same endpoints the claude.ai browser app uses. Session tokens expire; see [Authentication](#authentication).

## Features

- List projects and knowledge docs
- Upload, download, and delete docs
- Upsert (upload or replace by name)
- Sync docs from the web to a local folder
- Export a full project to a single Markdown file (title, description, instructions, memory, docs, conversations)
- Sync a full project to a local directory (project.md, docs/, conversations/)
- Export and sync conversations as Markdown files
- Migrate a project's docs, conversations, and memory to another project — even across accounts/orgs
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
claude-client project export <project-id> export.md

# Sync project to a directory (project.md, docs/, conversations/)
claude-client project sync <project-id> ./my-project/

# Migrate a project's docs, conversations, and memory to another project
# (source/dest accept a bare project id or a full claude.ai project URL;
#  source/dest orgs are auto-detected unless overridden)
claude-client project migrate <source> <dest> \
  --source-token sk-ant-sid01-... \
  --dest-token sk-ant-sid01-...

# Skip conversations or memory during migration
claude-client project migrate <source> <dest> --dest-token ... --no-conversations --no-memory

# Conversation operations
claude-client conversations list <project-id>
claude-client conversations get <project-id> <conversation-id>
claude-client conversations download <project-id> ./output/
claude-client conversations sync <project-id> ./local-convos/
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

# Export project to a single markdown file
client.export_project_to_file(project_id, "export.md")

# Sync project to a directory (project.md, docs/, conversations/)
client.export_project_to_dir(project_id, "./my-project/")

# Export all conversations as markdown files
client.export_conversations_to_files(project_id, "./convos/")

# Sync conversations from web (web wins, skips unchanged)
client.sync_conversations_from_web(project_id, "./local-convos/")

# Get a single conversation as markdown
markdown = client.export_conversation_to_file(project_id, conv_id, "conv.md")

# Migrate a project's docs/conversations/memory to another project, possibly
# across accounts and orgs (two separate clients, one per account)
from claude_client import migrate_project

source = ClaudeClient(source_token, org_id=source_org_id)
dest = ClaudeClient(dest_token, org_id=dest_org_id)
migrate_project(source, source_project_id, dest, dest_project_id)
```

## Development

```bash
uv run ruff check --fix .   # lint
uv run pytest tests/ -v     # tests
```
