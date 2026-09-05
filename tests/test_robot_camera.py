"""app/robot_camera.py's RobotCamera._grab_frame — failure-log throttling.

No real DDS/vendor SDK here: `_client` is set directly to a fake stand-in
(bypassing `_ensure_client`), so this tests only the failure-counting/
logging behaviour around GetImageSample(), not the real hardware path.
"""

from __future__ import annotations

import logging

from app.robot_camera import RobotCamera


class _FakeClient:
    def __init__(self, codes: list[int]) -> None:
        self._codes = codes
        self.calls = 0

    def GetImageSample(self):
        code = self._codes[min(self.calls, len(self._codes) - 1)]
        self.calls += 1
        if code == 0:
            return 0, b"\xff\xd8fake-jpeg"
        return code, b""


def test_repeated_failures_are_not_logged_on_every_single_poll(caplog) -> None:
    """2026-09-06 audit finding: a camera that's simply misconfigured (wrong
    PRESENCE_CAMERA_IFACE, or wired to a different onboard computer than
    expected) used to WARNING-log on EVERY failed poll — up to
    presence_camera_fps times a second, forever."""
    camera = RobotCamera(network_interface="eth10")
    camera._client = _FakeClient([1] * 10)  # type: ignore[attr-defined]
    caplog.set_level(logging.WARNING, logger="robot_camera")

    for _ in range(10):
        assert camera._grab_frame() is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "only the first failure should log a warning, not every poll"
    assert camera._consecutive_failures == 10


def test_recovery_after_failures_resets_the_counter_and_logs_once(caplog) -> None:
    camera = RobotCamera(network_interface="eth10")
    camera._client = _FakeClient([1, 1, 1, 0])  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO, logger="robot_camera")

    assert camera._grab_frame() is None
    assert camera._grab_frame() is None
    assert camera._grab_frame() is None
    frame = camera._grab_frame()

    assert frame == b"\xff\xd8fake-jpeg"
    assert camera._consecutive_failures == 0
    recovered = [r for r in caplog.records if "camera.recovered" in r.message]
    assert len(recovered) == 1
