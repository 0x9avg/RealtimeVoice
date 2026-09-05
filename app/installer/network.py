"""Best-effort LAN IP detection for the "open this in your browser" banner."""

from __future__ import annotations

import socket


def detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # No packet is actually sent (UDP, unconnected) — this only asks
            # the OS which local interface/IP would route to the internet,
            # which is the LAN address a phone/laptop on the same network
            # can actually reach.
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
