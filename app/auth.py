"""Bearer-token gate for the operator-panel endpoints (app/api_routes.py).

G1 ТЗ §7: "Доступ к административным функциям предоставляется только
авторизованным пользователям." A single shared token is proportionate to a
test bench — no user accounts, no sessions, just a fetch-time secret the
operator pastes into the browser once (see web/auth.js). Empty
`settings.access_token` (the default) disables the check entirely, so
nothing here gets in the way until a deployment actually sets one.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings


def require_token(authorization: str = Header(default="")) -> None:
    if not settings.access_token:
        return
    expected = f"Bearer {settings.access_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
