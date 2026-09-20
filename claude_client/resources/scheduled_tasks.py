from __future__ import annotations

from .._transport import BASE_URL, Transport


class ScheduledTasksResource:
    """
    Claude Projects/Cowork scheduled tasks (the "Programadas" tab — not Claude Code
    Routines, a separate product with its own `/claude_code/routines/{id}/fire` API).

    Scoped to the transport's org (`self._t.org_id`), not to a project — a task id is
    unique account-wide. On a multi-org account with no pinned org, `org_id` raises
    `AmbiguousOrgError` — use `ClaudeClient.scoped(org_id)` first.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def run(self, task_id: str) -> dict:
        """
        Run a scheduled task immediately — the "Ejecutar ahora" button.

        `task_id` is the trig_... id from the task's URL
        (claude.ai/scheduled-task/trig_...). Scoped to the transport's org.
        """
        resp = self._t.post(
            f"{BASE_URL}/organizations/{self._t.org_id}/cowork/scheduled_tasks/{task_id}/run",
            {},
        )
        return resp.json()
