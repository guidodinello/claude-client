# CLI consistency review

Advisor-reviewed pass over the command groups in `claude_client/cli.py` (2026-07-20), looking for
semantic grouping issues and naming incoherences. Findings below, ranked by how much they'd actually
mislead a user. Nothing here has been acted on yet — this is a punch list for later.

## The yardstick

`docs` and `conversations` are the coherent pattern to hold everything else to: same verb set
(`list`/`get`/`download`/`sync`), parallel structure, and a clean distinction between `download`
(dump, always overwrite) and `sync` (incremental, skips unchanged files — returns
created/updated/unchanged per file).

## Findings

### 1. `project sync` doesn't have sync semantics

`docs sync` → `sync_from_web` and `conversations sync` → `sync_conversations_from_web` both skip
unchanged files. `project sync` → `export_project_to_dir`, which always rewrites everything — no
skip-unchanged behavior at all. Same verb, different contract.

Git history shows this command used to be called `export-dir`, which was accurate; the rename to
`sync` (to match the `docs`/`conversations` naming) is what introduced the mismatch, without also
porting the skip-unchanged behavior.

**Fix options:** either give `export_project_to_dir` real skip-unchanged semantics (bigger, changes
behavior), or rename the CLI command back to something like `project export-dir` / `project download`
(cosmetic, no behavior change, just stops overpromising).

### 2. `projects list` and `account export` operate at different scopes

`projects list` → `list_projects()` shows projects in **one** org (`self.org_id`, the first
chat-capable one on the account). `account export` → `list_all_projects()` spans **every**
chat-capable org. So there's no command that previews the full set `account export` will act on —
`projects list` shows a subset of it.

Given `find_project_org` and `list_all_projects` exist specifically because accounts can belong to
multiple orgs, this is a real trap for anyone with a multi-org account, not a hypothetical.

**Fix:** add an `account list` action (or an `--all-orgs` flag on `projects list`) so the list scope
matches the export scope.

### 3. `project` vs `projects` — near-duplicate top-level groups

Two top-level command groups one character apart. Also breaks the yardstick pattern: `docs` and
`conversations` each bundle collection-ops (`list`) and item-ops under one plural noun, but projects
splits `projects list` (collection) from `project export/sync/migrate` (item-level).

**Fix:** fold `projects list` into `project list`, drop the `projects` group entirely. If that unified
`list` is also made to span all orgs, this resolves finding #2 for free.

## Left alone, deliberately

- **`export` means different things in different groups** (`project export` → single file,
  `account export` → directory tree). Worth a doc-comment/help-string clarification, not a rename —
  the shapes are genuinely different operations that happen to share a verb.
- **`account` is named by scope-breadth, everything else by object-noun.** This was a deliberate,
  discussed choice (see PR #1) over folding it into `project export-all` — not worth reopening.
