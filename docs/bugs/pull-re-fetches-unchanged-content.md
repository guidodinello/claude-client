# `pull`/`pull_all` re-fetches unchanged docs and conversations every run

Found while reviewing the `claude-web-backup` nightly pipeline (2026-08-02), which
calls `ProjectsResource.pull_all()` once per account per night.

## The issue

`pull` is incremental on disk, not on the network: every run re-fetches the full
content of every doc and every conversation, and only skips the *write* when the
rendered markdown already matches the local file:

- `DocsResource.pull` lists all docs, then `get()`s each one — even though, per a live
  check against the API, the list response already carries full `content` for every
  doc, making the per-doc `get()` entirely redundant (`claude_client/resources/docs.py`).
- `ConversationsResource.pull` lists all conversations (paginated, 30/page), then
  `get()`s each one with full message content
  (`claude_client/resources/conversations.py`).
- Fetches are sequential (the known "avoid n+1 on conversations requests" todo), so
  wall time is roughly `conversation_count × per-request latency`.
- `project.md` is rewritten unconditionally every run (`claude_client/resources/projects.py`).

There is no etag/`If-Modified-Since`/mtime/hash caching anywhere in the transport
layer, so the network cost of a nightly rerun is identical to a first run even when
nothing changed on the web.

`ConversationDict` carries `updated_at` per item, which is what makes the manifest
fix below feasible for conversations without any API changes. `DocDict` does not —
verified live against the docs list endpoint, which returns
`['content', 'created_at', 'estimated_token_count', 'file_name', 'project_uuid', 'uuid']`
and nothing resembling a last-modified timestamp. But that same live check also
showed the list response already contains full `content` (byte-identical to a
separate `get()`, confirmed on docs up to 62KB) — so docs don't need a
change-discriminator at all; the fix is deleting the redundant fetch, not skipping it.

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

**Fixed** (2026-08-19), by two different means depending on what the API actually exposes:

**Conversations — option A, the manifest.** A sidecar manifest
(`.claude-pull-manifest.json`, `claude_client/_manifest.py`) keyed by remote uuid, storing the
filename written and the `updated_at` it was pulled at. `ConversationsResource.pull` skips the
`get()` when the manifest entry's `updated_at` matches the remote list's and the local file
still exists; `force=True` bypasses the skip (the documented local-edit recovery path), so the
semantic change flagged above was accepted deliberately.

**Docs — not option A.** The manifest premise (`updated_at` on the list response) doesn't hold
for docs, but the live check that ruled it out also found the actual fix: the docs list
response already contains full `content`, so `DocsResource.pull` simply stopped calling
`get()` per doc — there's no separate fetch left to skip. `DocDict` was corrected to match the
verified response shape (added `project_uuid`, `estimated_token_count`; still no `updated_at`).
The manifest is still used for docs, purely to support pruning (see
`pull-never-prunes-deleted-items.md`), which needs the uuid → filename map and nothing else.

`ProjectsResource.export_data` (and `migrate_project`, which calls it) still does the old
per-doc `get()` — same redundant fetch, not fixed here since it's export/migrate rather than
pull; tracked in `todos.md`.

Option B (parallelize fetches) is still open for conversations — orthogonal, tracked in
`todos.md`.
