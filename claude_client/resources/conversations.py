from __future__ import annotations

from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import track

from .._transport import BASE_URL, Transport
from ..models import ConversationDetailDict, ConversationDict, Page
from ..render import conversation_filename, conversation_to_markdown

logger = get_logger(__name__)

_PAGE_LIMIT = 30


class ConversationsResource:
    """Conversations within a project."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def _list_page(self, project_id: str, *, limit: int, offset: int) -> Page[ConversationDict]:
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}/conversations_v2"
            f"?limit={limit}&offset={offset}"
        )
        raw = resp.json()
        return Page(data=raw["data"], pagination=raw["pagination"])

    def list(self, project_id: str) -> list[ConversationDict]:
        """Fetch every conversation in a project, handling pagination internally."""
        results: list[ConversationDict] = []
        offset = 0
        while True:
            page = self._list_page(project_id, limit=_PAGE_LIMIT, offset=offset)
            results.extend(page.data)
            if not page.pagination["has_more"]:
                break
            offset += _PAGE_LIMIT
        return results

    def get(self, conversation_id: str) -> ConversationDetailDict:
        """
        Fetch a single conversation with full message content.

        Conversation ids are unique within an org, so this doesn't need a project id —
        unlike `list`, which lists within one project's scope.
        """
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/chat_conversations/{conversation_id}"
            f"?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual"
        )
        return resp.json()

    def pull(
        self, project_id: str, output_dir: str | Path, *, force: bool = False
    ) -> dict[str, str]:
        """
        Pull conversations from the web project into a local directory as markdown files.

        Incremental by default: a conversation whose rendered markdown already matches
        the local file is left untouched and reported "unchanged". Pass force=True to
        always rewrite every file. Web is always the source of truth.
        Returns a dict mapping each filename to "created", "updated", or "unchanged".
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        results: dict[str, str] = {}
        for conv_meta in track(self.list(project_id), description="Pulling conversations…"):
            try:
                conv = self.get(conv_meta["uuid"])
            except requests.exceptions.RequestException:
                logger.warning(
                    "Failed to fetch conversation %s, skipping", conv_meta.get("uuid", "unknown")
                )
                continue

            content = conversation_to_markdown(conv)
            filename = conversation_filename(conv)
            dest = out / filename
            existed = dest.exists()
            if not force and existed and dest.read_text(encoding="utf-8") == content:
                results[filename] = "unchanged"
                continue
            dest.write_text(content, encoding="utf-8")
            results[filename] = "updated" if existed else "created"
        return results
