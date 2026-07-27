import json
import os
from collections.abc import Sequence
from functools import cached_property
from http import HTTPStatus
from pathlib import Path

from curl_cffi import requests
from logger import get_logger
from rich.progress import Progress, SpinnerColumn, TextColumn, track

from .exceptions import AuthError, NotFoundError, UploadError
from .models import (
    ConversationDetailDict,
    ConversationDict,
    DocDict,
    MemoryDict,
    OrgDict,
    Page,
    ProjectDict,
    ProjectExport,
    ProjectSyncResult,
)
from .render import (
    conversation_filename,
    conversation_to_markdown,
    render_project,
    render_project_metadata,
    slugify,
)

logger = get_logger(__name__)

BASE_URL = "https://claude.ai/api"


def _ensure_md(name: str) -> str:
    return name if name.endswith(".md") else f"{name}.md"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
_IMPERSONATE = "chrome110"
_DEFAULT_TIMEOUT = 30  # seconds; guards against a hung connection blocking forever


class ClaudeClient:
    """
    Client for the Claude.ai unofficial web API.

    Supports listing, uploading, downloading, syncing, and exporting
    project files (knowledge docs).
    """

    def __init__(self, session_token: str | None = None, *, org_id: str | None = None) -> None:
        token = session_token or os.getenv("CLAUDE_SESSION_TOKEN")
        if not token:
            raise ValueError("Session token required. Pass it or set CLAUDE_SESSION_TOKEN.")
        self._session_token = token
        self._cookie = f"sessionKey={token}"
        if org_id is not None:
            # Shadows the `org_id` cached_property: it's a non-data descriptor, so an
            # entry in the instance __dict__ takes precedence over it.
            self.__dict__["org_id"] = org_id

    # ------------------------------------------------------------------ auth

    def update_token(self, session_token: str) -> None:
        self._session_token = session_token
        self._cookie = f"sessionKey={session_token}"
        self.__dict__.pop("org_id", None)

    def scoped_client(self, org_id: str) -> "ClaudeClient":
        """A client sharing this account's token but pinned to a specific org."""
        return ClaudeClient(self._session_token, org_id=org_id)

    # ------------------------------------------------------------ internals

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://claude.ai/chats",
            "Content-Type": "application/json",
            "Origin": "https://claude.ai",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": self._cookie,
        }

    def _get(self, url: str) -> requests.Response:
        resp = requests.get(
            url,
            headers=self._headers(),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, payload: dict) -> requests.Response:
        resp = requests.post(
            url,
            headers=self._headers(),
            data=json.dumps(payload),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        return resp

    def _put(self, url: str, payload: dict) -> requests.Response:
        resp = requests.put(
            url,
            headers=self._headers(),
            data=json.dumps(payload),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        return resp

    def _delete(self, url: str) -> requests.Response:
        resp = requests.delete(
            url,
            headers=self._headers(),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        resp.raise_for_status()
        return resp

    def _check_auth(self, resp: requests.Response) -> None:
        if resp.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthError(
                "Session token is invalid or expired. Refresh CLAUDE_SESSION_TOKEN from claude.ai."
            )

    # ------------------------------------------------------- org / projects

    def list_organizations(self) -> list[OrgDict]:
        resp = self._get(f"{BASE_URL}/organizations")
        return resp.json()

    def chat_capable_org_ids(self) -> list[str]:
        """Every org uuid on this account with 'chat' or 'claude_pro' capabilities."""
        return [
            str(org["uuid"])
            for org in self.list_organizations()
            if "chat" in org.get("capabilities", []) or "claude_pro" in org.get("capabilities", [])
        ]

    @cached_property
    def org_id(self) -> str:
        org_ids = self.chat_capable_org_ids()
        if not org_ids:
            raise ValueError("No org found with 'chat' or 'claude_pro' capabilities.")
        return org_ids[0]

    def list_projects(self) -> list[ProjectDict]:
        resp = self._get(f"{BASE_URL}/organizations/{self.org_id}/projects")
        return resp.json()

    def list_all_projects(self) -> list[tuple[str, ProjectDict]]:
        """
        List every project across all chat-capable orgs on this account.

        Returns (org_id, project) pairs — useful when an account belongs to multiple
        orgs and you want every project regardless of which org owns it.
        """
        results: list[tuple[str, ProjectDict]] = []
        for org_id in self.chat_capable_org_ids():
            resp = self._get(f"{BASE_URL}/organizations/{org_id}/projects")
            results.extend((org_id, project) for project in resp.json())
        return results

    def get_project(self, project_id: str) -> ProjectDict:
        resp = self._get(f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}")
        return resp.json()

    def find_project(self, name: str) -> ProjectDict:
        """Find a project by exact name. Raises NotFoundError if not found."""
        for p in self.list_projects():
            if p.get("name") == name:
                return p
        raise NotFoundError(f"Project '{name}' not found.")

    def find_project_org(self, project_id: str) -> str:
        """
        Find which of this account's organizations owns a project.

        Needed because `org_id` only auto-picks the *first* chat-capable org, which
        isn't necessarily the one a given project lives in (accounts can belong to
        multiple orgs). Raises NotFoundError if no org has this project.
        """
        for org in self.list_organizations():
            resp = self._get(f"{BASE_URL}/organizations/{org['uuid']}/projects")
            if any(p["uuid"] == project_id for p in resp.json()):
                return str(org["uuid"])
        raise NotFoundError(f"Project '{project_id}' not found in any organization.")

    def update_project(
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
        resp = self._put(f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}", payload)
        resp.raise_for_status()
        return resp.json()

    # ----------------------------------------------------------------- docs

    def list_docs(self, project_id: str) -> list[DocDict]:
        resp = self._get(f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}/docs")
        return resp.json()

    def get_doc(self, project_id: str, doc_uuid: str) -> DocDict:
        """Fetch a single doc with its full content."""
        resp = self._get(
            f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}/docs/{doc_uuid}"
        )
        return resp.json()

    def upload_content(self, project_id: str, content: str, file_name: str) -> DocDict:
        url = f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}/docs"
        resp = self._post(url, {"file_name": file_name, "content": content})
        if resp.status_code != HTTPStatus.CREATED:
            raise UploadError(f"Upload of '{file_name}' failed: {resp.status_code} {resp.text}")
        return resp.json()

    def upload_file(
        self, project_id: str, file_path: str | Path, file_name: str | None = None
    ) -> DocDict:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        content = path.read_text(encoding="utf-8", errors="ignore")
        return self.upload_content(project_id, content, file_name or path.name)

    def delete_doc(self, project_id: str, doc_uuid: str) -> None:
        self._delete(
            f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}/docs/{doc_uuid}"
        )

    def delete_all_docs(self, project_id: str) -> int:
        """Delete all docs in a project. Returns the count deleted."""
        docs = self.list_docs(project_id)
        for doc in docs:
            self.delete_doc(project_id, doc["uuid"])
        return len(docs)

    # ----------------------------------------------------------------- sync

    def upsert_content(self, project_id: str, content: str, file_name: str) -> DocDict:
        """Upload content, replacing any existing doc with the same name."""
        deleted_existing = False
        for doc in self.list_docs(project_id):
            if doc.get("file_name") == file_name:
                self.delete_doc(project_id, doc["uuid"])
                deleted_existing = True
                break
        if not deleted_existing:
            return self.upload_content(project_id, content, file_name)
        try:
            return self.upload_content(project_id, content, file_name)
        except Exception as exc:
            raise UploadError(
                f"Upload of '{file_name}' failed after deleting the existing doc; "
                f"the original has been removed from the project."
            ) from exc

    def upsert_file(
        self, project_id: str, file_path: str | Path, file_name: str | None = None
    ) -> DocDict:
        """Upload a file, replacing any existing doc with the same name."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        content = path.read_text(encoding="utf-8", errors="ignore")
        return self.upsert_content(project_id, content, file_name or path.name)

    def sync_files(
        self,
        project_id: str,
        file_paths: Sequence[str | Path],
        name_prefix: str = "",
    ) -> dict[str, bool]:
        """
        Upsert multiple files into a project.

        Returns a dict mapping file names to success status.
        """
        results: dict[str, bool] = {}
        for fp in file_paths:
            path = Path(fp)
            name = f"{name_prefix}{path.name}" if name_prefix else path.name
            try:
                self.upsert_file(project_id, path, name)
                results[name] = True
            except Exception as exc:
                results[name] = False
                print(f"Failed to sync '{name}': {exc}")
        return results

    # --------------------------------------------------------- conversations

    def list_conversations(
        self, project_id: str, limit: int = 30, offset: int = 0
    ) -> Page[ConversationDict]:
        resp = self._get(
            f"{BASE_URL}/organizations/{self.org_id}/projects/{project_id}/conversations_v2"
            f"?limit={limit}&offset={offset}"
        )
        raw = resp.json()
        return Page(data=raw["data"], pagination=raw["pagination"])

    def list_all_conversations(self, project_id: str) -> list[ConversationDict]:
        """Fetch every conversation in a project, handling pagination automatically."""
        results: list[ConversationDict] = []
        offset = 0
        limit = 30
        while True:
            page = self.list_conversations(project_id, limit=limit, offset=offset)
            results.extend(page.data)
            if not page.pagination["has_more"]:
                break
            offset += limit
        return results

    def get_conversation(self, conversation_id: str) -> ConversationDetailDict:
        """
        Fetch a single conversation with full message content.

        Conversation ids are unique within an org, so this doesn't need a project id —
        unlike `list_conversations`, which lists within one project's scope.
        """
        resp = self._get(
            f"{BASE_URL}/organizations/{self.org_id}/chat_conversations/{conversation_id}"
            f"?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual"
        )
        return resp.json()

    def export_conversation_to_file(self, conversation_id: str, output_path: str | Path) -> Path:
        """Export a single conversation to a markdown file."""
        conv = self.get_conversation(conversation_id)
        content = conversation_to_markdown(conv)
        out = Path(output_path)
        out.write_text(content, encoding="utf-8")
        return out

    def export_conversations_to_files(self, project_id: str, output_dir: str | Path) -> list[Path]:
        """Export all conversations in a project to markdown files."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for conv_meta in track(
            self.list_all_conversations(project_id), description="Exporting conversations…"
        ):
            try:
                conv = self.get_conversation(conv_meta["uuid"])
                content = conversation_to_markdown(conv)
                filename = conversation_filename(conv)
                dest = out / filename
                dest.write_text(content, encoding="utf-8")
                written.append(dest)
            except Exception:
                logger.warning("Failed to export conversation %s", conv_meta.get("uuid", "unknown"))
        return written

    def sync_conversations_from_web(self, project_id: str, local_dir: str | Path) -> dict[str, str]:
        """
        Sync conversations from the web project to a local directory.

        Web is the source of truth. Returns a dict mapping each filename to
        "created", "updated", or "unchanged".
        """
        out = Path(local_dir)
        out.mkdir(parents=True, exist_ok=True)

        results: dict[str, str] = {}
        for conv_meta in track(
            self.list_all_conversations(project_id), description="Syncing conversations…"
        ):
            try:
                conv = self.get_conversation(conv_meta["uuid"])
            except Exception:
                logger.warning(
                    "Failed to fetch conversation %s, skipping",
                    conv_meta.get("uuid", "unknown"),
                )
                continue

            content = conversation_to_markdown(conv)
            filename = conversation_filename(conv)
            dest = out / filename

            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                if existing == content:
                    results[filename] = "unchanged"
                    continue
                dest.write_text(content, encoding="utf-8")
                results[filename] = "updated"
            else:
                dest.write_text(content, encoding="utf-8")
                results[filename] = "created"
        return results

    # --------------------------------------------------------------- memory

    def get_memory(self, project_id: str) -> MemoryDict:
        """Fetch the auto-generated project memory and controls."""
        resp = self._get(f"{BASE_URL}/organizations/{self.org_id}/memory?project_uuid={project_id}")
        return resp.json()

    def get_general_memory(self) -> MemoryDict:
        """Fetch the org-level general memory (not project-specific)."""
        resp = self._get(f"{BASE_URL}/organizations/{self.org_id}/memory")
        return resp.json()

    # --------------------------------------------------------------- export

    def export_project(self, project_id: str) -> ProjectExport:
        """
        Download all project data into a ProjectExport object.

        Includes title, description, instructions, generated memory, controls,
        knowledge docs, and all conversations with their messages.
        """
        project = self.get_project(project_id)
        docs_meta = self.list_docs(project_id)

        docs = []
        for meta in docs_meta:
            try:
                doc = self.get_doc(project_id, meta["uuid"])
                docs.append(doc)
            except Exception:
                docs.append(meta)

        memory_data = self.get_memory(project_id)

        conversations = []
        for conv in self.list_all_conversations(project_id):
            try:
                conv_detail = self.get_conversation(conv["uuid"])
                conversations.append(conv_detail)
            except Exception:
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

    def export_project_to_file(self, project_id: str, output_path: str | Path) -> Path:
        """Export a project to a markdown file. Returns the output path."""
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), transient=True
        ) as progress:
            progress.add_task("Exporting project…")
            export = self.export_project(project_id)
        out = Path(output_path)
        if out.is_dir():
            out = out / f"{project_id}.md"
        out.write_text(render_project(export), encoding="utf-8")
        return out

    def export_project_to_dir(self, project_id: str, output_dir: str | Path) -> ProjectSyncResult:
        """
        Sync a project to a directory, incrementally.

        Writes project.md (name, description, instructions, memory, controls) every run, and
        syncs docs/ and conversations/ via sync_from_web / sync_conversations_from_web — files
        whose content hasn't changed on the web side are left untouched. Returns the output
        directory path plus a per-file "created"/"updated"/"unchanged" status for docs and
        conversations.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        project = self.get_project(project_id)
        memory_data = self.get_memory(project_id)
        meta = ProjectExport(
            uuid=project_id,
            name=project.get("name", ""),
            description=project.get("description", ""),
            instructions=project.get("prompt_template", ""),
            memory=memory_data.get("memory", ""),
            controls=memory_data.get("controls", []),
        )
        (out / "project.md").write_text(render_project_metadata(meta), encoding="utf-8")

        docs_results = self.sync_from_web(project_id, out / "docs")
        conversations_results = self.sync_conversations_from_web(project_id, out / "conversations")

        return ProjectSyncResult(path=out, docs=docs_results, conversations=conversations_results)

    def export_all_projects_to_dir(self, out_dir: str | Path) -> dict[str, bool]:
        """
        Export every project across every chat-capable org on this account.

        Writes {out_dir}/{project_slug}/{project.md,docs/,conversations/} per project
        (see export_project_to_dir). Encapsulates the multi-org scoping so callers don't
        need their own org-to-client bookkeeping. Returns a map of project name -> success;
        one project failing is logged and skipped, never aborts the rest.
        """
        out_root = Path(out_dir)
        projects = self.list_all_projects()

        scoped_clients: dict[str, ClaudeClient] = {}
        results: dict[str, bool] = {}
        for org_id, project in projects:
            client = scoped_clients.setdefault(org_id, self.scoped_client(org_id))
            name = project.get("name", project["uuid"])
            try:
                client.export_project_to_dir(project["uuid"], out_root / slugify(name))
                results[name] = True
            except Exception:
                logger.warning("Failed to export project '%s', skipping", name)
                results[name] = False

        return results

    def download_docs(self, project_id: str, output_dir: str | Path) -> list[Path]:
        """
        Download each knowledge doc to its own file in output_dir.

        Uses the original file_name from the API. Returns list of written paths.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        docs_meta = self.list_docs(project_id)
        written: list[Path] = []
        for meta in track(docs_meta, description="Downloading docs…"):
            try:
                doc = self.get_doc(project_id, meta["uuid"])
            except Exception:
                doc = meta
            name = _ensure_md(doc.get("file_name", doc["uuid"]))
            dest = out / name
            dest.write_text(doc.get("content", ""), encoding="utf-8")
            written.append(dest)
        return written

    def sync_from_web(self, project_id: str, local_dir: str | Path) -> dict[str, str]:
        """
        Sync knowledge docs from the web project to a local directory.

        Web is the source of truth. Returns a dict mapping each filename to
        "created", "updated", or "unchanged".
        """
        out = Path(local_dir)
        out.mkdir(parents=True, exist_ok=True)

        docs_meta = self.list_docs(project_id)
        results: dict[str, str] = {}
        for meta in track(docs_meta, description="Syncing docs…"):
            try:
                doc = self.get_doc(project_id, meta["uuid"])
            except Exception:
                logger.warning("Failed to fetch doc %s, skipping", meta.get("uuid", "unknown"))
                continue
            name = _ensure_md(doc.get("file_name", doc["uuid"]))
            content = doc.get("content", "")
            dest = out / name
            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                if existing == content:
                    results[name] = "unchanged"
                    continue
                dest.write_text(content, encoding="utf-8")
                results[name] = "updated"
            else:
                dest.write_text(content, encoding="utf-8")
                results[name] = "created"
        return results
