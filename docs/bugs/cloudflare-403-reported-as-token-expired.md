# Cloudflare 403 challenge is misreported as "session token invalid or expired"

Found while debugging a `claude-web-backup` nightly failure (2026-08-18). The
systemd run failed with `AuthError: Session token is invalid or expired.` The user
pasted a fresh `sessionKey` from a live, logged-in browser session and reran —
same error, every time.

## The issue

`Transport._check_auth` (`claude_client/_transport.py`) collapses two very
different failure modes into one message:

```python
def _check_auth(self, resp: requests.Response) -> None:
    if resp.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise AuthError(
            "Session token is invalid or expired. Refresh CLAUDE_SESSION_TOKEN from claude.ai."
        )
```

A `403` from claude.ai's app layer (token really is bad) and a `403` from
**Cloudflare** in front of it (bot/JS challenge, token never even evaluated) hit
the exact same branch and produce the exact same error text.

Confirmed by bypassing `_check_auth` and inspecting the raw response for the
`claude-web-backup` failure: `status=403`, `server: cloudflare`, `cf-ray` present,
body is Cloudflare's `Just a moment...` JS-challenge HTML — not a claude.ai
response at all. The session token was valid; re-pasting it repeatedly could never
have fixed anything, since the request never reached the app layer.

## Who hits it

Anyone running `claude-client` unattended (the `claude-web-backup` nightly
systemd timer is the concrete case) who happens to trip Cloudflare's bot
scoring — e.g. `curl_cffi`'s `chrome110` impersonation profile drifting out of
date, IP reputation, or a Cloudflare policy change. They get sent on a wild goose
chase re-issuing tokens for a problem that isn't token-shaped at all.

**Root cause confirmed for this incident (2026-08-19):** the machine was on a
VPN. Same token, same code, VPN off → `200` immediately; VPN on → `403`
Cloudflare challenge every time. VPN exit IPs commonly carry poor Cloudflare
reputation scores, which is enough on its own to trigger the JS challenge
regardless of TLS/browser fingerprinting. Worth checking VPN state first the
next time this error shows up, before re-issuing tokens.

## Fix shipped

The header-gate in option A below turned out to be unsafe as written: claude.ai
is itself served through Cloudflare, so `server: cloudflare` / `cf-ray` are
present on **legitimate** app-layer responses too, including a genuine expired
token. Gating on them would have flipped which case gets misdiagnosed, not
fixed it — confirmed by capturing a live invalid-token 403 through the real
client (`curl_cffi`, `impersonate="chrome110"`), which came back with
`server: cloudflare` in the headers and a normal claude.ai JSON error body.

`_check_auth` (`claude_client/_transport.py`) now uses the inverse test for a
`403`: **positively identify the app layer** (`content-type: application/json`
+ a parseable dict body) → `AuthError`; anything else → `CloudflareChallengeError`
(a subclass of `AuthError`, so existing `except AuthError` callers — including
`claude-web-backup` — keep working, just with the corrected message). This
fails in the safe direction: an unrecognized response never claims the token
expired. A `401` is always `AuthError` unconditionally — Cloudflare doesn't
challenge with 401. `503`/`429` use the opposite test (positively identify
Cloudflare via `cf-mitigated` or `server: cloudflare` + a challenge-body
marker), since those codes have legitimate non-Cloudflare causes too.

The new message names the VPN explicitly, since that's the confirmed trigger:
"Request was blocked by Cloudflare before reaching claude.ai — this is not a
token problem... If you are on a VPN, try disabling it."

Regression fixture: `tests/fixtures/cloudflare_challenge.html`, the full,
live-captured Cloudflare JS challenge response body (bare `curl`, no browser
impersonation, tripped the challenge even without a VPN — impersonated
`curl_cffi` requests did not, consistent with TLS-fingerprint/IP-reputation
being the trigger).

Not shipped: option C (raw-response escape hatch) — no natural home in the
current design since all four HTTP verbs call `_check_auth` unconditionally,
so it would be a new public method rather than a flag; left for a separate PR
if it's ever needed again.

## Status

Fixed.
