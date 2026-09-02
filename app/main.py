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
from app.robot_audio import HalfDuplexGate, RobotMic, RobotSpeaker
from app.tools import ControlVideoWallTool, GetCurrentTimeTool, ToolRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("prototype")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="RealtimeVoice prototype")


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetCurrentTimeTool(default_timezone=settings.default_timezone))
    registry.register(
        ControlVideoWallTool(
            base_url=settings.wall_agent_url,
            token=settings.wall_agent_token,
            timeout_s=settings.wall_agent_timeout_s,
        )
    )
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
    await ws.send_json(
        {
            "type": "capabilities",
            # Whether a robot mic source can actually be selected — see
            # AUDIO_INPUT in app/config.py; browser mic is always offered.
            "robot_mic": settings.audio_input == "robot",
        }
    )
    # "robot": play Gemini's audio through the real robot speaker instead of
    # streaming it to the browser — text input/status/tool log stay in the
    # browser either way. See app/config.py's AUDIO_OUTPUT.
    speaker = (
        RobotSpeaker(
            network_interface=settings.robot_speaker_network_interface,
            volume_pct=settings.robot_speaker_volume_pct,
        )
        if settings.audio_output == "robot"
        else None
    )
    # "robot": stream the real head-mic multicast into the session instead of
    # the browser's mic button — half-duplex-gated the same way robot_main.py
    # does (mute while the robot itself is speaking). See AUDIO_INPUT.
    mic = (
        RobotMic(
            multicast_group=settings.robot_mic_multicast_group,
            multicast_port=settings.robot_mic_multicast_port,
            interface_ip=settings.robot_mic_interface_ip,
            input_channels=settings.robot_mic_input_channels,
        )
        if settings.audio_input == "robot"
        else None
    )
    gate = (
        HalfDuplexGate(guard_s=settings.robot_post_speech_guard_ms / 1000.0)
        if mic is not None
        else None
    )
    # Which mic the browser has told us is active right now — switchable
    # live via a {"type":"mic_source"} control message, not fixed at session
    # start, so one page can offer both "Микрофон браузера" and "Встроенный
    # микрофон робота" with a single working Start-mic button either way.
    mic_source = "none"

    async def robot_mic_task() -> None:
        assert mic is not None  # noqa: S101 — only created/started together
        async for pcm in mic.stream():
            if mic_source != "robot":
                continue
            if gate is not None and gate.mic_muted:
                continue
            await adapter.send_audio(pcm)

    async def forward_from_browser() -> None:
        nonlocal mic_source
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if mic_source == "browser":
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
            elif payload.get("type") == "mic_source":
                source = str(payload.get("source", "none"))
                if source in ("browser", "robot", "none"):
                    mic_source = source
                    _LOG.info("mic_source -> %s", mic_source)

    async def forward_from_gemini() -> None:
        async for event in adapter.events():
            match event:
                case AudioChunk(pcm=pcm):
                    if gate is not None:
                        gate.on_speaking_started()
                    if speaker is not None:
                        await speaker.play(pcm, sample_rate_hz=settings.receive_sample_rate_hz)
                        await ws.send_json({"type": "robot_audio", "bytes": len(pcm)})
                    else:
                        await ws.send_bytes(pcm)
                case ConnectionStateChanged(state=state, detail=detail):
                    await ws.send_json({"type": "status", "state": state, "detail": detail})
                case TurnComplete():
                    if gate is not None:
                        gate.on_speaking_stopped()
                    await ws.send_json({"type": "turn_complete"})
                case Interrupted():
                    if gate is not None:
                        gate.on_speaking_stopped()
                    if speaker is not None:
                        await speaker.stop()
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

    tasks = [
        asyncio.create_task(forward_from_browser(), name="ws-from-browser"),
        asyncio.create_task(forward_from_gemini(), name="ws-from-gemini"),
    ]
    if mic is not None:
        tasks.append(asyncio.create_task(robot_mic_task(), name="robot-mic"))
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                _LOG.warning("session.task_ended: %s", exc)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            # CancelledError is a BaseException, not an Exception — it must be
            # suppressed explicitly or it propagates out of this handler and
            # gets logged as an unhandled error on every normal disconnect.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if mic is not None:
            await mic.close()
        await adapter.close()


app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
