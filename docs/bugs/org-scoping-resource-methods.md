# Resource methods silently assume the wrong org on multi-org accounts

Found during self-review of PR #3 (refactor: split `ClaudeClient` into resource
sub-clients). Tracked instead of fixed there — see status below for why.

## The bug

`Transport.org_id` is a cached property that returns `self._org_ids[0]`: whichever
chat-capable org the API happens to list first for the account. It has no relationship
to which org actually owns a given `project_id`.

Four resource methods build their request URL from `self._t.org_id` without knowing
which org the `project_id` they were given actually lives in:

- `DocsResource.list(project_id)` — `claude_client/resources/docs.py`
- `ConversationsResource.list(project_id)` — `claude_client/resources/conversations.py`
- `ProjectsResource.get(project_id)` — `claude_client/resources/projects.py`
- `MemoryResource.get(project_id)` / `MemoryResource.get_general()` — `claude_client/resources/memory.py`

On a multi-org account, calling any of these for a project that lives in anything but
the first-listed org 404s — the request hits `organizations/{wrong_org}/projects/{id}/...`
— even though the project exists and the caller has access to it under its real org.

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

- Add an `org_id: str | None = None` parameter to all four methods, defaulting to
  `self._t.org_id`. Straightforward, but widens the public API surface of four methods
  across three files and needs new tests — a real change, not a review fix.
- Redesign so this class of mistake is structurally impossible (e.g. project handles
  that carry their own org, or resources that can't be constructed without a resolved
  org). This is the better fix but needs actual design work, not a rushed change during
  PR review.

Either belongs in its own PR with its own review, not folded into an unrelated refactor.

## Workaround until fixed

Multi-org callers should resolve the owning org first and use a scoped client:

```python
org_id = client.projects.find_org(project_id)
scoped = client.scoped(org_id)
scoped.docs.list(project_id)
```
