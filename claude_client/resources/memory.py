from __future__ import annotations

from .._transport import BASE_URL, Transport
from ..models import MemoryDict


class MemoryResource:
    """
    Auto-generated project and org memory (read-only — no write endpoint exists).

    Both methods are scoped to the transport's org (`self._t.org_id`). On a multi-org
    account with no pinned org, `org_id` raises `AmbiguousOrgError` — use
    `ClaudeClient.for_project(project_id)` or `.scoped(org_id)` first.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def get(self, project_id: str) -> MemoryDict:
        """Fetch the auto-generated project memory and controls. Scoped to the transport's org."""
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/memory?project_uuid={project_id}"
        )
        return resp.json()

    def get_general(self) -> MemoryDict:
        """
        Fetch the org-level general memory (not project-specific).

        Scoped to the transport's org, and there's no project id here to resolve one
        from — a multi-org caller must pin the org up front (e.g. via `ClaudeClient.scoped`).
        """
        resp = self._t.get(f"{BASE_URL}/organizations/{self._t.org_id}/memory")
        return resp.json()
