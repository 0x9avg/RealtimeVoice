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
import logging
import time

import pytest

from pathlib import Path

from app import env_file
from app.config import Settings
from app.gemini_live import AudioChunk, Interrupted, Transcript, TurnComplete
from app.env_file import read_values
from app.robot_mic_state import robot_mic_state
from app.session_hub import SessionHub


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # The hub now writes an operator's live audio choice back to `.env`
    # (see _remember_audio_choice) -- without this redirect, running the
    # suite would rewrite the developer's OWN `.env` at the project root.
    monkeypatch.setattr(env_file, "ENV_PATH", tmp_path / ".env")
    return tmp_path / ".env"


@pytest.fixture(autouse=True)
def _reset_hw_mic_state():
    # robot_mic_state is a process-wide singleton (see its module docstring)
    # -- without a reset, whichever hardware-mic-choice test ran last would
    # leak its "active_source" into every test after it, including the
    # capabilities assertion below.
    robot_mic_state.active_source = "browser"
    yield
    robot_mic_state.active_source = "browser"


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


# -- AUDIO_INPUT / USB mic: both constructed, ONE preferred at runtime ------
# 2026-09-06 ask, explicit: "Разделения и параллельного слушания не должно
# быть. Работает или/или. Оператор сам выбирает" -- AND, in a follow-up,
# "микрофон... выбирается не посредством .env файла, а напрямую на сайте".
# So both hardware mics are constructed together whenever AUDIO_INPUT is
# "robot"/"usb" (mirroring _speakers' pattern, for instant no-restart
# switching -- see set_preferred_mic_input), but only ONE of them is ever
# allowed to run wake-word inference (see _hw_mic_owns_wake_window's
# _preferred_mic_input gate, tested separately below).


def test_both_mics_are_constructed_together_when_audio_input_is_robot_or_usb() -> None:
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device="hw:MS,0"))
    assert hub.mic is not None
    assert hub.usb_mic is not None

    hub2 = SessionHub(Settings(audio_input="usb", usb_mic_alsa_device="hw:MS,0"))
    assert hub2.mic is not None
    assert hub2.usb_mic is not None


def test_usb_mic_is_not_constructed_without_a_configured_alsa_device() -> None:
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device=""))
    assert hub.mic is not None
    assert hub.usb_mic is None


def test_neither_mic_is_constructed_for_browser_input_even_with_a_usb_device_set() -> None:
    """browser input means no hardware-input mode at all -- same convention
    _speakers already uses for AUDIO_OUTPUT=browser."""
    hub = SessionHub(Settings(audio_input="browser", usb_mic_alsa_device="hw:MS,0"))
    assert hub.mic is None
    assert hub.usb_mic is None


def test_audio_input_only_picks_the_initial_preferred_mic() -> None:
    """AUDIO_INPUT is just the STARTING preference for a fresh process --
    the actual choice lives in _preferred_mic_input and is meant to be
    changed live from the website afterward (set_preferred_mic_input)."""
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device="hw:MS,0"))
    assert hub._preferred_mic_input == "robot"

    hub2 = SessionHub(Settings(audio_input="usb", usb_mic_alsa_device="hw:MS,0"))
    assert hub2._preferred_mic_input == "usb"


async def test_set_preferred_mic_input_switches_live_and_is_remembered(
    _isolate_env_file: Path,
) -> None:
    """Switching is instant (both mics stay constructed, nothing re-inits),
    AND the choice is written back to `.env` so a restart doesn't silently
    revert to whatever the setup panel last wrote -- see
    _remember_audio_choice."""
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device="hw:MS,0"))
    assert hub._preferred_mic_input == "robot"

    await hub.set_preferred_mic_input("usb")

    assert hub._preferred_mic_input == "usb"
    # Still both constructed -- switching again must be instant, no re-init.
    assert hub.mic is not None
    assert hub.usb_mic is not None
    assert read_values(_isolate_env_file)["AUDIO_INPUT"] == "usb"


async def test_audio_output_switch_is_remembered(_isolate_env_file: Path) -> None:
    hub = make_hub()
    hub._speakers = {"robot": FakeOutputSpeaker(), "usb": FakeOutputSpeaker()}
    hub._active_speaker_target = "robot"

    await hub.handle_audio_output("usb")

    assert read_values(_isolate_env_file)["AUDIO_OUTPUT"] == "usb"


async def test_a_no_op_switch_writes_nothing(_isolate_env_file: Path) -> None:
    """A request for an unconfigured source is already a no-op for the live
    state; it must not leave a stray line in `.env` claiming otherwise."""
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device=""))

    await hub.set_preferred_mic_input("usb")  # not configured

    assert not _isolate_env_file.exists()


async def test_a_failed_env_write_does_not_break_the_live_switch(
    _isolate_env_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A read-only checkout must not turn "switch my microphone" into an
    error in the websocket handler -- the switch has already happened."""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(env_file, "set_values", boom)
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device="hw:MS,0"))

    with caplog.at_level(logging.WARNING):
        await hub.set_preferred_mic_input("usb")

    assert hub._preferred_mic_input == "usb"
    assert any("audio_choice_not_persisted" in r.getMessage() for r in caplog.records)


async def test_set_preferred_mic_input_ignores_a_source_that_is_not_configured() -> None:
    hub = SessionHub(Settings(audio_input="robot", usb_mic_alsa_device=""))
    assert hub._preferred_mic_input == "robot"

    await hub.set_preferred_mic_input("usb")  # not configured -- no-op

    assert hub._preferred_mic_input == "robot"


async def test_join_sends_capabilities() -> None:
    hub = make_hub()
    ws = FakeWS()
    await hub.join(ws)
    await asyncio.sleep(0.05)
    assert ws.sent[0] == (
        "json",
        {
            "type": "capabilities",
            "robot_mic": False,
            "robot_mic_active": False,
            "usb_mic": False,
            "usb_mic_active": False,
            "preferred_mic_input": None,
            "speaker_targets": [],
            "active_speaker_target": None,
        },
    )


async def test_hardware_mic_choice_is_exclusive() -> None:
    """Regression guard for the thing app/robot_mic_state.py's docstring
    warns about: the robot head-mic and an external USB mic must never both
    read as "active" at once -- both would feed the same physical speaker/
    HalfDuplexGate, so picking one must always deselect the other, not add
    to a set of active sources."""
    hub = make_hub()
    hub.mic = object()  # sentinel: handle_mic_source only checks is-not-None
    hub.usb_mic = object()

    await hub.handle_mic_source("robot")
    assert robot_mic_state.active_source == "robot"

    await hub.handle_mic_source("usb")
    assert robot_mic_state.active_source == "usb", "switching to usb must deselect robot"

    await hub.handle_mic_source("browser")
    assert robot_mic_state.active_source == "browser"


async def test_mic_source_change_is_broadcast_to_every_open_tab() -> None:
    """Regression: the hardware mic choice is global (app/robot_mic_state.py
    -- one robot, one physical mic in use at a time), but a tab that was
    ALREADY connected when someone else changed it used to never find out --
    capabilities was only ever sent on join(), not broadcast on change. A
    second operator's tab would keep showing the mic as off (or as the
    wrong source) indefinitely, even though the server had already started
    (or stopped) forwarding that audio to Gemini."""
    hub = make_hub()
    hub.mic = object()
    hub.usb_mic = object()
    a, b = FakeWS(), FakeWS()
    await hub.join(a)
    await hub.join(b)
    a.sent.clear()
    b.sent.clear()

    await hub.handle_mic_source("usb")
    await asyncio.sleep(0.05)  # let each subscriber's writer task drain its queue

    expected = (
        "json",
        {
            "type": "capabilities",
            "robot_mic": True,
            "robot_mic_active": False,
            "usb_mic": True,
            "usb_mic_active": True,
            "preferred_mic_input": None,
            "speaker_targets": [],
            "active_speaker_target": None,
        },
    )
    assert expected in a.sent, "the tab that made the change must see it confirmed too"
    assert expected in b.sent, "an already-open OTHER tab must learn about the change live"


async def test_mic_source_ignored_when_hardware_not_configured() -> None:
    """A process with no robot mic and no usb mic wired up (the default —
    both AUDIO_INPUT and USB_MIC_ALSA_DEVICE unset) must not let a stray
    "robot"/"usb" WS message flip global state for hardware that isn't
    there — there's nothing to actually feed Gemini from in that case."""
    hub = make_hub()
    assert hub.mic is None
    assert hub.usb_mic is None

    await hub.handle_mic_source("robot")
    assert robot_mic_state.active_source == "browser"

    await hub.handle_mic_source("usb")
    assert robot_mic_state.active_source == "browser"


class FakeOutputSpeaker:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def warm_up(self) -> None:
        return None


async def test_handle_audio_output_switches_target_and_stops_the_old_one() -> None:
    """Item 2 of the 2026-09-05 fixes list: runtime toggle between hardware
    speaker targets. Switching must stop whatever the PREVIOUSLY active
    speaker still had in flight -- not the newly selected one (see
    handle_audio_output's docstring for why ordering matters here)."""
    hub = make_hub()
    robot_speaker = FakeOutputSpeaker()
    usb_speaker = FakeOutputSpeaker()
    hub._speakers = {"robot": robot_speaker, "usb": usb_speaker}
    hub._active_speaker_target = "robot"

    await hub.handle_audio_output("usb")

    assert hub._active_speaker_target == "usb"
    assert hub.speaker is usb_speaker
    assert robot_speaker.stopped is True, "the OLD (robot) speaker must be stopped, not the new one"
    assert usb_speaker.stopped is False


async def test_handle_audio_output_ignores_unconfigured_or_already_active_target() -> None:
    hub = make_hub()
    robot_speaker = FakeOutputSpeaker()
    hub._speakers = {"robot": robot_speaker}
    hub._active_speaker_target = "robot"

    await hub.handle_audio_output("usb")  # not configured -- no-op
    assert hub._active_speaker_target == "robot"
    assert robot_speaker.stopped is False

    await hub.handle_audio_output("robot")  # already active -- no-op
    assert robot_speaker.stopped is False


async def test_audio_output_switch_is_broadcast_to_every_open_tab() -> None:
    hub = make_hub()
    hub._speakers = {"robot": FakeOutputSpeaker(), "usb": FakeOutputSpeaker()}
    hub._active_speaker_target = "robot"
    a, b = FakeWS(), FakeWS()
    await hub.join(a)
    await hub.join(b)
    a.sent.clear()
    b.sent.clear()

    await hub.handle_audio_output("usb")
    await asyncio.sleep(0.05)

    for ws in (a, b):
        types_and_targets = [
            msg.get("active_speaker_target") for _, msg in ws.sent if msg.get("type") == "capabilities"
        ]
        assert "usb" in types_and_targets, "every open tab must learn about the switch live"


async def test_late_joiner_replays_history_same_page_as_everyone_else() -> None:
    hub = make_hub()
    a = FakeWS()
    await hub.join(a)

    await hub._broadcast_json({"type": "transcript", "role": "model", "text": "hello"}, remember=True)

    b = FakeWS()
    await hub.join(b)
    await asyncio.sleep(0.05)
    types_seen_by_b = [msg["type"] for _, msg in b.sent]
    assert "capabilities" in types_seen_by_b
    assert "transcript" in types_seen_by_b, f"late joiner did not get replayed history: {types_seen_by_b}"


async def test_broadcast_reaches_every_subscriber() -> None:
    hub = make_hub()
    a, b = FakeWS(), FakeWS()
    await hub.join(a)
    await hub.join(b)

    await hub._broadcast_json({"type": "turn_complete"})
    await asyncio.sleep(0.05)

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

    await asyncio.sleep(0.05)
    before = len(b.sent)
    await hub._broadcast_json({"type": "status", "state": "connected", "detail": ""})
    await asyncio.sleep(0.05)
    assert len(b.sent) == before + 1


async def test_dead_socket_is_dropped_without_breaking_broadcast_to_others() -> None:
    hub = make_hub()
    ok, dead = FakeWS(), FakeWS()
    await hub.join(ok)
    await hub.join(dead)
    dead.fail = True

    await hub._broadcast_json({"type": "status", "state": "connected", "detail": "x"})
    await asyncio.sleep(0.05)

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


async def test_one_stalled_subscriber_does_not_hold_up_anyone_else() -> None:
    """Regression: broadcasts used to `await ws.send_json(...)` per
    subscriber in a loop, so a browser stuck behind TCP backpressure (a
    phone on bad Wi-Fi) blocked the loop — and with it the robot's speech,
    tool calls and barge-in. Each subscriber now drains its own queue."""
    hub = make_hub()

    class StalledWS(FakeWS):
        async def send_json(self, data: object) -> None:
            await asyncio.sleep(30)  # never completes within the test

        async def send_bytes(self, data: bytes) -> None:
            await asyncio.sleep(30)

    stalled, healthy = StalledWS(), FakeWS()
    await hub.join(stalled)
    await hub.join(healthy)

    started = asyncio.get_running_loop().time()
    for i in range(20):
        await hub._broadcast_json({"type": "transcript", "role": "model", "text": f"line {i}"})
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1, f"broadcasting blocked for {elapsed:.2f}s on the stalled client"

    await asyncio.sleep(0.1)
    delivered = [m for _, m in healthy.sent if m.get("type") == "transcript"]
    assert len(delivered) == 20, f"healthy subscriber only got {len(delivered)}/20"


async def test_slow_subscriber_backlog_is_bounded_not_unbounded() -> None:
    """A client that never drains must not grow the robot's memory: its
    queue caps out and drops, rather than buffering forever."""
    from app.session_hub import _OUTBOX_MAXSIZE

    hub = make_hub()

    class StalledWS(FakeWS):
        async def send_json(self, data: object) -> None:
            await asyncio.sleep(30)

    stalled = StalledWS()
    await hub.join(stalled)
    for i in range(_OUTBOX_MAXSIZE * 3):
        await hub._broadcast_json({"type": "transcript", "role": "model", "text": str(i)})

    assert hub._outboxes[stalled].qsize() <= _OUTBOX_MAXSIZE


class PacingSpeaker:
    """Stands in for RobotSpeaker: paces audio out in real time, in 50ms
    chunks, and lets stop() cut an in-flight play short (the _epoch trick
    the real one uses)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._epoch = 0
        self.played_s = 0.0
        self.stopped_at: float | None = None

    async def play(self, pcm: bytes, *, sample_rate_hz: int) -> None:
        async with self._lock:
            epoch = self._epoch
            total = len(pcm) / (sample_rate_hz * 2)
            done = 0.0
            while done < total:
                if self._epoch != epoch:
                    return
                step = min(0.05, total - done)
                await asyncio.sleep(step)
                done += step
                self.played_s += step

    async def stop(self) -> None:
        self._epoch += 1
        async with self._lock:
            if self.stopped_at is None:
                self.stopped_at = asyncio.get_running_loop().time()


async def test_barge_in_stops_playback_immediately_not_after_the_utterance() -> None:
    """Regression: _handle_event used to `await speaker.play(...)`, which
    paces audio in REAL TIME — so the event pump was blocked for the length
    of the utterance and a barge-in arriving mid-sentence was only acted on
    seconds later (measured: 2.4s of the robot talking over the visitor).
    Playback now runs on its own task via _drain_speaker."""
    hub = make_hub()
    speaker = PacingSpeaker()
    # speaker is a read-only property resolving through _speakers/
    # _active_speaker_target (see SessionHub.__init__) -- inject the fake
    # via those instead of the old plain-attribute assignment.
    hub._speakers = {"robot": speaker}
    hub._active_speaker_target = "robot"
    hub.gate = None

    one_second = b"\x00" * (24_000 * 2)

    class FakeAdapter:
        async def events(self):
            for _ in range(3):  # 3s of audio, delivered in a burst
                yield AudioChunk(pcm=one_second)
            await asyncio.sleep(0.3)  # visitor listens, then interrupts
            yield Interrupted()
            await asyncio.sleep(5)

    hub.adapter = FakeAdapter()

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    hub._broadcast_json = noop
    hub._broadcast_bytes = noop

    loop = asyncio.get_running_loop()
    started = loop.time()
    tasks = [
        asyncio.create_task(hub._pump_gemini()),
        asyncio.create_task(hub._drain_speaker()),
    ]
    await asyncio.sleep(1.0)
    for task in tasks:
        task.cancel()

    assert speaker.stopped_at is not None, "barge-in never reached the speaker"
    lag = speaker.stopped_at - started - 0.3
    assert lag < 0.15, f"barge-in took {lag:.2f}s to take effect"
    # and it must NOT have played the whole 3s it had buffered
    assert speaker.played_s < 1.0, f"played {speaker.played_s:.2f}s despite the interrupt"


async def test_turn_complete_closes_the_stream_after_the_audio_not_before() -> None:
    """TurnComplete fires while the tail of the turn is still queued —
    closing the vendor stream (and re-opening the half-duplex mic gate)
    there would cut the last words off and let the robot hear itself."""
    hub = make_hub()
    speaker = PacingSpeaker()
    hub._speakers = {"robot": speaker}
    hub._active_speaker_target = "robot"
    hub.gate = None

    half_second = b"\x00" * (24_000 // 2 * 2)

    class FakeAdapter:
        async def events(self):
            yield AudioChunk(pcm=half_second)
            yield TurnComplete()
            await asyncio.sleep(5)

    hub.adapter = FakeAdapter()

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    hub._broadcast_json = noop
    hub._broadcast_bytes = noop

    tasks = [
        asyncio.create_task(hub._pump_gemini()),
        asyncio.create_task(hub._drain_speaker()),
    ]
    await asyncio.sleep(0.2)
    assert speaker.stopped_at is None, "stream closed while audio was still playing"
    await asyncio.sleep(0.6)
    assert speaker.stopped_at is not None, "stream never closed after the audio finished"
    assert speaker.played_s >= 0.45, f"only played {speaker.played_s:.2f}s of a 0.5s turn"
    for task in tasks:
        task.cancel()


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


class FakeMicThatDies:
    """A hardware mic whose `stream()` ends immediately every call -- the
    same shape `RobotMic`/`UsbMic` present when their underlying source goes
    away (UDP socket closed; `arecord` exited because the USB device was
    unplugged). Used to prove `_drain_hw_mic` retries instead of returning:
    if it ever returned, `_supervised` (see the tests above) would treat
    that as a fatal, unexpected task death and os._exit(1) the whole
    process -- turning "USB mic unplugged" into a full service restart
    mid-conversation."""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self):  # noqa: ANN201 — matches RobotMic/UsbMic's AsyncIterator[bytes]
        self.stream_calls += 1
        return
        yield  # pragma: no cover — makes this an async generator function


async def test_usb_mic_task_retries_instead_of_returning_when_stream_ends() -> None:
    hub = make_hub()
    hub.usb_mic = FakeMicThatDies()

    task = asyncio.create_task(hub._usb_mic_task())
    await asyncio.sleep(0.05)
    assert not task.done(), (
        "the task must keep retrying (sleeping on backoff), not return -- "
        "a returning coroutine reads as a fatal crash to _supervised"
    )
    assert hub.usb_mic.stream_calls >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_robot_mic_task_retries_instead_of_returning_when_stream_ends() -> None:
    hub = make_hub()
    hub.mic = FakeMicThatDies()

    task = asyncio.create_task(hub._robot_mic_task())
    await asyncio.sleep(0.05)
    assert not task.done()
    assert hub.mic.stream_calls >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class FakeHwMicStream:
    """Yields a fixed sequence of chunks once, then hangs -- lets a test
    drive _robot_mic_task deterministically without a real socket/arecord."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def stream(self):  # noqa: ANN201 — matches RobotMic/UsbMic's AsyncIterator[bytes]
        for chunk in self._chunks:
            yield chunk
        await asyncio.sleep(1000)


class FakeWakeDetector:
    """feed() returns True on exactly the Nth call (1-indexed) -- lets a
    test pick precisely which chunk "says" the wake word. Also tracks every
    chunk fed so far (concatenated) as `recent_audio`, mirroring the real
    WakeWordDetector's sliding buffer closely enough for tests to verify
    that the BUFFERED LEAD-UP (not just the single triggering chunk) is
    what gets forwarded on a wake hit -- see app/wake_word.py's
    `recent_audio` docstring for why that matters."""

    def __init__(self, trigger_on_call: int) -> None:
        self.trigger_on_call = trigger_on_call
        self.calls = 0
        self.recent_audio = b""

    async def feed(self, pcm: bytes) -> bool:
        self.calls += 1
        self.recent_audio += pcm
        return self.calls == self.trigger_on_call


async def test_hw_mic_forwards_only_after_wake_word() -> None:
    """Regression for item 5 of the 2026-09-04 fixes list: the hardware mic
    must NOT forward ambient audio to Gemini before the wake word is heard,
    and MUST start forwarding once it is.

    2026-09-06 audit finding (prompted by a direct question: "won't the
    robot miss the beginning of the sentence?"): forwarding used to start
    only from the SINGLE chunk that happened to trigger detection onward --
    everything the local Whisper check needed to already hear in order to
    detect the phrase at all (i.e. the wake word itself, spoken during the
    "before" chunk here) was silently dropped, never reaching Gemini. The
    detector's buffered lead-up must be replayed as ONE combined chunk on
    the exact call that triggers, not skipped.
    """
    hub = make_hub()
    hub.mic = FakeHwMicStream([b"before", b"wake", b"after"])
    hub._wake_detectors = {"robot": FakeWakeDetector(trigger_on_call=2)}  # "wake" chunk triggers
    hub._preferred_mic_input = "robot"

    sent: list[bytes] = []

    class FakeAdapterForAudio:
        async def send_audio(self, pcm: bytes) -> None:
            sent.append(pcm)

    hub.adapter = FakeAdapterForAudio()

    task = asyncio.create_task(hub._robot_mic_task())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sent == [b"beforewake", b"after"], (
        "must replay the buffered lead-up (everything the local check heard "
        "to detect the phrase, not just the single triggering chunk), then "
        "keep forwarding normally afterward"
    )
    assert robot_mic_state.active_source == "robot"


async def test_hw_mic_wake_word_ignored_while_robot_is_speaking() -> None:
    """No AEC on this hardware (see HalfDuplexGate) -- feeding the robot's
    own voice through the wake-word detector risks a self-triggered wake."""
    hub = make_hub()
    hub.mic = FakeHwMicStream([b"chunk1", b"chunk2"])
    detector = FakeWakeDetector(trigger_on_call=1)
    hub._wake_detectors = {"robot": detector}

    class AlwaysMutedGate:
        mic_muted = True

    hub.gate = AlwaysMutedGate()

    sent: list[bytes] = []

    class FakeAdapterForAudio:
        async def send_audio(self, pcm: bytes) -> None:
            sent.append(pcm)

    hub.adapter = FakeAdapterForAudio()

    task = asyncio.create_task(hub._robot_mic_task())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sent == []
    assert detector.calls == 0, "must not even run wake-word inference while muted"


async def test_only_the_preferred_mic_ever_runs_wake_word_detection() -> None:
    """2026-09-06 ask, explicit: "разделения и параллельного слушания не
    должно быть... работает или/или". With BOTH a built-in and a USB mic
    configured, only the PREFERRED one (_preferred_mic_input) may ever run
    wake-word inference at all -- this is now a deterministic gate, not a
    race the other source could still "lose" after also paying the cost of
    a faster-whisper pass (the older, weaker guarantee)."""
    hub = make_hub()
    hub.mic = FakeHwMicStream([b"robot1", b"robot2", b"robot3"])
    hub.usb_mic = FakeHwMicStream([b"usb1", b"usb2", b"usb3"])
    robot_detector = FakeWakeDetector(trigger_on_call=1)
    usb_detector = FakeWakeDetector(trigger_on_call=1)
    hub._wake_detectors = {"robot": robot_detector, "usb": usb_detector}
    hub._preferred_mic_input = "robot"

    sent_robot: list[bytes] = []
    sent_usb: list[bytes] = []

    class FakeAdapterForAudio:
        async def send_audio(self, pcm: bytes) -> None:
            (sent_robot if pcm.startswith(b"robot") else sent_usb).append(pcm)

    hub.adapter = FakeAdapterForAudio()

    tasks = [
        asyncio.create_task(hub._robot_mic_task()),
        asyncio.create_task(hub._usb_mic_task()),
    ]
    await asyncio.sleep(0.05)
    for t in tasks:
        t.cancel()
    for t in tasks:
        with pytest.raises(asyncio.CancelledError):
            await t

    assert sent_robot and not sent_usb, (
        f"only the preferred (robot) source should ever forward -- robot={sent_robot} usb={sent_usb}"
    )
    assert usb_detector.calls == 0, "the non-preferred mic must never even run wake-word inference"
    assert robot_mic_state.active_source in ("robot", "usb")


class FakeMicThatRaises:
    """A hardware mic whose `stream()` RAISES instead of returning -- e.g.
    UsbMicNotAvailableError when `arecord` itself isn't on PATH (a typo'd
    USB_MIC_ALSA_DEVICE, alsa-utils not installed). Distinct from
    FakeMicThatDies above: a raised exception is NOT the same code path as a
    generator returning, and _drain_hw_mic must swallow this one too --
    left uncaught, it propagates to _supervised's fatal os._exit path just
    like an unhandled return would, crash-looping the whole voice service
    forever on a config typo."""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self):  # noqa: ANN201 — matches RobotMic/UsbMic's AsyncIterator[bytes]
        self.stream_calls += 1
        msg = "simulated: arecord not found"
        raise RuntimeError(msg)
        yield  # pragma: no cover — makes this an async generator function


async def test_usb_mic_task_retries_instead_of_dying_when_stream_raises() -> None:
    hub = make_hub()
    hub.usb_mic = FakeMicThatRaises()

    task = asyncio.create_task(hub._usb_mic_task())
    await asyncio.sleep(0.05)
    assert not task.done(), (
        "a raised exception must be retried like a graceful return, not left "
        "to propagate into _supervised's fatal os._exit path"
    )
    assert hub.usb_mic.stream_calls >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_wake_window_never_expires_during_visit_intake() -> None:
    """UX audit finding, 2026-09-06: a visitor mid-registration who pauses
    longer than wake_active_window_s (recalling an ID number, digging for a
    pass) used to have the hardware mic silently fall back to wake-word-only
    listening -- their eventual answer was dropped and they had to say the
    wake phrase again in the middle of a formal interview. While
    visit_intake is engaged, a source that already owns the window must
    keep forwarding no matter how long the timer says it's been."""
    hub = SessionHub(Settings(robot_profile="g1"))
    hub._wake_detectors = {"robot": FakeWakeDetector(trigger_on_call=1)}
    robot_mic_state.active_source = "robot"
    hub._wake_active_until = time.monotonic() - 100.0  # long expired

    intake_tool = hub.tools.get("visit_intake")
    assert intake_tool is not None
    intake_tool._intake.start()  # engage the interview directly

    owns = await hub._hw_mic_owns_wake_window("robot", b"pcm")
    assert owns is True
    assert robot_mic_state.active_source == "robot", (
        "must not fall back to browser mid-interview even though the timer expired"
    )


async def test_wake_window_still_expires_normally_outside_visit_intake() -> None:
    """The intake exception above must not turn into a blanket "never
    expire" -- ordinary conversation (no interview running) keeps the
    normal wake_active_window_s timeout."""
    hub = SessionHub(Settings(robot_profile="g1"))
    hub._wake_detectors = {"robot": FakeWakeDetector(trigger_on_call=1)}
    robot_mic_state.active_source = "robot"
    hub._wake_active_until = time.monotonic() - 100.0  # long expired

    owns = await hub._hw_mic_owns_wake_window("robot", b"pcm")
    assert owns is False
    assert robot_mic_state.active_source == "browser", "must go back to sleep as before"
