"""app/presence.py — motion scoring + the presence hysteresis state machine.

No real camera/DDS here: frames are tiny synthetic JPEGs (a solid-color
image, and a second image with a block changed) built with PIL, and
PresenceRunner is driven off a plain async frame iterator instead of
app/camera_hub.py's CameraHub. SubprocessMotionDetector tests below DO spawn
a real `python -m app.presence_worker` child process — that's the actual
contract being tested (2026-09-06 UX ask: presence detection must be an
isolated OS process, not just an in-process thread), so it's worth the
~100ms of real subprocess startup cost rather than mocking it away.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from app.presence import (
    LocalMotionDetector,
    MotionDetector,
    PresenceRunner,
    SubprocessMotionDetector,
    build_motion_detector,
)


def _jpeg(fill: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (160, 120), color=fill)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


_QUIET_FRAME = _jpeg((60, 60, 60))
_CHANGED_FRAME = _jpeg((220, 60, 60))


class FakeScorer:
    """Feeds PresenceRunner a scripted sequence of scores instead of
    actually decoding frames — isolates the hysteresis state machine from
    MotionDetector's own behaviour, which gets its own tests below."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self._i = 0
        self.closed = False

    async def score(self, jpeg: bytes) -> float:  # noqa: ARG002 — content unused
        value = self._scores[min(self._i, len(self._scores) - 1)]
        self._i += 1
        return value

    async def aclose(self) -> None:
        self.closed = True


# -- MotionDetector (sync, in-process) --------------------------------------


def test_motion_detector_first_frame_is_never_a_score() -> None:
    detector = MotionDetector()
    assert detector.score(_QUIET_FRAME) == 0.0


def test_motion_detector_scores_a_changed_region_higher_than_a_static_one() -> None:
    detector = MotionDetector()
    detector.score(_QUIET_FRAME)
    static_score = detector.score(_QUIET_FRAME)
    detector.reset()
    detector.score(_QUIET_FRAME)
    changed_score = detector.score(_CHANGED_FRAME)
    assert changed_score > static_score


# -- LocalMotionDetector (thread-backed async wrapper) -----------------------


async def test_local_motion_detector_matches_sync_detector() -> None:
    local = LocalMotionDetector()
    assert await local.score(_QUIET_FRAME) == 0.0
    assert await local.score(_CHANGED_FRAME) > 0.0
    await local.aclose()  # no-op, must not raise


# -- SubprocessMotionDetector (real child process) ---------------------------


async def test_subprocess_motion_detector_scores_frames() -> None:
    detector = SubprocessMotionDetector()
    try:
        assert await detector.score(_QUIET_FRAME) == 0.0
        second = await detector.score(_CHANGED_FRAME)
        assert second > 0.0
    finally:
        await detector.aclose()


async def test_subprocess_motion_detector_respawns_after_the_worker_dies() -> None:
    """Regression target: a dead/killed worker must never surface as an
    exception to PresenceRunner — score() should quietly restart it and
    return 0.0 for the frame that was in flight when it died."""
    detector = SubprocessMotionDetector()
    try:
        await detector.score(_QUIET_FRAME)
        proc = detector._proc
        assert proc is not None
        proc.kill()
        await proc.wait()

        result = await detector.score(_QUIET_FRAME)
        assert result == 0.0

        # The next call must work again against a freshly spawned worker.
        again = await detector.score(_CHANGED_FRAME)
        assert isinstance(again, float)
        assert detector._proc is not None
        assert detector._proc.pid != proc.pid
    finally:
        await detector.aclose()


async def test_subprocess_motion_detector_treats_a_hung_worker_like_a_dead_one() -> None:
    """2026-09-06 audit finding: a worker that HANGS instead of crashing
    (wedged decode, CPU starvation) used to block score() -- and, via
    self._lock, every later score() call too -- forever: no exception, no
    log, no recovery. A timeout must catch this the same way a crash
    already is (kill, respawn, a safe 0.0 for the frame in flight)."""
    detector = SubprocessMotionDetector()
    try:
        await detector.score(_QUIET_FRAME)
        proc = detector._proc
        assert proc is not None
        detector._score_timeout_s = 0.05

        async def _never_replies(_n: int) -> bytes:
            await asyncio.sleep(10)
            return b""

        proc.stdout.readexactly = _never_replies  # type: ignore[method-assign]

        result = await asyncio.wait_for(detector.score(_QUIET_FRAME), timeout=2.0)
        assert result == 0.0
        assert detector._proc is not proc  # killed and respawned, not left hung

        again = await detector.score(_CHANGED_FRAME)
        assert isinstance(again, float)
    finally:
        await detector.aclose()


async def test_subprocess_motion_detector_aclose_terminates_the_worker() -> None:
    detector = SubprocessMotionDetector()
    await detector.score(_QUIET_FRAME)
    proc = detector._proc
    assert proc is not None
    await detector.aclose()
    assert proc.returncode is not None


def test_build_motion_detector_dispatches_on_isolation_setting() -> None:
    assert isinstance(build_motion_detector("thread"), LocalMotionDetector)
    assert isinstance(build_motion_detector("process"), SubprocessMotionDetector)
    # Unknown values fall back to the safer (fully isolated) default.
    assert isinstance(build_motion_detector("something-else"), SubprocessMotionDetector)


# -- PresenceRunner hysteresis ------------------------------------------------


async def _frames(n: int) -> "asyncio.AsyncIterator[bytes]":
    for _ in range(n):
        yield b"\x00"


async def test_runner_fires_once_after_trigger_frames_in_a_row() -> None:
    scorer = FakeScorer([1.0, 1.0, 1.0])
    fired = 0

    async def on_visitor_detected() -> None:
        nonlocal fired
        fired += 1

    runner = PresenceRunner(
        frames=_frames(3),
        on_visitor_detected=on_visitor_detected,
        detector=scorer,
        threshold=0.5,
        trigger_frames=2,
        clear_frames=10,
        rearm_s=60.0,
    )
    await runner.run()
    assert fired == 1
    assert runner.present is True


async def test_runner_does_not_fire_below_threshold() -> None:
    scorer = FakeScorer([0.0, 0.0, 0.0])
    fired = 0

    async def on_visitor_detected() -> None:
        nonlocal fired
        fired += 1

    runner = PresenceRunner(
        frames=_frames(3),
        on_visitor_detected=on_visitor_detected,
        detector=scorer,
        threshold=0.5,
        trigger_frames=2,
    )
    await runner.run()
    assert fired == 0
    assert runner.present is False


async def test_runner_rearms_only_after_clear_frames_and_rearm_s_elapsed() -> None:
    # Trigger, then go quiet long enough to clear, but rearm_s hasn't
    # elapsed yet (fake clock stands still) — must stay "present" and not
    # re-fire even though the quiet streak alone would otherwise clear it.
    scorer = FakeScorer([1.0, 1.0, 0.0, 0.0, 0.0])
    fired = 0

    async def on_visitor_detected() -> None:
        nonlocal fired
        fired += 1

    runner = PresenceRunner(
        frames=_frames(5),
        on_visitor_detected=on_visitor_detected,
        detector=scorer,
        threshold=0.5,
        trigger_frames=2,
        clear_frames=3,
        rearm_s=60.0,
        clock=lambda: 0.0,  # frozen — rearm_s can never elapse
    )
    await runner.run()
    assert fired == 1
    assert runner.present is True  # cleared streak-wise, but not rearmed
