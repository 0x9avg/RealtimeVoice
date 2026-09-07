"""/ws/session and the REST API (app/main.py, app/api_routes.py) are open —
no token, no auth check at all (item 4 of the 2026-09-04 fixes list removed
the ACCESS_TOKEN mechanism this file used to test the gate for). What's
worth a regression test now is simply that neither surface expects or
requires a credential to work — a stray `?token=...`/`Authorization` header
from an old client, or none at all, must both just work the same way.

Runs against the real FastAPI/Starlette WS stack via TestClient, in a
subprocess so SessionHub.start()/stop() can be stubbed out cleanly without
attempting a real Gemini connection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


_WS_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()
with TestClient(m.app) as client:
    # No token at all -- must just work.
    with client.websocket_connect("/ws/session") as ws:
        assert ws.receive_json()["type"] == "capabilities"
    # A stray token from an old client/bookmark must not be rejected either.
    with client.websocket_connect("/ws/session?token=whatever") as ws:
        assert ws.receive_json()["type"] == "capabilities"
print("OK")
"""

_REST_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()
with TestClient(m.app) as client:
    r = client.get("/api/visits")
    assert r.status_code == 200, r.status_code
    r = client.get("/api/visits", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200, r.status_code
print("OK")
"""


def test_ws_session_needs_no_token() -> None:
    result = _run(_WS_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_rest_api_needs_no_token() -> None:
    # /api/visits is the sample data endpoint here, and it exists only on
    # the G1 profile now (app/api_routes.py's `only_on`) -- so pin the
    # profile rather than let the default make this look like an auth
    # failure when it is really a route that belongs to the other robot.
    result = _run(_REST_SCRIPT, {"ROBOT_PROFILE": "g1"})
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
