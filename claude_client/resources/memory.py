from __future__ import annotations

from .._transport import BASE_URL, Transport
from ..models import MemoryDict


class MemoryResource:
    """Auto-generated project and org memory (read-only — no write endpoint exists)."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def get(self, project_id: str) -> MemoryDict:
        """Fetch the auto-generated project memory and controls."""
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/memory?project_uuid={project_id}"
        )
        return resp.json()

    def get_general(self) -> MemoryDict:
        """Fetch the org-level general memory (not project-specific)."""
        resp = self._t.get(f"{BASE_URL}/organizations/{self._t.org_id}/memory")
        return resp.json()
