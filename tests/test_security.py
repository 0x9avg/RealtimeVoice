"""Regression tests for the security holes found in the full audit.

Each of these was a live, reachable defect on a deployed robot — the
traversal one handed out `.env` (Gemini API key + ACCESS_TOKEN) to any
unauthenticated caller on the same network, which also defeated every other
auth check in the project.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(env_extra: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
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


_TRAVERSAL_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()

# Every one of these used to walk out of web/ and return the real file.
probes = [
    "/..%2f.env",
    "/..%2fpyproject.toml",
    "/..%2fapp%2fconfig.py",
    "/..%2f..%2f..%2f..%2f..%2f..%2fetc%2fhostname",
    "/..%2f..%2f..%2f..%2f..%2f..%2fetc%2fpasswd",
]
with TestClient(m.app, raise_server_exceptions=False) as c:
    for probe in probes:
        r = c.get(probe)
        body = r.text.lstrip()
        assert body.startswith("<!doctype"), f"{probe} leaked a real file: {r.text[:120]!r}"
    # the SPA and its genuine static assets must still work
    assert c.get("/").text.lstrip().startswith("<!doctype")
    assert c.get("/wall").text.lstrip().startswith("<!doctype")
    assert c.get("/capture-worklet.js").status_code == 200
print("OK")
"""


_NON_ASCII_TOKEN_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()

with TestClient(m.app, raise_server_exceptions=False) as c:
    # percent-encoded UTF-8 "Пароль" -- used to raise TypeError inside
    # hmac.compare_digest and surface as a 500 instead of a 401.
    r = c.get("/api/visits?token=%D0%9F%D0%B0%D1%80%D0%BE%D0%BB%D1%8C")
    assert r.status_code == 401, r.status_code

    # same input on the WS gate must be a clean 4401, not a crashed handler
    try:
        with c.websocket_connect("/ws/session?token=%D0%9F%D0%B0%D1%80%D0%BE%D0%BB%D1%8C") as ws:
            ws.receive_json()
        raise SystemExit("WS accepted a bad token")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401, exc.code
print("OK")
"""


def test_spa_fallback_does_not_serve_files_outside_web_dir() -> None:
    result = _run({"ACCESS_TOKEN": ""}, _TRAVERSAL_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_non_ascii_token_is_rejected_not_a_500() -> None:
    result = _run({"ACCESS_TOKEN": "secret123"}, _NON_ASCII_TOKEN_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_access_log_filter_redacts_the_token() -> None:
    """uvicorn's access log writes the full request line, and the token
    rides in the query string for /ws/session and /api/camera/*."""
    from app.main import _RedactTokenFilter

    log_filter = _RedactTokenFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5", "GET", "/ws/session?token=s3cr3t-value", "1.1", 200),
        exc_info=None,
    )
    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    assert "s3cr3t-value" not in rendered, rendered
    assert "token=<redacted>" in rendered, rendered
