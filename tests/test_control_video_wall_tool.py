"""app/tools.py's ControlVideoWallTool — must reuse ONE httpx.AsyncClient
across commands, not open a fresh one per call.

2026-09-06 audit finding: `_send()` used to do
`async with httpx.AsyncClient(base_url=..., timeout=..., headers=...) as client:`
on every single video-wall command — a brand-new TCP handshake to the
wall-agent PC before every "покажи автодорогу..." — exactly the
connection-setup latency app/knowledge.py's KnowledgeBase._http already
avoids for the same reason.
"""

from __future__ import annotations

import httpx

from app.tools import ControlVideoWallTool


async def test_send_reuses_one_http_client_across_multiple_commands() -> None:
    tool = ControlVideoWallTool(base_url="http://wall.example:8093")
    first_client = tool._http

    calls: list[tuple[int, str]] = []

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append((id(tool._http), url))
        return httpx.Response(200, json={"outcome": "accepted"})

    tool._http.post = fake_post  # type: ignore[method-assign]

    result_one = await tool.execute({"kind": "navigate", "nav": "main_menu"})
    result_two = await tool.execute({"kind": "navigate", "nav": "main_menu"})

    assert result_one.ok
    assert result_two.ok
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == id(first_client), (
        "the same client instance must handle every command, not a fresh one per call"
    )
    assert calls[0][1] == "http://wall.example:8093/wall/command"


async def test_send_reports_unreachable_agent_without_crashing() -> None:
    tool = ControlVideoWallTool(base_url="http://wall.example:8093")

    async def fake_post(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    tool._http.post = fake_post  # type: ignore[method-assign]

    result = await tool.execute({"kind": "navigate", "nav": "main_menu"})

    assert not result.ok
    assert "unreachable" in result.text
