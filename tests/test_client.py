"""Unit tests for ClaudeClient — HTTP layer mocked via unittest.mock."""

import json
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException

from claude_client import (
    AmbiguousOrgError,
    AuthError,
    ClaudeClient,
    CloudflareChallengeError,
    NotFoundError,
    UploadError,
)
from claude_client._manifest import MANIFEST_NAME
from claude_client.render import conversation_to_markdown, slugify

ORG_ID = "org-uuid"
OTHER_ORG_ID = "other-org-uuid"
PROJECT_ID = "proj-uuid"
DOC_UUID = "doc-uuid"
TOKEN = "sk-ant-sid01-test"

ORGS_RESPONSE = [{"uuid": ORG_ID, "capabilities": ["chat"], "name": "Test Org"}]
MULTI_ORGS_RESPONSE = [
    *ORGS_RESPONSE,
    {"uuid": OTHER_ORG_ID, "capabilities": ["chat"], "name": "Other Org"},
]
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


def test_org_id_explicit_pin_wins():
    client = ClaudeClient(TOKEN, org_id="explicit-org")
    assert client.org_id == "explicit-org"


@patch("claude_client._transport.requests")
def test_org_id_raises_ambiguous_on_multi_org_unpinned_account(mock_req, client):
    """The bug itself: an unpinned client on a multi-org account must not silently
    guess an org — it must refuse rather than send a request to the wrong one."""
    mock_req.get.return_value = _mock_response(MULTI_ORGS_RESPONSE)
    with pytest.raises(AmbiguousOrgError):
        _ = client.org_id


@patch("claude_client._transport.requests")
def test_docs_list_raises_ambiguous_on_multi_org_unpinned_account(mock_req, client):
    """Same bug, exercised through a real resource method rather than the bare property."""
    mock_req.get.return_value = _mock_response(MULTI_ORGS_RESPONSE)
    with pytest.raises(AmbiguousOrgError):
        client.docs.list(PROJECT_ID)


@patch("claude_client._transport.requests")
def test_for_project_resolves_and_pins_the_owning_org(mock_req, client):
    """client.for_project(project_id) is the one-call fix: it must target the org
    that actually owns the project, not whichever org is listed first."""
    mock_req.get.side_effect = [
        _mock_response(MULTI_ORGS_RESPONSE),  # find_org -> list_organizations()
        _mock_response([]),  # projects in first org — not found here
        _mock_response(PROJECTS_RESPONSE),  # projects in second org — found
        _mock_response([DOC_FULL]),  # docs.list(), now scoped to the second org
    ]

    scoped = client.for_project(PROJECT_ID)
    assert scoped.org_id == OTHER_ORG_ID

    docs = scoped.docs.list(PROJECT_ID)

    assert docs == [DOC_FULL]
    # The docs.list() request must have gone to the *second* org, not the first —
    # asserted against the exact expected URL rather than substring containment,
    # since OTHER_ORG_ID ("other-org-uuid") contains ORG_ID ("org-uuid") as a substring.
    requested_url = mock_req.get.call_args.args[0]
    assert requested_url == (
        f"https://claude.ai/api/organizations/{OTHER_ORG_ID}/projects/{PROJECT_ID}/docs"
    )


@patch("claude_client._transport.requests")
def test_org_id_resolves_normally_on_single_org_account(mock_req, client):
    """Regression guard: single-org accounts (every other test in this module) must
    keep working exactly as before — no raise, no behavior change."""
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    assert client.org_id == ORG_ID


@patch("claude_client._transport.requests")
def test_org_id_raises_value_error_when_no_chat_capable_org(mock_req, client):
    mock_req.get.return_value = _mock_response([])
    with pytest.raises(ValueError):
        _ = client.org_id


@patch("claude_client._transport.requests")
def test_explicit_pin_survives_update_token(mock_req):
    """A caller who pinned an org meant it regardless of which token is in use —
    update_token() must not silently revert a multi-org client to the buggy,
    auto-resolved path (which would now raise AmbiguousOrgError)."""
    client = ClaudeClient(TOKEN, org_id=OTHER_ORG_ID)
    client.update_token("sk-ant-sid01-new-test")

    assert client.org_id == OTHER_ORG_ID
    mock_req.get.assert_not_called()  # pinned — never needed to list orgs at all


@patch("claude_client._transport.requests")
def test_update_token_still_invalidates_auto_resolved_org_cache(mock_req, client):
    """The auto-resolved org list (unlike an explicit pin) legitimately depends on
    the token — update_token() must still drop it, or a token swap to an account
    with different org membership would keep serving the stale, previous org list."""
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    assert client.org_id == ORG_ID
    assert mock_req.get.call_count == 1

    client.update_token("sk-ant-sid01-new-test")
    mock_req.get.return_value = _mock_response(MULTI_ORGS_RESPONSE)

    # New token's account has two chat-capable orgs — this can only be seen if the
    # cached single-org list was actually dropped, not reused, by update_token().
    with pytest.raises(AmbiguousOrgError):
        _ = client.org_id
    assert mock_req.get.call_count == 2  # re-fetched /organizations, cache was dropped


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


# --------------------------------------------------------------- scheduled tasks

TASK_ID = "trig_01U9nqQru9TJTfYhYPqX1NDb"


@patch("claude_client._transport.requests")
def test_scheduled_tasks_run(mock_req, client):
    mock_req.get.return_value = _mock_response(ORGS_RESPONSE)
    mock_req.post.return_value = _mock_response({"status": "queued"})

    result = client.scheduled_tasks.run(TASK_ID)

    assert result == {"status": "queued"}
    requested_url = mock_req.post.call_args.args[0]
    assert requested_url == (
        f"https://claude.ai/api/organizations/{ORG_ID}/cowork/scheduled_tasks/{TASK_ID}/run"
    )
    payload = json.loads(mock_req.post.call_args.kwargs["data"])
    assert payload == {}


@patch("claude_client._transport.requests")
def test_scheduled_tasks_run_raises_ambiguous_on_multi_org_unpinned_account(mock_req, client):
    """Task ids are unique account-wide, but the org still has to be resolved (it's
    part of the URL) — same AmbiguousOrgError contract as every other org-scoped call."""
    mock_req.get.return_value = _mock_response(MULTI_ORGS_RESPONSE)
    with pytest.raises(AmbiguousOrgError):
        client.scheduled_tasks.run(TASK_ID)


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
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(docs),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    expected = {
        "Q3-2024-doc-slash.md": "slash",
        "Q3-2024-doc-dash.md": "dash",
        "README-doc-upper.md": "upper",
        "readme-doc-lower.md": "lower",
    }
    assert results == {name: "created" for name in expected}
    assert {
        path.name: path.read_text() for path in tmp_path.iterdir() if path.name != MANIFEST_NAME
    } == expected


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
    """docs.list() already returns full content — pull must not call get() per doc."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "created"
    assert (tmp_path / "notes.md").read_text() == "hello world"
    assert mock_req.get.call_count == 2


@patch("claude_client._transport.requests")
def test_docs_pull_unchanged(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("hello world")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "unchanged"


@patch("claude_client._transport.requests")
def test_docs_pull_updated(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("old content")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results["notes.md"] == "updated"
    assert (tmp_path / "notes.md").read_text() == "hello world"


@patch("claude_client._transport.requests")
def test_docs_pull_force_rewrites_unchanged_file(mock_req, client, tmp_path):
    (tmp_path / "notes.md").write_text("hello world")  # already identical to web content

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path, force=True)

    assert results["notes.md"] == "updated"  # not "unchanged" — force skips the comparison


@patch("claude_client._transport.requests")
def test_docs_pull_raises_on_list_failure(mock_req, client, tmp_path):
    """There is no per-doc fetch left to skip past — a failed list() call must
    propagate, same as ConversationsResource.pull."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        RequestException("boom"),
    ]

    with pytest.raises(RequestException):
        client.docs.pull(PROJECT_ID, tmp_path)


@patch("claude_client._transport.requests")
def test_docs_pull_prune_deletes_doc_removed_on_web(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]
    client.docs.pull(PROJECT_ID, tmp_path)
    assert (tmp_path / "notes.md").exists()

    mock_req.get.side_effect = [_mock_response([])]  # doc no longer listed
    results = client.docs.pull(PROJECT_ID, tmp_path, prune=True)

    assert results["notes.md"] == "deleted"
    assert not (tmp_path / "notes.md").exists()


@patch("claude_client._transport.requests")
def test_docs_pull_without_prune_keeps_doc_removed_on_web(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),
    ]
    client.docs.pull(PROJECT_ID, tmp_path)

    mock_req.get.side_effect = [_mock_response([])]
    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert "notes.md" not in results
    assert (tmp_path / "notes.md").exists()


@patch("claude_client._transport.requests")
def test_docs_pull_prune_removes_stale_file_left_by_rename(mock_req, client, tmp_path):
    """A doc's uuid persists across a rename; the OLD filename must become a prune
    target even though the uuid is still present remotely."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),  # file_name: notes.md
    ]
    client.docs.pull(PROJECT_ID, tmp_path)
    assert (tmp_path / "notes.md").exists()

    renamed = {**DOC_FULL, "file_name": "renamed.md"}
    mock_req.get.side_effect = [_mock_response([renamed])]
    results = client.docs.pull(PROJECT_ID, tmp_path, prune=True)

    assert results["renamed.md"] == "created"
    assert results["notes.md"] == "deleted"
    assert (tmp_path / "renamed.md").exists()
    assert not (tmp_path / "notes.md").exists()


@patch("claude_client._transport.requests")
def test_docs_pull_prune_does_not_delete_name_reused_by_a_new_doc(mock_req, client, tmp_path):
    """doc-uuid is deleted on the web and a NEW doc (different uuid) is created that
    resolves to the same filename. The old uuid's stale entry must not clobber the
    just-written file for the new uuid."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([DOC_FULL]),  # uuid=doc-uuid, file_name=notes.md
    ]
    client.docs.pull(PROJECT_ID, tmp_path)

    new_doc = {"uuid": "new-doc-uuid", "file_name": "notes.md", "content": "new content"}
    mock_req.get.side_effect = [_mock_response([new_doc])]
    results = client.docs.pull(PROJECT_ID, tmp_path, prune=True)

    assert results["notes.md"] in ("updated", "unchanged")
    assert "deleted" not in results.values()
    assert (tmp_path / "notes.md").read_text() == "new content"


@patch("claude_client._transport.requests")
def test_docs_pull_skips_doc_missing_content_key(mock_req, client, tmp_path):
    """If the list response ever omits `content` (API change) and there's no local
    file to fall back on, pull must not write an empty file — it must surface the gap
    rather than silently doing nothing."""
    no_content_doc = {"uuid": DOC_UUID, "file_name": "notes.md"}
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([no_content_doc]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert results == {"notes.md": "content_missing"}
    assert not (tmp_path / "notes.md").exists()


@patch("claude_client._transport.requests")
def test_docs_pull_missing_content_key_leaves_existing_local_file_untouched(
    mock_req, client, tmp_path
):
    """If content is missing but a prior successful pull already wrote the file, the
    existing local copy must be left alone rather than deleted or blanked."""
    (tmp_path / "notes.md").write_text("hello world")
    no_content_doc = {"uuid": DOC_UUID, "file_name": "notes.md"}
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([no_content_doc]),
    ]

    results = client.docs.pull(PROJECT_ID, tmp_path)

    assert "notes.md" not in results  # not reported as missing — the file is right there
    assert (tmp_path / "notes.md").read_text() == "hello world"


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


@patch("claude_client._transport.requests")
def test_conversations_pull_skips_fetch_when_manifest_matches(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    client.conversations.pull(PROJECT_ID, tmp_path)

    mock_req.get.reset_mock()
    mock_req.get.side_effect = [_mock_response(CONV_PAGE_RESPONSE)]  # no per-conversation GET
    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "unchanged"
    assert mock_req.get.call_count == 1


@patch("claude_client._transport.requests")
def test_conversations_pull_refetches_when_local_file_deleted(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    client.conversations.pull(PROJECT_ID, tmp_path)
    (tmp_path / "test-chat-conv-uui.md").unlink()

    mock_req.get.side_effect = [
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results["test-chat-conv-uui.md"] == "created"


@patch("claude_client._transport.requests")
def test_conversations_pull_force_refetches_despite_manifest_hit(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    client.conversations.pull(PROJECT_ID, tmp_path)

    mock_req.get.side_effect = [
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    results = client.conversations.pull(PROJECT_ID, tmp_path, force=True)

    assert results["test-chat-conv-uui.md"] == "updated"


@patch("claude_client._transport.requests")
def test_conversations_pull_prune_deletes_conversation_removed_on_web(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    client.conversations.pull(PROJECT_ID, tmp_path)

    empty_conv_page = {
        "data": [],
        "pagination": {"total": 0, "limit": 30, "offset": 0, "has_more": False},
    }
    mock_req.get.side_effect = [_mock_response(empty_conv_page)]
    results = client.conversations.pull(PROJECT_ID, tmp_path, prune=True)

    assert results["test-chat-conv-uui.md"] == "deleted"
    assert not (tmp_path / "test-chat-conv-uui.md").exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_fetch_failure_preserves_entry_for_later_prune(
    mock_req, client, tmp_path
):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    client.conversations.pull(PROJECT_ID, tmp_path)

    mock_req.get.side_effect = [_mock_response(CONV_PAGE_RESPONSE), RequestException("boom")]
    client.conversations.pull(PROJECT_ID, tmp_path, force=True)

    empty_conv_page = {
        "data": [],
        "pagination": {"total": 0, "limit": 30, "offset": 0, "has_more": False},
    }
    mock_req.get.side_effect = [_mock_response(empty_conv_page)]
    results = client.conversations.pull(PROJECT_ID, tmp_path, prune=True)

    assert results["test-chat-conv-uui.md"] == "deleted"


def _conv_meta(uuid: str, name: str = "Test Chat", updated_at: str = "2024-01-02") -> dict:
    return {**CONVERSATION_META[0], "uuid": uuid, "name": name, "updated_at": updated_at}


def _conv_detail(uuid: str, name: str = "Test Chat") -> dict:
    return {**CONVERSATION_DETAIL, "uuid": uuid, "name": name}


def _url_dispatch(page: dict, details_by_uuid: dict[str, dict], *, fail_uuids: Iterable[str] = ()):
    """A `requests.get` side_effect keyed by URL, for tests fetching >1 conversation.

    Positional side_effect lists can't be used once more than one conversation needs
    fetching: concurrent fetches submit to the mock in submission order, not the
    thread-completion order a positional list would implicitly assume.
    """
    fail_uuids = set(fail_uuids)

    def dispatch(url, **_kwargs):
        if url.endswith("/organizations"):
            return _mock_response(ORGS_RESPONSE)
        if "/conversations_v2" in url:
            return _mock_response(page)
        if "/chat_conversations/" in url:
            uuid = url.split("/chat_conversations/")[1].split("?")[0]
            if uuid in fail_uuids:
                raise RequestException("boom")
            return _mock_response(details_by_uuid[uuid])
        raise AssertionError(f"unexpected URL in test: {url}")

    return dispatch


@patch("claude_client._transport.requests")
def test_conversations_pull_fetches_multiple_conversations_all_succeed(mock_req, client, tmp_path):
    uuids = [f"conv-{i}" for i in range(3)]
    metas = [_conv_meta(u, name=f"Chat {u}") for u in uuids]
    page = {
        "data": metas,
        "pagination": {"total": len(metas), "limit": 30, "offset": 0, "has_more": False},
    }
    details = {u: _conv_detail(u, name=f"Chat {u}") for u in uuids}
    mock_req.get.side_effect = _url_dispatch(page, details)

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert len(results) == 3
    for u in uuids:
        filename = f"chat-{u}-{u[:8]}.md"
        assert results[filename] == "created"
        assert (tmp_path / filename).exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_partial_failure_does_not_abort_others(mock_req, client, tmp_path):
    uuids = [f"conv-{i}" for i in range(3)]
    metas = [_conv_meta(u, name=f"Chat {u}") for u in uuids]
    page = {
        "data": metas,
        "pagination": {"total": len(metas), "limit": 30, "offset": 0, "has_more": False},
    }
    details = {u: _conv_detail(u, name=f"Chat {u}") for u in uuids}
    failing = uuids[1]
    mock_req.get.side_effect = _url_dispatch(page, details, fail_uuids={failing})

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    for u in uuids:
        filename = f"chat-{u}-{u[:8]}.md"
        if u == failing:
            assert filename not in results
            assert not (tmp_path / filename).exists()
        else:
            assert results[filename] == "created"


@patch("claude_client._transport.requests")
def test_conversations_pull_parallelizes_fetches(mock_req, client, tmp_path):
    import threading
    import time

    from claude_client.resources.conversations import _MAX_CONVERSATION_FETCH_WORKERS

    uuids = [f"conv-{i}" for i in range(_MAX_CONVERSATION_FETCH_WORKERS * 2)]
    metas = [_conv_meta(u, name=f"Chat {u}") for u in uuids]
    page = {
        "data": metas,
        "pagination": {"total": len(metas), "limit": 30, "offset": 0, "has_more": False},
    }
    mock_req.get.side_effect = [_mock_response(ORGS_RESPONSE), _mock_response(page)]

    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def fake_get(uuid):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return _conv_detail(uuid, name=f"Chat {uuid}")

    with patch.object(client.conversations, "get", side_effect=fake_get):
        client.conversations.pull(PROJECT_ID, tmp_path)

    assert 1 < peak <= _MAX_CONVERSATION_FETCH_WORKERS


# ------------------------------------------------------------ conversations: standalone


def _standalone_conv(uuid: str, name: str = "Standalone Chat") -> dict:
    """A chat_conversations list entry with no project_uuid (i.e. not in any project)."""
    return {**CONVERSATION_META[0], "uuid": uuid, "name": name, "project_uuid": None}


def _standalone_detail(uuid: str, name: str = "Standalone Chat") -> dict:
    return {**CONVERSATION_DETAIL, "uuid": uuid, "name": name}


@patch("claude_client._transport.requests")
def test_conversations_list_standalone_filters_out_project_scoped(mock_req, client):
    project_conv = CONVERSATION_META[0]  # has project_uuid set
    standalone_conv = _standalone_conv("standalone-uuid")
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response([project_conv, standalone_conv]),  # chat_conversations, short page
    ]

    convs = client.conversations.list_standalone()

    assert len(convs) == 1
    assert convs[0]["uuid"] == "standalone-uuid"


@patch("claude_client._transport.requests")
def test_conversations_list_standalone_paginates_until_short_page(mock_req, client):
    from claude_client.resources.conversations import _STANDALONE_PAGE_LIMIT

    full_page = [_standalone_conv(f"c{i}") for i in range(_STANDALONE_PAGE_LIMIT)]
    short_page = [_standalone_conv("last")]
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(full_page),
        _mock_response(short_page),
    ]

    convs = client.conversations.list_standalone()

    assert len(convs) == _STANDALONE_PAGE_LIMIT + 1
    assert mock_req.get.call_count == 3  # org lookup + 2 pages


@patch("claude_client._transport.requests")
def test_conversations_pull_standalone_created(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # chat_capable_org_ids()
        _mock_response([_standalone_conv("standalone-uuid")]),  # list_standalone, short page
        _mock_response(_standalone_detail("standalone-uuid")),  # get()
    ]

    results = client.conversations.pull_standalone(tmp_path)

    assert len(results) == 1
    assert results["standalone-chat-standalo.md"] == "created"


@patch("claude_client._transport.requests")
def test_conversations_pull_standalone_multi_org_shares_one_manifest(mock_req, client, tmp_path):
    """Each org's standalone chats land in the same flat directory, via a transport
    correctly pinned to that org — not the (possibly unscoped) outer `self`."""
    other_org = {"uuid": OTHER_ORG_ID, "capabilities": ["chat"], "name": "Other Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),  # chat_capable_org_ids()
        _mock_response([_standalone_conv("conv-a", "Chat A")]),  # org 1: list_standalone
        _mock_response(_standalone_detail("conv-a", "Chat A")),  # org 1: get()
        _mock_response([_standalone_conv("conv-b", "Chat B")]),  # org 2: list_standalone
        _mock_response(_standalone_detail("conv-b", "Chat B")),  # org 2: get()
    ]

    results = client.conversations.pull_standalone(tmp_path)

    assert len(results) == 2
    assert results["chat-a-conv-a.md"] == "created"
    assert results["chat-b-conv-b.md"] == "created"
    assert (tmp_path / MANIFEST_NAME).exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_standalone_multi_org_unpinned_client_does_not_raise(
    mock_req, client, tmp_path
):
    """Regression guard: pull_standalone must scope each org's get() through a pinned
    transport, not the outer client's — which may be unscoped on a multi-org account and
    would otherwise raise AmbiguousOrgError the moment a conversation is fetched."""
    other_org = {"uuid": OTHER_ORG_ID, "capabilities": ["chat"], "name": "Other Org"}
    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),
        _mock_response([_standalone_conv("conv-a")]),
        _mock_response(_standalone_detail("conv-a")),
        _mock_response([]),  # org 2: no standalone chats
    ]

    results = client.conversations.pull_standalone(tmp_path)

    assert len(results) == 1


@patch("claude_client._transport.requests")
def test_conversations_pull_standalone_prune_deletes_removed_conversation(
    mock_req, client, tmp_path
):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([_standalone_conv("standalone-uuid")]),
        _mock_response(_standalone_detail("standalone-uuid")),
    ]
    client.conversations.pull_standalone(tmp_path)

    mock_req.get.side_effect = [_mock_response([])]  # gone on the web now
    results = client.conversations.pull_standalone(tmp_path, prune=True)

    assert results["standalone-chat-standalo.md"] == "deleted"
    assert not (tmp_path / "standalone-chat-standalo.md").exists()


# ------------------------------------------------------------ conversations: attachments

IMAGE_UUID = "img12345-aaaa-bbbb-cccc-dddddddddddd"
IMAGE_BYTES = b"RIFF\x00\x00\x00\x00WEBPVP8 fake"
IMAGE_RELPATH = f"attachments/{CONV_UUID}/{IMAGE_UUID}-screenshot_2024.webp"
CONV_MD = "test-chat-conv-uui.md"


def _image_file(file_uuid: str = IMAGE_UUID, name: str = "Screenshot_2024.jpg") -> dict:
    return {
        "file_kind": "image",
        "file_uuid": file_uuid,
        "file_name": name,
        "size_bytes": 1014534,
        "thumbnail_url": f"/api/{ORG_ID}/files/{file_uuid}/thumbnail",
        "preview_url": f"/api/{ORG_ID}/files/{file_uuid}/preview",
    }


def _detail_with_files(files: list[dict], key: str = "files") -> dict:
    """CONVERSATION_DETAIL with `files` attached to the human (first) message."""
    first, *rest = CONVERSATION_DETAIL["chat_messages"]
    return {**CONVERSATION_DETAIL, "chat_messages": [{**first, key: files}, *rest]}


def _attachment_dispatch(
    detail: dict, *, page: dict = CONV_PAGE_RESPONSE, fail_files=False, forbid_files=False
):
    """A `requests.get` side_effect serving one conversation and its image previews."""
    preview_urls: list[str] = []

    def dispatch(url, **_kwargs):
        if url.endswith("/organizations"):
            return _mock_response(ORGS_RESPONSE)
        if "/conversations_v2" in url:
            return _mock_response(page)
        if "/chat_conversations/" in url:
            return _mock_response(detail)
        if "/files/" in url:
            preview_urls.append(url)
            if fail_files:
                raise RequestException("preview boom")
            if forbid_files:
                return _mock_response(
                    {"error": "x"}, status_code=403, headers={"content-type": "application/json"}
                )
            r = _mock_response(None, headers={"content-type": "image/webp"})
            r.content = IMAGE_BYTES
            return r
        raise AssertionError(f"unexpected URL in test: {url}")

    return dispatch, preview_urls


@patch("claude_client._transport.requests")
def test_conversations_pull_saves_image_and_links_it_inline(mock_req, client, tmp_path):
    dispatch, preview_urls = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert preview_urls == [f"https://claude.ai/api/{ORG_ID}/files/{IMAGE_UUID}/preview"]
    assert (tmp_path / IMAGE_RELPATH).read_bytes() == IMAGE_BYTES
    assert results[IMAGE_RELPATH] == "created"
    md = (tmp_path / CONV_MD).read_text()
    assert f"**Human**\nHello\n\n![Screenshot_2024.jpg]({IMAGE_RELPATH})\n" in md


@patch("claude_client._transport.requests")
def test_conversations_pull_skips_blob_files(mock_req, client, tmp_path):
    blob = {"file_kind": "blob", "file_uuid": "blob-uuid", "file_name": "notes.txt"}
    dispatch, preview_urls = _attachment_dispatch(_detail_with_files([blob]))
    mock_req.get.side_effect = dispatch

    client.conversations.pull(PROJECT_ID, tmp_path)

    assert preview_urls == []
    assert not (tmp_path / "attachments").exists()
    assert "notes.txt" not in (tmp_path / CONV_MD).read_text()


@patch("claude_client._transport.requests")
def test_conversations_pull_dedupes_image_listed_under_files_and_files_v2(
    mock_req, client, tmp_path
):
    first, *rest = CONVERSATION_DETAIL["chat_messages"]
    msg = {**first, "files": [_image_file()], "files_v2": [_image_file()]}
    dispatch, preview_urls = _attachment_dispatch(
        {**CONVERSATION_DETAIL, "chat_messages": [msg, *rest]}
    )
    mock_req.get.side_effect = dispatch

    client.conversations.pull(PROJECT_ID, tmp_path)

    assert len(preview_urls) == 1
    assert (tmp_path / CONV_MD).read_text().count(IMAGE_RELPATH) == 1


@patch("claude_client._transport.requests")
def test_conversations_pull_keeps_same_named_images_with_shared_uuid_prefix_apart(
    mock_req, client, tmp_path
):
    """Regression: file uuids are UUIDv7, so two images uploaded within the same ~65s
    share their first 8 chars. Same-named ones must still be saved as two files, each
    linked from its own message — not the second skipped as "already on disk"."""
    first_uuid = "019d58ee-1111-7000-8000-000000000001"
    second_uuid = "019d58ee-2222-7000-8000-000000000002"
    human, assistant = CONVERSATION_DETAIL["chat_messages"]
    second_human = {
        **human,
        "uuid": "human-2",
        "parent_message_uuid": assistant["uuid"],
        "content": [{"type": "text", "text": "and this one"}],
        "files": [_image_file(second_uuid, name="sc.jpg")],
    }
    detail = {
        **CONVERSATION_DETAIL,
        "chat_messages": [
            {**human, "files": [_image_file(first_uuid, name="sc.png")]},
            assistant,
            second_human,
        ],
    }

    def dispatch(url, **_kwargs):
        if url.endswith("/organizations"):
            return _mock_response(ORGS_RESPONSE)
        if "/conversations_v2" in url:
            return _mock_response(CONV_PAGE_RESPONSE)
        if "/chat_conversations/" in url:
            return _mock_response(detail)
        if "/files/" in url:
            r = _mock_response(None, headers={"content-type": "image/webp"})
            r.content = url.split("/files/")[1].split("/")[0].encode()  # bytes = file uuid
            return r
        raise AssertionError(f"unexpected URL in test: {url}")

    mock_req.get.side_effect = dispatch

    client.conversations.pull(PROJECT_ID, tmp_path)

    first_path = f"attachments/{CONV_UUID}/{first_uuid}-sc.webp"
    second_path = f"attachments/{CONV_UUID}/{second_uuid}-sc.webp"
    assert (tmp_path / first_path).read_bytes() == first_uuid.encode()
    assert (tmp_path / second_path).read_bytes() == second_uuid.encode()
    md = (tmp_path / CONV_MD).read_text()
    assert f"Hello\n\n![sc.png]({first_path})" in md
    assert f"and this one\n\n![sc.jpg]({second_path})" in md


@patch("claude_client._transport.requests")
def test_conversations_pull_never_redownloads_existing_image(mock_req, client, tmp_path):
    dispatch, preview_urls = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    # force re-fetches the conversation itself, but the image is already on disk
    results = client.conversations.pull(PROJECT_ID, tmp_path, force=True)

    assert len(preview_urls) == 1
    assert IMAGE_RELPATH not in results
    assert IMAGE_RELPATH in (tmp_path / CONV_MD).read_text()


@patch("claude_client._transport.requests")
def test_conversations_pull_unchanged_skip_keeps_attachments(mock_req, client, tmp_path):
    dispatch, preview_urls = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results == {CONV_MD: "unchanged"}
    assert len(preview_urls) == 1
    assert (tmp_path / IMAGE_RELPATH).exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_refetches_when_recorded_image_deleted_locally(
    mock_req, client, tmp_path
):
    dispatch, preview_urls = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)
    (tmp_path / IMAGE_RELPATH).unlink()

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results[IMAGE_RELPATH] == "created"
    assert len(preview_urls) == 2


@patch("claude_client._transport.requests")
def test_conversations_pull_refetches_entry_from_before_attachment_support(
    mock_req, client, tmp_path
):
    """A manifest written by an older client has no attachments key: the conversation
    must be fetched once more so its images get backed up, not skipped as unchanged."""
    (tmp_path / CONV_MD).write_text("old render")
    old_manifest = {CONV_UUID: {"filename": CONV_MD, "updated_at": "2024-01-02T11:00:00Z"}}
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(old_manifest))
    dispatch, _ = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results[CONV_MD] == "updated"
    assert results[IMAGE_RELPATH] == "created"


@patch("claude_client._transport.requests")
def test_conversations_pull_image_failure_is_reported_and_retried_next_run(
    mock_req, client, tmp_path
):
    detail = _detail_with_files([_image_file()])
    dispatch, _ = _attachment_dispatch(detail, fail_files=True)
    mock_req.get.side_effect = dispatch

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    stem = IMAGE_RELPATH.removesuffix(".webp")
    assert results[stem] == "failed"
    assert results[CONV_MD] == "created"  # the conversation itself still lands
    assert "[Image not downloaded: Screenshot_2024.jpg]" in (tmp_path / CONV_MD).read_text()

    dispatch, preview_urls = _attachment_dispatch(detail)
    mock_req.get.side_effect = dispatch
    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert len(preview_urls) == 1  # not skipped as unchanged: the image is retried
    assert results[IMAGE_RELPATH] == "created"
    assert results[CONV_MD] == "updated"


@patch("claude_client._transport.requests")
def test_conversations_pull_image_403_fails_only_that_image(mock_req, client, tmp_path):
    """A 403 makes the transport raise AuthError, not RequestException; for one image's
    preview that must still be a per-image failure, not abort the whole pull."""
    dispatch, _ = _attachment_dispatch(_detail_with_files([_image_file()]), forbid_files=True)
    mock_req.get.side_effect = dispatch

    results = client.conversations.pull(PROJECT_ID, tmp_path)

    assert results[IMAGE_RELPATH.removesuffix(".webp")] == "failed"
    assert results[CONV_MD] == "created"
    assert (tmp_path / MANIFEST_NAME).exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_prune_removes_attachments_of_deleted_conversation(
    mock_req, client, tmp_path
):
    dispatch, _ = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    empty_conv_page = {
        "data": [],
        "pagination": {"total": 0, "limit": 30, "offset": 0, "has_more": False},
    }
    dispatch, _ = _attachment_dispatch({}, page=empty_conv_page)
    mock_req.get.side_effect = dispatch
    results = client.conversations.pull(PROJECT_ID, tmp_path, prune=True)

    assert results[f"attachments/{CONV_UUID}"] == "deleted"
    assert not (tmp_path / "attachments" / CONV_UUID).exists()
    assert CONV_UUID not in json.loads((tmp_path / MANIFEST_NAME).read_text())


@patch("claude_client._transport.requests")
def test_conversations_pull_without_prune_keeps_attachments_of_deleted_conversation(
    mock_req, client, tmp_path
):
    dispatch, _ = _attachment_dispatch(_detail_with_files([_image_file()]))
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    empty_conv_page = {
        "data": [],
        "pagination": {"total": 0, "limit": 30, "offset": 0, "has_more": False},
    }
    dispatch, _ = _attachment_dispatch({}, page=empty_conv_page)
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    assert (tmp_path / IMAGE_RELPATH).exists()


@patch("claude_client._transport.requests")
def test_conversations_pull_rename_with_prune_keeps_attachments(mock_req, client, tmp_path):
    detail = _detail_with_files([_image_file()])
    dispatch, _ = _attachment_dispatch(detail)
    mock_req.get.side_effect = dispatch
    client.conversations.pull(PROJECT_ID, tmp_path)

    renamed_meta = {**CONVERSATION_META[0], "name": "Renamed", "updated_at": "2024-02-01"}
    page = {**CONV_PAGE_RESPONSE, "data": [renamed_meta]}
    dispatch, preview_urls = _attachment_dispatch({**detail, "name": "Renamed"}, page=page)
    mock_req.get.side_effect = dispatch
    results = client.conversations.pull(PROJECT_ID, tmp_path, prune=True)

    assert results[CONV_MD] == "deleted"
    assert preview_urls == []
    assert (tmp_path / IMAGE_RELPATH).exists()
    assert IMAGE_RELPATH in (tmp_path / "renamed-conv-uui.md").read_text()
    assert CONV_UUID in json.loads((tmp_path / MANIFEST_NAME).read_text())


@patch("claude_client._transport.requests")
def test_conversations_pull_standalone_saves_attachments(mock_req, client, tmp_path):
    detail = {**_detail_with_files([_image_file()]), "name": "Standalone Chat"}
    preview = _mock_response(None, headers={"content-type": "image/webp"})
    preview.content = IMAGE_BYTES
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([_standalone_conv(CONV_UUID)]),
        _mock_response(detail),
        preview,
    ]

    results = client.conversations.pull_standalone(tmp_path)

    assert results[IMAGE_RELPATH] == "created"
    assert (tmp_path / IMAGE_RELPATH).read_bytes() == IMAGE_BYTES


def test_conversation_to_markdown_without_attachment_map_omits_images():
    md = conversation_to_markdown(_detail_with_files([_image_file()]))

    assert md == conversation_to_markdown(CONVERSATION_DETAIL)


def test_conversation_to_markdown_renders_image_only_message():
    first, *rest = CONVERSATION_DETAIL["chat_messages"]
    msg = {**first, "content": [], "files": [_image_file()]}
    conv = {**CONVERSATION_DETAIL, "chat_messages": [msg, *rest]}

    md = conversation_to_markdown(conv, {IMAGE_UUID: "attachments/x/img.webp"})

    assert "**Human**\n![Screenshot_2024.jpg](attachments/x/img.webp)\n" in md


# --------------------------------------------------------------------- projects
# (composite: pull / pull_all, which pull docs + conversations + memory together)


@patch("claude_client._transport.requests")
def test_projects_export_data_does_not_fetch_docs_individually(mock_req, client):
    """docs.list() already returns full content per doc, so export_data must not call
    docs.get() per doc — that would be the same N+1 pattern already fixed in
    DocsResource.pull. No `.../docs/{doc_id}` GET should appear here at all."""
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response(PROJECT_RESPONSE),  # get_project
        _mock_response([DOC_FULL]),  # list_docs (content included)
        _mock_response(MEMORY_RESPONSE),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]

    export = client.projects.export_data(PROJECT_ID)

    assert export.docs == [DOC_FULL]
    assert mock_req.get.call_count == 6  # no extra per-doc GET beyond the ones listed above


@patch("claude_client._transport.requests")
def test_projects_pull_raises_ambiguous_on_multi_org_unpinned_account(mock_req, client, tmp_path):
    """The composite path (pull/export/export_data, all built on projects.get) is
    scoped the same way as the direct resource methods — an unpinned multi-org
    client must refuse rather than pull the wrong org's project."""
    mock_req.get.return_value = _mock_response(MULTI_ORGS_RESPONSE)
    with pytest.raises(AmbiguousOrgError):
        client.projects.pull(PROJECT_ID, tmp_path / "export")


@patch("claude_client._transport.requests")
def test_projects_pull(mock_req, client, tmp_path):
    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # org_id
        _mock_response(PROJECT_RESPONSE),  # get_project
        _mock_response(MEMORY_RESPONSE),  # get_memory
        _mock_response([DOC_FULL]),  # list_docs (content included)
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
        _mock_response([DOC_FULL]),
        _mock_response(CONV_PAGE_RESPONSE),
        _mock_response(CONVERSATION_DETAIL),
    ]
    mock_req.get.side_effect = responses
    client.projects.pull(PROJECT_ID, tmp_path / "export")

    # Second pull: identical content on the web side.
    mock_req.get.side_effect = [
        _mock_response(PROJECT_RESPONSE),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([DOC_FULL]),
        _mock_response(CONV_PAGE_RESPONSE),
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

    assert results["Project A"].ok is True
    assert results["Project B"].ok is True
    assert (tmp_path / "project-a" / "project.md").exists()
    assert (tmp_path / "project-b" / "project.md").exists()


@patch("claude_client.resources.projects.Progress")
@patch("claude_client._transport.requests")
def test_projects_pull_all_uses_one_shared_progress_with_per_project_labels(
    mock_req, mock_progress_cls, client, tmp_path
):
    """Regression guard for the progress-bar-accumulation bug: `pull_all` must open
    exactly one `Progress` for the whole run (not one per project/docs/conversations
    call), and each project's tasks must be labeled with its name."""
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([project_a, project_b]),
        _mock_response(project_a),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    mock_progress = mock_progress_cls.return_value.__enter__.return_value

    client.projects.pull_all(tmp_path)

    # One Progress context for the entire run — not one per project.
    mock_progress_cls.assert_called_once()
    labels = [call.args[0] for call in mock_progress.add_task.call_args_list]
    assert any("Project A" in label for label in labels)
    assert any("Project B" in label for label in labels)


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

    assert results["Project A"].ok is False
    assert isinstance(results["Project A"].error, RequestException)
    assert results["Project B"].ok is True
    assert results["Project B"].error is None
    assert (tmp_path / "project-b" / "project.md").exists()
    # pull() creates the output dir before its first API call, so project-a's dir may
    # exist, but it must be empty — the failure happened before anything was written.
    assert not (tmp_path / "project-a" / "project.md").exists()


@patch("claude_client._transport.requests")
def test_projects_pull_all_surfaces_exception_per_project(mock_req, client, tmp_path):
    """The exact exception instance raised for a failed project must reach the
    caller through `ProjectPullResult.error`, so a caller's own retry/backoff wrapper
    can inspect it (e.g. distinguish a timeout from a 4xx/5xx/auth failure)."""
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}
    boom = RequestException("boom")

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([project_a, project_b]),
        boom,  # pull proj-a: get_project raises
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]

    results = client.projects.pull_all(tmp_path)

    assert results["Project A"].error is boom
    assert results["Project B"].error is None


@patch("claude_client._transport.requests")
def test_projects_pull_all_prune_removes_deleted_project_dir(mock_req, client, tmp_path):
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([project_a, project_b]),
        _mock_response(project_a),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    client.projects.pull_all(tmp_path)
    assert (tmp_path / "project-a").exists()
    assert (tmp_path / "project-b").exists()

    # Project A no longer exists on the web.
    mock_req.get.side_effect = [
        _mock_response([project_b]),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    results = client.projects.pull_all(tmp_path, prune=True)

    assert results["Project B"].ok is True
    assert not (tmp_path / "project-a").exists()
    assert (tmp_path / "project-b").exists()


@patch("claude_client._transport.requests")
def test_projects_pull_all_does_not_prune_dir_of_failed_project(mock_req, client, tmp_path):
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([project_a, project_b]),
        _mock_response(project_a),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    client.projects.pull_all(tmp_path)

    # Both projects still listed remotely, but Project A's own pull fails this run.
    mock_req.get.side_effect = [
        _mock_response([project_a, project_b]),
        RequestException("boom"),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    results = client.projects.pull_all(tmp_path, prune=True)

    assert results["Project A"].ok is False
    assert results["Project B"].ok is True
    assert (tmp_path / "project-a").exists()  # never pruned despite prune=True


@patch("claude_client._transport.requests")
def test_projects_pull_all_tracks_project_on_first_ever_pull_failure(mock_req, client, tmp_path):
    """A project whose very first pull fails must still get a manifest entry (its dir
    was already created by pull's mkdir), so a later run can prune it once it's
    confirmed gone remotely — otherwise it's an untracked, unprunable orphan forever."""
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),
        _mock_response([project_a]),
        RequestException("boom"),  # first-ever pull of proj-a fails
    ]
    results = client.projects.pull_all(tmp_path)
    assert results["Project A"].ok is False

    # Project A is now genuinely gone from the web.
    mock_req.get.side_effect = [_mock_response([])]
    client.projects.pull_all(tmp_path, prune=True)

    assert not (tmp_path / "project-a").exists()


@patch("claude_client._transport.requests")
def test_projects_pull_all_disambiguates_slug_collision(mock_req, client, tmp_path):
    """Two distinct projects (different orgs) with the same name must not write into
    the same directory — that would silently mix their docs/conversations together."""
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    project_a = {"uuid": "proj-a", "name": "Project X", "description": "A", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project X", "description": "B", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),
        _mock_response([project_a]),
        _mock_response([project_b]),
        _mock_response(project_a),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    client.projects.pull_all(tmp_path)

    assert not (tmp_path / "project-x").exists()  # bare slug never written — ambiguous
    dir_a = tmp_path / "project-x-proj-a"
    dir_b = tmp_path / "project-x-proj-b"
    assert dir_a.exists() and dir_b.exists()
    assert "A" in (dir_a / "project.md").read_text()
    assert "B" in (dir_b / "project.md").read_text()


@patch("claude_client._transport.requests")
def test_projects_pull_all_prune_does_not_delete_live_project_sharing_a_slug(
    mock_req, client, tmp_path
):
    other_org = {"uuid": "other-org", "capabilities": ["chat"], "name": "Other Org"}
    project_a = {"uuid": "proj-a", "name": "Project X", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project X", "description": "", "prompt_template": ""}
    empty_conv_page = {"data": [], "pagination": {"has_more": False}}

    mock_req.get.side_effect = [
        _mock_response([*ORGS_RESPONSE, other_org]),
        _mock_response([project_a]),
        _mock_response([project_b]),
        _mock_response(project_a),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    client.projects.pull_all(tmp_path)

    # proj-a deleted on the web; proj-b (same name, different org) still live.
    # (chat_capable_org_ids() is cached on the transport, so no re-fetch of orgs here.)
    mock_req.get.side_effect = [
        _mock_response([]),  # org 1: proj-a no longer listed
        _mock_response([project_b]),  # org 2: proj-b still there
        _mock_response(project_b),
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(empty_conv_page),
    ]
    client.projects.pull_all(tmp_path, prune=True)

    assert not (tmp_path / "project-x-proj-a").exists()
    assert (tmp_path / "project-x-proj-b").exists()


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
