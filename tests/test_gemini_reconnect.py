"""GeminiLiveAdapter.events() — the reconnect loop wrapping the Gemini Live
WebSocket. Real Gemini connections have no place in a test suite; instead
`websockets.connect` is monkeypatched with a fake async context manager
that hands back scripted messages, so the loop's own logic (proactive
session refresh, catching a broad-enough set of connect/stream errors) is
exercised without any network access.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

import app.gemini_live as gemini_live_module
from app.config import Settings
from app.gemini_live import ConnectionStateChanged, GeminiLiveAdapter, TurnComplete
from app.tools import ToolRegistry


class FakeWS:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent: list[dict] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


class FakeConnect:
    """Stands in for `async with websockets.connect(...) as ws:`."""

    instances: list[FakeWS] = []

    def __init__(self, url: str, open_timeout: float | None = None) -> None:
        FakeConnect.instances.append(
            FakeWS(
                [
                    json.dumps({"serverContent": {"turnComplete": True}}),
                    json.dumps({"serverContent": {"turnComplete": True}}),
                ]
            )
        )
        self.ws = FakeConnect.instances[-1]

    async def __aenter__(self) -> FakeWS:
        return self.ws

    async def __aexit__(self, *exc: object) -> bool:
        await self.ws.close()
        return False


@pytest.fixture(autouse=True)
def _reset_fake_connect():
    FakeConnect.instances = []
    yield
    FakeConnect.instances = []


async def test_proactive_refresh_swaps_session_between_turns_not_mid_turn(monkeypatch) -> None:
    """max_session_duration_s=0 forces the very first TurnComplete to
    trigger a refresh -- must break cleanly (never drain a second queued
    message from the same connection) and reconnect immediately, no
    backoff delay, no error status."""
    monkeypatch.setattr(gemini_live_module.websockets, "connect", FakeConnect)
    settings = Settings(max_session_duration_s=0.0)
    adapter = GeminiLiveAdapter(settings=settings, tools=ToolRegistry())

    events = []
    async for event in adapter.events():
        events.append(event)
        if len(events) >= 7:
            break
    await adapter.close()

    assert len(FakeConnect.instances) == 2, "expected exactly one reconnect"
    for ws in FakeConnect.instances:
        assert len(ws._messages) == 1, "should stop after ONE TurnComplete, not drain the connection"
        assert ws.closed

    closed_events = [e for e in events if isinstance(e, ConnectionStateChanged) and e.state == "closed"]
    assert closed_events
    assert all(e.detail == "proactive session refresh" for e in closed_events)


async def test_stale_queued_messages_are_dropped_once_a_new_session_opens(monkeypatch) -> None:
    """2026-09-06 audit finding: send_audio()/send_text() enqueue into
    _send_queue with no connectivity check (by design -- the mic-feeding
    task doesn't know or care about connection state), so anything
    captured while disconnected used to sit in the queue and get replayed
    into the NEXT, brand-new session right after `setup`, as if it were
    live -- contradicting the fact a fresh session has no memory of
    anything from before it."""
    monkeypatch.setattr(gemini_live_module.websockets, "connect", FakeConnect)
    adapter = GeminiLiveAdapter(settings=Settings(), tools=ToolRegistry())

    # Simulate audio/text captured while disconnected, before this
    # connection attempt even exists.
    await adapter.send_text("stale text from before the drop")
    assert adapter._send_queue.qsize() == 1

    events = []
    async for event in adapter.events():
        events.append(event)
        if len(events) >= 2:  # "connecting", "connected"
            break
    await adapter.close()

    assert adapter._send_queue.qsize() == 0, (
        "a message queued before this connection opened must be discarded, "
        "not replayed into the new session"
    )


class SendFailsWS:
    """Receive side looks alive forever (`__anext__` never resolves on its
    own -- standing in for a connection that's still nominally open, just
    quiet); `send()` succeeds ONCE (the initial `setup` message events()
    sends directly, before yielding "connected") and raises on every call
    after that. Models a half-broken connection where only the SEND
    direction fails, and only after the session was genuinely established
    -- nothing guarantees both directions of one TCP connection fail
    atomically."""

    def __init__(self) -> None:
        self.closed = False
        self._never = asyncio.Event()
        self._send_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._never.wait()  # never resolves within a test's lifetime
        raise StopAsyncIteration  # unreachable

    async def send(self, data: str) -> None:  # noqa: ARG002 — content doesn't matter
        self._send_calls += 1
        if self._send_calls == 1:
            return  # the setup message -- let the connection actually open
        raise ConnectionResetError("simulated send-side failure")

    async def close(self) -> None:
        self.closed = True


class SendFailsThenOkConnect:
    """First connection's ws has a send-side-only failure; the second is a
    normal, healthy FakeWS -- proves events() actually reconnects instead
    of hanging on the first one forever."""

    instances: list[object] = []

    def __init__(self, url: str, open_timeout: float | None = None) -> None:
        ws = (
            SendFailsWS()
            if not SendFailsThenOkConnect.instances
            else FakeWS([json.dumps({"serverContent": {"turnComplete": True}})])
        )
        SendFailsThenOkConnect.instances.append(ws)
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc: object) -> bool:
        await self.ws.close()
        return False


async def test_a_send_side_failure_triggers_reconnect_instead_of_going_silently_deaf(
    monkeypatch,
) -> None:
    """2026-09-06 stability hardening, prompted by a customer report
    ("голос иногда запинается и очень долго отвечает"): a send() failure
    that doesn't ALSO break the receive side used to leave
    _drain_send_queue dead while the receive loop kept right on iterating
    -- mic audio piled up in _send_queue forever, Gemini heard nothing new
    at all, and nothing reconnected until some UNRELATED event separately
    noticed. Must now abort the pump (and reconnect) the moment the send
    side fails, not wait indefinitely for the receive side to notice on
    its own."""
    SendFailsThenOkConnect.instances = []
    monkeypatch.setattr(gemini_live_module.websockets, "connect", SendFailsThenOkConnect)
    settings = Settings(reconnect_backoff_initial_s=0.01, reconnect_backoff_max_s=0.01)
    adapter = GeminiLiveAdapter(settings=settings, tools=ToolRegistry())

    events_iter = adapter.events()
    connecting = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
    assert connecting.state == "connecting"
    connected = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
    assert connected.state == "connected"

    # Queue this AFTER the connection is up (and past the connect-time
    # stale-queue discard) so it's guaranteed to be what _drain_send_queue
    # is sitting on `.get()` for when send() blows up.
    await adapter.send_audio(b"\x00\x00")

    reconnect_event = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
    assert reconnect_event.state == "closed", (
        "a send-side failure must end the pump (triggering a reconnect), "
        "not leave the receive loop running forever unaware of it"
    )

    # Two more events -- the retry's own "connecting" then "connected" --
    # prove a SECOND connection attempt actually started and succeeded,
    # not just that the first one ended (calling close() right after
    # "closed" would stop the loop before it ever retried, which would
    # pass for the wrong reason). `websockets.connect(...)` itself isn't
    # called until AFTER "connecting" is yielded, so instances only grows
    # once this second `__anext__()` resumes the generator past that point.
    retrying = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
    assert retrying.state == "connecting"
    reconnected = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
    assert reconnected.state == "connected"
    assert len(SendFailsThenOkConnect.instances) >= 2, "must actually reconnect after the failure"

    await adapter.close()
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError):
        await asyncio.wait_for(events_iter.__anext__(), timeout=1.0)


async def test_events_reconnects_on_connect_errors_beyond_connectionclosed(monkeypatch) -> None:
    """Regression: the reconnect loop used to catch only
    (ConnectionClosed, OSError, TimeoutError) -- anything else raised by
    websockets.connect() (bad API key, quota rejection, an odd
    InvalidHandshake/InvalidURI) escaped uncaught and killed the whole
    session permanently. Must now retry on any Exception."""

    class ExplodingConnect:
        call_count = 0

        def __init__(self, url: str, open_timeout: float | None = None) -> None:
            ExplodingConnect.call_count += 1

        async def __aenter__(self):
            raise gemini_live_module.websockets.exceptions.InvalidURI("ws://bad", "not a real reason")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(gemini_live_module.websockets, "connect", ExplodingConnect)
    settings = Settings(reconnect_backoff_initial_s=0.01, reconnect_backoff_max_s=0.01)
    adapter = GeminiLiveAdapter(settings=settings, tools=ToolRegistry())

    states = []
    async for event in adapter.events():
        states.append(event)
        if len(states) >= 4:
            break
    await adapter.close()

    assert ExplodingConnect.call_count >= 2, "must retry after the exotic exception, not die"
    closed = [e for e in states if isinstance(e, ConnectionStateChanged) and e.state == "closed"]
    assert closed
