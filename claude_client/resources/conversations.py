from __future__ import annotations

from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import track

from .. import _manifest
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
        self,
        project_id: str,
        output_dir: str | Path,
        *,
        force: bool = False,
        prune: bool = False,
    ) -> dict[str, str]:
        """
        Pull conversations from the web project into a local directory as markdown files.

        Incremental over the network via a sidecar manifest keyed by conversation uuid: a
        conversation whose remote `updated_at` matches the manifest and whose local file
        still exists is never re-fetched, and is reported "unchanged". Pass force=True to
        bypass the manifest and always re-fetch and rewrite every file (e.g. to recover
        from local edits) — web is the source of truth whenever it changed.

        Pass prune=True to delete local files for conversations removed on the web
        (reported "deleted"); default is off so ad-hoc pulls never delete anything.
        Returns a dict mapping each filename to "created", "updated", "unchanged", or "deleted".
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        previous = _manifest.load(out)
        conv_metas = self.list(project_id)

        results: dict[str, str] = {}
        # uuids confirmed present this run — see docs.py::pull for why this is kept
        # separate from what gets saved (stale, remote-absent entries must survive a
        # non-prune run so a later --prune can still find them).
        entries: dict[str, _manifest.ManifestEntry] = {}
        for conv_meta in track(conv_metas, description="Pulling conversations…"):
            uuid = conv_meta["uuid"]
            prior = previous.get(uuid)
            remote_updated_at = conv_meta.get("updated_at", "")
            if (
                not force
                and prior is not None
                and prior.updated_at
                and remote_updated_at
                and prior.updated_at == remote_updated_at
                and (out / prior.filename).exists()
            ):
                results[prior.filename] = "unchanged"
                entries[uuid] = prior
                continue

            try:
                conv = self.get(uuid)
            except requests.exceptions.RequestException:
                logger.warning("Failed to fetch conversation %s, skipping", uuid)
                if prior is not None:
                    entries[uuid] = prior
                continue

            content = conversation_to_markdown(conv)
            filename = conversation_filename(conv)
            dest = out / filename
            existed = dest.exists()
            if not force and existed and dest.read_text(encoding="utf-8") == content:
                results[filename] = "unchanged"
            else:
                dest.write_text(content, encoding="utf-8")
                results[filename] = "updated" if existed else "created"
            entries[uuid] = _manifest.ManifestEntry(filename=filename, updated_at=remote_updated_at)

        to_save = {**previous, **entries}
        if prune:
            for uuid, filename in _manifest.prune_targets(previous, entries):
                (out / filename).unlink(missing_ok=True)
                results[filename] = "deleted"
                to_save.pop(uuid, None)

        _manifest.save(out, to_save)
        return results
