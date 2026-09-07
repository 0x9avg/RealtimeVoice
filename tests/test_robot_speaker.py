"""RobotSpeaker's pacing (app/robot_audio.py) — item 10 of the 2026-09-04
fixes list ("робот иногда запинается при воспроизведении голоса").

No real vendor SDK/DDS here: the fake client is pre-assigned to
`RobotSpeaker._client` directly (bypassing `_ensure_client`, which
short-circuits once `_client` is already set), so these tests run without
unitree_sdk2py installed.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import robot_audio
from app.robot_audio import RobotSpeaker


class FakeAudioClient:
    def __init__(self) -> None:
        self.play_calls = 0

    def PlayStream(self, app_name: str, stream_id: str, chunk: bytes) -> tuple[int, str]:
        self.play_calls += 1
        return (0, "")

    def PlayStop(self, app_name: str) -> tuple[int, str]:
        return (0, "")


def make_speaker() -> RobotSpeaker:
    speaker = RobotSpeaker(network_interface="eth0")
    speaker._client = FakeAudioClient()
    return speaker


async def test_priming_burst_fires_once_per_utterance_not_every_play_call() -> None:
    """Regression: the "prime the vendor buffer" backdating used to fire on
    EVERY play() call whenever the pacer had merely caught up to real time
    between two chunks of the SAME utterance (the normal case, since Gemini
    delivers chunks roughly as fast as they play) -- audible as a stutter,
    not just a one-time buffer prime at the true start of an utterance."""
    speaker = make_speaker()
    pcm = b"\x00\x00" * 800  # 50ms of 16kHz mono silence, already at target rate

    assert speaker._is_first_chunk_of_utterance is True

    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False, "first play() must prime and clear the flag"

    # A second AudioChunk of the SAME utterance, arriving right after the
    # first (as it normally would) -- must NOT re-arm priming.
    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False, (
        "a second play() within the same utterance must not re-trigger the priming burst"
    )


async def test_stop_rearms_priming_for_the_next_utterance() -> None:
    speaker = make_speaker()
    pcm = b"\x00\x00" * 800

    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False

    await speaker.stop()
    assert speaker._is_first_chunk_of_utterance is True, (
        "stop() (called after every turn, not just barge-in) must rearm priming for the NEXT utterance"
    )


# ---------------------------------------------------------------------------
# 2026-09-07: "между словами робот по-прежнему может делать запинки".
#
# Root cause measured on both real robots: one `PlayStream` RPC costs ~3 ms
# at the median but 60-85 ms at p99 (~2.5% of calls) -- longer than the
# 50 ms slot it occupies. The pacer's schedule is ABSOLUTE precisely so that
# such a spike is caught up by sending the following chunks back-to-back;
# the lead (_LEAD_S) is the cushion that keeps the vendor playing while that
# happens. The bug was a branch in play() that snapped the schedule to `now`
# whenever it was behind at the start of ANY AudioChunk -- discarding that
# cushion permanently for the rest of the turn.
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable stand-in for the `time` module inside app.robot_audio."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


class SlowAudioClient(FakeAudioClient):
    """PlayStream that burns a configurable amount of the (fake) clock.

    `spikes` names the call indexes that cost `spike_s` instead of the
    usual `cost_s` — the shape actually measured on both robots
    (2026-09-07): ~3 ms almost always, 60-85 ms on roughly one call in 40.
    A client that is slow on EVERY call is a different failure entirely
    (the sender genuinely cannot keep up with real time and no cushion can
    save it); the fix under test here is about rare spikes.
    """

    def __init__(
        self,
        clock: FakeClock,
        cost_s: float = 0.0,
        *,
        spikes: set[int] | None = None,
        spike_s: float = 0.085,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.cost_s = cost_s
        self.spikes = spikes or set()
        self.spike_s = spike_s

    def PlayStream(self, app_name: str, stream_id: str, chunk: bytes) -> tuple[int, str]:
        self.clock.now += self.spike_s if self.play_calls in self.spikes else self.cost_s
        return super().PlayStream(app_name, stream_id, chunk)


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch):
    """RobotSpeaker on a fake clock, with sleeps advancing that clock
    instead of really waiting."""
    clock = FakeClock()
    monkeypatch.setattr(robot_audio, "time", clock)

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    speaker = RobotSpeaker(network_interface="eth0")
    speaker._client = FakeAudioClient()
    return speaker, clock, slept


async def test_first_chunk_of_an_utterance_is_sent_with_the_full_lead(paced) -> None:
    speaker, clock, slept = paced
    start = clock.now

    await speaker.play(b"\x00\x00" * 800, sample_rate_hz=16000)  # one 50ms chunk

    assert slept == [], "the priming chunk must go out immediately, not wait for a slot"
    # Schedule anchored one lead in the past, then advanced by one slot.
    assert speaker._next_send_monotonic == pytest.approx(start - speaker.lead_s + 0.05)


async def test_a_slow_rpc_spike_is_absorbed_by_the_lead(paced) -> None:
    """An 85 ms PlayStream among 3 ms ones must be absorbed by the cushion
    and caught up — not turned into a permanent loss of margin."""
    speaker, clock, _ = paced
    # Spike after the opening burst has filled the buffer, which is
    # where a mid-sentence hitch actually comes from.
    speaker._client = SlowAudioClient(clock, cost_s=0.003, spikes={7})
    start = clock.now
    chunks = 12

    await speaker.play(b"\x00\x00" * 800 * chunks, sample_rate_hz=16000)

    # The schedule stays anchored to where the utterance began, one slot per
    # chunk, regardless of how long any individual RPC took.
    assert speaker._next_send_monotonic == pytest.approx(start - speaker.lead_s + 0.05 * chunks)
    # The buffer dipped by the overrun (85 - 50 = 35 ms) and no further:
    # the chunks after the spike went out back-to-back until the schedule
    # was caught up again.
    worst_buffer_ms = speaker._min_buffered_s * 1000
    assert 150 < worst_buffer_ms < 200, "one spike must cost ~35 ms of a 200 ms buffer, no more"


async def test_a_new_audio_chunk_does_not_reanchor_the_schedule(paced) -> None:
    """THE regression. Gemini delivers one utterance as several AudioChunk
    events; each is its own play() call. Arriving while the pacer is a few
    ms behind (normal, after any slow RPC) used to re-anchor playback to
    real time with zero cushion for the rest of the turn."""
    speaker, clock, _ = paced
    speaker._client = SlowAudioClient(clock, cost_s=0.085)

    await speaker.play(b"\x00\x00" * 800 * 4, sample_rate_hz=16000)
    schedule_after_first_call = speaker._next_send_monotonic
    assert schedule_after_first_call < clock.now, "precondition: the pacer is behind wall clock"

    speaker._client = FakeAudioClient()  # the spikes stop
    await speaker.play(b"\x00\x00" * 800, sample_rate_hz=16000)

    assert speaker._next_send_monotonic == pytest.approx(schedule_after_first_call + 0.05), (
        "the second AudioChunk must continue the existing schedule, not restart it at `now`"
    )


async def test_a_real_stall_reprimes_the_buffer(paced) -> None:
    """The opposite case: after a genuine gap (nothing to play for half a
    second or more) the vendor buffer really is empty, so the lead must be
    restored rather than resuming with no margin at all."""
    speaker, clock, _ = paced

    await speaker.play(b"\x00\x00" * 800, sample_rate_hz=16000)
    clock.now += 2.0  # a long stall: a tool call, a slow model, a reconnect

    await speaker.play(b"\x00\x00" * 800, sample_rate_hz=16000)

    assert speaker._next_send_monotonic == pytest.approx(clock.now - speaker.lead_s + 0.05)


async def test_an_exhausted_cushion_is_reported_at_the_end_of_the_turn(
    paced, caplog: pytest.LogCaptureFixture
) -> None:
    """Stutter is inaudible in a log unless the pacer says so — this is what
    makes "робот запинается" checkable against journalctl."""
    speaker, clock, _ = paced
    # Two spikes far worse than p99, after priming — enough to drain 200 ms.
    speaker._client = SlowAudioClient(clock, cost_s=0.003, spikes={6, 7}, spike_s=0.3)

    await speaker.play(b"\x00\x00" * 800 * 12, sample_rate_hz=16000)
    with caplog.at_level(logging.WARNING):
        await speaker.stop()

    warnings = [r.getMessage() for r in caplog.records if "turn_pacing" in r.getMessage()]
    assert warnings, "an exhausted cushion must be logged as a warning"
    assert "worst_buffer_ms=-" in warnings[0], "an empty vendor buffer is the audible stutter"
