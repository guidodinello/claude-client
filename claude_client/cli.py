"""Command-line interface for claude-client."""

import argparse
import os
import sys
from pathlib import Path

from .client import ClaudeClient
from .exceptions import AuthError, NotFoundError, UploadError


def _client(args: argparse.Namespace) -> ClaudeClient:
    token = getattr(args, "token", None) or os.getenv("CLAUDE_SESSION_TOKEN")
    if not token:
        sys.exit("Error: CLAUDE_SESSION_TOKEN not set. Pass --token or export the env var.")
    return ClaudeClient(token)


# ------------------------------------------------------------------ projects

def _projects_list(args: argparse.Namespace) -> None:
    client = _client(args)
    projects = client.list_projects()
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        print(f"{p['uuid']}  {p['name']}")


# ---------------------------------------------------------------------- docs

def _docs_list(args: argparse.Namespace) -> None:
    client = _client(args)
    docs = client.list_docs(args.project_id)
    if not docs:
        print("No docs found.")
        return
    for d in docs:
        print(f"{d['uuid']}  {d['file_name']}")


def _docs_get(args: argparse.Namespace) -> None:
    client = _client(args)
    doc = client.get_doc(args.project_id, args.doc_id)
    print(doc.get("content", ""))


def _docs_upload(args: argparse.Namespace) -> None:
    client = _client(args)
    path = Path(args.file)
    name = args.name or path.name
    try:
        doc = client.upload_file(args.project_id, path, name)
        print(f"Uploaded: {doc['file_name']}  ({doc['uuid']})")
    except (FileNotFoundError, UploadError) as exc:
        sys.exit(f"Error: {exc}")


def _docs_download(args: argparse.Namespace) -> None:
    client = _client(args)
    written = client.download_docs(args.project_id, args.output_dir)
    for p in written:
        print(f"  {p}")
    print(f"Downloaded {len(written)} file(s) to {args.output_dir}")


def _docs_sync(args: argparse.Namespace) -> None:
    client = _client(args)
    results = client.sync_from_web(args.project_id, args.local_dir)
    for name, status in results.items():
        print(f"  [{status}] {name}")
    print(f"Synced {len(results)} file(s).")


# ------------------------------------------------------------------- export

def _export(args: argparse.Namespace) -> None:
    client = _client(args)
    out = client.export_project_to_file(args.project_id, args.output_file)
    print(f"Exported to {out}")


# ---------------------------------------------------------------- arg parsing

def _build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="claude-client",
        description="Interact with Claude.ai projects via the unofficial web API.",
    )
    root.add_argument("--token", metavar="TOKEN", help="Override CLAUDE_SESSION_TOKEN")

    sub = root.add_subparsers(dest="group", metavar="<command>")
    sub.required = True

    # ---- projects ----
    projects = sub.add_parser("projects", help="Project operations")
    psub = projects.add_subparsers(dest="action", metavar="<action>")
    psub.required = True
    p_list = psub.add_parser("list", help="List all projects")
    p_list.set_defaults(func=_projects_list)

    # ---- docs ----
    docs = sub.add_parser("docs", help="Knowledge doc operations")
    dsub = docs.add_subparsers(dest="action", metavar="<action>")
    dsub.required = True

    d_list = dsub.add_parser("list", help="List docs in a project")
    d_list.add_argument("project_id")
    d_list.set_defaults(func=_docs_list)

    d_get = dsub.add_parser("get", help="Print a doc's content")
    d_get.add_argument("project_id")
    d_get.add_argument("doc_id")
    d_get.set_defaults(func=_docs_get)

    d_upload = dsub.add_parser("upload", help="Upload a file to a project")
    d_upload.add_argument("project_id")
    d_upload.add_argument("file")
    d_upload.add_argument("--name", metavar="NAME", help="Override the file name in Claude")
    d_upload.set_defaults(func=_docs_upload)

    d_download = dsub.add_parser("download", help="Download all docs to a local folder")
    d_download.add_argument("project_id")
    d_download.add_argument("output_dir")
    d_download.set_defaults(func=_docs_download)

    d_sync = dsub.add_parser("sync", help="Sync web docs → local folder (web wins)")
    d_sync.add_argument("project_id")
    d_sync.add_argument("local_dir")
    d_sync.set_defaults(func=_docs_sync)

    # ---- export ----
    export = sub.add_parser("export", help="Export full project to a single markdown file")
    export.add_argument("project_id")
    export.add_argument("output_file")
    export.set_defaults(func=_export)

    return root


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except AuthError as exc:
        sys.exit(f"Auth error: {exc}")
    except NotFoundError as exc:
        sys.exit(f"Not found: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
