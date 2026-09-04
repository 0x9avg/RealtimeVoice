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
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import WebSocket

from app.camera_hub import camera_hub, sampled
from app.config import Settings
from app.g1_tools import log_relay_divergence
from app.knowledge import knowledge_base
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
from app.robot_audio import HalfDuplexGate, RobotMic, RobotSpeaker, UsbMic
from app.robot_mic_state import robot_mic_state
from app.scenarios import INTAKE_RESUME_MARKER, PRESENCE_MARKER
from app.tool_registry import build_tool_registry
from app.tools import ToolResult

_LOG = logging.getLogger("session_hub")

# Queued after a turn's audio to close the vendor stream in order, once that
# audio has actually been played — not when the model merely finished
# generating it (see _drain_speaker / the TurnComplete branch).
_END_OF_TURN = object()

# Bounded so a server left running for days doesn't grow this unboundedly —
# large enough that a newly-joined browser gets a genuinely useful amount of
# back-scroll, not just the last couple of lines.
_HISTORY_MAXLEN = 300

# Per-subscriber outbound backlog. Generous enough to ride out an audio
# burst on a healthy connection, small enough that a stalled client is
# noticed (and its backlog dropped) instead of growing without bound.
_OUTBOX_MAXSIZE = 256


class SessionHub:
    """Owns the one Gemini Live session and the hardware it drives."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tools = build_tool_registry(settings)
        self.adapter = GeminiLiveAdapter(
            settings=settings, tools=self.tools, refresh_guard=self._safe_to_refresh
        )
        # Set once a session has been established, so the NEXT "connected"
        # is recognisable as a reconnect (see _handle_event) rather than the
        # first connection of the process.
        self._connected_before = False
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
        # Optional third mic choice, independent of AUDIO_INPUT above — see
        # app/config.py's usb_mic_alsa_device and app/robot_audio.py's UsbMic.
        self.usb_mic = (
            UsbMic(alsa_device=settings.usb_mic_alsa_device)
            if settings.usb_mic_alsa_device
            else None
        )
        self.gate = (
            HalfDuplexGate(guard_s=settings.robot_post_speech_guard_ms / 1000.0)
            if self.mic is not None or self.usb_mic is not None
            else None
        )
        # One-shot "stop talking now" for the CURRENT turn (see handle_mute)
        # — shared, since there's one speaker/one set of browsers to silence.
        self.muted = False
        self.subscribers: set[WebSocket] = set()
        # One outbound queue + writer task per connected browser, so a slow
        # client can only ever slow down itself (see _enqueue).
        self._outboxes: dict[WebSocket, asyncio.Queue[dict[str, Any] | bytes]] = {}
        self._writers: dict[WebSocket, asyncio.Task[None]] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAXLEN)
        self.startup_error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # Robot-speaker playback runs on its own task, fed by this queue —
        # see _drain_speaker for why handing audio off instead of awaiting
        # it inline is load-bearing, not just tidier.
        self._speaker_queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        # In-flight tool calls — kept referenced so the event loop doesn't
        # garbage-collect a running task out from under the model.
        self._tool_tasks: set[asyncio.Task[None]] = set()
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
        if self.speaker is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._drain_speaker, "speaker"), name="session-hub-speaker"
                )
            )
        if self.mic is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._robot_mic_task, "mic"), name="session-hub-mic"
                )
            )
        if self.usb_mic is not None:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._usb_mic_task, "usb_mic"), name="session-hub-usb-mic"
                )
            )
        if self.settings.presence_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._supervised(self._presence_task, "presence"), name="session-hub-presence"
                )
            )
        # Not supervised: a best-effort warm-up, not a lifetime loop.
        self._tool_tasks.add(asyncio.create_task(self._warm_up(), name="session-hub-warmup"))

    async def _warm_up(self) -> None:
        """Pay the one-off costs at boot instead of on the first visitor.

        Two things are lazy by construction and both land on whoever talks
        to the robot first: opening the vendor audio client (DDS discovery
        plus a 10s-timeout Init) and building the knowledge index (reading
        files, and embedding anything not already cached). Neither is
        needed for correctness here — this just moves the wait to a moment
        when nobody is standing in front of the robot.
        """
        if self.speaker is not None:
            await self.speaker.warm_up()
        try:
            await knowledge_base.ensure_fresh()
        except Exception as exc:  # noqa: BLE001 — best effort only
            _LOG.warning("session_hub.knowledge_warm_up_failed: %s", exc)

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
        for task in (*self._tasks, *self._writers.values()):
            task.cancel()
        for task in (*self._tasks, *self._writers.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._writers.clear()
        self._outboxes.clear()
        if self.mic is not None:
            await self.mic.close()
        if self.usb_mic is not None:
            await self.usb_mic.close()
        await self.adapter.close()

    # -- subscriber management --------------------------------------------

    async def join(self, ws: WebSocket) -> None:
        """Register a newly-connected browser and bring it up to speed.

        Nothing is sent inline: everything goes through this subscriber's
        own outbound queue (see _subscriber_writer), so neither this call
        nor any later broadcast can be held up by one slow client.
        """
        self.subscribers.add(ws)
        queue: asyncio.Queue[dict[str, Any] | bytes] = asyncio.Queue(maxsize=_OUTBOX_MAXSIZE)
        self._outboxes[ws] = queue
        self._writers[ws] = asyncio.create_task(
            self._subscriber_writer(ws, queue), name="session-hub-subscriber"
        )
        self._enqueue(ws, self._capabilities_message())
        if self.startup_error:
            self._enqueue(ws, {"type": "status", "state": "error", "detail": self.startup_error})
        # Replay so a late joiner sees the same conversation everyone else
        # watching already sees, not a blank transcript/tool log.
        for message in list(self.history):
            self._enqueue(ws, message)

    def _capabilities_message(self) -> dict[str, Any]:
        """Which mic-source options this process offers, and which hardware
        one (if any) is currently active — see app/robot_mic_state.py. Used
        both for a newly-joined browser (join(), above) and to broadcast a
        live update to every ALREADY-connected browser when the choice
        changes (handle_mic_source, below) — the hardware mic is the
        robot's, not any one tab's, so every open tab must reflect a change
        made from any of them, not just tabs that (re)connect afterwards.
        """
        return {
            "type": "capabilities",
            "robot_mic": self.mic is not None,
            "robot_mic_active": robot_mic_state.active_source == "robot",
            "usb_mic": self.usb_mic is not None,
            "usb_mic_active": robot_mic_state.active_source == "usb",
        }

    def leave(self, ws: WebSocket) -> None:
        """Drop a disconnected browser — never tears down the shared session
        itself, which keeps running for whoever's still watching (or nobody
        at all — the robot keeps listening either way)."""
        self.subscribers.discard(ws)
        self._outboxes.pop(ws, None)
        writer = self._writers.pop(ws, None)
        if writer is not None:
            writer.cancel()

    def _enqueue(self, ws: WebSocket, message: dict[str, Any] | bytes) -> None:
        """Hand one message to a subscriber's writer task, never blocking.

        A full queue means that client can't keep up (a phone on bad Wi-Fi
        applies TCP backpressure, and ws.send() then blocks for as long as
        it takes). Dropping its OLDEST pending message keeps it on the
        freshest state instead of a stale backlog, and — the actual point —
        keeps the robot's event loop out of it entirely.
        """
        queue = self._outboxes.get(ws)
        if queue is None:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)
            _LOG.warning("session_hub.subscriber_too_slow dropping messages")

    async def _subscriber_writer(
        self, ws: WebSocket, queue: asyncio.Queue[dict[str, Any] | bytes]
    ) -> None:
        """One task per connected browser: drains that browser's queue and
        is the ONLY place a ws.send() is awaited."""
        try:
            while True:
                message = await queue.get()
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — dead/closing socket, drop the subscriber
            self.subscribers.discard(ws)
            self._outboxes.pop(ws, None)
            self._writers.pop(ws, None)

    # -- inbound, from any subscriber --------------------------------------

    async def handle_text(self, text: str) -> None:
        await self.adapter.send_text(text)

    async def handle_browser_audio(self, pcm: bytes) -> None:
        await self.adapter.send_audio(pcm)

    async def handle_mic_source(self, source: str) -> None:
        # Any subscriber choosing a hardware source (or away from one) is an
        # explicit operator action on the robot's actual hardware, not just
        # that one tab's input routing — see app/robot_mic_state.py. Only
        # ever selects a source that's actually configured on this process;
        # "robot"/"usb" requested for a source that doesn't exist here is a
        # no-op (leaves whatever was active before untouched), same as
        # before this method knew about more than one hardware source.
        if source == "robot" and self.mic is not None:
            robot_mic_state.active_source = "robot"
        elif source == "usb" and self.usb_mic is not None:
            robot_mic_state.active_source = "usb"
        elif source in ("browser", "none"):
            robot_mic_state.active_source = "browser"
        else:
            return
        _LOG.info("robot_mic_state.active_source -> %s", robot_mic_state.active_source)
        # Push the new state to every ALREADY-open tab, not just future
        # joiners — without this, a second operator's tab kept showing
        # "listening (robot mic)" (or the wrong source entirely) forever
        # after someone else switched it elsewhere, since it had no way to
        # learn the shared hardware state changed out from under it.
        await self._broadcast_json(self._capabilities_message())

    async def handle_mute(self) -> None:
        self.muted = True
        await self._silence_speaker()

    # -- tools ---------------------------------------------------------------

    async def _run_tool(self, call: ToolCall) -> None:
        """Execute one tool call and hand the result back to the model.

        Runs off the event pump (see the ToolCall branch) and under a hard
        timeout: the model blocks its turn waiting for this, so a tool that
        hangs is the visitor standing in silence. Better to tell the model
        it failed — it can say so out loud — than to wait out a network
        timeout that outlives the visitor's patience.
        """
        try:
            result = await asyncio.wait_for(
                self.tools.dispatch(call.name, call.args),
                timeout=self.settings.tool_timeout_s,
            )
        except TimeoutError:
            _LOG.warning(
                "session_hub.tool_timed_out name=%s after=%.1fs",
                call.name,
                self.settings.tool_timeout_s,
            )
            result = ToolResult(
                ok=False, text=f"{call.name}: не удалось получить ответ вовремя, попробуйте ещё раз"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never strand the model's turn
            _LOG.exception("session_hub.tool_failed name=%s", call.name)
            result = ToolResult(ok=False, text=f"{call.name} failed: {exc}")

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

    # -- visit interview continuity across session swaps ---------------------

    def _intake_tool(self) -> Any | None:
        """The G1 visit-intake tool, if this profile has one (R1 doesn't)."""
        return self.tools.get("visit_intake")

    def _safe_to_refresh(self) -> bool:
        """Veto a PROACTIVE session swap while a visit interview is running.

        The interview's state lives in Python and survives a reconnect, but
        the model's memory of being mid-interview does not — so a swap
        between "robot asked question 4" and "visitor answers question 4"
        used to strand the registration. Deferring costs nothing: the check
        runs again at the next turn, and the interview is minutes long at
        most. A reconnect we DIDN'T choose (Gemini closing the session) is
        handled separately, by _resume_intake_after_reconnect.
        """
        tool = self._intake_tool()
        if tool is not None and getattr(tool, "engaged", False):
            _LOG.info("session_hub.refresh_deferred reason=visit_intake_in_progress")
            return False
        return True

    async def _resume_intake_after_reconnect(self) -> None:
        """Put the model back on script after the session was recreated
        mid-interview — see app/scenarios.py's INTAKE_RESUME_MARKER."""
        tool = self._intake_tool()
        if tool is None or not getattr(tool, "engaged", False):
            return
        question = getattr(tool, "current_question", "")
        if not question:
            return
        _LOG.info("session_hub.intake_resumed_after_reconnect")
        await self.adapter.send_text(f"{INTAKE_RESUME_MARKER} {question}")

    # -- robot speaker -------------------------------------------------------

    async def _drain_speaker(self) -> None:
        """Play queued audio on its own task, off the event loop's critical
        path.

        RobotSpeaker.play() paces audio out in REAL TIME (the vendor
        pipeline expects that), so awaiting it inline from _handle_event
        stalled the whole event pump for the length of the utterance:
        a barge-in or "Замолчи" arriving mid-sentence sat unread in the
        socket buffer until the robot had finished saying everything it
        already had — measured at 3s of dead air on a 3s utterance — and
        tool calls/transcripts queued behind it too. Handing audio to this
        task keeps _handle_event non-blocking, so interruptions land at
        once.
        """
        assert self.speaker is not None  # noqa: S101 — only started together
        while True:
            item = await self._speaker_queue.get()
            try:
                if item is _END_OF_TURN:
                    # The turn's audio has now actually finished playing —
                    # only here is it safe to close the vendor stream and
                    # re-open the mic (TurnComplete itself fires while the
                    # tail is still queued; opening the gate there would let
                    # the robot hear its own last words — no AEC on this
                    # hardware).
                    await self.speaker.stop()
                    if self.gate is not None:
                        self.gate.on_speaking_stopped()
                else:
                    await self.speaker.play(
                        item,  # type: ignore[arg-type]
                        sample_rate_hz=self.settings.receive_sample_rate_hz,
                    )
            except Exception:  # noqa: BLE001 — one bad chunk must not end playback
                _LOG.exception("session_hub.speaker_playback_failed")

    async def _silence_speaker(self, *, arm_guard: bool = True) -> None:
        """Cut playback short NOW: drop whatever is still queued, then stop
        the vendor stream (RobotSpeaker.stop also cuts an in-flight play at
        its next 50ms chunk boundary).

        `arm_guard=False` for a barge-in. The post-speech guard exists to
        stop the robot hearing its own tail — but on a barge-in the thing
        the mic is picking up is the VISITOR, mid-sentence: that's what
        triggered the interrupt. Arming the guard there deafens us to the
        first fraction of a second of exactly the speech we're trying to
        hear, so the model gets a clipped question and takes longer to
        answer it.
        """
        while True:
            try:
                self._speaker_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self.speaker is not None:
            await self.speaker.stop()
        if self.gate is not None:
            if arm_guard:
                self.gate.on_speaking_stopped()
            else:
                self.gate.open_now()

    # -- outbound, to every subscriber --------------------------------------

    async def _broadcast_json(self, message: dict[str, Any], *, remember: bool = False) -> None:
        # Deliberately awaits nothing: every send is handed to the target's
        # own writer task (see _enqueue). Before that, one browser stuck
        # behind TCP backpressure stalled this loop — and with it the
        # robot's speech, tool calls and barge-in handling.
        if remember:
            self.history.append(message)
        for ws in list(self.subscribers):
            self._enqueue(ws, message)

    async def _broadcast_bytes(self, data: bytes) -> None:
        for ws in list(self.subscribers):
            self._enqueue(ws, data)

    # -- background tasks, started once by start() ------------------------

    async def _robot_mic_task(self) -> None:
        assert self.mic is not None  # noqa: S101 — only created/started together
        await self._drain_hw_mic(self.mic.stream, "robot", "mic")

    async def _usb_mic_task(self) -> None:
        assert self.usb_mic is not None  # noqa: S101 — only created/started together
        await self._drain_hw_mic(self.usb_mic.stream, "usb", "usb_mic")

    async def _drain_hw_mic(
        self, open_stream: Callable[[], AsyncIterator[bytes]], source: str, log_name: str
    ) -> None:
        """Forward one hardware mic's audio for as long as the PROCESS lives.

        `RobotMic.stream()`/`UsbMic.stream()` both RETURN (don't raise) when
        their underlying source goes away (UDP socket closed; `arecord` exited
        because the USB device was unplugged, busy, or hiccuped) — by design,
        so a transient hardware hiccup doesn't look like an unhandled
        exception. But `_supervised` (the caller of `_robot_mic_task`/
        `_usb_mic_task`) treats any wrapped coroutine that merely RETURNS as
        "something is broken deeply" and hard-exits the whole process
        (os._exit) — correct for a genuinely dead infinite loop, but it would
        turn "someone unplugged the USB mic" into a full service restart
        mid-conversation. Looping here with backoff is what keeps this task
        itself a true infinite loop (never returns), so a disappeared source
        is retried instead of taken as fatal — plug it back in and it resumes
        within a few seconds, no restart, no operator action needed.
        """
        backoff_s = 1.0
        while True:
            streamed_anything = False
            try:
                async for pcm in open_stream():
                    streamed_anything = True
                    backoff_s = 1.0  # streaming again — forget any prior failure
                    if robot_mic_state.active_source != source:
                        continue
                    if self.gate is not None and self.gate.mic_muted:
                        continue
                    await self.adapter.send_audio(pcm)
                _LOG.warning(
                    "session_hub.%s_stream_ended retrying_in=%.1fs streamed_anything=%s",
                    log_name,
                    backoff_s,
                    streamed_anything,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A RAISED error (e.g. UsbMicNotAvailableError — arecord
                # itself missing/not on PATH, a typo'd USB_MIC_ALSA_DEVICE)
                # is just as recoverable as the graceful "returned early"
                # case above, and must be treated the same way: retried, not
                # left to propagate into _supervised's fatal os._exit path.
                # Without this, a bad config value would crash-loop the
                # ENTIRE voice service forever — every restart hits the same
                # broken config and dies again — instead of only disabling
                # this one mic choice.
                _LOG.exception(
                    "session_hub.%s_stream_failed retrying_in=%.1fs", log_name, backoff_s
                )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 30.0)

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
                    # Hand off, don't await playback — see _drain_speaker.
                    self._speaker_queue.put_nowait(pcm)
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
                if state == "connected":
                    # A RE-connect (not the process's first session): the new
                    # session has no memory of anything, so a visit interview
                    # that's still running in Python has to be re-explained
                    # to the model or the visitor's next answer goes nowhere.
                    if self._connected_before:
                        await self._resume_intake_after_reconnect()
                    self._connected_before = True
            case TurnComplete():
                if self.speaker is not None:
                    # Queue the close instead of doing it here: the tail of
                    # this turn's audio is still waiting to play, and both
                    # closing the vendor stream and re-opening the mic have
                    # to happen AFTER it, not now (see _drain_speaker).
                    self._speaker_queue.put_nowait(_END_OF_TURN)
                elif self.gate is not None:
                    self.gate.on_speaking_stopped()
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
                # Barge-in: drop everything still queued and cut the audio
                # that's already playing, rather than letting the robot
                # finish talking over the person who interrupted it. No
                # post-speech guard here — the visitor is talking RIGHT NOW.
                await self._silence_speaker(arm_guard=False)
                # Cut short — not a relay divergence.
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
                # A tool call can genuinely take a few seconds (see
                # _run_tool's docstring), and the model holds its turn
                # silent the whole time — the system_instruction now also
                # asks it to say something out loud first (app/scenarios.py's
                # _TOOL_LATENCY_RULE), but that's a prompt-level ask, not a
                # guarantee. This status update is the code-level backstop:
                # independent of whether the model actually speaks, the web
                # UI's status badge stops reading "listening" and shows
                # something is happening, instead of looking frozen for
                # however long the tool takes. Not remembered in history —
                # transient, and turn_complete/interrupted always supersede
                # it well before a late joiner could replay it.
                await self._broadcast_json({"type": "status", "state": "thinking"})
                # Run the tool on its own task. Dispatching it inline froze
                # this pump for as long as the tool took — and tools reach
                # the network: the wall agent waits up to 5s if the wall PC
                # is off, the embedding call up to 20s. For all that time
                # the robot could not play audio, emit a transcript, or act
                # on a barge-in. Nothing downstream needs the result in this
                # coroutine; the model gets it via call.respond().
                task = asyncio.create_task(self._run_tool(call), name=f"tool-{call.name}")
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)
