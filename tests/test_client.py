"""Unit tests for ClaudeClient — HTTP layer mocked via unittest.mock."""

from unittest.mock import MagicMock, patch

import pytest

from claude_client import AuthError, ClaudeClient, NotFoundError, UploadError

ORG_ID = "org-uuid"
PROJECT_ID = "proj-uuid"
DOC_UUID = "doc-uuid"
TOKEN = "sk-ant-sid01-test"

ORGS_RESPONSE = [{"uuid": ORG_ID, "capabilities": ["chat"], "name": "Test Org"}]
PROJECTS_RESPONSE = [
    {"uuid": PROJECT_ID, "name": "My Project", "description": "", "prompt_template": ""}
]
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
