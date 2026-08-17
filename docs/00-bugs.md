# Known bugs

Tracked issues that are real but out of scope for the PR that found them. Each entry
links to a dedicated file with full detail.

| # | Summary | Detail |
|---|---------|--------|
| 1 | Resource methods that take a single `project_id` (`docs.list`, `conversations.list`, `projects.get`, `memory.get`/`get_general`) assume the project lives in `Transport.org_id` (the first chat-capable org), which breaks direct library callers on multi-org accounts. | [`docs/bugs/org-scoping-resource-methods.md`](bugs/org-scoping-resource-methods.md) |
| 2 | `pull`/`pull_all` progress bars accumulate per project+stage, reset for every project, and never say which project is being pulled — noisy but cosmetic. | [`docs/bugs/pull-progress-bars-accumulate.md`](bugs/pull-progress-bars-accumulate.md) |
| 3 | `pull`/`pull_all` re-fetches the full content of every doc and conversation on every run — incremental on disk only, so nightly reruns cost as much as first runs. | [`docs/bugs/pull-re-fetches-unchanged-content.md`](bugs/pull-re-fetches-unchanged-content.md) |
