"""G1's front camera — accessed the same way as the speaker/mic in
app/robot_audio.py, and for the same reason: the hardware isn't reachable
as a normal OS device.

The G1's camera is wired to a *different* onboard computer than the Jetson
this process runs on — there is no `/dev/video*`/USB camera to open here
(confirmed on this exact robot: no `/dev/video*`, nothing camera-like in
`lsusb`, no `uvcvideo` kernel module). The vendor exposes it as JPEG frames
over DDS through the `VideoClient` request/response service — the same one
the official Unitree app uses, per
`../../humanoid-robot/packages/humanoid-robot-adapters-unitree-g1/src/
humanoid_robot/adapters/unitree_g1/camera_mjpeg.py`, which this module
mirrors the client-setup half of. Note the class lives under
`unitree_sdk2py.go2.video`, not a `g1`-specific module — same
cross-namespace situation as `g1_audio_client`/`a2/audio` in
app/robot_audio.py; Unitree just didn't ship a separate G1 video module.

Deliberately simpler than camera_mjpeg.py: that module fans one capture
loop out to N HTTP viewers (a background thread + condvar + its own
stdlib HTTP server) because it serves the "robot's eyes" to any number of
browser tabs. This project has exactly one consumer (app/presence.py's
detector) — a plain async generator polling the DDS client is enough, no
threads/condvars/HTTP server needed.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.dds import ensure_channel_factory
from app.robot_audio import UnitreeSdkNotAvailableError

_LOG = logging.getLogger("robot_camera")


def _require_sdk() -> Any:
    try:
        return importlib.import_module("unitree_sdk2py.go2.video.video_client")
    except ImportError as exc:
        raise UnitreeSdkNotAvailableError from exc


@dataclass(slots=True)
class RobotCamera:
    """Polls the G1 front camera and yields JPEG frames."""

    network_interface: str
    _client: Any = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        video_client_mod = _require_sdk()
        ensure_channel_factory(self.network_interface)
        client = video_client_mod.VideoClient()
        client.SetTimeout(3.0)
        client.Init()
        self._client = client
        return client

    def _grab_frame(self) -> bytes | None:
        """Blocking DDS round-trip — always call via asyncio.to_thread."""
        client = self._ensure_client()
        code, data = client.GetImageSample()
        if code != 0 or not data:
            _LOG.warning("GetImageSample returned code=%s", code)
            return None
        return bytes(data)

    async def frames(self, *, fps: float) -> AsyncIterator[bytes]:
        """Yield JPEG frames at roughly `fps`, skipping failed polls."""
        period = 1.0 / fps if fps > 0 else 0.0
        while True:
            loop_start = asyncio.get_running_loop().time()
            frame = await asyncio.to_thread(self._grab_frame)
            if frame is not None:
                yield frame
            if period:
                elapsed = asyncio.get_running_loop().time() - loop_start
                await asyncio.sleep(max(0.0, period - elapsed))
