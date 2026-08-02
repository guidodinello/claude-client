# claude-client

Python client for the Claude.ai web API — manage projects, sync files, and export project knowledge.

> **Unofficial.** Uses the same endpoints the claude.ai browser app uses. Session tokens expire; see [Authentication](#authentication).

## Features

- List, show, and update projects — across every org on the account by default
- List, get, push (upsert), remove, and pull knowledge docs
- List, get, and pull conversations as Markdown files
- Export a full project to a single Markdown file (title, description, instructions, memory, docs, conversations)
- Pull a full project to a local directory (project.md, docs/, conversations/) — incremental by default, `--force` to always rewrite
- Migrate a project's docs, conversations, and memory to another project — even across accounts/orgs
- Resource-namespaced Python client (`client.projects`, `client.docs`, `client.conversations`, `client.memory`, `client.orgs`) and matching CLI

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

Every group follows the same verb set: `list` / `get` (or `show`) and `pull` — one incremental
verb per resource, always skipping unchanged files unless you pass `--force`.

```bash
# List projects across every chat-capable org on the account
claude-client project list

# Show a project's metadata
claude-client project show <project-id>

# List / get / push / remove / pull knowledge docs
claude-client docs list <project-id>
claude-client docs get <project-id> <doc-id>
claude-client docs push <project-id> path/to/file.md      # upserts by name
claude-client docs rm <project-id> <doc-id>
claude-client docs pull <project-id> ./local-docs/         # incremental; add --force to always rewrite

# Export full project to a single markdown file
claude-client project export <project-id> export.md

# Pull a project to a directory (project.md, docs/, conversations/), incrementally
claude-client project pull <project-id> ./my-project/
claude-client project pull <project-id> ./my-project/ --force

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
claude-client conversations pull <project-id> ./local-convos/

# Pull every project across every chat-capable org to a directory
claude-client account pull ./all-projects/
```

## Python usage

The client is namespaced by resource, matching the CLI groups above:

```python
from claude_client import ClaudeClient

client = ClaudeClient()  # reads CLAUDE_SESSION_TOKEN from env

# Projects — list() spans every chat-capable org by default, returning (org_id, project) pairs
projects = client.projects.list()
project = client.projects.get(project_id)
client.projects.update(project_id, description="New description")

# Docs — push always upserts (replaces any existing doc with the same name)
client.docs.push(project_id, "notes.md")
client.docs.push_content(project_id, "raw text", "notes.md")
client.docs.push_many(project_id, ["a.md", "b.md"], name_prefix="MyProject__")
client.docs.rm(project_id, doc_id)
client.docs.rm_all(project_id)

# Pull docs incrementally (skips files whose content already matches); pass force=True to
# always rewrite. Remote names are converted to safer local .md filenames, with UUID
# suffixes for case-insensitive collisions. Renamed destinations do not remove old local
# paths or retain a reversible remote-name mapping for later pushes.
client.docs.pull(project_id, "./local-docs/")

# Conversations
convs = client.conversations.list(project_id)
conv = client.conversations.get(conversation_id)
client.conversations.pull(project_id, "./local-convos/")

# Project composite operations
markdown = client.projects.export(project_id)  # single markdown string
client.projects.pull(project_id, "./my-project/")  # project.md, docs/, conversations/
client.projects.pull_all("./all-projects/")  # every project, every org

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
