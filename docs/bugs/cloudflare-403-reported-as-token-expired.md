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

`_check_auth` (`claude_client/_transport.py`) now checks a `403` in this order:

1. **Cloudflare evidence first** (`cf-mitigated` header, or `server: cloudflare`
   + a challenge-body marker) → `CloudflareChallengeError`, confident message
   naming the VPN. Checked before the JSON-body test because Cloudflare's own
   WAF block page can itself be JSON-shaped — without this ordering, a
   Cloudflare-intervened response with a JSON body would slip past as
   "app-layer" and reproduce the exact misdiagnosis this fix exists for.
2. Else, **positively identify the app layer** (`content-type:
   application/json` + a parseable dict body) → `AuthError`, but with a
   *hedged* message: a 403 JSON body from claude.ai means either an
   invalid/expired token or a real permission error (e.g. the tracked
   `org-scoping-resource-methods.md` bug), and the body shape alone can't
   distinguish them — so the message no longer claims "expired"
   unconditionally the way the `401` message does.
3. Else (no Cloudflare evidence, doesn't look like the app layer) →
   `CloudflareChallengeError` again, but with a message that only claims "not
   a token problem" — it does *not* confidently blame Cloudflare specifically,
   since an nginx/other-WAF 403 would otherwise get a confident wrong
   diagnosis.

`CloudflareChallengeError` subclasses `AuthError`, so existing `except
AuthError` callers — including `claude-web-backup` — keep working, just with
corrected messages. A `401` is always `AuthError` unconditionally — Cloudflare
doesn't challenge with 401. `503`/`429` use the Cloudflare-evidence test only
(no JSON-body branch), since those codes have legitimate non-Cloudflare causes
too and aren't the incident this bug tracks.

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
