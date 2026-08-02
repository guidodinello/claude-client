# `pull`/`pull_all` re-fetches unchanged docs and conversations every run

Found while reviewing the `claude-web-backup` nightly pipeline (2026-08-02), which
calls `ProjectsResource.pull_all()` once per account per night. Tracked instead of
fixed — see status below for why.

## The issue

`pull` is incremental on disk, not on the network: every run re-fetches the full
content of every doc and every conversation, and only skips the *write* when the
rendered markdown already matches the local file:

- `DocsResource.pull` lists all docs, then `get()`s each one
  (`claude_client/resources/docs.py`).
- `ConversationsResource.pull` lists all conversations (paginated, 30/page), then
  `get()`s each one with full message content
  (`claude_client/resources/conversations.py`).
- Fetches are sequential (the known "avoid n+1 on conversations requests" todo), so
  wall time is roughly `conversation_count × per-request latency`.
- `project.md` is rewritten unconditionally every run (`claude_client/resources/projects.py`).

There is no etag/`If-Modified-Since`/mtime/hash caching anywhere in the transport
layer, so the network cost of a nightly rerun is identical to a first run even when
nothing changed on the web.

Both list endpoints already return `updated_at` per item
(`claude_client/models.py`: `DocDict`, `ConversationDict`), which is what makes the
manifest fix below feasible without any API changes.

## Who hits it

Anyone running `project pull` / `account pull` / `pull_all` on the same projects
more than once — i.e. every scheduled backup. Pure inefficiency; nothing is
incorrect.

## Fix options, best first

**A. Pull manifest (skip unchanged fetches).** Store a sidecar file (e.g.
`.manifest.json` beside `docs/`/`conversations/`) mapping filename → the remote
`updated_at` it was pulled at. Next run, compare each item's `updated_at` from
`list()` against the manifest and skip the `get()` entirely for matches — only
changed items are fetched. Rules: always fetch when the local file is missing
(user deleted it), and re-pull everything if the manifest is absent/corrupt. Note
the one semantic change: today a local edit gets overwritten by the web version
even if the web didn't change; with a manifest skip it would stay untouched
("web is source of truth" becomes "web is source of truth when it changed").

**B. Parallelize the fetches.** Same requests, less wall time — the n+1 is
latency-bound (100 conversations × ~300 ms ≈ 30 s sequential vs ~2-3 s with a
`ThreadPoolExecutor`/async over the `get()` calls). Orthogonal to A; combine for
near-instant reruns.

**C. Real HTTP conditional requests (etag / `If-Modified-Since`).** Send the
previous response's validator; server answers `304` with no body. Saves bandwidth,
not latency (still one round-trip per conversation), and only helps if Claude.ai's
API actually emits validators — unverified, likely not. A is strictly better here;
a client-side content hash is not useful on its own because fetching the content
to hash it is the expensive part.

## Status

Tracked, not yet fixed. A or B would be their own small PRs; the semantic change in
A deserves a deliberate decision before implementing.
