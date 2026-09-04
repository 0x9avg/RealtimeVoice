"""Presence detection: motion-based visitor detector + a hysteresis state
machine that fires once per visit.

Ported from ../../humanoid-robot/apps/presence — `MotionDetector`
(detector.py) is carried over verbatim: grayscale frame differencing is a
"robust, dependency-light" someone-is-here signal for a static scene, no ML
model. `PresenceRunner` (runner.py) keeps the same quiet/present hysteresis
(trigger_frames/clear_frames/rearm_s), but:

- iterates an async frame generator (app/robot_camera.py's RobotCamera.frames,
  which already paces itself to `fps`) instead of polling a zero-arg
  FrameSource on a separate interval_s timer — one pacing knob, not two;
- calls a plain async callback instead of publishing a VisitorDetected event
  onto a NATS bus this project doesn't have. app/main.py/robot_main.py
  wire that callback to adapter.send_text(...) — see PRESENCE_MARKER's
  handling in app/scenarios.py for what happens on the model side.
- has only this one cooldown layer (rearm_s). The original has a *second*,
  independent cooldown in its bus consumer (apps/rag's greeting_cooldown_s)
  — that only exists because detector and greeting are separate services
  there; redundant in a single process.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import numpy as np
from PIL import Image

# Downscale before diffing: kills JPEG noise and makes the diff O(small).
_ANALYSIS_SIZE = (160, 120)
# A pixel counts as "changed" when its gray level moved by more than this
# (0..255); below is sensor/JPEG noise.
_PIXEL_DELTA = 25


class MotionDetector:
    """Stateful frame-differencing scorer; feed frames, read scores."""

    def __init__(self) -> None:
        self._previous: np.ndarray | None = None

    def score(self, jpeg: bytes) -> float:
        """0..1 — fraction of the frame that moved since the last call."""
        with Image.open(io.BytesIO(jpeg)) as img:
            gray = np.asarray(
                img.convert("L").resize(_ANALYSIS_SIZE, Image.BILINEAR),
                dtype=np.int16,
            )
        previous, self._previous = self._previous, gray
        if previous is None:
            return 0.0
        changed = np.abs(gray - previous) > _PIXEL_DELTA
        return float(changed.mean())

    def reset(self) -> None:
        self._previous = None


class PresenceRunner:
    """Consumes a frame stream, fires `on_visitor_detected` once per visit.

    State machine with hysteresis:

        quiet --(score >= threshold for `trigger_frames` in a row)--> present
          ^                                                             |
          +--(score < threshold for `clear_frames` in a row,            |
              and at least `rearm_s` since the trigger)-----------------+
    """

    def __init__(
        self,
        *,
        frames: AsyncIterator[bytes],
        on_visitor_detected: Callable[[], Awaitable[None]],
        detector: MotionDetector | None = None,
        threshold: float = 0.02,
        trigger_frames: int = 2,
        clear_frames: int = 10,
        rearm_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frames = frames
        self._on_visitor_detected = on_visitor_detected
        self._detector = detector or MotionDetector()
        self._threshold = threshold
        self._trigger_frames = trigger_frames
        self._clear_frames = clear_frames
        self._rearm_s = rearm_s
        self._clock = clock
        # State.
        self.present = False
        self._active_streak = 0
        self._quiet_streak = 0
        self._triggered_at = 0.0

    async def run(self) -> None:
        async for frame in self._frames:
            await self.step(frame)

    async def step(self, frame: bytes) -> None:
        """One score+transition cycle (extracted for tests)."""
        # Off-loop: score() decodes a 1280x720 JPEG and diffs it with numpy
        # — tens of milliseconds of pure CPU on the robot, every frame. Run
        # inline it stalls the event loop, and the loop is what paces the
        # robot's own speech out in 50ms slots, so the stutter is audible.
        score = await asyncio.to_thread(self._detector.score, frame)
        if score >= self._threshold:
            self._active_streak += 1
            self._quiet_streak = 0
        else:
            self._quiet_streak += 1
            self._active_streak = 0
        if not self.present and self._active_streak >= self._trigger_frames:
            self.present = True
            self._triggered_at = self._clock()
            await self._on_visitor_detected()
        elif (
            self.present
            and self._quiet_streak >= self._clear_frames
            and self._clock() - self._triggered_at >= self._rearm_s
        ):
            self.present = False
