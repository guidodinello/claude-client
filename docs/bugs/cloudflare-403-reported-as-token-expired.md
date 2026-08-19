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

## Fix options, best first

**A. Detect the Cloudflare challenge specifically and raise a distinct error.**
Check `resp.headers.get("server") == "cloudflare"` combined with either a `403`
lacking claude.ai's normal JSON error shape, or a body sniff for the challenge
page (`cf-mitigated` header, or `<title>Just a moment` in the HTML). Raise a new
`CloudflareChallengeError(AppError)` (or similar) with a message that doesn't
tell the user to refresh their token — something like "Request was blocked by
Cloudflare before reaching claude.ai; this is not a token problem." This is the
only fix that actually stops the misdiagnosis.

**B. Only ever collapse a real `401` into `AuthError`; treat `403` as a distinct,
more cautiously-worded error** even without full Cloudflare detection — a `403`
is "forbidden", not necessarily "expired," and conflating them already loses
information regardless of Cloudflare.

**C. Keep an escape hatch for manual diagnosis.** Even with A/B done, it's worth
keeping (or exposing publicly) a low-level path that skips `_check_auth` and
returns the raw response, so a caller stuck on a misleading error can always
get the real status/headers/body without monkeypatching `_transport.py` — that's
what had to happen to actually diagnose this incident.

## Status

Tracked, not yet fixed.
