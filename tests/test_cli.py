"""Unit tests for the `_client` org-resolution helper in claude_client.cli."""

import argparse
from unittest.mock import MagicMock, patch

from claude_client.cli import _client, _project_list, _project_show

ORG_ID = "org-uuid"
OTHER_ORG_ID = "other-org-uuid"
PROJECT_ID = "proj-uuid"
TOKEN = "sk-ant-sid01-test"

ORGS_RESPONSE = [{"uuid": ORG_ID, "capabilities": ["chat"], "name": "Test Org"}]
MULTI_ORGS_RESPONSE = [
    *ORGS_RESPONSE,
    {"uuid": OTHER_ORG_ID, "capabilities": ["chat"], "name": "Other Org"},
]
PROJECTS_RESPONSE = [{"uuid": PROJECT_ID, "name": "My Project"}]


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


def _args() -> argparse.Namespace:
    return argparse.Namespace(token=TOKEN)


@patch("claude_client._transport.requests")
def test_client_without_project_id_skips_org_resolution(mock_req):
    _client(_args())
    # No API calls at all — org resolution is only attempted when a project id is given.
    mock_req.get.assert_not_called()


@patch("claude_client._transport.requests")
def test_client_single_org_account_skips_lookup(mock_req):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)

    client = _client(_args(), PROJECT_ID)

    # Only the one chat_capable_org_ids() call — no find_project_org scan.
    assert mock_req.get.call_count == 1
    assert client.org_id == ORG_ID
    # org_id derives from the cached _org_ids list, already populated by the
    # chat_capable_ids() call above — touching it here must not trigger a second
    # /organizations round trip. The returned client is unpinned (no explicit
    # org_id=); it works here only because there's exactly one chat-capable org.
    assert mock_req.get.call_count == 1


@patch("claude_client._transport.requests")
def test_client_multi_org_account_resolves_owning_org(mock_req):
    mock_req.get.side_effect = [
        _mock_response(MULTI_ORGS_RESPONSE),  # chat_capable_org_ids()
        _mock_response(MULTI_ORGS_RESPONSE),  # find_project_org -> list_organizations()
        _mock_response([]),  # projects in first org — not found here
        _mock_response(PROJECTS_RESPONSE),  # projects in second org — found
    ]

    client = _client(_args(), PROJECT_ID)

    assert client.org_id == OTHER_ORG_ID


ARCHIVED_AT = "2026-11-28T01:15:50.288098Z"
ACTIVE = {"uuid": PROJECT_ID, "name": "My Project", "archived_at": None}
ARCHIVED = {"uuid": "old-proj", "name": "Old Project", "archived_at": ARCHIVED_AT}


@patch("claude_client.cli._client")
def test_project_list_marks_only_archived_rows(mock_client, capsys):
    mock_client.return_value.projects.list.return_value = [(ORG_ID, ACTIVE), (ORG_ID, ARCHIVED)]

    _project_list(_args())

    assert capsys.readouterr().out.splitlines() == [
        f"{ORG_ID}  {PROJECT_ID}  My Project",
        f"{ORG_ID}  old-proj  Old Project  archived",
    ]


@patch("claude_client.cli._client")
def test_project_show_prints_archived_only_when_archived(mock_client, capsys):
    project = {**ARCHIVED, "description": "", "prompt_template": ""}
    mock_client.return_value.projects.get.return_value = project
    _project_show(argparse.Namespace(token=TOKEN, project_id="old-proj"))
    assert f"Archived: {ARCHIVED_AT}" in capsys.readouterr().out

    mock_client.return_value.projects.get.return_value = {**project, "archived_at": None}
    _project_show(argparse.Namespace(token=TOKEN, project_id="old-proj"))
    assert "Archived" not in capsys.readouterr().out
