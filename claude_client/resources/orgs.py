from __future__ import annotations

from .._transport import Transport
from ..models import OrgDict


class OrgsResource:
    """Organizations on the authenticated account."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> list[OrgDict]:
        return self._t.list_organizations()

    def chat_capable_ids(self) -> list[str]:
        """Every org uuid with 'chat' or 'claude_pro' capabilities."""
        return self._t.chat_capable_org_ids()
