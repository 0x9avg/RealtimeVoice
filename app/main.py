"""FastAPI bridge between browsers and the one shared Gemini Live session.

There is exactly ONE `SessionHub` (app/session_hub.py) for the life of this
process, started at server startup and kept alive regardless of how many
browsers are connected. `/ws/session` is a thin subscribe/publish layer over
it: every connected browser sees the same transcript/tool log and controls
the same conversation (text, mic on/off, mute) — open the site on two
devices and both show the same page, because there both looking at the same
session, not each getting their own. Binary WS frames carry raw PCM16 audio
in both directions; JSON text frames carry control/status messages. See
`web-src/src/lib/useVoiceSession.ts` for the browser side of this protocol
(built into `web/`, which this module serves as a static SPA — see
`spa_fallback` below).
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api_routes import router as api_router
from app.config import settings
from app.session_hub import SessionHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("realtimevoice")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

session_hub = SessionHub(settings)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await session_hub.start()
    try:
        yield
    finally:
        await session_hub.stop()


app = FastAPI(title="RealtimeVoice", lifespan=_lifespan)
app.include_router(api_router)


@app.websocket("/ws/session")
async def session(ws: WebSocket) -> None:
    await ws.accept()
    # Same secret/rule as app/auth.py's require_token (REST endpoints) —
    # empty settings.access_token means "no token configured", no-op, same
    # as everywhere else. A WS handshake can't carry an Authorization
    # header from browser JS, so this is query-param only (?token=...),
    # same as the camera <img> stream. This is the ONE actuator surface
    # that used to have NO auth check at all: anyone on the same network
    # could open this socket directly (bypassing the page entirely) and
    # talk to/control the robot — mute it, drive the wall, run visit_intake
    # — without ever touching the UI. Close with a distinct 4401 code so
    # the browser side (useVoiceSession.ts) can tell "wrong/missing token"
    # apart from an ordinary disconnect and prompt for one.
    # hmac.compare_digest, not != — see app/auth.py's require_token for why
    # a plain string comparison is a timing side channel.
    if settings.access_token and not hmac.compare_digest(
        ws.query_params.get("token", ""), settings.access_token
    ):
        await ws.close(code=4401, reason="unauthorized")
        return
    await session_hub.join(ws)
    # This one browser tab's own choice of input route — "browser" (its own
    # mic, captured client-side) or not. Per-connection on purpose: unlike
    # the robot's mic (a shared piece of hardware — see
    # app/robot_mic_state.py), a browser's own mic is inherently local to
    # that one tab, so there's nothing global to synchronise here.
    mic_source = "none"
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                _LOG.info("browser.disconnect code=%s", message.get("code"))
                return
            data = message.get("bytes")
            if data is not None:
                if mic_source == "browser":
                    await session_hub.handle_browser_audio(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "text":
                await session_hub.handle_text(str(payload.get("text", "")))
            elif payload.get("type") == "mic_source":
                source = str(payload.get("source", "none"))
                if source in ("browser", "robot", "none"):
                    mic_source = source
                    await session_hub.handle_mic_source(source)
            elif payload.get("type") == "mute":
                await session_hub.handle_mute()
    finally:
        session_hub.leave(ws)


app.mount("/assets", StaticFiles(directory=_WEB_DIR / "assets"), name="web-assets")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str) -> FileResponse:
    """React Router does client-side routing (/wall, /guard aren't real
    files) — StaticFiles(html=True) alone can't serve index.html for those,
    only for `/`. Registered last so it never shadows /ws/session or the
    /api/* router above; falls back to index.html for anything that isn't a
    real static asset (e.g. capture-worklet.js, served from web-src/public/
    verbatim by the Vite build).
    """
    candidate = _WEB_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(_WEB_DIR / "index.html")
