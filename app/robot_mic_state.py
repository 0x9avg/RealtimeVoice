"""Global on/off switch for the robot's physical microphone.

The operator's mic toggle in the browser (web-src's HomePage, via the
{"type":"mic_source"} WS message in app/main.py) used to only flip a local
variable inside one WebSocket session's closure — closing/reloading that
tab, or a second tab connecting, silently reset capture to that session's
own default, even though the operator had explicitly turned the robot's
actual hardware mic on or off, not just this one browser tab's view of it.
This is a process-wide singleton (mirrors app/camera_hub.py's camera_hub)
read and written by every session, so the on/off state is the robot's, not
any one connection's.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RobotMicState:
    enabled: bool = False


robot_mic_state = RobotMicState()
