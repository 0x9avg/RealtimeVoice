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
import time
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
from app.presence import PresenceRunner, build_motion_detector
from app.robot_audio import HalfDuplexGate, RobotMic, RobotSpeaker, UsbMic, UsbSpeaker
from app.robot_mic_state import robot_mic_state
from app.scenarios import INTAKE_RESUME_MARKER, PRESENCE_MARKER
from app.tool_registry import build_tool_registry
from app.tools import ToolResult
from app.wake_word import WakeWordDetector

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
        # "robot"/"usb": play Gemini's audio through real hardware instead of
        # streaming it to browsers. See app/config.py's AUDIO_OUTPUT.
        #
        # Both hardware targets are constructed (not just the configured
        # default) whenever this deployment is in hardware-output mode at
        # all, so an operator can switch between them at runtime without a
        # restart — see handle_audio_output below and the `speaker`
        # property, which is what every OTHER method in this class actually
        # reads/awaits (self.speaker.play(...) etc.), so nothing downstream
        # needed to change to support the toggle. Constructing both is
        # cheap: RobotSpeaker only touches DDS/unitree_sdk2py lazily, on its
        # first real play(); UsbSpeaker only spawns `aplay` on its first
        # real play() too.
        self._speakers: dict[str, RobotSpeaker | UsbSpeaker] = {}
        if settings.audio_output in ("robot", "usb"):
            self._speakers["robot"] = RobotSpeaker(
                network_interface=settings.robot_speaker_network_interface,
                volume_pct=settings.robot_speaker_volume_pct,
            )
            if settings.usb_speaker_alsa_device:
                self._speakers["usb"] = UsbSpeaker(
                    alsa_device=settings.usb_speaker_alsa_device,
                    volume_pct=settings.usb_speaker_volume_pct,
                    target_sample_rate_hz=settings.usb_speaker_sample_rate_hz,
                )
        # AUDIO_OUTPUT picks the INITIAL target; "browser" (the default)
        # means neither hardware target was constructed above, so this is
        # None, same as before this toggle existed.
        self._active_speaker_target: str | None = (
            settings.audio_output
            if settings.audio_output in self._speakers
            else next(iter(self._speakers), None)
        )
        # "robot"/"usb": read mic audio from real hardware instead of any
        # one browser's mic button. See app/config.py's AUDIO_INPUT.
        #
        # Both hardware sources are constructed (not just the configured
        # default) whenever this deployment is in hardware-input mode at
        # all, exactly mirroring `_speakers` above — so an operator can
        # switch which one is listened to at runtime, from the website, with
        # no restart and no `.env` edit (2026-09-06 ask, explicit: "и
        # микрофон с динамиком выбирается не посредством .env файла, а
        # напрямую на сайте"). See `_preferred_mic_input`/
        # `set_preferred_mic_input` below for the actual selection —
        # constructing both here is cheap and side-effect-free (RobotMic/
        # UsbMic only touch the network/spawn `arecord` lazily, inside their
        # own `stream()`, called from `_robot_mic_task`/`_usb_mic_task`).
        self.mic: RobotMic | None = None
        self.usb_mic: UsbMic | None = None
        if settings.audio_input in ("robot", "usb"):
            self.mic = RobotMic(
                multicast_group=settings.robot_mic_multicast_group,
                multicast_port=settings.robot_mic_multicast_port,
                interface_ip=settings.robot_mic_interface_ip,
                input_channels=settings.robot_mic_input_channels,
            )
            if settings.usb_mic_alsa_device:
                self.usb_mic = UsbMic(alsa_device=settings.usb_mic_alsa_device)
        self.gate = (
            HalfDuplexGate(guard_s=settings.robot_post_speech_guard_ms / 1000.0)
            if self.mic is not None or self.usb_mic is not None
            else None
        )
        # Item 5 of the 2026-09-04 fixes list: the hardware mic used to
        # forward EVERYTHING it heard to Gemini all day, all the time. Now
        # it only forwards during a "wake window" — opened by a local
        # wake-word hit (app/wake_word.py) or a manual override
        # (handle_mic_source, repurposed from its old "pick a source and
        # press Start" meaning — see that method's docstring), and extended
        # by each robot turn so an ongoing conversation doesn't need
        # re-waking every utterance. See _drain_hw_mic for where this is
        # read/written; monotonic time, 0.0 (never) until first opened.
        self._wake_active_until: float = 0.0
        # Per-SOURCE detector instances, not one shared between them — each
        # owns its own internal audio buffer (see app/wake_word.py). Sharing
        # one instance used to let two concurrently-running mic tasks (both
        # still idle, neither having triggered yet) interleave calls to the
        # same detector's feed() across an `await` point inside it,
        # splicing bytes from two unrelated physical microphones into one
        # buffer — audit finding, 2026-09-06, alongside the closely related
        # _hw_mic_owns_wake_window fix below (that one stops a triggered
        # source's audio doubling up with another's; this stops their
        # PRE-trigger detection from corrupting each other in the first
        # place). Only built for sources that actually exist here.
        self._wake_detectors: dict[str, WakeWordDetector] = {}
        if settings.wake_word_enabled:
            if self.mic is not None:
                self._wake_detectors["robot"] = WakeWordDetector(
                    phrases=settings.wake_word_phrases,
                    language=settings.wake_word_language,
                    model_name=settings.wake_word_whisper_model,
                    window_s=settings.wake_word_window_s,
                )
            if self.usb_mic is not None:
                self._wake_detectors["usb"] = WakeWordDetector(
                    phrases=settings.wake_word_phrases,
                    language=settings.wake_word_language,
                    model_name=settings.wake_word_whisper_model,
                    window_s=settings.wake_word_window_s,
                )
        # Which ONE of the sources in `_wake_detectors` is actually allowed
        # to run wake-word detection right now — 2026-09-06 ask: "разделения
        # и параллельного слушания не должно быть... оператор сам выбирает".
        # AUDIO_INPUT only picks the INITIAL preference (so a fresh process
        # starts in a sane state); `set_preferred_mic_input` below changes it
        # live, from the website, without touching `.env` or restarting —
        # both mic tasks keep running underneath either way (see `self.mic`/
        # `self.usb_mic` above), only their audio's fate differs: the
        # preferred source's feeds the wake-word detector and can open the
        # window; the other's is read (so switching is instant, no cold
        # `arecord`/socket start) and immediately discarded.
        self._preferred_mic_input: str | None = (
            settings.audio_input
            if settings.audio_input in self._wake_detectors
            else next(iter(self._wake_detectors), None)
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

    @property
    def speaker(self) -> RobotSpeaker | UsbSpeaker | None:
        """Whichever hardware speaker (if any) is CURRENTLY the audio-output
        target — see _speakers/_active_speaker_target above and
        handle_audio_output below. A property, not a plain attribute, so
        every other method here (_drain_speaker, warm_up, mute, ...) always
        reads the live selection without needing to know a runtime toggle
        exists at all.
        """
        if self._active_speaker_target is None:
            return None
        return self._speakers.get(self._active_speaker_target)

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
            # Deliberately NOT self._supervised(): see _presence_task's own
            # docstring for why a presence bug must never os._exit(1) the
            # whole robot the way a dead mic/speaker task correctly does.
            self._tasks.append(
                asyncio.create_task(self._presence_task(), name="session-hub-presence")
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
        # Warm up EVERY constructed hardware speaker, not just the currently
        # active one — an operator can switch targets at runtime
        # (handle_audio_output) without a restart, and the whole point of
        # warming up ahead of time is that the first utterance after a
        # switch shouldn't pay this cost either.
        for speaker in self._speakers.values():
            await speaker.warm_up()
        # UX audit finding, 2026-09-06: without this, the first wake-word
        # check of the process's life (usually a visitor's very first
        # "робот" of the day) paid the full faster-whisper model-load cost
        # inline — see WakeWordDetector.warm_up's docstring.
        for detector in self._wake_detectors.values():
            try:
                await detector.warm_up()
            except Exception as exc:  # noqa: BLE001 — best effort only
                _LOG.warning("session_hub.wake_word_warm_up_failed: %s", exc)
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
            # Which hardware mic is currently PREFERRED for wake-word
            # listening — 2026-09-06 ask: chosen live from the website, not
            # `.env` (see set_preferred_mic_input below). Distinct from the
            # *_active fields above, which reflect the momentary "is a wake
            # window open right now" state: this is the standing choice of
            # which source is even allowed to try in the first place.
            "preferred_mic_input": self._preferred_mic_input,
            # Hardware speaker OUTPUT targets — item 2 of the 2026-09-05
            # fixes list. Same shape/idea as the mic fields above: which
            # targets exist on this process, and which one is currently
            # active (see handle_audio_output/the `speaker` property).
            "speaker_targets": list(self._speakers.keys()),
            "active_speaker_target": self._active_speaker_target,
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
        """Manual override for the hardware mic's wake state.

        Before item 5 of the 2026-09-04 fixes list, this was "pick a source
        and press Start" — the hardware mic forwarded everything, all the
        time, once selected. Now the hardware mic (if configured) is always
        locally listening for the wake word (app/wake_word.py) and only
        forwards to Gemini during the resulting window — there is nothing
        left to "start" in the old sense. This method is now a manual
        override for that same window, useful for an operator who wants the
        robot listening right now without saying the wake phrase (a demo, a
        noisy room, testing): "robot"/"usb" opens the window immediately for
        that source (only if it's actually configured here — a request for a
        source that doesn't exist is a no-op); "browser"/"none" closes it
        early (an explicit "go back to sleep", separate from — and stronger
        than — waiting out the window's own timeout).
        """
        now = time.monotonic()
        if source == "robot" and self.mic is not None:
            self._wake_active_until = now + self.settings.wake_active_window_s
            robot_mic_state.active_source = "robot"
        elif source == "usb" and self.usb_mic is not None:
            self._wake_active_until = now + self.settings.wake_active_window_s
            robot_mic_state.active_source = "usb"
        elif source in ("browser", "none"):
            self._wake_active_until = 0.0
            robot_mic_state.active_source = "browser"
        else:
            return
        _LOG.info("robot_mic_state.active_source -> %s (manual override)", robot_mic_state.active_source)
        # Push the new state to every ALREADY-open tab, not just future
        # joiners — without this, a second operator's tab kept showing
        # "listening (robot mic)" (or the wrong source entirely) forever
        # after someone else switched it elsewhere, since it had no way to
        # learn the shared hardware state changed out from under it.
        await self._broadcast_json(self._capabilities_message())

    async def handle_audio_output(self, target: str) -> None:
        """Runtime toggle between hardware speaker targets — item 2 of the
        2026-09-05 fixes list ("добавь возможность выбора вывода звука
        (внешний usb динамик, внутренний)"). Only switches to a target
        that's actually configured here (see _speakers in __init__); a
        request for one that doesn't exist, or the one already active, is a
        no-op — same convention as handle_mic_source.

        Stops the OLD speaker explicitly, in this order, BEFORE flipping
        _active_speaker_target — the `speaker` property always resolves to
        whichever target is currently active, so stopping it AFTER the flip
        would silence the just-selected NEW speaker instead of cutting off
        whatever the old one still had queued/in flight.
        """
        if target not in self._speakers or target == self._active_speaker_target:
            return
        old_speaker = self.speaker
        while True:
            try:
                self._speaker_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if old_speaker is not None:
            await old_speaker.stop()
        self._active_speaker_target = target
        if self.gate is not None:
            self.gate.on_speaking_stopped()
        _LOG.info("session_hub.audio_output -> %s", target)
        await self._broadcast_json(self._capabilities_message())

    async def set_preferred_mic_input(self, source: str) -> None:
        """Runtime choice of WHICH hardware mic is allowed to listen for the
        wake word — 2026-09-06 ask, explicit: "микрофон... выбирается не
        посредством .env файла, а напрямую на сайте". Mirrors
        `handle_audio_output` above: both hardware mics are always
        constructed together (see `self.mic`/`self.usb_mic` in `__init__`),
        so switching here is instant — no restart, no cold `arecord`/socket
        start, no `.env` edit. Only switches to a source that's actually
        configured here (see `_wake_detectors`); a request for one that
        doesn't exist, or the one already preferred, is a no-op — same
        convention as handle_mic_source/handle_audio_output.

        If the OLD preferred source currently owns an open wake window,
        that window is closed immediately — otherwise it would keep
        forwarding audio from a mic the operator just said to stop
        listening to until its timeout ran out on its own.
        """
        if source not in self._wake_detectors or source == self._preferred_mic_input:
            return
        self._preferred_mic_input = source
        if robot_mic_state.active_source not in (source, "browser"):
            robot_mic_state.active_source = "browser"
            self._wake_active_until = 0.0
        _LOG.info("session_hub.preferred_mic_input -> %s", source)
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
                    if self.gate is not None and self.gate.mic_muted:
                        # Also skip wake-word detection here, not just
                        # forwarding — otherwise the robot's own voice
                        # coming back through the mic (no AEC on this
                        # hardware, see HalfDuplexGate) could self-trigger
                        # the wake word.
                        continue
                    if source in self._wake_detectors:
                        if not await self._hw_mic_owns_wake_window(source, pcm):
                            continue
                    elif robot_mic_state.active_source != source:
                        # No wake-word model loaded (WAKE_WORD_ENABLED=false)
                        # — fall back to the old manual-selection gate so
                        # this source isn't a silent no-op.
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

    async def _hw_mic_owns_wake_window(self, source: str, pcm: bytes) -> bool:
        """Wake-word gate for ONE hardware source — whether the CALLER
        should still forward `pcm` (already known not to be gate-muted)
        itself. Returns False both when the window isn't open at all AND
        on the exact call where a wake hit just fired — in that second
        case this method has already forwarded the detector's buffered
        lead-up (which ends with `pcm`) itself; see the "recent_audio"
        replay below for why the caller must not also send `pcm` again.

        Audit finding, 2026-09-06: `_wake_active_until` is ONE timer shared
        by every `_drain_hw_mic` task (there's only one active conversation
        at a time, not one per source) — but if BOTH a built-in AND a USB
        mic are configured simultaneously, checking only "is the window
        open" here let EITHER source's task treat an open window as license
        to forward, regardless of which source actually opened it. Saying
        the wake word into ONE mic used to make BOTH start streaming to
        Gemini at once — exactly the "two sources feeding Gemini
        simultaneously" outcome app/robot_mic_state.py's docstring already
        warns the exclusive `active_source` field exists to prevent. This
        method is the fix: a source may only ride an open window, or open a
        new one, while `robot_mic_state.active_source` is either itself or
        "browser" (idle) — never while a DIFFERENT source currently owns it.

        UX audit finding, 2026-09-06: `wake_active_window_s` (12s default)
        is tuned for ordinary conversational back-and-forth, but the G1
        visit-intake interview can have a visitor pause well past that —
        recalling an ID number, digging a pass out of a bag — and the
        window used to expire right under them mid-registration: their
        eventual answer was silently dropped and they had to say the wake
        phrase again with no indication why, in the middle of a formal
        registration flow where that reads as the robot malfunctioning, not
        as expected behaviour. While a visit-intake interview is actively
        engaged (app/visit_intake.py), the window this source already owns
        simply never expires — see `intake_keeps_awake` below — so a slow
        answer is still heard whenever it comes. Ordinary conversation
        outside an interview keeps the normal 12s timeout.
        """
        now = time.monotonic()
        intake_tool = self._intake_tool()
        intake_keeps_awake = intake_tool is not None and getattr(intake_tool, "engaged", False)
        is_awake = intake_keeps_awake or now < self._wake_active_until
        owns_window = robot_mic_state.active_source == source
        if is_awake:
            return owns_window  # someone else's turn -- stay silent, don't even feed
        if owns_window:
            # The window we owned just expired -- go back to sleep. (Only
            # the owning source ever makes this transition; the others were
            # already excluded by `owns_window` being False for them.)
            robot_mic_state.active_source = "browser"
            await self._broadcast_json(self._capabilities_message())
            return False
        if robot_mic_state.active_source not in (source, "browser"):
            return False  # a different source has the floor -- don't even try to wake
        if source != self._preferred_mic_input:
            # 2026-09-06 ask: "разделения и параллельного слушания не
            # должно быть... оператор сам выбирает". Both hardware mics'
            # background tasks run regardless (see __init__ — that's what
            # makes switching instant), but only the PREFERRED one may ever
            # run wake-word inference. Without this, a robot with both a
            # built-in AND a USB mic configured would run faster-whisper
            # TWICE, once per source, racing to see which "wins" — wasted
            # CPU and an ambiguous answer to "which mic actually wakes it".
            return False
        # await, not a plain call: WakeWordDetector.feed runs real Whisper
        # inference off-thread (see its docstring) — this suspends only
        # THIS task while that runs, the event loop keeps serving everyone
        # else.
        detector = self._wake_detectors[source]
        if not await detector.feed(pcm):
            return False
        self._wake_active_until = time.monotonic() + self.settings.wake_active_window_s
        robot_mic_state.active_source = source
        _LOG.info("session_hub.wake_word_detected source=%s", source)
        await self._broadcast_json(self._capabilities_message())
        # 2026-09-06 audit finding (prompted by a direct question: "won't
        # the robot miss the beginning of the sentence?"): feed() needs a
        # full window_s-second buffer to even attempt a check, so the wake
        # phrase itself -- and whatever the visitor said in the same
        # breath right up to this instant -- was ALREADY spoken by the
        # time this fires. Forwarding only `pcm` (the single ~50ms chunk
        # being fed right now, as the caller normally would) meant Gemini
        # never heard any of that: only the tail end onward. Replaying the
        # detector's own sliding buffer (see WakeWordDetector.recent_audio)
        # recovers it -- it already ends with `pcm`, so returning False
        # here (not True) tells the caller NOT to also forward `pcm`
        # itself, which would otherwise repeat that one trailing chunk.
        await self.adapter.send_audio(detector.recent_audio)
        return False

    async def _presence_task(self) -> None:
        """Greets a visitor once they appear (R1 ТЗ §4.1 / G1 ТЗ §3.1). One
        instance for the whole process — previously this ran once PER
        connected browser tab, so a visitor could be greeted multiple times
        if more than one tab happened to be open.

        Runs its own true-infinite-loop retry-with-backoff (same contract
        `_supervised`'s other tasks rely on it enforcing for THEM) instead
        of being wrapped by `_supervised` itself: presence is a
        supplementary feature layered on the voice pipeline (2026-09-06 UX
        ask called it "скорее дополнение", not core), not a hardware I/O
        path like the mic/speaker tasks that the robot is genuinely unusable
        without. Motion scoring itself already runs out-of-process by
        default (`PRESENCE_ISOLATION=process` — see app/presence.py's
        SubprocessMotionDetector, which never raises out of score() at all,
        it just respawns and reports 0.0 for that frame) — this catch-all is
        the second, outer layer of the same isolation goal: whatever else
        could go wrong here (camera_hub itself raising, say) must cost this
        loop a retry, never the whole robot going silently mute.
        """

        async def on_visitor_detected() -> None:
            _LOG.info("presence.visitor_detected")
            await self.adapter.send_text(PRESENCE_MARKER)

        backoff_s = 1.0
        while True:
            detector = build_motion_detector(self.settings.presence_isolation)
            try:
                runner = PresenceRunner(
                    frames=sampled(camera_hub, fps=self.settings.presence_fps),
                    on_visitor_detected=on_visitor_detected,
                    detector=detector,
                    threshold=self.settings.presence_threshold,
                    rearm_s=self.settings.presence_rearm_s,
                )
                await runner.run()
                _LOG.warning("presence.runner_ended_unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("presence.task_error")
            finally:
                await detector.aclose()
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 30.0)

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
                # Extend (not open) the hardware mic's wake window — a
                # visitor mid-conversation shouldn't have to repeat the wake
                # phrase before every follow-up question. Only extends an
                # ALREADY-open window (never opens one fresh here): a turn
                # driven entirely by the browser/operator panel must not
                # wake the physical mic up on its own.
                if self._wake_active_until > 0:
                    self._wake_active_until = time.monotonic() + self.settings.wake_active_window_s
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
