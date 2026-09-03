"""SessionHub — the one shared Gemini Live session all browsers subscribe to.

No real Gemini/network connection here: SessionHub.adapter is swapped for a
fake with the same async `events()`/`close()` shape so these tests run
fully offline. `_broadcast_json`/`_broadcast_bytes` are exercised directly
(bypassing the real /ws/session FastAPI route, which is covered separately
in test_ws_auth.py) to keep these tests focused on the hub's own
join/leave/broadcast/history/resilience contract.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.gemini_live import Transcript
from app.session_hub import SessionHub


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail = False

    async def send_json(self, data: object) -> None:
        if self.fail:
            raise RuntimeError("closed")
        self.sent.append(("json", data))

    async def send_bytes(self, data: bytes) -> None:
        if self.fail:
            raise RuntimeError("closed")
        self.sent.append(("bytes", data))


def make_hub() -> SessionHub:
    return SessionHub(Settings())


async def test_join_sends_capabilities() -> None:
    hub = make_hub()
    ws = FakeWS()
    await hub.join(ws)
    assert ws.sent[0] == ("json", {"type": "capabilities", "robot_mic": False, "robot_mic_active": False})


async def test_late_joiner_replays_history_same_page_as_everyone_else() -> None:
    hub = make_hub()
    a = FakeWS()
    await hub.join(a)

    await hub._broadcast_json({"type": "transcript", "role": "model", "text": "hello"}, remember=True)

    b = FakeWS()
    await hub.join(b)
    types_seen_by_b = [msg["type"] for _, msg in b.sent]
    assert "capabilities" in types_seen_by_b
    assert "transcript" in types_seen_by_b, f"late joiner did not get replayed history: {types_seen_by_b}"


async def test_broadcast_reaches_every_subscriber() -> None:
    hub = make_hub()
    a, b = FakeWS(), FakeWS()
    await hub.join(a)
    await hub.join(b)

    await hub._broadcast_json({"type": "turn_complete"})

    assert ("json", {"type": "turn_complete"}) in a.sent
    assert ("json", {"type": "turn_complete"}) in b.sent


async def test_leave_does_not_affect_other_subscribers() -> None:
    hub = make_hub()
    a, b = FakeWS(), FakeWS()
    await hub.join(a)
    await hub.join(b)

    hub.leave(a)
    assert a not in hub.subscribers
    assert b in hub.subscribers

    before = len(b.sent)
    await hub._broadcast_json({"type": "status", "state": "connected", "detail": ""})
    assert len(b.sent) == before + 1


async def test_dead_socket_is_dropped_without_breaking_broadcast_to_others() -> None:
    hub = make_hub()
    ok, dead = FakeWS(), FakeWS()
    await hub.join(ok)
    await hub.join(dead)
    dead.fail = True

    await hub._broadcast_json({"type": "status", "state": "connected", "detail": "x"})

    assert dead not in hub.subscribers
    assert ok in hub.subscribers


class FakeAdapter:
    """Yields a fixed sequence of events, then stays idle -- lets a test
    drive SessionHub._pump_gemini deterministically without a real socket."""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def events(self):
        for event in self._events:
            yield event
        await asyncio.sleep(1000)


async def test_one_bad_event_does_not_kill_the_pump_loop() -> None:
    """Regression: an exception while handling one event (e.g. a broken
    tool payload, a speaker/DDS hiccup) used to kill _pump_gemini entirely
    -- the process kept answering HTTP while the robot went permanently,
    silently mute. _handle_event failures must be isolated per-event."""
    hub = make_hub()
    hub.adapter = FakeAdapter(
        [
            Transcript(role="model", text="ok-before"),
            Transcript(role="model", text="ok-after"),
        ]
    )

    calls: list[dict] = []
    should_raise = {"ok-before"}

    async def flaky_broadcast(message: dict, **kwargs: object) -> None:
        calls.append(message)
        if message.get("text") in should_raise:
            should_raise.discard(message["text"])
            raise RuntimeError("simulated bug in event handling")

    hub._broadcast_json = flaky_broadcast

    task = asyncio.create_task(hub._pump_gemini())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    texts = [c["text"] for c in calls if c.get("type") == "transcript"]
    assert "ok-before" in texts
    assert "ok-after" in texts, "pump loop must keep going after one handler raises"


async def test_supervised_cancellation_never_hard_exits() -> None:
    """_supervised()'s os._exit(1) safety net must fire ONLY on a genuine
    unexpected task death -- never on ordinary cancellation (ctrl-C /
    SessionHub.stop()). If this ever regresses, it kills the whole test
    process instead of raising CancelledError -- that's the assertion."""
    hub = make_hub()

    async def cancels_immediately() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hub._supervised(cancels_immediately, "test")

    async def sleeps_forever() -> None:
        await asyncio.sleep(1000)

    task = asyncio.create_task(hub._supervised(sleeps_forever, "test2"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
