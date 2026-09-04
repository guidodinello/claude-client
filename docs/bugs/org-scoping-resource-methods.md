# Resource methods silently assumed the wrong org on multi-org accounts

**Status: Fixed.** Found during self-review of PR #3 (refactor: split `ClaudeClient`
into resource sub-clients). Tracked instead of fixed there — see "Why not fixed in
PR #3" below for why fixing it needed its own PR.

## The bug

`Transport.org_id` was a cached property that returned `self._org_ids[0]`: whichever
chat-capable org the API happens to list first for the account. It had no relationship
to which org actually owns a given `project_id`.

This doc originally listed 4 affected methods; a full audit found the real count is
**12 direct `self._t.org_id` URL sites across 4 files** — every resource method that
doesn't take an org explicitly:

- `claude_client/resources/docs.py`: `list`, `get`, `rm`, `_create` (and, indirectly,
  every composite built on them — `rm_all`, `push_content`, `push`, `push_many`, `pull`)
- `claude_client/resources/conversations.py`: `_list_page`, `get` (and, indirectly,
  `list`, `pull`)
- `claude_client/resources/memory.py`: `get`, `get_general`
- `claude_client/resources/projects.py`: `get`, `update` (and, indirectly,
  `export`/`export_data`/`pull`)

On a multi-org account, calling any of these for a project that lives in anything but
the first-listed org 404d — the request hit `organizations/{wrong_org}/projects/{id}/...`
— even though the project existed and the caller had access to it under its real org.

## Why the CLI doesn't hit this

`cli._client(args, project_id)` pre-resolves the owning org via
`ProjectsResource.find_org()` and returns `client.scoped(org_id)` — a `Transport` pinned
to the correct org — before any project-scoped call is made. `ProjectsResource.find()`
and `ProjectsResource.pull_all()` also avoid it, since both iterate every chat-capable
org up front via `self.list()`.

## Who hits it

A direct library caller — `ClaudeClient(token)` straight into
`client.docs.list(project_id)`, with no CLI-equivalent pre-resolution step — on a
multi-org account, for a project not in the first-listed org. Single-org accounts have
no ambiguity to hit. Nothing in the docstrings of the four affected methods currently
warns about this precondition.

## Why not fixed in PR #3

Two shapes were considered:

- Add an `org_id: str | None = None` parameter to all four (really twelve) methods,
  defaulting to `self._t.org_id`. Rejected even as the eventual fix: the default stays
  wrong, so the caller who doesn't know to pass an org — precisely the caller who hits
  this bug — still hits it. It also widens the public API surface across four files
  and duplicates `client.scoped(org_id)`, which already existed.
- Redesign so this class of mistake is structurally impossible. This is the fix that
  shipped — see below.

Either belonged in its own PR with its own review, not folded into an unrelated refactor.

## The fix: pin-or-fail

`Transport.org_id` now raises `AmbiguousOrgError` when the account has more than one
chat-capable org and no org has been explicitly pinned, instead of silently returning
`_org_ids[0]`. An explicit pin (`ClaudeClient(token, org_id=...)`, or `.scoped(org_id)`)
still wins outright and is unaffected. Single-org accounts see no behavior change at
all — there's no ambiguity to raise on.

An auto-resolve-from-`project_id` design was considered and rejected:
`ConversationsResource.get(conversation_id)` takes no `project_id` at all, so that
approach would leave at least one method with no way to auto-resolve, making the fix
incomplete by construction.

`ClaudeClient.for_project(project_id)` (`claude_client/client.py`) is the one-call fix —
it resolves the owning org via `projects.find_org()` and returns a client scoped to it:

```python
scoped = client.for_project(project_id)
scoped.docs.list(project_id)
```

`ClaudeClient.scoped(org_id)` remains available when the org id is already known.

An explicitly pinned org survives `update_token()` — a caller who said "this client is
org X" meant it regardless of which token is in use.
