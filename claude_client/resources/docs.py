from __future__ import annotations

from collections.abc import Sequence
from http import HTTPStatus
from pathlib import Path

from logger import get_logger
from rich.progress import track

from .._transport import BASE_URL, Transport
from ..exceptions import UploadError
from ..models import DocDict

logger = get_logger(__name__)


def _ensure_md(name: str) -> str:
    return name if name.endswith(".md") else f"{name}.md"


class DocsResource:
    """Knowledge docs within a project."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self, project_id: str) -> list[DocDict]:
        resp = self._t.get(f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}/docs")
        return resp.json()

    def get(self, project_id: str, doc_id: str) -> DocDict:
        """Fetch a single doc with its full content."""
        resp = self._t.get(
            f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}/docs/{doc_id}"
        )
        return resp.json()

    def rm(self, project_id: str, doc_id: str) -> None:
        self._t.delete(
            f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}/docs/{doc_id}"
        )

    def rm_all(self, project_id: str) -> int:
        """Delete every doc in a project. Returns the count deleted."""
        docs = self.list(project_id)
        for doc in docs:
            self.rm(project_id, doc["uuid"])
        return len(docs)

    # ----------------------------------------------------------------- push

    def push_content(self, project_id: str, content: str, name: str) -> DocDict:
        """
        Upsert doc content by name: replaces any existing doc with the same name.

        Always upserts (never creates a duplicate on re-push) — the doc/file
        distinction that mattered for the old upload/upsert split doesn't apply
        here, since content is already in hand either way.
        """
        existing = next((d for d in self.list(project_id) if d.get("file_name") == name), None)
        if existing is None:
            return self._create(project_id, content, name)
        self.rm(project_id, existing["uuid"])
        try:
            return self._create(project_id, content, name)
        except Exception as exc:
            raise UploadError(
                f"Push of '{name}' failed after deleting the existing doc; "
                f"the original has been removed from the project."
            ) from exc

    def push(self, project_id: str, file_path: str | Path, *, name: str | None = None) -> DocDict:
        """Upsert a doc from a local file."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        content = path.read_text(encoding="utf-8", errors="ignore")
        return self.push_content(project_id, content, name or path.name)

    def push_many(
        self, project_id: str, file_paths: Sequence[str | Path], *, name_prefix: str = ""
    ) -> dict[str, bool]:
        """Upsert multiple local files. Returns a dict mapping file names to success."""
        results: dict[str, bool] = {}
        for fp in file_paths:
            path = Path(fp)
            name = f"{name_prefix}{path.name}" if name_prefix else path.name
            try:
                self.push(project_id, path, name=name)
                results[name] = True
            except Exception as exc:
                results[name] = False
                logger.warning("Failed to push '%s': %s", name, exc)
        return results

    def _create(self, project_id: str, content: str, file_name: str) -> DocDict:
        url = f"{BASE_URL}/organizations/{self._t.org_id}/projects/{project_id}/docs"
        resp = self._t.post(url, {"file_name": file_name, "content": content})
        if resp.status_code != HTTPStatus.CREATED:
            raise UploadError(f"Upload of '{file_name}' failed: {resp.status_code} {resp.text}")
        return resp.json()

    # ----------------------------------------------------------------- pull

    def pull(
        self, project_id: str, output_dir: str | Path, *, force: bool = False
    ) -> dict[str, str]:
        """
        Pull knowledge docs from the web project into a local directory.

        Incremental by default: a doc whose content already matches the local file is
        left untouched and reported "unchanged". Pass force=True to always rewrite
        every file regardless of content (e.g. to recover from local edits).
        Web is always the source of truth — this never writes back to claude.ai.
        Returns a dict mapping each filename to "created", "updated", or "unchanged".
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        docs_meta = self.list(project_id)
        results: dict[str, str] = {}
        for meta in track(docs_meta, description="Pulling docs…"):
            try:
                doc = self.get(project_id, meta["uuid"])
            except Exception:
                logger.warning("Failed to fetch doc %s, skipping", meta.get("uuid", "unknown"))
                continue
            name = _ensure_md(doc.get("file_name", doc["uuid"]))
            content = doc.get("content", "")
            dest = out / name
            existed = dest.exists()
            if not force and existed and dest.read_text(encoding="utf-8") == content:
                results[name] = "unchanged"
                continue
            dest.write_text(content, encoding="utf-8")
            results[name] = "updated" if existed else "created"
        return results
