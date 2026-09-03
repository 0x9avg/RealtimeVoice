"""GeminiLiveAdapter.events() — the reconnect loop wrapping the Gemini Live
WebSocket. Real Gemini connections have no place in a test suite; instead
`websockets.connect` is monkeypatched with a fake async context manager
that hands back scripted messages, so the loop's own logic (proactive
session refresh, catching a broad-enough set of connect/stream errors) is
exercised without any network access.
"""

from __future__ import annotations

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
