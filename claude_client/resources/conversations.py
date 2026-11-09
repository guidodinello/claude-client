from __future__ import annotations

import mimetypes
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import Progress

from .. import _manifest
from .._transport import BASE_URL, Transport
from ..exceptions import AuthError
from ..models import ChatFileDict, ConversationDetailDict, ConversationDict, Page
from ..render import conversation_filename, conversation_to_markdown, message_images, slugify

logger = get_logger(__name__)

_PAGE_LIMIT = 30
_STANDALONE_PAGE_LIMIT = 200  # this endpoint tolerates far larger pages than conversations_v2
# Conservative: this endpoint isn't documented as rate-limited, but the impersonated-browser
# fingerprint in _transport.py is itself evidence claude.ai is bot-detection sensitive.
_MAX_CONVERSATION_FETCH_WORKERS = 5

# A file's preview_url is site-relative ("/api/<org>/files/<uuid>/preview"), so it's joined
# to the origin, not BASE_URL (which already ends in /api).
_CLAUDE_ORIGIN = "https://claude.ai"
# Images live under {out}/attachments/{conversation_uuid}/ — keyed by the conversation's
# uuid rather than its filename slug, so a renamed conversation keeps its images (and its
# links stay valid) and the directory is owned by exactly one conversation for pruning.
ATTACHMENTS_DIR = "attachments"
_UNKNOWN_IMAGE_SUFFIX = ".bin"


def _attachment_stem(conv_uuid: str, image: ChatFileDict) -> str:
    """
    Relative path (to the pulled dir) of a saved image, minus its extension.

    Uses the FULL file_uuid: file uuids are UUIDv7 (time-ordered), so a prefix is a
    timestamp bucket, not an id — two images uploaded close together share it, and with
    the same name would collide and the second be skipped as "already on disk".
    """
    name = slugify(Path(image.get("file_name", "")).stem, fallback="image")
    return f"{ATTACHMENTS_DIR}/{conv_uuid}/{image['file_uuid']}-{name}"


def _existing_attachment(out: Path, stem: str) -> str | None:
    """The already-saved file for `stem`, whatever extension it was saved with."""
    stem_path = out / stem
    # The extension comes from the download's content-type, so it isn't known up front.
    # In-progress downloads are dot-prefixed (see `_download_attachment`), so never match.
    match = next(stem_path.parent.glob(f"{stem_path.name}.*"), None)
    return f"{Path(stem).parent.as_posix()}/{match.name}" if match else None


@dataclass(slots=True)
class _FetchedConversation:
    """A conversation plus the outcome of backing up its images (see `_fetch`)."""

    conv: ConversationDetailDict
    saved: dict[str, str] = field(default_factory=dict)  # file_uuid -> relpath on disk
    downloaded: list[str] = field(default_factory=list)  # relpaths written this run
    failed: dict[str, Exception] = field(default_factory=dict)  # stem -> error


class ConversationsResource:
    """
    Conversations within a project, plus account-wide standalone (non-project) chats.

    `list`, `get`, and `pull` are scoped to the transport's org (`self._t.org_id`). On a
    multi-org account with no pinned org, `org_id` raises `AmbiguousOrgError` — use
    `ClaudeClient.for_project(project_id)` or `.scoped(org_id)` first. `pull_standalone` is
    the exception: like `ProjectsResource.pull_all`, it fans out over every chat-capable org
    itself via `.scoped()`, so it's unaffected by org pinning and should be called unscoped.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def _list_page(self, project_id: str, *, limit: int, offset: int) -> Page[ConversationDict]:
        """Fetch one page of conversations. Scoped to the transport's org."""
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
        unlike `list`, which lists within one project's scope. Still scoped to the
        transport's org, though: there's no project id here to resolve it from, so a
        multi-org caller must pin the org up front (e.g. via `ClaudeClient.scoped`).
        """
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/chat_conversations/{conversation_id}"
            f"?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual"
        )
        return resp.json()

    def _download_attachment(self, image: ChatFileDict, out: Path, stem: str) -> str:
        """
        Download an image's preview and save it as `stem` + an extension from the
        response's content-type. Returns the saved relative path.

        Only the compressed preview is fetched (a webp, verified live): no endpoint for
        the original upload is known. Written via a temp file then renamed, so "file
        exists" reliably means "fully downloaded" for the incremental check.
        """
        # `message_images` only yields images that have a preview_url.
        resp = self._t.get(_CLAUDE_ORIGIN + image.get("preview_url", ""))
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        relpath = stem + (mimetypes.guess_extension(content_type) or _UNKNOWN_IMAGE_SUFFIX)
        dest = out / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return relpath

    def _fetch(self, conversation_id: str, out: Path) -> _FetchedConversation:
        """
        `get()` a conversation, then save each of its images not already on disk.

        Runs in a pool worker. A failed image download is recorded in `failed` rather
        than raised, so one bad image never costs the whole conversation; only the
        conversation fetch itself raising propagates.
        """
        fetched = _FetchedConversation(self.get(conversation_id))
        for msg in fetched.conv.get("chat_messages", []):
            for image in message_images(msg):
                file_uuid = image["file_uuid"]
                if file_uuid in fetched.saved:
                    continue
                stem = _attachment_stem(conversation_id, image)
                existing = _existing_attachment(out, stem)
                if existing:
                    fetched.saved[file_uuid] = existing
                    continue
                try:
                    relpath = self._download_attachment(image, out, stem)
                # AuthError too: the transport raises it for any 403, and a 403 here is a
                # per-file refusal — `self.get()` just succeeded with the same token. A
                # genuinely expired token still aborts on the next conversation fetch.
                except (requests.exceptions.RequestException, AuthError) as exc:
                    fetched.failed[stem] = exc
                    continue
                fetched.saved[file_uuid] = relpath
                fetched.downloaded.append(relpath)
        return fetched

    def _pull_conversations(
        self,
        conv_metas: list[ConversationDict],
        out: Path,
        *,
        force: bool,
        progress: Progress,
        label: str,
    ) -> tuple[
        dict[str, str], dict[str, _manifest.ManifestEntry], dict[str, _manifest.ManifestEntry]
    ]:
        """
        Fetch/render/skip each conversation in `conv_metas` into `out`.

        Shared by `pull` (project-scoped) and `pull_standalone` (account-wide) — the
        incremental-skip and manifest-entry logic is identical either way, only the
        source of `conv_metas` differs. Returns (results, entries, previous): `entries`
        holds only uuids confirmed present this run (see the inline comment below for
        why that's kept separate from what eventually gets saved), and `previous` is
        returned so callers can compute `to_save` and prune targets themselves.

        `progress` is an already-open `rich.progress.Progress` owned by the caller (see
        `pull`/`pull_standalone`) — this method only adds its own task to it, under
        `label`, so multiple calls (e.g. one per org in `pull_standalone`) share one
        rendered display instead of each opening a separate one.

        Conversations that need a network fetch (i.e. not skipped as unchanged) are
        fetched concurrently via a bounded thread pool — `self.get()` opens a fresh
        HTTP session per call (curl_cffi, not a shared `Session`), so this is safe
        without locking. Futures are drained in submission order (not `as_completed`)
        so that filename-collision tie-breaking and the reported `results`/`entries`
        stay identical to a purely sequential run. Each worker also saves its
        conversation's images (`_fetch`) — safe without locking because every image
        path is under that conversation's own uuid directory. Everything else (the
        markdown files, `results`/`entries`) is written here, in the calling thread,
        while futures are drained.

        A conversation is only skipped when its recorded images are all still on disk
        and it was scanned for images at all (`attachments is not None` — entries
        written before image backup existed are re-fetched once). One whose image
        download failed is recorded with an empty `updated_at`, so the next run
        re-fetches it and retries the image instead of skipping it as unchanged.
        """
        previous = _manifest.load(out)
        results: dict[str, str] = {}
        # uuids confirmed present this run — see docs.py::pull for why this is kept
        # separate from what gets saved (stale, remote-absent entries must survive a
        # non-prune run so a later --prune can still find them).
        entries: dict[str, _manifest.ManifestEntry] = {}

        to_fetch: list[tuple[str, str]] = []  # (uuid, remote_updated_at)
        for conv_meta in conv_metas:
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
                and prior.attachments is not None
                and all((out / relpath).exists() for relpath in prior.attachments)
            ):
                results[prior.filename] = "unchanged"
                entries[uuid] = prior
                continue
            to_fetch.append((uuid, remote_updated_at))

        if not to_fetch:
            return results, entries, previous

        task_id = progress.add_task(label, total=len(to_fetch))
        with ThreadPoolExecutor(max_workers=_MAX_CONVERSATION_FETCH_WORKERS) as pool:
            futures = [pool.submit(self._fetch, uuid, out) for uuid, _ in to_fetch]
            for (uuid, remote_updated_at), future in zip(to_fetch, futures, strict=True):
                prior = previous.get(uuid)
                try:
                    fetched = future.result()
                except requests.exceptions.RequestException as exc:
                    logger.warning("Failed to fetch conversation %s, skipping: %s", uuid, exc)
                    if prior is not None:
                        entries[uuid] = prior
                    progress.advance(task_id)
                    continue

                for relpath in fetched.downloaded:
                    results[relpath] = "created"
                for stem, exc in fetched.failed.items():
                    logger.warning(
                        "Failed to download attachment %s of conversation %s, skipping: %s",
                        stem,
                        uuid,
                        exc,
                    )
                    results[stem] = "failed"

                conv = fetched.conv
                content = conversation_to_markdown(conv, fetched.saved)
                filename = conversation_filename(conv)
                dest = out / filename
                existed = dest.exists()
                if not force and existed and dest.read_text(encoding="utf-8") == content:
                    results[filename] = "unchanged"
                else:
                    dest.write_text(content, encoding="utf-8")
                    results[filename] = "updated" if existed else "created"
                entries[uuid] = _manifest.ManifestEntry(
                    filename=filename,
                    updated_at="" if fetched.failed else remote_updated_at,
                    attachments=tuple(sorted(fetched.saved.values())),
                )
                progress.advance(task_id)

        return results, entries, previous

    @staticmethod
    def _save_manifest(
        out: Path,
        results: dict[str, str],
        entries: dict[str, _manifest.ManifestEntry],
        previous: dict[str, _manifest.ManifestEntry],
        *,
        prune: bool,
    ) -> None:
        """
        Save the manifest, first pruning (if asked) what was removed on the web.

        Shared by `pull` and `pull_standalone`. A prune target whose uuid is still in
        `entries` is a rename: only its old .md goes, its images (keyed by uuid, not
        filename) and new manifest entry stay. A uuid absent from `entries` is gone on
        the web, so its whole attachments directory goes with it. Only called after a
        successful listing — a failed one raises before reaching here, so nothing is
        pruned on a failed pull.
        """
        to_save = {**previous, **entries}
        if prune:
            for uuid, filename in _manifest.prune_targets(previous, entries):
                (out / filename).unlink(missing_ok=True)
                results[filename] = "deleted"
                if uuid in entries:
                    continue
                to_save.pop(uuid, None)
                attachments_dir = out / ATTACHMENTS_DIR / uuid
                if attachments_dir.exists():
                    shutil.rmtree(attachments_dir)
                    results[f"{ATTACHMENTS_DIR}/{uuid}"] = "deleted"
        _manifest.save(out, to_save)

    def _pull_into(
        self,
        project_id: str,
        output_dir: str | Path,
        *,
        force: bool,
        prune: bool,
        progress: Progress,
        label: str,
    ) -> dict[str, str]:
        """Worker for `pull` — fetches into an already-open, caller-owned `progress`."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        conv_metas = self.list(project_id)
        results, entries, previous = self._pull_conversations(
            conv_metas, out, force=force, progress=progress, label=label
        )

        self._save_manifest(out, results, entries, previous, prune=prune)
        return results

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

        Images uploaded to a conversation are saved under
        `attachments/<conversation_uuid>/` and linked inline at the message they were
        attached to. An image already on disk is never re-downloaded. A failed image
        download is logged and reported "failed" under its path (without extension),
        never aborting the conversation or the run.

        Incremental over the network via a sidecar manifest keyed by conversation uuid: a
        conversation whose remote `updated_at` matches the manifest and whose local file
        still exists is never re-fetched, and is reported "unchanged". Pass force=True to
        bypass the manifest and always re-fetch and rewrite every file (e.g. to recover
        from local edits) — web is the source of truth whenever it changed.

        Pass prune=True to delete local files (and attachments) for conversations removed
        on the web (reported "deleted"); default is off so ad-hoc pulls never delete
        anything. Returns a dict mapping each filename to "created", "updated",
        "unchanged", "deleted", or (attachments only) "failed".
        """
        with Progress() as progress:
            return self._pull_into(
                project_id,
                output_dir,
                force=force,
                prune=prune,
                progress=progress,
                label="Pulling conversations…",
            )

    def list_standalone(self) -> list[ConversationDict]:
        """
        Every conversation in this org NOT in a project.

        Org-scoped like `get()` — on a multi-org account with no pinned org, this raises
        `AmbiguousOrgError`; use `.scoped(org_id)` first. Hits a different endpoint than
        `list()`: `chat_conversations` (no project id) returns every conversation in the
        org, project-scoped and standalone alike, distinguished by `project_uuid` (absent
        for standalone ones) — filtered here to standalone only. Unlike `conversations_v2`,
        this endpoint returns a flat list with no pagination metadata, so termination is
        "got fewer than asked for". It also tolerates a much larger page size than
        `conversations_v2`'s de facto 30 (verified: 200 and even 1000 both work), which
        matters once an account has 1000+ conversations.
        """
        results: list[ConversationDict] = []
        offset = 0
        while True:
            resp = self._t.get(
                f"{BASE_URL}/organizations/{self._t.org_id}/chat_conversations"
                f"?limit={_STANDALONE_PAGE_LIMIT}&offset={offset}"
            )
            page = resp.json()
            results.extend(c for c in page if not c.get("project_uuid"))
            if len(page) < _STANDALONE_PAGE_LIMIT:
                break
            offset += _STANDALONE_PAGE_LIMIT
        return results

    def pull_standalone(
        self, out_dir: str | Path, *, force: bool = False, prune: bool = False
    ) -> dict[str, str]:
        """
        Pull every standalone (non-project) conversation across every chat-capable org on
        this account into one flat directory.

        Account-wide like `ProjectsResource.pull_all` — call on an unscoped client, not one
        already pinned to a single org, or only that org's standalone chats are pulled.

        Same incremental/prune semantics as `pull()`: unchanged conversations are skipped
        via the manifest, images are saved under `attachments/`, prune=True deletes local
        files and attachments for conversations removed on the web. Conversation uuids
        are unique account-wide, so all orgs safely share one manifest in `out_dir`.

        `_pull_conversations` is called once per org, through a transport pinned to that
        org (not `self`, which may be unscoped on a multi-org account) — `get()` needs a
        pinned org to resolve the right one, same as it would if called directly. Each
        call reloads the same on-disk manifest (harmless: nothing is saved until the very
        last one), and their `results`/`entries` are merged before the final save.

        A per-conversation fetch failure is handled exactly like `pull()` — logged and
        skipped, never aborting the run. But unlike `pull_all`'s per-project isolation, a
        `list_standalone()` failure (the listing call itself, not a single conversation)
        in any one org fails this call entirely rather than skipping that org —
        deliberately simpler, matching how `self.list()` is allowed to propagate elsewhere
        in this SDK. Revisit only if a real multi-org account hits this in practice.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        results: dict[str, str] = {}
        entries: dict[str, _manifest.ManifestEntry] = {}
        previous: dict[str, _manifest.ManifestEntry] = {}
        with Progress() as progress:
            for org_id in self._t.chat_capable_org_ids():
                scoped = type(self)(self._t.scoped(org_id))
                org_results, org_entries, previous = scoped._pull_conversations(
                    scoped.list_standalone(),
                    out,
                    force=force,
                    progress=progress,
                    label=f"Pulling standalone conversations — org {org_id}",
                )
                results.update(org_results)
                entries.update(org_entries)

        self._save_manifest(out, results, entries, previous, prune=prune)
        return results
