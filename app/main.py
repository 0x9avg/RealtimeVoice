"""FastAPI bridge between the browser and one Gemini Live session.

One WebSocket connection (`/ws/session`) == one `GeminiLiveAdapter` session.
Binary WS frames carry raw PCM16 audio in both directions; JSON text frames
carry control/status messages. See `web/app.js` for the browser side of this
protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.gemini_live import (
    AudioChunk,
    ConnectionStateChanged,
    GeminiLiveAdapter,
    Interrupted,
    ToolCall,
    Transcript,
    TurnComplete,
)
from app.tools import GetCurrentTimeTool, ToolRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("prototype")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="RealtimeVoice prototype")


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetCurrentTimeTool(default_timezone=settings.default_timezone))
    return registry


@app.websocket("/ws/session")
async def session(ws: WebSocket) -> None:
    await ws.accept()

    api_key = settings.resolved_api_key()
    if not api_key:
        await ws.send_json(
            {
                "type": "status",
                "state": "error",
                "detail": f"no Gemini API key set: export {settings.api_key_env}",
            }
        )
        await ws.close()
        return

    tools = build_tool_registry()
    adapter = GeminiLiveAdapter(settings=settings, tools=tools)

    async def forward_from_browser() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                await adapter.send_audio(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "text":
                await adapter.send_text(str(payload.get("text", "")))

    async def forward_from_gemini() -> None:
        async for event in adapter.events():
            match event:
                case AudioChunk(pcm=pcm):
                    await ws.send_bytes(pcm)
                case ConnectionStateChanged(state=state, detail=detail):
                    await ws.send_json({"type": "status", "state": state, "detail": detail})
                case TurnComplete():
                    await ws.send_json({"type": "turn_complete"})
                case Interrupted():
                    await ws.send_json({"type": "interrupted"})
                case Transcript(role=role, text=text):
                    await ws.send_json({"type": "transcript", "role": role, "text": text})
                case ToolCall() as call:
                    _LOG.info("tool_call name=%s args=%s", call.name, call.args)
                    await ws.send_json(
                        {"type": "tool_call", "name": call.name, "args": call.args}
                    )
                    result = await tools.dispatch(call.name, call.args)
                    await call.respond(result.text)
                    await ws.send_json(
                        {
                            "type": "tool_result",
                            "name": call.name,
                            "ok": result.ok,
                            "text": result.text,
                        }
                    )

    browser_task = asyncio.create_task(forward_from_browser(), name="ws-from-browser")
    gemini_task = asyncio.create_task(forward_from_gemini(), name="ws-from-gemini")
    try:
        done, _pending = await asyncio.wait(
            {browser_task, gemini_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                _LOG.warning("session.task_ended: %s", exc)
    finally:
        for task in (browser_task, gemini_task):
            task.cancel()
        for task in (browser_task, gemini_task):
            # CancelledError is a BaseException, not an Exception — it must be
            # suppressed explicitly or it propagates out of this handler and
            # gets logged as an unhandled error on every normal disconnect.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await adapter.close()


app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
