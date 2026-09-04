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

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api_routes import router as api_router
from app.auth import token_matches
from app.config import settings
from app.session_hub import SessionHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("realtimevoice")

_TOKEN_IN_URL_RE = re.compile(r"(token=)[^&\s\"']+")


class _RedactTokenFilter(logging.Filter):
    """Keep ACCESS_TOKEN out of the logs.

    `/ws/session` and `/api/camera/*` take the token as a query parameter
    (a browser WebSocket/<img> can't send an Authorization header), and
    uvicorn's access log writes the full request line — so every operator
    connection was printing a working credential into journald verbatim,
    where anyone with shell access or a copy of the logs could lift it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _TOKEN_IN_URL_RE.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL_RE.sub(r"\1<redacted>", record.msg)
        return True


for _name in ("uvicorn.access", "uvicorn.error", "realtimevoice"):
    logging.getLogger(_name).addFilter(_RedactTokenFilter())

_WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()

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
    # token_matches (app/auth.py), not != — constant-time, and safe against
    # the non-ASCII input that would otherwise crash this handler.
    if settings.access_token and not token_matches(
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
                if source in ("browser", "robot", "usb", "none"):
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

    The resolve()+relative_to() containment check is load-bearing, not
    defensive dressing: `_WEB_DIR / full_path` alone happily walks out of
    web/ when the path contains `..` (percent-encoded `..%2f` survives
    routing and lands in `full_path` verbatim), which turned this route
    into an unauthenticated "read any file the process can read" endpoint —
    `.env` (API key + ACCESS_TOKEN) included. resolve() also collapses
    symlinks, so a link inside web/ can't point out either.
    """
    index = _WEB_DIR / "index.html"
    if not full_path:
        return FileResponse(index)
    candidate = (_WEB_DIR / full_path).resolve()
    if not candidate.is_relative_to(_WEB_DIR):
        return FileResponse(index)
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(index)
