"""
Client for the Claude.ai unofficial web API.

Resource-namespaced, matching the shape of the official Anthropic SDK
(`client.messages.create`, ...):

    client.orgs             list orgs, chat-capable org ids
    client.projects         list/get/find/update, plus composite export/pull/pull_all
    client.docs             list/get/rm/push/pull within a project
    client.conversations    list/get/pull within a project
    client.memory           read-only project + org memory

Each resource holds a reference to the shared `Transport`, which owns the session
token, headers, raw HTTP verbs, and org resolution. See `_transport.py`.
"""

from ._transport import Transport
from .resources import ConversationsResource, DocsResource, MemoryResource, OrgsResource
from .resources.projects import ProjectsResource


class ClaudeClient:
    """Client for the Claude.ai unofficial web API."""

    def __init__(self, session_token: str | None = None, *, org_id: str | None = None) -> None:
        self._transport = Transport(session_token, org_id=org_id)
        self.orgs = OrgsResource(self._transport)
        self.docs = DocsResource(self._transport)
        self.conversations = ConversationsResource(self._transport)
        self.memory = MemoryResource(self._transport)
        self.projects = ProjectsResource(
            self._transport, docs=self.docs, conversations=self.conversations, memory=self.memory
        )

    @property
    def org_id(self) -> str:
        return self._transport.org_id

    def update_token(self, session_token: str) -> None:
        self._transport.update_token(session_token)

    def scoped(self, org_id: str) -> "ClaudeClient":
        """A client sharing this account's token but pinned to a specific org."""
        return ClaudeClient(self._transport.session_token, org_id=org_id)
