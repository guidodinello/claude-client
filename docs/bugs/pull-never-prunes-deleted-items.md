# `pull`/`pull_all` never remove local files deleted on the web

Found while reviewing the `claude-web-backup` nightly pipeline (2026-08-17), right
after the backup dir became its own git repo (`git add -A` + `--allow-empty` commit
per run). Tracked instead of fixed — see status below for why.

## The issue

`pull` is purely additive: it lists what exists on the web, fetches it, and writes
local files. Anything that no longer exists on the web is never removed locally, so
deletions accumulate as orphans forever:

- `DocsResource.pull` — deletes nothing; a doc removed on the web keeps its stale
  local file (`claude_client/resources/docs.py`). Renamed docs leave the old path
  behind too (the docstring says so: "prior local paths are not removed").
- `ConversationsResource.pull` — same; a deleted conversation keeps its stale local
  file (`claude_client/resources/conversations.py`).
- `ProjectsResource.pull_all` — iterates only the current remote project list; a
  deleted project's directory is never removed (`claude_client/resources/projects.py`).

## Who hits it

Anyone using `pull`/`pull_all` as a long-lived mirror — i.e. every scheduled backup
(`claude-web-backup` nightly). Also anyone who deletes or renames projects on
claude.ai and expects their local mirror to reflect it.

## Fix options, best first

**A. Prune per resource from the remote list.** After the incremental pull of a
resource, delete local files whose identity is absent from the remote list, then
report them in the results dict (e.g. "deleted"). Feasibility differs per resource:

- *Conversations:* reliable — local filenames end in `<uuid>.md`
  (`conversation_filename`), so prune = diff local uuid suffixes vs remote uuids.
- *Projects:* reliable at the directory level — `pull_all` writes
  `{out}/{slug}/`, so prune = remove directories whose slug isn't in the remote
  project list.
- *Docs:* unreliable by filename — `_safe_md_filename` only embeds the uuid on
  name collisions, so a removed doc's uuid can't be recovered from its path.
  Needs a small sidecar manifest (uuid → filename) in `docs/`, or changing the
  local filename scheme to always suffix the uuid (one-time rename churn for
  existing mirrors).

**B. Prune from a manifest instead of the remote list.** Reuse the pull manifest
proposed in `pull-re-fetches-unchanged-content.md` (option A there): anything in
the manifest that's absent from the remote list is deleted. Same safety property,
one mechanism for both incremental-skip and pruning. Docs still need the manifest
either way.

**Safety notes (the part that used to block this):** pruning is only safe when the
list call succeeded — it has, since pull iterates it — and when deletions are
recoverable. The claude-web-backup mirror is now git-versioned (one commit per
nightly run), so pruned files stay in history and a bad prune is a `git checkout`
away. A defensive option: prune only in "mirror" mode (explicit flag, e.g.
`pull(..., prune=True)`), default off, so ad-hoc pulls never surprise.

## Status

Tracked, not yet fixed. The claude-web-backup git repo makes option A safe to
implement; worth coordinating with the `pull-re-fetches-unchanged-content.md`
manifest work if both get done, since B shares the manifest.