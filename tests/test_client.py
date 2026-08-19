"""Unit tests for ClaudeClient — HTTP layer mocked via unittest.mock."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException

from claude_client import (
    AuthError,
    ClaudeClient,
    CloudflareChallengeError,
    NotFoundError,
    UploadError,
)
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


def _mock_response(
    json_data, status_code: int = 200, headers: dict | None = None, text: str = ""
) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    r.headers = headers or {}
    r.text = text
    return r


@pytest.fixture()
def client():
    return ClaudeClient(TOKEN)


# ------------------------------------------------------------------------ org


@patch("claude_client._transport.requests")
def test_org_id(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    assert client.org_id == ORG_ID


@patch("claude_client._transport.requests")
def test_org_id_and_chat_capable_ids_share_one_fetch(mock_req, client):
    """chat_capable_ids() and org_id both derive from the same cached org list —
    touching both must not trigger a second /organizations round trip."""
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)

    assert client.orgs.chat_capable_ids() == [ORG_ID]
    assert client.org_id == ORG_ID

    assert mock_req.get.call_count == 1


def test_org_id_override_shadows_cached_property():
    client = ClaudeClient(TOKEN, org_id="explicit-org")
    assert client.org_id == "explicit-org"


@patch("claude_client._transport.requests")
def test_check_auth_401_is_plain_auth_error_not_cloudflare(mock_req, client):
    """A 401 is always the app layer — Cloudflare never challenges with 401."""
    mock_req.get.return_value = _mock_response({}, status_code=401)
    with pytest.raises(AuthError) as exc_info:
        _ = client.org_id
    assert not isinstance(exc_info.value, CloudflareChallengeError)


@patch("claude_client._transport.requests")
def test_check_auth_403_app_layer_json_is_hedged_auth_error(mock_req, client):
    """A 403 that parses as claude.ai's own JSON error body may be a token problem
    or a real permission error (e.g. wrong-org access) — the body shape alone can't
    tell them apart, so the message must not claim expiry unconditionally."""
    mock_req.get.return_value = _mock_response(
        {"error": {"type": "permission_error"}},
        status_code=403,
        headers={"content-type": "application/json"},
    )
    with pytest.raises(AuthError) as exc_info:
        _ = client.org_id
    assert not isinstance(exc_info.value, CloudflareChallengeError)
    assert "may mean" in str(exc_info.value)


@patch("claude_client._transport.requests")
def test_check_auth_403_cloudflare_json_block_is_not_app_layer(mock_req, client):
    """A Cloudflare-intervened 403 can still carry a JSON body (its WAF block
    format) — cf-mitigated must win over a JSON-shaped body, or this is the exact
    misdiagnosis the PR set out to fix."""
    mock_req.get.return_value = _mock_response(
        {"success": False},
        status_code=403,
        headers={"cf-mitigated": "challenge", "content-type": "application/json"},
    )
    with pytest.raises(CloudflareChallengeError):
        _ = client.org_id


@patch("claude_client._transport.requests")
def test_check_auth_403_unrecognized_body_is_hedged_unknown_block(mock_req, client):
    """A 403 that looks like neither the app layer nor a recognizable Cloudflare
    challenge/block must not confidently blame Cloudflare specifically — only that
    it isn't a token problem."""
    mock_req.get.return_value = _mock_response({}, status_code=403)
    with pytest.raises(CloudflareChallengeError) as exc_info:
        _ = client.org_id
    assert "VPN" in str(exc_info.value)
    assert "expired" not in str(exc_info.value)


def test_is_app_layer_json_false_on_decode_error_not_crash():
    """curl_cffi's real Response.json() does loads(self.content) on raw bytes: a
    non-UTF-8 body raises UnicodeDecodeError, and with orjson installed (which
    curl_cffi prefers) a decode failure raises orjson.JSONDecodeError — a
    ValueError subclass, not json.JSONDecodeError. Either must be treated as
    'not the app layer', not escape and crash _check_auth."""
    from claude_client._transport import _is_app_layer_json

    for exc in (UnicodeDecodeError("utf-8", b"", 0, 1, "bad"), ValueError("bad json")):
        resp = _mock_response({}, headers={"content-type": "application/json"})
        resp.json.side_effect = exc
        assert _is_app_layer_json(resp) is False


def test_is_cloudflare_challenge_recognizes_live_captured_response():
    """Regression fixture: a live-captured Cloudflare challenge response (see
    tests/fixtures/cloudflare_challenge.html) — the exact bytes this repo
    actually received from Cloudflare, not a hand-written guess at its shape.

    Calls the body-marker helper directly (no `cf-mitigated` header) so this
    exercises the marker-matching branch specifically, rather than being
    short-circuited by the (also-valid, separately tested) header check.
    """
    from claude_client._transport import _is_cloudflare_challenge

    challenge_html = (Path(__file__).parent / "fixtures" / "cloudflare_challenge.html").read_text()
    resp = _mock_response(
        {},
        status_code=403,
        headers={"server": "cloudflare", "content-type": "text/html"},
        text=challenge_html,
    )
    assert _is_cloudflare_challenge(resp)


@patch("claude_client._transport.requests")
def test_check_auth_403_real_cloudflare_challenge_body(mock_req, client):
    """End-to-end: the live-captured challenge response, run through the full
    request path, raises CloudflareChallengeError rather than AuthError."""
    challenge_html = (Path(__file__).parent / "fixtures" / "cloudflare_challenge.html").read_text()
    mock_req.get.return_value = _mock_response(
        {},
        status_code=403,
        headers={"server": "cloudflare", "content-type": "text/html"},
        text=challenge_html,
    )
    with pytest.raises(CloudflareChallengeError):
        _ = client.org_id


@patch("claude_client._transport.requests")
def test_check_auth_403_cf_mitigated_header_is_conclusive(mock_req, client):
    """`cf-mitigated` is set by Cloudflare only when it actively intervenes —
    conclusive on its own, no body sniff needed."""
    mock_req.get.return_value = _mock_response(
        {}, status_code=403, headers={"cf-mitigated": "challenge"}
    )
    with pytest.raises(CloudflareChallengeError):
        _ = client.org_id


@patch("claude_client._transport.requests")
def test_check_auth_503_cloudflare_challenge(mock_req, client):
    mock_req.get.return_value = _mock_response(
        {},
        status_code=503,
        headers={"server": "cloudflare"},
        text="<title>Just a moment...</title>",
    )
    with pytest.raises(CloudflareChallengeError):
        _ = client.org_id


def test_check_auth_503_without_cloudflare_evidence_is_not_challenge():
    """A real 503 from claude.ai itself (no Cloudflare headers/body) must not be
    misreported as a Cloudflare block — _check_auth should raise nothing, leaving
    the caller's raise_for_status() to report it as the plain HTTP error it is."""
    from claude_client._transport import Transport

    resp = _mock_response({}, status_code=503)
    Transport(TOKEN)._check_auth(resp)  # must not raise


@patch("claude_client._transport.requests")
def test_cloudflare_challenge_error_is_caught_by_except_auth_error(mock_req, client):
    """Pins the backward-compatibility contract: existing `except AuthError`
    callers (e.g. claude-web-backup, outside this repo) keep working unchanged."""
    mock_req.get.return_value = _mock_response({}, status_code=403)
    try:
        _ = client.org_id
    except AuthError as exc:
        assert isinstance(exc, CloudflareChallengeError)
    else:
        pytest.fail("expected AuthError (as CloudflareChallengeError) to be raised")


# -------------------------------------------------------------------- projects


@patch("claude_client._transport.requests")
def test_projects_list_spans_all_orgs_by_default(mock_req, client):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    other_project = {
        "uuid": "other-proj",
        "name": "Other Project",
        "description": "",
        "prompt_template": "",
    }
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # chat_capable_org_ids
        _mock_response(PROJECTS_RESPONSE),  # projects in first org
        _mock_response([other_project]),  # projects in second org
    ]

    results = client.projects.list()

    assert results == [
        (ORG_ID, PROJECTS_RESPONSE[0]),
        ("other-org", other_project),
    ]


@patch("claude_client._transport.requests")
def test_projects_list_skips_non_chat_orgs(mock_req, client):
    non_chat_org = {"uuid": "no-chat-org", "capabilities": ["other"], "name": "No Chat Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, non_chat_org]),
        _mock_response(PROJECTS_RESPONSE),  # only the chat-capable org gets queried
    ]

    results = client.projects.list()

    assert [org_id for org_id, _ in results] == [ORG_ID]


@patch("claude_client._transport.requests")
def test_projects_list_scoped_to_one_org_skips_org_enumeration(mock_req, client):
    mock_req.get.return_value = _mock_response(PROJECTS_RESPONSE)

    results = client.projects.list(org_id=ORG_ID)

    assert results == [(ORG_ID, PROJECTS_RESPONSE[0])]
    assert mock_req.get.call_count == 1  # no chat_capable_org_ids() call needed


@patch("claude_client._transport.requests")
def test_projects_find(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECTS_RESPONSE),
    ]
    org_id, p = client.projects.find("My Project")
    assert org_id == ORG_ID
    assert p["uuid"] == PROJECT_ID


@patch("claude_client._transport.requests")
def test_projects_find_returns_first_match_on_ambiguous_name(mock_req, client):
    """A same-named project in two orgs must not error or silently drop information —
    it returns the first match, org included, so the caller can tell which one."""
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    duplicate_project = {**PROJECTS_RESPONSE[0], "uuid": "other-proj-uuid"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),
        _mock_response(PROJECTS_RESPONSE),
        _mock_response([duplicate_project]),
    ]

    org_id, p = client.projects.find("My Project")

    assert org_id == ORG_ID
    assert p["uuid"] == PROJECT_ID


@patch("claude_client._transport.requests")
def test_projects_find_not_found(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECTS_RESPONSE),
    ]
    with pytest.raises(NotFoundError):
        client.projects.find("Nonexistent")


@patch("claude_client._transport.requests")
def test_projects_find_org(mock_req, client):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # list_organizations
        _mock_response([]),  # projects in first org — not found here
        _mock_response(PROJECTS_RESPONSE),  # projects in second org — found
    ]
    org = client.projects.find_org(PROJECT_ID)
    assert org == "other-org"


@patch("claude_client._transport.requests")
def test_projects_find_org_not_found(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([]),
    ]
    with pytest.raises(NotFoundError):
        client.projects.find_org("missing-project")


@patch("claude_client._transport.requests")
def test_projects_update(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    updated = {**PROJECT_RESPONSE, "prompt_template": "New instructions."}
    mock_req.put.return_value = _mock_response(updated)

    result = client.projects.update(PROJECT_ID, instructions="New instructions.")

    assert result["prompt_template"] == "New instructions."
    payload = json.loads(mock_req.put.call_args.kwargs["data"])
    assert payload == {"prompt_template": "New instructions."}


# ------------------------------------------------------------------------ docs


@pytest.mark.parametrize(
    ("name", "fallback", "expected"),
    [
        (" report ", "doc-fallback", "report.md"),
        ("folder/notes", "doc-fallback", "folder-notes.md"),
        ("Q3::2024", "doc-fallback", "Q3-2024.md"),
        ("notes.md", "doc-fallback", "notes.md"),
        ("   ", "doc-fallback", "doc-fallback.md"),
        ("???", "doc-fallback", "doc-fallback.md"),
    ],
)
def test_safe_md_filename(name, fallback, expected):
    from claude_client.resources.docs import _safe_md_filename

    assert _safe_md_filename(name, fallback) == expected


@patch("claude_client._transport.requests")
def test_docs_pull_disambiguates_lossy_and_case_only_collisions(mock_req, client, tmp_path):
    docs = [
        {"uuid": "doc-slash", "file_name": "Q3/2024", "content": "slash"},
        {"uuid": "doc-dash", "file_name": "Q3-2024", "content": "dash"},
        {"uuid": "doc-upper", "file_name": "README", "content": "upper"},
        {"uuid": "doc-lower", "file_name": "readme", "content": "lower"},
    ]
    docs_meta = [{key: doc[key] for key in ("uuid", "file_name")} for doc in docs]
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(docs_meta),
        *[_mock_response(doc) for doc in docs],
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    expected = {
        "Q3-2024-doc-slash.md": "slash",
        "Q3-2024-doc-dash.md": "dash",
        "README-doc-upper.md": "upper",
        "readme-doc-lower.md": "lower",
    }
    assert results == {name: "created" for name in expected}
    assert {path.name: path.read_text() for path in tmp_path.iterdir()} == expected


@patch("claude_client._transport.requests")
def test_docs_get(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(DOC_FULL),
    ]
    doc = client.docs.get(PROJECT_ID, DOC_UUID)
    assert doc["content"] == "hello world"


@patch("claude_client._transport.requests")
def test_docs_rm(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    mock_req.delete.return_value = _mock_response(None, status_code=204)

    client.docs.rm(PROJECT_ID, DOC_UUID)
    assert mock_req.delete.called


@patch("claude_client._transport.requests")
def test_docs_rm_all(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META, {**DOC_META, "uuid": "doc-2", "file_name": "b.md"}]),
    ]
    mock_req.delete.return_value = _mock_response(None, status_code=204)

    count = client.docs.rm_all(PROJECT_ID)

    assert count == 2
    assert mock_req.delete.call_count == 2


@patch("claude_client._transport.requests")
def test_docs_push_many(mock_req, client, tmp_path):
    file_a = tmp_path / "a.md"
    file_b = tmp_path / "b.md"
    file_a.write_text("content a")
    file_b.write_text("content b")

    # Each push_content call: list_docs (no existing match) then create.
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([]),
        _mock_response([]),
    ]
    mock_req.post.return_value = _mock_response({**DOC_FULL}, status_code=201)

    results = client.docs.push_many(PROJECT_ID, [file_a, file_b], name_prefix="Proj__")

    assert results == {"Proj__a.md": True, "Proj__b.md": True}
    assert mock_req.post.call_count == 2


@patch("claude_client._transport.requests")
def test_docs_push_many_reports_per_file_failure(mock_req, client, tmp_path):
    missing = tmp_path / "missing.md"  # never written — push() will raise FileNotFoundError

    results = client.docs.push_many(PROJECT_ID, [missing])

    assert results == {"missing.md": False}


@patch("claude_client._transport.requests")
def test_docs_push_content_creates_when_no_existing_doc(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([]),  # list_docs — nothing matches
    ]
    mock_req.post.return_value = _mock_response({**DOC_FULL}, status_code=201)

    doc = client.docs.push_content(PROJECT_ID, "hello world", "notes.md")

    assert doc["uuid"] == DOC_UUID
    assert not mock_req.delete.called
    payload = json.loads(mock_req.post.call_args.kwargs["data"])
    assert payload == {"file_name": "notes.md", "content": "hello world"}


@patch("claude_client._transport.requests")
def test_docs_push_content_error_no_existing_doc_not_wrapped(mock_req, client):
    """When there's no existing doc to delete, an upload failure should propagate
    as the plain UploadError, without the delete-related wrapper message."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([]),
    ]
    mock_req.post.return_value = _mock_response({}, status_code=500)

    with pytest.raises(UploadError) as exc_info:
        client.docs.push_content(PROJECT_ID, "new content", "notes.md")

    assert "original has been removed" not in str(exc_info.value)
    assert not mock_req.delete.called


@patch("claude_client._transport.requests")
def test_docs_push_content_replaces_existing(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),  # list_docs — matching file_name found
    ]
    mock_req.delete.return_value = _mock_response(None, status_code=204)
    mock_req.post.return_value = _mock_response({**DOC_FULL}, status_code=201)

    client.docs.push_content(PROJECT_ID, "new content", "notes.md")

    assert mock_req.delete.called
    assert mock_req.post.called


@patch("claude_client._transport.requests")
def test_docs_push_content_failure_with_existing_doc_leaves_original(mock_req, client):
    """Create happens before delete, so a failed create must not touch the
    existing doc — the caller shouldn't lose it."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
    ]
    mock_req.post.return_value = _mock_response({}, status_code=500)

    with pytest.raises(UploadError):
        client.docs.push_content(PROJECT_ID, "new content", "notes.md")

    assert mock_req.post.called
    assert not mock_req.delete.called


@patch("claude_client._transport.requests")
def test_docs_pull_created(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "created"
    assert (tmp_path / "notes.md").read_text() == "hello world"


@patch("claude_client._transport.requests")
def test_docs_pull_unchanged(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("hello world")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "unchanged"


@patch("claude_client._transport.requests")
def test_docs_pull_updated(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("old content")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "updated"
    assert (tmp_path / "notes.md").read_text() == "hello world"


@patch("claude_client._transport.requests")
def test_docs_pull_force_rewrites_unchanged_file(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("hello world")  # already identical to web content

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path, force=True)

    assert results["notes.md"] == "updated"  # not "unchanged" — force skips the comparison


@patch("claude_client._transport.requests")
def test_docs_pull_skips_doc_on_fetch_failure(mock_req, client, tmp_path):
    """A failed get() must be skipped, not written as empty content."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_META]),
        RequestException("boom"),  # get_doc fails
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

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


# ----------------------------------------------------------------- conversations


@patch("claude_client._transport.requests")
def test_conversations_get(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    conv = client.conversations.get(CONV_UUID)

    assert conv["uuid"] == CONV_UUID
    assert len(conv["chat_messages"]) == 2


@patch("claude_client._transport.requests")
def test_conversations_list(mock_req, client):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
    ]

    convs = client.conversations.list(PROJECT_ID)

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


@patch("claude_client._transport.requests")
def test_conversations_pull_created(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert len(results) == 1
    assert results["test-chat-conv-uui.md"] == "created"


@patch("claude_client._transport.requests")
def test_conversations_pull_unchanged(mock_req, client, tmp_path):
    md = conversation_to_markdown(CONVERSATION_DETAIL)
    (tmp_path / "test-chat-conv-uui.md").write_text(md)

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "unchanged"


@patch("claude_client._transport.requests")
def test_conversations_pull_updated(mock_req, client, tmp_path):
    (tmp_path / "test-chat-conv-uui.md").write_text("old content")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "updated"


# --------------------------------------------------------------------- projects
# (composite: pull / pull_all, which pull docs + conversations + memory together)


@patch("claude_client._transport.requests")
def test_projects_pull(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response(PROJECT_RESPONSE),  # get_project
        _mock_response(MEMORY_RESPONSE),  # get_memory
        _mock_response([DOC_META]),  # list_docs
        _mock_response(DOC_FULL),  # get_doc
        _mock_response(CONV_PAGE_RESPONSE),  # list_conversations
        _mock_response(CONVERSATION_DETAIL),  # get_conversation
    ]

    result = client.projects.pull(PROJECT_ID, tmp_path / "export")

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


@patch("claude_client._transport.requests")
def test_projects_pull_is_incremental(mock_req, client, tmp_path):
    """A second pull into the same directory reports unchanged files, not rewrites."""
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
    client.projects.pull(PROJECT_ID, tmp_path / "export")

    # Second pull: identical content on the web side.
    mock_req.get.side_effect = [
        _mock_response(PROJECT_RESPONSE),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([DOC_META]),
        _mock_response(DOC_FULL),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    result = client.projects.pull(PROJECT_ID, tmp_path / "export")

    assert result.docs == {"notes.md": "unchanged"}
    assert result.conversations == {"test-chat-conv-uui.md": "unchanged"}


@patch("claude_client._transport.requests")
def test_projects_pull_all_multi_org(mock_req, client, tmp_path):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # list(): chat_capable_org_ids
        _mock_response([project_a]),  # list(): org 1 projects
        _mock_response([project_b]),  # list(): org 2 projects
        _mock_response(project_a),  # pull proj-a: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),  # list_docs
        _mock_response(empty_conv_page),  # list_conversations
        _mock_response(project_b),  # pull proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]

    results = client.projects.pull_all(tmp_path)

    assert results == {"Project A": True, "Project B": True}
    assert (tmp_path / "project-a" / "project.md").exists()
    assert (tmp_path / "project-b" / "project.md").exists()


@patch("claude_client._transport.requests")
def test_projects_pull_all_one_failure_does_not_abort_others(mock_req, client, tmp_path):
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # list(): chat_capable_org_ids
        _mock_response([project_a, project_b]),  # list(): projects in org
        RequestException("boom"),  # pull proj-a: get_project raises
        _mock_response(project_b),  # pull proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]

    results = client.projects.pull_all(tmp_path)

    assert results == {"Project A": False, "Project B": True}
    assert (tmp_path / "project-b" / "project.md").exists()
    # pull() creates the output dir before its first API call, so project-a's dir may
    # exist, but it must be empty — the failure happened before anything was written.
    assert not (tmp_path / "project-a" / "project.md").exists()


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


@patch("claude_client._transport.requests")
def test_migrate_project_skips_update_when_source_has_no_metadata(mock_req):
    """An empty description/instructions must not trigger a PUT with an empty
    payload — the real API rejects that with 400 'must update at least one field'."""
    from claude_client.migrate import migrate_project

    source = ClaudeClient("source-token", org_id=ORG_ID)
    dest = ClaudeClient("dest-token", org_id=ORG_ID)

    empty_project = {**PROJECT_RESPONSE, "description": "", "prompt_template": ""}
    mock_req.get.side_effect = [
        _mock_response(empty_project),  # source.projects.get
        _mock_response([DOC_META]),  # source.docs.list
        _mock_response(DOC_FULL),  # source.docs.get
        _mock_response([]),  # dest.docs.push_content -> dest.docs.list (no existing doc)
        _mock_response({"data": [], "pagination": {"has_more": False}}),  # conversations.list
        _mock_response(MEMORY_RESPONSE),  # source.memory.get
        _mock_response([]),  # dest.docs.push_content -> dest.docs.list (no existing doc)
    ]
    mock_req.post.return_value = _mock_response(DOC_FULL, status_code=201)

    counts = migrate_project(source, PROJECT_ID, dest, PROJECT_ID)

    mock_req.put.assert_not_called()
    assert counts == {"docs": 1, "conversations": 0, "memory": 1}
