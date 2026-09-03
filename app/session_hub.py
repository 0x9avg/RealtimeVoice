"""The one active Gemini Live session — shared by every connected browser.

Before this module existed, `/ws/session` (app/main.py) built a fresh
`GeminiLiveAdapter` + tool registry + `RobotSpeaker`/`RobotMic` for EVERY
WebSocket connection. Two browser tabs (two operators, or one operator who
left a tab open and opened another) each got their own independent
conversation with the model — worse, on real hardware each would also try
to drive the SAME physical mic/speaker concurrently, and with
`PRESENCE_ENABLED=true` a visitor could get greeted once per open tab.

`SessionHub` inverts that: one adapter, one tool registry, one set of
hardware handles, created once and kept alive for the life of the server
process — independent of whether zero, one, or many browsers are currently
watching. `/ws/session` is now a thin subscribe/publish layer over this hub
(see `join`/`leave`/`handle_*` below): every connected browser sees the same
transcript, the same tool log, and controls the same session — the mute
button, the mic on/off switch and typed text all act on the one shared
conversation, not "this tab's copy of it". A newly-joined browser also
replays the hub's own bounded history (`history`) so it starts on the same
page as everyone else already watching, rather than a blank transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from typing import Any

from fastapi import WebSocket

from app.camera_hub import camera_hub, sampled
from app.config import Settings
from app.g1_tools import log_relay_divergence
from app.gemini_live import (
    AudioChunk,
    ConnectionStateChanged,
    GeminiLiveAdapter,
    Interrupted,
    LiveEvent,
    ToolCall,
    Transcript,
    TurnComplete,
)
from app.presence import PresenceRunner
from app.robot_audio import HalfDuplexGate, RobotMic, RobotSpeaker
from app.robot_mic_state import robot_mic_state
from app.scenarios import PRESENCE_MARKER
from app.tool_registry import build_tool_registry

_LOG = logging.getLogger("session_hub")

# Bounded so a server left running for days doesn't grow this unboundedly —
# large enough that a newly-joined browser gets a genuinely useful amount of
# back-scroll, not just the last couple of lines.
_HISTORY_MAXLEN = 300


class SessionHub:
    """Owns the one Gemini Live session and the hardware it drives."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tools = build_tool_registry(settings)
        self.adapter = GeminiLiveAdapter(settings=settings, tools=self.tools)
        # "robot": play Gemini's audio through the real robot speaker instead
        # of streaming it to browsers. See app/config.py's AUDIO_OUTPUT.
        self.speaker = (
            RobotSpeaker(
                network_interface=settings.robot_speaker_network_interface,
                volume_pct=settings.robot_speaker_volume_pct,
            )
            if settings.audio_output == "robot"
            else None
        )
        # "robot": stream the real head-mic multicast into the session
        # instead of any one browser's mic button. See AUDIO_INPUT.
        self.mic = (
            RobotMic(
                multicast_group=settings.robot_mic_multicast_group,
                multicast_port=settings.robot_mic_multicast_port,
                interface_ip=settings.robot_mic_interface_ip,
                input_channels=settings.robot_mic_input_channels,
            )
            if settings.audio_input == "robot"
            else None
        )
        self.gate = (
            HalfDuplexGate(guard_s=settings.robot_post_speech_guard_ms / 1000.0)
            if self.mic is not None
            else None
        )
        # One-shot "stop talking now" for the CURRENT turn (see handle_mute)
        # — shared, since there's one speaker/one set of browsers to silence.
        self.muted = False
        self.subscribers: set[WebSocket] = set()
        self.history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAXLEN)
        self.startup_error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # For log_relay_divergence — see app/main.py's old copy of this same
        # bookkeeping for why it accumulates fragments across TurnComplete.
        self._last_tool_name: str | None = None
        self._last_tool_text = ""
        self._model_turn_fragments: list[str] = []

    # -- lifecycle (app/main.py's lifespan calls these once) -------------

    async def start(self) -> None:
        if self._tasks:
            return  # idempotent — mirrors app/camera_hub.py's CameraHub.start()
        if not self.settings.resolved_api_key():
            self.startup_error = f"no Gemini API key set: export {self.settings.api_key_env}"
            _LOG.error(self.startup_error)
            return
        _LOG.info(
            "session_hub.start profile=%s tools=%s", self.settings.robot_profile, self.tools.names()
        )
        self._tasks.append(
            asyncio.create_task(
                self._supervised(self._pump_gemini, "gemini"), name="session-hub-gemini"
            )
        )
        if self.mic is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._robot_mic_task, "mic"), name="session-hub-mic"
                )
            )
        if self.settings.presence_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._presence_task, "presence"), name="session-hub-presence"
                )
            )

    async def _supervised(self, coro_fn: Any, name: str) -> None:
        """Run one of the process-lifetime background tasks; if it EVER ends
        without being cancelled — an unhandled exception, or (shouldn't
        happen, they're all infinite loops) just returning — that means
        something is broken deeply enough that this process can't recover
        on its own, and half of it (the web server) would otherwise keep
        running and looking fine while the robot has gone silently,
        permanently mute. Hard-exit so systemd's Restart=on-failure
        (deploy/realtimevoice.service) brings up a clean process instead of
        leaving a zombie one nobody notices until the demo already failed.
        _pump_gemini's own event loop already isolates ordinary per-event
        failures (see its docstring) — this is the last-resort net under
        that, not a replacement for it.
        """
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.critical("session_hub.task_died name=%s", name, exc_info=True)
        else:
            _LOG.critical("session_hub.task_ended_unexpectedly name=%s", name)
        os._exit(1)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self.mic is not None:
            await self.mic.close()
        await self.adapter.close()

    # -- subscriber management --------------------------------------------

    async def join(self, ws: WebSocket) -> None:
        """Register a newly-connected browser and bring it up to speed."""
        self.subscribers.add(ws)
        await ws.send_json(
            {
                "type": "capabilities",
                "robot_mic": self.settings.audio_input == "robot",
                "robot_mic_active": robot_mic_state.enabled,
            }
        )
        if self.startup_error:
            await ws.send_json(
                {"type": "status", "state": "error", "detail": self.startup_error}
            )
        # Replay so a late joiner sees the same conversation everyone else
        # watching already sees, not a blank transcript/tool log.
        for message in list(self.history):
            with contextlib.suppress(Exception):
                await ws.send_json(message)

    def leave(self, ws: WebSocket) -> None:
        """Drop a disconnected browser — never tears down the shared session
        itself, which keeps running for whoever's still watching (or nobody
        at all — the robot keeps listening either way)."""
        self.subscribers.discard(ws)

    # -- inbound, from any subscriber --------------------------------------

    async def handle_text(self, text: str) -> None:
        await self.adapter.send_text(text)

    async def handle_browser_audio(self, pcm: bytes) -> None:
        await self.adapter.send_audio(pcm)

    async def handle_mic_source(self, source: str) -> None:
        if self.mic is not None:
            # Any subscriber choosing "robot" (or away from it) is an
            # explicit operator action on the actual hardware mic, not just
            # that one tab's input routing — see app/robot_mic_state.py.
            robot_mic_state.enabled = source == "robot"
            _LOG.info("robot_mic_state.enabled -> %s", robot_mic_state.enabled)

    async def handle_mute(self) -> None:
        self.muted = True
        if self.speaker is not None:
            await self.speaker.stop()

    # -- outbound, to every subscriber --------------------------------------

    async def _broadcast_json(self, message: dict[str, Any], *, remember: bool = False) -> None:
        if remember:
            self.history.append(message)
        dead: list[WebSocket] = []
        for ws in list(self.subscribers):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 — a dead/closing socket, drop it
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    async def _broadcast_bytes(self, data: bytes) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.subscribers):
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    # -- background tasks, started once by start() ------------------------

    async def _robot_mic_task(self) -> None:
        assert self.mic is not None  # noqa: S101 — only created/started together
        async for pcm in self.mic.stream():
            if not robot_mic_state.enabled:
                continue
            if self.gate is not None and self.gate.mic_muted:
                continue
            await self.adapter.send_audio(pcm)

    async def _presence_task(self) -> None:
        """Greets a visitor once they appear (R1 ТЗ §4.1 / G1 ТЗ §3.1). One
        instance for the whole process — previously this ran once PER
        connected browser tab, so a visitor could be greeted multiple times
        if more than one tab happened to be open."""

        async def on_visitor_detected() -> None:
            _LOG.info("presence.visitor_detected")
            await self.adapter.send_text(PRESENCE_MARKER)

        runner = PresenceRunner(
            frames=sampled(camera_hub, fps=self.settings.presence_fps),
            on_visitor_detected=on_visitor_detected,
            threshold=self.settings.presence_threshold,
            rearm_s=self.settings.presence_rearm_s,
        )
        await runner.run()

    async def _pump_gemini(self) -> None:
        """Consume adapter events for the life of the process.

        Each event is handled in its own try/except (see _handle_event) —
        this loop itself must never die from an ordinary bad event (a
        malformed tool response, a speaker/DDS hiccup, whatever): this task
        is started once at process boot, not per browser connection, so if
        it dies the whole conversation goes silently, permanently mute —
        the web server keeps answering HTTP requests just fine, so nothing
        about the process looks obviously broken from the outside. One bad
        event should cost that one turn, not the rest of the demo.
        """
        async for event in self.adapter.events():
            try:
                await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — deliberately broad, see docstring
                _LOG.exception("session_hub.event_handling_failed event=%r", event)

    async def _handle_event(self, event: LiveEvent) -> None:
        match event:
            case AudioChunk(pcm=pcm):
                if self.muted:
                    return
                if self.gate is not None:
                    self.gate.on_speaking_started()
                if self.speaker is not None:
                    await self.speaker.play(
                        pcm, sample_rate_hz=self.settings.receive_sample_rate_hz
                    )
                    await self._broadcast_json({"type": "robot_audio", "bytes": len(pcm)})
                else:
                    await self._broadcast_bytes(pcm)
            case ConnectionStateChanged(state=state, detail=detail):
                # A proactive refresh (see GeminiLiveAdapter.events'
                # max_session_duration_s) is expected, healthy behaviour,
                # not a dropped connection — don't log it as a warning.
                is_error_close = state == "closed" and detail != "proactive session refresh"
                log_fn = _LOG.warning if is_error_close else _LOG.info
                log_fn("gemini.connection_state state=%s detail=%s", state, detail)
                await self._broadcast_json({"type": "status", "state": state, "detail": detail})
            case TurnComplete():
                if self.gate is not None:
                    self.gate.on_speaking_stopped()
                if self.speaker is not None:
                    # Close out the vendor stream cleanly (see
                    # RobotSpeaker.stop's docstring) — without this the
                    # second+ utterance of a session can go silently
                    # mute on the real robot speaker.
                    await self.speaker.stop()
                if self._last_tool_name is not None and self._model_turn_fragments:
                    log_relay_divergence(
                        self._last_tool_name,
                        self._last_tool_text,
                        "".join(self._model_turn_fragments),
                    )
                self._last_tool_name = None
                self._last_tool_text = ""
                self._model_turn_fragments = []
                self.muted = False
                await self._broadcast_json({"type": "turn_complete"})
            case Interrupted():
                if self.gate is not None:
                    self.gate.on_speaking_stopped()
                if self.speaker is not None:
                    await self.speaker.stop()
                # Barge-in cut the turn short — not a relay divergence.
                self._last_tool_name = None
                self._last_tool_text = ""
                self._model_turn_fragments = []
                self.muted = False
                await self._broadcast_json({"type": "interrupted"})
            case Transcript(role=role, text=text):
                if role == "model":
                    self._model_turn_fragments.append(text)
                await self._broadcast_json(
                    {"type": "transcript", "role": role, "text": text}, remember=True
                )
            case ToolCall() as call:
                _LOG.info("tool_call name=%s args=%s", call.name, call.args)
                await self._broadcast_json(
                    {"type": "tool_call", "name": call.name, "args": call.args}, remember=True
                )
                result = await self.tools.dispatch(call.name, call.args)
                await call.respond(result.text)
                self._last_tool_name = call.name
                self._last_tool_text = result.text
                self._model_turn_fragments = []
                await self._broadcast_json(
                    {
                        "type": "tool_result",
                        "name": call.name,
                        "ok": result.ok,
                        "text": result.text,
                    },
                    remember=True,
                )
