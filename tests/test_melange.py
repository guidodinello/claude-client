"""Tests for melange memory (MemoryResource.list_entries/read_entry/pull) — HTTP mocked.

The HTTP layer is replaced by a small fake melange server (`FakeMelange`) that answers
`melange/list` and `melange/read` per org, so tests describe remote state rather than an
ordered sequence of responses.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import HTTPError, RequestException

from claude_client import ClaudeClient
from claude_client._manifest import MANIFEST_NAME
from claude_client.models import ProjectExport
from claude_client.render import render_project_metadata
from claude_client.resources.projects import _memory_is_frozen

ORG_ID = "org-uuid"
OTHER_ORG_ID = "other-org-uuid"
TOKEN = "sk-ant-sid01-test"
PROJECT_PATH = "/projects/019a10b3/topics/hardware.md"

_URL_RE = re.compile(r"/organizations/([^/]+)/melange/(list|read)$")


def _response(json_data, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.headers = {}
    r.text = ""
    if status_code >= 400:
        r.raise_for_status.side_effect = HTTPError(f"HTTP Error {status_code}", 0, r)
    return r


def _orgs(*org_ids: str) -> list[dict]:
    return [{"uuid": org_id, "capabilities": ["chat"], "name": org_id} for org_id in org_ids]


class FakeMelange:
    """Per-org melange state: {org_id: {path: (version, content)}}, or None for a 404 org."""

    def __init__(self, orgs: dict[str, dict[str, tuple[str, str]] | None]) -> None:
        self.orgs = orgs
        self.reads: list[tuple[str, str]] = []
        self.failing_reads: set[str] = set()
        self.list_status: dict[str, int] = {}

    def post(self, url: str, *, data: str, **_kwargs) -> MagicMock:
        match = _URL_RE.search(url)
        assert match, f"unexpected POST {url}"
        org_id, verb = match.groups()
        if org_id in self.list_status:
            return _response({}, self.list_status[org_id])
        files = self.orgs[org_id]
        if files is None:
            return _response({"error": "not found"}, 404)
        if verb == "list":
            return _response(
                {
                    "data": [
                        {"path": path, "version": version, "updated_at": "2026-10-26"}
                        for path, (version, _content) in files.items()
                    ],
                    "categories": [],
                }
            )
        path = json.loads(data)["path"]
        self.reads.append((org_id, path))
        if path in self.failing_reads:
            raise RequestException("connection reset")
        version, content = files[path]
        return _response({"path": path, "version": version, "content": content})


@pytest.fixture()
def server():
    """Patch the transport's HTTP layer with a fake melange server (one org by default)."""
    fake = FakeMelange({ORG_ID: {}})
    with patch("claude_client._transport.requests") as mock_req:
        mock_req.get.side_effect = lambda url, **_kw: _response(_orgs(*fake.orgs))
        mock_req.post.side_effect = fake.post
        yield fake


@pytest.fixture()
def client():
    return ClaudeClient(TOKEN)


def _tree(root: Path) -> dict[str, str]:
    """Every pulled file under `root` (manifests excluded), keyed by posix relpath."""
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }


# ----------------------------------------------------------------- list / read


def test_list_entries_returns_entry_metadata(server, client):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me")}

    entries = client.scoped(ORG_ID).memory.list_entries()

    assert [(e["path"], e["version"]) for e in entries] == [("/profile.md", "v1")]


def test_read_entry_returns_raw_content(server, client):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "---\nname: Profile\n---\nbody")}

    file = client.scoped(ORG_ID).memory.read_entry("/profile.md")

    assert file["content"] == "---\nname: Profile\n---\nbody"


# ------------------------------------------------------------------------ pull


def test_pull_mirrors_tree_verbatim_under_org_dir(server, client, tmp_path):
    server.orgs[ORG_ID] = {
        "/profile.md": ("v1", "---\nname: Profile\n---\nme"),
        PROJECT_PATH: ("v1", "hardware notes"),
    }

    results = client.memory.pull(tmp_path)

    assert _tree(tmp_path) == {
        f"{ORG_ID}/profile.md": "---\nname: Profile\n---\nme",
        f"{ORG_ID}/projects/019a10b3/topics/hardware.md": "hardware notes",
    }
    assert results == {f"{ORG_ID}/profile.md": "created", f"{ORG_ID}{PROJECT_PATH}": "created"}


def test_pull_again_with_unchanged_versions_reads_nothing(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me"), PROJECT_PATH: ("v1", "hw")}
    client.memory.pull(tmp_path)
    server.reads.clear()

    results = client.memory.pull(tmp_path)

    assert server.reads == []
    assert set(results.values()) == {"unchanged"}


def test_pull_rereads_only_entries_whose_version_changed(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me"), PROJECT_PATH: ("v1", "hw")}
    client.memory.pull(tmp_path)
    server.reads.clear()
    server.orgs[ORG_ID][PROJECT_PATH] = ("v2", "hw, now with a GPU")

    results = client.memory.pull(tmp_path)

    assert server.reads == [(ORG_ID, PROJECT_PATH)]
    assert results[f"{ORG_ID}{PROJECT_PATH}"] == "updated"
    assert _tree(tmp_path)[f"{ORG_ID}/projects/019a10b3/topics/hardware.md"] == (
        "hw, now with a GPU"
    )


def test_pull_refetches_a_locally_deleted_file(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me")}
    client.memory.pull(tmp_path)
    (tmp_path / ORG_ID / "profile.md").unlink()

    results = client.memory.pull(tmp_path)

    assert results == {f"{ORG_ID}/profile.md": "created"}
    assert (tmp_path / ORG_ID / "profile.md").read_text() == "me"


def test_pull_force_rereads_unchanged_entries(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me")}
    client.memory.pull(tmp_path)
    server.reads.clear()

    client.memory.pull(tmp_path, force=True)

    assert server.reads == [(ORG_ID, "/profile.md")]


def test_pull_prune_deletes_removed_paths_and_empty_dirs(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me"), PROJECT_PATH: ("v1", "hw")}
    client.memory.pull(tmp_path)
    del server.orgs[ORG_ID][PROJECT_PATH]

    results = client.memory.pull(tmp_path, prune=True)

    assert results[f"{ORG_ID}{PROJECT_PATH}"] == "deleted"
    assert _tree(tmp_path) == {f"{ORG_ID}/profile.md": "me"}
    assert not (tmp_path / ORG_ID / "projects").exists()


def test_pull_without_prune_keeps_removed_paths_for_a_later_prune(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me"), PROJECT_PATH: ("v1", "hw")}
    client.memory.pull(tmp_path)
    del server.orgs[ORG_ID][PROJECT_PATH]

    client.memory.pull(tmp_path)
    assert f"{ORG_ID}/projects/019a10b3/topics/hardware.md" in _tree(tmp_path)

    client.memory.pull(tmp_path, prune=True)
    assert f"{ORG_ID}/projects/019a10b3/topics/hardware.md" not in _tree(tmp_path)


@pytest.mark.parametrize(
    "bad_path",
    ["/../escape.md", "/a/../../escape.md", "relative.md", "/a//b.md", "/a\\..\\b.md", "/./x.md"],
)
def test_pull_rejects_unsafe_paths_without_writing(server, client, tmp_path, bad_path):
    server.orgs[ORG_ID] = {bad_path: ("v1", "pwned"), "/profile.md": ("v1", "me")}
    out = tmp_path / "out"

    results = client.memory.pull(out)

    assert results[f"{ORG_ID}{bad_path}"] == "rejected"
    assert (ORG_ID, bad_path) not in server.reads
    assert _tree(tmp_path) == {f"out/{ORG_ID}/profile.md": "me"}


def test_pull_rejects_path_escaping_through_a_symlink(server, client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    org_dir = tmp_path / "out" / ORG_ID
    org_dir.mkdir(parents=True)
    (org_dir / "link").symlink_to(outside)
    server.orgs[ORG_ID] = {"/link/x.md": ("v1", "pwned")}

    results = client.memory.pull(tmp_path / "out")

    assert results == {f"{ORG_ID}/link/x.md": "rejected"}
    assert list(outside.iterdir()) == []


def test_pull_failed_read_keeps_previous_copy_and_entry(server, client, tmp_path):
    server.orgs[ORG_ID] = {"/profile.md": ("v1", "me")}
    client.memory.pull(tmp_path)
    server.orgs[ORG_ID]["/profile.md"] = ("v2", "new me")
    server.failing_reads.add("/profile.md")

    results = client.memory.pull(tmp_path, prune=True)

    assert results == {f"{ORG_ID}/profile.md": "failed"}
    assert (tmp_path / ORG_ID / "profile.md").read_text() == "me"
    # Still recorded at the old version, so the next run retries the read.
    server.failing_reads.clear()
    assert client.memory.pull(tmp_path) == {f"{ORG_ID}/profile.md": "updated"}


def test_pull_keeps_orgs_with_the_same_path_apart(server, client, tmp_path):
    server.orgs = {
        ORG_ID: {"/profile.md": ("v1", "work me")},
        OTHER_ORG_ID: {"/profile.md": ("v1", "personal me")},
    }

    client.memory.pull(tmp_path)

    assert _tree(tmp_path) == {
        f"{ORG_ID}/profile.md": "work me",
        f"{OTHER_ORG_ID}/profile.md": "personal me",
    }


def test_pull_skips_org_without_melange(server, client, tmp_path):
    server.orgs = {ORG_ID: None, OTHER_ORG_ID: {"/profile.md": ("v1", "me")}}

    results = client.memory.pull(tmp_path)

    assert results == {f"{OTHER_ORG_ID}/profile.md": "created"}
    assert not (tmp_path / ORG_ID).exists()


def test_pull_propagates_non_404_listing_errors(server, client, tmp_path):
    server.orgs = {ORG_ID: {"/profile.md": ("v1", "me")}}
    server.list_status[ORG_ID] = 500

    with pytest.raises(HTTPError):
        client.memory.pull(tmp_path)


# ------------------------------------------------------- legacy memory label


@pytest.mark.parametrize(
    ("frozen", "heading"),
    [(False, "## Memory\n"), (True, "## Memory (frozen pre-migration snapshot)\n")],
)
def test_project_metadata_labels_frozen_legacy_memory(frozen, heading):
    export = ProjectExport(
        uuid="p", name="P", description="", instructions="", memory="blob", memory_frozen=frozen
    )

    assert heading in render_project_metadata(export)


@pytest.mark.parametrize(
    ("memory_data", "frozen"),
    [
        ({"memory": "m", "controls": [], "updated_at": "x"}, False),
        ({"memory": "m", "controls": [], "updated_at": "x", "project_import": None}, False),
        (
            {
                "memory": "m",
                "controls": [],
                "updated_at": "x",
                "project_import": {"status": "migrated"},
            },
            True,
        ),
    ],
)
def test_memory_is_frozen_handles_absent_null_and_migrated_import(memory_data, frozen):
    assert _memory_is_frozen(memory_data) is frozen


def test_project_pull_marks_migrated_memory_as_frozen(tmp_path):
    migrated = {
        "memory": "old blob",
        "controls": [],
        "updated_at": "2026-10-08",
        "project_import": {
            "status": "migrated",
            "migrated_at": "2026-09-24",
            "withheld_reason": None,
        },
        "project_memory_mode": "melange",
    }
    with patch("claude_client._transport.requests") as mock_req:
        mock_req.get.side_effect = lambda url, **_kw: _response(
            migrated
            if "/memory?" in url
            else {"data": [], "pagination": {"has_more": False}}
            if "conversations" in url
            else []
            if url.endswith("/docs")
            else {"uuid": "p", "name": "P", "description": "", "prompt_template": ""}
        )
        ClaudeClient(TOKEN, org_id=ORG_ID).projects.pull("p", tmp_path)

    assert (
        "## Memory (frozen pre-migration snapshot)\n\nold blob"
        in (tmp_path / "project.md").read_text()
    )
