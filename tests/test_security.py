"""Regression test for a security hole found in the full audit.

This one was a live, reachable defect on a deployed robot — the traversal
bug handed out `.env` (Gemini API key) to any caller on the same network.

The token/auth-specific regression tests that used to live here (non-ASCII
token handling, the access-log redaction filter) were removed 2026-09-04
along with the ACCESS_TOKEN mechanism itself (item 4 of that day's fixes
list) — this is now an intentionally open API, so there is no token-related
attack surface left to test.
"""

from __future__ import annotations

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


def test_spa_fallback_does_not_serve_files_outside_web_dir() -> None:
    result = _run({}, _TRAVERSAL_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
