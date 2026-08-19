from __future__ import annotations

import shutil
from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import Progress, SpinnerColumn, TextColumn

from .. import _manifest
from .._transport import BASE_URL, Transport
from ..exceptions import NotFoundError
from ..models import ProjectDict, ProjectExport, ProjectSyncResult
from ..render import render_project, render_project_metadata, slugify
from .conversations import ConversationsResource
from .docs import DocsResource
from .memory import MemoryResource

logger = get_logger(__name__)


def _resolve_project_slugs(
    projects: list[tuple[str, ProjectDict]], previous: dict[str, _manifest.ManifestEntry]
) -> dict[str, str]:
    """
    Disambiguate slugify(name) collisions across every chat-capable org's projects.

    Two projects with the same (or same-after-slugify) name in different orgs would
    otherwise both write to `out_root / slug`, silently mixing their docs and
    conversations together. Mirrors `docs.py::_resolve_doc_filenames`.

    A uuid already in the manifest keeps its previously-assigned directory (`stable`
    below) rather than being re-slugified from scratch every run — otherwise, if a
    colliding sibling later disappeared, the survivor's slug would drop its
    disambiguating suffix and rename-then-prune its own directory for no reason other
    than a sibling being gone from this run's listing. A brand-new uuid only gets a
    suffix if its bare slug collides with another new uuid this run OR with an
    already-stable slug on disk.
    """
    stable = {uuid: entry.filename for uuid, entry in previous.items()}
    new_candidates = {
        project["uuid"]: slugify(project.get("name", project["uuid"]))
        for _, project in projects
        if project["uuid"] not in previous
    }
    counts: dict[str, int] = {}
    for slug in new_candidates.values():
        counts[slug] = counts.get(slug, 0) + 1
    taken = set(stable.values())

    resolved = dict(stable)
    for uuid, slug in new_candidates.items():
        if counts[slug] > 1 or slug in taken:
            resolved[uuid] = f"{slug}-{uuid[:8]}"
        else:
            resolved[uuid] = slug
            taken.add(slug)
    return resolved


class ProjectsResource:
    """
    Projects, plus the composite operations that pull a project's docs,
    conversations, and memory together (export/pull/pull_all).
    """

    def __init__(
        self,
        transport: Transport,
        *,
        docs: DocsResource,
        conversations: ConversationsResource,
        memory: MemoryResource,
    ) -> None:
        self._t = transport
        self._docs = docs
        self._conversations = conversations
        self._memory = memory

    # --------------------------------------------------------------- lookup

    def list(self, *, org_id: str | None = None) -> list[tuple[str, ProjectDict]]:
        """
        List projects as (org_id, project) pairs.

        Defaults to every chat-capable org on the account — pass org_id to scope to
        just one. The org is always included in the result because a project id alone
        isn't enough to operate on it again; on a multi-org account you need to know
        which org it lives in.
        """
        org_ids = [org_id] if org_id else self._t.chat_capable_org_ids()
        results: list[tuple[str, ProjectDict]] = []
        for oid in org_ids:
            resp = self._t.get(f"{BASE_URL}/organizations/{oid}/projects")
            results.extend((oid, project) for project in resp.json())
        return results

    def get(self, project_id: str) -> ProjectDict:
        resp = self._t.get(f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}")
        return resp.json()

    def find(self, name: str) -> tuple[str, ProjectDict]:
        """
        Find a project by exact name across every chat-capable org.

        Returns (org_id, project) — same shape as `list()`, and for the same reason: a
        project name alone isn't enough to act on it again on a multi-org account. If
        more than one org has a project with this name, the first match wins and a
        warning is logged; callers who need to disambiguate should use `list()` directly.
        Raises NotFoundError if no org has a matching project.
        """
        matches = [(org_id, p) for org_id, p in self.list() if p.get("name") == name]
        if not matches:
            raise NotFoundError(f"Project '{name}' not found.")
        if len(matches) > 1:
            logger.warning(
                "Project name '%s' matches %d projects across orgs; returning the first (org %s)",
                name,
                len(matches),
                matches[0][0],
            )
        return matches[0]

    def find_org(self, project_id: str) -> str:
        """
        Find which of this account's organizations owns a project.

        Needed because `Transport.org_id` only auto-picks the *first* chat-capable org,
        which isn't necessarily the one a given project lives in (accounts can belong
        to multiple orgs). Raises NotFoundError if no org has this project.
        """
        for org in self._t.list_organizations():
            resp = self._t.get(f"{BASE_URL}/organizations/{org['uuid']}/projects")
            if any(p["uuid"] == project_id for p in resp.json()):
                return str(org["uuid"])
        raise NotFoundError(f"Project '{project_id}' not found in any organization.")

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
    ) -> ProjectDict:
        """Update project metadata. Only provided fields are sent."""
        payload: dict[str, str] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if instructions is not None:
            payload["prompt_template"] = instructions
        resp = self._t.put(
            f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}", payload
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------- composite

    def export_data(self, project_id: str) -> ProjectExport:
        """
        Download all project data into a ProjectExport object.

        Includes title, description, instructions, generated memory, controls,
        knowledge docs, and all conversations with their messages.
        """
        project = self.get(project_id)
        docs_meta = self._docs.list(project_id)

        docs = []
        for meta in docs_meta:
            try:
                docs.append(self._docs.get(project_id, meta["uuid"]))
            except requests.exceptions.RequestException:
                logger.warning(
                    "Failed to fetch doc %s for export, using metadata only",
                    meta.get("uuid", "unknown"),
                )
                docs.append(meta)

        memory_data = self._memory.get(project_id)

        conversations = []
        for conv in self._conversations.list(project_id):
            try:
                conversations.append(self._conversations.get(conv["uuid"]))
            except requests.exceptions.RequestException:
                logger.warning(
                    "Failed to fetch conversation %s for export, skipping",
                    conv.get("uuid", "unknown"),
                )

        return ProjectExport(
            uuid=project_id,
            name=project.get("name", ""),
            description=project.get("description", ""),
            instructions=project.get("prompt_template", ""),
            memory=memory_data.get("memory", ""),
            controls=memory_data.get("controls", []),
            docs=docs,
            conversations=conversations,
        )

    def export(self, project_id: str) -> str:
        """Render a project to a single markdown string (title, docs, and conversations)."""
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), transient=True
        ) as progress:
            progress.add_task("Exporting project…")
            data = self.export_data(project_id)
        return render_project(data)

    def pull(
        self,
        project_id: str,
        output_dir: str | Path,
        *,
        force: bool = False,
        prune: bool = False,
    ) -> ProjectSyncResult:
        """
        Pull a project into a directory: project.md, docs/, conversations/.

        project.md (name, description, instructions, memory, controls) is rewritten
        every run — it's small and has no per-file identity to diff against. docs/ and
        conversations/ are pulled incrementally via DocsResource.pull /
        ConversationsResource.pull; pass force=True to make those unconditional too, and
        prune=True to delete local docs/conversations removed on the web.
        Returns the output directory path plus a per-file status for docs/conversations.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        project = self.get(project_id)
        memory_data = self._memory.get(project_id)
        meta = ProjectExport(
            uuid=project_id,
            name=project.get("name", ""),
            description=project.get("description", ""),
            instructions=project.get("prompt_template", ""),
            memory=memory_data.get("memory", ""),
            controls=memory_data.get("controls", []),
        )
        (out / "project.md").write_text(render_project_metadata(meta), encoding="utf-8")

        docs_results = self._docs.pull(project_id, out / "docs", force=force, prune=prune)
        conversations_results = self._conversations.pull(
            project_id, out / "conversations", force=force, prune=prune
        )

        return ProjectSyncResult(path=out, docs=docs_results, conversations=conversations_results)

    def pull_all(
        self, out_dir: str | Path, *, force: bool = False, prune: bool = False
    ) -> dict[str, bool]:
        """
        Pull every project across every chat-capable org on this account.

        Writes {out_dir}/{project_slug}/{project.md,docs/,conversations/} per project
        (see `pull`). Encapsulates the multi-org scoping so callers don't need their
        own org-to-transport bookkeeping. Returns a map of project name -> success; one
        project failing is logged and skipped, never aborts the rest.

        Pass prune=True to also delete per-project docs/conversations removed on the
        web (via `pull`) and to remove local project directories for projects deleted
        on the web. A project directory is only removed once its uuid is confirmed
        absent from the remote project list; a project whose own pull fails is never
        pruned, even with prune=True. Two projects that would slugify to the same
        directory name (e.g. same name in different orgs) are disambiguated with a
        uuid suffix so neither's pull overwrites the other's mirror.
        """
        out_root = Path(out_dir)
        projects = self.list()
        previous = _manifest.load(out_root)
        slugs = _resolve_project_slugs(projects, previous)

        scoped: dict[str, ProjectsResource] = {}
        results: dict[str, bool] = {}
        # uuids confirmed present this run — see docs.py::pull for why this is kept
        # separate from what gets saved.
        entries: dict[str, _manifest.ManifestEntry] = {}
        for org_id, project in projects:
            resource = scoped.setdefault(org_id, self._scoped(org_id))
            uuid = project["uuid"]
            name = project.get("name", uuid)
            slug = slugs[uuid]
            try:
                resource.pull(uuid, out_root / slug, force=force, prune=prune)
                results[name] = True
                entries[uuid] = _manifest.ManifestEntry(filename=slug, updated_at="")
            except requests.exceptions.RequestException:
                logger.warning("Failed to pull project '%s', skipping", name)
                results[name] = False
                # Keep tracking this uuid's directory even on a first-ever failure (its
                # dir was already created by `pull`'s mkdir), so a later prune run can
                # still remove it if the project turns out to be gone for good.
                fallback = _manifest.ManifestEntry(filename=slug, updated_at="")
                entries[uuid] = previous.get(uuid, fallback)

        to_save = {**previous, **entries}
        if prune:
            for uuid, slug in _manifest.prune_targets(previous, entries):
                shutil.rmtree(out_root / slug, ignore_errors=True)
                to_save.pop(uuid, None)

        _manifest.save(out_root, to_save)
        return results

    def _scoped(self, org_id: str) -> ProjectsResource:
        """A ProjectsResource (with matching docs/conversations/memory) pinned to org_id."""
        transport = self._t.scoped(org_id)
        docs = DocsResource(transport)
        conversations = ConversationsResource(transport)
        memory = MemoryResource(transport)
        return ProjectsResource(transport, docs=docs, conversations=conversations, memory=memory)
