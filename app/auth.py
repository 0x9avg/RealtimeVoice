"""Bearer-token gate for the operator-panel endpoints (app/api_routes.py).

G1 ТЗ §7: "Доступ к административным функциям предоставляется только
авторизованным пользователям." A single shared token is proportionate to a
test bench — no user accounts, no sessions, just a secret the operator
pastes into the browser once (see web-src/src/lib/authPrompt.tsx +
api/client.ts). Empty `settings.access_token` (the default) disables the
check entirely, so nothing here gets in the way until a deployment actually
sets one.

Accepts the token two ways: `Authorization: Bearer <token>` (what
`api/client.ts`'s fetch wrapper sends) or `?token=<token>` — a plain
`<img src="/api/camera/stream?token=...">` can't set headers at all, same
reasoning as humanoid-robot's own camera bridge auth check.

`/ws/session` (app/main.py) checks the SAME `settings.access_token` — via
`?token=`, same as the camera stream, since a WS handshake can't carry an
Authorization header from browser JS either — but doesn't use this
`require_token` dependency, since FastAPI's WS route decorator doesn't take
`Depends()` gates the same way; see the inline check right after
`ws.accept()` in app/main.py's `session()`. Worth remembering: that WS
endpoint is the actual "control the robot" surface (mute, mic, text,
everything voice-driven goes through it) — it is at least as sensitive as
anything gated here, more so on a shared/venue Wi-Fi network.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query

from app.config import settings


def token_matches(supplied: str, expected: str) -> bool:
    """Constant-time token comparison, safe for arbitrary caller input.

    hmac.compare_digest, not ==: a plain string comparison short-circuits on
    the first mismatched character, so how long a wrong guess survives leaks
    how many leading characters it got right — a timing side channel an
    attacker can use to brute-force the token one character at a time.

    The .encode() is equally load-bearing: compare_digest REFUSES str
    arguments containing non-ASCII (raises TypeError), and both the
    `?token=` query param and the Authorization header carry whatever bytes
    the caller sends. Comparing the str forms directly turned any request
    with a non-ASCII token into an unhandled 500 (and, on /ws/session, a
    crashed handler) instead of a plain 401.
    """
    return hmac.compare_digest(
        supplied.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def require_token(authorization: str = Header(default=""), token: str = Query(default="")) -> None:
    if not settings.access_token:
        return
    if token_matches(authorization, f"Bearer {settings.access_token}") or token_matches(
        token, settings.access_token
    ):
        return
    raise HTTPException(status_code=401, detail="unauthorized")
