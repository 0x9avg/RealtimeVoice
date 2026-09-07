"""Each robot must only expose its own half of the operator surface.

A G1 at a security desk has no video wall; an R1 in a demo hall keeps no
visit journal. Both halves used to be served by both profiles — the tab bar
hid the other robot's tab, but the URL still worked, so `/wall` on a G1
opened a full wall remote whose every button then hung on an unconfigured
wall agent.

Run in subprocesses because `app.config.settings` is a frozen singleton
built from the environment at import time — the same technique
test_security.py uses, and the only way to exercise a real profile choice
end to end rather than a patched attribute.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = """
from unittest.mock import AsyncMock
from starlette.testclient import TestClient
import app.main as m
m.session_hub.start = AsyncMock()
m.session_hub.stop = AsyncMock()

with TestClient(m.app, raise_server_exceptions=False) as c:
    assert c.get("/api/profile").json()["profile"] == {profile!r}

    for path in {gone!r}:
        r = c.get(path)
        assert r.status_code == 404, f"{{path}} answered {{r.status_code}} on {profile}"
        assert "{other_upper}" in r.json()["detail"], r.json()

    r = c.post("/api/wall/command", json={{"kind": "navigate", "nav": "main_menu"}})
    assert r.status_code == {wall_command_status}, r.status_code

    for path in {kept!r}:
        assert c.get(path).status_code == 200, f"{{path}} is missing on {profile}"

    # The SPA itself still answers on the other profile's path: the React
    # router redirects to the home page client-side (web-src/src/App.tsx),
    # which beats a dead end for anyone following an old bookmark.
    assert c.get("/wall").text.lstrip().startswith("<!doctype")
print("OK")
"""


def _run(profile: str, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "ROBOT_PROFILE": profile,
            # Refused instantly, so /api/wall/health cannot sit on a timeout.
            "WALL_AGENT_URL": "http://127.0.0.1:9",
            "PRESENCE_ENABLED": "false",
            "WAKE_WORD_ENABLED": "false",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_g1_does_not_serve_the_video_wall_api() -> None:
    result = _run(
        "g1",
        _SCRIPT.format(
            profile="g1",
            gone=["/api/wall/health"],
            kept=["/api/visits"],
            other_upper="R1",
            wall_command_status=404,
        ),
    )
    assert result.returncode == 0, result.stderr


def test_r1_does_not_serve_the_visit_journal_api() -> None:
    result = _run(
        "r1",
        _SCRIPT.format(
            profile="r1",
            gone=["/api/visits"],
            kept=["/api/wall/health"],
            other_upper="G1",
            # Reachable here; 502 because the fake wall agent refuses the
            # connection, which is the point — the route exists on R1.
            wall_command_status=502,
        ),
    )
    assert result.returncode == 0, result.stderr
