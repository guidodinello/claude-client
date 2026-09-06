# Progress bars from `pull`/`pull_all` accumulate and don't identify the project

Found during a manual run of the `claude-web-backup` nightly pipeline (2026-08-02),
which calls `ProjectsResource.pull_all()` once per account. Tracked instead of
fixed — see status below for why.

## The issue

In a terminal, a `pull` over several projects looks like a pile of leftover bars:

```
Pulling docs… ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pulling conversations… ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
Pulling docs… ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
Pulling conversations… ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
…
Pulling docs… ━━━━━━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━  30% 0:00:03
```

- Every stage opens its own progress bar: `DocsResource.pull` and
  `ConversationsResource.pull` each call `rich.progress.track()` fresh
  (`claude_client/resources/docs.py`, `claude_client/resources/conversations.py`).
- `ProjectsResource.pull` runs them sequentially per project
  (`claude_client/resources/projects.py`), so the bar resets and re-prints once per
  project per stage — finished bars are left on screen because `track()` isn't
  `transient`.
- No bar says *which* project is being pulled; only the resource name.
- Instant (already-pulled, empty, or no-op) stages render as a full-width bar with no
  percentage, which reads as "done" but is actually the bar having nothing to advance.

## Who hits it

Anyone running `project pull`, `account pull`, or `projects.pull_all()` in a terminal
over more than one project. Cosmetic — nothing is wrong with the data written.

## Fix options

- One shared `rich.progress.Progress` at the `pull`/`pull_all` level with a task per
  project (or per project+stage), and `transient=True` so finished tasks sweep off
  screen instead of accumulating.
- Include the project name in the task description (e.g. "Pulling my-project docs…").
- Consider not rendering bars at all when stdout isn't a TTY (systemd/cron runs).

## Fix shipped

`ProjectsResource.pull`/`pull_all` now open a single `rich.progress.Progress`
(`transient=True`) and pass it down to `DocsResource._pull_worker` and
`ConversationsResource._pull_into`, which add a task to the shared instance
instead of each starting their own with `track()`. Each task's description is
labeled with the project name (`f"{label} — docs"` / `f"{label} — conversations"`),
so the display reads as one set of live-updating bars — one per project+stage —
that sweep off screen on completion instead of piling up, and each one says
which project it belongs to.

Not shipped: suppressing bars entirely on a non-TTY (systemd/cron runs) — left
for a separate PR if it's ever needed.

## Status

Fixed.
