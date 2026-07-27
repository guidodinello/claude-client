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
    if project_id is None:
        return client

    org_ids = client.orgs.chat_capable_ids()
    if len(org_ids) <= 1:
        return client
    org_id = client.projects.find_org(project_id)
    return client.scoped(org_id)


def _parse_project_id(value: str) -> str:
    """Accept either a bare project uuid or a full claude.ai project URL."""
    match = _PROJECT_URL_RE.search(value)
    if not match:
        sys.exit(f"Error: could not parse a project id from '{value}'.")
    return match.group(1)


def _print_pull_results(results: dict[str, str], label: str) -> None:
    for name, status in results.items():
        print(f"  [{label}/{status}] {name}")


# ------------------------------------------------------------------- project


def _project_list(args: argparse.Namespace) -> None:
    client = _client(args)
    projects = client.projects.list()
    if not projects:
        print("No projects found.")
        return
    for org_id, p in projects:
        print(f"{org_id}  {p['uuid']}  {p['name']}")


def _project_show(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    project = client.projects.get(args.project_id)
    print(f"Name: {project.get('name', '')}")
    print(f"Description: {project.get('description', '')}")
    print(f"Instructions: {project.get('prompt_template', '')}")


def _project_export(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    markdown = client.projects.export(args.project_id)
    out = Path(args.output_file)
    if out.is_dir():
        out = out / f"{args.project_id}.md"
    out.write_text(markdown, encoding="utf-8")
    print(f"Exported to {out}")


def _project_pull(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    result = client.projects.pull(args.project_id, args.output_dir, force=args.force)
    _print_pull_results(result.docs, "docs")
    _print_pull_results(result.conversations, "conversations")
    print(f"Pulled to {result.path}/")


def _project_migrate(args: argparse.Namespace) -> None:
    source_token = args.source_token or os.getenv("CLAUDE_SESSION_TOKEN")
    if not source_token:
        sys.exit("Error: no source token. Pass --source-token or export CLAUDE_SESSION_TOKEN.")

    source_pid = _parse_project_id(args.source)
    dest_pid = _parse_project_id(args.dest)

    source = ClaudeClient(source_token)
    dest = ClaudeClient(args.dest_token)

    source_org = args.source_org or source.projects.find_org(source_pid)
    dest_org = args.dest_org or dest.projects.find_org(dest_pid)
    source = source.scoped(source_org)
    dest = dest.scoped(dest_org)

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


# ---------------------------------------------------------------------- docs


def _docs_list(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    docs = client.docs.list(args.project_id)
    if not docs:
        print("No docs found.")
        return
    for d in docs:
        print(f"{d['uuid']}  {d['file_name']}")


def _docs_get(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    doc = client.docs.get(args.project_id, args.doc_id)
    print(doc.get("content", ""))


def _docs_push(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    try:
        doc = client.docs.push(args.project_id, args.file, name=args.name)
        print(f"Pushed: {doc['file_name']}  ({doc['uuid']})")
    except (FileNotFoundError, UploadError) as exc:
        sys.exit(f"Error: {exc}")


def _docs_rm(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    if args.all:
        count = client.docs.rm_all(args.project_id)
        print(f"Removed {count} doc(s)")
        return
    if not args.doc_id:
        sys.exit("Error: doc_id is required unless --all is given.")
    client.docs.rm(args.project_id, args.doc_id)
    print(f"Removed doc {args.doc_id}")


def _docs_pull(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    results = client.docs.pull(args.project_id, args.local_dir, force=args.force)
    for name, status in results.items():
        print(f"  [{status}] {name}")
    print(f"Pulled {len(results)} file(s).")


# --------------------------------------------------------------------- account


def _account_pull(args: argparse.Namespace) -> None:
    client = _client(args)
    results = client.projects.pull_all(args.output_dir, force=args.force)
    for name, ok in results.items():
        print(f"  [{'ok' if ok else 'FAILED'}] {name}")
    succeeded = sum(ok for ok in results.values())
    print(f"Pulled {succeeded}/{len(results)} project(s) to {args.output_dir}/")


# --------------------------------------------------------------- conversations


def _conversations_list(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    convs = client.conversations.list(args.project_id)
    if not convs:
        print("No conversations found.")
        return
    for c in convs:
        print(f"{c['uuid']}  {c['name']}")


def _conversations_get(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    conv = client.conversations.get(args.conversation_id)
    content = conversation_to_markdown(conv)
    print(content)


def _conversations_pull(args: argparse.Namespace) -> None:
    client = _client(args, args.project_id)
    results = client.conversations.pull(args.project_id, args.local_dir, force=args.force)
    for name, status in results.items():
        print(f"  [{status}] {name}")
    print(f"Pulled {len(results)} file(s).")


# ---------------------------------------------------------------- arg parsing


def _add_force_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite every file unconditionally, instead of skipping unchanged ones",
    )


def _build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="claude-client",
        description="Interact with Claude.ai projects via the unofficial web API.",
    )
    root.add_argument("--token", metavar="TOKEN", help="Override CLAUDE_SESSION_TOKEN")

    sub = root.add_subparsers(dest="group", metavar="<command>")
    sub.required = True

    # ---- project ----
    project = sub.add_parser("project", help="Project operations")
    prsub = project.add_subparsers(dest="action", metavar="<action>")
    prsub.required = True

    pr_list = prsub.add_parser("list", help="List projects across every chat-capable org")
    pr_list.set_defaults(func=_project_list)

    pr_show = prsub.add_parser("show", help="Show a project's metadata")
    pr_show.add_argument("project_id")
    pr_show.set_defaults(func=_project_show)

    pr_export = prsub.add_parser("export", help="Export full project to a single markdown file")
    pr_export.add_argument("project_id")
    pr_export.add_argument("output_file")
    pr_export.set_defaults(func=_project_export)

    pr_pull = prsub.add_parser(
        "pull", help="Pull project to a directory (project.md, docs/, conversations/)"
    )
    pr_pull.add_argument("project_id")
    pr_pull.add_argument("output_dir")
    _add_force_flag(pr_pull)
    pr_pull.set_defaults(func=_project_pull)

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

    d_push = dsub.add_parser("push", help="Push a file to a project (upserts by name)")
    d_push.add_argument("project_id")
    d_push.add_argument("file")
    d_push.add_argument("--name", metavar="NAME", help="Override the file name in Claude")
    d_push.set_defaults(func=_docs_push)

    d_rm = dsub.add_parser("rm", help="Remove a doc from a project")
    d_rm.add_argument("project_id")
    d_rm.add_argument("doc_id", nargs="?", help="Omit when using --all")
    d_rm.add_argument("--all", action="store_true", help="Remove every doc in the project")
    d_rm.set_defaults(func=_docs_rm)

    d_pull = dsub.add_parser("pull", help="Pull web docs → local folder (web wins)")
    d_pull.add_argument("project_id")
    d_pull.add_argument("local_dir")
    _add_force_flag(d_pull)
    d_pull.set_defaults(func=_docs_pull)

    # ---- account ----
    account = sub.add_parser("account", help="Account-wide operations (across all orgs)")
    asub = account.add_subparsers(dest="action", metavar="<action>")
    asub.required = True

    a_pull = asub.add_parser(
        "pull", help="Pull every project across all chat-capable orgs to a directory"
    )
    a_pull.add_argument("output_dir")
    _add_force_flag(a_pull)
    a_pull.set_defaults(func=_account_pull)

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

    c_pull = csub.add_parser("pull", help="Pull web conversations → local folder (web wins)")
    c_pull.add_argument("project_id")
    c_pull.add_argument("local_dir")
    _add_force_flag(c_pull)
    c_pull.set_defaults(func=_conversations_pull)

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
