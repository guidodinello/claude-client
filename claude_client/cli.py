"""Command-line interface for claude-client."""

import argparse
import os
import re
import sys
from pathlib import Path

from logger import init_logging

from .client import ClaudeClient
from .exceptions import AuthError, NotFoundError, UploadError
from .migrate import migrate_project
from .render import conversation_to_markdown

_PROJECT_URL_RE = re.compile(r"([0-9a-f-]{36})/?$", re.IGNORECASE)


def _client(args: argparse.Namespace, project_id: str | None = None) -> ClaudeClient:
    """
    Build a client for this command.

    `org_id` defaults to the first chat-capable org on the account, which isn't
    necessarily the org that owns `project_id` on a multi-org account. When a
    project id is given and the account has more than one chat-capable org, resolve
    and pin the owning org so project-scoped requests don't 404 against the wrong
    org. Single-org accounts skip the extra lookup — there's no ambiguity to resolve.
    """
    token = getattr(args, "token", None) or os.getenv("CLAUDE_SESSION_TOKEN")
    if not token:
        sys.exit("Error: CLAUDE_SESSION_TOKEN not set. Pass --token or export the env var.")
    client = ClaudeClient(token)
    if project_id is None or len(client.chat_capable_org_ids()) <= 1:
        return client
    org_id = client.find_project_org(project_id)
    return client.scoped_client(org_id)


def _parse_project_id(value: str) -> str:
    """Accept either a bare project uuid or a full claude.ai project URL."""
    match = _PROJECT_URL_RE.search(value)
    if not match:
        sys.exit(f"Error: could not parse a project id from '{value}'.")
    return match.group(1)


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
    client = _client(args, args.project_id)
    docs = client.list_docs(args.project_id)
    if not docs:
        print("No docs found.")
        return
    for d in docs:
        print(f"{d['uuid']}  {d['file_name']}")


def _docs_get(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    doc = client.get_doc(args.project_id, args.doc_id)
    print(doc.get("content", ""))


def _docs_upload(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    path = Path(args.file)
    name = args.name or path.name
    try:
        doc = client.upload_file(args.project_id, path, name)
        print(f"Uploaded: {doc['file_name']}  ({doc['uuid']})")
    except (FileNotFoundError, UploadError) as exc:
        sys.exit(f"Error: {exc}")


def _docs_download(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    written = client.download_docs(args.project_id, args.output_dir)
    for p in written:
        print(f"  {p}")
    print(f"Downloaded {len(written)} file(s) to {args.output_dir}")


def _docs_sync(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    results = client.sync_from_web(args.project_id, args.local_dir)
    for name, status in results.items():
        print(f"  [{status}] {name}")
    print(f"Synced {len(results)} file(s).")


# ------------------------------------------------------------------- export


def _project_export(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    out = client.export_project_to_file(args.project_id, args.output_file)
    print(f"Exported to {out}")


def _project_sync(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    result = client.export_project_to_dir(args.project_id, args.output_dir)
    for name, status in result.docs.items():
        print(f"  [docs/{status}] {name}")
    for name, status in result.conversations.items():
        print(f"  [conversations/{status}] {name}")
    print(f"Synced to {result.path}/")


def _project_migrate(args: argparse.Namespace) -> None:
    source_token = args.source_token or os.getenv("CLAUDE_SESSION_TOKEN")
    if not source_token:
        sys.exit("Error: no source token. Pass --source-token or export CLAUDE_SESSION_TOKEN.")

    source_pid = _parse_project_id(args.source)
    dest_pid = _parse_project_id(args.dest)

    source = ClaudeClient(source_token)
    dest = ClaudeClient(args.dest_token)

    source_org = args.source_org or source.find_project_org(source_pid)
    dest_org = args.dest_org or dest.find_project_org(dest_pid)
    source = ClaudeClient(source_token, org_id=source_org)
    dest = ClaudeClient(args.dest_token, org_id=dest_org)

    counts = migrate_project(
        source,
        source_pid,
        dest,
        dest_pid,
        include_conversations=not args.no_conversations,
        include_memory=not args.no_memory,
    )
    print(
        f"Migrated {counts['docs']} doc(s), {counts['conversations']} conversation(s), "
        f"{counts['memory']} memory snapshot(s) to project {dest_pid}."
    )


# ---------------------------------------------------------------------- account


def _account_export(args: argparse.Namespace) -> None:
    client = _client(args)
    results = client.export_all_projects_to_dir(args.output_dir)
    for name, ok in results.items():
        print(f"  [{'ok' if ok else 'FAILED'}] {name}")
    succeeded = sum(ok for ok in results.values())
    print(f"Exported {succeeded}/{len(results)} project(s) to {args.output_dir}/")


# ----------------------------------------------------------------- conversations


def _conversations_list(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    convs = client.list_all_conversations(args.project_id)
    if not convs:
        print("No conversations found.")
        return
    for c in convs:
        print(f"{c['uuid']}  {c['name']}")


def _conversations_get(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    conv = client.get_conversation(args.project_id, args.conversation_id)
    content = conversation_to_markdown(conv)
    print(content)


def _conversations_download(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    written = client.export_conversations_to_files(args.project_id, args.output_dir)
    for p in written:
        print(f"  {p}")
    print(f"Downloaded {len(written)} file(s) to {args.output_dir}")


def _conversations_sync(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    results = client.sync_conversations_from_web(args.project_id, args.local_dir)
    for name, status in results.items():
        print(f"  [{status}] {name}")
    print(f"Synced {len(results)} file(s).")


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

    # ---- project ----
    project = sub.add_parser("project", help="Project-level operations")
    prsub = project.add_subparsers(dest="action", metavar="<action>")
    prsub.required = True

    pr_export = prsub.add_parser("export", help="Export full project to a single markdown file")
    pr_export.add_argument("project_id")
    pr_export.add_argument("output_file")
    pr_export.set_defaults(func=_project_export)

    pr_sync = prsub.add_parser(
        "sync", help="Sync project to a directory (project.md, docs/, conversations/)"
    )
    pr_sync.add_argument("project_id")
    pr_sync.add_argument("output_dir")
    pr_sync.set_defaults(func=_project_sync)

    pr_migrate = prsub.add_parser(
        "migrate", help="Migrate a project's docs/conversations/memory to another project"
    )
    pr_migrate.add_argument("source", help="Source project id or claude.ai project URL")
    pr_migrate.add_argument("dest", help="Destination project id or claude.ai project URL")
    pr_migrate.add_argument("--source-token", metavar="TOKEN", help="Source account session token")
    pr_migrate.add_argument(
        "--dest-token", metavar="TOKEN", required=True, help="Destination account session token"
    )
    pr_migrate.add_argument("--source-org", metavar="ORG_ID", help="Override source org id")
    pr_migrate.add_argument("--dest-org", metavar="ORG_ID", help="Override destination org id")
    pr_migrate.add_argument(
        "--no-conversations", action="store_true", help="Skip migrating conversations"
    )
    pr_migrate.add_argument("--no-memory", action="store_true", help="Skip migrating memory")
    pr_migrate.set_defaults(func=_project_migrate)

    # ---- account ----
    account = sub.add_parser("account", help="Account-wide operations (across all orgs)")
    asub = account.add_subparsers(dest="action", metavar="<action>")
    asub.required = True

    a_export = asub.add_parser(
        "export", help="Export every project across all chat-capable orgs to a directory"
    )
    a_export.add_argument("output_dir")
    a_export.set_defaults(func=_account_export)

    # ---- conversations ----
    conversations = sub.add_parser("conversations", help="Conversation operations")
    csub = conversations.add_subparsers(dest="action", metavar="<action>")
    csub.required = True

    c_list = csub.add_parser("list", help="List conversations in a project")
    c_list.add_argument("project_id")
    c_list.set_defaults(func=_conversations_list)

    c_get = csub.add_parser("get", help="Print a conversation as markdown")
    c_get.add_argument("project_id")
    c_get.add_argument("conversation_id")
    c_get.set_defaults(func=_conversations_get)

    c_download = csub.add_parser("download", help="Download all conversations to a local folder")
    c_download.add_argument("project_id")
    c_download.add_argument("output_dir")
    c_download.set_defaults(func=_conversations_download)

    c_sync = csub.add_parser("sync", help="Sync web conversations → local folder (web wins)")
    c_sync.add_argument("project_id")
    c_sync.add_argument("local_dir")
    c_sync.set_defaults(func=_conversations_sync)

    return root


def main() -> None:
    init_logging()
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
