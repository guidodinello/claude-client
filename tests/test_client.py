"""Unit tests for ClaudeClient — HTTP layer mocked via unittest.mock."""

from unittest.mock import MagicMock, patch

import pytest

from claude_client import AuthError, ClaudeClient, NotFoundError, UploadError
from claude_client.render import conversation_to_markdown, slugify

ORG_ID = "org-uuid"
PROJECT_ID = "proj-uuid"
DOC_UUID = "doc-uuid"
TOKEN = "sk-ant-sid01-test"

ORGS_RESPONSE = [{"uuid": ORG_ID, "capabilities": ["chat"], "name": "Test Org"}]
PROJECTS_RESPONSE = [
    {"uuid": PROJECT_ID, "name": "My Project", "description": "", "prompt_template": ""}
]
PROJECT_RESPONSE = {
    "uuid": PROJECT_ID,
    "name": "My Project",
    "description": "A test project",
    "prompt_template": "Be helpful.",
}
MEMORY_RESPONSE = {"memory": "Auto-memory content", "controls": [], "updated_at": "2024-01-01"}
DOC_META = {"uuid": DOC_UUID, "file_name": "notes.md", "created_at": "2024-01-01"}
DOC_FULL = {**DOC_META, "content": "hello world"}


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture()
def client():
    return ClaudeClient(TOKEN)


@patch("claude_client.client.requests")
def test_org_id(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    assert client.org_id == ORG_ID


@patch("claude_client.client.requests")
def test_list_all_projects_across_multiple_orgs(mock_req, client):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    other_project = {
        "uuid": "other-proj",
        "name": "Other Project",
        "description": "",
        "prompt_template": "",
    }
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # list_organizations
        _mock_response(PROJECTS_RESPONSE),  # projects in first org
        _mock_response([other_project]),  # projects in second org
    ]

    results = client.list_all_projects()

    assert results == [
        (ORG_ID, PROJECTS_RESPONSE[0]),
        ("other-org", other_project),
    ]


@patch("claude_client.client.requests")
def test_list_all_projects_skips_non_chat_orgs(mock_req, client):
    non_chat_org = {"uuid": "no-chat-org", "capabilities": ["other"], "name": "No Chat Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, non_chat_org]),  # list_organizations
        _mock_response(PROJECTS_RESPONSE),  # only the chat-capable org gets queried
    ]

    results = client.list_all_projects()

    assert [org_id for org_id, _ in results] == [ORG_ID]


@patch("claude_client.client.requests")
def test_list_projects(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECTS_RESPONSE),
    ]
    projects = client.list_projects()
    assert len(projects) == 1
    assert projects[0]["name"] == "My Project"


@patch("claude_client.client.requests")
def test_find_project(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECTS_RESPONSE),
    ]
    p = client.find_project("My Project")
    assert p["uuid"] == PROJECT_ID


@patch("claude_client.client.requests")
def test_find_project_not_found(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECTS_RESPONSE),
    ]
    with pytest.raises(NotFoundError):
        client.find_project("Nonexistent")


@patch("claude_client.client.requests")
def test_get_doc(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(DOC_FULL),
    ]
    doc = client.get_doc(PROJECT_ID, DOC_UUID)
    assert doc["content"] == "hello world"


@patch("claude_client.client.requests")
def test_upload_content_success(mock_req, client):
    created_doc = {**DOC_FULL}
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    mock_req.post.return_value = _mock_response(created_doc, status_code=201)

    doc = client.upload_content(PROJECT_ID, "hello world", "notes.md")
    assert doc["uuid"] == DOC_UUID
    call_kwargs = mock_req.post.call_args
    import json

    payload = json.loads(call_kwargs.kwargs["data"])
    assert payload["file_name"] == "notes.md"
    assert payload["content"] == "hello world"


@patch("claude_client.client.requests")
def test_upload_content_error(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    mock_req.post.return_value = _mock_response({}, status_code=500)

    with pytest.raises(UploadError):
        client.upload_content(PROJECT_ID, "hello", "notes.md")


@patch("claude_client.client.requests")
def test_delete_doc(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    mock_req.delete.return_value = _mock_response(None, status_code=204)

    client.delete_doc(PROJECT_ID, DOC_UUID)
    assert mock_req.delete.called


@patch("claude_client.client.requests")
def test_upsert_content_replaces_existing(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response([DOC_META]),  # list_docs
        _mock_response(ORGS_RESPONSE),  # org_id cached — skip (cached_property)
    ]
    mock_req.delete.return_value = _mock_response(None, status_code=204)
    mock_req.post.return_value = _mock_response({**DOC_FULL}, status_code=201)

    client.upsert_content(PROJECT_ID, "new content", "notes.md")

    assert mock_req.delete.called
    assert mock_req.post.called


@patch("claude_client.client.requests")
def test_upsert_content_wraps_upload_failure_after_delete(mock_req, client):
    """If the re-upload fails after an existing doc was deleted, the error must
    say so — the caller needs to know the original is gone."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response([DOC_META]),  # list_docs
    ]
    mock_req.delete.return_value = _mock_response(None, status_code=204)
    mock_req.post.return_value = _mock_response({}, status_code=500)

    with pytest.raises(UploadError, match="original has been removed"):
        client.upsert_content(PROJECT_ID, "new content", "notes.md")

    assert mock_req.delete.called
    assert mock_req.post.called


@patch("claude_client.client.requests")
def test_upsert_content_no_existing_doc_does_not_wrap_error(mock_req, client):
    """When there's no existing doc to delete, an upload failure should
    propagate as the plain UploadError, without the delete-related message."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response([]),  # list_docs — nothing matches
    ]
    mock_req.post.return_value = _mock_response({}, status_code=500)

    with pytest.raises(UploadError) as exc_info:
        client.upsert_content(PROJECT_ID, "new content", "notes.md")

    assert "original has been removed" not in str(exc_info.value)
    assert not mock_req.delete.called


@patch("claude_client.client.requests")
def test_check_auth_raises_on_401(mock_req, client):
    r = _mock_response({}, status_code=401)
    mock_req.get.return_value = r

    with pytest.raises(AuthError):
        _ = client.org_id


@patch("claude_client.client.requests")
def test_check_auth_raises_on_403(mock_req, client):
    r = _mock_response({}, status_code=403)
    mock_req.get.return_value = r

    with pytest.raises(AuthError):
        _ = client.org_id


@patch("claude_client.client.requests")
def test_download_docs(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response([DOC_META]),  # list_docs
        _mock_response(DOC_FULL),  # get_doc
    ]

    written = client.download_docs(PROJECT_ID, tmp_path)

    assert len(written) == 1
    assert written[0] == tmp_path / "notes.md"
    assert written[0].read_text() == "hello world"


@patch("claude_client.client.requests")
def test_sync_from_web_created(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.sync_from_web(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "created"
    assert (tmp_path / "notes.md").read_text() == "hello world"


@patch("claude_client.client.requests")
def test_sync_from_web_unchanged(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("hello world")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.sync_from_web(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "unchanged"


@patch("claude_client.client.requests")
def test_sync_from_web_updated(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("old content")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.sync_from_web(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "updated"
    assert (tmp_path / "notes.md").read_text() == "hello world"


@patch("claude_client.client.requests")
def test_sync_from_web_skips_doc_on_fetch_failure(mock_req, client, tmp_path):
    """A failed get_doc must be skipped, not written as empty content."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        Exception("boom"),  # get_doc fails
    ]

    results = client.sync_from_web(PROJECT_ID, tmp_path)

    assert results == {}
    assert not (tmp_path / "notes.md").exists()


CONV_UUID = "conv-uuid"
CONVERSATION_META = [
    {
        "uuid": CONV_UUID,
        "name": "Test Chat",
        "summary": "",
        "model": "claude-sonnet-4-20250514",
        "created_at": "2024-01-01",
        "updated_at": "2024-01-02",
        "is_starred": False,
        "is_temporary": False,
        "project_uuid": PROJECT_ID,
        "current_leaf_message_uuid": "leaf-uuid",
    }
]

CONV_PAGE_RESPONSE = {
    "data": CONVERSATION_META,
    "pagination": {"total": 1, "limit": 30, "offset": 0, "has_more": False},
}
CONVERSATION_DETAIL = {
    "uuid": CONV_UUID,
    "name": "Test Chat",
    "summary": "",
    "model": "claude-sonnet-4-20250514",
    "created_at": "2024-01-01T10:00:00Z",
    "updated_at": "2024-01-02T11:00:00Z",
    "current_leaf_message_uuid": "leaf-uuid",
    "chat_messages": [
        {
            "uuid": "root-uuid",
            "sender": "human",
            "content": [{"type": "text", "text": "Hello"}],
            "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
            "index": 0,
            "created_at": "2024-01-01T10:00:00Z",
            "updated_at": "2024-01-01T10:00:00Z",
        },
        {
            "uuid": "leaf-uuid",
            "sender": "assistant",
            "content": [{"type": "text", "text": "Hi there!"}],
            "parent_message_uuid": "root-uuid",
            "index": 1,
            "created_at": "2024-01-01T10:01:00Z",
            "updated_at": "2024-01-01T10:01:00Z",
        },
    ],
}


@patch("claude_client.client.requests")
def test_get_conversation(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    conv = client.get_conversation(PROJECT_ID, CONV_UUID)

    assert conv["uuid"] == CONV_UUID
    assert len(conv["chat_messages"]) == 2


@patch("claude_client.client.requests")
def test_list_all_conversations(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
    ]

    convs = client.list_all_conversations(PROJECT_ID)

    assert len(convs) == 1
    assert convs[0]["name"] == "Test Chat"


def test_slugify_basic():
    assert slugify("My Cool Project!") == "my-cool-project"


def test_slugify_empty_falls_back():
    assert slugify("!!!") == "untitled"
    assert slugify("!!!", fallback="conversation") == "conversation"


def test_conversation_to_markdown():
    md = conversation_to_markdown(CONVERSATION_DETAIL)

    assert "### Test Chat" in md
    assert "claude-sonnet" in md
    assert "Human" in md
    assert "Claude" in md
    assert "Hello" in md
    assert "Hi there!" in md


def test_conversation_to_markdown_renders_tool_content():
    conv = {
        "uuid": "tool-conv",
        "name": "Tool Chat",
        "model": "claude-sonnet-4-20250514",
        "created_at": "2024-01-01T10:00:00Z",
        "updated_at": "2024-01-01T10:00:00Z",
        "current_leaf_message_uuid": "msg-3",
        "chat_messages": [
            {
                "uuid": "root",
                "sender": "human",
                "content": [{"type": "text", "text": "search the web"}],
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "index": 0,
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z",
            },
            {
                "uuid": "msg-2",
                "sender": "assistant",
                "content": [
                    {"type": "tool_use", "name": "tavily", "input": {"query": "weather"}},
                    {"type": "text", "text": "Let me look that up"},
                ],
                "parent_message_uuid": "root",
                "index": 1,
                "created_at": "2024-01-01T10:01:00Z",
                "updated_at": "2024-01-01T10:01:00Z",
            },
            {
                "uuid": "msg-3",
                "sender": "assistant",
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "sunny 72°F"}]},
                    {"type": "text", "text": "It's sunny and 72°F."},
                ],
                "parent_message_uuid": "msg-2",
                "index": 2,
                "created_at": "2024-01-01T10:02:00Z",
                "updated_at": "2024-01-01T10:02:00Z",
            },
        ],
    }

    md = conversation_to_markdown(conv)

    assert "[Tool: tavily]" in md
    assert "{'query': 'weather'}" in md
    assert "[Result]" in md
    assert "sunny 72°F" in md
    assert "It's sunny and 72°F." in md


@patch("claude_client.client.requests")
def test_sync_conversations_from_web_created(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.sync_conversations_from_web(PROJECT_ID, tmp_path)

    assert len(results) == 1
    assert "test-chat-conv-uui.md" in results
    assert results["test-chat-conv-uui.md"] == "created"


@patch("claude_client.client.requests")
def test_sync_conversations_from_web_unchanged(mock_req, client, tmp_path):
    md = conversation_to_markdown(CONVERSATION_DETAIL)
    (tmp_path / "test-chat-conv-uui.md").write_text(md)

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.sync_conversations_from_web(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "unchanged"


@patch("claude_client.client.requests")
def test_sync_conversations_from_web_updated(mock_req, client, tmp_path):
    (tmp_path / "test-chat-conv-uui.md").write_text("old content")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.sync_conversations_from_web(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "updated"


@patch("claude_client.client.requests")
def test_export_project_to_dir(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response(PROJECT_RESPONSE),  # get_project
        _mock_response(MEMORY_RESPONSE),  # get_memory
        _mock_response([DOC_META]),  # list_docs
        _mock_response(DOC_FULL),  # get_doc
        _mock_response(CONV_PAGE_RESPONSE),  # list_conversations
        _mock_response(CONVERSATION_DETAIL),  # get_conversation
    ]

    result = client.export_project_to_dir(PROJECT_ID, tmp_path / "export")

    out = result.path
    assert out == tmp_path / "export"
    assert (out / "project.md").exists()
    assert (out / "docs" / "notes.md").exists()
    assert (out / "conversations" / "test-chat-conv-uui.md").exists()

    project_md = (out / "project.md").read_text()
    assert "# My Project" in project_md
    assert "A test project" in project_md
    assert "Be helpful." in project_md
    assert "Auto-memory content" in project_md

    assert (out / "docs" / "notes.md").read_text() == "hello world"
    assert result.docs == {"notes.md": "created"}
    assert result.conversations == {"test-chat-conv-uui.md": "created"}


@patch("claude_client.client.requests")
def test_export_project_to_dir_is_incremental(mock_req, client, tmp_path):
    """A second sync into the same directory reports unchanged files, not rewrites."""
    responses = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECT_RESPONSE),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    mock_req.get.side_effect = responses
    client.export_project_to_dir(PROJECT_ID, tmp_path / "export")

    # Second sync: identical content on the web side.
    mock_req.get.side_effect = [
        _mock_response(PROJECT_RESPONSE),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    result = client.export_project_to_dir(PROJECT_ID, tmp_path / "export")

    assert result.docs == {"notes.md": "unchanged"}
    assert result.conversations == {"test-chat-conv-uui.md": "unchanged"}


@patch("claude_client.client.requests")
def test_export_all_projects_to_dir_multi_org(mock_req, client, tmp_path):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # list_all_projects: list_organizations
        _mock_response([project_a]),  # list_all_projects: org 1 projects
        _mock_response([project_b]),  # list_all_projects: org 2 projects
        _mock_response(project_a),  # export proj-a: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),  # list_docs
        _mock_response(empty_conv_page),  # list_conversations
        _mock_response(project_b),  # export proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]

    results = client.export_all_projects_to_dir(tmp_path)

    assert results == {"Project A": True, "Project B": True}
    assert (tmp_path / "project-a" / "project.md").exists()
    assert (tmp_path / "project-b" / "project.md").exists()


@patch("claude_client.client.requests")
def test_export_all_projects_to_dir_one_failure_does_not_abort_others(mock_req, client, tmp_path):
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # list_all_projects: list_organizations
        _mock_response([project_a, project_b]),  # list_all_projects: projects in org
        Exception("boom"),  # export proj-a: get_project raises
        _mock_response(project_b),  # export proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]

    results = client.export_all_projects_to_dir(tmp_path)

    assert results == {"Project A": False, "Project B": True}
    assert (tmp_path / "project-b" / "project.md").exists()
    # export_project_to_dir creates the output dir before its first API call, so
    # project-a's dir may exist, but it must be empty — the failure happened before
    # anything was written into it.
    assert not (tmp_path / "project-a" / "project.md").exists()


# ---------------------------------------------------------------- org targeting


def test_org_id_override_shadows_cached_property():
    client = ClaudeClient(TOKEN, org_id="explicit-org")
    assert client.org_id == "explicit-org"


@patch("claude_client.client.requests")
def test_find_project_org(mock_req, client):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # list_organizations
        _mock_response([]),  # projects in first org — not found here
        _mock_response(PROJECTS_RESPONSE),  # projects in second org — found
    ]
    org = client.find_project_org(PROJECT_ID)
    assert org == "other-org"


@patch("claude_client.client.requests")
def test_find_project_org_not_found(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([]),
    ]
    with pytest.raises(NotFoundError):
        client.find_project_org("missing-project")


@patch("claude_client.client.requests")
def test_update_project(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    updated = {**PROJECT_RESPONSE, "prompt_template": "New instructions."}
    mock_req.put.return_value = _mock_response(updated)

    result = client.update_project(PROJECT_ID, instructions="New instructions.")

    assert result["prompt_template"] == "New instructions."

    import json

    payload = json.loads(mock_req.put.call_args.kwargs["data"])
    assert payload == {"prompt_template": "New instructions."}


# ------------------------------------------------------------------------ CLI


def test_parse_project_id_from_url():
    from claude_client.cli import _parse_project_id

    url = "https://claude.ai/project/01999596-e432-71e7-87f3-1e326fcd142b"
    assert _parse_project_id(url) == "01999596-e432-71e7-87f3-1e326fcd142b"


def test_parse_project_id_from_bare_uuid():
    from claude_client.cli import _parse_project_id

    uuid = "01999596-e432-71e7-87f3-1e326fcd142b"
    assert _parse_project_id(uuid) == uuid


# -------------------------------------------------------------------- migrate


@patch("claude_client.client.requests")
def test_migrate_project_skips_update_when_source_has_no_metadata(mock_req):
    """An empty description/instructions must not trigger a PUT with an empty
    payload — the real API rejects that with 400 'must update at least one field'."""
    from claude_client.migrate import migrate_project

    source = ClaudeClient("source-token", org_id=ORG_ID)
    dest = ClaudeClient("dest-token", org_id=ORG_ID)

    empty_project = {**PROJECT_RESPONSE, "description": "", "prompt_template": ""}
    mock_req.get.side_effect = [
        _mock_response(empty_project),  # source.get_project
        _mock_response([DOC_META]),  # source.list_docs
        _mock_response(DOC_FULL),  # source.get_doc
        _mock_response([]),  # dest.upsert_content -> dest.list_docs (no existing doc)
        _mock_response({"data": [], "pagination": {"has_more": False}}),  # list_conversations
        _mock_response(MEMORY_RESPONSE),  # source.get_memory
        _mock_response([]),  # dest.upsert_content -> dest.list_docs (no existing doc)
    ]
    mock_req.post.return_value = _mock_response(DOC_FULL, status_code=201)

    counts = migrate_project(source, PROJECT_ID, dest, PROJECT_ID)

    mock_req.put.assert_not_called()
    assert counts == {"docs": 1, "conversations": 0, "memory": 1}
