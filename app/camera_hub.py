"""Single camera poller shared by presence detection and the web viewer.

Mirrors ../../humanoid-robot/packages/humanoid-robot-adapters-unitree-g1/
src/humanoid_robot/adapters/unitree_g1/camera_mjpeg.py's `FrameHub`: one
`RobotCamera` polling loop feeds every consumer, so N browser tabs plus the
presence detector cost the DDS bus exactly one stream, not N+1. Simpler than
that reference: `RobotCamera.frames()` already offloads the blocking DDS
call via `asyncio.to_thread`, so this hub is a plain asyncio task — no
background thread/condvar needed, an `asyncio.Condition` is enough.

Started lazily (first call to `snapshot()`/`stream()`, from either
app/api_routes.py's camera endpoints or app/main.py's presence_task) and
just keeps running for the process lifetime — matches this project's
existing "load once, don't bother tearing down" precedent (app/knowledge.py's
embeddings cache).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.robot_audio import UnitreeSdkNotAvailableError
from app.robot_camera import RobotCamera

_LOG = logging.getLogger("camera_hub")


@dataclass(slots=True)
class CameraHub:
    camera: RobotCamera
    fps: float
    _frame: bytes | None = field(default=None, init=False)
    _seq: int = field(default=0, init=False)
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _unavailable: bool = field(default=False, init=False)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="camera-hub")

    async def _run(self) -> None:
        try:
            async for frame in self.camera.frames(fps=self.fps):
                async with self._condition:
                    self._frame = frame
                    self._seq += 1
                    self._condition.notify_all()
        except UnitreeSdkNotAvailableError:
            # Off-robot dev, or the SDK genuinely isn't installed — log once
            # and stay quiet rather than let the task die silently; snapshot/
            # stream callers already time out gracefully with no frame.
            _LOG.warning("camera.unavailable: unitree_sdk2py not importable")
            async with self._condition:
                self._unavailable = True
                self._condition.notify_all()

    async def snapshot(self, timeout: float = 5.0) -> bytes | None:
        """The current frame, waiting up to `timeout` for the first one."""
        self.start()
        async with self._condition:
            if self._frame is None and not self._unavailable:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._condition.wait(), timeout)
            return self._frame

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield each new frame as it arrives — for an MJPEG multipart response."""
        self.start()
        last_seq = 0
        while True:
            async with self._condition:
                if self._seq <= last_seq and not self._unavailable:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._condition.wait(), 5.0)
                if self._unavailable and self._frame is None:
                    return
                last_seq = self._seq
                frame = self._frame
            if frame is not None:
                yield frame


async def sampled(hub: CameraHub, *, fps: float) -> AsyncIterator[bytes]:
    """Poll `hub` for its latest frame at `fps` — independent of the hub's
    own DDS poll rate (`presence_camera_fps`), for a consumer like
    app/presence.py's detector that doesn't need every frame the hub
    captures. Two knobs, one DDS poller: the hub decides how often it talks
    to the vendor SDK, this decides how often a given consumer looks."""
    period = 1.0 / fps if fps > 0 else 0.0
    while True:
        frame = await hub.snapshot()
        if frame is not None:
            yield frame
        if period:
            await asyncio.sleep(period)


def _build_default() -> CameraHub:
    from app.config import settings

    camera = RobotCamera(network_interface=settings.presence_camera_network_interface)
    return CameraHub(camera=camera, fps=settings.presence_camera_fps)


# Module-level singleton, same pattern as app/knowledge.py's knowledge_base —
# one hub for the whole process, regardless of how many WS sessions or HTTP
# camera requests come in.
camera_hub = _build_default()
