"""Standalone OS process for presence motion scoring.

2026-09-06 UX ask: "Давай разберемся с детекцией человека перед роботом...
Это должен быть отдельный процесс от генерации речи и слушания, чтобы не
возникало задержек из-за этого и он не тормозил основной контур." This
script IS that separate process — spawned by app/presence.py's
SubprocessMotionDetector, it does nothing but run app.presence.MotionDetector
against frames handed to it over stdin, so that scoring work (and anything
that could go wrong with it — a decode hang, a crash, eventually a heavier
ML model) can never share a process, a GIL, or a crash domain with the
FastAPI/asyncio process running the voice pipeline. It does NOT talk to the
camera or DDS itself — CameraHub (app/camera_hub.py) stays the single DDS
poller, frames are piped in from the parent, preserving that "one DDS
consumer" design.

Wire protocol, both directions length/value framed, synchronous blocking
I/O (no asyncio needed — this process only ever does one thing at a time):
  stdin:  4-byte big-endian uint32 length, then that many JPEG bytes.
  stdout: 4-byte big-endian IEEE-754 float32, the 0..1 motion score.
EOF on stdin (the parent closed the pipe — shutting down, or already dead)
ends the loop cleanly, so this process never outlives its parent as a
zombie.

Run via `python -m app.presence_worker`. See app/presence.py's
SubprocessMotionDetector for the parent-side half of this protocol.
"""

from __future__ import annotations

import struct
import sys

from app.presence import MotionDetector

_LENGTH_HEADER = struct.Struct(">I")
_SCORE_REPLY = struct.Struct(">f")


def main() -> None:
    detector = MotionDetector()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        header = stdin.read(_LENGTH_HEADER.size)
        if len(header) < _LENGTH_HEADER.size:
            return  # parent closed stdin — clean shutdown, not an error
        (length,) = _LENGTH_HEADER.unpack(header)
        jpeg = stdin.read(length)
        if len(jpeg) < length:
            return  # truncated write — parent died mid-frame
        score = detector.score(jpeg)
        stdout.write(_SCORE_REPLY.pack(score))
        stdout.flush()


if __name__ == "__main__":
    main()
