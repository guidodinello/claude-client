"""HTTP + auth core for talking to the Claude.ai web API.

Knows about sessions, headers, org resolution, and raw HTTP verbs. Knows nothing
about docs, projects, or conversations — that's the resource classes in `resources/`.
"""

import json
import os
from functools import cached_property
from http import HTTPStatus

from curl_cffi import requests
from logger import get_logger

from .exceptions import AuthError
from .models import OrgDict

logger = get_logger(__name__)

BASE_URL = "https://claude.ai/api"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
_IMPERSONATE = "chrome110"
_DEFAULT_TIMEOUT = 30  # seconds; guards against a hung connection blocking forever


class Transport:
    """Owns the session token and does raw HTTP + org resolution."""

    def __init__(self, session_token: str | None = None, *, org_id: str | None = None) -> None:
        token = session_token or os.getenv("CLAUDE_SESSION_TOKEN")
        if not token:
            raise ValueError("Session token required. Pass it or set CLAUDE_SESSION_TOKEN.")
        self._session_token = token
        self._cookie = f"sessionKey={token}"
        if org_id is not None:
            # Shadows the `org_id` cached_property: it's a non-data descriptor, so an
            # entry in the instance __dict__ takes precedence over it.
            self.__dict__["org_id"] = org_id

    # ------------------------------------------------------------------ auth

    @property
    def session_token(self) -> str:
        return self._session_token

    def update_token(self, session_token: str) -> None:
        self._session_token = session_token
        self._cookie = f"sessionKey={session_token}"
        self.__dict__.pop("org_id", None)
        self.__dict__.pop("_org_ids", None)

    def scoped(self, org_id: str) -> "Transport":
        """A transport sharing this account's token but pinned to a specific org."""
        return Transport(self._session_token, org_id=org_id)

    # ------------------------------------------------------------------- raw

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://claude.ai/chats",
            "Content-Type": "application/json",
            "Origin": "https://claude.ai",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": self._cookie,
        }

    def get(self, url: str) -> requests.Response:
        resp = requests.get(
            url, headers=self._headers(), impersonate=_IMPERSONATE, timeout=_DEFAULT_TIMEOUT
        )
        self._check_auth(resp)
        resp.raise_for_status()
        return resp

    def post(self, url: str, payload: dict) -> requests.Response:
        resp = requests.post(
            url,
            headers=self._headers(),
            data=json.dumps(payload),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        return resp

    def put(self, url: str, payload: dict) -> requests.Response:
        resp = requests.put(
            url,
            headers=self._headers(),
            data=json.dumps(payload),
            impersonate=_IMPERSONATE,
            timeout=_DEFAULT_TIMEOUT,
        )
        self._check_auth(resp)
        return resp

    def delete(self, url: str) -> requests.Response:
        resp = requests.delete(
            url, headers=self._headers(), impersonate=_IMPERSONATE, timeout=_DEFAULT_TIMEOUT
        )
        self._check_auth(resp)
        resp.raise_for_status()
        return resp

    def _check_auth(self, resp: requests.Response) -> None:
        if resp.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthError(
                "Session token is invalid or expired. Refresh CLAUDE_SESSION_TOKEN from claude.ai."
            )

    # ------------------------------------------------------------------- org

    def list_organizations(self) -> list[OrgDict]:
        resp = self.get(f"{BASE_URL}/organizations")
        return resp.json()

    @cached_property
    def _org_ids(self) -> list[str]:
        """Every org uuid on this account with 'chat' or 'claude_pro' capabilities.

        Cached: an account's org membership doesn't change within a client's lifetime,
        and this backs both `org_id` and `chat_capable_org_ids()` — without caching,
        checking the org count before resolving `org_id` fetches the org list twice.
        """
        return [
            str(org["uuid"])
            for org in self.list_organizations()
            if "chat" in org.get("capabilities", []) or "claude_pro" in org.get("capabilities", [])
        ]

    def chat_capable_org_ids(self) -> list[str]:
        return list(self._org_ids)

    @cached_property
    def org_id(self) -> str:
        if not self._org_ids:
            raise ValueError("No org found with 'chat' or 'claude_pro' capabilities.")
        return self._org_ids[0]
