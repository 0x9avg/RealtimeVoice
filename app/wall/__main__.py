"""Standalone runner: `uv run python -m app.wall --driver sim --port 8093`.

Runs the wall agent as its own process — the same deployment topology as
production: this runs on (or next to) the machine driving the wall app,
separate from the voice backend, and is reached over plain HTTP via
`WALL_AGENT_URL` (see ../config.py), which can point anywhere on the
network. `--driver sim` needs nothing special and works on any OS. `--driver
pipe` drives the real Factories.exe and requires this process itself to run
on Windows (see drivers.py's module docstring) — on Linux/WSL it fails fast
at startup with a clear error instead of pretending to work.
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from app.wall.drivers import build_driver
from app.wall.server import build_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wall-agent",
        description="Control agent for the MinTrans (Factories.exe) video wall.",
    )
    parser.add_argument(
        "--driver",
        choices=["sim", "pipe"],
        default="sim",
        help="sim = in-memory simulator (any OS); pipe = real app via its named pipe (Windows only)",
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — LAN appliance, token-gated
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument(
        "--token", default=os.environ.get("WALL_AGENT_TOKEN", ""), help="shared secret"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    driver = build_driver(args.driver)
    app = build_app(driver, token=args.token)
    logging.getLogger("app.wall").info(
        "wall agent listening on %s:%d (driver=%s, auth=%s)",
        args.host,
        args.port,
        driver.name,
        "on" if args.token else "off",
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
