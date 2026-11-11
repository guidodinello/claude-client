from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import Progress

from .. import _manifest
from .._transport import BASE_URL, Transport
from ..models import MelangeEntryDict, MelangeFileDict, MemoryDict

logger = get_logger(__name__)

# Path segments that could make a remote melange path escape the local org directory.
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})


class MelangePathError(ValueError):
    """A remote melange path that can't be safely mapped under the local output dir."""


def _safe_local_path(root: Path, remote_path: str) -> Path:
    """
    Map a remote melange path (`/projects/{uuid}/topics/x.md`) to a file under `root`.

    Fails closed: anything that isn't a plain absolute path of normal segments is
    rejected (empty/`.`/`..` segments, backslashes, NUL), and the resolved result must
    still be inside `root` — that last check also catches a symlink under `root` that
    points elsewhere.
    """
    if not remote_path.startswith("/") or "\\" in remote_path or "\0" in remote_path:
        raise MelangePathError(f"unsafe melange path: {remote_path!r}")
    segments = remote_path[1:].split("/")
    if any(segment in _FORBIDDEN_SEGMENTS for segment in segments):
        raise MelangePathError(f"unsafe melange path: {remote_path!r}")
    local = root.joinpath(*segments)
    if not local.resolve().is_relative_to(root.resolve()):
        raise MelangePathError(f"melange path escapes output dir: {remote_path!r}")
    return local


def _remove_empty_parents(path: Path, root: Path) -> None:
    """Remove now-empty directories from `path`'s parent up to (not including) `root`."""
    parent = path.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:  # not empty (or already gone) — stop climbing
            return
        parent = parent.parent


def _is_not_found(exc: requests.exceptions.HTTPError) -> bool:
    response = getattr(exc, "response", None)
    return response is not None and response.status_code == HTTPStatus.NOT_FOUND


class MemoryResource:
    """
    Project and org memory (read-only — no write endpoint exists).

    Two systems coexist: the legacy single-blob memory (`get`/`get_general`), now frozen
    for projects migrated to melange, and melange — org-wide, path-addressed memory files
    (`list_entries`/`read_entry`/`pull`).

    Every call is scoped to the transport's org (`self._t.org_id`). On a multi-org
    account with no pinned org, `org_id` raises `AmbiguousOrgError` — use
    `ClaudeClient.for_project(project_id)` or `.scoped(org_id)` first. `pull` is the
    exception: it spans every chat-capable org itself.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def get(self, project_id: str) -> MemoryDict:
        """
        Fetch the legacy project memory and controls. Scoped to the transport's org.

        `include_project_import=true` is what the web app sends: without it the server
        returns `project_import`/`project_memory_mode` as null even for projects migrated
        to melange (verified live), hiding that the blob is frozen.
        """
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/memory"
            f"?project_uuid={project_id}&include_project_import=true"
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

    # ------------------------------------------------------------- melange

    def list_entries(self) -> list[MelangeEntryDict]:
        """List every melange memory file in the transport's org (metadata only)."""
        resp = self._t.post(f"{BASE_URL}/organizations/{self._t.org_id}/melange/list", {})
        return resp.json()["data"]

    def read_entry(self, path: str) -> MelangeFileDict:
        """Read one melange memory file, including its raw markdown `content`."""
        resp = self._t.post(
            f"{BASE_URL}/organizations/{self._t.org_id}/melange/read", {"path": path}
        )
        return resp.json()

    def pull(
        self, out_dir: str | Path, *, force: bool = False, prune: bool = False
    ) -> dict[str, str]:
        """
        Mirror every chat-capable org's melange memory to `{out_dir}/{org_id}/<path>`.

        Account-wide like `ConversationsResource.pull_standalone` — call on an unscoped
        client. The org level exists because paths are only unique within an org (every
        org has its own `/profile.md`). Each file's `content` is written verbatim.

        Incremental per org via a manifest keyed by remote path, recording each file's
        `version`: a file is only read when its version changed or the local copy is
        missing (or force=True). prune=True deletes local files whose path is gone from
        a successful listing. An org answering 404 to `melange/list` doesn't have
        melange: it's logged and skipped, not a failure. Any other listing error
        propagates. A failed per-file read is logged, reported "failed", and keeps the
        previous local copy; an unsafe remote path is reported "rejected" and never
        written. Returns `{"{org_id}{path}": status}`.
        """
        out = Path(out_dir)
        results: dict[str, str] = {}
        with Progress() as progress:
            for org_id in self._t.chat_capable_org_ids():
                scoped = type(self)(self._t.scoped(org_id))
                try:
                    listed = scoped.list_entries()
                except requests.exceptions.HTTPError as exc:
                    if not _is_not_found(exc):
                        raise
                    logger.warning("Melange memory not enabled for org %s, skipping", org_id)
                    continue
                org_results = scoped._pull_org(
                    listed, out / org_id, force=force, prune=prune, progress=progress
                )
                results.update({f"{org_id}{path}": status for path, status in org_results.items()})
        return results

    def _pull_org(
        self,
        listed: list[MelangeEntryDict],
        out: Path,
        *,
        force: bool,
        prune: bool,
        progress: Progress,
    ) -> dict[str, str]:
        """Pull one org's listed entries into `out`; returns `{remote_path: status}`."""
        out.mkdir(parents=True, exist_ok=True)
        previous = _manifest.load(out)
        results: dict[str, str] = {}
        # Paths confirmed present this run — kept separate from what gets saved (see
        # `_manifest.prune_targets`).
        entries: dict[str, _manifest.ManifestEntry] = {}

        task = progress.add_task(f"Pulling memory — org {out.name}", total=len(listed))
        for entry in listed:
            path = entry["path"]
            progress.advance(task)
            try:
                local = _safe_local_path(out, path)
            except MelangePathError as exc:
                logger.error("Skipping melange entry: %s", exc)
                results[path] = "rejected"
                continue
            relpath = local.relative_to(out).as_posix()
            version = entry["version"]
            prev = previous.get(path)
            if (
                not force
                and prev is not None
                and prev.filename == relpath
                and prev.updated_at == version
                and local.exists()
            ):
                results[path] = "unchanged"
                entries[path] = prev
                continue
            try:
                content = self.read_entry(path)["content"]
            except requests.exceptions.RequestException as exc:
                logger.warning("Failed to read melange entry %s, skipping: %s", path, exc)
                results[path] = "failed"
                if prev is not None:  # still listed, so keep the old copy and its entry
                    entries[path] = prev
                continue
            existed = local.exists()
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content, encoding="utf-8")
            results[path] = "updated" if existed else "created"
            # `updated_at` holds melange's `version`, the remote change token.
            entries[path] = _manifest.ManifestEntry(filename=relpath, updated_at=version)

        to_save = {**previous, **entries}
        if prune:
            for path, filename in _manifest.prune_targets(previous, entries):
                stale = out / filename
                stale.unlink(missing_ok=True)
                _remove_empty_parents(stale, out)
                results[path] = "deleted"
                if path not in entries:
                    to_save.pop(path, None)
        _manifest.save(out, to_save)
        return results
