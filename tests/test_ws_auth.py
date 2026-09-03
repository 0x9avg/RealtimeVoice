"""/ws/session's token gate (app/main.py) — the one control surface that
used to have no auth check at all. Runs against the real FastAPI/Starlette
WS stack via TestClient; SessionHub.start()/stop() are stubbed out so no
real Gemini connection is attempted.

settings.access_token is read from the ACCESS_TOKEN env var at import time
(app/config.py) into a frozen singleton — pytest's normal in-process test
isolation can't give three scenarios three different token values in the
same run, so each one launches app.main in its own subprocess with the
right ACCESS_TOKEN set beforehand (see `_run` below).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(env_extra: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
    import os

    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


_NO_TOKEN_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()
with TestClient(m.app) as client:
    with client.websocket_connect("/ws/session") as ws:
        assert ws.receive_json()["type"] == "capabilities"
print("OK")
"""

_WRONG_TOKEN_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()
with TestClient(m.app) as client:
    try:
        with client.websocket_connect("/ws/session") as ws:
            ws.receive_json()
        raise SystemExit("connection was NOT rejected")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401, exc.code
print("OK")
"""

_RIGHT_TOKEN_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()
with TestClient(m.app) as client:
    with client.websocket_connect("/ws/session?token=secret123") as ws:
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
    assert r.status_code == 401, r.status_code
    r = client.get("/api/visits", headers={"Authorization": "Bearer secret123"})
    assert r.status_code == 200, r.status_code
    r = client.get("/api/visits?token=secret123")
    assert r.status_code == 200, r.status_code
print("OK")
"""


def test_no_token_configured_allows_connection() -> None:
    result = _run({"ACCESS_TOKEN": ""}, _NO_TOKEN_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_wrong_or_missing_token_rejected_with_4401() -> None:
    result = _run({"ACCESS_TOKEN": "secret123"}, _WRONG_TOKEN_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_correct_token_accepted() -> None:
    result = _run({"ACCESS_TOKEN": "secret123"}, _RIGHT_TOKEN_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_rest_require_token_accepts_bearer_header_and_query_param() -> None:
    result = _run({"ACCESS_TOKEN": "secret123"}, _REST_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
