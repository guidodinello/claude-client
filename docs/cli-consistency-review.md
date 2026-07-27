# CLI consistency review

Advisor-reviewed pass over the command groups in `claude_client/cli.py` (2026-07-20), looking for
semantic grouping issues and naming incoherences. Findings below, ranked by how much they'd actually
mislead a user.

**Status (2026-07-26):** All three findings are fixed. #1 and #2 were fixed directly (see
corrected diagnosis under #2). #3 was resolved by a broader API redesign — the client is now
resource-namespaced (`client.projects`, `client.docs`, `client.conversations`, `client.memory`,
`client.orgs`), `download`/`sync` collapsed into one incremental `pull` (`--force` for the old
always-overwrite behavior), and the `projects`/`project` split is gone. See the CLI section of
`README.md` for the current command tree.

## The yardstick (historical — see status above)

`docs` and `conversations` were the coherent pattern to hold everything else to: same verb set
(`list`/`get`/`download`/`sync`), parallel structure, and a clean distinction between `download`
(dump, always overwrite) and `sync` (incremental, skips unchanged files — returns
created/updated/unchanged per file). This shaped the fixes below; the redesign later generalized
it into `pull`/`--force` across every resource, including `project` and `account`.

## Findings

### 1. `project sync` doesn't have sync semantics — FIXED

`export_project_to_dir` now delegates to `sync_from_web` / `sync_conversations_from_web` instead
of the always-overwrite `download_docs` / `export_conversations_to_files`, and returns a
`ProjectSyncResult` with per-file `docs`/`conversations` status dicts that `project sync` prints,
matching `docs sync` / `conversations sync`.

<details>
<summary>Original finding</summary>

`docs sync` → `sync_from_web` and `conversations sync` → `sync_conversations_from_web` both skip
unchanged files. `project sync` → `export_project_to_dir`, which always rewrites everything — no
skip-unchanged behavior at all. Same verb, different contract.

Git history shows this command used to be called `export-dir`, which was accurate; the rename to
`sync` (to match the `docs`/`conversations` naming) is what introduced the mismatch, without also
porting the skip-unchanged behavior.

**Fix options:** either give `export_project_to_dir` real skip-unchanged semantics (bigger, changes
behavior), or rename the CLI command back to something like `project export-dir` / `project download`
(cosmetic, no behavior change, just stops overpromising).

</details>

### 2. `projects list` and `account export` operate at different scopes — FIXED (diagnosis corrected)

**The original diagnosis undersold this.** It isn't a listing-scope mismatch fixable by adding
`account list` — every project-scoped CLI command (`docs *`, `conversations *`, `project
export`/`sync`) built its client with `ClaudeClient(token)`, which defaults `org_id` to the first
chat-capable org (`org_id` `cached_property`, picks `chat_capable_org_ids()[0]`). For a project
living in a different org, those commands hit the wrong org and 404 — regardless of how the user
got the project id. Adding `account list` would have made this worse: it'd surface ids the other
commands then couldn't act on.

**Fix implemented:** `cli._client()` now takes an optional `project_id`. When given one and the
account has more than one chat-capable org, it resolves the owning org via `find_project_org`
(already existed, previously only used by `project migrate`) and returns a client pinned to it via
the now-public `ClaudeClient.scoped_client()`. Single-org accounts pay no extra cost — the
ambiguity doesn't exist for them.

<details>
<summary>Original finding</summary>

`projects list` → `list_projects()` shows projects in **one** org (`self.org_id`, the first
chat-capable one on the account). `account export` → `list_all_projects()` spans **every**
chat-capable org. So there's no command that previews the full set `account export` will act on —
`projects list` shows a subset of it.

Given `find_project_org` and `list_all_projects` exist specifically because accounts can belong to
multiple orgs, this is a real trap for anyone with a multi-org account, not a hypothetical.

**Fix:** add an `account list` action (or an `--all-orgs` flag on `projects list`) so the list scope
matches the export scope.

</details>

### 3. `project` vs `projects` — near-duplicate top-level groups — FIXED (API redesign)

**Fix implemented:** `projects list` folded into `project list`; the `projects` group is gone.
`project list` spans every chat-capable org by default (via `ProjectsResource.list()`), which
resolves finding #2's "no command previews what `account pull` will act on" for free, exactly as
originally proposed.

<details>
<summary>Original finding</summary>

Two top-level command groups one character apart. Also breaks the yardstick pattern: `docs` and
`conversations` each bundle collection-ops (`list`) and item-ops under one plural noun, but projects
splits `projects list` (collection) from `project export/sync/migrate` (item-level).

**Fix:** fold `projects list` into `project list`, drop the `projects` group entirely. If that unified
`list` is also made to span all orgs, this resolves finding #2 for free.

</details>

## Left alone, deliberately

- **`export` still means different things in different groups** (`project export` → single markdown
  file via `ProjectsResource.export()`, `project pull` / `account pull` → directory tree). The
  redesign renamed the directory-tree operation to `pull` specifically to stop it colliding with
  `export`'s single-file meaning — see `README.md`.
- **`account` is named by scope-breadth, everything else by object-noun.** This was a deliberate,
  discussed choice (see PR #1) over folding it into `project pull-all` — not worth reopening.
