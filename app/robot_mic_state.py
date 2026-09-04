"""Which single hardware mic (if any) is feeding Gemini right now.

The operator's mic-source picker in the browser (web-src's HomePage, via the
{"type":"mic_source"} WS message in app/main.py) used to only flip a local
variable inside one WebSocket session's closure — closing/reloading that
tab, or a second tab connecting, silently reset capture to that session's
own default, even though the operator had explicitly turned a piece of the
robot's actual hardware on or off, not just this one browser tab's view of
it. This is a process-wide singleton (mirrors app/camera_hub.py's
camera_hub) read and written by every session, so the choice is the
robot's, not any one connection's.

Originally this only ever meant "the head-mic array on or off" (hence the
module's name, kept for now to avoid churning its two importers). It now
covers any hardware source app/robot_audio.py exposes: RobotMic (the vendor
head-mic array) and, optionally, UsbMic (an external USB mic plugged into
the robot's own computer). Only one can be active at a time — both would
end up feeding the same physical robot speaker/HalfDuplexGate, so mixing
them in would just double up (or fight over) what Gemini hears. "browser"
means neither is active — a connected browser tab's own mic (captured
client-side, inherently per-connection) is used instead, if anything.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HardwareMicState:
    active_source: str = "browser"  # "browser" | "robot" | "usb"


robot_mic_state = HardwareMicState()
