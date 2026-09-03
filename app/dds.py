"""Process-wide guard for `unitree_sdk2py`'s `ChannelFactoryInitialize`.

Per humanoid-robot's own comment on this exact call: "DDS ChannelFactory is
a process-global side effect." `app/robot_audio.py`'s `RobotSpeaker` used to
guard it with a plain instance attribute, which was fine while it was the
only DDS RPC client in the process. `app/robot_camera.py`'s `RobotCamera` is
a second, independent one — two instance-level guards give no guarantee
against calling this twice in one process, so the guard moves here, keyed
on the process instead of on whichever class happens to ask first.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_initialized = False


def ensure_channel_factory(interface: str) -> None:
    """Idempotent, thread-safe `ChannelFactoryInitialize(0, interface)`.

    Safe to call from multiple DDS-client classes — only the first call in
    the process actually does anything. Blocking (DDS discovery) — callers
    already run this off-loop via `asyncio.to_thread` for the same reason
    `RobotSpeaker`/`RobotCamera`'s other vendor-SDK calls are.
    """
    global _initialized
    with _lock:
        if _initialized:
            return
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize

        ChannelFactoryInitialize(0, interface)
        _initialized = True
