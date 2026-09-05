"""Gemini Live (BidiGenerateContent) realtime voice adapter.

Protocol details — the WS URL, and the `setup`/`realtimeInput`/
`clientContent`/`toolCall`/`toolResponse` message shapes, the PCM16
16kHz-send/24kHz-receive convention — are carried over from a working
reference implementation in the sibling `humanoid-robot` repo:
`packages/humanoid-robot-adapters-live-gemini/src/humanoid_robot/adapters/
live_gemini/client.py`, already proven against a real customer key. This
module strips out everything specific to the robot (ALSA audio devices, the
platform event bus, gestures, cross-session memory) and keeps only what a
browser-driven service needs, plus two things that version doesn't use:
text-turn input (`clientContent`) and input/output transcription.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import Settings
from app.tools import ToolRegistry

_LOG = logging.getLogger("gemini_live")

# --------------------------------------------------------------------------
# Events yielded by a RealtimeAdapter
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AudioChunk:
    """Raw PCM16 audio from the model, at `settings.receive_sample_rate_hz`."""

    pcm: bytes


@dataclass(slots=True, frozen=True)
class ToolCall:
    """A function call the model wants executed; the caller must `respond()`."""

    call_id: str
    name: str
    args: dict[str, Any]
    _adapter: GeminiLiveAdapter = field(repr=False)

    async def respond(self, result: str) -> None:
        await self._adapter._send_tool_response(self.call_id, self.name, result)


@dataclass(slots=True, frozen=True)
class TurnComplete:
    """The model finished speaking."""


@dataclass(slots=True, frozen=True)
class Interrupted:
    """The user barged in — discard any buffered/queued playback audio."""


@dataclass(slots=True, frozen=True)
class ConnectionStateChanged:
    state: Literal["connecting", "connected", "closed"]
    detail: str = ""


@dataclass(slots=True, frozen=True)
class Transcript:
    role: Literal["user", "model"]
    text: str


LiveEvent = (
    AudioChunk | ToolCall | TurnComplete | Interrupted | ConnectionStateChanged | Transcript
)


class RealtimeAdapterError(RuntimeError):
    """Non-recoverable client-side error (e.g. no API key configured)."""


class RealtimeAdapter(Protocol):
    """Provider-agnostic interface for one realtime speech-to-speech session.

    Keeping this thin means the browser<->backend bridge in `main.py` never
    has to change if a second provider is added later — only a new class
    implementing this Protocol. No second implementation exists yet; this is
    the seam, not a speculative abstraction layer.
    """

    async def send_audio(self, pcm16: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class GeminiLiveAdapter:
    """One logical Gemini Live session. Reconnects internally with backoff.

    Usage::

        adapter = GeminiLiveAdapter(settings=settings, tools=tools)
        async for event in adapter.events():
            match event:
                case AudioChunk(pcm=pcm):
                    ...
                case ToolCall() as call:
                    ...

    Feed microphone audio from a concurrent task via `send_audio()` — do not
    call it from inside the `events()` consumer loop, it would serialize
    behind the receive loop.
    """

    settings: Settings
    tools: ToolRegistry
    # Asked before a PROACTIVE session swap (never before a forced
    # reconnect — that isn't ours to postpone). Returning False defers the
    # swap to a later turn: app/session_hub.py uses it to avoid dropping a
    # fresh session on top of a half-finished visit interview.
    refresh_guard: Callable[[], bool] | None = None
    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _send_queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False)

    # -- public API ----------------------------------------------------

    async def send_audio(self, pcm16: bytes) -> None:
        """Enqueue a chunk of mono PCM16 microphone audio."""
        if not pcm16:
            return
        await self._send_queue.put(
            {
                "realtimeInput": {
                    "audio": {
                        "mimeType": f"audio/pcm;rate={self.settings.send_sample_rate_hz}",
                        "data": base64.b64encode(pcm16).decode("ascii"),
                    }
                }
            }
        )

    async def send_text(self, text: str) -> None:
        """Inject a typed user turn into the live session."""
        if not text.strip():
            return
        await self._send_queue.put(
            {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": text}]}],
                    "turnComplete": True,
                }
            }
        )

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Connect, stay connected with exponential backoff, yield events.

        Also proactively swaps to a fresh Gemini session once the current
        one has been open past `settings.max_session_duration_s` — see that
        setting's docstring for why (Gemini itself eventually kills a
        long-lived session with a 1008, and per-turn latency creeps up
        beforehand). The swap only happens right after a TurnComplete, never
        mid-turn, so it's never audible as a cut-off response — from the
        caller's point of view it looks exactly like the ordinary
        "connecting" -> "connected" cycle already used for real
        disconnects, just without the backoff delay (this one isn't an
        error, so there's nothing to back off from).

        Stops iterating once `close()` has been called.
        """
        backoff = self.settings.reconnect_backoff_initial_s
        while not self._closed:
            yield ConnectionStateChanged(state="connecting")
            proactive_refresh = False
            try:
                async with websockets.connect(
                    self._ws_url(), open_timeout=self.settings.open_timeout_s
                ) as ws:
                    self._ws = ws
                    backoff = self.settings.reconnect_backoff_initial_s
                    # 2026-09-06 audit finding: send_audio()/send_text() keep
                    # enqueueing into _send_queue even while disconnected
                    # (there's no connectivity check there, by design — the
                    # mic-feeding task doesn't know or care about connection
                    # state). Left alone, whatever queued up during the
                    # outage got replayed into this BRAND-NEW session right
                    # after setup, as if it were live — audio or typed text
                    # from before the drop surfacing after reconnect,
                    # contradicting this class's own assumption elsewhere
                    # that a fresh session has no memory of anything before
                    # it. Dropping it here is the right call: it's stale by
                    # definition (captured for a session that no longer
                    # exists), and this also caps how much can pile up
                    # in the queue during one outage instead of growing
                    # unboundedly for the whole `reconnect_backoff_max_s`
                    # wait.
                    self._discard_stale_queue()
                    await ws.send(json.dumps(self._build_setup_message()))
                    yield ConnectionStateChanged(state="connected")
                    session_opened = time.monotonic()
                    async for event in self._pump(ws):
                        yield event
                        if (
                            isinstance(event, TurnComplete)
                            and time.monotonic() - session_opened
                            >= self.settings.max_session_duration_s
                            and (self.refresh_guard is None or self.refresh_guard())
                        ):
                            proactive_refresh = True
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — deliberately broad, see below
                # Anything from here down is a "the connection attempt or an
                # in-flight message failed" case, not just the couple of
                # exception types this used to name explicitly
                # (ConnectionClosed/OSError/TimeoutError). websockets.connect
                # can also raise InvalidHandshake/InvalidStatusCode/
                # InvalidURI (bad API key, quota rejection, a proxy/DNS
                # hiccup with an unusual error type) — none of those are
                # ConnectionClosed subclasses, so the narrower catch used to
                # let them escape uncaught and permanently kill this
                # generator (and, one level up, the whole session — see
                # app/session_hub.py's SessionHub._pump_gemini for the
                # matching fix on the consumer side). Every one of these is
                # something a reconnect-with-backoff can plausibly recover
                # from (a bad key won't fix itself, but retrying costs
                # nothing and the alternative is silently going mute for
                # the rest of the process's life).
                yield ConnectionStateChanged(state="closed", detail=str(exc))
            finally:
                self._ws = None
            if self._closed:
                break
            if proactive_refresh:
                yield ConnectionStateChanged(state="closed", detail="proactive session refresh")
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.settings.reconnect_backoff_max_s)

    def _discard_stale_queue(self) -> None:
        """Drop anything enqueued before this (re)connection — see the call
        site's comment. Best-effort: a message enqueued by a concurrent
        `send_audio()`/`send_text()` call in the exact instant this runs
        can still slip through, which is fine — the goal is bounding
        staleness to "at most a few milliseconds old", not a hard
        real-time guarantee."""
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # -- internals -------------------------------------------------------

    def _ws_url(self) -> str:
        api_key = self.settings.resolved_api_key()
        if not api_key:
            msg = f"no Gemini API key: set env {self.settings.api_key_env!r}"
            raise RealtimeAdapterError(msg)
        return (
            f"wss://{self.settings.ws_host}/ws/"
            "google.ai.generativelanguage.v1alpha.GenerativeService"
            f".BidiGenerateContent?key={api_key}"
        )

    def _build_setup_message(self) -> dict[str, Any]:
        setup: dict[str, Any] = {
            "model": self.settings.model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": self._speech_config(),
            },
            # googleSearch — Gemini Live's own built-in Google Search
            # grounding (item 7 of the 2026-09-04 fixes list): lets the
            # model answer general/current-events questions outside its
            # narrow scope without a separate search API/key. Older Gemini
            # generations disallowed mixing search grounding with custom
            # function declarations in one request; this model generation
            # allows both together (see DEPLOYMENT.md's checklist for how
            # to re-verify against your own account/model if you swap
            # GEMINI_LIVE_MODEL).
            "tools": [
                {"functionDeclarations": self.tools.function_declarations()},
                {"googleSearch": {}},
            ],
            "systemInstruction": {"parts": [{"text": self.settings.system_instruction}]},
        }
        if self.settings.enable_transcription:
            # Empty objects, not flags — presence enables it. Lets the UI show
            # what the model heard and said, useful for debugging tool calls.
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
        if self.settings.context_window_compression_enabled:
            # A sibling of generationConfig at this level, NOT nested inside
            # it (confirmed against Google's BidiGenerateContentSetup
            # reference — easy to get wrong since it's easy to assume this
            # lives alongside responseModalities). See
            # Settings.context_window_compression_enabled's docstring for
            # why this exists alongside, not instead of,
            # max_session_duration_s's own periodic reconnect.
            setup["contextWindowCompression"] = self._context_window_compression_config()
        return {"setup": setup}

    def _context_window_compression_config(self) -> dict[str, Any]:
        sliding_window: dict[str, Any] = {}
        if self.settings.context_window_compression_target_tokens is not None:
            sliding_window["targetTokens"] = self.settings.context_window_compression_target_tokens
        config: dict[str, Any] = {"slidingWindow": sliding_window}
        if self.settings.context_window_compression_trigger_tokens is not None:
            config["triggerTokens"] = self.settings.context_window_compression_trigger_tokens
        return config

    def _speech_config(self) -> dict[str, Any]:
        speech: dict[str, Any] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.settings.voice_name}}
        }
        if self.settings.language_code:
            speech["languageCode"] = self.settings.language_code
        return speech

    async def _send_tool_response(self, call_id: str, name: str, result: str) -> None:
        """Deliver a tool result, or say loudly why it couldn't be.

        The model blocks its turn waiting for this message, so a silently
        dropped response is the robot going mute mid-answer with nothing in
        the log to explain it. That's what used to happen whenever the
        socket flipped between the toolCall arriving and the result being
        ready (a reconnect, or Gemini closing a long session): `if
        self._ws is None: return`, no trace. The turn is unrecoverable
        either way — a reconnect starts a fresh session — but it must be
        diagnosable, so this logs instead of swallowing.
        """
        ws = self._ws
        if ws is None:
            _LOG.warning(
                "gemini.tool_response_undeliverable name=%s call_id=%s reason=no_connection",
                name,
                call_id,
            )
            return
        payload = json.dumps(
            {
                "toolResponse": {
                    "functionResponses": [
                        {"id": call_id, "name": name, "response": {"result": result}}
                    ]
                }
            }
        )
        try:
            await ws.send(payload)
        except Exception as exc:  # noqa: BLE001 — connection died mid-send
            _LOG.warning(
                "gemini.tool_response_undeliverable name=%s call_id=%s reason=%s",
                name,
                call_id,
                exc,
            )

    async def _pump(self, ws: ClientConnection) -> AsyncIterator[LiveEvent]:
        """Drain the outbound queue and the inbound socket concurrently.

        2026-09-06 stability hardening, prompted by a customer report
        ("голос иногда запинается и очень долго отвечает"): the receive
        loop (`async for raw in ws`) and the send loop
        (`_drain_send_queue`) used to be independent — if `ws.send()` ever
        raised for a reason that didn't ALSO immediately break the receive
        side (nothing guarantees the two directions of one TCP connection
        fail atomically; a half-open socket, a proxy hiccup, or a
        write-side-only error are all real possibilities), the sender task
        died silently while this loop kept right on yielding whatever
        Gemini still had queued to say. From the outside that looks exactly
        like "connected and working" — mic audio piles up in `_send_queue`
        forever, Gemini hears nothing new at all, and the ONLY way out was
        whatever unrelated event eventually noticed the receive side was
        also dead (or the next proactive refresh). Racing the two here
        means a send failure now ends the pump immediately — reconnecting
        exactly like a receive-side failure already does — instead of
        leaving the session quietly deaf for however long it takes
        something else to notice.
        """
        sender = asyncio.create_task(self._drain_send_queue(ws), name="gemini-live-send")
        receiver = ws.__aiter__()
        try:
            while True:
                recv_task = asyncio.ensure_future(receiver.__anext__())
                done, _pending = await asyncio.wait(
                    {recv_task, sender}, return_when=asyncio.FIRST_COMPLETED
                )
                if recv_task in done:
                    try:
                        raw = recv_task.result()
                    except StopAsyncIteration:
                        return
                    data = json.loads(raw)
                    for event in self._events_from_message(data):
                        yield event
                    continue
                # `sender` finished first. It's an infinite loop by design
                # (see _drain_send_queue), so this only happens if it
                # raised — surface that so events()'s reconnect handling
                # runs, instead of the pump just quietly stalling forever
                # with the receive side still nominally "connected".
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv_task
                exc = sender.exception()
                raise exc if exc is not None else RealtimeAdapterError(
                    "gemini.send_task_ended_unexpectedly"
                )
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def _drain_send_queue(self, ws: ClientConnection) -> None:
        while True:
            msg = await self._send_queue.get()
            await ws.send(json.dumps(msg))

    def _events_from_message(self, data: dict[str, Any]) -> list[LiveEvent]:
        events: list[LiveEvent] = []
        server_content = data.get("serverContent")
        if server_content:
            model_turn = server_content.get("modelTurn")
            if model_turn:
                for part in model_turn.get("parts", []):
                    inline = part.get("inlineData")
                    if inline and inline.get("data"):
                        events.append(AudioChunk(pcm=base64.b64decode(inline["data"])))
            input_transcription = server_content.get("inputTranscription")
            if input_transcription and input_transcription.get("text"):
                events.append(Transcript(role="user", text=input_transcription["text"]))
            output_transcription = server_content.get("outputTranscription")
            if output_transcription and output_transcription.get("text"):
                events.append(Transcript(role="model", text=output_transcription["text"]))
            if server_content.get("interrupted"):
                events.append(Interrupted())
            if server_content.get("turnComplete"):
                events.append(TurnComplete())
        events.extend(
            ToolCall(
                call_id=call.get("id", ""),
                name=call.get("name", ""),
                args=call.get("args", {}) or {},
                _adapter=self,
            )
            for call in data.get("toolCall", {}).get("functionCalls", [])
        )
        return events
