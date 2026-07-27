# Known bugs

Tracked issues that are real but out of scope for the PR that found them. Each entry
links to a dedicated file with full detail.

| # | Summary | Detail |
|---|---------|--------|
| 1 | Resource methods that take a single `project_id` (`docs.list`, `conversations.list`, `projects.get`, `memory.get`/`get_general`) assume the project lives in `Transport.org_id` (the first chat-capable org), which breaks direct library callers on multi-org accounts. | [`docs/bugs/org-scoping-resource-methods.md`](bugs/org-scoping-resource-methods.md) |
