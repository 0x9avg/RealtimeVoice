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
  onto a NATS bus this project doesn't have. app/main.py wires that
  callback to adapter.send_text(...) — see PRESENCE_MARKER's
  handling in app/scenarios.py for what happens on the model side.
- has only this one cooldown layer (rearm_s). The original has a *second*,
  independent cooldown in its bus consumer (apps/rag's greeting_cooldown_s)
  — that only exists because detector and greeting are separate services
  there; redundant in a single process.

Process isolation (2026-09-06 UX ask): "Давай разберемся с детекцией
человека перед роботом... Это должен быть отдельный процесс от генерации
речи и слушания, чтобы не возникало задержек... и он не тормозил основной
контур." `PresenceRunner` no longer talks to a sync `MotionDetector`
directly — it drives anything shaped like `MotionScorer` (an async
`score()`/`aclose()` pair). Two implementations:

- `LocalMotionDetector` — the original behaviour, `MotionDetector.score()`
  off-loaded to a worker thread via `asyncio.to_thread`. Still real
  isolation from the event loop (a slow score() can't stall WS/audio
  pumping), but it's a THREAD: same process, same memory space, same GIL,
  same fate if something in here segfaults or wedges hard.
- `SubprocessMotionDetector` — runs the exact same `MotionDetector` inside
  a standalone OS process (app/presence_worker.py), talking to it over a
  length-prefixed stdin/stdout protocol. A genuinely separate process: a
  crash, a hang, or (if this scorer ever grows into a real CPU-hungry ML
  model) a sustained CPU hog there cannot touch the process that's running
  the voice pipeline. On any I/O failure `score()` restarts the worker and
  returns 0.0 for that one frame (never raises) — `app/session_hub.py`'s
  `_presence_task` never sees a subprocess hiccup at all.

`app/config.py`'s `PRESENCE_ISOLATION` ("process", default, or "thread")
picks which one `build_motion_detector()` returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import struct
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

import numpy as np
from PIL import Image

_LOG = logging.getLogger("presence")

# Downscale before diffing: kills JPEG noise and makes the diff O(small).
_ANALYSIS_SIZE = (160, 120)
# A pixel counts as "changed" when its gray level moved by more than this
# (0..255); below is sensor/JPEG noise.
_PIXEL_DELTA = 25

# Wire format shared with app/presence_worker.py: a 4-byte big-endian
# length prefix ahead of each JPEG frame, a 4-byte big-endian float32 score
# in reply. Lockstep request/response — SubprocessMotionDetector never has
# more than one frame in flight, so no sequence numbers are needed.
_LENGTH_HEADER = struct.Struct(">I")
_SCORE_REPLY = struct.Struct(">f")


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


class MotionScorer(Protocol):
    """What `PresenceRunner` needs from a scorer — either a thread- or a
    subprocess-backed one, it doesn't care which."""

    async def score(self, jpeg: bytes) -> float: ...

    async def aclose(self) -> None: ...


class LocalMotionDetector:
    """`MotionDetector`, off-loaded to a thread — the pre-isolation
    behaviour, kept as the "thread" `PRESENCE_ISOLATION` option."""

    def __init__(self) -> None:
        self._detector = MotionDetector()

    async def score(self, jpeg: bytes) -> float:
        # Off-loop: score() decodes a 1280x720 JPEG and diffs it with numpy
        # — tens of milliseconds of pure CPU on the robot, every frame. Run
        # inline it stalls the event loop, and the loop is what paces the
        # robot's own speech out in 50ms slots, so the stutter is audible.
        return await asyncio.to_thread(self._detector.score, jpeg)

    async def aclose(self) -> None:
        pass


class SubprocessMotionDetector:
    """`MotionDetector`, run inside `python -m app.presence_worker` — see
    the module docstring for why. Lazily spawns the worker on first
    `score()` call; transparently respawns it if it ever dies (crash, OOM
    kill, whatever) so callers never see that as an error, just one frame
    scored as 0.0 (no motion — the safe default, since it can only delay a
    greeting by one tick, never cause a spurious one).
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        # One frame in flight at a time (PresenceRunner.run() awaits step()
        # sequentially) — this lock isn't about serializing concurrent
        # callers, it's about never letting a respawn race with an
        # in-flight score() from a second caller of the same instance.
        self._lock = asyncio.Lock()
        # Generous margin above MotionDetector.score()'s "tens of
        # milliseconds" baseline (see LocalMotionDetector.score's comment)
        # — see `score()` below for why this exists.
        self._score_timeout_s = 3.0
        self._proc_started_at: float = 0.0
        # 2026-09-06 audit finding: without this, a worker that fails to
        # even start cleanly (broken venv, an import error inside
        # app.presence_worker) died almost immediately after every spawn,
        # and `_ensure_proc` respawned a full OS process on every single
        # `score()` call as a result — up to `presence_fps` times a
        # second, forever, with no cooldown. Only applies when the
        # previous process died within ~1s of starting (a crash LOOP);
        # an occasional crash after running fine for a while still
        # respawns immediately, unaffected, matching the class's existing
        # "transparently respawns" behaviour.
        self._respawn_backoff_s: float = 0.0

    async def _ensure_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            if self._proc is not None and time.monotonic() - self._proc_started_at < 1.0:
                self._respawn_backoff_s = min(max(self._respawn_backoff_s, 0.5) * 2, 30.0)
                _LOG.warning(
                    "presence_worker.crash_loop backing_off_s=%.1f", self._respawn_backoff_s
                )
                await asyncio.sleep(self._respawn_backoff_s)
            else:
                self._respawn_backoff_s = 0.0
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.presence_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Inherit stderr rather than pipe it: a Python traceback
                # from the worker lands in the same journal/log as
                # everything else, which is all we need — nobody reads it
                # programmatically.
            )
            self._proc_started_at = time.monotonic()
            _LOG.info("presence_worker.started pid=%s", self._proc.pid)
        return self._proc

    async def score(self, jpeg: bytes) -> float:
        async with self._lock:
            proc = await self._ensure_proc()
            assert proc.stdin is not None  # noqa: S101 — PIPE was requested above
            assert proc.stdout is not None  # noqa: S101 — PIPE was requested above
            try:
                proc.stdin.write(_LENGTH_HEADER.pack(len(jpeg)))
                proc.stdin.write(jpeg)
                await proc.stdin.drain()
                # 2026-09-06 audit finding: a HUNG worker (wedged decode, CPU
                # starvation) never raised any of the exceptions below --
                # readexactly() just waited forever, holding self._lock
                # forever, so every later score() call blocked too and
                # presence detection stopped completely with no log, no
                # exception, no recovery. A crashed worker was already
                # handled; a merely-stuck one wasn't. Timing out and
                # treating that the same as a crash (kill + respawn + a
                # safe 0.0 for this one frame) closes that gap.
                header = await asyncio.wait_for(
                    proc.stdout.readexactly(_SCORE_REPLY.size), timeout=self._score_timeout_s
                )
            except (
                TimeoutError,
                BrokenPipeError,
                ConnectionResetError,
                asyncio.IncompleteReadError,
                OSError,
            ) as exc:
                _LOG.warning("presence_worker.died, respawning: %r", exc)
                await self._kill(proc)
                self._proc = None
                return 0.0
            (score,) = _SCORE_REPLY.unpack(header)
            return score

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def aclose(self) -> None:
        if self._proc is not None:
            await self._kill(self._proc)
        self._proc = None


def build_motion_detector(isolation: str) -> MotionScorer:
    """`PRESENCE_ISOLATION` ("process", default, or "thread") -> scorer."""
    if isolation == "thread":
        return LocalMotionDetector()
    return SubprocessMotionDetector()


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
        detector: MotionScorer | None = None,
        threshold: float = 0.02,
        trigger_frames: int = 2,
        clear_frames: int = 10,
        rearm_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frames = frames
        self._on_visitor_detected = on_visitor_detected
        self._detector = detector or LocalMotionDetector()
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
        # `self._detector.score` is always async now — either thread- or
        # subprocess-backed (see MotionScorer/LocalMotionDetector/
        # SubprocessMotionDetector above); either way it never blocks the
        # event loop, which is what paces the robot's own speech out in
        # 50ms slots, so a stall here would be an audible stutter there.
        score = await self._detector.score(frame)
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
